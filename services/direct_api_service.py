"""
Direct API 调用服务
用于直接调用第三方API（如Google、DeepSeek等官方API）
同时保持与LMArena统计系统的集成

支持两种模式：
1. 转换模式：将OpenAI格式转换为目标API格式（默认）
2. 透传模式：完全透传请求和响应，不做任何转换
"""

import asyncio
import aiohttp
from email.utils import parsedate_to_datetime
import json
import logging
import math
import re
import time
import uuid
import copy
from contextlib import asynccontextmanager
from core.exchange_archive import upstream_snapshot
from core.request_context import UPSTREAM_HEADER_EXCLUSIONS, current_request
from services.provider_capabilities import apply_native_tool_defaults
from services.protocol_events import gemini_termination, merge_gemini_safety
from services.gemini_continuation import GeminiContinuation, InteractionsContinuation
from services.gemini_history import GeminiThoughtHistory, GeminiTurnAssembler, compact_empty_text_parts
from services.gemini_system_prompt import strip_claude_agent_sdk_prefix
from typing import AsyncGenerator, Optional, Dict, Any, List, Tuple

from core.config_loader import CONFIG
from core.constants import GeminiDefaults, TimeoutDefaults, resolve_max_continuation_hops
from core.conversion_report import ConversionRejected, raise_if_strict
from converters.gemini_generate import GeminiRequestError, build_generate_content_body
from converters.gemini_interactions import build_interactions_request_body, interaction_failure, load_signature_history
from converters.gemini_partial_args import PartialCallMerger
from services import upstream_compression
from services.model_capabilities import limits_for
from services.provider_capabilities import provider_name
from utils import fastjson
from utils.upstream_fields import apply_upstream_rules, omit_configured_fields

logger = logging.getLogger(__name__)


_SSE_EVENT_SEPARATOR = re.compile(rb"\r?\n\r?\n")


def _extract_complete_sse_events(buffer: bytes, search_start: int = 0) -> Tuple[List[bytes], bytes]:
    """从字节缓冲中提取所有完整的 SSE 事件（以空行分隔）。

    SSE 规范中事件之间以空行（LF 或 CRLF）分隔；上游分包可能把一个事件
    从中间切断（例如携带签名的 signature_delta），因此这里只输出已收到
    完整终止符的事件，剩余不完整数据留给后续分包拼全。

    返回 (完整事件列表, 剩余不完整数据)。事件内容原样保留（含 event: 行），
    分隔符统一为 LF 空行。

    search_start: 分隔符扫描起点。🔧 性能修复：调用方持续追加数据时，
    已确认无分隔符的前缀无需重复扫描（旧版每个 TCP 分包都对整个缓冲
    全量正则扫描，超大单事件下退化为 O(n²)）；分隔符最长 4 字节，
    调用方应从剩余缓冲末尾回退 3 字节作为下次起点。
    """
    events: List[bytes] = []
    pos = 0
    for match in _SSE_EVENT_SEPARATOR.finditer(buffer, search_start):
        # bytes() 拷贝：兼容 bytearray 缓冲，事件对外始终是不可变 bytes
        event_bytes = bytes(buffer[pos:match.start()]) + b"\n\n"
        if event_bytes.strip():
            events.append(event_bytes)
        pos = match.end()
    if pos == 0:
        # 🔧 无完整事件：原对象直接返回，不做切片拷贝。调用方用 bytearray
        # 原地追加时，这里的 buffer[0:] 切片会把整个缓冲复制一遍，
        # 大事件（base64 图片）下每个 TCP 分包都全量复制，退化为 O(n²)
        return events, buffer
    return events, buffer[pos:]


def _lift_nested_error(error: dict) -> dict:
    """把 error.data 里的说明提到 error 顶层，下游只读 error.message 也能拿到原因。

    WorkBuddy 额度耗尽时返回 {"error": {"data": {"code", "msg", "requestId"}}}，没有 message。
    """
    data = error.get('data')
    if isinstance(data, dict):
        if not error.get('message'):
            error['message'] = data.get('message') or data.get('msg') or ''
        for key in ('code', 'requestId', 'request_id'):
            if data.get(key) is not None:
                error.setdefault(key, data[key])
    if not error.get('message'):
        # 认不出结构时保留原始错误，失败原因不能是空的。
        error['message'] = json.dumps(error, ensure_ascii=False)
    return error


def _normalize_error_for_passthrough(error_json: dict, status_code: int = 0) -> dict:
    """将上游返回的错误 JSON 归一化为 OpenAI 兼容格式，并保留原始 HTTP 状态码。

    处理以下情况：
    - {"error": {"message": "...", ...}}  → 保持原样，注入 _http_status
    - {"error": "some string"}            → 包装为 {"error": {"message": "...", ...}}
    - {"message": "...", "code": ...}    → 包装
    """
    _remember_retry_delay(error_json)
    if not isinstance(error_json, dict):
        return {
            "error": {
                "message": str(error_json),
                "type": "api_error",
                "code": status_code or 500
            },
            "_http_status": status_code if status_code else 500
        }

    error_val = error_json.get('error')
    if isinstance(error_val, dict):
        result = dict(error_json)
        error = _lift_nested_error(dict(error_val))
    elif error_val is not None:
        result = dict(error_json)
        error = {
            "message": str(error_val), "type": "api_error",
            "code": error_json.get('code', status_code or 500)
        }
    else:
        result = {}
        error = {
            "message": error_json.get('message') or error_json.get('msg') or str(error_json),
            "type": "api_error",
            "code": error_json.get('code', status_code or 500),
            # 保留渠道原始错误，含多语言提示、请求编号和其他诊断字段。
            "details": dict(error_json)
        }

    upstream_request_id = (error.get('request_id') or error.get('requestId')
                           or error_json.get('request_id') or error_json.get('requestId'))
    if upstream_request_id:
        error['request_id'] = upstream_request_id
        message = str(error.get('message') or "上游返回错误")
        # 有些客户端只展示 message，因此也在正文里保留可用于查日志的编号。
        if str(upstream_request_id) not in message:
            error['message'] = (
                f"{message} (code: {error.get('code', status_code)}; "
                f"request_id: {upstream_request_id})")
    result['error'] = error
    if status_code >= 400 or not isinstance(error_val, dict):
        result['_http_status'] = status_code or 500
    return result


