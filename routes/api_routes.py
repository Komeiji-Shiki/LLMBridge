"""
核心API路由入口
将请求分发到对应的处理模块。
当前以 Direct API 为主路径；LMArena 分支已弃用，但仍保留兼容能力。

重构说明：
- Anthropic ↔ OpenAI 协议转换已拆分到 converters/anthropic_openai.py
- 依赖通过模块 import 与 AppState 单例获取，不再使用长参数链注入
- 人机验证状态统一读写 AppState.server（旧版传布尔标量导致状态失效）
"""
import asyncio
import copy
from core.auth_observer import observe_authenticated_request
from core.request_context import current_request
import json
import logging
from core.endpoint_observer import observe_endpoint
from utils.anthropic_params import set_output_effort
import secrets
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, Response

# 拆分的路由模块
from .models_api import get_models, get_gemini_models
from .gemini_v1beta_api import gemini_native_api
from .direct_api_handler import handle_direct_api_request
from .lmarena_handler import handle_lmarena_request
from ._direct_api_anthropic import apply_auto_cache_control
from ._direct_api_utils import (
    get_round_robin_api_key,
    is_error_json,
    estimate_message_tokens_non_blocking,
    estimate_text_tokens_non_blocking,
)
from modules.token_counter import estimate_message_tokens, estimate_tokens

# 协议转换
from converters.anthropic_openai import (
    convert_anthropic_to_openai_request,
    build_anthropic_error_payload,
    extract_anthropic_response_content,
    extract_anthropic_sse_content,
    flush_anthropic_sse_state,
    convert_openai_non_stream_response_to_anthropic,
    build_anthropic_streaming_response,
    sanitize_anthropic_thinking_blocks,
)
from utils.json_unescape import AnthropicSSEToolArgsRewriter

# 全局配置与状态
from core.config_loader import (
    CONFIG,
    MODEL_ENDPOINT_MAP,
    MODEL_NAME_TO_ID_MAP,
    MODEL_ROUND_ROBIN_INDEX,
    MODEL_ROUND_ROBIN_LOCK,
)
from core.app_state import get_app_state
from core.api_key_manager import api_key_manager
from core.model_archive import is_model_archived
from core.errors import (
    BadRequestError,
    AuthenticationError,
    ServiceUnavailableError,
    VerificationRequiredError,
    BrowserNotConnectedError,
    GatewayTimeoutError,
    RateLimitError,
)

# 服务与工具
from modules.monitoring import monitoring_service
from utils.monitor_params import build_monitor_request_params
from utils.task_registry import spawn

logger = logging.getLogger(__name__)
router = APIRouter(tags=["core-api"])

_app_state = get_app_state()

# 重新导出函数，保持向后兼容
__all__ = [
    "get_models",
    "get_gemini_models",
    "gemini_native_api",
    "chat_completions",
    "anthropic_messages",
    "handle_single_completion",
    "handle_direct_api_request",
    "handle_lmarena_request",
]

_DIRECT_API_TYPES = ("direct_api", "gemini_native", "anthropic_native", "responses_native")


# ============================================================================
# 内部辅助
# ============================================================================

async def _select_endpoint_config_for_model(model_name: Optional[str]):
    """按模型名解析端点配置；列表配置使用线程安全轮询。

    🔧 越界修复：取值前先对当前列表长度取模。旧版本先用旧索引取值再取模，
    配置热重载导致端点数量减少时会 IndexError。
    """
    endpoint_config = MODEL_ENDPOINT_MAP.get(model_name) if model_name else None

    if isinstance(endpoint_config, list) and endpoint_config:
        endpoints = endpoint_config
        async with MODEL_ROUND_ROBIN_LOCK:
            current_index = MODEL_ROUND_ROBIN_INDEX.get(model_name, 0) % len(endpoints)
            endpoint_config = endpoints[current_index]
            MODEL_ROUND_ROBIN_INDEX[model_name] = (current_index + 1) % len(endpoints)
            logger.info(f"[DIRECT_API] 多端点轮询: 模型'{model_name}' 选择端点#{current_index + 1}/{len(endpoints)}")

    return endpoint_config


def _resolve_model_type(model_name: Optional[str]) -> str:
    """解析模型类型（text/image），优先 MODEL_ENDPOINT_MAP，回退 models.json。"""
    endpoint_mapping = MODEL_ENDPOINT_MAP.get(model_name) if model_name else None

    if isinstance(endpoint_mapping, dict) and "type" in endpoint_mapping:
        return endpoint_mapping.get("type", "text")
    if isinstance(endpoint_mapping, list) and endpoint_mapping:
        first_mapping = endpoint_mapping[0] if isinstance(endpoint_mapping[0], dict) else {}
        if "type" in first_mapping:
            return first_mapping.get("type", "text")

    return MODEL_NAME_TO_ID_MAP.get(model_name, {}).get("type", "text")


def _extract_client_api_key(request: Request, allow_gemini_style: bool = False) -> Optional[str]:
    """从请求中提取客户端提交的 API Key。

    覆盖三种客户端习惯：
    - OpenAI:    Authorization: Bearer <key>
    - Anthropic: x-api-key: <key>
    - Gemini:    x-goog-api-key: <key> 或 ?key=<key>（仅 allow_gemini_style 时）
    """
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        provided = auth_header.split(" ", 1)[1].strip()
        if provided:
            return provided

    x_api_key = request.headers.get("x-api-key")
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()

    if allow_gemini_style:
        goog_key = request.headers.get("x-goog-api-key")
        if goog_key and goog_key.strip():
            return goog_key.strip()
        query_key = request.query_params.get("key")
        if query_key and query_key.strip():
            return query_key.strip()

    return None


