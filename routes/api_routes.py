"""
核心API路由入口
将请求分发到对应的处理模块。
当前以 Direct API 为主路径；LMArena 分支已弃用，但仍保留兼容能力。
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse, Response

# 导入拆分的模块
from .models_api import get_models, get_gemini_models
from .gemini_v1beta_api import gemini_native_api
from .direct_api_handler import handle_direct_api_request
from .lmarena_handler import handle_lmarena_request

# 导入统一错误处理
from core.errors import (
    BadRequestError,
    AuthenticationError,
    ServiceUnavailableError,
    VerificationRequiredError,
    BrowserNotConnectedError,
    GatewayTimeoutError,
    RateLimitError,
)
from core.api_key_manager import api_key_manager

logger = logging.getLogger(__name__)

# 重新导出函数，保持向后兼容
__all__ = [
    "get_models",
    "get_gemini_models",
    "gemini_native_api",
    "chat_completions",
    "anthropic_messages",
    "handle_direct_api_request",
    "handle_lmarena_request",
]


# ============================================================================
# Anthropic ↔ OpenAI 转换辅助函数
# ============================================================================

def _map_openai_finish_reason_to_anthropic(reason: Optional[str]) -> str:
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "content_filter": "stop_sequence",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
    }
    return mapping.get(reason or "stop", "end_turn")


def _format_anthropic_sse_event(event_name: str, payload: Dict[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _convert_anthropic_tools_to_openai(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将 Anthropic tools 格式转换为 OpenAI tools 格式

    Anthropic:
        [{"name": "get_weather", "description": "...", "input_schema": {...}}]
    OpenAI:
        [{"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}]
    """
    openai_tools: List[Dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name", "")
        description = tool.get("description", "")
        input_schema = tool.get("input_schema", {})
        if not name:
            continue
        openai_tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": input_schema if isinstance(input_schema, dict) else {}
            }
        })
    return openai_tools


def _convert_anthropic_tool_choice_to_openai(tool_choice: Any) -> Any:
    """将 Anthropic tool_choice 转换为 OpenAI tool_choice

    Anthropic:
        {"type": "auto"} | {"type": "any"} | {"type": "tool", "name": "..."}
    OpenAI:
        "auto" | "required" | {"type": "function", "function": {"name": "..."}} | "none"
    """
    if not tool_choice:
        return None
    if isinstance(tool_choice, str):
        return tool_choice
    if isinstance(tool_choice, dict):
        tc_type = tool_choice.get("type", "auto")
        if tc_type == "auto":
            return "auto"
        elif tc_type == "any":
            return "required"
        elif tc_type == "tool":
            tool_name = tool_choice.get("name", "")
            if tool_name:
                return {"type": "function", "function": {"name": tool_name}}
            return "auto"
        elif tc_type == "none":
            return "none"
    return None


def _extract_system_text(system_field: Any) -> str:
    if isinstance(system_field, str):
        return system_field
    if isinstance(system_field, list):
        texts: List[str] = []
        for block in system_field:
            if isinstance(block, dict) and block.get("type") in ("text", "input_text"):
                texts.append(str(block.get("text", "")))
        return "\n".join([t for t in texts if t])
    return ""


def _convert_anthropic_content_to_openai(content: Any) -> Any:
    """将 Anthropic content 转换为 OpenAI content 格式。

    处理 text / image / tool_result / tool_use / thinking 等块类型。
    注意：assistant 消息中的 tool_use 和 user 消息中的 tool_result
    的完整结构化转换在 _convert_anthropic_to_openai_request 中处理，
    此函数仅做基础的内容块→OpenAI content 转换。
    """
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        return str(content) if content is not None else ""

    converted_parts: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue

        block_type = block.get("type")
        if block_type in ("text", "input_text"):
            text = block.get("text", "")
            converted_parts.append({"type": "text", "text": str(text)})

        elif block_type == "image":
            source = block.get("source", {}) if isinstance(block.get("source"), dict) else {}
            source_type = source.get("type")

            if source_type == "base64":
                media_type = source.get("media_type", "image/jpeg")
                data = source.get("data", "")
                if data:
                    converted_parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{data}"
                        }
                    })
            elif source_type == "url":
                image_url = source.get("url", "")
                if image_url:
                    converted_parts.append({
                        "type": "image_url",
                        "image_url": {"url": image_url}
                    })

        elif block_type == "tool_result":
            tool_content = block.get("content")
            is_error = block.get("is_error", False)
            if isinstance(tool_content, str):
                prefix = "[tool_result_error] " if is_error else ""
                converted_parts.append({"type": "text", "text": prefix + tool_content})
            elif isinstance(tool_content, list):
                # tool_result content 也可能是 content blocks 数组
                for tc in tool_content:
                    if isinstance(tc, dict) and tc.get("type") in ("text", "input_text"):
                        converted_parts.append({"type": "text", "text": str(tc.get("text", ""))})
            else:
                converted_parts.append({"type": "text", "text": json.dumps(tool_content, ensure_ascii=False)})

        elif block_type == "tool_use":
            tool_name = block.get("name", "tool")
            tool_input = block.get("input", {})
            converted_parts.append({
                "type": "text",
                "text": f"[tool_use:{tool_name}] {json.dumps(tool_input, ensure_ascii=False)}"
            })

        elif block_type == "thinking":
            # thinking 块在 OpenAI 中没有直接对应，序列化为标记文本
            thinking_text = block.get("thinking", "")
            if thinking_text:
                converted_parts.append({"type": "text", "text": f"<thinking>{thinking_text}</thinking>"})

        # 跳过其他未知类型（如 redacted_thinking）

    if not converted_parts:
        return " "
    if len(converted_parts) == 1 and converted_parts[0].get("type") == "text":
        return converted_parts[0].get("text", " ")
    return converted_parts


