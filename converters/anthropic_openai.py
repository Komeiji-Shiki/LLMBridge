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

from converters import is_content_parts
from converters.audio import audio_to_openai, split_audio_text
from converters.media import document_text, extract_file, file_part, search_result_text
from converters.anthropic_responses_signatures import (
    encode_thinking_item,
    responses_items_from_anthropic,
    responses_reasoning_text,
    responses_segments,
    thinking_carrier,
)
from converters.cache_warnings import warning_headers
from converters.gemini_anthropic_signatures import (
    decode_signature,
    gemini_parts_from_anthropic,
    gemini_segments,
)
from converters.interactions_anthropic_signatures import (
    interactions_segments,
    interactions_signature_segments,
    interactions_steps_from_anthropic,
)
from core.conversion_report import note_cache_diagnostics, note_loss
from services.error_translation import translate_anthropic_error
from services.model_capabilities import budget_to_effort
from utils.json_unescape import StreamingUnicodeUnescaper, normalize_tool_args_json
from utils.usage_tokens import total_output_tokens

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
        "content_filter": "refusal",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
    }
    return mapping.get(reason or "stop", "end_turn")


def _stop_reason(finish_reason: Optional[str], has_tool_use: bool, has_output: bool) -> str:
    """有工具调用就是 tool_use：部分上游和中转在调用后仍报 stop，靠它判断的 Agent 循环会提前结束。

    上游过滤只在既没有正文也没有工具调用时才报 refusal。Claude Code 收到 refusal 会把整轮标成违规、
    要求开新会话，而触发过滤的内容留在上下文里，之后每一轮都会再被拦，已经产出的回答也跟着作废。
    过滤照常记在监控的停止原因里。
    """
    reason = map_openai_finish_reason_to_anthropic(finish_reason)
    if reason == "refusal" and has_output:
        reason = "end_turn"
    return "tool_use" if has_tool_use and reason == "end_turn" else reason


def _source_lines(annotations: Any) -> str:
    """引用来源没有通用的 Anthropic 表示（citations 要上游签发的 encrypted_index），写成文字。"""
    seen, lines = set(), []
    for annotation in annotations if isinstance(annotations, list) else []:
        citation = annotation.get("url_citation") if isinstance(annotation, dict) else None
        url = citation.get("url") if isinstance(citation, dict) else None
        if url and url not in seen:
            seen.add(url)
            lines.append(f"- [{citation.get('title') or url}]({url})")
    return "\n\nSources:\n" + "\n".join(lines) if lines else ""


