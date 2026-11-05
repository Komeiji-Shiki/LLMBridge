"""
Gemini Interactions API ↔ OpenAI / Gemini generateContent 协议转换模块

谷歌新推出的 Interactions API（POST /v1beta/interactions）与旧 generateContent
差异巨大：请求用 `input`（字符串/内容数组/步骤数组）+ `store` + `previous_interaction_id`，
响应是 Interaction 对象（id/steps/status/usage），流式是 event: + data: 双行 SSE
（interaction.created → step.start/delta/stop → interaction.completed → done）。

本模块提供：
- OAI messages ↔ interactions steps（无状态模式 store=false，中转桥不维护会话状态）
- interactions Interaction/事件流 → OpenAI chat.completion / chunk 流
- Gemini generateContent 请求/响应 ↔ interactions（gemini_v1beta_api 链路用）
- 思考签名缓存 + 前缀匹配注入：
  Gemini 3 默认思考，无状态模式下 thought 步骤必须带签名原样回传才能保持推理连续性。
  OAI 客户端历史里没有 signature 字段，但保留了 reasoning_content（= thought_summary 文本）。
  因此响应侧捕获 (summary 片段 → signature)，请求侧对客户端 reasoning_content 做前缀匹配，
  命中后把对应 signature 注入回传的 thought steps。
"""
import json
import copy
import logging
import mimetypes
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from converters.audio import extract_audio, separate_tool_audio
from converters.gemini_generate import TOOL_ERROR_PREFIX
from converters.media import FILE_PART_TYPES, file_to_interactions, gemini_code_text
from core.conversion_report import note_loss
from core.request_context import current_request
from services.model_capabilities import (
    budget_to_effort,
    limits_for,
    supported_thinking_level,
)
from utils.usage_tokens import MODE_MERGE, compose_chat_usage

logger = logging.getLogger(__name__)

__all__ = [
    "cache_thought_signatures",
    "match_and_inject_thought_signatures",
    "convert_oai_messages_to_interactions",
    "build_interactions_request_body",
    "convert_interactions_to_openai_response",
    "InteractionsStreamConverter",
    "convert_gemini_gc_to_interactions",
    "convert_interactions_to_gemini_gc",
    "InteractionsToGeminiGCConverter",
    "interaction_failure",
]

# 按对话缓存，存档随每个请求读写，必须有上限。
_MAX_CACHED_TURNS = 64
# 服务端工具的结果模型已经写进正文，步骤本身不需要给 Chat 客户端看。
_SERVER_TOOL_STEPS = frozenset({
    "google_search_call", "google_search_result", "url_context_call", "url_context_result",
    "google_maps_call", "google_maps_result", "file_search_call", "file_search_result",
    "processing_call", "processing_result", "mcp_server_tool_call", "mcp_server_tool_result",
})
_CONTINUATION_STOPPED = "续传跳数已达上限或上游没有给出新的 token，回复被截断"
# 官方文档：这几个错误码表示内容被拦截，重试也没用。
_BLOCKED_CODES = frozenset({"safety", "jailbreak", "prohibited_content", "blocklist"})


# ============================================================================
# 思考签名缓存（响应侧捕获 → 请求侧前缀匹配注入）
# ============================================================================

def signature_cache():
    """Request-local signatures, loaded and persisted by the scoped transport."""
    context = current_request.get()
    return context.artifacts.setdefault('gemini_thoughts', {}) if context and context.authenticated else {}


def _turn_cache():
    context = current_request.get()
    return context.artifacts.setdefault('interaction_turns', {}) if context and context.authenticated else {}


def _trim_cache(cache: dict) -> None:
    while len(cache) > _MAX_CACHED_TURNS:
        cache.pop(next(iter(cache)))


def cache_thought_signatures(fragments: List[Tuple[str, str]]) -> None:
    valid = [(text, signature) for text, signature in fragments if text and signature]
    if valid:
        cache = signature_cache()
        cache["".join(text for text, _ in valid)] = valid
        _trim_cache(cache)


def match_and_inject_thought_signatures(reasoning_content: str) -> List[dict]:
    """Only exact text matches may restore a signed thought; never sign edited text."""
    fragments = signature_cache().get(reasoning_content, [])
    return [{"type": "thought", "signature": signature,
             "summary": [{"type": "text", "text": text}]} for text, signature in fragments]


def _code_step_text(step: dict) -> Optional[str]:
    """代码执行步骤显示给 Chat 客户端的 Markdown，其他步骤返回 None。"""
    kind = step.get("type")
    if kind == "code_execution_call":
        arguments = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
        return gemini_code_text({"executableCode": {
            "language": arguments.get("language"), "code": arguments.get("code", "")}})
    if kind == "code_execution_result":
        return gemini_code_text({"codeExecutionResult": {"output": step.get("result", "")}})
    return None


def _visible_text(steps: List[dict]) -> str:
    """整轮步骤里 Chat 客户端能看到的正文，回传时据此判断内容有没有被改过。"""
    parts: List[str] = []
    for step in steps:
        if step.get("type") == "model_output":
            parts.extend(str(block.get("text", "")) for block in step.get("content") or []
                         if isinstance(block, dict) and block.get("type") == "text")
        else:
            parts.append(_code_step_text(step) or "")
    return "".join(parts)


def remember_turn_steps(steps: List[dict]) -> None:
    """按函数调用 ID 保存带签名的整轮步骤。

    Chat 客户端一定回传调用 ID，provider_metadata 却经常被丢掉；只带签名、没有
    摘要的步骤也无法靠 reasoning_content 对上。媒体数据不进缓存。
    """
    calls = [step.get("id") for step in steps if step.get("type") == "function_call" and step.get("id")]
    if not calls or not any(step.get("signature") for step in steps):
        return
    kept: List[dict] = []
    for step in steps:
        if step.get("type") == "model_output":
            blocks = [block for block in step.get("content") or []
                      if isinstance(block, dict) and block.get("type") == "text"]
            if blocks:
                kept.append({**step, "content": blocks})
        else:
            kept.append(step)
    cache = _turn_cache()
    cache[calls[0]] = copy.deepcopy(kept)
    _trim_cache(cache)


def _restored_turn(message: dict) -> Optional[List[dict]]:
    """客户端没带元数据时按调用 ID 找回整轮；调用或正文对不上就不恢复。"""
    tool_calls = message.get("tool_calls")
    calls = [call.get("id") for call in tool_calls if isinstance(call, dict)] if isinstance(tool_calls, list) else []
    stored = _turn_cache().get(calls[0]) if calls else None
    if not stored:
        return None
    stored_calls = [step.get("id") for step in stored if step.get("type") == "function_call"]
    if stored_calls != calls or _visible_text(stored).strip() != _extract_message_text(message.get("content")).strip():
        return None
    return copy.deepcopy(stored)


def _thought_fragments(steps: List[dict]) -> List[Tuple[str, str]]:
    return [(_extract_text_from_content_block(step.get("summary")), step.get("signature"))
            for step in steps if step.get("type") == "thought"]


# ============================================================================
# OAI → interactions 请求转换
# ============================================================================

def _extract_text_from_content_block(block: Any) -> str:
    """从 interactions 内容块（TextContent/ImageContent 等）提取文本。

    `summary` 在最新 Interactions API 中是内容块数组，旧版本/部分代理
    仍可能返回单个对象，因此这里统一支持两种形态。
    """
    if isinstance(block, list):
        return "".join(_extract_text_from_content_block(item) for item in block)
    if isinstance(block, dict):
        if block.get("type") == "text":
            return str(block.get("text", ""))
    return ""