def _convert_anthropic_to_openai_request(anthropic_req: Dict[str, Any]) -> Dict[str, Any]:
    """将 Anthropic Messages API 请求转换为 OpenAI chat.completions 请求。

    支持：text / image / tool_use / tool_result / thinking / tools / tool_choice
    """
    model = anthropic_req.get("model")
    if not model:
        raise ValueError("Anthropic 请求缺少 'model' 字段")

    messages_in = anthropic_req.get("messages")
    if not isinstance(messages_in, list):
        raise ValueError("Anthropic 请求缺少或包含无效的 'messages' 字段")

    openai_messages: List[Dict[str, Any]] = []

    system_text = _extract_system_text(anthropic_req.get("system"))
    if system_text:
        openai_messages.append({"role": "system", "content": system_text})

    for msg in messages_in:
        if not isinstance(msg, dict):
            continue

        role = msg.get("role", "user")
        raw_content = msg.get("content", "")

        # ── assistant 消息：检查是否包含 tool_use 块 ──
        if role == "assistant" and isinstance(raw_content, list):
            text_blocks: List[Dict[str, Any]] = []
            thinking_texts: List[str] = []
            tool_calls: List[Dict[str, Any]] = []
            for block in raw_content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "tool_use":
                    tool_id = block.get("id") or f"toolu_{uuid.uuid4().hex}"
                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {})
                    args_str = json.dumps(tool_input, ensure_ascii=False) if isinstance(tool_input, dict) else str(tool_input)
                    tool_calls.append({
                        "id": tool_id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": args_str
                        }
                    })
                elif bt == "thinking":
                    # 提取 thinking 文本，后续转为 reasoning_content
                    t = block.get("thinking", "")
                    if t:
                        thinking_texts.append(t)
                else:
                    text_blocks.append(block)

            content = _convert_anthropic_content_to_openai(text_blocks if text_blocks else " ")

            if tool_calls:
                msg = {
                    "role": "assistant",
                    "content": content if text_blocks else None,
                    "tool_calls": tool_calls
                }
                if thinking_texts:
                    msg["reasoning_content"] = "\n".join(thinking_texts)
                openai_messages.append(msg)
                continue

            # 无 tool_calls 的 assistant 消息：正常构建，附加 reasoning_content
            msg = {"role": "assistant", "content": content}
            if thinking_texts:
                msg["reasoning_content"] = "\n".join(thinking_texts)
            openai_messages.append(msg)
            continue

        # ── user 消息：检查是否包含 tool_result 块 ──
        if role == "user" and isinstance(raw_content, list):
            has_tool_result = any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in raw_content
            )
            if has_tool_result:
                non_tool_blocks: List[Dict[str, Any]] = []
                for block in raw_content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_result":
                        tool_use_id = block.get("tool_use_id", "")
                        tool_content = block.get("content", "")
                        # 提取文本
                        if isinstance(tool_content, list):
                            text_parts = []
                            for tc in tool_content:
                                if isinstance(tc, dict) and tc.get("type") in ("text", "input_text"):
                                    text_parts.append(str(tc.get("text", "")))
                            tool_content = "\n".join(text_parts) if text_parts else json.dumps(tool_content, ensure_ascii=False)
                        elif not isinstance(tool_content, str):
                            tool_content = json.dumps(tool_content, ensure_ascii=False)

                        openai_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_use_id,
                            "content": tool_content if isinstance(tool_content, str) else str(tool_content)
                        })
                    else:
                        non_tool_blocks.append(block)

                if non_tool_blocks:
                    # 非 tool_result 块作为普通 user 消息
                    content = _convert_anthropic_content_to_openai(non_tool_blocks)
                    openai_messages.append({"role": "user", "content": content})
                continue

        # ── 默认处理 ──
        content = _convert_anthropic_content_to_openai(raw_content)
        openai_messages.append({"role": role, "content": content})

    max_tokens = anthropic_req.get("max_tokens")
    if max_tokens is None:
        max_tokens = anthropic_req.get("max_tokens_to_sample", 4096)

    openai_req: Dict[str, Any] = {
        "model": model,
        "messages": openai_messages,
        "max_tokens": max_tokens,
        "stream": bool(anthropic_req.get("stream", False)),
    }

    # 常见参数映射
    passthrough_fields = [
        "temperature",
        "top_p",
        "top_k",
        "metadata",
        "stop",
        "user",
    ]
    for key in passthrough_fields:
        if key in anthropic_req:
            openai_req[key] = anthropic_req[key]

    stop_sequences = anthropic_req.get("stop_sequences")
    if isinstance(stop_sequences, list) and stop_sequences:
        openai_req["stop"] = stop_sequences

    # ── tools 转换 ──
    tools = anthropic_req.get("tools")
    if isinstance(tools, list) and tools:
        openai_tools = _convert_anthropic_tools_to_openai(tools)
        if openai_tools:
            openai_req["tools"] = openai_tools
            logger.info(f"[ANTHROPIC_CONVERT] 已转换 {len(openai_tools)} 个 tools → OpenAI 格式")

    # ── tool_choice 转换 ──
    tool_choice = anthropic_req.get("tool_choice")
    if tool_choice is not None:
        oai_tool_choice = _convert_anthropic_tool_choice_to_openai(tool_choice)
        if oai_tool_choice is not None:
            openai_req["tool_choice"] = oai_tool_choice
            logger.info(f"[ANTHROPIC_CONVERT] tool_choice: {tool_choice} → {oai_tool_choice}")

    # ── thinking 参数 ──
    # Anthropic thinking 没有 OpenAI 标准对应，保留为自定义字段
    thinking = anthropic_req.get("thinking")
    if isinstance(thinking, dict):
        openai_req["_anthropic_thinking"] = thinking
        logger.info(f"[ANTHROPIC_CONVERT] thinking 配置已保留: budget_tokens={thinking.get('budget_tokens', 'N/A')}")

    return openai_req


# ============================================================================
# Anthropic 错误 / 响应构建
# ============================================================================

def _build_anthropic_error_payload(error_obj: Any) -> Dict[str, Any]:
    error_type = "api_error"
    message = "请求失败"

    if isinstance(error_obj, dict):
        if "error" in error_obj and isinstance(error_obj.get("error"), dict):
            nested = error_obj["error"]
            error_type = nested.get("type", error_type)
            message = nested.get("message", message)
        else:
            error_type = error_obj.get("type", error_type)
            message = error_obj.get("message", message)
    elif isinstance(error_obj, str):
        message = error_obj
    elif error_obj is not None:
        message = str(error_obj)

    return {
        "type": "error",
        "error": {
            "type": error_type,
            "message": message
        }
    }


