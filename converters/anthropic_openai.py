"""
Anthropic ↔ OpenAI 协议转换模块

从 routes/api_routes.py 拆分而来，包含：
- Anthropic Messages 请求 → OpenAI chat.completions 请求
- OpenAI 响应（流式/非流式）→ Anthropic 响应
- Anthropic SSE 旁路解析（用于监控记录）
"""
import codecs
import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

from fastapi.responses import JSONResponse, StreamingResponse, Response

from utils.json_unescape import StreamingUnicodeUnescaper, normalize_tool_args_json

logger = logging.getLogger(__name__)

__all__ = [
    "map_openai_finish_reason_to_anthropic",
    "format_anthropic_sse_event",
    "convert_anthropic_tools_to_openai",
    "convert_anthropic_tool_choice_to_openai",
    "extract_system_text",
    "convert_anthropic_content_to_openai",
    "convert_anthropic_to_openai_request",
    "build_anthropic_error_payload",
    "extract_anthropic_usage_tokens",
    "convert_openai_usage_to_anthropic",
    "sanitize_anthropic_thinking_blocks",
    "extract_anthropic_response_content",
    "extract_anthropic_sse_content",
    "flush_anthropic_sse_state",
    "read_response_body_bytes",
    "extract_openai_message_text",
    "extract_openai_message_reasoning",
    "convert_openai_non_stream_response_to_anthropic",
    "iter_openai_sse_payloads",
    "build_anthropic_streaming_response",
]


# ============================================================================
# Anthropic → OpenAI 请求转换
# ============================================================================

def map_openai_finish_reason_to_anthropic(reason: Optional[str]) -> str:
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "content_filter": "stop_sequence",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
    }
    return mapping.get(reason or "stop", "end_turn")


