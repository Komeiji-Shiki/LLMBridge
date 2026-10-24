"""Gemini 思考签名经 Anthropic thinking.signature 往返。

Anthropic 客户端（Claude Code 等）会原样保存并回传 thinking 块的 signature。
把 Gemini 的 thoughtSignature 编码进去，下一轮即可还原到签发它的内容块上，
不依赖服务端缓存。编码前缀标明签名归属（思考 / 正文 / 函数调用）；
没有前缀的签名（如 Claude 自己签发的）回传时一律忽略。
"""
from typing import Any, Dict, List, Optional, Tuple

PREFIX = "gemini.v1."
THOUGHT, TEXT, CALL = "thought", "text", "call"


def encode_signature(target: str, signature: str) -> str:
    return f"{PREFIX}{target}.{signature}"


def decode_signature(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """返回 (归属, 原始签名)；不是本网关签发的签名返回 (None, None)。"""
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return None, None
    target, _, signature = value[len(PREFIX):].partition(".")
    if target in (THOUGHT, TEXT, CALL) and signature:
        return target, signature
    return None, None


def _google_signature(value: Dict[str, Any]) -> Optional[str]:
    extra = value.get("extra_content") or {}
    return (extra.get("google") or {}).get("thought_signature")


def restore_google_tool_signatures(message: Dict[str, Any]) -> Dict[str, Any]:
    """把 Anthropic 回传或旧原生历史的调用签名恢复为 Google OAI 扩展字段。"""
    metadata = message.get("provider_metadata") or {}
    parts = metadata.get("gemini_parts") or []
    call_parts = [part for part in parts if isinstance(part, dict) and "functionCall" in part]
    calls = []
    for index, call in enumerate(message.get("tool_calls") or []):
        signature = call.get("_thought_signature")
        if index < len(call_parts):
            signature = call_parts[index].get("thoughtSignature") or signature
        call = {key: value for key, value in call.items() if key != "_thought_signature"}
        if signature and not _google_signature(call):
            extra = call.get("extra_content") or {}
            call = {**call, "extra_content": {
                **extra, "google": {**(extra.get("google") or {}), "thought_signature": signature}}}
        calls.append(call)
    return {**message, "tool_calls": calls} if calls else message


def gemini_segments(delta: Dict[str, Any]) -> Optional[List[Tuple[str, Any]]]:
    """按 Gemini 原生 parts 顺序把 OpenAI delta/message 展开为片段序列。

    片段类型：thinking / text / tool_calls / signature（已编码）。同一块里
    思考、正文、调用的先后与每个签名的归属都按原生顺序保留。不是 Gemini
    链路的 delta（没有原生 parts 或 Google 签名）返回 None。
    """
    metadata = delta.get("provider_metadata")
    parts = metadata.get("gemini_parts") if isinstance(metadata, dict) else None
    if not isinstance(parts, list):
        # Google OAI 将签名附在原工具调用上，平行调用不能共用一个签名。
        calls = delta.get("tool_calls") or []
        if not any(_google_signature(call) for call in calls):
            return None
        segments = []
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            segments.append(("thinking", reasoning))
        if delta.get("content"):
            segments.append(("text", delta["content"]))
        for call in calls:
            signature = _google_signature(call)
            if signature:
                segments.append(("signature", encode_signature(CALL, signature)))
            segments.append(("tool_calls", [call]))
        return segments
    # tool_calls 由 parts 中的 functionCall 按序转换而来（id 可能是转换时补的）
    calls = iter(delta.get("tool_calls") or [])
    segments: List[Tuple[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        signature = part.get("thoughtSignature")
        if "functionCall" in part:
            if signature:
                segments.append(("signature", encode_signature(CALL, signature)))
            segments.append(("tool_calls", [next(calls)]))
        elif isinstance(part.get("text"), str):
            thought = bool(part.get("thought"))
            if part["text"]:
                segments.append(("thinking" if thought else "text", part["text"]))
            if signature:
                segments.append(("signature", encode_signature(THOUGHT if thought else TEXT, signature)))
    return segments


def gemini_parts_from_anthropic(blocks: Any) -> Optional[List[Dict[str, Any]]]:
    """把带本网关签名的 Anthropic assistant 内容还原为 Gemini 模型轮次 parts。

    一条 assistant 消息来自同一次上游响应，只要其中有本网关签名，其余
    thinking 块也是该响应的思考。没有本网关签名时返回 None，交给普通转换。
    """
    if not isinstance(blocks, list) or not any(
            isinstance(block, dict) and block.get("type") == "thinking"
            and decode_signature(block.get("signature"))[0] for block in blocks):
        return None
    parts: List[Dict[str, Any]] = []
    call_signature = None
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "thinking":
            target, signature = decode_signature(block.get("signature"))
            text = block.get("thinking") or ""
            if text or target == THOUGHT:
                part = {"text": text, "thought": True}
                if target == THOUGHT:
                    part["thoughtSignature"] = signature
                parts.append(part)
            if target == CALL:
                call_signature = signature
            elif target == TEXT:
                last = parts[-1] if parts else {}
                if "text" in last and not last.get("thought") and not last.get("thoughtSignature"):
                    last["thoughtSignature"] = signature
                else:
                    parts.append({"text": "", "thoughtSignature": signature})
        elif kind == "text":
            parts.append({"text": block.get("text") or ""})
        elif kind == "tool_use":
            call = {"name": block.get("name", ""), "args": block.get("input") or {}}
            if block.get("id"):
                call["id"] = block["id"]
            part = {"functionCall": call}
            if call_signature:
                part["thoughtSignature"] = call_signature
                call_signature = None
            parts.append(part)
    return parts