from services.sse import iter_sse_json_events as _iter_sse_json_events
from services.request_execution import retry_before_output
from core.google_oauth import GoogleOAuthError
from core.upstream_auth import apply_upstream_auth


def _auth_error(error: GoogleOAuthError) -> Dict[str, Any]:
    logger.error(f"[UPSTREAM_AUTH] {error}")
    return {"error": {"message": str(error), "type": "authentication_error", "code": 401}}


def _upstream_timeout() -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(
        total=CONFIG.get("api_call_timeout_seconds", TimeoutDefaults.API_CALL_TIMEOUT),
        sock_read=CONFIG.get("stream_response_timeout_seconds", TimeoutDefaults.STREAM_RESPONSE_TIMEOUT),
        sock_connect=CONFIG.get("download_timeout", {}).get("connect", TimeoutDefaults.DOWNLOAD_CONNECT_TIMEOUT))


_FORWARDED_RESPONSE_HEADERS = ('retry-after', 'x-should-retry')
_FORWARDED_RESPONSE_PREFIXES = ('anthropic-ratelimit-', 'x-ratelimit-')


def _retry_after_seconds(value: str) -> Optional[str]:
    """客户端要整数秒：上游可能给小数或 HTTP 日期。"""
    try:
        return str(max(0, math.ceil(float(value))))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return str(max(0, math.ceil(when.timestamp() - time.time())))


@asynccontextmanager
async def _post_upstream(session, url: str, body: Dict[str, Any], headers: Dict[str, str]):
    """发往上游的 POST。渠道开了请求体压缩时压缩发送，上游拒收压缩就在这一次尝试里改发明文。

    端点配置的 upstream_rules 在这里作用于最终请求体（见 utils.upstream_fields）。开始发送的时间点在
    压缩之后记下（之前算网关构造请求，之后算等上游）；生效的规则和压缩明细记在本次尝试上。
    """
    context = current_request.get()
    endpoint = context.endpoint if context else {}
    applied = []
    if endpoint.get('upstream_rules'):
        # 续传和重试会复用同一份请求体，改副本，规则不会叠加生效
        body = copy.deepcopy(body)
        applied = apply_upstream_rules(body, endpoint, context)
    payload = fastjson.dumps(body)
    data, extra, report = await upstream_compression.prepare(url, payload, endpoint)
    if context:
        context.mark('upstream_post')
        if context.attempts:
            if applied:
                context.attempts[-1]['upstream_rules'] = applied
            if report:
                context.attempts[-1]['upstream_compression'] = report
    async with session.post(url, data=data, headers={**headers, **extra}, timeout=_upstream_timeout()) as response:
        if not (extra and await upstream_compression.rejected(response, report)):
            # 发完就释放正文，否则整份请求体要陪着流式响应活到结束
            payload = data = None
            yield response
            return
    async with session.post(url, data=payload, headers=headers, timeout=_upstream_timeout()) as response:
        report['sent_bytes'] += len(payload)
        report['status'] = 'fallback_failed' if response.status >= 400 else 'fallback_ok'
        if response.status < 400:
            upstream_compression.remember(url, report)
        logger.warning(f"[UPSTREAM_COMPRESSION] 上游不接受 {report['encoding']}，已改发明文（HTTP {response.status}）")
        payload = data = None
        yield response


def _remember_response_headers(response) -> None:
    """保存上游的限流和重试提示，由中间件转给客户端。每次尝试覆盖，重试成功后不留旧值。"""
    context = current_request.get()
    if context is None:
        return
    captured = {}
    for name, value in response.headers.items():
        lower = name.lower()
        if lower in _FORWARDED_RESPONSE_HEADERS or lower.startswith(_FORWARDED_RESPONSE_PREFIXES):
            captured[lower] = value
    if 'retry-after' in captured:
        seconds = _retry_after_seconds(captured.pop('retry-after'))
        if seconds is not None:
            captured['retry-after'] = seconds
    context.upstream_response_headers = captured


def _remember_retry_delay(error_json: Any) -> None:
    """Gemini 把重试等待写在错误正文的 RetryInfo 里，转成客户端认识的响应头。"""
    context = current_request.get()
    error = error_json.get('error') if isinstance(error_json, dict) else None
    details = error.get('details') if isinstance(error, dict) else None
    if context is None or not isinstance(details, list):
        return
    for detail in details:
        delay = detail.get('retryDelay') if isinstance(detail, dict) else None
        if isinstance(delay, str) and delay.endswith('s'):
            seconds = _retry_after_seconds(delay[:-1])
            if seconds is not None:
                context.upstream_response_headers.setdefault('retry-after', seconds)
            return


def _assign_function_call_ids(value: Any) -> None:
    """上游没给 functionCall 编号时就地补上，下游回传的调用才能与服务端历史对上。"""
    for candidate in (value.get("candidates") or []) if isinstance(value, dict) else []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            call = part.get("functionCall")
            if isinstance(call, dict) and not call.get("id"):
                call["id"] = f"call_{uuid.uuid4().hex[:8]}"



