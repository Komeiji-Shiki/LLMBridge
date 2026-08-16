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
import logging
import mimetypes
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

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
]


# ============================================================================
# 思考签名缓存（响应侧捕获 → 请求侧前缀匹配注入）
# ============================================================================

# 结构：{joined_summary: {"fragments": [(fragment_text, signature), ...], "ts": float}}
# 以"拼接后的完整思考文本"为键；匹配时对客户端 reasoning_content 做前缀匹配。
_thought_signature_cache: Dict[str, Dict[str, Any]] = {}
_SIGNATURE_CACHE_MAX_ENTRIES = 200
_SIGNATURE_CACHE_TTL_SECONDS = 3600  # 1 小时


def _cleanup_thought_signature_cache() -> None:
    """清理过期与超容量的签名缓存（惰性调用，请求路径上不阻塞）"""
    now = time.time()
    expired = [
        key for key, entry in _thought_signature_cache.items()
        if now - entry.get("ts", 0) > _SIGNATURE_CACHE_TTL_SECONDS
    ]
    for key in expired:
        del _thought_signature_cache[key]
    # 超容量时淘汰最旧条目（dict 保持插入顺序）
    while len(_thought_signature_cache) > _SIGNATURE_CACHE_MAX_ENTRIES:
        _thought_signature_cache.pop(next(iter(_thought_signature_cache)))


def cache_thought_signatures(fragments: List[Tuple[str, str]]) -> None:
    """缓存一轮思考的 (摘要片段, 签名) 列表。

    Args:
        fragments: [(thought_summary 文本片段, signature), ...]，按出现顺序。
            流式：thought step 在 step.stop 时收尾调用一次；
            非流式：遍历 Interaction.steps 中的 thought 步骤时调用。
    """
    if not fragments:
        return
    valid = [(text, sig) for text, sig in fragments if text and sig]
    if not valid:
        return
    joined = "".join(text for text, _ in valid)
    if not joined:
        return
    _thought_signature_cache[joined] = {
        "fragments": valid,
        "ts": time.time(),
    }
    _cleanup_thought_signature_cache()
    logger.debug(f"[GEMINI_INTERACTIONS] 已缓存思考签名: {len(valid)} 片段, {len(joined)} 字符")


