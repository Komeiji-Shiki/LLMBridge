"""转换预览：跑真实的转换链路，把网络换成截获，不向上游发送任何请求。

复用真实处理器，预览和线上行为不会分叉。截获时不保留请求头，里面有上游密钥。
"""
import copy
import json
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from converters.anthropic_openai import convert_anthropic_to_openai_request
from converters.responses_openai import convert_responses_to_chat_request
from core.conversion_report import conversion_notes
from core.request_context import RequestContext, current_request
from services.direct_api_service import DirectAPIService
from services.model_capabilities import sanitize_anthropic_body
from services.provider_capabilities import protocol_name
from utils.upstream_fields import omit_configured_fields

CLIENT_PROTOCOLS = ('chat', 'anthropic', 'responses')
_NOT_SENT = json.dumps({'error': {'message': '转换预览：请求未发送', 'type': 'preview', 'code': 418}})


class _PreviewResponse:
    status = 418
    headers: Dict[str, str] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def read(self):
        return _NOT_SENT.encode()

    async def text(self):
        return _NOT_SENT

    async def json(self):
        return json.loads(_NOT_SENT)


class _CaptureSession:
    """顶替 aiohttp 会话：记录要发的内容，一律回“未发送”。"""

    def __init__(self):
        self.sent: List[Tuple[str, Any]] = []

    def post(self, url, **kwargs):
        self.sent.append((str(url).split('?', 1)[0], kwargs.get('data') or kwargs.get('json')))
        return _PreviewResponse()

    def get(self, _url, **_kwargs):
        return _PreviewResponse()


class _NullMonitor:
    """预览不是真实请求，不进监控和统计。"""

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None

    async def broadcast_to_monitors(self, *_args, **_kwargs):
        return None


def _decode(data: Any) -> Any:
    if isinstance(data, (bytes, bytearray)):
        data = data.decode('utf-8', errors='replace')
    if isinstance(data, str):
        try:
            return json.loads(data)
        except ValueError:
            return data
    return data


def _to_chat(client_protocol: str, body: dict) -> dict:
    if client_protocol == 'anthropic':
        return convert_anthropic_to_openai_request(body)
    if client_protocol == 'responses':
        return convert_responses_to_chat_request(body)
    return copy.deepcopy(body)


def _passthrough(client_protocol: str, body: dict, target: str, config: dict) -> dict:
    upstream = copy.deepcopy(body)
    upstream['model'] = target
    if client_protocol == 'anthropic':
        sanitize_anthropic_body(upstream, target)
    return omit_configured_fields(upstream, config)


async def _run_handler(chat_request: dict, alias: str, config: dict, session: _CaptureSession) -> Optional[str]:
    """跑真实处理器；没有走到发送那一步时返回拒绝原因。"""
    from core.app_state import get_app_state
    from core.config_loader import CONFIG
    from modules.token_counter import estimate_message_tokens, estimate_tokens
    from routes.direct_api_handler import handle_direct_api_request
    from services.image_service import process_image_data
    try:
        response = await handle_direct_api_request(
            openai_req={**chat_request, 'model': alias, 'stream': False},
            model_name=alias,
            endpoint_config=copy.deepcopy(config),
            CONFIG=CONFIG,
            PROCESSED_IMAGE_CACHE=get_app_state().image.PROCESSED_IMAGE_CACHE,
            monitoring_service=_NullMonitor(),
            direct_api_service=DirectAPIService(session),
            estimate_message_tokens_func=estimate_message_tokens,
            estimate_tokens_func=estimate_tokens,
            process_image_data_func=process_image_data,
            full_messages=chat_request.get('messages', []),
        )
    except HTTPException as error:
        return None if session.sent else str(error.detail)
    if isinstance(response, StreamingResponse):
        # 配置了 force_stream 时请求在消费响应时才发出
        async for _chunk in response.body_iterator:
            pass
    return None


async def preview_conversion(client_protocol: str, body: dict, alias: str, config: dict) -> dict:
    """返回发往上游的地址和正文，以及这次转换损失了什么。"""
    upstream_protocol = protocol_name(config)
    target = config.get('model_id') or alias
    # transport_depth 让重试层直接放行：预览不重试，也不碰密钥冷却状态。
    context = RequestContext(model=alias, endpoint=copy.deepcopy(config), transport_depth=1)
    token = current_request.set(context)
    try:
        if client_protocol == upstream_protocol and client_protocol != 'chat':
            return {'sent': False, 'mode': 'passthrough', 'client_protocol': client_protocol,
                    'upstream_protocol': upstream_protocol, 'url': None,
                    'request': _passthrough(client_protocol, body, target, config), 'losses': conversion_notes()}
        session = _CaptureSession()
        rejected = await _run_handler(_to_chat(client_protocol, body), alias, config, session)
        url, data = session.sent[-1] if session.sent else (None, None)
        result = {'sent': False, 'mode': 'converted', 'client_protocol': client_protocol,
                  'upstream_protocol': upstream_protocol, 'url': url, 'request': _decode(data),
                  'losses': conversion_notes()}
        if rejected:
            result['rejected'] = rejected
        return result
    finally:
        current_request.reset(token)