@observe_authenticated_request
def _validate_request_api_key(request: Request, model_name: Optional[str],
                              allow_gemini_style: bool = False) -> None:
    """
    统一 API Key 认证逻辑：
    1) 全局 api_key（管理员 key）始终可用（常数时间比较）
    2) 访客 key（api_key_manager）用于模型权限和 RPM 控制
    3) 支持 Anthropic x-api-key 头（与 Bearer 等效）
    4) allow_gemini_style=True 时额外支持 Gemini 的 x-goog-api-key / ?key=
    """
    provided_key = _extract_client_api_key(request, allow_gemini_style=allow_gemini_style)

    global_api_key = CONFIG.get("api_key")
    has_guest_keys = api_key_manager.has_keys()

    # 未配置认证时，是否携带任意字符串都不能改变仅本机可用的边界。
    if not global_api_key and not has_guest_keys:
        client_host = request.client.host if request.client else ""
        if client_host not in ("127.0.0.1", "::1", "localhost"):
            raise AuthenticationError("未配置 API Key，仅允许本机访问。").to_http_exception()
        return

    # 没提供 key：只在配置了任一认证方式时拦截
    if not provided_key:
        if global_api_key or has_guest_keys:
            hint = "，或使用 x-api-key 头部。"
            if allow_gemini_style:
                hint = "，或使用 x-api-key / x-goog-api-key 头部、?key= 查询参数。"
            raise AuthenticationError(
                "未提供 API Key。请在 Authorization 头部中以 'Bearer YOUR_KEY' 格式提供" + hint
            ).to_http_exception()
        # 未配置任何认证时，仅允许本机访问，防止成为开放代理
        client_host = request.client.host if request.client else ""
        if client_host not in ("127.0.0.1", "::1", "localhost"):
            raise AuthenticationError(
                "未提供 API Key。请在 Authorization 头部中以 'Bearer YOUR_KEY' 格式提供，或使用 x-api-key 头部。"
            ).to_http_exception()
        return

    # ✅ 管理员 key 永远可用（compare_digest 防时序攻击）
    if global_api_key and secrets.compare_digest(str(provided_key), str(global_api_key)):
        return

    # 访客 key 校验（模型白名单 + RPM）
    if has_guest_keys:
        valid, key_id, error_msg = api_key_manager.validate_request(provided_key, model_name)
        if not valid:
            if error_msg and "频率超限" in error_msg:
                raise RateLimitError(error_msg).to_http_exception()
            elif error_msg and "无权访问模型" in error_msg:
                from core.errors import PermissionError as APIPermissionError
                raise APIPermissionError(error_msg).to_http_exception()
            else:
                raise AuthenticationError(error_msg or "API Key 验证失败。").to_http_exception()
        return

    # 仅有全局 key 时，且上面没匹配成功 -> 认证失败
    if global_api_key:
        raise AuthenticationError("提供的 API Key 不正确。").to_http_exception()


# 超过该大小的请求体才丢线程池解析（小 body 的线程切换开销比解析本身还大）
_JSON_OFFLOAD_THRESHOLD_BYTES = 64 * 1024


async def _read_request_json_non_blocking(request: Request) -> Dict[str, Any]:
    """读取并解析请求 JSON；大 body（长上下文对话）才移入线程池，避免阻塞事件循环。"""
    body = await request.body()
    if len(body) > _JSON_OFFLOAD_THRESHOLD_BYTES:
        data = await asyncio.to_thread(json.loads, body)
    else:
        data = json.loads(body or b"")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象。")
    if 'model' in data and (not isinstance(data['model'], str) or not data['model'].strip()):
        raise HTTPException(status_code=400, detail="model 必须是非空字符串。")
    if 'messages' in data and (not isinstance(data['messages'], list)
                               or not all(isinstance(message, dict) for message in data['messages'])):
        raise HTTPException(status_code=400, detail="messages 必须是消息对象列表。")
    if 'stream' in data and not isinstance(data['stream'], bool):
        raise HTTPException(status_code=400, detail="stream 必须是布尔值。")
    context = current_request.get()
    if context and not context.request_body:
        context.request_body = copy.deepcopy(data)
    return data


def _check_verification_cooldown() -> None:
    """检查人机验证冷却状态（统一从 AppState 读取），冷却中直接拒绝请求。"""
    cooldown_until = _app_state.server.VERIFICATION_COOLDOWN_UNTIL
    if cooldown_until is not None:
        remaining = cooldown_until - time.time()
        if remaining > 0:
            adjusted_remaining = max(0, int(remaining - 3))
            logger.warning(f"⏰ 请求被拒绝：人机验证冷却中（剩余 {int(remaining)} 秒）")
            raise VerificationRequiredError(adjusted_remaining).to_http_exception()


# ============================================================================
# 核心分发逻辑
# ============================================================================