async def _select_endpoint_config_for_model(
    model_name: str,
    MODEL_ENDPOINT_MAP: dict,
    MODEL_ROUND_ROBIN_INDEX: dict,
    MODEL_ROUND_ROBIN_LOCK,
):
    endpoint_config = MODEL_ENDPOINT_MAP.get(model_name) if model_name else None

    if isinstance(endpoint_config, list) and endpoint_config:
        async with MODEL_ROUND_ROBIN_LOCK:
            if model_name not in MODEL_ROUND_ROBIN_INDEX:
                MODEL_ROUND_ROBIN_INDEX[model_name] = 0
            current_index = MODEL_ROUND_ROBIN_INDEX[model_name]
            endpoint_config = endpoint_config[current_index]
            MODEL_ROUND_ROBIN_INDEX[model_name] = (current_index + 1) % len(MODEL_ENDPOINT_MAP[model_name])
            logger.info(f"[DIRECT_API] 多端点轮询: 模型'{model_name}' 选择端点#{current_index + 1}")

    return endpoint_config


def _validate_request_api_key(
    request: Request,
    model_name: Optional[str],
    CONFIG: dict
) -> None:
    """
    统一 API Key 认证逻辑：
    1) 全局 api_key（管理员 key）始终可用
    2) 访客 key（api_key_manager）用于模型权限和 RPM 控制
    3) 支持 Anthropic x-api-key 头（与 Bearer 等效）
    """
    auth_header = request.headers.get("Authorization")
    provided_key = None
    if auth_header and auth_header.startswith("Bearer "):
        provided_key = auth_header.split(" ", 1)[1]

    # ── 同时支持 Anthropic 风格的 x-api-key 头 ──
    if not provided_key:
        x_api_key = request.headers.get("x-api-key")
        if x_api_key:
            provided_key = x_api_key.strip()

    global_api_key = CONFIG.get("api_key")
    has_guest_keys = api_key_manager.has_keys()

    # 没提供 key：只在配置了任一认证方式时拦截
    if not provided_key:
        if global_api_key or has_guest_keys:
            raise AuthenticationError(
                "未提供 API Key。请在 Authorization 头部中以 'Bearer YOUR_KEY' 格式提供，"
                "或使用 x-api-key 头部。"
            ).to_http_exception()
        return

    # ✅ 管理员 key 永远可用
    if global_api_key and provided_key == global_api_key:
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


# ============================================================================
# OpenAI → Anthropic 响应转换
# ============================================================================

async def _read_response_body_bytes(response: Response) -> bytes:
    body = getattr(response, "body", None)
    if body is not None:
        return body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8", errors="ignore")

    data = bytearray()
    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is None:
        return bytes(data)

    async for chunk in body_iterator:
        if isinstance(chunk, bytes):
            data.extend(chunk)
        else:
            data.extend(str(chunk).encode("utf-8", errors="ignore"))
    return bytes(data)


async def _read_request_json_non_blocking(request: Request) -> Dict[str, Any]:
    body = await request.body()
    return await asyncio.to_thread(json.loads, body or b"")


def _extract_openai_message_text(choice: Dict[str, Any]) -> str:
    """从 OpenAI choice 中提取纯文本（不含 reasoning_content）。"""
    message = choice.get("message", {}) if isinstance(choice, dict) else {}
    content = message.get("content", "")

    if isinstance(content, str) and content:
        return content
    elif isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        return "".join(parts)
    return ""


def _extract_openai_message_reasoning(choice: Dict[str, Any]) -> str:
    """从 OpenAI choice 中提取 reasoning_content。"""
    message = choice.get("message", {}) if isinstance(choice, dict) else {}
    reasoning = message.get("reasoning_content", "")
    return reasoning if isinstance(reasoning, str) else ""


async def _convert_openai_non_stream_response_to_anthropic(
    openai_response: Response,
    request_model: str
) -> Response:
    """非流式：OpenAI JSON 响应 → Anthropic JSON 响应（支持多 content blocks）"""
    status_code = getattr(openai_response, "status_code", 200)
    raw_body = await _read_response_body_bytes(openai_response)

    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return JSONResponse(
            status_code=500,
            content=_build_anthropic_error_payload({
                "type": "api_error",
                "message": "上游返回了无法解析的JSON响应"
            })
        )

    if status_code >= 400 or ("error" in data):
        return JSONResponse(
            status_code=status_code if status_code >= 400 else 500,
            content=_build_anthropic_error_payload(data.get("error", data))
        )

    choices = data.get("choices", [])
    first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    finish_reason = first_choice.get("finish_reason")
    message = first_choice.get("message", {}) if isinstance(first_choice, dict) else {}

    usage = data.get("usage", {}) if isinstance(data.get("usage"), dict) else {}
    input_tokens = int(usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(usage.get("completion_tokens", 0) or 0)

    # ── 构建 Anthropic content blocks（多 block 支持）──
    content_blocks: List[Dict[str, Any]] = []
    reasoning_content = _extract_openai_message_reasoning(first_choice)
    text_content = _extract_openai_message_text(first_choice)
    tool_calls = message.get("tool_calls")

    # thinking block
    if reasoning_content:
        content_blocks.append({"type": "thinking", "thinking": reasoning_content})

    # text block
    if text_content:
        content_blocks.append({"type": "text", "text": text_content})

    # tool_use blocks
    if isinstance(tool_calls, list) and tool_calls:
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            tc_func = tc.get("function", {})
            tc_name = tc_func.get("name", "")
            tc_args_str = tc_func.get("arguments", "{}")
            try:
                tc_args = json.loads(tc_args_str) if isinstance(tc_args_str, str) else tc_args_str
            except json.JSONDecodeError:
                tc_args = {}
            content_blocks.append({
                "type": "tool_use",
                "id": tc.get("id") or f"toolu_{uuid.uuid4().hex}",
                "name": tc_name,
                "input": tc_args if isinstance(tc_args, dict) else {"raw": tc_args}
            })

    if not content_blocks:
        content_blocks = [{"type": "text", "text": ""}]

    anthropic_response = {
        "id": data.get("id") or f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": data.get("model") or request_model,
        "content": content_blocks,
        "stop_reason": _map_openai_finish_reason_to_anthropic(finish_reason),
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens
        }
    }

    return JSONResponse(status_code=200, content=anthropic_response)


