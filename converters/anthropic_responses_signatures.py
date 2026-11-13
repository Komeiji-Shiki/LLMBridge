"""在 Anthropic 与 Responses、Interactions 之间携带各自的原始思考块。"""

import base64
import json
from typing import Any, Dict, List, Optional, Tuple

from core.conversion_report import note_loss


_PREFIXES = {"anthropic": "anthropic.v1.", "responses": "responses.v1.", "interactions": "interactions.v1."}
_ITEM_TYPES = {"anthropic": ("thinking", "redacted_thinking"), "responses": ("reasoning",),
               "interactions": ("thought",)}


def thinking_carrier(value: Any) -> Optional[str]:
    """返回本网关思考载体所属的协议；不是本网关签发的返回 None。"""
    if isinstance(value, str):
        for protocol, prefix in _PREFIXES.items():
            if value.startswith(prefix):
                return protocol
    return None


def encode_thinking_item(protocol: str, item: Dict[str, Any]) -> str:
    """编码原始思考项，并明确标记签发它的协议。"""
    payload = json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _PREFIXES[protocol] + base64.urlsafe_b64encode(payload).decode("ascii")


def decode_thinking_item(protocol: str, value: Any) -> Optional[Dict[str, Any]]:
    """只解码本协议的载体；格式错误时明确拒绝。"""
    prefix = _PREFIXES[protocol]
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    try:
        payload = base64.b64decode(value[len(prefix):], altchars=b"-_", validate=True)
        item = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"无效的 {protocol} 思考状态载体") from exc
    if not isinstance(item, dict) or item.get("type") not in _ITEM_TYPES[protocol]:
        raise ValueError(f"无效的 {protocol} 思考状态类型")
    return item


def responses_reasoning_text(item: Dict[str, Any]) -> str:
    return "".join(
        part.get("text", "") for part in item.get("summary", [])
        if isinstance(part, dict) and part.get("type") in ("summary_text", "text")
    )


def responses_segments(message: Dict[str, Any]) -> Optional[List[Tuple[str, Any]]]:
    """把完整 Responses 输出按原有顺序展开为 Anthropic 内容片段。"""
    metadata = message.get("provider_metadata") or {}
    output = metadata.get("responses_output") if isinstance(metadata, dict) else None
    if not isinstance(output, list):
        return None
    segments: List[Tuple[str, Any]] = []
    for item in output:
        kind = item.get("type")
        if kind == "reasoning":
            text = responses_reasoning_text(item)
            if text:
                segments.append(("thinking", text))
            segments.append(("signature", encode_thinking_item("responses", item)))
        elif kind == "message":
            for part in item.get("content", []):
                if part.get("type") in ("output_text", "text") and part.get("text"):
                    segments.append(("text", part["text"]))
                elif part.get("type") == "refusal" and part.get("refusal"):
                    segments.append(("text", part["refusal"]))
        elif kind == "function_call":
            segments.append(("tool_calls", [{
                "id": item["call_id"],
                "type": "function",
                "function": {"name": item["name"], "arguments": item["arguments"]},
            }]))
        else:
            # 托管工具由上游执行完了，结果已体现在后面的正文里；Anthropic 没有对应的块。
            note_loss(f"output.{kind}", f"Responses 输出项 {kind} 没有对应的 Anthropic 内容块，已跳过")
    return segments


def responses_items_from_anthropic(blocks: Any) -> Optional[List[Dict[str, Any]]]:
    """从客户端回传的签名和内容块重建 Responses 助手输出。"""
    if not isinstance(blocks, list):
        return None
    if not any(block.get("type") == "thinking" and thinking_carrier(block.get("signature")) == "responses"
               for block in blocks if isinstance(block, dict)):
        return None
    items: List[Dict[str, Any]] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "thinking":
            item = decode_thinking_item("responses", block.get("signature"))
            if item is None or responses_reasoning_text(item) != block.get("thinking", ""):
                raise ValueError("回传的 Responses 思考内容或签名已改变")
            items.append(item)
        elif kind == "text":
            items.append({"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": block.get("text", "")}]})
        elif kind == "tool_use":
            items.append({"type": "function_call", "call_id": block["id"],
                          "name": block["name"],
                          "arguments": json.dumps(block["input"], ensure_ascii=False, separators=(",", ":"))})
    return items