async def _dispatch_chat_completions_core(
    openai_req: Dict[str, Any],
    request: Optional[Request] = None,
    skip_api_auth: bool = False,
):
    """将 OpenAI 格式请求分发到 Direct API 或 LMArena 处理链。

    Args:
        openai_req: OpenAI 格式请求体
        request: FastAPI Request（仅用于 API Key 验证；skip_api_auth=True 时可为 None）
        skip_api_auth: 是否跳过 API Key 验证（内部重试/已在上层验证时使用）
    """
    model_name = openai_req.get("model")
    if not isinstance(model_name, str) or not model_name.strip():
        raise HTTPException(status_code=400, detail="model 必须是非空字符串。")
    if model_name not in MODEL_ENDPOINT_MAP and model_name not in MODEL_NAME_TO_ID_MAP:
        raise HTTPException(status_code=404, detail=f"模型 '{model_name}' 不存在")
    model_type = _resolve_model_type(model_name)

    # 归档模型拦截：配置存在但已归档 → 按“模型不存在”处理。
    # 检查原始配置而非轮询后的单端点，保证 list（多端点）配置任意端点都拦得住；
    # 覆盖 /v1/chat/completions、/v1/messages（转换链路）、/v1/responses 全部入口。
    raw_config = MODEL_ENDPOINT_MAP.get(model_name) if model_name else None
    if raw_config is not None and is_model_archived(raw_config):
        raise HTTPException(status_code=404, detail=f"模型 '{model_name}' 不存在")

    # 端点配置解析（列表配置轮询）
    endpoint_config = await _select_endpoint_config_for_model(model_name)

    # 如果是Direct API模式，跳过浏览器连接检查
    endpoint_config_dict: Optional[Dict[str, Any]] = endpoint_config if isinstance(endpoint_config, dict) else None
    is_direct_api_mode = endpoint_config_dict is not None and endpoint_config_dict.get("api_type") in _DIRECT_API_TYPES

    # API Key 验证（所有模式都需要验证）
    if not skip_api_auth and request is not None:
        _validate_request_api_key(request, model_name)

    browser_ws = _app_state.browser_ws

    # 连接检查与自动重试逻辑（Direct API模式跳过）
    if not browser_ws and not is_direct_api_mode:
        if CONFIG.get("enable_auto_retry", False):
            logger.warning("油猴脚本未连接，但自动重试已启用。请求将被暂存。")

            future = asyncio.get_running_loop().create_future()
            pending_queue = _app_state.pending_requests_queue

            item = {
                "future": future,
                "request_data": openai_req
            }
            await pending_queue.put(item)

            logger.info(f"一个新请求已被放入暂存队列。当前队列大小: {pending_queue.qsize()}")

            timeout = CONFIG.get("retry_timeout_seconds", 120)
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                item["request_data"] = None  # 释放内存，避免无界队列撑爆
                logger.warning(f"一个暂存的请求等待了 {timeout} 秒后超时。")
                raise GatewayTimeoutError(
                    f"浏览器与服务器连接断开，并在 {timeout} 秒内未能恢复。请求失败。"
                ).to_http_exception()
        else:
            raise BrowserNotConnectedError().to_http_exception()

    if _app_state.server.IS_REFRESHING_FOR_VERIFICATION and not browser_ws:
        raise ServiceUnavailableError(
            "正在等待浏览器刷新以完成人机验证，请在几秒钟后重试。"
        ).to_http_exception()

    # Direct API模式处理
    if is_direct_api_mode and endpoint_config_dict is not None:
        from modules.token_counter import estimate_message_tokens, estimate_tokens
        from services.image_service import process_image_data

        return await handle_direct_api_request(
            openai_req=openai_req,
            model_name=model_name,
            endpoint_config=endpoint_config_dict,
            CONFIG=CONFIG,
            PROCESSED_IMAGE_CACHE=_app_state.image.PROCESSED_IMAGE_CACHE,
            monitoring_service=monitoring_service,
            direct_api_service=_app_state.server.direct_api_service,
            estimate_message_tokens_func=estimate_message_tokens,
            estimate_tokens_func=estimate_tokens,
            process_image_data_func=process_image_data,
            full_messages=openai_req.get("messages", []),
        )

    # LMArena模式处理
    return await handle_lmarena_request(
        openai_req=openai_req,
        model_name=model_name,
        model_type=model_type,
    )


# ============================================================================
# 暂存队列请求处理（浏览器重连后的自动重试）
# ============================================================================

async def _resend_request_to_browser(openai_req: Dict[str, Any], request_id: str):
    """向浏览器重发一个仍有活跃消费者（stream_generator 挂在旧 channel 上）的请求。

    🔧 修复：旧版本会创建全新的响应通道覆盖旧通道，导致挂在旧 Queue 上的
    stream_generator 永远收不到数据（资源泄漏 + 客户端超时）。
    正确做法是复用原 request_id / channel，仅把请求重新发给浏览器。
    """
    from services.message_converter import convert_openai_to_lmarena_payload
    from core.load_balancer import select_best_tab

    metadata = _app_state.request_metadata.get(request_id) or {}
    session_id = metadata.get("session_id") or CONFIG.get("session_id")
    if not session_id:
        raise ValueError(f"请求 {request_id[:8]} 无可用 session_id，无法恢复")

    lmarena_payload = await convert_openai_to_lmarena_payload(
        openai_req,
        session_id,
        mode_override=metadata.get("mode_override"),
        battle_target_override=metadata.get("battle_target_override"),
    )

    if metadata.get("model_name") and _resolve_model_type(metadata["model_name"]) == "image":
        lmarena_payload["is_image_request"] = True

    tab_id, ws = await select_best_tab()

    if request_id in _app_state.request_metadata:
        _app_state.request_metadata[request_id]["tab_id"] = tab_id
        if not _app_state.request_metadata[request_id].get("original_tab_id"):
            _app_state.request_metadata[request_id]["original_tab_id"] = tab_id

    await ws.send_text(json.dumps({
        "request_id": request_id,
        "payload": lmarena_payload,
    }, ensure_ascii=False))

    logger.info(f"[HANDLE_SINGLE] ✅ 请求 {request_id[:8]} 已通过标签页 '{tab_id}' 重发")
    return {"request_id": request_id, "status": "resent"}


async def handle_single_completion(openai_req: dict, retry_request_id: Optional[str] = None):
    """处理暂存队列中的单个请求（由 process_pending_requests 调用）。

    两种场景：
    1. retry_request_id 存在且旧响应通道仍在 → 原客户端还挂在流上，
       仅向浏览器重发请求（复用原 channel）
    2. 全新暂存请求 → 走完整分发流程，返回 Response 由 future 送回客户端
    """
    if retry_request_id and retry_request_id in _app_state.response_channels:
        return await _resend_request_to_browser(openai_req, retry_request_id)

    # 重试 ID 存在但通道已消失：旧客户端已断开，放弃而非走完整分发（避免泄漏新通道/元数据/标签页计数）
    if retry_request_id:
        logger.warning(f"[HANDLE_SINGLE] 请求 {retry_request_id[:8]} 的响应通道已消失（客户端断开），放弃重试")
        return {"request_id": retry_request_id, "status": "abandoned"}

    return await _dispatch_chat_completions_core(openai_req, request=None, skip_api_auth=True)