class DirectAPIService:
    """Direct API调用服务"""

    @classmethod
    def _apply_context_upstream_headers(cls, request_headers: Dict[str, str]) -> None:
        """转发客户端提供的端到端请求头，不为缺失字段生成替代值；
        再写入模型配置 extra_headers 里固定的上游请求头（同名时覆盖客户端的值）。"""
        context = current_request.get()
        context_headers = getattr(context, 'upstream_headers', None) if context else None
        if isinstance(context_headers, dict):
            for name, value in context_headers.items():
                if str(name).lower() not in UPSTREAM_HEADER_EXCLUSIONS:
                    request_headers[name] = value
        endpoint = getattr(context, 'endpoint', None) if context else None
        extra_headers = endpoint.get('extra_headers') if isinstance(endpoint, dict) else None
        if not isinstance(extra_headers, dict):
            return
        for name, value in extra_headers.items():
            name = str(name)
            for existing in [key for key in request_headers if key.lower() == name.lower()]:
                del request_headers[existing]
            request_headers[name] = str(value)

    @staticmethod
    def split_thinking_content(content: str, separator: str) -> tuple:
        """
        根据分隔符将内容分为思考部分和正文部分
        
        Args:
            content: 完整的响应内容
            separator: 分隔符字符串
        
        Returns:
            (reasoning_content, main_content) 元组
        """
        if not separator or separator not in content:
            return "", content
        
        # 找到分隔符的位置
        separator_index = content.find(separator)
        
        # 分隔符之前的是思考内容
        reasoning_content = content[:separator_index].strip()
        
        # 分隔符之后的是正文（不包括分隔符本身）
        main_content = content[separator_index + len(separator):].strip()
        
        return reasoning_content, main_content
    
    def __init__(self, aiohttp_session: aiohttp.ClientSession = None):
        """
        初始化Direct API服务
        
        Args:
            aiohttp_session: 共享的aiohttp会话（可选）
        """
        self.session = aiohttp_session
        self._own_session = False
        
        if not self.session:
            self.session = aiohttp.ClientSession()
            self._own_session = True
    
    async def close(self):
        """关闭服务"""
        if self._own_session and self.session:
            await self.session.close()
    
    @retry_before_output
    async def call_api(
        self,
        base_url: str,
        api_key: str,
        model: str,
        messages: list,
        stream: bool = False,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        endpoint_path: str = "/chat/completions",
        **kwargs
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        调用第三方API
        
        Args:
            base_url: API基础URL（如 https://api.openai.com/v1）
            api_key: API密钥
            model: 模型名称
            messages: OpenAI格式的消息列表
            stream: 是否流式响应
            temperature: 温度参数
            top_p: top_p参数
            max_tokens: 最大token数
            **kwargs: 其他参数
        
        Yields:
            响应数据块
        """
        # 构建请求URL（支持自定义端点路径）
        endpoint_path = (endpoint_path or "/chat/completions").strip()
        if not endpoint_path.startswith("/"):
            endpoint_path = "/" + endpoint_path
        endpoint = f"{base_url.rstrip('/')}{endpoint_path}"
        
        # 构建请求体
        request_body = {
            "model": model,
            "messages": messages,
            "stream": stream
        }
        
        # 添加可选参数
        if temperature is not None:
            request_body["temperature"] = temperature
        if top_p is not None:
            request_body["top_p"] = top_p
        if max_tokens is not None:
            request_body["max_tokens"] = max_tokens
        
        # 合并其他参数
        request_body.update(kwargs)
        
        # 构建请求头
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        self._apply_context_upstream_headers(headers)
        
        logger.info(f"[DIRECT_API] 调用API: {endpoint}")
        logger.info(f"[DIRECT_API] 模型: {model}, 流式: {stream}")
        
        try:
            await apply_upstream_auth(headers, api_key)
            context = current_request.get()
            if context:
                request_body = apply_native_tool_defaults(request_body, context.endpoint, 'chat')
                context.upstream_request = copy.deepcopy(request_body)
            # 序列化结果不留在局部变量里：发完就该释放，否则整份请求体要陪着流式响应活到结束
            async with _post_upstream(self.session, endpoint, request_body, headers) as response:
                _remember_response_headers(response)
                # 检查响应状态
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"[DIRECT_API] API调用失败: {response.status} - {error_text}")
                    try:
                        # 尝试将错误解析为JSON，并归一化 error 格式
                        error_json = json.loads(error_text)
                        yield _normalize_error_for_passthrough(
                            error_json, response.status)
                    except json.JSONDecodeError:
                        # 如果不是JSON，就封装成一个OpenAI风格的错误格式
                        yield {
                            "error": {
                                "message": error_text,
                                "type": "api_error",
                                "code": response.status
                            }
                        }
                    return
                
                if stream:
                    # 流式响应（公共 SSE 解析器，支持超长行与跨 chunk UTF-8）
                    async for chunk in _iter_sse_json_events(response, "DIRECT_API"):
                        yield chunk
                else:
                    # 非流式响应
                    response_data = await response.json()
                    yield response_data
        
        except asyncio.TimeoutError:
            logger.error(f"[DIRECT_API] 请求超时 (模型: {model})")
            yield {
                "error": {
                    "message": "Request timed out while waiting for API response",
                    "type": "timeout_error"
                }
            }
        except GoogleOAuthError as e:
            yield _auth_error(e)
        except aiohttp.ClientError as e:
            logger.error(f"[DIRECT_API] 网络请求失败: {e}")
            yield {
                "error": {
                    "message": f"Network error: {str(e)}",
                    "type": "network_error"
                }
            }
        except Exception as e:
            logger.error(f"[DIRECT_API] 未知错误: {e}", exc_info=True)
            yield {
                "error": {
                    "message": f"Unexpected error: {str(e)}",
                    "type": "internal_error"
                }
            }
    
    async def call_api_non_stream(
        self,
        base_url: str,
        api_key: str,
        model: str,
        messages: list,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        endpoint_path: str = "/chat/completions",
        **kwargs
    ) -> Dict[str, Any]:
        """
        调用第三方API（非流式）
        
        Returns:
            完整的响应字典
        """
        async for chunk in self.call_api(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=messages,
            stream=False,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            endpoint_path=endpoint_path,
            **kwargs
        ):
            return chunk
        
        return {"error": True, "message": "No response received"}
    
    @retry_before_output
    async def call_gemini_native_api(
        self,
        api_key: str,
        model: str,
        messages: list,
        stream: bool = False,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        base_url: Optional[str] = None,
        thinking_config: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        extra_body: Optional[Dict[str, Any]] = None,
        response_format: Optional[Dict[str, Any]] = None,
        stop_sequences: Optional[List[str]] = None,
        api_key_auth_mode: Optional[str] = None,
        generation_extras: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        调用Gemini原生API（非OpenAI兼容格式）

        Args:
            api_key: Google API密钥
            model: 模型名称（如gemini-2.5-pro）
            messages: OpenAI格式的消息列表（需要转换）
            stream: 是否流式响应
            temperature: 温度参数
            top_p: top_p参数
            max_tokens: 最大token数
            base_url: 自定义API基础地址（版本路径由用户填写，留空使用Google官方 /v1beta 地址）
            thinking_config: 思维链配置（可选）
            tools: OpenAI 格式的 tools 列表（将转换为 Gemini functionDeclarations）
            tool_choice: OAI tool_choice（将转换为 Gemini toolConfig）
            extra_body: 额外的 Gemini 原生请求体键值对（直接合并到 request_body）。
                使用显式参数而非 **kwargs，消除配置键与命名形参碰撞导致 TypeError 的风险。
            api_key_auth_mode: 显式指定鉴权方式；未指定时读取当前模型配置。

        Yields:
            响应数据块（Gemini原生格式）
        """
        # 构建Gemini API URL
        method = "streamGenerateContent" if stream else "generateContent"
        
        # 修复：流式请求必须添加 alt=sse 参数，让 Gemini 返回标准 SSE 格式
        sse_param = "?alt=sse" if stream else ""

        def _redact(text: str) -> str:
            """脱敏异常消息中的 API key。"""
            return text.replace(api_key, '***') if api_key else text

        def _termination_error(value):
            reason, error = gemini_termination(value)
            context = current_request.get()
            if reason and context:
                context.outcome['stop_reason'] = reason
            safety = merge_gemini_safety(context.outcome.get('safety'), value) if context else None
            if safety:
                context.outcome['safety'] = safety
            return error

        # 自定义地址的版本路径由用户决定，仅在未配置地址时使用官方默认值。
        configured_base = (base_url or GeminiDefaults.BASE_URL).rstrip("/")
        endpoint = f"{configured_base}/models/{model}:{method}{sse_param}"
        
        # 2026-10-26 起新建的付费项目默认保存请求。官方地址下默认不保存，需要时用自定义参数打开；
        # 中转可能不认识这个字段，不主动发。
        request_context = current_request.get()
        official = bool(request_context and provider_name(request_context.endpoint) == 'gemini')
        try:
            request_body = await build_generate_content_body(
                messages, self.session, limits_for(model).function_response_media,
                temperature=temperature, top_p=top_p, max_tokens=max_tokens, thinking_config=thinking_config,
                tools=tools, tool_choice=tool_choice, response_format=response_format,
                stop_sequences=stop_sequences, generation_extras=generation_extras,
                store=False if official else None, extra_body=extra_body)
        except GeminiRequestError as exc:
            yield {"error": {"message": str(exc), "type": "invalid_request_error"}}
            return

        logger.info(f"[GEMINI_NATIVE] 调用Gemini原生API: {_redact(endpoint)}")
        logger.info(f"[GEMINI_NATIVE] 模型: {model}, 流式: {stream}")
        if temperature is not None:
            logger.info(f"[GEMINI_NATIVE] temperature: {temperature}")
        if top_p is not None:
            logger.info(f"[GEMINI_NATIVE] topP: {top_p}")
        if max_tokens is not None:
            logger.info(f"[GEMINI_NATIVE] maxOutputTokens: {max_tokens}")

        continuation = GeminiContinuation(
            resolve_max_continuation_hops(CONFIG, request_context.endpoint if request_context else None))
        # Vertex 流式参数分片在这里拼回完整调用，转换链路只处理完整的 functionCall
        partial_calls = PartialCallMerger()

        try:
            context = current_request.get()
            history = assembler = None
            if context:
                request_body = apply_native_tool_defaults(request_body, context.endpoint, 'gemini')
                omit_configured_fields(request_body, context.endpoint)
                strip_claude_agent_sdk_prefix(request_body, context.endpoint, model)
                # 客户端回传的签名优先（已带签名的轮次不会被覆盖），缺失时按服务端历史补回
                history = GeminiThoughtHistory(context, context.endpoint, request_body)
                assembler = GeminiTurnAssembler()
                restored = await history.restore(request_body)
                if restored:
                    logger.info(f"[GEMINI_NATIVE] 已按服务端历史补回 {restored} 个思考签名")
            compact_empty_text_parts(request_body.get("contents"))
            if context:
                if context.gemini_signature_recovery is not None:
                    request_body = context.gemini_signature_recovery.prepare(request_body)
                context.upstream_request = await asyncio.to_thread(upstream_snapshot, request_body)
                # 重试复用同一个上下文，上一次尝试的过滤依据不能算到这一次
                context.outcome.pop('safety', None)
            upstream_body = request_body
            headers = {"Content-Type": "application/json"}
            # 与 Google SDK 和原生透传一致，使用请求头传递上游密钥。
            if api_key:
                headers["x-goog-api-key"] = api_key
            # 转换协议后仍保留客户端的会话标识和其他端到端请求头。
            self._apply_context_upstream_headers(headers)
            await apply_upstream_auth(headers, api_key, api_key_auth_mode)
            response_data = None
            while True:
                async with _post_upstream(self.session, endpoint, upstream_body, headers) as response:
                    _remember_response_headers(response)
                    # 检查响应状态
                    if response.status != 200:
                        error_text = await response.text()
                        error_message = error_text.strip() or f"Gemini API 返回 HTTP {response.status}（响应正文为空）"
                        if continuation.can_fallback_to_truncated(response.status, error_message):
                            logger.warning(
                                f"[GEMINI_NATIVE] 第 {continuation.hops} 次续传被上游拒绝，保留前序输出并按截断收尾: {error_message[:200]}")
                            if stream:
                                chunk = continuation.fallback_chunk()
                                _termination_error(chunk)
                                if assembler is not None:
                                    assembler.feed(chunk)
                                yield chunk
                            else:
                                response_data = continuation.fallback_response(response_data)
                                _termination_error(response_data)
                            break
                        logger.error(f"[GEMINI_NATIVE] API调用失败: {response.status} - {error_message}")
                        try:
                            error_json = json.loads(error_text)
                            yield continuation.with_usage(_normalize_error_for_passthrough(
                                error_json, response.status))
                        except json.JSONDecodeError:
                            yield continuation.with_usage({
                                "error": {
                                    "message": error_message,
                                    "type": "api_error",
                                    "code": response.status
                                }
                            })
                        return

                    if stream:
                        # 流式响应（公共 SSE 解析器；Gemini 兼容无前缀的裸 JSON 行）
                        async for chunk in _iter_sse_json_events(response, "GEMINI_NATIVE", parse_bare_json=True):
                            continuation.feed(chunk)
                            # 终止块本身可能带有最后一段内容和 usageMetadata：先原样交给
                            # 调用方转换计费，再单独发出错误事件
                            error = _termination_error(chunk)
                            chunk = partial_calls.merge_chunk(chunk)
                            _assign_function_call_ids(chunk)
                            if assembler is not None:
                                assembler.feed(chunk)
                            yield chunk
                            if error:
                                yield {'error': error}
                                return
                    else:
                        # 非流式响应：错误与原始响应合并，调用方仍可读取 usageMetadata
                        response_data = continuation.merge_response(await response.json())
                        error = _termination_error(response_data)
                        _assign_function_call_ids(response_data)
                        if error:
                            yield {**response_data, 'error': error}
                            return

                token = continuation.next_hop()
                if token is None:
                    break
                logger.info(f"[GEMINI_NATIVE] 上游达到单次输出上限，开始第 {continuation.hops} 次续传")
                # 已生成内容由 token 携带，不能追加到 contents
                upstream_body = {**request_body, "continuationToken": token}

            if history is not None:
                if not stream:
                    assembler.feed(response_data)
                await history.remember(request_body.get("contents"), assembler)
            if not stream:
                yield response_data

        except asyncio.TimeoutError:
            logger.error(f"[GEMINI_NATIVE] 请求超时 (模型: {model})")
            yield continuation.with_usage({
                "error": {
                    "message": "Request timed out while waiting for Gemini API response",
                    "type": "timeout_error"
                }
            })
        except GoogleOAuthError as e:
            yield _auth_error(e)
        except aiohttp.ClientError as e:
            # 网络异常可能包含请求信息，统一清除密钥后再返回。
            safe_err = _redact(str(e))
            logger.error(f"[GEMINI_NATIVE] 网络请求失败: {safe_err}")
            yield continuation.with_usage({
                "error": {
                    "message": f"Network error: {safe_err}",
                    "type": "network_error"
                }
            })
        except Exception as e:
            safe_err = _redact(str(e))
            logger.error(f"[GEMINI_NATIVE] 未知错误: {safe_err}", exc_info=True)
            yield continuation.with_usage({
                "error": {
                    "message": f"Unexpected error: {safe_err}",
                    "type": "internal_error"
                }
            })

    @retry_before_output
    async def call_gemini_interactions_api(
        self,
        api_key: str,
        model: str,
        messages: list,
        stream: bool = False,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        base_url: Optional[str] = None,
        thinking_config: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        response_format: Optional[Any] = None,
        stop_sequences: Optional[List[str]] = None,
        extra_body: Optional[Dict[str, Any]] = None,
        api_key_auth_mode: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """调用 Gemini Interactions API（基础地址后追加 /interactions）。

        与 call_gemini_native_api 平行：OpenAI 消息在内部转换为 interactions
        无状态步骤数组（store=false，签名前缀匹配注入由转换器完成），
        流式请求带 alt=sse，事件逐个 yield（含 event_type 字段的 data JSON）。

        Args:
            api_key: Google API密钥
            model: 上游模型ID
            messages: OpenAI格式的消息列表（需要转换）
            stream: 是否流式响应
            temperature: 温度参数
            top_p: top_p参数（interactions 未确认支持，转换器忽略）
            max_tokens: 最大token数
            base_url: 自定义API基础地址（如 https://generativelanguage.googleapis.com/v1beta
                或 http://127.0.0.1:7861/v1；版本路径由用户填写，留空使用Google官方 /v1beta 地址）
            thinking_config: 思维链配置（thinkingLevel 映射为 thinking_level）
            tools: OpenAI 格式的 tools 列表（转换为 Interactions 扁平工具定义）
            tool_choice: OAI tool_choice（仅 "none" 受支持，不传 tools）
            response_format: OpenAI 结构化输出配置
            stop_sequences: OpenAI stop 列表，映射为 generation_config.stop_sequences
            extra_body: 额外的请求体键值对（直接合并到 request_body）
            api_key_auth_mode: 显式指定鉴权方式；未指定时读取当前模型配置。

        Yields:
            流式：interactions SSE 事件 dict；非流式：interaction 对象 dict；
            出错时：_normalize_error_for_passthrough 格式的错误 dict
        """
        # Interactions 使用 x-goog-api-key 请求头，版本路径沿用用户填写的基础地址。
        sse_param = "?alt=sse" if stream else ""

        def _redact(text: str) -> str:
            """脱敏异常消息中的 API key。"""
            return text.replace(api_key, "***") if api_key else text

        configured_base = (base_url or GeminiDefaults.BASE_URL).rstrip("/")
        endpoint = f"{configured_base}/interactions{sse_param}"

        await load_signature_history(messages)
        request_body = build_interactions_request_body(
            model=model,
            messages=messages,
            stream=stream,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            thinking_config=thinking_config,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            stop_sequences=stop_sequences,
            extra_body=extra_body,
        )
        request_context = current_request.get()
        try:
            raise_if_strict(request_context.endpoint if request_context else {})
        except ConversionRejected as exc:
            yield {"error": {"message": str(exc), "type": "invalid_request_error", "code": 400}}
            return

        continuation = InteractionsContinuation(
            resolve_max_continuation_hops(CONFIG, request_context.endpoint if request_context else None))

        logger.info(f"[GEMINI_INTERACTIONS] 调用Interactions API: {_redact(endpoint)}")
        logger.info(f"[GEMINI_INTERACTIONS] 模型: {model}, 流式: {stream}, 步骤数: {len(request_body.get('input', []))}")
        if request_body.get("system_instruction"):
            logger.info(f"[GEMINI_INTERACTIONS] system_instruction: {len(request_body['system_instruction'])} 字符")
        if request_body.get("generation_config"):
            logger.info(f"[GEMINI_INTERACTIONS] generation_config: {request_body['generation_config']}")

        try:
            context = current_request.get()
            if context:
                request_body = apply_native_tool_defaults(request_body, context.endpoint, 'interactions')
                omit_configured_fields(request_body, context.endpoint)
                strip_claude_agent_sdk_prefix(request_body, context.endpoint, model)
                context.upstream_request = await asyncio.to_thread(upstream_snapshot, request_body)
            upstream_body = request_body
            headers = {
                "Content-Type": "application/json"
            }
            if api_key:
                headers["x-goog-api-key"] = api_key
            self._apply_context_upstream_headers(headers)
            await apply_upstream_auth(headers, api_key, api_key_auth_mode)
            response_data = None
            while True:
                interrupted = None
                try:
                    async with _post_upstream(self.session, endpoint, upstream_body, headers) as response:
                        _remember_response_headers(response)
                        if response.status != 200:
                            error_text = await response.text()
                            if continuation.can_fallback_to_truncated(response.status, error_text):
                                logger.warning(
                                    f"[GEMINI_INTERACTIONS] 第 {continuation.hops} 次续传被上游拒绝，保留前序输出并按截断收尾: {error_text[:200]}")
                                if stream:
                                    yield continuation.fallback_event()
                                else:
                                    response_data = continuation.fallback_response(response_data)
                                break
                            logger.error(f"[GEMINI_INTERACTIONS] API调用失败: {response.status} - {error_text}")
                            try:
                                error_json = json.loads(error_text)
                                yield continuation.with_usage(_normalize_error_for_passthrough(
                                    error_json, response.status))
                            except json.JSONDecodeError:
                                yield continuation.with_usage({
                                    "error": {
                                        "message": error_text,
                                        "type": "api_error",
                                        "code": response.status
                                    }
                                })
                            return

                        if stream:
                            # 流式：复用公共 SSE 解析器。interactions 官方是 event:+data: 双行，
                            # event: 行会被跳过；parse_bare_json=True 兼容反代发裸 JSON 行。
                            async for event in _iter_sse_json_events(
                                response, "GEMINI_INTERACTIONS", parse_bare_json=True):
                                if continuation.feed(event):
                                    yield event
                        else:
                            response_data = continuation.merge_response(await response.json())
                            failure = interaction_failure(response_data)
                            if failure:
                                yield failure
                                return
                except (asyncio.TimeoutError, aiohttp.ClientError) as error:
                    interrupted = error

                token = continuation.next_hop()
                if token is None:
                    if interrupted is not None:
                        raise interrupted
                    break
                logger.info(f"[GEMINI_INTERACTIONS] 上游到达单次输出上限或连接中断，开始第 {continuation.hops} 次续传")
                if continuation.step_open:
                    # 断线时步骤还没收尾，转换器要靠 step.stop 才会把它写完。
                    continuation.step_open = False
                    yield {"event_type": "step.stop"}
                # 已生成内容由 token 携带，不能追加到 input
                upstream_body = {**request_body, "continuation_token": token}

            if not stream:
                yield response_data

        except asyncio.TimeoutError:
            logger.error(f"[GEMINI_INTERACTIONS] 请求超时 (模型: {model})")
            yield continuation.with_usage({
                "error": {
                    "message": "Request timed out while waiting for Gemini Interactions API response",
                    "type": "timeout_error"
                }
            })
        except GoogleOAuthError as e:
            yield _auth_error(e)
        except aiohttp.ClientError as e:
            safe_err = _redact(str(e))
            logger.error(f"[GEMINI_INTERACTIONS] 网络请求失败: {safe_err}")
            yield continuation.with_usage({
                "error": {
                    "message": f"Network error: {safe_err}",
                    "type": "network_error"
                }
            })
        except Exception as e:
            safe_err = _redact(str(e))
            logger.error(f"[GEMINI_INTERACTIONS] 未知错误: {safe_err}", exc_info=True)
            yield continuation.with_usage({
                "error": {
                    "message": f"Unexpected error: {safe_err}",
                    "type": "internal_error"
                }
            })

    @staticmethod
    def _format_error_bytes(error_response: dict, is_stream: bool) -> bytes:
        """将错误响应格式化为合适的字节输出。
        流式模式：包装为 SSE 格式（data: + [DONE]），确保流式处理器能正确转发。
        非流式模式：纯 JSON，调用方直接拼接后解析。
        """
        error_json_str = json.dumps(error_response, ensure_ascii=False)
        if is_stream:
            return f"data: {error_json_str}\n\ndata: [DONE]\n\n".encode('utf-8')
        else:
            return error_json_str.encode('utf-8')

    async def _passthrough_headers(self, endpoint_path: str, api_key: str,
                                   headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        request_headers = {
            "Content-Type": "application/json"
        }

        # API Key 可选：本地反代场景可能无需认证
        if api_key:
            # 默认 OpenAI Bearer 方式
            request_headers["Authorization"] = f"Bearer {api_key}"

            # Anthropic /messages 兼容：自动附加 x-api-key 和版本头
            endpoint_lower = endpoint_path.lower()
            if endpoint_lower.endswith(("/messages", "/messages/count_tokens")):
                request_headers["x-api-key"] = api_key
                request_headers.setdefault("anthropic-version", "2023-06-01")

        self._apply_context_upstream_headers(request_headers)

        # 合并额外的请求头
        if headers:
            request_headers.update(headers)
            if 'x-goog-api-key' in headers:
                request_headers['x-goog-api-key'] = api_key
                request_headers.pop('Authorization', None)

        await apply_upstream_auth(request_headers, api_key)
        return request_headers

    async def open_passthrough(self, method: str, base_url: str, api_key: str, endpoint_path: str,
                               headers: Optional[Dict[str, str]] = None):
        """转发没有请求体的原生请求（查询、取消、删除），返回尚未读取的上游响应，调用方负责 release。"""
        request_headers = await self._passthrough_headers(endpoint_path, api_key, headers)
        request_headers.pop("Content-Type", None)
        response = await self.session.request(method, f"{base_url.rstrip('/')}{endpoint_path}",
                                              headers=request_headers, timeout=_upstream_timeout())
        _remember_response_headers(response)
        return response

    @retry_before_output(output_bytes=True)
    async def call_api_passthrough(
        self,
        base_url: str,
        api_key: str,
        request_body: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
        endpoint_path: str = "/chat/completions",
        stream_override: Optional[bool] = None,
    ) -> AsyncGenerator[bytes, None]:
        """
        透传模式：完全透传请求和响应
        
        Args:
            base_url: API基础URL
            api_key: API密钥
            request_body: 原始请求体（不做任何转换）
            headers: 额外的请求头（可选）
        
        Yields:
            原始响应字节流
        """
        # 构建请求URL（支持自定义端点路径）
        endpoint_path = (endpoint_path or "/chat/completions").strip()
        if not endpoint_path.startswith("/"):
            endpoint_path = "/" + endpoint_path
        endpoint = f"{base_url.rstrip('/')}{endpoint_path}"

        is_stream = request_body.get("stream", False) if stream_override is None else stream_override
        try:
            request_headers = await self._passthrough_headers(endpoint_path, api_key, headers)
        except GoogleOAuthError as e:
            yield self._format_error_bytes(_auth_error(e), is_stream)
            return
        
        logger.info(f"[DIRECT_API_PASSTHROUGH] 透传模式调用API: {endpoint}")
        logger.info(f"[DIRECT_API_PASSTHROUGH] 模型: {request_body.get('model')}, 流式: {is_stream}")

        # ── 诊断：检查图片 media_type ──
        for msg_idx, msg in enumerate(request_body.get("messages", [])):
            content = msg.get("content")
            if isinstance(content, list):
                for blk_idx, block in enumerate(content):
                    if isinstance(block, dict) and block.get("type") == "image":
                        src = block.get("source", {})
                        mt = src.get("media_type", "N/A")
                        dl = len(src.get("data", ""))
                        logger.info(
                            f"[DIRECT_API_PASSTHROUGH] 消息[{msg_idx}] 图片块[{blk_idx}]: "
                            f"source.type={src.get('type')!r}, media_type={mt!r}, data_len={dl}")
                        if ";" in str(mt):
                            logger.error(
                                f"[DIRECT_API_PASSTHROUGH] ❌ 检测到异常的 media_type: {mt!r} "
                                f"(包含分号，将导致上游400错误)")

        try:
            async with _post_upstream(self.session, endpoint, request_body, request_headers) as response:
                _remember_response_headers(response)
                # 检查响应状态
                if response.status != 200:
                    error_body = await response.read()
                    error_text = error_body.decode(errors='ignore')
                    logger.error(f"[DIRECT_API_PASSTHROUGH] API调用失败: {response.status} - {error_text}")
                    
                    try:
                        # 检查原始错误是否为有效JSON，并归一化 error 格式
                        error_json = json.loads(error_text)
                        normalized = _normalize_error_for_passthrough(
                            error_json, response.status)
                        yield self._format_error_bytes(normalized, is_stream)
                    except json.JSONDecodeError:
                        # 如果不是JSON，则封装成OpenAI兼容的错误格式
                        error_response = {
                            "error": {
                                "message": error_text,
                                "type": "api_error",
                                "code": response.status
                            }
                        }
                        yield self._format_error_bytes(error_response, is_stream)
                    return
                
                # 🔧 关键修复：检查Content-Type，防止Cloudflare等返回HTML被当作JSON/SSE透传
                content_type = response.headers.get('Content-Type', '').lower()
                if 'text/html' in content_type:
                    html_body = await response.text()
                    snippet = html_body[:300].replace('\n', ' ')
                    logger.error(f"[DIRECT_API_PASSTHROUGH] 上游返回HTML而非JSON/SSE: {snippet}...")
                    error_response = {
                        "error": {
                            "message": f"上游返回HTML页面（可能被Cloudflare拦截或WAF阻断），请稍后重试。",
                            "type": "upstream_html_error",
                            "code": 502
                        }
                    }
                    yield self._format_error_bytes(error_response, is_stream)
                    return
                if is_stream:
                    # 按 SSE 事件边界（空行）缓冲转发：一个事件必须完整送达。
                    # 旧实现按"最后一个换行符"切分并给每个片段强补 \n\n 终止符，
                    # 会把被 TCP 分包切断的事件拆成两个残缺事件。携带思维链
                    # 签名的 signature_delta 一旦被切断，客户端便拿不到完整签名，
                    # 下一轮回传历史时会被上游 400 拒绝（signature: undefined）。
                    # 🔧 bytearray 缓冲：bytes 的 += 每次全量复制（CPython 仅对
                    # str 有原地优化），bytearray 的 += 是真正的原地追加
                    buffer = bytearray()
                    scan_start = 0
                    async for chunk, _ in response.content.iter_chunks():
                        if not chunk:
                            continue
                        buffer += chunk
                        events, buffer = _extract_complete_sse_events(buffer, scan_start)
                        # 剩余缓冲已确认无完整分隔符；下次从末尾回退 3 字节
                        # 开始扫（分隔符最长 \r\n\r\n = 4 字节，防跨包切断）
                        scan_start = max(0, len(buffer) - 3)
                        for event_bytes in events:
                            yield event_bytes

                    # 流结束后输出残留数据（上游未按规范以空行收尾的最后一段）
                    if buffer.strip():
                        yield bytes(buffer.rstrip(b"\r\n")) + b"\n\n"
                else:
                    # 🔧 非流式 JSON：原样透传字节，不做 SSE 事件切分。
                    # 旧版非流式也走事件切分：大 JSON 在 buffer 中 O(n²) 拼接，
                    # 还会被追加 \n\n、CRLF 被改写 —— 正是下游
                    # json.loads(text[:e.pos]) 截断 hack 存在的根源。
                    async for chunk, _ in response.content.iter_chunks():
                        if chunk:
                            yield chunk

        except asyncio.TimeoutError:
            logger.error(f"[DIRECT_API_PASSTHROUGH] 请求超时 (模型: {request_body.get('model')})")
            error_response = {
                "error": {
                    "message": "Request timed out while waiting for API response",
                    "type": "timeout_error"
                }
            }
            yield self._format_error_bytes(error_response, is_stream)
        except aiohttp.ClientError as e:
            logger.error(f"[DIRECT_API_PASSTHROUGH] 网络请求失败: {e}")
            error_response = {
                "error": {
                    "message": f"Network error: {str(e)}",
                    "type": "network_error"
                }
            }
            yield self._format_error_bytes(error_response, is_stream)
        except Exception as e:
            logger.error(f"[DIRECT_API_PASSTHROUGH] 未知错误: {e}", exc_info=True)
            error_response = {
                "error": {
                    "message": f"Unexpected error: {str(e)}",
                    "type": "internal_error"
                }
            }
            yield self._format_error_bytes(error_response, is_stream)
    
    def calculate_cost(
        self, input_tokens: int, output_tokens: int, pricing: Dict[str, Any],
        cached_tokens: int = 0, cache_write_tokens: int = 0,
        cache_write_1h_tokens: int = 0, upstream_usage: Optional[dict] = None,
        tier_input_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """复用共享计费逻辑，输入总量已经包含缓存读取和写入。"""
        from utils.api_pricing import calculate_api_cost, uses_explicit_cache
        from core.request_context import current_request
        try:
            context = current_request.get()
            explicit = uses_explicit_cache(context.upstream_request) if context else False
            return calculate_api_cost(input_tokens, output_tokens, pricing, cached_tokens,
                                      cache_write_tokens, cache_write_1h_tokens, upstream_usage, explicit,
                                      tier_input_tokens)
        except Exception as exc:
            logger.error(f"[DIRECT_API] 成本计算失败: {exc}")
            return {"input_tokens": input_tokens, "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens, "error": str(exc)}


# 全局服务实例（将在api_server.py中初始化）
direct_api_service: Optional[DirectAPIService] = None


def get_direct_api_service(aiohttp_session: aiohttp.ClientSession = None) -> DirectAPIService:
    """
    获取Direct API服务实例
    """
    global direct_api_service
    if direct_api_service is None:
        direct_api_service = DirectAPIService(aiohttp_session=aiohttp_session)
    elif aiohttp_session is not None and direct_api_service.session is not aiohttp_session:
        # 注意：同步函数无法 await 关闭旧的自建 session，直接切换为共享连接池；
        # 正常启动流程会先调 init_direct_api_service，不会走到这个分支
        direct_api_service.session = aiohttp_session
        direct_api_service._own_session = False
    return direct_api_service


def init_direct_api_service(aiohttp_session: aiohttp.ClientSession) -> DirectAPIService:
    """
    初始化Direct API服务实例（应在应用启动时调用）
    """
    global direct_api_service
    direct_api_service = DirectAPIService(aiohttp_session=aiohttp_session)
    logger.info("[DIRECT_API] ✅ Direct API服务已初始化（使用共享连接池）")
    return direct_api_service