async def _iter_openai_sse_payloads(upstream_response: StreamingResponse):
    """从 OpenAI SSE 流中逐个提取 data: 负载字符串"""
    buffer = ""
    async for raw_chunk in upstream_response.body_iterator:
        if isinstance(raw_chunk, bytes):
            chunk_text = raw_chunk.decode("utf-8", errors="ignore")
        else:
            chunk_text = str(raw_chunk)

        buffer += chunk_text
        while "\n\n" in buffer:
            event_block, buffer = buffer.split("\n\n", 1)
            for line in event_block.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    yield line[5:].strip()

    if buffer.strip():
        for line in buffer.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                yield line[5:].strip()


def _build_anthropic_streaming_response(
    openai_streaming_response: StreamingResponse,
    request_model: str
) -> StreamingResponse:
    """流式：OpenAI SSE 流 → Anthropic SSE 事件流

    完整支持 multi content blocks：
    - thinking 块 (reasoning_content → thinking_delta)
    - text 块 (content → text_delta)
    - tool_use 块 (tool_calls → input_json_delta)
    自动处理块之间的切换、索引分配、block start/stop 事件。
    """

    async def anthropic_stream_generator():
        message_id = f"msg_{uuid.uuid4().hex}"
        model_name = request_model

        # ── 消息级状态 ──
        sent_message_start = False
        stop_reason = "end_turn"
        input_tokens = 0
        output_tokens = 0

        # ── content block 状态机 ──
        next_block_idx = 0
        active_thinking_idx: Optional[int] = None
        active_text_idx: Optional[int] = None
        # tool_use:  {OAI_index: {"real_idx": int, "id": str, "name": str}}
        active_tool_use: Dict[int, Dict[str, Any]] = {}
        prev_delta_type: Optional[str] = None  # "thinking" | "text" | "tool_calls"

        def _emit_block_start(idx: int, block_type: str, extra: Dict[str, Any] = None) -> str:
            if block_type == "thinking":
                cb = {"type": "thinking", "thinking": ""}
            elif block_type == "text":
                cb = {"type": "text", "text": ""}
            elif block_type == "tool_use":
                cb = {
                    "type": "tool_use",
                    "id": (extra or {}).get("id", ""),
                    "name": (extra or {}).get("name", ""),
                    "input": {}
                }
            else:
                cb = {"type": "text", "text": ""}
            return _format_anthropic_sse_event(
                "content_block_start",
                {"type": "content_block_start", "index": idx, "content_block": cb}
            )

        def _emit_block_delta(idx: int, delta_type: str, text: str) -> str:
            # Anthropic 规范中不同 delta 类型对应不同的值键名:
            #   thinking_delta  → "thinking"
            #   text_delta      → "text"
            #   input_json_delta → "partial_json"
            _DELTA_KEY_MAP = {
                "thinking_delta": "thinking",
                "text_delta": "text",
                "input_json_delta": "partial_json",
            }
            value_key = _DELTA_KEY_MAP.get(delta_type, delta_type.split("_")[0])
            return _format_anthropic_sse_event(
                "content_block_delta",
                {"type": "content_block_delta", "index": idx,
                 "delta": {"type": delta_type, value_key: text}}
            )

        def _emit_block_stop(idx: int) -> str:
            return _format_anthropic_sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": idx}
            )

        def _close_current_blocks():
            """关闭所有当前活跃的 content block，返回 SSE 事件字符串列表"""
            events: List[str] = []
            nonlocal active_thinking_idx, active_text_idx
            if active_thinking_idx is not None:
                events.append(_emit_block_stop(active_thinking_idx))
                active_thinking_idx = None
            if active_text_idx is not None:
                events.append(_emit_block_stop(active_text_idx))
                active_text_idx = None
            for oai_idx in sorted(active_tool_use.keys()):
                info = active_tool_use[oai_idx]
                events.append(_emit_block_stop(info["real_idx"]))
            active_tool_use.clear()
            return events

        # ── 主循环：逐 chunk 处理 ──
        async for payload in _iter_openai_sse_payloads(openai_streaming_response):
            if payload == "[DONE]":
                break

            try:
                chunk = json.loads(payload)
            except Exception:
                continue

            if isinstance(chunk, dict) and "error" in chunk:
                yield _format_anthropic_sse_event(
                    "error",
                    _build_anthropic_error_payload(chunk.get("error"))
                )
                return

            if not isinstance(chunk, dict):
                continue

            model_name = chunk.get("model") or model_name

            usage = chunk.get("usage")
            if isinstance(usage, dict):
                input_tokens = int(usage.get("prompt_tokens", input_tokens) or input_tokens)
                output_tokens = int(usage.get("completion_tokens", output_tokens) or output_tokens)

            if not sent_message_start:
                sent_message_start = True
                yield _format_anthropic_sse_event("message_start", {
                    "type": "message_start",
                    "message": {
                        "id": message_id, "type": "message", "role": "assistant",
                        "model": model_name, "content": [],
                        "stop_reason": None, "stop_sequence": None,
                        "usage": {"input_tokens": input_tokens}
                    }
                })

            choices = chunk.get("choices", [])
            if not choices or not isinstance(choices[0], dict):
                continue

            choice = choices[0]
            delta = choice.get("delta", {}) if isinstance(choice.get("delta"), dict) else {}

            delta_reasoning = delta.get("reasoning_content", "")
            delta_content = delta.get("content", "")
            delta_tool_calls = delta.get("tool_calls")

            # 判断当前 delta 类型
            if isinstance(delta_tool_calls, list) and delta_tool_calls:
                current_type = "tool_calls"
            elif isinstance(delta_reasoning, str) and delta_reasoning:
                current_type = "thinking"
            elif isinstance(delta_content, str) and delta_content:
                current_type = "text"
            else:
                current_type = prev_delta_type

            # ── 类型切换 → 关闭旧 block ──
            if current_type != prev_delta_type and prev_delta_type is not None:
                for ev in _close_current_blocks():
                    yield ev

            # ── 发送当前 delta ──
            if current_type == "thinking" and isinstance(delta_reasoning, str) and delta_reasoning:
                if active_thinking_idx is None:
                    active_thinking_idx = next_block_idx
                    next_block_idx += 1
                    yield _emit_block_start(active_thinking_idx, "thinking")
                yield _emit_block_delta(active_thinking_idx, "thinking_delta", delta_reasoning)

            elif current_type == "text" and isinstance(delta_content, str) and delta_content:
                if active_text_idx is None:
                    active_text_idx = next_block_idx
                    next_block_idx += 1
                    yield _emit_block_start(active_text_idx, "text")
                yield _emit_block_delta(active_text_idx, "text_delta", delta_content)

            elif current_type == "tool_calls" and isinstance(delta_tool_calls, list):
                for tc in delta_tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    oai_idx = tc.get("index", 0)
                    tc_id = tc.get("id")
                    tc_func = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
                    tc_name = tc_func.get("name", "")
                    tc_args = tc_func.get("arguments", "")

                    if oai_idx not in active_tool_use:
                        real_idx = next_block_idx
                        next_block_idx += 1
                        active_tool_use[oai_idx] = {
                            "real_idx": real_idx,
                            "id": tc_id or "",
                            "name": tc_name or ""
                        }
                        # 发送 content_block_start（等到有 id/name 或第一个 delta 时）
                        if tc_id or tc_name:
                            yield _emit_block_start(real_idx, "tool_use", {"id": tc_id or "", "name": tc_name or ""})
                            active_tool_use[oai_idx]["started"] = True
                    else:
                        info = active_tool_use[oai_idx]
                        # 如果之前没有 id/name，现在补充
                        if not info.get("started") and (tc_id or tc_name):
                            info["id"] = tc_id or info["id"]
                            info["name"] = tc_name or info["name"]
                            yield _emit_block_start(info["real_idx"], "tool_use", {"id": info["id"], "name": info["name"]})
                            info["started"] = True

                    info = active_tool_use.get(oai_idx)
                    if info and tc_args:
                        yield _emit_block_delta(info["real_idx"], "input_json_delta", tc_args)

            prev_delta_type = current_type

            finish_reason = choice.get("finish_reason")
            if finish_reason:
                stop_reason = _map_openai_finish_reason_to_anthropic(finish_reason)

        # ── 关闭所有剩余 block ──
        for ev in _close_current_blocks():
            yield ev

        # ── 消息收尾事件 ──
        if not sent_message_start:
            yield _format_anthropic_sse_event("message_start", {
                "type": "message_start",
                "message": {
                    "id": message_id, "type": "message", "role": "assistant",
                    "model": model_name, "content": [],
                    "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": input_tokens}
                }
            })

        yield _format_anthropic_sse_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens}
        })

        yield _format_anthropic_sse_event("message_stop", {"type": "message_stop"})

    return StreamingResponse(
        anthropic_stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked",
        },
    )


