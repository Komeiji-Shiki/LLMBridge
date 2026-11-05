"""token 计数和压缩路由：原生上游直接转发，其他上游用本地分词器估算。"""
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from converters.anthropic_openai import convert_anthropic_to_openai_request
from converters.responses_openai import (
    ResponsesRequestError,
    build_responses_error_response,
    convert_responses_to_chat_request,
)
from core.constants import RetryDefaults
from modules.token_counter import estimate_message_tokens, estimate_tokens
from services.protocol_events import error_status
from . import api_routes as _chat_api
from ._direct_api_utils import get_api_key

logger = logging.getLogger(__name__)
router = APIRouter(tags=["token-count"])

# 中转常常没实现这些接口，遇到就退回本地估算；鉴权、限流等错误如实返回。
_UNSUPPORTED_STATUS = (404, 405, 501)


async def _load_request(request: Request) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
    _chat_api._app_state.update_activity()
    _chat_api._check_verification_cooldown()
    try:
        body = await _chat_api._read_request_json_non_blocking(request)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "无效的 JSON 请求体") from exc
    model = body.get("model") if isinstance(body, dict) else None
    if not isinstance(model, str) or not model:
        raise HTTPException(400, "请求缺少 'model' 字段")
    _chat_api._validate_request_api_key(request, model)
    config = await _chat_api._select_endpoint_config_for_model(model)
    return body, model, config if isinstance(config, dict) else {}


async def _forward(body: Dict[str, Any], model: str, config: Dict[str, Any], default_path: str,
                   suffix: str) -> Tuple[Any, int]:
    """把请求转发到原生上游的子路径，返回 (响应 JSON, 状态码)。"""
    key = await get_api_key(
        model, config.get("api_keys") or config.get("api_key"),
        strategy=config.get("api_key_strategy", "round_robin"),
        cooldown_seconds=int(config.get("api_key_cooldown_seconds") or RetryDefaults.API_KEY_COOLDOWN_SECONDS))
    path = (config.get("endpoint_path") or default_path).strip().rstrip("/") + suffix
    upstream = {name: value for name, value in body.items() if name != "stream"}
    upstream["model"] = config.get("model_id") or model
    service = _chat_api._app_state.server.direct_api_service
    raw = b"".join([chunk async for chunk in service.call_api_passthrough(
        base_url=config.get("api_base_url") or "", api_key=key, request_body=upstream,
        endpoint_path=path, stream_override=False)])
    try:
        value = json.loads(raw)
    except ValueError:
        return {"error": {"message": "上游返回的不是 JSON", "type": "api_error", "code": 502}}, 502
    return value, error_status(value) or 200


def _estimate(messages: List[Dict[str, Any]], tools: Optional[List[Any]], model: str) -> int:
    total = estimate_message_tokens(messages, model)
    if tools:
        total += estimate_tokens(json.dumps(tools, ensure_ascii=False), model)
    return total


@router.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(request: Request):
    body, model, config = await _load_request(request)
    if config.get("api_type") == "anthropic_native":
        value, status = await _forward(body, model, config, "/messages", "/count_tokens")
        if status not in _UNSUPPORTED_STATUS:
            return JSONResponse(value, status)
    chat = convert_anthropic_to_openai_request({"max_tokens": 1, **body})
    tokens = await asyncio.to_thread(_estimate, chat.get("messages") or [], chat.get("tools"), model)
    return {"input_tokens": tokens}


@router.post("/v1/responses/input_tokens")
async def responses_input_tokens(request: Request):
    body, model, config = await _load_request(request)
    if config.get("api_type") == "responses_native":
        value, status = await _forward(body, model, config, "/responses", "/input_tokens")
        if status not in _UNSUPPORTED_STATUS:
            return JSONResponse(value, status)
    try:
        chat = convert_responses_to_chat_request(body)
    except ResponsesRequestError as exc:
        return build_responses_error_response(400, str(exc))
    tokens = await asyncio.to_thread(_estimate, chat.get("messages") or [], chat.get("tools"), model)
    return {"object": "response.input_tokens", "input_tokens": tokens}


@router.post("/v1/responses/compact")
async def responses_compact(request: Request):
    body, model, config = await _load_request(request)
    if config.get("api_type") != "responses_native":
        # 服务端压缩的结果是上游加密的，别的协议造不出来；客户端收到 400 会退回本地压缩。
        return build_responses_error_response(400, f"模型 '{model}' 的上游不是 Responses 协议，不支持服务端压缩")
    value, status = await _forward(body, model, config, "/responses", "/compact")
    return JSONResponse(value, status)