# ============================================================================
# 公开端点
# ============================================================================

async def chat_completions(request: Request):
    """处理聊天补全请求的入口函数。
    根据模型配置分发到对应的处理逻辑（Direct API或LMArena模式）。"""
    _app_state.update_activity()
    logger.info("API请求已收到，活动时间已更新")

    _check_verification_cooldown()

    try:
        openai_req = await _read_request_json_non_blocking(request)
    except json.JSONDecodeError:
        raise BadRequestError("无效的 JSON 请求体").to_http_exception()
    except Exception as e:
        if "ClientDisconnect" in type(e).__name__ or "Disconnect" in type(e).__name__:
            logger.warning("⚡ 客户端在请求发送完成前断开了连接，忽略此请求")
            return JSONResponse(status_code=499, content={"error": "Client Disconnected"})
        raise

    return await _dispatch_chat_completions_core(openai_req, request=request)


async def anthropic_messages(request: Request):
    """
    处理 Anthropic Claude 兼容接口：/v1/messages
    - 输入：Anthropic messages 格式
    - anthropic_native 配置：原样透传上游 Anthropic API
    - 其他配置：转换到 OpenAI chat.completions 流程，再转换回 Anthropic 格式

    支持：text / image / tool_use / tool_result / thinking / tools / tool_choice
    支持：x-api-key 头认证（与 Bearer 等效）
    """
    _app_state.update_activity()
    logger.info("[ANTHROPIC_COMPAT] /v1/messages 请求已收到，活动时间已更新")

    _check_verification_cooldown()

    try:
        anthropic_req = await _read_request_json_non_blocking(request)
    except json.JSONDecodeError:
        raise BadRequestError("无效的 JSON 请求体").to_http_exception()

    model_name = anthropic_req.get("model")
    if not model_name:
        raise BadRequestError("Anthropic 请求缺少 'model' 字段").to_http_exception()

    # API Key 验证（支持 x-api-key / Bearer 双模式）
    _validate_request_api_key(request, model_name)

    # 先解析端点配置，支持 Claude /messages 原样透传
    endpoint_config = await _select_endpoint_config_for_model(model_name)

    _endpoint_api_type = endpoint_config.get("api_type") if isinstance(endpoint_config, dict) else None
    is_direct_api = _endpoint_api_type in _DIRECT_API_TYPES
    is_anthropic_native = _endpoint_api_type == "anthropic_native"
    default_endpoint = "/messages" if is_anthropic_native else "/chat/completions"
    endpoint_path = (endpoint_config.get("endpoint_path") or default_endpoint) if isinstance(endpoint_config, dict) else "/chat/completions"
    endpoint_path = endpoint_path.strip()
    if not endpoint_path.startswith("/"):
        endpoint_path = "/" + endpoint_path

    # ── Anthropic 原生格式兼容（透传模式）──
    if is_anthropic_native and isinstance(endpoint_config, dict):
        return await _handle_anthropic_passthrough(
            anthropic_req=anthropic_req,
            model_name=model_name,
            endpoint_config=endpoint_config,
            endpoint_path=endpoint_path,
        )

    # ── Anthropic → OpenAI 转换模式 ──
    try:
        openai_req = convert_anthropic_to_openai_request(anthropic_req)
    except ValueError as e:
        raise BadRequestError(str(e)).to_http_exception()

    # 保存原始 stream 值（_dispatch_chat_completions_core 可能就地修改 openai_req）
    original_stream = openai_req.get("stream", False)
    openai_response = await _dispatch_chat_completions_core(
        openai_req, request=request, skip_api_auth=True
    )

    # 流式：OpenAI SSE -> Anthropic SSE
    if original_stream and isinstance(openai_response, StreamingResponse):
        return build_anthropic_streaming_response(
            openai_streaming_response=openai_response,
            request_model=openai_req.get("model", anthropic_req.get("model", "unknown")),
        )

    # 非流式：OpenAI JSON -> Anthropic JSON
    return await convert_openai_non_stream_response_to_anthropic(
        openai_response=openai_response,
        request_model=openai_req.get("model", anthropic_req.get("model", "unknown")),
    )


# ============================================================================
# Anthropic 原生透传模式
# ============================================================================