# ============================================================================
# 核心分发逻辑
# ============================================================================

async def _dispatch_chat_completions_core(
    openai_req: Dict[str, Any],
    request: Request,
    CONFIG: dict,
    MODEL_ENDPOINT_MAP: dict,
    MODEL_NAME_TO_ID_MAP: dict,
    MODEL_ROUND_ROBIN_INDEX: dict,
    MODEL_ROUND_ROBIN_LOCK,
    last_activity_time_setter,
    VERIFICATION_COOLDOWN_UNTIL,
    IS_REFRESHING_FOR_VERIFICATION,
    browser_ws,
    browser_connections: dict,
    browser_connections_lock,
    tab_request_counts: dict,
    response_channels: dict,
    request_metadata: dict,
    pending_requests_queue,
    monitoring_service,
    direct_api_service,
    aiohttp_session,
    IMAGE_BASE64_CACHE: dict,
    IMAGE_CACHE_MAX_SIZE: int,
    IMAGE_CACHE_TTL: int,
    save_downloaded_image_async_func,
    download_image_data_with_retry_func,
    release_tab_request_func,
    select_best_tab_for_request_func,
    convert_openai_to_lmarena_payload_func,
    process_lmarena_stream_func,
    stream_generator_func,
    non_stream_response_func,
    format_openai_chunk_func,
    format_openai_finish_chunk_func,
    format_openai_error_chunk_func,
    format_openai_non_stream_response_func,
    estimate_message_tokens_func,
    estimate_tokens_func,
    process_image_data_func,
    skip_api_auth: bool = False,
):
    model_name = openai_req.get("model")

    # 优先从 MODEL_ENDPOINT_MAP 获取模型类型
    model_type = "text"
    endpoint_mapping = MODEL_ENDPOINT_MAP.get(model_name)
    if endpoint_mapping:
        if isinstance(endpoint_mapping, dict) and "type" in endpoint_mapping:
            model_type = endpoint_mapping.get("type", "text")
        elif isinstance(endpoint_mapping, list) and endpoint_mapping:
            first_mapping = endpoint_mapping[0] if isinstance(endpoint_mapping[0], dict) else {}
            if "type" in first_mapping:
                model_type = first_mapping.get("type", "text")

    # 回退到 models.json
    model_info = MODEL_NAME_TO_ID_MAP.get(model_name, {})
    if not (
        endpoint_mapping
        and (
            (isinstance(endpoint_mapping, dict) and "type" in endpoint_mapping)
            or (isinstance(endpoint_mapping, list) and endpoint_mapping and "type" in endpoint_mapping[0])
        )
    ):
        model_type = model_info.get("type", "text")

    # 检测Direct API模式
    endpoint_config = MODEL_ENDPOINT_MAP.get(model_name) if model_name else None

    # 处理多端点情况
    if isinstance(endpoint_config, list) and endpoint_config:
        async with MODEL_ROUND_ROBIN_LOCK:
            if model_name not in MODEL_ROUND_ROBIN_INDEX:
                MODEL_ROUND_ROBIN_INDEX[model_name] = 0
            current_index = MODEL_ROUND_ROBIN_INDEX[model_name]
            endpoint_config = endpoint_config[current_index]
            MODEL_ROUND_ROBIN_INDEX[model_name] = (current_index + 1) % len(MODEL_ENDPOINT_MAP[model_name])
            logger.info(f"[DIRECT_API] 多端点轮询: 模型'{model_name}' 选择端点#{current_index + 1}")

    # 如果是Direct API模式，跳过浏览器连接检查
    is_direct_api_mode = isinstance(endpoint_config, dict) and endpoint_config.get("api_type") in ["direct_api", "gemini_native"]

    # API Key 验证（所有模式都需要验证）
    if not skip_api_auth:
        await asyncio.to_thread(
            _validate_request_api_key,
            request=request,
            model_name=model_name,
            CONFIG=CONFIG
        )

    # 连接检查与自动重试逻辑（Direct API模式跳过）
    if not browser_ws and not is_direct_api_mode:
        if CONFIG.get("enable_auto_retry", False):
            logger.warning("油猴脚本未连接，但自动重试已启用。请求将被暂存。")

            future = asyncio.get_running_loop().create_future()

            await pending_requests_queue.put({
                "future": future,
                "request_data": openai_req
            })

            logger.info(f"一个新请求已被放入暂存队列。当前队列大小: {pending_requests_queue.qsize()}")

            try:
                timeout = CONFIG.get("retry_timeout_seconds", 120)
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"一个暂存的请求等待了 {timeout} 秒后超时。")
                raise GatewayTimeoutError(
                    f"浏览器与服务器连接断开，并在 {timeout} 秒内未能恢复。请求失败。"
                ).to_http_exception()
        else:
            raise BrowserNotConnectedError().to_http_exception()

    if IS_REFRESHING_FOR_VERIFICATION and not browser_ws:
        raise ServiceUnavailableError(
            "正在等待浏览器刷新以完成人机验证，请在几秒钟后重试。"
        ).to_http_exception()

    # Direct API模式处理
    if is_direct_api_mode:
        return await handle_direct_api_request(
            openai_req=openai_req,
            model_name=model_name,
            endpoint_config=endpoint_config,
            CONFIG=CONFIG,
            PROCESSED_IMAGE_CACHE=IMAGE_BASE64_CACHE,
            monitoring_service=monitoring_service,
            direct_api_service=direct_api_service,
            estimate_message_tokens_func=estimate_message_tokens_func,
            estimate_tokens_func=estimate_tokens_func,
            process_image_data_func=process_image_data_func,
            full_messages=openai_req.get("messages", []),
        )

    # LMArena模式处理
    return await handle_lmarena_request(
        openai_req=openai_req,
        model_name=model_name,
        model_type=model_type,
        CONFIG=CONFIG,
        MODEL_ENDPOINT_MAP=MODEL_ENDPOINT_MAP,
        MODEL_ROUND_ROBIN_INDEX=MODEL_ROUND_ROBIN_INDEX,
        MODEL_ROUND_ROBIN_LOCK=MODEL_ROUND_ROBIN_LOCK,
        browser_connections=browser_connections,
        browser_connections_lock=browser_connections_lock,
        tab_request_counts=tab_request_counts,
        response_channels=response_channels,
        request_metadata=request_metadata,
        monitoring_service=monitoring_service,
        aiohttp_session=aiohttp_session,
        IMAGE_BASE64_CACHE=IMAGE_BASE64_CACHE,
        IMAGE_CACHE_MAX_SIZE=IMAGE_CACHE_MAX_SIZE,
        IMAGE_CACHE_TTL=IMAGE_CACHE_TTL,
        save_downloaded_image_async_func=save_downloaded_image_async_func,
        download_image_data_with_retry_func=download_image_data_with_retry_func,
        release_tab_request_func=release_tab_request_func,
        select_best_tab_for_request_func=select_best_tab_for_request_func,
        convert_openai_to_lmarena_payload_func=convert_openai_to_lmarena_payload_func,
        process_lmarena_stream_func=process_lmarena_stream_func,
        stream_generator_func=stream_generator_func,
        non_stream_response_func=non_stream_response_func,
        format_openai_chunk_func=format_openai_chunk_func,
        format_openai_finish_chunk_func=format_openai_finish_chunk_func,
        format_openai_error_chunk_func=format_openai_error_chunk_func,
        format_openai_non_stream_response_func=format_openai_non_stream_response_func,
        estimate_message_tokens_func=estimate_message_tokens_func,
        estimate_tokens_func=estimate_tokens_func,
        process_image_data_func=process_image_data_func,
        IS_REFRESHING_FOR_VERIFICATION=IS_REFRESHING_FOR_VERIFICATION,
        VERIFICATION_COOLDOWN_UNTIL=VERIFICATION_COOLDOWN_UNTIL,
    )