def _oai_part_to_interactions_block(item: Any) -> Optional[dict]:
    """OpenAI content part → interactions 内容块；无法转换返回 None"""
    if isinstance(item, str):
        return {"type": "text", "text": item} if item else None
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")

    if item_type in ("text", "input_text", "output_text"):
        text = item.get("text", "")
        return {"type": "text", "text": text} if text else None

    if item_type in ("image_url", "input_image"):
        image = item.get("image_url") or item.get("url") or ""
        url = image.get("url", "") if isinstance(image, dict) else image
        if url.startswith("data:"):
            try:
                header, base64_data = url.split(",", 1)
                mime_type = header.split(";")[0].split(":")[1]
                return {"type": "image", "data": base64_data, "mime_type": mime_type}
            except Exception as e:
                logger.warning(f"[GEMINI_INTERACTIONS] 解析 base64 图片失败: {e}")
                return None
        if url.startswith(("http://", "https://")):
            guessed_mime, _ = mimetypes.guess_type(url.split("?", 1)[0])
            mime_type = guessed_mime if (guessed_mime or "").startswith("image/") else "image/jpeg"
            return {"type": "image", "uri": url, "mime_type": mime_type}
        return None

    if item_type in ("audio", "input_audio", "audio_url"):
        audio = extract_audio(item)
        return {"type": "audio", **audio} if audio else None

    if item_type in FILE_PART_TYPES:
        audio = extract_audio(item)
        if audio:
            return {"type": "audio", **audio}
        block = file_to_interactions(item)
        if block is None:
            note_loss(f"content.{item_type}", "文件引用只在原服务商有效，Interactions 无法读取，已跳过")
            return None
        return {**block, "type": _mime_to_content_type(block.get("mime_type", ""))}

    if item_type == "video_url":
        url_data = item.get("video_url") or {}
        url = url_data.get("url", "")
        kind = "video"
        if url.startswith(("http://", "https://")):
            guessed_mime, _ = mimetypes.guess_type(url.split("?", 1)[0])
            return {"type": kind, "uri": url, "mime_type": guessed_mime or f"{kind}/mp4"}
        return None

    # 未知类型块：能提取文本则保留，否则跳过
    fallback_text = item.get("text")
    if isinstance(fallback_text, str) and fallback_text:
        return {"type": "text", "text": fallback_text}
    note_loss(f"content.{item_type}", f"Interactions 无法读取内容块 {item_type}，已跳过")
    return None



def _oai_tool_to_interactions_tool(tool: Any) -> Optional[dict]:
    """把 Chat Completions 或 Responses 的工具定义转成 Interactions 格式。

    Chat Completions 使用 `{"type":"function", "function": {...}}`，而
    Interactions 使用 `{"type":"function", "name":..., "parameters":...}`。
    已经是扁平格式的定义也允许直接通过，便于配置中的额外工具使用。
    """
    if not isinstance(tool, dict):
        return None

    if tool.get("type") == "function":
        function = tool.get("function")
        if isinstance(function, dict):
            name = function.get("name", "")
            if not name:
                return None
            converted = {
                "type": "function",
                "name": name,
                "description": function.get("description", ""),
                "parameters": function.get("parameters") or {},
            }
            return converted
        if tool.get("name"):
            return {
                "type": "function",
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters") or {},
            }

    # Interactions 内置工具或已转换的函数工具可以原样保留。
    if tool.get("type") in {
        "code_execution", "url_context", "computer_use", "mcp_server",
        "google_search", "file_search", "google_maps",
    }:
        return dict(tool)
    note_loss(f"tools.{tool.get('type')}", f"Interactions 不支持工具类型 {tool.get('type')}，未发送")
    return None