def format_anthropic_sse_event(event_name: str, payload: Dict[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def convert_anthropic_tools_to_openai(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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


def convert_anthropic_tool_choice_to_openai(tool_choice: Any) -> Any:
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


def extract_system_text(system_field: Any) -> str:
    if isinstance(system_field, str):
        return system_field
    if isinstance(system_field, list):
        texts: List[str] = []
        for block in system_field:
            if isinstance(block, dict) and block.get("type") in ("text", "input_text"):
                texts.append(str(block.get("text", "")))
        return "\n".join([t for t in texts if t])
    return ""


def convert_anthropic_content_to_openai(content: Any) -> Any:
    """将 Anthropic content 转换为 OpenAI content 格式。

    处理 text / image / tool_result / tool_use / thinking 等块类型。
    注意：assistant 消息中的 tool_use 和 user 消息中的 tool_result
    的完整结构化转换在 convert_anthropic_to_openai_request 中处理，
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
                            "url": "data:" + str(media_type) + ";base64," + str(data)
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


def convert_anthropic_to_openai_request(anthropic_req: Dict[str, Any]) -> Dict[str, Any]:
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

    system_text = extract_system_text(anthropic_req.get("system"))
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

            content = convert_anthropic_content_to_openai(text_blocks if text_blocks else " ")

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
                    content = convert_anthropic_content_to_openai(non_tool_blocks)
                    openai_messages.append({"role": "user", "content": content})
                continue

        # ── 默认处理 ──
        content = convert_anthropic_content_to_openai(raw_content)
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
        openai_tools = convert_anthropic_tools_to_openai(tools)
        if openai_tools:
            openai_req["tools"] = openai_tools
            logger.info(f"[ANTHROPIC_CONVERT] 已转换 {len(openai_tools)} 个 tools → OpenAI 格式")

    # ── tool_choice 转换 ──
    tool_choice = anthropic_req.get("tool_choice")
    if tool_choice is not None:
        oai_tool_choice = convert_anthropic_tool_choice_to_openai(tool_choice)
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

def build_anthropic_error_payload(error_obj: Any) -> Dict[str, Any]:
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


def extract_anthropic_usage_tokens(usage: Any) -> tuple:
    """从 Anthropic usage dict 提取 (总输入, 输出, 缓存命中) token 数。

    Anthropic 启用 prompt caching 时，input_tokens 只包含未命中缓存的部分；
    缓存命中在 cache_read_input_tokens、缓存写入在 cache_creation_input_tokens，
    三者相加才是真实的总输入。
    """
    if not isinstance(usage, dict):
        return 0, 0, 0
    base_input = int(usage.get("input_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    cache_creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
    output = int(usage.get("output_tokens", 0) or 0)
    return base_input + cache_read + cache_creation, output, cache_read


def extract_anthropic_response_content(response_json: dict) -> tuple:
    """从 Anthropic 响应 JSON 中提取内容用于监控记录。

    返回 (content_text, reasoning_text, tool_calls_list, input_tokens, output_tokens, cached_tokens)
    """
    content_text = ""
    reasoning_text = ""
    tool_calls = []
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0

    if not isinstance(response_json, dict):
        return content_text, reasoning_text, tool_calls, input_tokens, output_tokens, cached_tokens

    content_blocks = response_json.get("content")
    if isinstance(content_blocks, list):
        text_parts = []
        thinking_parts = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = block.get("text", "")
                if t:
                    text_parts.append(t)
            elif btype == "thinking":
                t = block.get("thinking", "")
                if t:
                    thinking_parts.append(t)
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)
                    }
                })
        content_text = "\n".join(text_parts)
        reasoning_text = "\n".join(thinking_parts)

    input_tokens, output_tokens, cached_tokens = extract_anthropic_usage_tokens(
        response_json.get("usage"))

    return content_text, reasoning_text, tool_calls, input_tokens, output_tokens, cached_tokens


def _normalize_tool_args_str(args_str: str) -> str:
    """规范化工具调用参数字符串（委托 utils.json_unescape）。

    模型在长上下文下有时会用 ASCII 转义风格输出 JSON（中文变成 \\uXXXX），
    语义合法但完全不可读。合法 JSON 重新序列化为中文明文；非法 JSON
    （流截断等）退化为字符级安全解码，转义仍被还原。
    """
    return normalize_tool_args_json(args_str)


def extract_anthropic_sse_content(chunk_bytes: bytes, state: dict) -> None:
    """旁路解析 Anthropic SSE chunk，累积内容到 state dict。

    state 包含: content_parts, reasoning_parts, tool_calls, input_tokens,
    output_tokens, cached_tokens；内部使用 _sse_buf 做跨 chunk 行缓冲。

    🔧 修复：SSE 的 data 行经常被 TCP chunk 边界切断，旧版对每个 chunk
    独立按行解析，被切断的行 json.loads 失败后直接丢弃，导致监控日志中
    工具调用参数缺失中段。现在把最后一个换行之后的不完整尾部字节缓冲到
    state["_sse_buf"]，与下一个 chunk 拼接后再解析；UTF-8 多字节序列不含
    换行字节(0x0A)，因此按最后一个换行切分同时保证字符不会被解码截断。
    流结束后调用 flush_anthropic_sse_state 冲刷残留。
    """
    buf = state.get("_sse_buf", b"") + chunk_bytes
    last_nl = buf.rfind(b"\n")
    if last_nl < 0:
        state["_sse_buf"] = buf
        return
    state["_sse_buf"] = buf[last_nl + 1:]
    try:
        chunk_str = buf[:last_nl + 1].decode("utf-8", errors="replace")
    except Exception:
        return
    _parse_anthropic_sse_lines(chunk_str, state)


def flush_anthropic_sse_state(state: dict) -> None:
    """流结束时冲刷旁路解析的残留状态。

    1. 处理 _sse_buf 中最后一个不完整行（正常 SSE 流以换行结尾，此时为空操作）；
    2. 把未收到 content_block_stop 的工具参数落入对应 tool_call（流中断场景）。
    """
    buf = state.pop("_sse_buf", b"")
    if buf:
        try:
            _parse_anthropic_sse_lines(buf.decode("utf-8", errors="replace"), state)
        except Exception:
            pass
    pending = state.pop("pending_tool_args", "")
    tidx = state.pop("current_tool_idx", None)
    if pending and tidx is not None and tidx < len(state.get("tool_calls", [])):
        state["tool_calls"][tidx]["function"]["arguments"] = _normalize_tool_args_str(pending)


def _parse_anthropic_sse_lines(chunk_str: str, state: dict) -> None:
    """解析若干完整的 SSE 行，累积事件内容到 state（仅供上面两个入口调用）。"""
    for line in chunk_str.splitlines():
        line = line.strip()
        # 🔧 修复：只处理 data: 行；旧版本未检查前缀，会把 event: 行也
        # 截掉前 5 个字符再尝试 json.loads（碰巧失败被吞，但属于逻辑错误）
        if not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if not data_str or data_str == "[DONE]":
            continue
        try:
            event = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        etype = event.get("type")
        if etype == "content_block_delta":
            delta = event.get("delta")
            if not isinstance(delta, dict):
                continue
            dtype = delta.get("type")
            if dtype == "text_delta":
                t = delta.get("text", "")
                if t:
                    state["content_parts"].append(t)
            elif dtype == "thinking_delta":
                t = delta.get("thinking", "")
                if t:
                    state["reasoning_parts"].append(t)
            elif dtype == "input_json_delta":
                t = delta.get("partial_json", "")
                if t and "pending_tool_args" not in state:
                    state["pending_tool_args"] = ""
                if t:
                    state["pending_tool_args"] = state.get("pending_tool_args", "") + t
        elif etype == "content_block_start":
            cb = event.get("content_block")
            if isinstance(cb, dict) and cb.get("type") == "tool_use":
                # tool_use block 开始，记录 id/name
                state.setdefault("tool_calls", []).append({
                    "id": cb.get("id", ""),
                    "type": "function",
                    "function": {"name": cb.get("name", ""), "arguments": ""}
                })
                state["current_tool_idx"] = len(state["tool_calls"]) - 1
                state["pending_tool_args"] = ""
        elif etype == "content_block_stop":
            # 把累积的 tool args 存入对应的 tool_call（顺带把 \uXXXX 转义规范化为明文）
            pending = state.pop("pending_tool_args", "")
            tidx = state.pop("current_tool_idx", None)
            if tidx is not None and tidx < len(state.get("tool_calls", [])):
                state["tool_calls"][tidx]["function"]["arguments"] = _normalize_tool_args_str(pending) or "{}"
        elif etype == "message_delta":
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict) and raw_usage:
                state.setdefault("upstream_usage", {}).update(raw_usage)
            inp, outp, cached = extract_anthropic_usage_tokens(raw_usage)
            if inp:
                state["input_tokens"] = inp
            if outp:
                state["output_tokens"] = outp
            if cached:
                state["cached_tokens"] = cached
        elif etype == "message_start":
            msg = event.get("message")
            if isinstance(msg, dict):
                raw_usage = msg.get("usage")
                if isinstance(raw_usage, dict) and raw_usage:
                    state.setdefault("upstream_usage", {}).update(raw_usage)
                inp, outp, cached = extract_anthropic_usage_tokens(raw_usage)
                if inp:
                    state["input_tokens"] = inp
                if outp:
                    state["output_tokens"] = outp
                if cached:
                    state["cached_tokens"] = cached


# ============================================================================
# OpenAI → Anthropic 响应转换
# ============================================================================

async def read_response_body_bytes(response: Response) -> bytes:
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


def extract_openai_message_text(choice: Dict[str, Any]) -> str:
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


def extract_openai_message_reasoning(choice: Dict[str, Any]) -> str:
    """从 OpenAI choice 中提取 reasoning_content。"""
    message = choice.get("message", {}) if isinstance(choice, dict) else {}
    reasoning = message.get("reasoning_content", "")
    return reasoning if isinstance(reasoning, str) else ""


def convert_openai_usage_to_anthropic(usage: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """OpenAI usage → Anthropic 官方 usage 格式。

    语义差异：内部 OpenAI 格式的 prompt_tokens 是总输入（含缓存命中），
    缓存命中量在 prompt_tokens_details.cached_tokens；而 Anthropic 官方的
    input_tokens 仅统计未命中缓存的部分，缓存命中量放 cache_read_input_tokens。
    统一按官方四字段返回，避免客户端解析不到用量。
    """
    if not isinstance(usage, dict):
        usage = {}
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    details = usage.get("prompt_tokens_details")
    cached_tokens = int(details.get("cached_tokens", 0) or 0) if isinstance(details, dict) else 0
    return {
        "input_tokens": max(0, prompt_tokens - cached_tokens),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": cached_tokens,
        "output_tokens": completion_tokens,
    }


def sanitize_anthropic_thinking_blocks(messages: Any) -> Any:
    """剔除历史消息中缺失签名的 thinking / redacted_thinking block（透传前清洗）。

    OpenRouter 等上游对 /messages 请求体做严格 schema 校验：thinking block
    必须携带字符串 signature、redacted_thinking 必须携带 data，否则整个
    content 的 union 校验直接 400（"signature": expected string, received
    undefined）。经本服务从 reasoning 拼装的思维链没有签名，客户端下一轮
    原样回传时就会触发该错误。

    注意：带签名的 thinking 与带 data 的 redacted_thinking 块必须全部
    原样保留（包括 summarized 摘要块），Anthropic 会逐块校验历史思维链，
    剔除或修改任意一块都会触发 "thinking blocks cannot be modified"。

    返回新的 messages 列表，不修改传入对象；若某条消息的 content 剔除后
    为空，则整条消息一并移除（空 content 数组同样无法通过上游校验）。
    """
    if not isinstance(messages, list):
        return messages

    sanitized: List[Dict[str, Any]] = []
    removed_blocks = 0
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            sanitized.append(msg)
            continue

        kept_blocks = []
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type")
                if btype == "thinking" and not block.get("signature"):
                    removed_blocks += 1
                    continue
                if btype == "redacted_thinking" and not block.get("data"):
                    removed_blocks += 1
                    continue
            kept_blocks.append(block)

        if len(kept_blocks) == len(content):
            sanitized.append(msg)
        elif kept_blocks:
            sanitized.append({**msg, "content": kept_blocks})
        else:
            # 保留占位块避免破坏角色交替（连续同 role 消息会触发 Anthropic 400）
            sanitized.append({**msg, "content": [{"type": "text", "text": "[content sanitized]"}]})

    if removed_blocks:
        logger.info(
            f"[ANTHROPIC_COMPAT] 已剔除 {removed_blocks} 个缺失签名的 "
            f"thinking/redacted_thinking block（上游严格校验兼容）"
        )
    return sanitized


async def convert_openai_non_stream_response_to_anthropic(
    openai_response: Response,
    request_model: str
) -> Response:
    """非流式：OpenAI JSON 响应 → Anthropic JSON 响应（支持多 content blocks）"""
    status_code = getattr(openai_response, "status_code", 200)
    raw_body = await read_response_body_bytes(openai_response)

    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return JSONResponse(
            status_code=500,
            content=build_anthropic_error_payload({
                "type": "api_error",
                "message": "上游返回了无法解析的JSON响应"
            })
        )

    if status_code >= 400 or (isinstance(data, dict) and data.get("error") is not None):
        return JSONResponse(
            status_code=status_code if status_code >= 400 else 500,
            content=build_anthropic_error_payload(data.get("error", data))
        )

    choices = data.get("choices", [])
    first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    finish_reason = first_choice.get("finish_reason")
    message = first_choice.get("message", {}) if isinstance(first_choice, dict) else {}

    anthropic_usage = convert_openai_usage_to_anthropic(data.get("usage"))

    # ── 构建 Anthropic content blocks（多 block 支持）──
    content_blocks: List[Dict[str, Any]] = []
    reasoning_content = extract_openai_message_reasoning(first_choice)
    text_content = extract_openai_message_text(first_choice)
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
        "stop_reason": map_openai_finish_reason_to_anthropic(finish_reason),
        "stop_sequence": None,
        "usage": anthropic_usage
    }

    return JSONResponse(status_code=200, content=anthropic_response)


# SSE 事件分隔符：兼容 LF 空行与 CRLF 空行
_SSE_EVENT_SPLIT = re.compile(r"\r?\n\r?\n")


async def iter_openai_sse_payloads(upstream_response: StreamingResponse):
    """从 OpenAI SSE 流中逐个提取负载字符串。

    🔧 修复：
    - bytes chunk 改用增量 UTF-8 解码器，旧版 errors="ignore" 会在
      多字节字符被 chunk 边界切断时静默丢字；
    - 事件分隔符兼容 CRLF，旧版只认 \\n\\n，上游用 CRLF 时缓冲区
      会无限增长直到流结束才一次性处理。
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    async for raw_chunk in upstream_response.body_iterator:
        if isinstance(raw_chunk, bytes):
            chunk_text = decoder.decode(raw_chunk, final=False)
        else:
            chunk_text = str(raw_chunk)

        buffer += chunk_text
        while True:
            m = _SSE_EVENT_SPLIT.search(buffer)
            if not m:
                break
            event_block, buffer = buffer[:m.start()], buffer[m.end():]
            for line in event_block.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    yield line[5:].strip()

    buffer += decoder.decode(b"", final=True)
    if buffer.strip():
        for line in buffer.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                yield line[5:].strip()


def build_anthropic_streaming_response(
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
        # 累积上游 usage（OpenAI 流式 usage 为最终累计值而非增量，合并覆盖即可）
        merged_usage: Dict[str, Any] = {}

        # ── content block 状态机 ──
        next_block_idx = 0
        active_thinking_idx: Optional[int] = None
        active_text_idx: Optional[int] = None
        # tool_use:  {OAI_index: {"real_idx": int, "id": str, "name": str}}
        active_tool_use: Dict[int, Dict[str, Any]] = {}
        prev_delta_type: Optional[str] = None  # "thinking" | "text" | "tool_calls"

        def _emit_block_start(idx: int, block_type: str, extra: Optional[Dict[str, Any]] = None) -> str:
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
            return format_anthropic_sse_event(
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
            return format_anthropic_sse_event(
                "content_block_delta",
                {"type": "content_block_delta", "index": idx,
                 "delta": {"type": delta_type, value_key: text}}
            )

        def _emit_block_stop(idx: int) -> str:
            return format_anthropic_sse_event(
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
                # 未发过 content_block_start 的块不能发 block_stop（协议顺序）
                if info.get("started"):
                    # 冲刷参数解码器残留（正常完整 JSON 无残留；截断流原样补发）
                    unescaper = info.get("args_unescaper")
                    tail = unescaper.flush() if unescaper else ""
                    if tail:
                        events.append(_emit_block_delta(info["real_idx"], "input_json_delta", tail))
                    events.append(_emit_block_stop(info["real_idx"]))
            active_tool_use.clear()
            return events

        # ── 主循环：逐 chunk 处理 ──
        async for payload in iter_openai_sse_payloads(openai_streaming_response):
            if payload == "[DONE]":
                break

            try:
                chunk = json.loads(payload)
            except Exception:
                continue

            if isinstance(chunk, dict) and chunk.get("error") is not None:
                yield format_anthropic_sse_event(
                    "error",
                    build_anthropic_error_payload(chunk.get("error"))
                )
                return

            if not isinstance(chunk, dict):
                continue

            model_name = chunk.get("model") or model_name

            usage = chunk.get("usage")
            if isinstance(usage, dict) and usage:
                merged_usage.update({k: v for k, v in usage.items() if v is not None})

            if not sent_message_start:
                sent_message_start = True
                yield format_anthropic_sse_event("message_start", {
                    "type": "message_start",
                    "message": {
                        "id": message_id, "type": "message", "role": "assistant",
                        "model": model_name, "content": [],
                        "stop_reason": None, "stop_sequence": None,
                        "usage": convert_openai_usage_to_anthropic(merged_usage)
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
                            "name": tc_name or "",
                            # 把 arguments 增量中的 \uXXXX 转义提前解码为明文
                            "args_unescaper": StreamingUnicodeUnescaper()
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
                        if not info.get("started"):
                            # 上游首个 delta 只有 arguments 没有 id/name：
                            # 先补发 content_block_start 保证协议顺序，再发 input_json_delta
                            yield _emit_block_start(info["real_idx"], "tool_use",
                                                    {"id": info["id"], "name": info["name"]})
                            info["started"] = True
                        # 提前把 \uXXXX 转义解码为明文再发给下游（跨 chunk 安全）
                        decoded = info["args_unescaper"].feed(tc_args)
                        if decoded:
                            yield _emit_block_delta(info["real_idx"], "input_json_delta", decoded)

            prev_delta_type = current_type

            finish_reason = choice.get("finish_reason")
            if finish_reason:
                stop_reason = map_openai_finish_reason_to_anthropic(finish_reason)

        # ── 关闭所有剩余 block ──
        for ev in _close_current_blocks():
            yield ev

        # ── 消息收尾事件 ──
        if not sent_message_start:
            yield format_anthropic_sse_event("message_start", {
                "type": "message_start",
                "message": {
                    "id": message_id, "type": "message", "role": "assistant",
                    "model": model_name, "content": [],
                    "stop_reason": None, "stop_sequence": None,
                    "usage": convert_openai_usage_to_anthropic(merged_usage)
                }
            })

        # 统一按 Anthropic 官方格式输出最终用量（含缓存字段），
        # 多数客户端从 message_delta.usage 读取计费数据
        yield format_anthropic_sse_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": convert_openai_usage_to_anthropic(merged_usage)
        })

        yield format_anthropic_sse_event("message_stop", {"type": "message_stop"})

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