def format_anthropic_sse_event(event_name: str, payload: Dict[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


_OBJECT_WITH_ACTION = {"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]}
# 客户端自己执行的内置工具：其他上游不认识这些版本化类型，按官方参数补上 schema 和说明，
# 模型才能正确调用，调用照常回到客户端执行。
_CLIENT_TOOL_DEFINITIONS = {
    "bash_": ("Run a shell command and return its output.", {
        "type": "object",
        "properties": {"command": {"type": "string"}, "restart": {"type": "boolean"}},
    }),
    "text_editor_": ("View, create and edit files.", {
        "type": "object",
        "properties": {
            "command": {"type": "string", "enum": ["view", "create", "str_replace", "insert", "undo_edit"]},
            "path": {"type": "string"},
            "file_text": {"type": "string"},
            "old_str": {"type": "string"},
            "new_str": {"type": "string"},
            "insert_line": {"type": "integer"},
            "insert_text": {"type": "string"},
            "view_range": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["command", "path"],
    }),
    "memory_": ("Read and write files in the persistent memory directory.", {
        "type": "object",
        "properties": {
            "command": {"type": "string", "enum": ["view", "create", "str_replace", "insert", "delete", "rename"]},
            "path": {"type": "string"},
            "file_text": {"type": "string"},
            "old_str": {"type": "string"},
            "new_str": {"type": "string"},
            "insert_line": {"type": "integer"},
            "insert_text": {"type": "string"},
            "old_path": {"type": "string"},
            "new_path": {"type": "string"},
            "view_range": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["command"],
    }),
    "computer_": ("Control the computer: screenshots, mouse and keyboard.", _OBJECT_WITH_ACTION),
    "browser_": ("Control a web browser.", _OBJECT_WITH_ACTION),
}
# 由 Anthropic 服务端执行的工具，其他上游没有执行者，声明了只会得到一个没人处理的调用。
# web_search、web_fetch 不在此列：多数上游和中转会按名字接管。
_SERVER_ONLY_TOOL_PREFIXES = ("code_execution_", "tool_search_tool_", "advisor_", "mcp_toolset")


def convert_anthropic_tools_to_openai(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将 Anthropic tools 格式转换为 OpenAI tools 格式

    Anthropic:
        [{"name": "get_weather", "description": "...", "input_schema": {...}}]
    OpenAI:
        [{"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}]
    """
    openai_tools: List[Dict[str, Any]] = []
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            continue
        name = tool.get("name", "")
        description = tool.get("description", "")
        input_schema = tool.get("input_schema", {})
        kind = str(tool.get("type") or "")
        if kind.startswith(_SERVER_ONLY_TOOL_PREFIXES):
            note_loss(f"tools[{index}]", f"内置工具 {kind} 只能由 Anthropic 服务端执行，已移除")
            continue
        for prefix, (default_description, schema) in _CLIENT_TOOL_DEFINITIONS.items():
            if kind.startswith(prefix):
                description = description or default_description
                input_schema = input_schema or schema
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
        if isinstance(tool.get("strict"), bool):
            openai_tools[-1]["function"]["strict"] = tool["strict"]
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

    处理 text / image / audio / tool_result / tool_use / thinking 等块类型。
    注意：assistant 消息中的 tool_use 和 user 消息中的 tool_result
    的完整结构化转换在 convert_anthropic_to_openai_request 中处理，
    此函数仅做基础的内容块→OpenAI content 转换。
    """
    if isinstance(content, str):
        if "<graywill_audio" not in content:
            return content
        content = [{"type": "text", "text": content}]

    if not isinstance(content, list):
        return str(content) if content is not None else ""

    converted_parts: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue

        block_type = block.get("type")
        if block_type in ("text", "input_text"):
            text = block.get("text", "")
            converted_parts.extend(split_audio_text(str(text)))

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
            else:
                note_loss("image.source", f"图片来源 {source_type} 只在 Anthropic 有效，已用文字占位")
                converted_parts.append({"type": "text", "text": "[图片未发送：引用的文件只在 Anthropic 有效]"})

        elif block_type in ("audio", "input_audio", "audio_url"):
            audio_part = audio_to_openai(block)
            if audio_part:
                converted_parts.append(audio_part)

        elif block_type == "document":
            # 音频、纯文本和文件三种形态都走 document，按内容分流。
            audio_part = audio_to_openai(block)
            text = None if audio_part else document_text(block)
            found = None if audio_part or text is not None else extract_file(block)
            if audio_part:
                converted_parts.append(audio_part)
            elif text is not None:
                converted_parts.append({"type": "text", "text": text})
            elif found and "file_id" not in found:
                converted_parts.append(file_part(found))
            else:
                note_loss("document", "文档引用的文件只在 Anthropic 有效，已用文字占位")
                converted_parts.append({"type": "text", "text": "[文档未发送：引用的文件只在 Anthropic 有效]"})

        elif block_type == "search_result":
            converted_parts.append({"type": "text", "text": search_result_text(block)})

        elif block_type == "tool_result":
            tool_content = block.get("content")
            is_error = block.get("is_error", False)
            if isinstance(tool_content, str):
                prefix = "[tool_result_error] " if is_error else ""
                converted_parts.append({"type": "text", "text": prefix + tool_content})
            elif isinstance(tool_content, list):
                # 工具结果复用普通内容块转换，保留其中的图片及原有顺序。
                if is_error:
                    converted_parts.append({"type": "text", "text": "[tool_result_error] "})
                converted = convert_anthropic_content_to_openai(tool_content)
                if isinstance(converted, list):
                    converted_parts.extend(converted)
                else:
                    converted_parts.append({"type": "text", "text": converted})
            else:
                prefix = "[tool_result_error] " if is_error else ""
                converted_parts.append({"type": "text", "text": prefix + json.dumps(tool_content, ensure_ascii=False)})

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

        elif block_type != "redacted_thinking":
            # 服务端工具记录、压缩块等只有 Anthropic 能用。
            note_loss(f"content.{block_type}", f"内容块 {block_type} 无法转换到当前上游，已跳过")

    if not converted_parts:
        return " "
    if len(converted_parts) == 1 and converted_parts[0].get("type") == "text":
        return converted_parts[0].get("text", " ")
    return converted_parts


def convert_anthropic_to_openai_request(anthropic_req: Dict[str, Any]) -> Dict[str, Any]:
    """将 Anthropic Messages API 请求转换为 OpenAI chat.completions 请求。

    支持：text / image / audio / tool_use / tool_result / thinking / tools / tool_choice
    """
    model = anthropic_req.get("model")
    if not model:
        raise ValueError("Anthropic 请求缺少 'model' 字段")

    messages_in = anthropic_req.get("messages")
    if not isinstance(messages_in, list):
        raise ValueError("Anthropic 请求缺少或包含无效的 'messages' 字段")

    openai_messages: List[Dict[str, Any]] = []

    system_text = extract_system_text(anthropic_req.get("system"))
    # Claude Code 的专用计费标记不属于系统提示，不传给其他协议的上游。
    system_text = re.sub(
        r"\Ax-anthropic-billing-header:[^\r\n]*(?:\r?\n|$)", "", system_text)
    if system_text:
        openai_messages.append({"role": "system", "content": system_text})

    output = anthropic_req.get("output_config") or {}
    effort = output.get("effort")

    for msg in messages_in:
        if not isinstance(msg, dict):
            continue

        role = msg.get("role", "user")
        raw_content = msg.get("content", "")

        if role == "system":
            # 中段 system 条目可以带本轮的思考强度，内容可能是空的。
            effort = (msg.get("output_config") or {}).get("effort") or effort
            if not raw_content:
                continue

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
            else:
                msg = {"role": "assistant", "content": content}
            if thinking_texts:
                msg["reasoning_content"] = "\n".join(thinking_texts)
            # 本网关下发的 Gemini 签名随 thinking 块回传：按块顺序还原原生 parts
            gemini_parts = gemini_parts_from_anthropic(raw_content)
            metadata = {}
            if gemini_parts:
                metadata["gemini_parts"] = gemini_parts
            responses_items = responses_items_from_anthropic(raw_content)
            if responses_items is not None:
                metadata["responses_items"] = responses_items
            interactions_steps = interactions_steps_from_anthropic(raw_content)
            if interactions_steps is not None:
                metadata["interactions_steps"] = interactions_steps
            if metadata:
                msg["provider_metadata"] = metadata
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
                        if block.get("is_error"):
                            tool_content = convert_anthropic_content_to_openai([block])
                        elif isinstance(tool_content, str):
                            tool_content = convert_anthropic_content_to_openai(tool_content)
                        elif is_content_parts(tool_content):
                            tool_content = convert_anthropic_content_to_openai(tool_content)
                        elif isinstance(tool_content, list):
                            text_parts = [str(part.get("text", "")) for part in tool_content
                                          if isinstance(part, dict) and part.get("type") in ("text", "input_text")]
                            tool_content = "\n".join(text_parts) if text_parts else json.dumps(tool_content, ensure_ascii=False)
                        elif not isinstance(tool_content, str):
                            tool_content = json.dumps(tool_content, ensure_ascii=False)

                        openai_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_use_id,
                            "content": tool_content
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

    # 常见参数映射。top_k、metadata 不转：其他上游多数不接受，严格校验时整个请求报 400。
    passthrough_fields = [
        "temperature",
        "top_p",
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

    if isinstance(tool_choice, dict) and isinstance(tool_choice.get("disable_parallel_tool_use"), bool):
        openai_req["parallel_tool_calls"] = not tool_choice["disable_parallel_tool_use"]

    output_format = output.get("format")
    if isinstance(output_format, dict) and output_format.get("type") == "json_schema":
        openai_req["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "response", "schema": output_format.get("schema", {})}}

    # thinking 在内部统一成 reasoning_effort，各上游的转换器再换成自己的写法。
    thinking = anthropic_req.get("thinking")
    if isinstance(thinking, dict):
        if thinking.get("type") == "disabled":
            effort = "none"
        elif thinking.get("type") == "enabled" and effort is None:
            effort = budget_to_effort(thinking.get("budget_tokens"))
    if effort is not None:
        openai_req["reasoning_effort"] = effort

    return openai_req


# ============================================================================
# Anthropic 错误 / 响应构建
# ============================================================================

def build_anthropic_error_payload(error_obj: Any, status: Optional[int] = None) -> Dict[str, Any]:
    error_type = "api_error"
    message = "请求失败"
    error_details: Dict[str, Any] = {}
    request_id = None

    if isinstance(error_obj, dict):
        request_id = error_obj.get("request_id") or error_obj.get("requestId")
        if "error" in error_obj and isinstance(error_obj.get("error"), dict):
            error_details = translate_anthropic_error(dict(error_obj["error"]), status)
        else:
            error_details = translate_anthropic_error(dict(error_obj), status)
        error_type = error_details.get("type", error_type)
        message = error_details.get("message", message)
        request_id = (error_details.get("request_id")
                      or error_details.get("requestId") or request_id)
    elif isinstance(error_obj, str):
        message = error_obj
    elif error_obj is not None:
        message = str(error_obj)

    # 保留扩展诊断字段，客户端仍按 Anthropic 的 type/error/message 读取错误。
    payload = {
        "type": "error",
        "error": {
            **error_details,
            "type": error_type,
            "message": message
        }
    }
    if request_id:
        payload["request_id"] = request_id
    return payload


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
    note_cache_diagnostics(response_json)
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
    _parse_anthropic_sse_lines(buf[:last_nl + 1].decode("utf-8", errors="replace"), state)


def flush_anthropic_sse_state(state: dict) -> None:
    """流结束时冲刷旁路解析的残留状态。

    1. 处理 _sse_buf 中最后一个不完整行（正常 SSE 流以换行结尾，此时为空操作）；
    2. 把未收到 content_block_stop 的工具参数落入对应 tool_call（流中断场景）。
    """
    buf = state.pop("_sse_buf", b"")
    if buf:
        _parse_anthropic_sse_lines(buf.decode("utf-8", errors="replace"), state)
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
            note_cache_diagnostics(event.get("message"))
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
    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
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
    # Anthropic 的 output_tokens 含 thinking：内部 usage 若是 separate 口径（只记正文），
    # 这里要还原成正文 + 思考，否则 Anthropic 客户端读到的输出会偏小
    completion_tokens = total_output_tokens(usage)
    details = usage.get("prompt_tokens_details")
    cached_tokens = int(details.get("cached_tokens", 0) or 0) if isinstance(details, dict) else 0
    from utils.api_pricing import cache_write_usage
    writes, one_hour = cache_write_usage(usage)
    result = {
        "input_tokens": max(0, prompt_tokens - cached_tokens - writes),
        "cache_creation_input_tokens": writes,
        "cache_read_input_tokens": cached_tokens,
        "output_tokens": completion_tokens,
    }
    if one_hour:
        result['cache_creation'] = {'ephemeral_5m_input_tokens': max(0, writes - one_hour),
                                    'ephemeral_1h_input_tokens': one_hour}
    return result


def sanitize_anthropic_thinking_blocks(messages: Any) -> Any:
    """剔除历史消息中缺失签名的 thinking / redacted_thinking block（透传前清洗）。

    本网关为其他协议签发的 thinking 签名（gemini.v1. / responses.v1. /
    interactions.v1. 前缀）对 Anthropic 无效，客户端中途切回 Claude 模型时一并剔除。

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
                if btype == "thinking" and (not block.get("signature")
                                            or decode_signature(block.get("signature"))[0]
                                            or thinking_carrier(block.get("signature"))):
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


def _flat_segments(reasoning: Any, text: Any, tool_calls: Any) -> List[tuple]:
    """非 Gemini 来源没有原生顺序：按 思考 → 正文 → 调用 的自然顺序展开。

    签名片段的规则（流式/非流式一致）：签名收束当前思考块；没有打开的思考块
    时单独成一个空思考块（与 Claude display=omitted 的形态一致）。
    """
    segments: List[tuple] = []
    if isinstance(reasoning, str) and reasoning:
        segments.append(("thinking", reasoning))
    if isinstance(text, str) and text:
        segments.append(("text", text))
    if isinstance(tool_calls, list) and tool_calls:
        segments.append(("tool_calls", tool_calls))
    return segments


async def convert_openai_non_stream_response_to_anthropic(
    openai_response: Response,
    request_model: str,
    warnings: Optional[List[Dict[str, Any]]] = None,
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
            content=build_anthropic_error_payload(data.get("error", data), status_code)
        )

    choices = data.get("choices", [])
    first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    finish_reason = first_choice.get("finish_reason")
    message = first_choice.get("message", {}) if isinstance(first_choice, dict) else {}

    anthropic_usage = convert_openai_usage_to_anthropic(data.get("usage"))

    # ── 构建 Anthropic content blocks（多 block 支持，规则与流式一致）──
    try:
        segments = responses_segments(message)
    except ValueError as exc:
        return JSONResponse(status_code=502, content=build_anthropic_error_payload({"type": "api_error", "message": str(exc)}))
    if segments is None:
        segments = gemini_segments(message)
    if segments is None:
        segments = interactions_segments(message)
    if segments is None:
        segments = _flat_segments(extract_openai_message_reasoning(first_choice),
                                  extract_openai_message_text(first_choice) or message.get("refusal"),
                                  message.get("tool_calls"))
    content_blocks: List[Dict[str, Any]] = []
    open_thinking: Optional[Dict[str, Any]] = None
    for kind, value in segments:
        if kind == "thinking":
            if open_thinking is None:
                open_thinking = {"type": "thinking", "thinking": ""}
                content_blocks.append(open_thinking)
            open_thinking["thinking"] += value
            continue
        if kind == "signature":
            if open_thinking is None:
                content_blocks.append({"type": "thinking", "thinking": "", "signature": value})
            else:
                open_thinking["signature"] = value
            open_thinking = None
            continue
        open_thinking = None
        if kind == "text":
            if content_blocks and content_blocks[-1]["type"] == "text":
                content_blocks[-1]["text"] += value
            else:
                content_blocks.append({"type": "text", "text": value})
            continue
        for tc in value:
            if not isinstance(tc, dict):
                continue
            tc_func = tc.get("function", {})
            tc_name = tc_func.get("name", "")
            tc_args_str = tc_func.get("arguments", "{}")
            try:
                tc_args = json.loads(tc_args_str) if isinstance(tc_args_str, str) else tc_args_str
            except json.JSONDecodeError as exc:
                return JSONResponse(status_code=502, content=build_anthropic_error_payload({
                    "type": "api_error", "message": f"上游工具调用参数不是有效 JSON: {exc.msg}"}))
            if not isinstance(tc_args, dict):
                return JSONResponse(status_code=502, content=build_anthropic_error_payload({
                    "type": "api_error", "message": "上游工具调用参数必须是 JSON 对象。"}))
            content_blocks.append({
                "type": "tool_use",
                "id": tc.get("id") or f"toolu_{uuid.uuid4().hex}",
                "name": tc_name,
                "input": tc_args
            })

    sources = _source_lines(message.get("annotations"))
    if sources:
        content_blocks.append({"type": "text", "text": sources})
    if message.get("images"):
        note_loss("images", "Anthropic 协议的回复不能带图片，生成的图片未返回")
        content_blocks.append({"type": "text", "text": "[上游生成了图片，Anthropic 协议的回复无法携带]"})
    if not content_blocks:
        content_blocks = [{"type": "text", "text": ""}]

    has_tool_use = any(block["type"] == "tool_use" for block in content_blocks)
    stop_reason = _stop_reason("content_filter" if message.get("refusal") else finish_reason, has_tool_use,
                               has_tool_use or any(block["type"] == "text" and block["text"] for block in content_blocks))
    anthropic_response = {
        "id": data.get("id") or f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": request_model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": anthropic_usage
    }
    if stop_reason == "refusal":
        anthropic_response["stop_details"] = {"type": "refusal", "category": None,
                                              "explanation": message.get("refusal")}
    if warnings:
        anthropic_response["bridge_warnings"] = warnings

    return JSONResponse(status_code=200, content=anthropic_response, headers=warning_headers(warnings or []))


# SSE 事件分隔符：兼容 LF 空行与 CRLF 空行
_SSE_EVENT_SPLIT = re.compile(r"\r?\n\r?\n")


async def iter_openai_sse_payloads(upstream_response: StreamingResponse):
    """从 OpenAI SSE 流中逐个提取负载字符串；SSE 注释（心跳）产出 None。

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
                elif line.startswith(":"):
                    yield None

    buffer += decoder.decode(b"", final=True)
    if buffer.strip():
        for line in buffer.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                yield line[5:].strip()


class _ToolBlocks:
    """保证同一时间只有一个 tool_use 块打开：官方事件流就是这样，客户端也按此处理。

    下一个调用到来时，当前块的参数已经是完整 JSON 就先关闭；否则说明上游在交错发送，
    把后到的增量缓存起来，等当前块收齐再放出。个别中转所有调用都用 index 0，
    所以同一个 index 出现新的 id 也算新调用。
    """

    def __init__(self, next_index, emit_start, emit_delta, emit_stop):
        self.next_index = next_index
        self.emit_start, self.emit_delta, self.emit_stop = emit_start, emit_delta, emit_stop
        self.current: Optional[Dict[str, Any]] = None
        self.waiting: List[Dict[str, Any]] = []
        self.closed_keys = set()
        self.seen = False

    @staticmethod
    def _new(key: Any) -> Dict[str, Any]:
        # 把 arguments 增量中的 \uXXXX 转义提前解码为明文（跨 chunk 安全）
        return {"key": key, "id": "", "name": "", "raw": "", "started": False, "buffered": [],
                "unescaper": StreamingUnicodeUnescaper()}

    def _write(self, block: Dict[str, Any], tc_id: Any, name: Any, args: Any) -> List[str]:
        block["id"] = block["id"] or tc_id or ""
        block["name"] = block["name"] or name or ""
        events: List[str] = []
        if not block["started"] and (block["id"] or block["name"] or args):
            block["started"] = True
            self.seen = True
            block["index"] = self.next_index()
            events.append(self.emit_start(block["index"], "tool_use", {
                "id": block["id"] or f"toolu_{uuid.uuid4().hex}", "name": block["name"]}))
        if args:
            block["raw"] += args
            decoded = block["unescaper"].feed(args)
            if decoded:
                events.append(self.emit_delta(block["index"], "input_json_delta", decoded))
        return events

    @staticmethod
    def _complete(block: Dict[str, Any]) -> bool:
        # 还没收到参数不算完整：常见上游先发齐所有调用的 id，再发参数。
        if not block["raw"]:
            return False
        try:
            json.loads(block["raw"])
            return True
        except ValueError:
            return False

    def _close_current(self) -> List[str]:
        block, self.current = self.current, None
        if block is None or not block["started"]:
            return []
        self.closed_keys.add(block["key"])
        # 冲刷参数解码器残留（正常完整 JSON 无残留；截断流原样补发）
        tail = block["unescaper"].flush()
        events = [self.emit_delta(block["index"], "input_json_delta", tail)] if tail else []
        events.append(self.emit_stop(block["index"]))
        return events

    def _activate_next(self) -> List[str]:
        events: List[str] = []
        while self.current is None and self.waiting:
            block = self.waiting.pop(0)
            self.current = block
            for fragment in block.pop("buffered"):
                events.extend(self._write(block, *fragment))
            if self.waiting and self._complete(block):
                events.extend(self._close_current())
        return events

    def feed(self, tool_call: Dict[str, Any]) -> List[str]:
        key = tool_call.get("index", 0)
        tc_id = tool_call.get("id")
        function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
        fragment = (tc_id, function.get("name", ""), function.get("arguments", ""))
        events: List[str] = []
        current = self.current
        if current is not None and current["key"] == key and not (tc_id and current["id"] and tc_id != current["id"]):
            return self._write(current, *fragment)
        if not tc_id and key in self.closed_keys:
            # 块已经关闭，再开一个没有 id 的块客户端也没法回传结果。
            logger.warning(f"[ANTHROPIC_CONVERT] 工具调用 index={key} 的块关闭后又收到增量，已丢弃")
            return events
        waiting = next((block for block in self.waiting if block["key"] == key
                        and not (tc_id and block["buffered"][0][0] and tc_id != block["buffered"][0][0])), None)
        if waiting is not None:
            waiting["buffered"].append(fragment)
        else:
            self.waiting.append({**self._new(key), "buffered": [fragment]})
        if current is None or self._complete(current):
            events.extend(self._close_current())
            events.extend(self._activate_next())
        return events

    def close_all(self) -> List[str]:
        events = self._close_current()
        while self.waiting:
            events.extend(self._activate_next())
            events.extend(self._close_current())
        return events


def build_anthropic_streaming_response(
    openai_streaming_response: StreamingResponse,
    request_model: str,
    warnings: Optional[List[Dict[str, Any]]] = None,
    usage_pending: bool = False,
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
        # 与官方一致回报客户端请求的模型：Claude Code 按 message.model 判断思考签名是否由
        # 当前模型签发，回报上游或显示名时，CLI 重启（回退、恢复）后会剥掉全部签名思考块
        model_name = request_model

        # ── 消息级状态 ──
        sent_message_start = False
        finish_reason: Optional[str] = None
        refusal_parts: List[str] = []
        has_text = False
        annotations: List[Dict[str, Any]] = []
        saw_images = False
        # 累积上游 usage（OpenAI 流式 usage 为最终累计值而非增量，合并覆盖即可）
        merged_usage: Dict[str, Any] = {}

        # ── content block 状态机 ──
        next_block_idx = 0
        active_thinking_idx: Optional[int] = None
        active_text_idx: Optional[int] = None
        prev_delta_type: Optional[str] = None  # "thinking" | "text" | "tool_calls"

        def _allocate_block_index() -> int:
            nonlocal next_block_idx
            next_block_idx += 1
            return next_block_idx - 1

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
            events.extend(tool_blocks.close_all())
            return events

        tool_blocks = _ToolBlocks(_allocate_block_index, _emit_block_start, _emit_block_delta, _emit_block_stop)

        # ── 主循环：逐 chunk 处理 ──
        async for payload in iter_openai_sse_payloads(openai_streaming_response):
            if payload is None:
                # 模型思考或续传时可能长时间没有事件，客户端读超时前要持续有字节。
                # 注释可出现在 message_start 之前，ping 则不行。
                yield ": keep-alive\n\n"
                continue
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
                        "usage": convert_openai_usage_to_anthropic(merged_usage),
                        **({"bridge_warnings": warnings} if warnings else {}),
                        **({"bridge_usage_pending": True} if usage_pending else {})
                    }
                })

            choices = chunk.get("choices", [])
            if not choices or not isinstance(choices[0], dict):
                continue

            choice = choices[0]
            delta = choice.get("delta", {}) if isinstance(choice.get("delta"), dict) else {}

            # Responses 推理项完成时才包含完整密文，随对应思考块的签名返回。
            provider_event = delta.get("provider_event")
            if isinstance(provider_event, dict) and provider_event.get("type") == "response.output_item.added":
                item = provider_event.get("item") or {}
                if item.get("type") not in ("reasoning", "message"):
                    # 托管工具由上游执行完了，结果已体现在后面的正文里；Anthropic 没有对应的块。
                    note_loss(f"output.{item.get('type')}", f"Responses 输出项 {item.get('type')} 没有对应的 Anthropic 内容块，已跳过")
            if isinstance(provider_event, dict) and provider_event.get("type") == "response.output_item.done":
                item = provider_event.get("item") or {}
                if item.get("type") == "reasoning":
                    if active_thinking_idx is None:
                        for ev in _close_current_blocks():
                            yield ev
                        active_thinking_idx = next_block_idx
                        next_block_idx += 1
                        yield _emit_block_start(active_thinking_idx, "thinking")
                        text = responses_reasoning_text(item)
                        if text:
                            yield _emit_block_delta(active_thinking_idx, "thinking_delta", text)
                    yield _emit_block_delta(active_thinking_idx, "signature_delta", encode_thinking_item("responses", item))
                    yield _emit_block_stop(active_thinking_idx)
                    active_thinking_idx = None
                    prev_delta_type = None
                    continue

            # Gemini 链路按原生 parts 顺序展开（签名随 thinking 块下发），Interactions
            # 在每段思考结束时单独下发签名，其余来源按 思考 → 正文 → 调用展开；
            # 同一 chunk 混有多种内容时逐段发送，不再只取一种
            if isinstance(delta.get("refusal"), str):
                refusal_parts.append(delta["refusal"])
            if isinstance(delta.get("annotations"), list):
                annotations.extend(delta["annotations"])
            saw_images = saw_images or bool(delta.get("images"))
            segments = gemini_segments(delta)
            if segments is None:
                segments = interactions_signature_segments(delta)
            if segments is None:
                segments = _flat_segments(delta.get("reasoning_content") or delta.get("reasoning"),
                                          delta.get("content") or delta.get("refusal"),
                                          delta.get("tool_calls"))

            for current_type, value in segments:
                if current_type == "signature":
                    if active_thinking_idx is None:
                        for ev in _close_current_blocks():
                            yield ev
                        active_thinking_idx = next_block_idx
                        next_block_idx += 1
                        yield _emit_block_start(active_thinking_idx, "thinking")
                    yield _emit_block_delta(active_thinking_idx, "signature_delta", value)
                    yield _emit_block_stop(active_thinking_idx)
                    active_thinking_idx = None
                    prev_delta_type = None
                    continue

                # ── 类型切换 → 关闭旧 block ──
                if current_type != prev_delta_type and prev_delta_type is not None:
                    for ev in _close_current_blocks():
                        yield ev

                # ── 发送当前片段 ──
                if current_type == "thinking":
                    if active_thinking_idx is None:
                        active_thinking_idx = next_block_idx
                        next_block_idx += 1
                        yield _emit_block_start(active_thinking_idx, "thinking")
                    yield _emit_block_delta(active_thinking_idx, "thinking_delta", value)

                elif current_type == "text":
                    if active_text_idx is None:
                        active_text_idx = next_block_idx
                        next_block_idx += 1
                        yield _emit_block_start(active_text_idx, "text")
                    yield _emit_block_delta(active_text_idx, "text_delta", value)
                    has_text = has_text or bool(value)

                else:
                    for tc in value:
                        if isinstance(tc, dict):
                            for ev in tool_blocks.feed(tc):
                                yield ev

                prev_delta_type = current_type

            finish_reason = choice.get("finish_reason") or finish_reason

        # ── 流尾补充：来源和图片说明接在最后一段正文后面 ──
        trailing = _source_lines(annotations)
        if saw_images:
            note_loss("images", "Anthropic 协议的回复不能带图片，生成的图片未返回")
            trailing += "\n\n[上游生成了图片，Anthropic 协议的回复无法携带]"
        if trailing:
            if active_text_idx is None:
                for ev in _close_current_blocks():
                    yield ev
                active_text_idx = _allocate_block_index()
                yield _emit_block_start(active_text_idx, "text")
            yield _emit_block_delta(active_text_idx, "text_delta", trailing)
            has_text = True

        # ── 关闭所有剩余 block ──
        for ev in _close_current_blocks():
            yield ev

        stop_reason = _stop_reason("content_filter" if refusal_parts else finish_reason, tool_blocks.seen,
                                   has_text or tool_blocks.seen)

        # ── 消息收尾事件 ──
        if not sent_message_start:
            yield format_anthropic_sse_event("message_start", {
                "type": "message_start",
                "message": {
                    "id": message_id, "type": "message", "role": "assistant",
                    "model": model_name, "content": [],
                    "stop_reason": None, "stop_sequence": None,
                    "usage": convert_openai_usage_to_anthropic(merged_usage),
                    **({"bridge_warnings": warnings} if warnings else {}),
                    **({"bridge_usage_pending": True} if usage_pending else {})
                }
            })

        # 统一按 Anthropic 官方格式输出最终用量（含缓存字段），
        # 多数客户端从 message_delta.usage 读取计费数据
        stop_details = ({"stop_details": {"type": "refusal", "category": None, "explanation": "".join(refusal_parts)}}
                        if stop_reason == "refusal" else {})
        yield format_anthropic_sse_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None, **stop_details},
            "usage": convert_openai_usage_to_anthropic(merged_usage),
            **({"bridge_usage_pending": False} if usage_pending else {})
        })

        yield format_anthropic_sse_event("message_stop", {"type": "message_stop"})

    return StreamingResponse(
        anthropic_stream_generator(),
        media_type="text/event-stream",
        headers={
            **warning_headers(warnings or []),
            **({"X-Bridge-Usage-Event": "message_delta"} if usage_pending else {}),
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked",
        },
    )