# ============================================================================
# 公开端点
# ============================================================================

async def chat_completions(
    request: Request,
    CONFIG: dict,
    MODEL_ENDPOINT_MAP: dict,
    MODEL_NAME_TO_ID_MAP: dict,
    MODEL_ROUND_ROBIN_INDEX: dict,
    MODEL_ROUND_ROBIN_LOCK,
    last_activity_time_setter,
    VERIFICATION_COOLDOWN_UNTIL,
    IS_REFRESHING_FOR_VERIFICATION,
    browser_ws,
    browser_connections: dict,
    browser_connections_lock,
    tab_request_counts: dict,
    response_channels: dict,
    request_metadata: dict,
    pending_requests_queue,
    monitoring_service,
    direct_api_service,
    aiohttp_session,
    IMAGE_BASE64_CACHE: dict,
    IMAGE_CACHE_MAX_SIZE: int,
    IMAGE_CACHE_TTL: int,
    save_downloaded_image_async_func,
    download_image_data_with_retry_func,
    release_tab_request_func,
    select_best_tab_for_request_func,
    convert_openai_to_lmarena_payload_func,
    process_lmarena_stream_func,
    stream_generator_func,
    non_stream_response_func,
    format_openai_chunk_func,
    format_openai_finish_chunk_func,
    format_openai_error_chunk_func,
    format_openai_non_stream_response_func,
    estimate_message_tokens_func,
    estimate_tokens_func,
    process_image_data_func,
):
    """处理聊天补全请求的入口函数。
    根据模型配置分发到对应的处理逻辑（Direct API或LMArena模式）。"""
    last_activity_time_setter(datetime.now())
    logger.info("API请求已收到，活动时间已更新")

    if VERIFICATION_COOLDOWN_UNTIL is not None:
        remaining = VERIFICATION_COOLDOWN_UNTIL - time.time()
        if remaining > 0:
            adjusted_remaining = max(0, int(remaining - 3))
            logger.warning(f"⏰ 请求被拒绝：人机验证冷却中（剩余 {int(remaining)} 秒）")
            raise VerificationRequiredError(adjusted_remaining).to_http_exception()

    try:
        openai_req = await _read_request_json_non_blocking(request)
    except json.JSONDecodeError:
        raise BadRequestError("无效的 JSON 请求体").to_http_exception()
    except Exception as e:
        if "ClientDisconnect" in type(e).__name__ or "Disconnect" in type(e).__name__:
            logger.warning("⚡ 客户端在请求发送完成前断开了连接，忽略此请求")
            from starlette.responses import JSONResponse
            return JSONResponse(status_code=499, content={"error": "Client Disconnected"})
        raise

    return await _dispatch_chat_completions_core(
        openai_req=openai_req,
        request=request,
        CONFIG=CONFIG,
        MODEL_ENDPOINT_MAP=MODEL_ENDPOINT_MAP,
        MODEL_NAME_TO_ID_MAP=MODEL_NAME_TO_ID_MAP,
        MODEL_ROUND_ROBIN_INDEX=MODEL_ROUND_ROBIN_INDEX,
        MODEL_ROUND_ROBIN_LOCK=MODEL_ROUND_ROBIN_LOCK,
        last_activity_time_setter=last_activity_time_setter,
        VERIFICATION_COOLDOWN_UNTIL=VERIFICATION_COOLDOWN_UNTIL,
        IS_REFRESHING_FOR_VERIFICATION=IS_REFRESHING_FOR_VERIFICATION,
        browser_ws=browser_ws,
        browser_connections=browser_connections,
        browser_connections_lock=browser_connections_lock,
        tab_request_counts=tab_request_counts,
        response_channels=response_channels,
        request_metadata=request_metadata,
        pending_requests_queue=pending_requests_queue,
        monitoring_service=monitoring_service,
        direct_api_service=direct_api_service,
        aiohttp_session=aiohttp_session,
        IMAGE_BASE64_CACHE=IMAGE_BASE64_CACHE,
        IMAGE_CACHE_MAX_SIZE=IMAGE_CACHE_MAX_SIZE,
        IMAGE_CACHE_TTL=IMAGE_CACHE_TTL,
        save_downloaded_image_async_func=save_downloaded_image_async_func,
        download_image_data_with_retry_func=download_image_data_with_retry_func,
        release_tab_request_func=release_tab_request_func,
        select_best_tab_for_request_func=select_best_tab_for_request_func,
        convert_openai_to_lmarena_payload_func=convert_openai_to_lmarena_payload_func,
        process_lmarena_stream_func=process_lmarena_stream_func,
        stream_generator_func=stream_generator_func,
        non_stream_response_func=non_stream_response_func,
        format_openai_chunk_func=format_openai_chunk_func,
        format_openai_finish_chunk_func=format_openai_finish_chunk_func,
        format_openai_error_chunk_func=format_openai_error_chunk_func,
        format_openai_non_stream_response_func=format_openai_non_stream_response_func,
        estimate_message_tokens_func=estimate_message_tokens_func,
        estimate_tokens_func=estimate_tokens_func,
        process_image_data_func=process_image_data_func,
    )