def _apply_native_thinking_config(passthrough_body: dict, endpoint_config: dict) -> None:
    """对透传请求体应用模型级 thinking 配置（就地修改）。

    enable_thinking 支持以下值（兼容旧配置的布尔值 true/false）：
      None/""：透传客户端 thinking 参数，不做任何修改
      true/"enabled"：强制 thinking.type=enabled + budget_tokens
      "adaptive"：将客户端 thinking.type=enabled 转为 adaptive（适用于 Claude Opus 4.7 等新模型）
      false/"disabled"：强制 thinking.type=disabled
    """
    et_mode = endpoint_config.get("enable_thinking")
    if et_mode is True:
        et_mode = "enabled"
    elif et_mode is False:
        et_mode = "disabled"

    if et_mode == "enabled":
        # 思考控制方式二选一（与 _direct_api_anthropic.apply_thinking_config 保持一致）：
        #   - 配置了 reasoning_effort（前端“强度等级”模式）：Anthropic 新模型
        #     已废弃 thinking.budget_tokens，只注入 output_config.effort。
        #   - 仅配置了 thinking_budget（前端“Token 预算”模式，旧模型兼容）：
        #     注入 thinking.type=enabled + budget_tokens。
        #   - 均未配置：不强改，透传客户端原始 thinking 参数。
        configured_effort = endpoint_config.get("reasoning_effort")
        thinking_budget = endpoint_config.get("thinking_budget")
        if configured_effort:
            set_output_effort(passthrough_body, configured_effort)
            # 客户端已带 thinking 时：enabled → adaptive 并清掉废弃的 budget_tokens
            thinking_obj = passthrough_body.get("thinking")
            if isinstance(thinking_obj, dict) and thinking_obj.get("type") in ("enabled", "adaptive"):
                thinking_obj["type"] = "adaptive"
                thinking_obj.pop("budget_tokens", None)
            else:
                passthrough_body["thinking"] = {"type": "adaptive"}
            logger.info(f"[ANTHROPIC_COMPAT] thinking 启用(强度等级): output_config.effort={configured_effort}, thinking=adaptive（无 budget_tokens）")
        elif thinking_budget:
            budget = int(thinking_budget or 0)
            passthrough_body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            set_output_effort(passthrough_body, None)
            logger.info(f"[ANTHROPIC_COMPAT] thinking 强制启用(Token预算): budget_tokens={budget}")
        else:
            logger.info("[ANTHROPIC_COMPAT] thinking 配置未指定控制方式，透传客户端参数")
    elif et_mode == "adaptive":
        # adaptive 模式：将客户端 thinking.type=enabled 转为 adaptive
        # 不强制注入 output_config.effort，仅在配置了 thinking_effort 时才添加
        thinking = passthrough_body.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "enabled":
            thinking["type"] = "adaptive"
            thinking.pop("budget_tokens", None)
            logger.info("[ANTHROPIC_COMPAT] thinking.type=enabled → adaptive")
        elif not isinstance(thinking, dict):
            passthrough_body["thinking"] = {"type": "adaptive"}
            logger.info("[ANTHROPIC_COMPAT] thinking 注入 adaptive")
        # 仅在显式配置 thinking_effort 时才添加 output_config，否则让模型自行决定
        configured_effort = endpoint_config.get("thinking_effort")
        if configured_effort:
            set_output_effort(passthrough_body, configured_effort)
            logger.info(f"[ANTHROPIC_COMPAT] output_config.effort={configured_effort}")
        else:
            set_output_effort(passthrough_body, None)
    elif et_mode == "disabled":
        passthrough_body["thinking"] = {"type": "disabled"}
        set_output_effort(passthrough_body, None)
        logger.info("[ANTHROPIC_COMPAT] thinking 显式禁用")

    # ── thinking display 注入 ──
    # Opus 4.7/4.8、Mythos 5、Fable 5 等新模型 thinking.display 默认 omitted（返回空思维链）
    # 显式注入 display=summarized 确保返回思维链内容（可被 thinking_display 配置覆盖）
    # 仅在 thinking 对象存在且未设 display 时注入，尊重客户端显式设置
    # 🔧 白名单修复：disabled 等不支持 display 的 type 不能注入，否则
    # enable_thinking=false 的模型每次请求都会被上游 400
    thinking_display = endpoint_config.get("thinking_display", "summarized")
    if thinking_display:
        thinking_obj = passthrough_body.get("thinking")
        if (isinstance(thinking_obj, dict)
                and thinking_obj.get("type") in ("enabled", "adaptive")
                and "display" not in thinking_obj):
            thinking_obj["display"] = thinking_display
            logger.info(f"[ANTHROPIC_COMPAT] thinking.display={thinking_display}")


def _apply_native_system_injection(passthrough_body: dict, endpoint_config: dict) -> None:
    """对透传请求体应用系统提示词注入（Anthropic 原生格式，就地修改）。

    Anthropic 的 system 是顶层字段（字符串或 content blocks 数组），
    不能复用 OpenAI 的 inject_system_prompt。
    """
    system_injection_config = endpoint_config.get("system_prompt_injection")
    if not (isinstance(system_injection_config, dict) and system_injection_config.get("enabled", False)):
        return

    inject_content = (system_injection_config.get("content") or "").strip()
    if not inject_content:
        return

    position = system_injection_config.get("position", "before_system")
    inject_block = {"type": "text", "text": inject_content}
    existing_system = passthrough_body.get("system")

    # 将现有 system 统一为 blocks 列表形式
    if isinstance(existing_system, str):
        existing_blocks = [{"type": "text", "text": existing_system}] if existing_system else []
    elif isinstance(existing_system, list):
        existing_blocks = existing_system
    else:
        existing_blocks = []

    if position == "replace_system":
        passthrough_body["system"] = [inject_block]
    elif position == "after_system":
        passthrough_body["system"] = existing_blocks + [inject_block]
    else:  # before_system（默认）
        passthrough_body["system"] = [inject_block] + existing_blocks

    logger.info(
        f"[ANTHROPIC_COMPAT] 系统提示词注入已启用 "
        f"(位置: {position}, 内容长度: {len(inject_content)})")


async def _estimate_anthropic_local_usage(
    passthrough_body: Dict[str, Any],
    resp_content: Optional[str],
    resp_reasoning: Optional[str],
    model: str,
    fallback_input: int = 0,
    fallback_output: int = 0,
) -> tuple:
    """token_stats_mode=local：用本地 tokenizer 估算 Anthropic 请求的输入/输出 token。

    注意：可能在流式生成器的 finally 中被调用，此时若任务正在取消，
    await 会抛 CancelledError，故这里捕获 BaseException 回退上游值，
    保证监控记录一定能写入。
    """
    try:
        msgs = list(passthrough_body.get("messages") or [])
        system = passthrough_body.get("system")
        system_text = ""
        if isinstance(system, str):
            system_text = system
        elif isinstance(system, list):
            system_text = "\n".join(
                b.get("text", "") for b in system if isinstance(b, dict))
        if system_text:
            msgs = [{"role": "system", "content": system_text}] + msgs
        input_tokens = await estimate_message_tokens_non_blocking(
            estimate_message_tokens, msgs, model)
        output_text = (resp_reasoning or "") + (resp_content or "")
        output_tokens = await estimate_text_tokens_non_blocking(
            estimate_tokens, output_text, model) if output_text else 0
        logger.info(
            f"[TOKEN_STATS_LOCAL] 本地tokenizer统计: 输入={input_tokens}, 输出={output_tokens} (model={model})")
        return input_tokens, output_tokens
    except BaseException as e:
        logger.warning(f"[TOKEN_STATS_LOCAL] 本地token估算失败，回退上游值: {e}")
        return fallback_input, fallback_output


