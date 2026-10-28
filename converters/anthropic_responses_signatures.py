"""在 Anthropic 与 Responses 之间携带各自的原始思考块。"""

import base64
import json
from typing import Any, Dict, List, Optional, Tuple


_PREFIXES = {"anthropic": "anthropic.v1.", "responses": "responses.v1."}


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
    expected = ("thinking", "redacted_thinking") if protocol == "anthropic" else ("reasoning",)
    if not isinstance(item, dict) or item.get("type") not in expected:
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
        elif kind == "function_call":
            segments.append(("tool_calls", [{
                "id": item["call_id"],
                "type": "function",
                "function": {"name": item["name"], "arguments": item["arguments"]},
            }]))
        else:
            raise ValueError(f"Responses 输出项类型 '{kind}' 无法转换为 Anthropic 内容块")
    return segments


def responses_items_from_anthropic(blocks: Any) -> Optional[List[Dict[str, Any]]]:
    """从客户端回传的签名和内容块重建 Responses 助手输出。"""
    if not isinstance(blocks, list):
        return None
    if not any(block.get("type") == "thinking" and
               isinstance(block.get("signature"), str) and
               block["signature"].startswith(_PREFIXES["responses"])
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