def _extract_message_text(content: Any) -> str:
    """从 OAI 消息 content（str / list）提取纯文本"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in ("text", "input_text"):
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return ""


def convert_oai_messages_to_interactions(
    messages: List[dict],
    model: Optional[str] = None,
) -> Tuple[List[dict], str]:
    """OpenAI 消息列表 → (interactions steps, system_instruction 文本)。

    无状态模式约定（store=false）：
    - system/developer → 拼入 system_instruction（单独返回，调用方写入顶层字段）
    - user → user_input step（content 块数组，支持文本/图片/音频）
    - assistant → model_output step（文本）+ function_call steps（tool_calls）；
      reasoning_content 前缀匹配签名缓存后注入 thought steps（位于 model_output 之前）
    - tool → function_result step（call_id 关联 assistant tool_calls）

    Returns:
        (steps, system_instruction)
    """
    steps: List[dict] = []
    system_parts: List[str] = []
    tool_id_to_name: Dict[str, str] = {}

    # 新模型的函数结果能直接带文件和音视频；旧模型移到紧随其后的 user 轮。
    media_in_result = bool(model) and limits_for(model).function_response_media
    source = messages if media_in_result else separate_tool_audio(messages, include_files=True)
    for msg in source:
        role = msg.get("role")
        content = msg.get("content", "")

        if role in ("system", "developer"):
            text = _extract_message_text(content)
            if text:
                system_parts.append(text)
            continue

        if role == "user":
            blocks = []
            if isinstance(content, list):
                for item in content:
                    block = _oai_part_to_interactions_block(item)
                    if block:
                        blocks.append(block)
            elif isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            if not blocks:
                blocks.append({"type": "text", "text": " "})
            steps.append({"type": "user_input", "content": blocks})
            continue

        if role == "assistant":
            metadata = msg.get('provider_metadata')
            native_steps = metadata.get('interactions_steps') if isinstance(metadata, dict) else None
            if not isinstance(native_steps, list):
                native_steps = _restored_turn(msg)
            if isinstance(native_steps, list):
                steps.extend(copy.deepcopy(native_steps))
                for native in native_steps:
                    if native.get("type") == "function_call" and native.get("id"):
                        tool_id_to_name[native["id"]] = native.get("name", "")
                continue
            # 思考签名注入：thought steps 必须位于 model_output 之前（与上游输出顺序一致）
            reasoning = msg.get("reasoning_content")
            if reasoning:
                injected = match_and_inject_thought_signatures(str(reasoning))
                steps.extend(injected)

            text = _extract_message_text(content)
            if text:
                steps.append({
                    "type": "model_output",
                    "content": [{"type": "text", "text": text}],
                })

            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    func = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    fc_name = func.get("name", "")
                    args_raw = func.get("arguments", "{}")
                    try:
                        fc_args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                    except json.JSONDecodeError:
                        note_loss("tool_calls.arguments", f"历史工具调用 {fc_name} 的参数不是合法 JSON，已按空参数发送")
                        fc_args = {}
                    fc_step: dict = {
                        "type": "function_call",
                        "name": fc_name,
                        "arguments": fc_args if isinstance(fc_args, dict) else {},
                    }
                    tc_id = tc.get("id")
                    if tc_id:
                        fc_step["id"] = tc_id
                        if fc_name:
                            tool_id_to_name[tc_id] = fc_name
                    steps.append(fc_step)
            continue

        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            func_name = tool_id_to_name.get(tool_call_id)
            is_error = isinstance(content, str) and content.startswith(TOOL_ERROR_PREFIX)
            if is_error:
                content = content[len(TOOL_ERROR_PREFIX):]

            result_blocks: List[dict] = []
            if isinstance(content, str) and content:
                result_blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for item in content:
                    block = _oai_part_to_interactions_block(item)
                    if block:
                        result_blocks.append(block)
            elif isinstance(content, dict):
                # dict 内容（如 {"content": "..."} 或 {"result": ...}）尝试提取
                value = content.get("content", content.get("result"))
                if isinstance(value, str) and value:
                    result_blocks.append({"type": "text", "text": value})
                elif value is not None:
                    try:
                        result_blocks.append({
                            "type": "text",
                            "text": json.dumps(value, ensure_ascii=False),
                        })
                    except (TypeError, ValueError):
                        result_blocks.append({"type": "text", "text": str(value)})
                elif content:
                    try:
                        result_blocks.append({
                            "type": "text",
                            "text": json.dumps(content, ensure_ascii=False),
                        })
                    except (TypeError, ValueError):
                        result_blocks.append({"type": "text", "text": str(content)})

            result_step: dict = {
                "type": "function_result",
                "call_id": tool_call_id,
                "result": result_blocks or [{"type": "text", "text": ""}],
            }
            # name 在协议里是可选的，查不到就不写，不能猜一个发给上游。
            if func_name:
                result_step["name"] = func_name
            if is_error:
                result_step["is_error"] = True
            steps.append(result_step)
            continue

        logger.warning(f"[GEMINI_INTERACTIONS] 跳过未知消息角色: {role}")

    return steps, "\n".join(system_parts)


def map_thinking_config_to_level(thinking_config: Optional[dict], model: Optional[str] = None) -> Optional[str]:
    """将 thinkingLevel 或 thinkingBudget 映射为模型支持的 Interactions thinking_level。"""
    if not isinstance(thinking_config, dict):
        return None
    level = thinking_config.get("thinkingLevel")
    budget = thinking_config.get("thinkingBudget")
    if not level and budget is not None and budget != -1:
        # Interactions 没有预算字段；-1 表示动态，不设等级就是默认行为。
        level = {"none": "minimal"}.get(budget_to_effort(budget), budget_to_effort(budget))
        note_loss("thinkingBudget", f"Interactions 不接受思考预算，已换成 {level} 等级")
    if not level:
        return None
    return supported_thinking_level(model, str(level).lower()) if model else str(level).lower()


def map_thinking_config_to_summaries(thinking_config: Optional[dict]) -> Optional[str]:
    """将 includeThoughts 映射为 Interactions 的 thinking_summaries。"""
    if not isinstance(thinking_config, dict) or "includeThoughts" not in thinking_config:
        return None
    return "auto" if thinking_config.get("includeThoughts") else "none"

def map_tool_choice_to_interactions(tool_choice: Optional[Any]) -> Optional[Any]:
    """把 OpenAI tool_choice 映射为 Interactions generation_config.tool_choice。"""
    if tool_choice in (None, "auto"):
        return None
    if tool_choice == "none":
        return "none"
    if tool_choice in ("required", "any"):
        return {"allowed_tools": {"mode": "any"}}
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "none":
            return "none"
        if tool_choice.get("type") == "function":
            function = tool_choice.get("function") or {}
            name = function.get("name")
            if name:
                return {"allowed_tools": {"mode": "any", "tools": [name]}}
        allowed = tool_choice.get("allowed_tools")
        if isinstance(allowed, dict):
            names = [(item.get("function") or {}).get("name") or item.get("name") if isinstance(item, dict) else item
                     for item in allowed.get("tools") or []]
            mapped = {"mode": "any" if allowed.get("mode") in ("required", "any") else "auto"}
            if names:
                mapped["tools"] = [name for name in names if name]
            return {"allowed_tools": mapped}
    return None


def build_interactions_request_body(
    model: str,
    messages: List[dict],
    stream: bool = False,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    max_tokens: Optional[int] = None,
    thinking_config: Optional[dict] = None,
    tools: Optional[List[dict]] = None,
    tool_choice: Optional[Any] = None,
    response_format: Optional[Any] = None,
    stop_sequences: Optional[List[str]] = None,
    extra_body: Optional[dict] = None,
) -> dict:
    """构建 interactions 请求体（无状态模式 store=false）。

    tools 会从 Chat Completions 的嵌套 function 定义转换为 Interactions 扁平定义；
    tool_choice 映射为 generation_config.tool_choice.allowed_tools；
    response_format 使用 Interactions 的 text/mime_type/schema 对象。
    """
    steps, system_instruction = convert_oai_messages_to_interactions(messages, model)

    body: Dict[str, Any] = {
        "model": model,
        "input": steps,
        "store": False,
        "stream": bool(stream),
    }
    if system_instruction:
        body["system_instruction"] = system_instruction

    gen_config: Dict[str, Any] = {}
    # 现行 GenerationConfig 没有采样字段，发送可能触发 schema 错误。
    for field, value in (("temperature", temperature), ("top_p", top_p)):
        if value is not None:
            note_loss(field, "Interactions 不接受此参数，未发送到上游")
    if max_tokens is not None:
        gen_config["max_output_tokens"] = max_tokens
    if isinstance(stop_sequences, list) and stop_sequences:
        gen_config["stop_sequences"] = stop_sequences
    thinking_level = map_thinking_config_to_level(thinking_config, model)
    if thinking_level:
        gen_config["thinking_level"] = thinking_level
    thinking_summaries = map_thinking_config_to_summaries(thinking_config)
    if thinking_summaries:
        gen_config["thinking_summaries"] = thinking_summaries
    if gen_config:
        body["generation_config"] = gen_config

    # tool_choice="none" 也保留 tools：删掉会改变缓存前缀。
    if tools:
        interaction_tools = [
            converted for tool in tools
            if (converted := _oai_tool_to_interactions_tool(tool)) is not None
        ]
        if interaction_tools:
            body["tools"] = interaction_tools
            logger.info(f"[GEMINI_INTERACTIONS] 透传 {len(interaction_tools)} 个工具")

    mapped_tool_choice = map_tool_choice_to_interactions(tool_choice)
    if mapped_tool_choice and body.get("tools"):
        body.setdefault("generation_config", {})["tool_choice"] = mapped_tool_choice

    if isinstance(response_format, (dict, list)):
        if isinstance(response_format, list):
            body["response_format"] = response_format
        else:
            rf_type = response_format.get("type")
            if rf_type == "json_object":
                body["response_format"] = {
                    "type": "text",
                    "mime_type": "application/json",
                }
            elif rf_type == "json_schema":
                schema_info = response_format.get("json_schema") or {}
                body["response_format"] = {
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": schema_info.get("schema") or response_format.get("schema") or {},
                }
            elif rf_type:
                body["response_format"] = dict(response_format)

    if extra_body:
        body.update(extra_body)
        logger.info(f"[GEMINI_INTERACTIONS] 已添加额外请求体字段: {list(extra_body.keys())}")

    return body


# ============================================================================
# interactions → OAI 响应转换（非流式）
# ============================================================================

def _oai_chunk_base(model: str, request_id: str) -> dict:
    """OpenAI SSE chunk 公共字段"""
    return {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }


def _interactions_finish_reason(status: Optional[str]) -> str:
    """Interaction.status → OpenAI finish_reason；失败和取消由 interaction_failure 处理。"""
    if status == "requires_action":
        return "tool_calls"
    if status in ("incomplete", "requires_continuation"):
        return "length"
    return "stop"


def _usage_int(usage: dict, *keys: str) -> int:
    """从新旧 Interaction usage 字段中读取整数值。"""
    for key in keys:
        value = usage.get(key)
        if value is not None:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _usage_fields(usage: dict) -> dict:
    """Interaction.usage → 各项 token。

    total_input_tokens 在长输出多跳时会累加，提示量按第一跳算才反映上下文大小；
    联网、代码执行的工具提示按输入计费，但不含在输入里。
    """
    total_input = _usage_int(usage, "total_input_tokens", "prompt_tokens")
    initial = _usage_int(usage, "initial_input_tokens") or total_input
    tool_use = _usage_int(usage, "total_tool_use_tokens")
    prompt = initial + tool_use
    output = _usage_int(usage, "total_output_tokens", "completion_tokens")
    thought = _usage_int(usage, "total_thought_tokens", "reasoning_tokens")
    return {
        "initial": initial, "tool_use": tool_use, "prompt": prompt, "output": output, "thought": thought,
        "cached": min(_usage_int(usage, "initial_cached_tokens", "total_cached_tokens", "cached_tokens"), prompt),
        "total": prompt + output + thought if initial != total_input else _usage_int(usage, "total_tokens"),
    }


def _chat_usage(usage: dict, completion_tokens_mode: str) -> dict:
    # interactions 的 total_output_tokens 不含思考，两者相加才是真实总输出；
    # 下发给下游的 completion_tokens 按模型配置决定给总量还是仅正文
    fields = _usage_fields(usage)
    return compose_chat_usage(
        prompt_tokens=fields["prompt"],
        output_tokens=fields["output"] + fields["thought"],
        reasoning_tokens=fields["thought"],
        cached_tokens=fields["cached"],
        completion_mode=completion_tokens_mode,
        total_tokens=fields["total"],
    )


def interaction_failure(interaction: Any) -> Optional[dict]:
    """失败或取消的 Interaction 转成错误负载，其他状态返回 None。

    带上已计费的用量，调用方记录成本时能读到（和 generateContent 续传中断的处理一致）。
    """
    if not isinstance(interaction, dict) or interaction.get("status") not in ("failed", "cancelled"):
        return None
    errors = interaction.get("errors")
    first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
    upstream_code = first.get("code")
    error = {"message": first.get("message") or f"Interaction {interaction['status']}",
             "type": "upstream_error", "code": 400 if upstream_code in _BLOCKED_CODES else 502}
    if upstream_code:
        error["upstream_code"] = upstream_code
    usage = interaction.get("usage") or {}
    fields = _usage_fields(usage)
    metadata = {
        "promptTokenCount": fields["prompt"], "candidatesTokenCount": fields["output"],
        "thoughtsTokenCount": fields["thought"], "cachedContentTokenCount": fields["cached"]}
    # 续传时每一跳都按完整提示计费，合并用量里带着实际计费量。
    for field in ("billedPromptTokenCount", "billedCachedTokenCount"):
        if field in usage:
            metadata[field] = usage[field]
    return {"error": error, "usageMetadata": metadata}


def _chat_annotations(annotations: Any) -> List[dict]:
    converted: List[dict] = []
    for annotation in annotations if isinstance(annotations, list) else []:
        if not isinstance(annotation, dict):
            continue
        kind = annotation.get("type")
        if kind in ("url_citation", "place_citation") and annotation.get("url"):
            converted.append({"type": "url_citation", "url_citation": {
                "url": annotation["url"],
                "title": annotation.get("title") or annotation.get("name") or annotation["url"]}})
        elif kind == "file_citation":
            note_loss("annotations.file_citation", "文件引用没有网址，Chat 的 url_citation 无法表达，已跳过")
    return converted


def _block_delta(block: Any) -> dict:
    """模型输出的内容块转成 Chat 的 delta 字段；流式和非流式共用，保证结果一致。"""
    if not isinstance(block, dict):
        return {}
    kind = block.get("type")
    if kind == "text":
        delta = {}
        if block.get("text"):
            delta["content"] = str(block["text"])
        annotations = _chat_annotations(block.get("annotations"))
        if annotations:
            delta["annotations"] = annotations
        return delta
    if kind == "image":
        url = block.get("uri")
        if block.get("data"):
            url = f"data:{block.get('mime_type', 'image/png')};base64,{block['data']}"
        return {"images": [{"type": "image_url", "image_url": {"url": url}}]} if url else {}
    note_loss(f"output.{kind}", f"Chat 协议无法表达模型输出的 {kind} 内容，已跳过")
    return {}


def _merge_delta(message: dict, delta: dict) -> None:
    if delta.get("content"):
        message["content"] = (message.get("content") or "") + delta["content"]
    for field in ("images", "annotations"):
        if delta.get(field):
            message.setdefault(field, []).extend(delta[field])


def _note_step_loss(step: dict) -> None:
    kind = step.get("type")
    if kind not in _SERVER_TOOL_STEPS and kind not in ("thought", "model_output", "function_call", "function_result"):
        note_loss(f"steps.{kind}", f"Chat 协议无法表达步骤 {kind}，已跳过")


def convert_interactions_to_openai_response(
    interaction: dict,
    model: str,
    request_id: str,
    completion_tokens_mode: str = MODE_MERGE,
) -> dict:
    """interactions Interaction 对象 → OpenAI chat.completion 响应。

    失败和取消由调用方先用 interaction_failure 处理。同时捕获思考签名，
    供后续轮次回放（流式路径在 InteractionsStreamConverter 里做同样的事）。
    """
    message: dict = {"role": "assistant", "content": None}
    reasoning_parts: List[str] = []
    tool_calls: List[dict] = []
    steps = [step for step in interaction.get("steps") or [] if isinstance(step, dict)]

    for step in steps:
        stype = step.get("type")
        if stype == "model_output":
            for block in step.get("content") or []:
                _merge_delta(message, _block_delta(block))
        elif stype == "thought":
            text = _extract_text_from_content_block(step.get("summary"))
            if text:
                reasoning_parts.append(text)
        elif stype == "function_call":
            args = step.get("arguments") or {}
            if not isinstance(args, dict):
                raise ValueError(f"Interactions function_call '{step.get('name', '')}' 的 arguments 不是对象: {type(args).__name__}")
            tool_calls.append({
                "id": step.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": step.get("name", ""),
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            })
        elif _code_step_text(step):
            _merge_delta(message, {"content": _code_step_text(step)})
        else:
            _note_step_loss(step)

    cache_thought_signatures(_thought_fragments(steps))
    remember_turn_steps(steps)
    for error in interaction.get("errors") or []:
        if isinstance(error, dict):
            note_loss("errors", str(error.get("message") or error.get("code")))
    if interaction.get("status") == "requires_continuation":
        note_loss("continuation", _CONTINUATION_STOPPED)

    message["provider_metadata"] = {"interactions_steps": copy.deepcopy(interaction.get("steps") or [])}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls" if tool_calls else _interactions_finish_reason(interaction.get("status")),
        }],
        "usage": _chat_usage(interaction.get("usage") or {}, completion_tokens_mode),
    }


# ============================================================================
# interactions 流式事件 → OpenAI chunk 流（状态机）
# ============================================================================

def _argument_piece(delta: dict) -> str:
    piece = delta.get("arguments")
    if piece is None:
        # 部分中转仍用旧字段名
        piece = delta.get("partial_arguments")
    if piece is None:
        return ""
    return json.dumps(piece, ensure_ascii=False) if isinstance(piece, (dict, list)) else str(piece)


class _StepAssembler:
    """把流式事件拼回完整步骤，结构和非流式响应的 steps 一致。"""

    def __init__(self):
        self.steps: List[dict] = []
        self.current: Optional[dict] = None
        self.arguments = ""

    def start(self, step: dict) -> dict:
        self.current = copy.deepcopy(step)
        self.arguments = ""
        if self.current.get("type") == "function_call":
            initial = self.current.get("arguments")
            if isinstance(initial, str):
                self.arguments = initial
            elif initial:
                self.arguments = json.dumps(initial, ensure_ascii=False)
        self.steps.append(self.current)
        return self.current

    def delta(self, delta: dict) -> None:
        kind = delta.get("type")
        if self.current is None:
            # 部分中转不发 step.start
            is_thought = kind in ("thought_summary", "thought", "thought_signature")
            self.start({"type": "thought" if is_thought else "model_output"})
        step = self.current
        if kind == "text":
            blocks = step.setdefault("content", [])
            if blocks and blocks[-1].get("type") == "text":
                blocks[-1]["text"] = blocks[-1].get("text", "") + delta.get("text", "")
            else:
                blocks.append({"type": "text", "text": delta.get("text", "")})
        elif kind == "text_annotation_delta":
            blocks = step.setdefault("content", [])
            if blocks and blocks[-1].get("type") == "text":
                blocks[-1].setdefault("annotations", []).extend(delta.get("annotations") or [])
        elif kind in ("thought_summary", "thought"):
            self._add_summary(step, delta)
        elif kind == "thought_signature":
            step["signature"] = delta.get("signature")
        elif kind in ("arguments_delta", "arguments"):
            self.arguments += _argument_piece(delta)
        elif kind in ("image", "audio", "video", "document"):
            step.setdefault("content", []).append(dict(delta))
        elif kind == step.get("type"):
            step.update({key: value for key, value in delta.items() if key != "type"})

    @staticmethod
    def _add_summary(step: dict, delta: dict) -> None:
        block = delta.get("content")
        if not isinstance(block, dict):
            block = {"type": "text", "text": delta["text"]} if delta.get("text") else None
        if block is None:
            return
        summary = step.get("summary")
        summary = summary if isinstance(summary, list) else ([summary] if summary else [])
        if summary and summary[-1].get("type") == "text" and block.get("type") == "text":
            summary[-1] = {**summary[-1], "text": summary[-1].get("text", "") + block.get("text", "")}
        else:
            summary.append(dict(block))
        step["summary"] = summary

    def stop(self) -> Optional[dict]:
        step, self.current = self.current, None
        if step is not None and step.get("type") == "function_call":
            try:
                parsed = json.loads(self.arguments) if self.arguments else {}
            except ValueError:
                parsed = None
            if not isinstance(parsed, dict):
                # 被截断的调用：回放只能用空对象，发给客户端的仍是上游原文。
                note_loss("tool_calls.arguments", f"工具调用 {step.get('name', '')} 的参数不完整")
                parsed = {}
            step["arguments"] = parsed
        return step


class InteractionsStreamConverter:
    """把 interactions SSE 事件流转换为 OpenAI chat.completion.chunk 流。"""

    def __init__(self, model: str, request_id: str, completion_tokens_mode: str = MODE_MERGE):
        self.model = model
        self.request_id = request_id
        self.completion_tokens_mode = completion_tokens_mode
        self.assembler = _StepAssembler()
        self.tool_calls = 0
        # 本次响应签发的全部签名，按 step 顺序供监控展示
        self.signatures: List[str] = []
        self.usage: Optional[dict] = None
        self.completed: bool = False
        self.status: Optional[str] = None
        self._turn_saved = False

    def _chunk(self, delta: dict, finish_reason: Optional[str] = None) -> dict:
        chunk = _oai_chunk_base(self.model, self.request_id)
        chunk["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
        return chunk

    def _save_turn(self) -> None:
        if self._turn_saved:
            return
        self._turn_saved = True
        steps = self.assembler.steps
        self.signatures = [step["signature"] for step in steps if step.get("signature")]
        cache_thought_signatures(_thought_fragments(steps))
        remember_turn_steps(steps)

    def _on_step_start(self, step: dict) -> List[dict]:
        started = self.assembler.start(step)
        chunks: List[dict] = []
        if started.get("type") == "thought":
            text = _extract_text_from_content_block(started.get("summary"))
            if text:
                chunks.append(self._chunk({"reasoning_content": text}))
        elif started.get("type") == "model_output":
            for block in started.get("content") or []:
                delta = _block_delta(block)
                if delta:
                    chunks.append(self._chunk(delta))
        return chunks

    def _on_step_delta(self, delta: dict) -> List[dict]:
        self.assembler.delta(delta)
        kind = delta.get("type")
        if kind == "text":
            return [self._chunk({"content": delta["text"]})] if delta.get("text") else []
        if kind == "text_annotation_delta":
            annotations = _chat_annotations(delta.get("annotations"))
            return [self._chunk({"annotations": annotations})] if annotations else []
        if kind in ("thought_summary", "thought"):
            text = _extract_text_from_content_block(delta.get("content")) or str(delta.get("text", "") or "")
            return [self._chunk({"reasoning_content": text})] if text else []
        if kind in ("image", "audio", "video", "document"):
            converted = _block_delta(delta)
            return [self._chunk(converted)] if converted else []
        return []

    def _on_step_stop(self) -> List[dict]:
        raw_arguments = self.assembler.arguments
        step = self.assembler.stop()
        if step is None:
            return []
        if step.get("type") == "function_call":
            step.setdefault("id", f"call_{uuid.uuid4().hex[:24]}")
            arguments = step["arguments"]
            index, self.tool_calls = self.tool_calls, self.tool_calls + 1
            return [self._chunk({"tool_calls": [{
                "index": index,
                "id": step["id"],
                "type": "function",
                "function": {
                    "name": step.get("name", ""),
                    "arguments": json.dumps(arguments, ensure_ascii=False) if arguments else raw_arguments,
                },
            }]})]
        code = _code_step_text(step)
        if code:
            return [self._chunk({"content": code})]
        _note_step_loss(step)
        return []

    def _on_completed(self, interaction: dict) -> List[dict]:
        self.status = interaction.get("status", self.status)
        self.usage = interaction.get("usage") if isinstance(interaction.get("usage"), dict) else None
        self.completed = True
        failure = interaction_failure({**interaction, "status": self.status})
        if failure:
            return [failure]
        self._save_turn()
        if self.status == "requires_continuation":
            note_loss("continuation", _CONTINUATION_STOPPED)
        chunks: List[dict] = []
        if self.signatures:
            # 客户端保留元数据时可以原样回放；丢了也能按调用 ID 或思考文本找回。
            chunks.append(self._chunk({"provider_metadata": {"interactions_steps": copy.deepcopy(self.assembler.steps)}}))
        finish = "tool_calls" if self.tool_calls else _interactions_finish_reason(self.status)
        chunks.append(self._chunk({}, finish))
        usage_chunk = _oai_chunk_base(self.model, self.request_id)
        usage_chunk.update(choices=[], usage=_chat_usage(self.usage or {}, self.completion_tokens_mode))
        chunks.append(usage_chunk)
        return chunks

    def feed(self, event: dict) -> List[dict]:
        """处理单个 interactions 事件，返回 OpenAI chunk 列表（可能为空）。"""
        if not isinstance(event, dict):
            return []
        event_type = event.get("event_type")
        if event_type == "error" or (event_type is None and "error" in event):
            # 没有 event_type 的是调用层产出的错误（如网络中断），续传中途还带着已计费的用量
            chunk = {"error": event.get("error") or {"message": "Unknown interactions error"}}
            if isinstance(event.get("usageMetadata"), dict):
                chunk["usageMetadata"] = event["usageMetadata"]
            return [chunk]
        interaction = event.get("interaction") if isinstance(event.get("interaction"), dict) else {}
        if event_type in ("interaction.created", "interaction.status_update", "interaction.requires_action"):
            self.status = event.get("status") or interaction.get("status") or self.status
        elif event_type == "step.start":
            return self._on_step_start(event["step"]) if isinstance(event.get("step"), dict) else []
        elif event_type == "step.delta":
            return self._on_step_delta(event["delta"]) if isinstance(event.get("delta"), dict) else []
        elif event_type == "step.stop":
            return self._on_step_stop()
        elif event_type == "interaction.completed":
            return self._on_completed(interaction)
        return []

    def finalize(self) -> List[dict]:
        """上游没有发送 completed 事件时也保存已收到的步骤。"""
        self._save_turn()
        return []


# ============================================================================
# Gemini generateContent ↔ interactions（gemini_v1beta_api 链路用）
# ============================================================================

def _mime_to_content_type(mime_type: str) -> str:
    """MIME 类型 → interactions 内容块 type（image/audio/video/document）"""
    mime_type = (mime_type or "").lower()
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("audio/"):
        return "audio"
    if mime_type.startswith("video/"):
        return "video"
    return "document"


def _gemini_part_to_interactions_block(part: Any) -> Optional[dict]:
    """把 generateContent 的 Part 转成 Interactions 内容块。"""
    if not isinstance(part, dict):
        return None
    if "text" in part and part.get("text") is not None:
        return {"type": "text", "text": str(part.get("text", ""))}

    inline = part.get("inlineData") or part.get("inline_data")
    if isinstance(inline, dict):
        data = inline.get("data", "")
        mime_type = inline.get("mimeType") or inline.get("mime_type") or ""
        if data:
            return {
                "type": _mime_to_content_type(mime_type),
                "data": data,
                "mime_type": mime_type,
            }

    file_data = part.get("fileData") or part.get("file_data")
    if isinstance(file_data, dict):
        uri = file_data.get("fileUri") or file_data.get("file_uri") or ""
        mime_type = file_data.get("mimeType") or file_data.get("mime_type") or ""
        if uri:
            return {
                "type": _mime_to_content_type(mime_type),
                "uri": uri,
                "mime_type": mime_type,
            }
    return None


def _function_response_to_interactions_blocks(response: Any) -> List[dict]:
    """把 Gemini functionResponse 的任意结果整理为内容块数组。"""
    blocks: List[dict] = []

    def append_value(value: Any) -> None:
        if isinstance(value, str):
            if value:
                blocks.append({"type": "text", "text": value})
            return
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item.get("type") in {
                    "text", "image", "audio", "video", "document"
                }:
                    blocks.append(dict(item))
                elif isinstance(item, dict):
                    converted = _gemini_part_to_interactions_block(item)
                    if converted:
                        blocks.append(converted)
                    else:
                        append_value(item)
                else:
                    append_value(item)
            return
        if isinstance(value, dict):
            if "content" in value:
                append_value(value.get("content"))
                return
            if "result" in value:
                append_value(value.get("result"))
                return
            converted = _gemini_part_to_interactions_block(value)
            if converted:
                blocks.append(converted)
                return
            try:
                blocks.append({"type": "text", "text": json.dumps(value, ensure_ascii=False)})
            except (TypeError, ValueError):
                blocks.append({"type": "text", "text": str(value)})
            return
        if value is not None:
            blocks.append({"type": "text", "text": str(value)})

    append_value(response)
    return blocks or [{"type": "text", "text": ""}]


def _interactions_block_to_gemini_part(block: Any, thought: bool = False) -> Optional[dict]:
    """把 Interactions 内容块转成 generateContent Part。"""
    if not isinstance(block, dict):
        return None
    block_type = block.get("type")
    if block_type == "text":
        part: dict = {"text": str(block.get("text", ""))}
    elif block_type in {"image", "audio", "video", "document"}:
        mime_type = block.get("mime_type", "")
        if block.get("data"):
            part = {"inlineData": {"mimeType": mime_type, "data": block["data"]}}
        elif block.get("uri"):
            part = {"fileData": {"mimeType": mime_type, "fileUri": block["uri"]}}
        else:
            return None
    else:
        return None
    if thought:
        part["thought"] = True
    return part


def _interactions_to_gemini_finish_reason(status: Optional[str]) -> str:
    """Interaction.status → generateContent 的 FinishReason 枚举。"""
    if status == "incomplete":
        return "MAX_TOKENS"
    if status == "requires_continuation":
        return "CONTINUATION"
    if status in ("failed", "cancelled"):
        return "OTHER"
    return "STOP"


_GC_TOOL_FIELDS = {
    "googleSearch": "google_search", "google_search": "google_search",
    "urlContext": "url_context", "url_context": "url_context",
    "codeExecution": "code_execution", "code_execution": "code_execution",
    "googleMaps": "google_maps", "google_maps": "google_maps",
    "fileSearch": "file_search", "file_search": "file_search",
    "computerUse": "computer_use", "computer_use": "computer_use",
}
_GC_TOOL_OPTIONS = {
    "searchTypes": "search_types", "enableWidget": "enable_widget", "fileSearchStoreNames": "file_search_store_names",
    "topK": "top_k", "metadataFilter": "metadata_filter", "excludedPredefinedFunctions": "excluded_predefined_functions",
}
_GC_SAFETY_CATEGORIES = {
    "HARM_CATEGORY_HATE_SPEECH": "hate_speech", "HARM_CATEGORY_DANGEROUS_CONTENT": "dangerous_content",
    "HARM_CATEGORY_HARASSMENT": "harassment", "HARM_CATEGORY_SEXUALLY_EXPLICIT": "sexually_explicit",
    "HARM_CATEGORY_JAILBREAK": "jailbreak",
}
_GC_SAFETY_THRESHOLDS = {
    "BLOCK_LOW_AND_ABOVE": "block_low_and_above", "BLOCK_MEDIUM_AND_ABOVE": "block_medium_and_above",
    "BLOCK_ONLY_HIGH": "block_only_high", "BLOCK_NONE": "off", "OFF": "off",
}
_GC_HANDLED_FIELDS = frozenset({
    "contents", "systemInstruction", "system_instruction", "generationConfig", "tools", "toolConfig",
    "safetySettings", "labels", "store", "model", "continuationToken",
})
_GC_HANDLED_GENERATION_FIELDS = frozenset({
    "maxOutputTokens", "stopSequences", "seed", "thinkingConfig", "responseMimeType", "responseSchema",
    "responseJsonSchema", "responseModalities",
})


def _gc_tools(tools: Any) -> List[dict]:
    """generateContent 的 tools → Interactions 工具，保持客户端列出的顺序。"""
    converted: List[dict] = []
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict):
            continue
        for key, value in tool.items():
            if key in ("functionDeclarations", "function_declarations"):
                for declaration in value or []:
                    if not isinstance(declaration, dict) or not declaration.get("name"):
                        continue
                    converted.append({
                        "type": "function",
                        "name": declaration["name"],
                        "description": declaration.get("description", ""),
                        "parameters": (declaration.get("parametersJsonSchema")
                                       or declaration.get("parameters_json_schema")
                                       or declaration.get("parameters") or {}),
                    })
            elif key in _GC_TOOL_FIELDS:
                options = value if isinstance(value, dict) else {}
                converted.append({"type": _GC_TOOL_FIELDS[key],
                                  **{_GC_TOOL_OPTIONS.get(name, name): option for name, option in options.items()}})
            else:
                note_loss(f"tools.{key}", f"Interactions 没有工具 {key}，未发送")
    return converted


def _gc_safety(settings: Any) -> List[dict]:
    converted: List[dict] = []
    for setting in settings if isinstance(settings, list) else []:
        category = setting.get("category") if isinstance(setting, dict) else None
        threshold = setting.get("threshold") if isinstance(setting, dict) else None
        if category in _GC_SAFETY_CATEGORIES and threshold in _GC_SAFETY_THRESHOLDS:
            converted.append({"type": _GC_SAFETY_CATEGORIES[category], "threshold": _GC_SAFETY_THRESHOLDS[threshold]})
        else:
            note_loss(f"safetySettings.{category}", f"Interactions 不支持安全设置 {category}={threshold}，未发送")
    return converted


def _gc_response_format(config: dict) -> Optional[Any]:
    schema = config.get("responseJsonSchema") or config.get("responseSchema")
    mime_type = config.get("responseMimeType") or ("application/json" if schema else None)
    text_format = None
    if mime_type:
        if mime_type in ("application/json", "text/plain"):
            text_format = {"type": "text", "mime_type": mime_type}
            if schema:
                text_format["schema"] = schema
        else:
            note_loss("generationConfig.responseMimeType", f"Interactions 不支持输出格式 {mime_type}，未发送")
    modalities = [str(item).lower() for item in config.get("responseModalities") or []]
    if not modalities or modalities == ["text"]:
        return text_format
    formats = [text_format or {"type": "text"}] if "text" in modalities else []
    formats.extend({"type": kind} for kind in modalities if kind != "text")
    return formats


def _gc_generation_config(config: dict, model: str) -> dict:
    converted: Dict[str, Any] = {}
    for source, target in (("maxOutputTokens", "max_output_tokens"), ("seed", "seed")):
        if config.get(source) is not None:
            converted[target] = config[source]
    if isinstance(config.get("stopSequences"), list) and config["stopSequences"]:
        converted["stop_sequences"] = config["stopSequences"]
    thinking = config.get("thinkingConfig")
    if isinstance(thinking, dict):
        level = map_thinking_config_to_level(thinking, model)
        if level:
            converted["thinking_level"] = level
        summaries = map_thinking_config_to_summaries(thinking)
        if summaries:
            converted["thinking_summaries"] = summaries
    for field in config.keys() - _GC_HANDLED_GENERATION_FIELDS:
        note_loss(f"generationConfig.{field}", "Interactions 没有对应字段，未发送")
    return converted


def _gc_function_result(response: dict, call_id: str) -> dict:
    payload = response.get("response")
    is_error = isinstance(payload, dict) and payload.keys() == {"error"}
    blocks = _function_response_to_interactions_blocks(payload["error"] if is_error else payload)
    for media_part in response.get("parts") or []:
        converted = _gemini_part_to_interactions_block(media_part)
        if converted:
            blocks.append(converted)
    step = {"type": "function_result", "name": response.get("name", ""), "call_id": call_id, "result": blocks}
    if is_error:
        step["is_error"] = True
    return step


def _append_thought_parts(parts: List[dict], step: dict) -> None:
    """将一个 thought step 追加为带 thoughtSignature 的 Gemini Parts。"""
    summary = step.get("summary")
    blocks = summary if isinstance(summary, list) else ([summary] if summary else [])
    thought_parts: List[dict] = []
    for block in blocks:
        part = _interactions_block_to_gemini_part(block, thought=True)
        if part:
            thought_parts.append(part)
    signature = step.get("signature")
    if signature:
        if thought_parts:
            thought_parts[-1]["thoughtSignature"] = signature
        else:
            thought_parts.append({"thought": True, "thoughtSignature": signature})
    parts.extend(thought_parts)


def convert_gemini_gc_to_interactions(gc_req: dict, model: str) -> dict:
    """Gemini generateContent 请求体 → interactions 请求体（无状态模式）。"""
    steps: List[dict] = []
    system_parts: List[str] = []
    # 没有 ID 的调用按位置编号，同一段历史每次编号相同，缓存前缀才稳定。
    pending_calls: Dict[str, List[str]] = {}
    raw_contents = gc_req.get("contents", []) if isinstance(gc_req, dict) else []
    if isinstance(raw_contents, str):
        raw_contents = [{"role": "user", "parts": [{"text": raw_contents}]}]

    for content in raw_contents or []:
        if isinstance(content, str):
            content = {"role": "user", "parts": [{"text": content}]}
        if not isinstance(content, dict):
            continue
        role = content.get("role") or "user"
        parts = content.get("parts", []) or []
        if not isinstance(parts, list):
            parts = [parts]
        content_blocks: List[dict] = []

        def flush_content_blocks() -> None:
            nonlocal content_blocks
            if content_blocks:
                step_type = "model_output" if role == "model" else "user_input"
                steps.append({"type": step_type, "content": content_blocks})
                content_blocks = []

        for part in parts:
            if isinstance(part, str):
                if part:
                    content_blocks.append({"type": "text", "text": part})
                continue
            if not isinstance(part, dict):
                continue

            is_thought = bool(part.get("thought"))
            if is_thought:
                flush_content_blocks()
                thought_step: dict = {"type": "thought"}
                thought_block = _gemini_part_to_interactions_block(part)
                if thought_block:
                    thought_step["summary"] = [thought_block]
                signature = part.get("thoughtSignature") or part.get("thought_signature")
                if signature:
                    thought_step["signature"] = signature
                # 即使只有签名也必须保留 thought step。
                steps.append(thought_step)
                continue

            if "functionCall" in part or "function_call" in part:
                flush_content_blocks()
                fc = part.get("functionCall") or part.get("function_call") or {}
                if not isinstance(fc, dict):
                    continue
                signature = part.get("thoughtSignature") or part.get("thought_signature")
                if signature:
                    steps.append({"type": "thought", "signature": signature})
                call_id = fc.get("id") or f"call_{len(steps)}"
                pending_calls.setdefault(fc.get("name", ""), []).append(call_id)
                steps.append({
                    "type": "function_call",
                    "name": fc.get("name", ""),
                    "arguments": fc.get("args") or fc.get("arguments") or {},
                    "id": call_id,
                })
                continue

            if "functionResponse" in part or "function_response" in part:
                flush_content_blocks()
                fr = part.get("functionResponse") or part.get("function_response") or {}
                if not isinstance(fr, dict):
                    continue
                pending = pending_calls.get(fr.get("name", "")) or []
                call_id = fr.get("id") or fr.get("call_id") or (pending[0] if pending else "")
                if call_id in pending:
                    pending.remove(call_id)
                steps.append(_gc_function_result(fr, call_id))
                continue

            converted = _gemini_part_to_interactions_block(part)
            if converted:
                content_blocks.append(converted)

        flush_content_blocks()

    body: Dict[str, Any] = {
        "model": model,
        "input": steps,
        "store": gc_req.get("store", False) if isinstance(gc_req, dict) else False,
    }

    system_instruction = (gc_req.get("systemInstruction") or gc_req.get("system_instruction")) if isinstance(gc_req, dict) else None
    if isinstance(system_instruction, dict):
        for part in system_instruction.get("parts", []) or []:
            if isinstance(part, dict) and part.get("text"):
                system_parts.append(str(part["text"]))
    elif isinstance(system_instruction, str) and system_instruction:
        system_parts.append(system_instruction)
    if system_parts:
        body["system_instruction"] = "\n".join(system_parts)

    gc = gc_req.get("generationConfig") if isinstance(gc_req.get("generationConfig"), dict) else {}
    gen_config = _gc_generation_config(gc, model)
    response_format = _gc_response_format(gc)
    if response_format:
        body["response_format"] = response_format
    if gen_config:
        body["generation_config"] = gen_config

    function_tools = _gc_tools(gc_req.get("tools"))
    if function_tools:
        body["tools"] = function_tools
    safety = _gc_safety(gc_req.get("safetySettings"))
    if safety:
        body["safety_settings"] = safety
    if gc_req.get("labels"):
        body["labels"] = gc_req["labels"]
    # 原生入口由客户端决定是否续传：每跳重发原请求，只多一个 token。
    if gc_req.get("continuationToken"):
        body["continuation_token"] = gc_req["continuationToken"]
    for field in gc_req.keys() - _GC_HANDLED_FIELDS:
        note_loss(field, "Interactions 没有对应字段，未发送")

    tool_config = gc_req.get("toolConfig") or {}
    function_calling_config = (
        tool_config.get("functionCallingConfig")
        if isinstance(tool_config, dict) else None
    )
    if isinstance(function_calling_config, dict) and body.get("tools"):
        mode = str(function_calling_config.get("mode", "AUTO")).upper()
        if mode == "NONE":
            # 保留 tools：删掉会改变缓存前缀。
            body.setdefault("generation_config", {})["tool_choice"] = "none"
        elif mode in {"ANY", "VALIDATED"}:
            allowed = {"mode": "any" if mode == "ANY" else "validated"}
            names = function_calling_config.get("allowedFunctionNames")
            if isinstance(names, list) and names:
                allowed["tools"] = names
            body.setdefault("generation_config", {})["tool_choice"] = {
                "allowed_tools": allowed
            }
        elif mode != "AUTO":
            logger.debug(
                "[GEMINI_INTERACTIONS] 未识别的 functionCallingConfig.mode=%s，使用默认模式",
                mode,
            )

    return body


def _server_step_parts(step: dict) -> List[dict]:
    """代码执行步骤 → generateContent 的 part；其他步骤没有对应 part。"""
    kind = step.get("type")
    if kind == "code_execution_call":
        arguments = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
        return [{"executableCode": {"language": str(arguments.get("language") or "python").upper(),
                                    "code": arguments.get("code", "")}}]
    if kind == "code_execution_result":
        return [{"codeExecutionResult": {"outcome": "OUTCOME_FAILED" if step.get("is_error") else "OUTCOME_OK",
                                         "output": step.get("result", "")}}]
    return []


class _Grounding:
    """从步骤和引用标注里汇总 generateContent 的 groundingMetadata。"""

    def __init__(self):
        self.queries: List[str] = []
        self.chunks: List[dict] = []
        self.suggestions: Optional[str] = None

    def add_step(self, step: dict) -> None:
        if step.get("type") == "google_search_call":
            arguments = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
            self.queries.extend(query for query in arguments.get("queries") or [] if query not in self.queries)
        elif step.get("type") == "google_search_result" and step.get("search_suggestions"):
            self.suggestions = step["search_suggestions"]
        for block in step.get("content") or []:
            for annotation in (block.get("annotations") or []) if isinstance(block, dict) else []:
                url = annotation.get("url") if isinstance(annotation, dict) else None
                if url and annotation.get("type") == "url_citation" and all(chunk["web"]["uri"] != url for chunk in self.chunks):
                    self.chunks.append({"web": {"uri": url, "title": annotation.get("title") or url}})

    def metadata(self) -> Optional[dict]:
        grounding: Dict[str, Any] = {}
        if self.queries:
            grounding["webSearchQueries"] = self.queries
        if self.chunks:
            grounding["groundingChunks"] = self.chunks
        if self.suggestions:
            grounding["searchEntryPoint"] = {"renderedContent": self.suggestions}
        return grounding or None


def _gc_usage_metadata(usage: dict) -> dict:
    fields = _usage_fields(usage)
    metadata = {
        "promptTokenCount": fields["initial"],
        "candidatesTokenCount": fields["output"],
        "thoughtsTokenCount": fields["thought"],
        "totalTokenCount": fields["total"] or (fields["prompt"] + fields["output"] + fields["thought"]),
    }
    if fields["tool_use"]:
        metadata["toolUsePromptTokenCount"] = fields["tool_use"]
    if fields["cached"]:
        metadata["cachedContentTokenCount"] = fields["cached"]
    return metadata


def convert_interactions_to_gemini_gc(interaction: dict) -> dict:
    """interactions Interaction 对象 → Gemini generateContent 响应。"""
    parts: List[dict] = []
    grounding = _Grounding()
    for step in interaction.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        grounding.add_step(step)
        stype = step.get("type")
        if stype == "thought":
            _append_thought_parts(parts, step)
        elif stype == "model_output":
            for block in step.get("content", []) or []:
                part = _interactions_block_to_gemini_part(block)
                if part:
                    parts.append(part)
        elif stype == "function_call":
            args = step.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            fc: dict = {
                "name": step.get("name", ""),
                "args": args if isinstance(args, dict) else {},
            }
            if step.get("id"):
                fc["id"] = step["id"]
            part = {"functionCall": fc}
            # 签名属于 part，不属于 functionCall 对象。
            if step.get("signature"):
                part["thoughtSignature"] = step["signature"]
            parts.append(part)
        else:
            parts.extend(_server_step_parts(step))

    usage = interaction.get("usage") or {}
    metadata = _gc_usage_metadata(usage)
    candidate: dict = {
        "content": {"role": "model", "parts": parts},
        "finishReason": _interactions_to_gemini_finish_reason(interaction.get("status")),
    }
    if grounding.metadata():
        candidate["groundingMetadata"] = grounding.metadata()
    if interaction.get("continuation_token"):
        candidate["continuationToken"] = interaction["continuation_token"]
    response: dict = {"candidates": [candidate], "usageMetadata": metadata}
    response["usage"] = {
        "prompt_tokens": metadata["promptTokenCount"],
        "completion_tokens": metadata["candidatesTokenCount"] + metadata["thoughtsTokenCount"],
        "total_tokens": metadata["totalTokenCount"],
    }
    return response


class InteractionsToGeminiGCConverter:
    """interactions 流式事件 → Gemini generateContent SSE chunk 流。"""

    def __init__(self):
        self.current_step_type: Optional[str] = None
        self.current_func: Optional[dict] = None
        self.current_thought_blocks: List[dict] = []
        self.current_thought_signature: Optional[str] = None
        self.completed: bool = False
        self.status: Optional[str] = None
        self.usage: Optional[dict] = None
        self.grounding = _Grounding()
        self.assembler = _StepAssembler()

    @staticmethod
    def _chunk(parts: Optional[List[dict]] = None) -> dict:
        return {
            "candidates": [{
                "content": {"role": "model", "parts": parts or []},
            }]
        }

    @staticmethod
    def _argument_piece(delta: dict) -> str:
        piece = delta.get("arguments")
        if piece is None:
            piece = delta.get("partial_arguments")
        if piece is None:
            return ""
        if isinstance(piece, (dict, list)):
            return json.dumps(piece, ensure_ascii=False)
        return str(piece)

    def _flush_thought(self) -> List[dict]:
        # 摘要已经逐段发出，这里只补签名。
        signature, self.current_thought_signature = self.current_thought_signature, None
        return [self._chunk([{"thought": True, "thoughtSignature": signature}])] if signature else []

    def feed(self, event: dict) -> List[dict]:
        if not isinstance(event, dict):
            return []
        event_type = event.get("event_type")
        chunks: List[dict] = []

        if event_type in ("interaction.created", "interaction.status_update", "interaction.requires_action"):
            interaction = event.get("interaction") or {}
            if isinstance(interaction, dict):
                self.status = interaction.get("status", self.status)
            self.status = event.get("status", self.status)

        elif event_type == "step.start":
            step = event.get("step") or {}
            if not isinstance(step, dict):
                return []
            self.current_step_type = step.get("type")
            self.assembler.start(step)
            if self.current_step_type == "function_call":
                initial_arguments = step.get("arguments")
                if isinstance(initial_arguments, (dict, list)):
                    initial_arguments = (
                        json.dumps(initial_arguments, ensure_ascii=False)
                        if initial_arguments else ""
                    )
                self.current_func = {
                    "name": step.get("name", ""),
                    "arguments": str(initial_arguments or ""),
                    "id": step.get("id"),
                }
            elif self.current_step_type == "thought":
                summary = step.get("summary")
                blocks = summary if isinstance(summary, list) else ([summary] if summary else [])
                self.current_thought_signature = step.get("signature") or None
                parts = [_interactions_block_to_gemini_part(block, thought=True)
                         for block in blocks if isinstance(block, dict)]
                if any(parts):
                    chunks.append(self._chunk([part for part in parts if part]))
            elif self.current_step_type == "model_output":
                for block in step.get("content", []) or []:
                    if isinstance(block, dict):
                        part = _interactions_block_to_gemini_part(block)
                        if part:
                            chunks.append(self._chunk([part]))

        elif event_type == "step.delta":
            delta = event.get("delta") or {}
            if not isinstance(delta, dict):
                return []
            delta_type = delta.get("type")
            self.assembler.delta(delta)
            if delta_type == "text":
                text = delta.get("text", "")
                if text:
                    chunks.append(self._chunk([{"text": text}]))
            elif delta_type in ("image", "audio", "video", "document"):
                block = dict(delta)
                part = _interactions_block_to_gemini_part(block)
                if part:
                    chunks.append(self._chunk([part]))
            elif delta_type in ("thought_summary", "thought"):
                block = delta.get("content")
                if not isinstance(block, dict):
                    text = delta.get("text", "")
                    block = {"type": "text", "text": text} if text else None
                part = _interactions_block_to_gemini_part(block, thought=True)
                if part:
                    chunks.append(self._chunk([part]))
            elif delta_type == "thought_signature":
                if delta.get("signature"):
                    self.current_thought_signature = str(delta["signature"])
            elif delta_type in ("arguments_delta", "arguments"):
                if self.current_func:
                    self.current_func["arguments"] += self._argument_piece(delta)

        elif event_type == "step.stop":
            if self.current_step_type == "function_call" and self.current_func:
                args_value: Any = self.current_func["arguments"]
                try:
                    args_value = json.loads(args_value) if args_value else {}
                except json.JSONDecodeError:
                    args_value = {}
                fc: dict = {
                    "name": self.current_func["name"],
                    "args": args_value if isinstance(args_value, dict) else {},
                }
                if self.current_func.get("id"):
                    fc["id"] = self.current_func["id"]
                chunks.append(self._chunk([{"functionCall": fc}]))
                self.current_func = None
            elif self.current_step_type == "thought":
                chunks.extend(self._flush_thought())
            else:
                step = self.assembler.current or {}
                self.grounding.add_step(step)
                parts = _server_step_parts(step)
                if parts:
                    chunks.append(self._chunk(parts))
            self.assembler.stop()
            self.current_step_type = None

        elif event_type == "interaction.completed":
            interaction = event.get("interaction") or {}
            if not isinstance(interaction, dict):
                interaction = {}
            self.status = interaction.get("status", self.status)
            self.usage = interaction.get("usage") if isinstance(
                interaction.get("usage"), dict) else None
            self.completed = True
            if self.status in ("failed", "cancelled"):
                # 错误由 forward_native_exchange 统一发出，这里再发客户端会收到两次。
                return []
            candidate: dict = {"finishReason": _interactions_to_gemini_finish_reason(self.status)}
            if self.grounding.metadata():
                candidate["groundingMetadata"] = self.grounding.metadata()
            if interaction.get("continuation_token"):
                candidate["continuationToken"] = interaction["continuation_token"]
            chunks.append({"candidates": [candidate]})
            chunks.append({"usageMetadata": _gc_usage_metadata(self.usage or {})})

        elif event_type == "error":
            chunks.append({"error": event.get("error") or {"message": "Unknown interactions error"}})

        return chunks