def _extract_upstream_stream_error(chunk: bytes) -> Optional[Dict[str, Any]]:
    """从流式首块中提取上游错误对象。

    覆盖两种形态：
    - 裸 JSON 错误体（上游直接返回 JSON 而非 SSE）
    - call_api_passthrough 对非 2xx 响应输出的 "data: {错误}" SSE 包装块

    正常 SSE 流（如 event: message_start）返回 None。
    """
    try:
        text = chunk.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not text:
        return None

    candidate = None
    if text.startswith("{"):
        candidate = text
    else:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data = line[5:].strip()
                if data and data != "[DONE]":
                    candidate = data
                break
    if not candidate:
        return None
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return obj if is_error_json(obj) else None


@observe_endpoint
async def _handle_anthropic_passthrough(
    anthropic_req: Dict[str, Any],
    model_name: str,
    endpoint_config: dict,
    endpoint_path: str,
):
    """anthropic_native 配置：请求原样透传到上游 Anthropic API。"""
    direct_api_service = _app_state.server.direct_api_service
    if not direct_api_service:
        raise ServiceUnavailableError("Direct API service not initialized").to_http_exception()

    api_base_url = endpoint_config.get("api_base_url")
    if not api_base_url:
        raise BadRequestError(f"模型 '{model_name}' 配置缺少 api_base_url").to_http_exception()

    # 模型 ID 映射：用户请求中的 model_name → 上游实际 model_id
    target_model_id = endpoint_config.get("model_id", model_name)
    display_name = endpoint_config.get("display_name", model_name)

    # API Key 轮询支持（兼容 api_keys 数组和 api_key 单值）
    raw_api_key = endpoint_config.get("api_keys") or endpoint_config.get("api_key")
    upstream_api_key = await get_round_robin_api_key(model_name, raw_api_key)

    is_stream = bool(anthropic_req.get("stream", False))
    token_stats_local = endpoint_config.get("token_stats_mode") == "local"
    pricing_config = endpoint_config.get("pricing", {})

    # 构建透传请求体：复制原始请求并替换 model 为目标模型 ID
    passthrough_body = dict(anthropic_req)
    passthrough_body["model"] = target_model_id

    # 剔除历史中缺失签名的 thinking block（OpenRouter 等上游严格校验会 400）
    passthrough_body["messages"] = sanitize_anthropic_thinking_blocks(
        passthrough_body.get("messages")
    )

    # 图片压缩预处理（Anthropic 原生 image 块，与其他链路共用 optimize_image）
    from services.image_service import preprocess_anthropic_images
    await preprocess_anthropic_images(
        passthrough_body,
        CONFIG=CONFIG,
        model_image_config=endpoint_config.get("image_compression"),
        PROCESSED_IMAGE_CACHE=_app_state.image.PROCESSED_IMAGE_CACHE,
    )

    _apply_native_thinking_config(passthrough_body, endpoint_config)
    _apply_native_system_injection(passthrough_body, endpoint_config)

    # ── 自定义参数 / 附加主体参数合并（provider 路由指定等依赖此注入）──
    # 与 handle_anthropic_native_from_openai 保持一致：custom 先、extra 后，同名键 extra 优先
    # deepcopy：后续 apply_auto_cache_control 会在请求体上原地打 cache_control
    # 断点，直接引用配置对象会把断点永久写进内存配置，造成跨请求污染
    custom_params = endpoint_config.get("custom_params", {})
    if isinstance(custom_params, dict) and custom_params:
        passthrough_body.update(copy.deepcopy(custom_params))
        logger.info(f"[ANTHROPIC_COMPAT] 已合并自定义参数: {list(custom_params.keys())}")

    extra_body_params = endpoint_config.get("extra_body_params", {})
    if isinstance(extra_body_params, dict) and extra_body_params:
        passthrough_body.update(copy.deepcopy(extra_body_params))
        logger.info(f"[ANTHROPIC_COMPAT] 已合并附加主体参数: {list(extra_body_params.keys())}")

    # 自动提示词缓存：在 custom_params 之后注入，避免断点被顶层字段覆盖丢失
    apply_auto_cache_control(passthrough_body, endpoint_config, log_tag="ANTHROPIC_COMPAT")

    # ── 监控记录 ──
    request_id = str(uuid.uuid4())
    full_messages = anthropic_req.get("messages", [])
    messages_count = len(full_messages)

    monitor_extra = {"upstream_model": target_model_id, "endpoint_path": endpoint_path}
    if isinstance(custom_params, dict) and custom_params:
        monitor_extra["custom_params"] = custom_params
    if isinstance(extra_body_params, dict) and extra_body_params:
        monitor_extra["extra_body_params"] = extra_body_params

    monitoring_service.request_start(
        request_id=request_id,
        model=display_name,
        messages_count=messages_count,
        session_id=None,
        mode="anthropic_passthrough",
        messages=full_messages,
        params=build_monitor_request_params(passthrough_body, extra=monitor_extra)
    )
    await monitoring_service.broadcast_to_monitors({
        "type": "request_start",
        "request_id": request_id,
        "model": display_name,
        "timestamp": time.time()
    })

    logger.info(
        f"[ANTHROPIC_COMPAT] 直通模式启用: model={model_name} → target={target_model_id}, "
        f"endpoint_path={endpoint_path}, stream={is_stream}")

    if is_stream:
        async def _monitored_anthropic_stream():
            success = True
            error_msg = None
            first_chunk = True
            # 把 input_json_delta 中的 \uXXXX 转义提前解码为明文再透传下游
            sse_rewriter = AnthropicSSEToolArgsRewriter()
            # 旁路解析状态：累积响应内容用于监控记录
            stream_state = {
                "content_parts": [],
                "reasoning_parts": [],
                "tool_calls": [],
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "upstream_usage": {},
            }
            try:
                api_iter = direct_api_service.call_api_passthrough(
                    base_url=api_base_url,
                    api_key=upstream_api_key,
                    request_body=passthrough_body,
                    endpoint_path=endpoint_path
                )
                async for chunk in api_iter:
                    if first_chunk:
                        first_chunk = False
                        # 检测首块是否为上游错误：裸 JSON 错误体，或
                        # call_api_passthrough 对非 2xx 输出的 "data: {错误}" SSE 块
                        upstream_error = _extract_upstream_stream_error(chunk)
                        if upstream_error is not None:
                            success = False
                            # 完整记录原始错误 JSON，方便排查
                            error_msg = json.dumps(upstream_error, ensure_ascii=False)
                            logger.warning(
                                f"[ANTHROPIC_COMPAT] 流式请求上游返回错误: {error_msg[:300]}")
                            # 转为 Anthropic 原生 error 事件，/v1/messages 客户端才能识别
                            err_obj = upstream_error.get("error")
                            error_payload = build_anthropic_error_payload(
                                err_obj if isinstance(err_obj, dict) else upstream_error)
                            yield (
                                "event: error\n"
                                f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
                            ).encode("utf-8")
                            return
                    # 重写工具参数增量中的 unicode 转义（其余字节原样），
                    # 旁路监控解析与下游转发使用同一份重写后的数据
                    rewritten = sse_rewriter.feed(chunk)
                    if not rewritten:
                        continue
                    extract_anthropic_sse_content(rewritten, stream_state)
                    yield rewritten
                # 转发重写器缓冲中的残留（正常 SSE 流以空行结尾，此处为空操作）
                tail_bytes = sse_rewriter.flush()
                if tail_bytes:
                    extract_anthropic_sse_content(tail_bytes, stream_state)
                    yield tail_bytes
            except asyncio.CancelledError:
                success = False
                error_msg = "客户端断开连接"
                raise
            except Exception as e:
                success = False
                error_msg = str(e)
                raise
            finally:
                # 冲刷旁路解析残留（不完整尾行 + 未收到 stop 的工具参数）
                flush_anthropic_sse_state(stream_state)
                # 从旁路累积的 state 中提取内容
                resp_content = "".join(stream_state["content_parts"]) if success else (error_msg or "")
                resp_reasoning = "".join(stream_state["reasoning_parts"]) if success else None
                resp_tool_calls = stream_state.get("tool_calls") or None
                final_input_tokens = stream_state.get("input_tokens") or 0
                final_output_tokens = stream_state.get("output_tokens") or 0
                if token_stats_local:
                    # local 统计模式：忽略上游 usage，用本地 tokenizer 重算
                    final_input_tokens, final_output_tokens = await _estimate_anthropic_local_usage(
                        passthrough_body,
                        "".join(stream_state["content_parts"]),
                        "".join(stream_state["reasoning_parts"]),
                        display_name,
                        fallback_input=final_input_tokens,
                        fallback_output=final_output_tokens)
                final_cached_tokens = stream_state.get("cached_tokens") or 0
                cost_info = direct_api_service.calculate_cost(
                    input_tokens=final_input_tokens,
                    output_tokens=final_output_tokens,
                    cached_tokens=final_cached_tokens,
                    pricing=pricing_config) if pricing_config else None
                if cost_info and cost_info.get("total_cost"):
                    logger.info(
                        f"[ANTHROPIC_COMPAT] 总成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")
                monitoring_service.request_end(
                    request_id=request_id,
                    success=success,
                    error=error_msg,
                    response_content=resp_content or None,
                    reasoning_content=resp_reasoning or None,
                    response_tool_calls=resp_tool_calls,
                    input_tokens=final_input_tokens,
                    output_tokens=final_output_tokens,
                    cached_tokens=final_cached_tokens,
                    cost_info=cost_info,
                    full_messages=full_messages,
                    upstream_usage=stream_state.get("upstream_usage") or None
                )
                # 广播使用后台任务避免阻塞生成器退出
                spawn(
                    monitoring_service.broadcast_to_monitors({
                        "type": "request_end",
                        "request_id": request_id,
                        "success": success
                    }),
                    name="anthropic-stream-broadcast"
                )

        return StreamingResponse(
            _monitored_anthropic_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "Transfer-Encoding": "chunked",
            },
        )

    # ── 非流式透传 ──
    # 🔧 bytes 的 += 没有原地扩容优化（CPython 仅对 str 有），大响应下退化为
    # O(n²) 复制；改用 bytearray 累积（与 _direct_api_passthrough 保持一致）
    response_buf = bytearray()
    success = True
    error_msg = None
    # 预初始化变量，避免 finally 里未定义
    resp_content = None
    resp_reasoning = None
    resp_tool_calls = None
    resp_input_tokens = None
    resp_output_tokens = None
    resp_cached_tokens = 0
    resp_upstream_usage = None
    error_response_content = None
    error_response_payload = None
    error_status_code = 502
    try:
        async for chunk in direct_api_service.call_api_passthrough(
            base_url=api_base_url,
            api_key=upstream_api_key,
            request_body=passthrough_body,
            endpoint_path=endpoint_path
        ):
            response_buf.extend(chunk)

        # 检测非流式响应是否为错误，同时提取内容用于监控
        response_bytes = bytes(response_buf)
        response_text = response_bytes.decode('utf-8')
        try:
            # 🔧 大响应的 JSON 解析移入线程池，避免阻塞事件循环
            if len(response_bytes) > _JSON_OFFLOAD_THRESHOLD_BYTES:
                response_json = await asyncio.to_thread(json.loads, response_text)
            else:
                response_json = json.loads(response_text)
            if is_error_json(response_json):
                success = False
                # 完整记录原始错误 JSON，方便排查
                error_msg = json.dumps(response_json, ensure_ascii=False)
                error_response_content = error_msg
                logger.warning(
                    f"[ANTHROPIC_COMPAT] 非流式请求上游返回错误: {error_msg[:300]}")
                # 转为 Anthropic 错误格式并保留上游状态码，客户端才能正确识别失败
                err_obj = response_json.get("error")
                code = err_obj.get("code") if isinstance(err_obj, dict) else None
                if isinstance(code, int) and 400 <= code <= 599:
                    error_status_code = code
                error_response_payload = build_anthropic_error_payload(
                    err_obj if isinstance(err_obj, dict) else response_json)
            elif isinstance(response_json, dict):
                # 成功响应：提取内容用于监控记录
                resp_content, resp_reasoning, resp_tool_calls, resp_input_tokens, resp_output_tokens, resp_cached_tokens = \
                    extract_anthropic_response_content(response_json)
                raw_usage = response_json.get("usage")
                resp_upstream_usage = raw_usage if isinstance(raw_usage, dict) else None
                if token_stats_local:
                    # local 统计模式：忽略上游 usage，用本地 tokenizer 重算
                    resp_input_tokens, resp_output_tokens = await _estimate_anthropic_local_usage(
                        passthrough_body, resp_content, resp_reasoning, display_name,
                        fallback_input=resp_input_tokens or 0,
                        fallback_output=resp_output_tokens or 0)
        except json.JSONDecodeError:
            pass  # 非 JSON 响应，按成功处理
    except asyncio.CancelledError:
        # 🔧 CancelledError 不是 Exception（BaseException 直系）：客户端断开时
        # 必须显式标记失败，否则 finally 会把被取消的请求连同 token 用量
        # 记成成功，污染统计与费用数据
        success = False
        error_msg = "Client disconnected before non-stream response completed"
        raise
    except Exception as e:
        success = False
        error_msg = str(e)
        raise
    finally:
        final_input_tokens = (resp_input_tokens or 0) if success else 0
        final_output_tokens = (resp_output_tokens or 0) if success else 0
        final_cached_tokens = (resp_cached_tokens or 0) if success else 0
        cost_info = direct_api_service.calculate_cost(
            input_tokens=final_input_tokens,
            output_tokens=final_output_tokens,
            cached_tokens=final_cached_tokens,
            pricing=pricing_config) if pricing_config else None
        if cost_info and cost_info.get("total_cost"):
            logger.info(
                f"[ANTHROPIC_COMPAT] 总成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")
        monitoring_service.request_end(
            request_id=request_id,
            success=success,
            error=error_msg,
            response_content=(error_response_content if not success else resp_content),
            reasoning_content=resp_reasoning if success else None,
            response_tool_calls=resp_tool_calls if success else None,
            input_tokens=final_input_tokens,
            output_tokens=final_output_tokens,
            cached_tokens=final_cached_tokens,
            cost_info=cost_info,
            full_messages=full_messages,
            upstream_usage=resp_upstream_usage if success else None
        )
        # 🔧 用后台任务广播：取消传播路径上的 await 会立刻再抛 CancelledError，
        # 旧版的 except Exception 拦不住（与流式分支的 spawn 广播保持一致）
        spawn(
            monitoring_service.broadcast_to_monitors({
                "type": "request_end",
                "request_id": request_id,
                "success": success
            }),
            name="anthropic-nonstream-broadcast"
        )

    if error_response_payload is not None:
        return JSONResponse(status_code=error_status_code, content=error_response_payload)
    return Response(content=bytes(response_buf), media_type="application/json")


