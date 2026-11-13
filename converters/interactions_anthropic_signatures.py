"""Gemini Interactions 的 thought 步骤经 Anthropic thinking.signature 往返。

无状态模式下 thought 必须带签名原样回传。每个带签名的 thought 步骤整步编码进
对应 thinking 块的 signature，Claude Code 等客户端原样保存回传，下一轮按块顺序
还原整轮步骤，不依赖会话库；会话库只留给丢掉签名的 Chat 客户端。
"""
from typing import Any, Dict, List, Optional, Tuple

from converters.anthropic_responses_signatures import decode_thinking_item, encode_thinking_item, thinking_carrier
from converters.gemini_interactions import code_step_text, thought_summary_text
from converters.interactions_thought_strip import strip_summarizer_leaked_thoughts
from core.conversion_report import note_loss


def interactions_signature_segments(delta: Dict[str, Any]) -> Optional[List[Tuple[str, Any]]]:
    """流式：thought 步骤结束时下发的签名片段；其他 delta 返回 None。"""
    metadata = delta.get("provider_metadata")
    step = metadata.get("interactions_thought") if isinstance(metadata, dict) else None
    if not isinstance(step, dict):
        return None
    return [("signature", encode_thinking_item("interactions", step))]


def interactions_segments(message: Dict[str, Any]) -> Optional[List[Tuple[str, Any]]]:
    """非流式：按整轮步骤顺序展开，块的划分与流式一致。"""
    metadata = message.get("provider_metadata")
    steps = metadata.get("interactions_steps") if isinstance(metadata, dict) else None
    if not isinstance(steps, list):
        return None
    # tool_calls 由 function_call 步骤按序转换而来（id 可能是转换时补的）
    calls = iter(message.get("tool_calls") or [])
    segments: List[Tuple[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        kind = step.get("type")
        if kind == "thought":
            text = strip_summarizer_leaked_thoughts(thought_summary_text(step))
            if text:
                segments.append(("thinking", text))
            if step.get("signature"):
                segments.append(("signature", encode_thinking_item("interactions", step)))
        elif kind == "model_output":
            text = "".join(str(block.get("text", "")) for block in step.get("content") or []
                           if isinstance(block, dict) and block.get("type") == "text")
            if text:
                segments.append(("text", text))
        elif kind == "function_call":
            segments.append(("tool_calls", [next(calls)]))
        elif code_step_text(step):
            segments.append(("text", code_step_text(step)))
    return segments


def interactions_steps_from_anthropic(blocks: Any) -> Optional[List[Dict[str, Any]]]:
    """从客户端回传的签名和内容块重建 Interactions 整轮步骤；没有本网关签名返回 None。"""
    if not isinstance(blocks, list) or not any(
            isinstance(block, dict) and block.get("type") == "thinking"
            and thinking_carrier(block.get("signature")) == "interactions" for block in blocks):
        return None
    steps: List[Dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "thinking":
            step = decode_thinking_item("interactions", block.get("signature"))
            if step is None:
                # 无状态模式不能发送未签名的思考
                continue
            if strip_summarizer_leaked_thoughts(thought_summary_text(step)) != block.get("thinking", ""):
                note_loss("thinking", "回传的 Interactions 思考内容已改变，这段思考不再带签名回放")
                continue
            steps.append(step)
        elif kind == "text":
            steps.append({"type": "model_output", "content": [{"type": "text", "text": block.get("text", "")}]})
        elif kind == "tool_use":
            steps.append({"type": "function_call", "id": block.get("id"), "name": block.get("name", ""),
                          "arguments": block.get("input") or {}})
    return steps