def match_and_inject_thought_signatures(reasoning_content: str) -> List[dict]:
    """对客户端回传的 reasoning_content 做前缀匹配，返回注入签名的 thought steps。

    匹配策略（按缓存条目顺序切分客户端文本）：
    - 客户端文本是缓存拼接文本的前缀（客户端裁剪了思考尾部）→ 注入能匹配的前缀部分
    - 客户端文本完整等于缓存拼接文本 → 全部注入
    - 无法匹配 → 返回空列表（不注入，对话仍可继续，仅丢失推理连续性）

    Returns:
        [{"type": "thought", "signature": ..., "summary": {"type": "text", "text": ...}}, ...]
    """
    if not reasoning_content or not reasoning_content.strip():
        return []
    _cleanup_thought_signature_cache()

    best_key = None
    best_joined = ""
    for key in _thought_signature_cache:
        # 键即拼接后的完整思考文本；前缀匹配：客户端文本是缓存的前缀，
        # 或缓存是客户端文本的前缀（取更长的缓存项，信息更完整）
        if reasoning_content.startswith(key) or key.startswith(reasoning_content):
            if len(key) > len(best_joined):
                best_joined = key
                best_key = key
    if best_key is None:
        return []

    entry = _thought_signature_cache[best_key]
    steps: List[dict] = []
    remaining = reasoning_content
    for fragment, signature in entry["fragments"]:
        if not remaining:
            break
        if remaining.startswith(fragment):
            steps.append({
                "type": "thought",
                "signature": signature,
                "summary": [{"type": "text", "text": fragment}],
            })
            remaining = remaining[len(fragment):]
        elif fragment.startswith(remaining):
            # 客户端文本是当前片段的前缀（尾部被裁剪）
            steps.append({
                "type": "thought",
                "signature": signature,
                "summary": [{"type": "text", "text": remaining}],
            })
            remaining = ""
            break
        else:
            # 文本被改写或与片段不连续，放弃剩余部分的注入
            logger.debug("[GEMINI_INTERACTIONS] 思考文本前缀匹配中断，放弃剩余签名注入")
            break

    if steps:
        logger.info(
            f"[GEMINI_INTERACTIONS] 前缀匹配命中，注入 {len(steps)} 个 thought 步骤的签名")
    return steps


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

    if item_type in ("text", "input_text"):
        text = item.get("text", "")
        return {"type": "text", "text": text} if text else None

    if item_type == "image_url":
        image_url_data = item.get("image_url") or {}
        url = image_url_data.get("url", "")
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

    if item_type == "input_audio":
        audio_data = item.get("input_audio") or {}
        data = audio_data.get("data", "")
        fmt = audio_data.get("format", "")
        if data:
            mime_type = f"audio/{fmt}" if fmt else "audio/wav"
            return {"type": "audio", "data": data, "mime_type": mime_type}
        return None

    if item_type in ("audio_url", "video_url"):
        url_data = item.get("audio_url") or item.get("video_url") or {}
        url = url_data.get("url", "")
        kind = "audio" if item_type == "audio_url" else "video"
        if url.startswith(("http://", "https://")):
            guessed_mime, _ = mimetypes.guess_type(url.split("?", 1)[0])
            return {"type": kind, "uri": url, "mime_type": guessed_mime or f"{kind}/mp4"}
        return None

    # 未知类型块：能提取文本则保留，否则跳过
    fallback_text = item.get("text")
    if isinstance(fallback_text, str) and fallback_text:
        return {"type": "text", "text": fallback_text}
    logger.warning(f"[GEMINI_INTERACTIONS] 跳过不支持的内容块类型: {item_type}")
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
        "google_search", "file_search", "google_maps", "retrieval",
    }:
        return dict(tool)
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
) -> Tuple[List[dict], str]:
    """OpenAI 消息列表 → (interactions steps, system_instruction 文本)。

    无状态模式约定（store=false）：
    - system → 拼入 system_instruction（单独返回，调用方写入顶层字段）
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

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")

        if role == "system":
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
            # 优先用 assistant tool_calls 建立的映射；未命中时回退到旧版推断
            func_name = tool_id_to_name.get(tool_call_id) or (
                tool_call_id.split("_", 1)[-1] if "_" in tool_call_id else "_unknown")

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
                "name": func_name,
                "call_id": tool_call_id,
                "result": result_blocks or [{"type": "text", "text": ""}],
            }
            steps.append(result_step)
            continue

        logger.warning(f"[GEMINI_INTERACTIONS] 跳过未知消息角色: {role}")

    return steps, "\n".join(system_parts)


def map_thinking_config_to_level(thinking_config: Optional[dict]) -> Optional[str]:
    """将 Gemini thinkingConfig.thinkingLevel 映射为 Interactions 的 thinking_level。"""
    if not isinstance(thinking_config, dict):
        return None
    level = thinking_config.get("thinkingLevel")
    if level:
        return str(level).lower()
    if thinking_config.get("thinkingBudget") is not None:
        logger.debug("[GEMINI_INTERACTIONS] thinkingBudget 在 interactions 无对应字段，已忽略")
    return None


def map_thinking_config_to_summaries(thinking_config: Optional[dict]) -> Optional[str]:
    """将 includeThoughts 映射为 Interactions 的 thinking_summaries。"""
    if not isinstance(thinking_config, dict) or "includeThoughts" not in thinking_config:
        return None
    return "auto" if thinking_config.get("includeThoughts") else "none"

def map_tool_choice_to_interactions(tool_choice: Optional[Any]) -> Optional[dict]:
    """把 OpenAI tool_choice 映射为 Interactions generation_config.tool_choice。"""
    if tool_choice in (None, "auto"):
        return None
    if tool_choice in ("required", "any"):
        return {"allowed_tools": {"mode": "any"}}
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "none":
            return None
        if tool_choice.get("type") == "function":
            function = tool_choice.get("function") or {}
            name = function.get("name")
            if name:
                return {"allowed_tools": {"mode": "any", "tools": [name]}}
        allowed = tool_choice.get("allowed_tools")
        if isinstance(allowed, dict):
            return {"allowed_tools": dict(allowed)}
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
    steps, system_instruction = convert_oai_messages_to_interactions(messages)

    body: Dict[str, Any] = {
        "model": model,
        "input": steps,
        "store": False,
        "stream": bool(stream),
    }
    if system_instruction:
        body["system_instruction"] = system_instruction

    gen_config: Dict[str, Any] = {}
    if temperature is not None:
        gen_config["temperature"] = temperature
    if top_p is not None:
        # 当前 Interactions GenerationConfig 没有稳定公开 top_p 字段，不能把
        # Chat Completions 的旧字段原样发送，否则新版接口会返回 schema 错误。
        logger.debug("[GEMINI_INTERACTIONS] interactions 未使用 top_p 参数")
    if max_tokens is not None:
        gen_config["max_output_tokens"] = max_tokens
    if isinstance(stop_sequences, list) and stop_sequences:
        gen_config["stop_sequences"] = stop_sequences
    thinking_level = map_thinking_config_to_level(thinking_config)
    if thinking_level:
        gen_config["thinking_level"] = thinking_level
    thinking_summaries = map_thinking_config_to_summaries(thinking_config)
    if thinking_summaries:
        gen_config["thinking_summaries"] = thinking_summaries
    if gen_config:
        body["generation_config"] = gen_config

    # tool_choice="none" 时不带 tools；其余情况透传
    tool_choice_none = tool_choice == "none" or (
        isinstance(tool_choice, dict) and tool_choice.get("type") == "none")
    if tools and not tool_choice_none:
        interaction_tools = [
            converted for tool in tools
            if (converted := _oai_tool_to_interactions_tool(tool)) is not None
        ]
        if interaction_tools:
            body["tools"] = interaction_tools
            logger.info(f"[GEMINI_INTERACTIONS] 透传 {len(interaction_tools)} 个工具")
    elif tool_choice_none:
        logger.info("[GEMINI_INTERACTIONS] tool_choice=none，不携带 tools")

    mapped_tool_choice = map_tool_choice_to_interactions(tool_choice)
    if mapped_tool_choice:
        body.setdefault("generation_config", {})["tool_choice"] = mapped_tool_choice

    if isinstance(response_format, (dict, list)):
        if isinstance(response_format, list):
            body["response_format"] = response_format
            for item in response_format:
                if isinstance(item, dict) and item.get("mime_type"):
                    body["response_mime_type"] = item["mime_type"]
                    break
        else:
            rf_type = response_format.get("type")
            if rf_type == "json_object":
                body["response_format"] = {
                    "type": "text",
                    "mime_type": "application/json",
                }
                body["response_mime_type"] = "application/json"
            elif rf_type == "json_schema":
                schema_info = response_format.get("json_schema") or {}
                body["response_format"] = {
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": schema_info.get("schema") or response_format.get("schema") or {},
                }
                body["response_mime_type"] = "application/json"
            elif rf_type:
                body["response_format"] = dict(response_format)
                if response_format.get("mime_type"):
                    body["response_mime_type"] = response_format["mime_type"]

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
    """Interaction.status → OpenAI finish_reason"""
    if status == "requires_action":
        return "tool_calls"
    if status == "incomplete":
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


def convert_interactions_to_openai_response(
    interaction: dict,
    model: str,
    request_id: str,
) -> dict:
    """interactions Interaction 对象 → OpenAI chat.completion 响应。

    同时捕获 thought 步骤的签名进缓存（非流式路径；流式路径由
    InteractionsStreamConverter 在 step.stop 时捕获）。
    """
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: List[dict] = []
    thought_fragments: List[Tuple[str, str]] = []

    for step in interaction.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        stype = step.get("type")
        if stype == "model_output":
            for block in step.get("content", []) or []:
                text = _extract_text_from_content_block(block)
                if text:
                    content_parts.append(text)
        elif stype == "thought":
            text = _extract_text_from_content_block(step.get("summary"))
            if text:
                reasoning_parts.append(text)
            signature = step.get("signature")
            if text and signature:
                thought_fragments.append((text, signature))
        elif stype == "function_call":
            args = step.get("arguments")
            if isinstance(args, dict):
                try:
                    args_str = json.dumps(args, ensure_ascii=False)
                except (TypeError, ValueError):
                    args_str = "{}"
            else:
                args_str = str(args) if args else "{}"
            tool_calls.append({
                "id": step.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": step.get("name", ""),
                    "arguments": args_str,
                },
            })
        # 其他步骤类型（google_search_call 等服务器端工具）信息有限，跳过。

    if thought_fragments:
        cache_thought_signatures(thought_fragments)

    usage = interaction.get("usage") or {}
    prompt_tokens = _usage_int(usage, "total_input_tokens", "prompt_tokens")
    output_tokens = _usage_int(usage, "total_output_tokens", "completion_tokens")
    thought_tokens = _usage_int(usage, "total_thought_tokens", "reasoning_tokens")
    completion_tokens = output_tokens + thought_tokens
    total_tokens = _usage_int(usage, "total_tokens") or (prompt_tokens + completion_tokens)

    message: dict = {"role": "assistant"}
    message["content"] = "".join(content_parts) if content_parts else None
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls
        if not content_parts:
            message["content"] = None

    return {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": _interactions_finish_reason(interaction.get("status")),
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }


# ============================================================================
# interactions 流式事件 → OpenAI chunk 流（状态机）
# ============================================================================

class InteractionsStreamConverter:
    """把 interactions SSE 事件流转换为 OpenAI chat.completion.chunk 流。

    Interactions 的 thought signature 属于整个 thought step。流式期间可能有
    多个 thought_summary 增量，必须先合并摘要，再与 step.stop 前收到的签名
    一起缓存，下一轮无状态请求才能完整回传。
    """

    def __init__(self, model: str, request_id: str):
        self.model = model
        self.request_id = request_id
        self.current_step_type: Optional[str] = None
        self.current_func: Optional[dict] = None
        self.current_thought_text = ""
        self.current_thought_signature: Optional[str] = None
        self.usage: Optional[dict] = None
        self.completed: bool = False
        self.status: Optional[str] = None

    def _chunk(self, extra: dict) -> dict:
        chunk = _oai_chunk_base(self.model, self.request_id)
        chunk.update(extra)
        return chunk

    def _reasoning_chunk(self, text: str) -> dict:
        return self._chunk({
            "choices": [{
                "index": 0,
                "delta": {"reasoning_content": text},
            }],
        })

    def _append_argument_delta(self, delta: dict) -> None:
        if not self.current_func:
            return
        piece = delta.get("arguments")
        if piece is None:
            piece = delta.get("partial_arguments")
        if piece is None:
            return
        if isinstance(piece, (dict, list)):
            piece = json.dumps(piece, ensure_ascii=False)
        self.current_func["arguments"] += str(piece)

    def _cache_current_thought(self) -> None:
        if self.current_thought_text and self.current_thought_signature:
            cache_thought_signatures([(
                self.current_thought_text,
                self.current_thought_signature,
            )])
        self.current_thought_text = ""
        self.current_thought_signature = None

    def feed(self, event: dict) -> List[dict]:
        """处理单个 interactions 事件，返回 OpenAI chunk 列表（可能为空）。"""
        if not isinstance(event, dict):
            return []
        event_type = event.get("event_type")
        chunks: List[dict] = []

        if event_type == "interaction.created":
            interaction = event.get("interaction") or {}
            if isinstance(interaction, dict):
                self.status = interaction.get("status", self.status)

        elif event_type in ("interaction.status_update", "interaction.requires_action"):
            interaction = event.get("interaction") or {}
            self.status = event.get("status") or (
                interaction.get("status") if isinstance(interaction, dict) else None
            ) or self.status

        elif event_type == "step.start":
            step = event.get("step") or {}
            if not isinstance(step, dict):
                return []
            self.current_step_type = step.get("type")
            if self.current_step_type == "function_call":
                initial_arguments = step.get("arguments")
                if isinstance(initial_arguments, (dict, list)):
                    initial_arguments = (
                        json.dumps(initial_arguments, ensure_ascii=False)
                        if initial_arguments else ""
                    )
                elif initial_arguments is None:
                    initial_arguments = ""
                self.current_func = {
                    "id": step.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "name": step.get("name", ""),
                    "arguments": str(initial_arguments),
                }
            elif self.current_step_type == "thought":
                self.current_thought_text = _extract_text_from_content_block(
                    step.get("summary"))
                self.current_thought_signature = step.get("signature") or None
                if self.current_thought_text:
                    chunks.append(self._reasoning_chunk(self.current_thought_text))

        elif event_type == "step.delta":
            delta = event.get("delta") or {}
            if not isinstance(delta, dict):
                return []
            delta_type = delta.get("type")
            if delta_type == "text":
                text = delta.get("text", "")
                if text:
                    chunks.append(self._chunk({
                        "choices": [{
                            "index": 0,
                            "delta": {"content": text},
                        }],
                    }))
            elif delta_type in ("thought_summary", "thought"):
                content = delta.get("content")
                text = _extract_text_from_content_block(content)
                if not text:
                    text = str(delta.get("text", "") or "")
                if text:
                    chunks.append(self._reasoning_chunk(text))
                    if self.current_step_type == "thought":
                        self.current_thought_text += text
            elif delta_type == "thought_signature":
                signature = delta.get("signature")
                if signature:
                    self.current_thought_signature = str(signature)
            elif delta_type in ("arguments_delta", "arguments"):
                self._append_argument_delta(delta)
            # image/audio/document/video 与服务器端工具 delta 无法在普通
            # OpenAI 文本流中表达，保留状态并跳过未知内容。

        elif event_type == "step.stop":
            if self.current_step_type == "function_call" and self.current_func:
                args_str = self.current_func["arguments"]
                try:
                    if args_str:
                        parsed = json.loads(args_str)
                        args_str = json.dumps(parsed, ensure_ascii=False)
                except json.JSONDecodeError:
                    pass
                chunks.append(self._chunk({
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "tool_calls": [{
                                "index": 0,
                                "id": self.current_func["id"],
                                "type": "function",
                                "function": {
                                    "name": self.current_func["name"],
                                    "arguments": args_str,
                                },
                            }],
                        },
                    }],
                }))
                self.current_func = None
            elif self.current_step_type == "thought":
                self._cache_current_thought()
            self.current_step_type = None

        elif event_type == "interaction.completed":
            interaction = event.get("interaction") or {}
            if not isinstance(interaction, dict):
                interaction = {}
            self.status = interaction.get("status", self.status)
            self.usage = interaction.get("usage") if isinstance(
                interaction.get("usage"), dict) else None
            self.completed = True
            chunks.append(self._chunk({
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": _interactions_finish_reason(self.status),
                }],
            }))
            usage = self.usage or {}
            prompt_tokens = _usage_int(usage, "total_input_tokens", "prompt_tokens")
            output_tokens = _usage_int(usage, "total_output_tokens", "completion_tokens")
            thought_tokens = _usage_int(usage, "total_thought_tokens", "reasoning_tokens")
            completion_tokens = output_tokens + thought_tokens
            total_tokens = _usage_int(usage, "total_tokens") or (
                prompt_tokens + completion_tokens)
            chunks.append(self._chunk({
                "choices": [],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                },
            }))

        elif event_type == "error":
            error = event.get("error") or {"message": "Unknown interactions error"}
            chunks.append({"error": error})

        return chunks

    def finalize(self) -> List[dict]:
        """在上游没有发送 completed 事件时完成当前思考状态的清理。"""
        if self.current_step_type == "thought":
            self._cache_current_thought()
            self.current_step_type = None
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
    if status == "failed":
        return "OTHER"
    return "STOP"


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
                fc_step: dict = {
                    "type": "function_call",
                    "name": fc.get("name", ""),
                    "arguments": fc.get("args") or fc.get("arguments") or {},
                }
                if fc.get("id"):
                    fc_step["id"] = fc["id"]
                steps.append(fc_step)
                continue

            if "functionResponse" in part or "function_response" in part:
                flush_content_blocks()
                fr = part.get("functionResponse") or part.get("function_response") or {}
                if not isinstance(fr, dict):
                    continue
                result_blocks = _function_response_to_interactions_blocks(fr.get("response"))
                steps.append({
                    "type": "function_result",
                    "name": fr.get("name", "_unknown"),
                    "call_id": fr.get("id") or fr.get("call_id", ""),
                    "result": result_blocks,
                })
                continue

            converted = _gemini_part_to_interactions_block(part)
            if converted:
                content_blocks.append(converted)

        flush_content_blocks()

    body: Dict[str, Any] = {
        "model": model,
        "input": steps,
        "store": False,
    }

    system_instruction = gc_req.get("systemInstruction") if isinstance(gc_req, dict) else None
    if isinstance(system_instruction, dict):
        for part in system_instruction.get("parts", []) or []:
            if isinstance(part, dict) and part.get("text"):
                system_parts.append(str(part["text"]))
    elif isinstance(system_instruction, str) and system_instruction:
        system_parts.append(system_instruction)
    if system_parts:
        body["system_instruction"] = "\n".join(system_parts)

    gen_config: Dict[str, Any] = {}
    gc = gc_req.get("generationConfig") or {}
    if isinstance(gc, dict):
        if gc.get("temperature") is not None:
            gen_config["temperature"] = gc["temperature"]
        if gc.get("maxOutputTokens") is not None:
            gen_config["max_output_tokens"] = gc["maxOutputTokens"]
        if isinstance(gc.get("stopSequences"), list) and gc["stopSequences"]:
            gen_config["stop_sequences"] = gc["stopSequences"]
        thinking = gc.get("thinkingConfig")
        if isinstance(thinking, dict):
            thinking_level = map_thinking_config_to_level(thinking)
            if thinking_level:
                gen_config["thinking_level"] = thinking_level
            thinking_summaries = map_thinking_config_to_summaries(thinking)
            if thinking_summaries:
                gen_config["thinking_summaries"] = thinking_summaries
        response_mime_type = gc.get("responseMimeType")
        response_schema = gc.get("responseSchema")
        if response_mime_type or response_schema:
            response_format: dict = {
                "type": "text",
                "mime_type": response_mime_type or "application/json",
            }
            if response_schema:
                response_format["schema"] = response_schema
            body["response_format"] = response_format
            body["response_mime_type"] = response_format["mime_type"]
    if gen_config:
        body["generation_config"] = gen_config

    # functionDeclarations → interactions function tools
    tools = gc_req.get("tools") or []
    if isinstance(tools, list):
        function_tools: List[dict] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            declarations = tool.get("functionDeclarations") or tool.get("function_declarations") or []
            for decl in declarations:
                if not isinstance(decl, dict) or not decl.get("name"):
                    continue
                function_tools.append({
                    "type": "function",
                    "name": decl["name"],
                    "description": decl.get("description", ""),
                    "parameters": decl.get("parameters") or {},
                })
        if function_tools:
            body["tools"] = function_tools

    tool_config = gc_req.get("toolConfig") or {}
    function_calling_config = (
        tool_config.get("functionCallingConfig")
        if isinstance(tool_config, dict) else None
    )
    if isinstance(function_calling_config, dict):
        mode = str(function_calling_config.get("mode", "AUTO")).upper()
        if mode == "NONE":
            body.pop("tools", None)
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


def convert_interactions_to_gemini_gc(interaction: dict) -> dict:
    """interactions Interaction 对象 → Gemini generateContent 响应。"""
    parts: List[dict] = []
    for step in interaction.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
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
            if step.get("signature"):
                fc["thoughtSignature"] = step["signature"]
            parts.append({"functionCall": fc})

    usage = interaction.get("usage") or {}
    prompt_tokens = _usage_int(usage, "total_input_tokens", "prompt_tokens")
    candidates_tokens = _usage_int(usage, "total_output_tokens", "completion_tokens")
    thoughts_tokens = _usage_int(usage, "total_thought_tokens", "reasoning_tokens")
    total_tokens = _usage_int(usage, "total_tokens") or (
        prompt_tokens + candidates_tokens + thoughts_tokens)

    response: dict = {
        "candidates": [{
            "content": {"role": "model", "parts": parts},
            "finishReason": _interactions_to_gemini_finish_reason(interaction.get("status")),
        }],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": candidates_tokens,
            "thoughtsTokenCount": thoughts_tokens,
            "totalTokenCount": total_tokens,
        },
    }
    response["usage"] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": candidates_tokens + thoughts_tokens,
        "total_tokens": total_tokens,
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
        parts: List[dict] = []
        for block in self.current_thought_blocks:
            part = _interactions_block_to_gemini_part(block, thought=True)
            if part:
                parts.append(part)
        if self.current_thought_signature:
            if parts:
                parts[-1]["thoughtSignature"] = self.current_thought_signature
            else:
                parts.append({"thought": True, "thoughtSignature": self.current_thought_signature})
        self.current_thought_blocks = []
        self.current_thought_signature = None
        return [self._chunk(parts)] if parts else []

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
                self.current_thought_blocks = [
                    dict(block) for block in blocks if isinstance(block, dict)
                ]
                self.current_thought_signature = step.get("signature") or None
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
                if block:
                    self.current_thought_blocks.append(dict(block))
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
            self.current_step_type = None

        elif event_type == "interaction.completed":
            interaction = event.get("interaction") or {}
            if not isinstance(interaction, dict):
                interaction = {}
            self.status = interaction.get("status", self.status)
            self.usage = interaction.get("usage") if isinstance(
                interaction.get("usage"), dict) else None
            self.completed = True
            chunks.append({"candidates": [{
                "finishReason": _interactions_to_gemini_finish_reason(self.status),
            }]})
            usage = self.usage or {}
            prompt_tokens = _usage_int(usage, "total_input_tokens", "prompt_tokens")
            candidates_tokens = _usage_int(usage, "total_output_tokens", "completion_tokens")
            thoughts_tokens = _usage_int(usage, "total_thought_tokens", "reasoning_tokens")
            total_tokens = _usage_int(usage, "total_tokens") or (
                prompt_tokens + candidates_tokens + thoughts_tokens)
            chunks.append({
                "usageMetadata": {
                    "promptTokenCount": prompt_tokens,
                    "candidatesTokenCount": candidates_tokens,
                    "thoughtsTokenCount": thoughts_tokens,
                    "totalTokenCount": total_tokens,
                },
            })

        elif event_type == "error":
            chunks.append({"error": event.get("error") or {"message": "Unknown interactions error"}})

        return chunks