async def anthropic_messages(
    request: Request,
    CONFIG: dict,
    MODEL_ENDPOINT_MAP: dict,
    MODEL_NAME_TO_ID_MAP: dict,
    MODEL_ROUND_ROBIN_INDEX: dict,
    MODEL_ROUND_ROBIN_LOCK,
    last_activity_time_setter,
    VERIFICATION_COOLDOWN_UNTIL,
    IS_REFRESHING_FOR_VERIFICATION,
    browser_ws,
    browser_connections: dict,
    browser_connections_lock,
    tab_request_counts: dict,
    response_channels: dict,
    request_metadata: dict,
    pending_requests_queue,
    monitoring_service,
    direct_api_service,
    aiohttp_session,
    IMAGE_BASE64_CACHE: dict,
    IMAGE_CACHE_MAX_SIZE: int,
    IMAGE_CACHE_TTL: int,
    save_downloaded_image_async_func,
    download_image_data_with_retry_func,
    release_tab_request_func,
    select_best_tab_for_request_func,
    convert_openai_to_lmarena_payload_func,
    process_lmarena_stream_func,
    stream_generator_func,
    non_stream_response_func,
    format_openai_chunk_func,
    format_openai_finish_chunk_func,
    format_openai_error_chunk_func,
    format_openai_non_stream_response_func,
    estimate_message_tokens_func,
    estimate_tokens_func,
    process_image_data_func,
):
    """
    处理 Anthropic Claude 兼容接口：/v1/messages
    - 输入：Anthropic messages 格式
    - 内部：转换到 OpenAI chat.completions 流程
    - 输出：再转换回 Anthropic 格式（含流式 SSE 事件）
    
    支持：text / image / tool_use / tool_result / thinking / tools / tool_choice
    支持：x-api-key 头认证（与 Bearer 等效）
    """
    last_activity_time_setter(datetime.now())
    logger.info("[ANTHROPIC_COMPAT] /v1/messages 请求已收到，活动时间已更新")

    if VERIFICATION_COOLDOWN_UNTIL is not None:
        remaining = VERIFICATION_COOLDOWN_UNTIL - time.time()
        if remaining > 0:
            adjusted_remaining = max(0, int(remaining - 3))
            logger.warning(f"⏰ 请求被拒绝：人机验证冷却中（剩余 {int(remaining)} 秒）")
            raise VerificationRequiredError(adjusted_remaining).to_http_exception()

    try:
        anthropic_req = await _read_request_json_non_blocking(request)
    except json.JSONDecodeError:
        raise BadRequestError("无效的 JSON 请求体").to_http_exception()

    model_name = anthropic_req.get("model")
    if not model_name:
        raise BadRequestError("Anthropic 请求缺少 'model' 字段").to_http_exception()

    # API Key 验证（支持 x-api-key / Bearer 双模式）
    await asyncio.to_thread(
        _validate_request_api_key,
        request=request,
        model_name=model_name,
        CONFIG=CONFIG
    )

    # 先解析端点配置，支持 Claude /messages 原样透传
    endpoint_config = await _select_endpoint_config_for_model(
        model_name=model_name,
        MODEL_ENDPOINT_MAP=MODEL_ENDPOINT_MAP,
        MODEL_ROUND_ROBIN_INDEX=MODEL_ROUND_ROBIN_INDEX,
        MODEL_ROUND_ROBIN_LOCK=MODEL_ROUND_ROBIN_LOCK,
    )

    is_direct_api = isinstance(endpoint_config, dict) and endpoint_config.get("api_type") in ["direct_api", "gemini_native"]
    endpoint_path = (endpoint_config.get("endpoint_path", "/chat/completions") if isinstance(endpoint_config, dict) else "/chat/completions") or "/chat/completions"
    endpoint_path = endpoint_path.strip()
    if not endpoint_path.startswith("/"):
        endpoint_path = "/" + endpoint_path

    # ── Claude 原生直通 ──
    if (
        is_direct_api
        and isinstance(endpoint_config, dict)
        and endpoint_config.get("passthrough", False)
        and endpoint_path.lower().endswith("/messages")
    ):
        if not direct_api_service:
            raise ServiceUnavailableError("Direct API service not initialized").to_http_exception()

        api_base_url = endpoint_config.get("api_base_url")
        if not api_base_url:
            raise BadRequestError(f"模型 '{model_name}' 配置缺少 api_base_url").to_http_exception()

        upstream_api_key = endpoint_config.get("api_key", "")
        is_stream = bool(anthropic_req.get("stream", False))

        logger.info(f"[ANTHROPIC_COMPAT] 直通模式启用: model={model_name}, endpoint_path={endpoint_path}, stream={is_stream}")

        if is_stream:
            passthrough_iter = direct_api_service.call_api_passthrough(
                base_url=api_base_url,
                api_key=upstream_api_key,
                request_body=anthropic_req,
                endpoint_path=endpoint_path
            )
            return StreamingResponse(
                passthrough_iter,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                    "Transfer-Encoding": "chunked",
                },
            )
        else:
            response_bytes = b""
            async for chunk in direct_api_service.call_api_passthrough(
                base_url=api_base_url,
                api_key=upstream_api_key,
                request_body=anthropic_req,
                endpoint_path=endpoint_path
            ):
                response_bytes += chunk
            return Response(content=response_bytes, media_type="application/json")

    # ── Anthropic → OpenAI 转换模式 ──
    try:
        openai_req = _convert_anthropic_to_openai_request(anthropic_req)
    except ValueError as e:
        raise BadRequestError(str(e)).to_http_exception()

    openai_response = await _dispatch_chat_completions_core(
        openai_req=openai_req,
        request=request,
        CONFIG=CONFIG,
        MODEL_ENDPOINT_MAP=MODEL_ENDPOINT_MAP,
        MODEL_NAME_TO_ID_MAP=MODEL_NAME_TO_ID_MAP,
        MODEL_ROUND_ROBIN_INDEX=MODEL_ROUND_ROBIN_INDEX,
        MODEL_ROUND_ROBIN_LOCK=MODEL_ROUND_ROBIN_LOCK,
        last_activity_time_setter=last_activity_time_setter,
        VERIFICATION_COOLDOWN_UNTIL=VERIFICATION_COOLDOWN_UNTIL,
        IS_REFRESHING_FOR_VERIFICATION=IS_REFRESHING_FOR_VERIFICATION,
        browser_ws=browser_ws,
        browser_connections=browser_connections,
        browser_connections_lock=browser_connections_lock,
        tab_request_counts=tab_request_counts,
        response_channels=response_channels,
        request_metadata=request_metadata,
        pending_requests_queue=pending_requests_queue,
        monitoring_service=monitoring_service,
        direct_api_service=direct_api_service,
        aiohttp_session=aiohttp_session,
        IMAGE_BASE64_CACHE=IMAGE_BASE64_CACHE,
        IMAGE_CACHE_MAX_SIZE=IMAGE_CACHE_MAX_SIZE,
        IMAGE_CACHE_TTL=IMAGE_CACHE_TTL,
        save_downloaded_image_async_func=save_downloaded_image_async_func,
        download_image_data_with_retry_func=download_image_data_with_retry_func,
        release_tab_request_func=release_tab_request_func,
        select_best_tab_for_request_func=select_best_tab_for_request_func,
        convert_openai_to_lmarena_payload_func=convert_openai_to_lmarena_payload_func,
        process_lmarena_stream_func=process_lmarena_stream_func,
        stream_generator_func=stream_generator_func,
        non_stream_response_func=non_stream_response_func,
        format_openai_chunk_func=format_openai_chunk_func,
        format_openai_finish_chunk_func=format_openai_finish_chunk_func,
        format_openai_error_chunk_func=format_openai_error_chunk_func,
        format_openai_non_stream_response_func=format_openai_non_stream_response_func,
        estimate_message_tokens_func=estimate_message_tokens_func,
        estimate_tokens_func=estimate_tokens_func,
        process_image_data_func=process_image_data_func,
        skip_api_auth=True,
    )

    # 流式：OpenAI SSE -> Anthropic SSE
    if openai_req.get("stream", False) and isinstance(openai_response, StreamingResponse):
        return _build_anthropic_streaming_response(
            openai_streaming_response=openai_response,
            request_model=openai_req.get("model", anthropic_req.get("model", "unknown")),
        )

    # 非流式：OpenAI JSON -> Anthropic JSON
    return await _convert_openai_non_stream_response_to_anthropic(
        openai_response=openai_response,
        request_model=openai_req.get("model", anthropic_req.get("model", "unknown")),
    )
