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
                "summary": {"type": "text", "text": fragment},
            })
            remaining = remaining[len(fragment):]
        elif fragment.startswith(remaining):
            # 客户端文本是当前片段的前缀（尾部被裁剪）
            steps.append({
                "type": "thought",
                "signature": signature,
                "summary": {"type": "text", "text": remaining},
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
    """从 interactions 内容块（TextContent/ImageContent 等）提取文本"""
    if isinstance(block, dict):
        if block.get("type") == "text":
            return str(block.get("text", ""))
        # ImageContent 等媒体块无文本
    return ""


def _oai_part_to_interactions_block(item: Any) -> Optional[dict]:
    """OpenAI content part → interactions 内容块；无法转换返回 None"""
    if isinstance(item, str):
        return {"type": "text", "text": item} if item else None
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")

    if item_type == "text":
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


def _extract_message_text(content: Any) -> str:
    """从 OAI 消息 content（str / list）提取纯文本"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
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
                # dict 内容（如 {"content": "..."}）尝试提取
                text = content.get("content")
                if isinstance(text, str) and text:
                    result_blocks.append({"type": "text", "text": text})

            result_step: dict = {
                "type": "function_result",
                "name": func_name,
                "call_id": tool_call_id,
                "result": {"content": result_blocks or [{"type": "text", "text": ""}]},
            }
            steps.append(result_step)
            continue

        logger.warning(f"[GEMINI_INTERACTIONS] 跳过未知消息角色: {role}")

    return steps, "\n".join(system_parts)


def map_thinking_config_to_level(thinking_config: Optional[dict]) -> Optional[str]:
    """Gemini generateContent 的 thinkingConfig → interactions thinking_level。

    - thinkingLevel → 小写映射（官方示例 "low"）
    - includeThoughts=False → "none"
    - thinkingBudget（Gemini 2.5）在 interactions 无对应字段，忽略并记日志
    """
    if not thinking_config:
        return None
    level = thinking_config.get("thinkingLevel")
    if level:
        return str(level).lower()
    if thinking_config.get("includeThoughts") is False:
        return "none"
    if thinking_config.get("thinkingBudget") is not None:
        logger.debug("[GEMINI_INTERACTIONS] thinkingBudget 在 interactions 无对应字段，已忽略")
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
    response_format: Optional[dict] = None,
    extra_body: Optional[dict] = None,
) -> dict:
    """构建 interactions 请求体（无状态模式 store=false）。

    - tools：OAI function 工具格式与 interactions 完全一致（{type, name, description, parameters}），直接透传
    - tool_choice：仅 "none" 受支持（不传 tools）；其余按默认行为透传 tools
    - response_format.json_object → response_mime_type="application/json"；
      json_schema 的 schema 无法映射（interactions 的 response_format 是多模态数组），仅设 mime_type
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
        # interactions GenerationConfig 未确认支持 top_p，官方 protobuf 会忽略未知字段；
        # 传了无副作用，但保守起见记日志便于排查
        logger.debug("[GEMINI_INTERACTIONS] interactions 未确认支持 top_p，已忽略")
    if max_tokens is not None:
        gen_config["max_output_tokens"] = max_tokens
    thinking_level = map_thinking_config_to_level(thinking_config)
    if thinking_level:
        gen_config["thinking_level"] = thinking_level
    if gen_config:
        body["generation_config"] = gen_config

    # tool_choice="none" 时不带 tools；其余情况透传
    tool_choice_none = tool_choice == "none" or (
        isinstance(tool_choice, dict) and tool_choice.get("type") == "none")
    if tools and not tool_choice_none:
        body["tools"] = tools
        logger.info(f"[GEMINI_INTERACTIONS] 透传 {len(tools)} 个 OAI tools")
    elif tool_choice_none:
        logger.info("[GEMINI_INTERACTIONS] tool_choice=none，不携带 tools")

    if isinstance(response_format, dict):
        rf_type = response_format.get("type")
        if rf_type == "json_object":
            body["response_mime_type"] = "application/json"
        elif rf_type == "json_schema":
            # interactions 的 response_format 是多模态数组，json_schema 无法直接映射
            body["response_mime_type"] = "application/json"
            logger.warning("[GEMINI_INTERACTIONS] json_schema 无法映射为 interactions response_format，仅设置 response_mime_type")

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
    return "stop"


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
            summary = step.get("summary")
            text = _extract_text_from_content_block(summary)
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
        # 其他步骤类型（google_search_call 等服务器端工具）信息有限，跳过

    if thought_fragments:
        cache_thought_signatures(thought_fragments)

    usage = interaction.get("usage") or {}
    prompt_tokens = int(usage.get("total_input_tokens", 0) or 0)
    # 🔧 思考 token 计入输出（对齐 UNFIXED_ISSUES #44 的修复方向）
    completion_tokens = int(usage.get("total_output_tokens", 0) or 0) + int(
        usage.get("total_thought_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", 0) or 0) or (prompt_tokens + completion_tokens)

    message: dict = {"role": "assistant"}
    if content_parts:
        message["content"] = "".join(content_parts)
    else:
        message["content"] = None
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

    用法：对每个事件调用 feed(event)，返回 0 个或多个 OpenAI chunk dict；
    事件流结束时调用 finalize() 获取收尾块（usage + finish_reason）。

    支持的事件：
    - interaction.created / interaction.status_update：忽略
    - step.start / step.delta / step.stop：文本/思考/工具调用
    - interaction.completed：usage + finish_reason
    - error：错误块
    """

    def __init__(self, model: str, request_id: str):
        self.model = model
        self.request_id = request_id
        self.current_step_type: Optional[str] = None
        self.current_func: Optional[dict] = None
        self.current_thought_fragments: List[Tuple[str, str]] = []
        self.usage: Optional[dict] = None
        self.completed: bool = False
        self.status: Optional[str] = None

    def _chunk(self, extra: dict) -> dict:
        chunk = _oai_chunk_base(self.model, self.request_id)
        chunk.update(extra)
        return chunk

    def feed(self, event: dict) -> List[dict]:
        """处理单个 interactions 事件，返回 OpenAI chunk 列表（可能为空）"""
        if not isinstance(event, dict):
            return []
        event_type = event.get("event_type")
        chunks: List[dict] = []

        if event_type == "step.start":
            step = event.get("step") or {}
            self.current_step_type = step.get("type")
            if self.current_step_type == "function_call":
                self.current_func = {
                    "id": step.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "name": step.get("name", ""),
                    "arguments": "",
                }
            elif self.current_step_type == "thought":
                self.current_thought_fragments = []

        elif event_type == "step.delta":
            delta = event.get("delta") or {}
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
            elif delta_type == "thought_summary":
                content = delta.get("content")
                text = _extract_text_from_content_block(content)
                if text:
                    chunks.append(self._chunk({
                        "choices": [{
                            "index": 0,
                            "delta": {"reasoning_content": text},
                        }],
                    }))
                    # 记录片段文本，step.stop 时与签名一起入缓存
                    if self.current_step_type == "thought":
                        self.current_thought_fragments.append((text, ""))
            elif delta_type == "thought_signature":
                signature = delta.get("signature")
                if signature and self.current_thought_fragments:
                    # 签名是 step 最后一个 delta，与当前累积的摘要文本配对
                    self.current_thought_fragments[-1] = (
                        self.current_thought_fragments[-1][0], signature)
            elif delta_type == "arguments_delta":
                if self.current_func:
                    self.current_func["arguments"] += delta.get("arguments", "")
            # image/audio/document/video 等媒体 delta 无法在 OAI 文本流表达，跳过

        elif event_type == "step.stop":
            if self.current_step_type == "function_call" and self.current_func:
                args_str = self.current_func["arguments"]
                # 尝试规范化参数 JSON；不完整时原样透传
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
                valid = [(t, s) for t, s in self.current_thought_fragments if t and s]
                if valid:
                    cache_thought_signatures(valid)
                self.current_thought_fragments = []
            self.current_step_type = None

        elif event_type == "interaction.completed":
            interaction = event.get("interaction") or {}
            self.status = interaction.get("status")
            self.usage = interaction.get("usage") if isinstance(interaction.get("usage"), dict) else None
            self.completed = True
            finish_reason = _interactions_finish_reason(self.status)
            chunks.append(self._chunk({
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": finish_reason,
                }],
            }))
            usage = self.usage or {}
            prompt_tokens = int(usage.get("total_input_tokens", 0) or 0)
            completion_tokens = int(usage.get("total_output_tokens", 0) or 0) + int(
                usage.get("total_thought_tokens", 0) or 0)
            total_tokens = int(usage.get("total_tokens", 0) or 0) or (prompt_tokens + completion_tokens)
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


def convert_gemini_gc_to_interactions(gc_req: dict, model: str) -> dict:
    """Gemini generateContent 请求体 → interactions 请求体（无状态模式）。

    - contents → input steps（user → user_input；model → model_output；
      functionCall / functionResponse part 拆为独立 step）
    - systemInstruction → system_instruction（文本拼接）
    - generationConfig → generation_config（temperature / max_output_tokens / thinking_level）
    - tools（functionDeclarations）→ interactions function tools
    """
    steps: List[dict] = []
    system_parts: List[str] = []

    for content in gc_req.get("contents", []) or []:
        if not isinstance(content, dict):
            continue
        role = content.get("role")
        parts = content.get("parts", []) or []
        text_blocks: List[dict] = []

        def _flush_text_blocks():
            """把已累积的文本块落为一个 step（保持原始 parts 顺序）"""
            nonlocal text_blocks
            if text_blocks:
                if role == "model":
                    steps.append({"type": "model_output", "content": text_blocks})
                else:
                    steps.append({"type": "user_input", "content": text_blocks})
                text_blocks = []

        for part in parts:
            if not isinstance(part, dict):
                continue
            if "text" in part:
                text_blocks.append({"type": "text", "text": part["text"]})
            elif "inline_data" in part:
                inline = part["inline_data"]
                mime_type = inline.get("mimeType", "")
                data = inline.get("data", "")
                if data:
                    text_blocks.append({
                        "type": _mime_to_content_type(mime_type),
                        "data": data,
                        "mime_type": mime_type,
                    })
            elif "fileData" in part:
                file_data = part["fileData"]
                mime_type = file_data.get("mimeType", "")
                uri = file_data.get("fileUri", "")
                if uri:
                    text_blocks.append({
                        "type": _mime_to_content_type(mime_type),
                        "uri": uri,
                        "mime_type": mime_type,
                    })
            elif "functionCall" in part:
                # 先落文本块，再落函数调用步骤（保持 parts 顺序）
                _flush_text_blocks()
                fc = part["functionCall"]
                fc_step: dict = {
                    "type": "function_call",
                    "name": fc.get("name", ""),
                    "arguments": fc.get("args") or {},
                }
                if fc.get("id"):
                    fc_step["id"] = fc["id"]
                steps.append(fc_step)
            elif "functionResponse" in part:
                _flush_text_blocks()
                fr = part["functionResponse"]
                response = fr.get("response") or {}
                result_blocks: List[dict] = []
                if isinstance(response, dict):
                    inner = response.get("content")
                    if isinstance(inner, str) and inner:
                        result_blocks.append({"type": "text", "text": inner})
                steps.append({
                    "type": "function_result",
                    "name": fr.get("name", "_unknown"),
                    "call_id": fr.get("id", ""),
                    "result": {"content": result_blocks or [{"type": "text", "text": ""}]},
                })

        _flush_text_blocks()

    body: Dict[str, Any] = {
        "model": model,
        "input": steps,
        "store": False,
    }

    system_instruction = gc_req.get("systemInstruction")
    if isinstance(system_instruction, dict):
        for part in system_instruction.get("parts", []) or []:
            if isinstance(part, dict) and part.get("text"):
                system_parts.append(part["text"])
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
        thinking = gc.get("thinkingConfig")
        if isinstance(thinking, dict):
            thinking_level = map_thinking_config_to_level(thinking)
            if thinking_level:
                gen_config["thinking_level"] = thinking_level
    if gen_config:
        body["generation_config"] = gen_config

    # functionDeclarations → interactions function tools
    tools = gc_req.get("tools") or []
    if isinstance(tools, list):
        function_tools: List[dict] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            for decl in tool.get("functionDeclarations", []) or []:
                if not isinstance(decl, dict):
                    continue
                function_tools.append({
                    "type": "function",
                    "name": decl.get("name", ""),
                    "description": decl.get("description", ""),
                    "parameters": decl.get("parameters") or {},
                })
        if function_tools:
            body["tools"] = function_tools

    # toolConfig 在 interactions 无直接对应字段（allowed_tools 语义不同），忽略并提示
    if gc_req.get("toolConfig"):
        logger.debug("[GEMINI_INTERACTIONS] toolConfig 无法映射到 interactions，已忽略")

    return body


def convert_interactions_to_gemini_gc(interaction: dict) -> dict:
    """interactions Interaction 对象 → Gemini generateContent 响应。

    - model_output → candidates[].content.parts 文本块
    - function_call → parts 中 functionCall 块
    - thought → 忽略（旧格式客户端无签名概念，不注入思考）
    - usage → usageMetadata（promptTokenCount / candidatesTokenCount / thoughtsTokenCount）
    """
    parts: List[dict] = []
    for step in interaction.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        stype = step.get("type")
        if stype == "model_output":
            for block in step.get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    parts.append({"text": block["text"]})
        elif stype == "function_call":
            fc: dict = {
                "name": step.get("name", ""),
                "args": step.get("arguments") or {},
            }
            if step.get("id"):
                fc["id"] = step["id"]
            parts.append({"functionCall": fc})

    usage = interaction.get("usage") or {}
    prompt_tokens = int(usage.get("total_input_tokens", 0) or 0)
    candidates_tokens = int(usage.get("total_output_tokens", 0) or 0)
    thoughts_tokens = int(usage.get("total_thought_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", 0) or 0) or (
        prompt_tokens + candidates_tokens + thoughts_tokens)

    response: dict = {
        "candidates": [{
            "content": {"role": "model", "parts": parts},
            "finishReason": _interactions_finish_reason(interaction.get("status")),
        }],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": candidates_tokens,
            "thoughtsTokenCount": thoughts_tokens,
            "totalTokenCount": total_tokens,
        },
    }
    # OpenAI 兼容 usage（与 gemini_v1beta_api 现有注入行为一致）
    response["usage"] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": candidates_tokens + thoughts_tokens,
        "total_tokens": total_tokens,
    }
    return response


class InteractionsToGeminiGCConverter:
    """interactions 流式事件 → Gemini generateContent SSE chunk 流（状态机）。

    与 InteractionsStreamConverter 类似，但输出旧版 generateContent 格式：
    - step.delta(text) → candidates[].content.parts[].text
    - function_call step.stop → parts 中 functionCall 块
    - interaction.completed → finishReason + usageMetadata 收尾块
    """

    def __init__(self):
        self.current_step_type: Optional[str] = None
        self.current_func: Optional[dict] = None
        self.completed: bool = False
        self.status: Optional[str] = None
        self.usage: Optional[dict] = None

    def feed(self, event: dict) -> List[dict]:
        if not isinstance(event, dict):
            return []
        event_type = event.get("event_type")
        chunks: List[dict] = []

        if event_type == "step.start":
            step = event.get("step") or {}
            self.current_step_type = step.get("type")
            if self.current_step_type == "function_call":
                self.current_func = {
                    "name": step.get("name", ""),
                    "arguments": "",
                    "id": step.get("id"),
                }

        elif event_type == "step.delta":
            delta = event.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text":
                text = delta.get("text", "")
                if text:
                    chunks.append({
                        "candidates": [{
                            "content": {"role": "model", "parts": [{"text": text}]},
                        }],
                    })
            elif delta_type == "arguments_delta":
                if self.current_func:
                    self.current_func["arguments"] += delta.get("arguments", "")
            # thought_summary / thought_signature 等思考内容旧格式不表达，跳过

        elif event_type == "step.stop":
            if self.current_step_type == "function_call" and self.current_func:
                args_str = self.current_func["arguments"]
                try:
                    if args_str:
                        parsed = json.loads(args_str)
                        args_str = parsed
                except json.JSONDecodeError:
                    args_str = self.current_func["arguments"] or {}
                fc: dict = {
                    "name": self.current_func["name"],
                    "args": args_str if isinstance(args_str, dict) else {},
                }
                if self.current_func.get("id"):
                    fc["id"] = self.current_func["id"]
                chunks.append({
                    "candidates": [{
                        "content": {"role": "model", "parts": [{"functionCall": fc}]},
                    }],
                })
                self.current_func = None
            self.current_step_type = None

        elif event_type == "interaction.completed":
            interaction = event.get("interaction") or {}
            self.status = interaction.get("status")
            self.usage = interaction.get("usage") if isinstance(interaction.get("usage"), dict) else None
            self.completed = True
            chunks.append({
                "candidates": [{
                    "finishReason": _interactions_finish_reason(self.status),
                }],
            })
            usage = self.usage or {}
            prompt_tokens = int(usage.get("total_input_tokens", 0) or 0)
            candidates_tokens = int(usage.get("total_output_tokens", 0) or 0)
            thoughts_tokens = int(usage.get("total_thought_tokens", 0) or 0)
            total_tokens = int(usage.get("total_tokens", 0) or 0) or (
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