# ============================================================================
# 端点注册
# ============================================================================

@router.post("/v1/chat/completions")
async def chat_completions_endpoint(request: Request):
    """处理聊天补全请求"""
    return await chat_completions(request)


@router.post("/v1/messages")
async def anthropic_messages_endpoint(request: Request):
    """处理 Anthropic Claude 兼容消息请求"""
    return await anthropic_messages(request)


@router.post("/v1beta/models/{model_name}:generateContent")
@router.post("/v1beta/models/{model_name}:streamGenerateContent")
async def gemini_native_api_endpoint(model_name: str, request: Request):
    """处理Gemini原生API格式的请求"""
    _app_state.update_activity()
    _check_verification_cooldown()

    # 🔒 安全修复：这两个端点此前完全没有做 API Key 校验——/v1/chat/completions、
    # /v1/messages、/v1/models、/v1beta/models 全都校验，唯独这里漏掉。
    # 任何人只要知道模型名就能不带 key 直接调用，白嫖服务端配置的上游额度。
    # Gemini 客户端习惯用 ?key= 或 x-goog-api-key，故一并接受。
    from urllib.parse import unquote as _unquote
    _validate_request_api_key(request, _unquote(model_name), allow_gemini_style=True)

    server_state = _app_state.server
    return await gemini_native_api(
        model_name=model_name,
        request=request,
        MODEL_ENDPOINT_MAP=MODEL_ENDPOINT_MAP,
        monitoring_service=monitoring_service,
        direct_api_service=server_state.direct_api_service,
        last_activity_time_setter=lambda dt: server_state.last_activity_time_ref.update({'time': dt}),
        aiohttp_session=server_state.aiohttp_session
    )


@router.post('/v1beta/interactions')
async def gemini_interactions_endpoint(request: Request):
    from routes.gemini_v1beta_api import select_gemini_endpoint
    from services.native_exchange import forward_native_exchange
    _app_state.update_activity()
    _check_verification_cooldown()
    body = await _read_request_json_non_blocking(request)
    model = body.get('model')
    if not isinstance(model, str) or not model:
        raise HTTPException(400, 'model 为必填项')
    _validate_request_api_key(request, model, allow_gemini_style=True)
    config = await select_gemini_endpoint(model, MODEL_ENDPOINT_MAP)
    if config.get('upstream_protocol') != 'interactions':
        raise HTTPException(400, '该模型未配置 Interactions 上游协议')
    return await forward_native_exchange(body, config, model, _app_state.server.direct_api_service,
                                         monitoring_service, stream=body.get('stream', False))
