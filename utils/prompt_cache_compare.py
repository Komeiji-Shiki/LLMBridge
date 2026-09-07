"""Prompt-cache regression analysis shared by the CLI script and the monitor UI.

This module holds the pure comparison logic (no FastAPI / I/O dependencies)
so both ``scripts/analyze_prompt_cache.py`` and the ``/api/monitor/compare``
route consume the same implementation. All public helpers accept plain
request-detail dicts as returned by ``monitoring_service.get_request_details``
and return JSON-serializable dicts.

TPS terminology used here and in the monitor detail view:

- ``decode_tps``: ``output_tokens / output_s`` where ``output_s`` comes from
  ``timings.output_ms`` (first business event -> finish). For streaming
  responses this is the real decode speed.
- ``e2e_tps``: ``output_tokens / duration_s`` where ``duration_s`` is the
  wall-clock request duration. Always available when tokens and duration
  are recorded.
- ``ttft_s``: ``timings.first_business_ms / 1000`` (time to first token).
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable, Optional

# Fields that always differ between two runs and say nothing about whether
# the shared prompt prefix survived. Everything else at the top level of a
# request log is considered cache-relevant (model, system_fingerprint,
# temperature/top_p/thinking budget, tools metadata, ...).
VOLATILE_FIELDS = frozenset({
    "type", "timestamp", "end_timestamp", "request_id", "status", "success",
    "duration", "error", "messages_count", "input_tokens", "output_tokens",
    "cached_tokens", "response_content", "response_tool_calls", "reasoning_content",
    "cost_info", "upstream_usage", "streaming",
    # Performance measurements / derived values: differ on every run.
    "timings", "total_tokens",
    "cached_cost", "input_cost", "output_cost", "total_cost", "currency",
    "stop_reason", "pricing_snapshot",
    "request_messages_preview", "response_preview", "reasoning_preview",
    # Timestamps and per-request unique IDs.
    "created_at", "date", "gateway_request_id",
    # Rebuilt in-memory view, not a native log field (see
    # MonitoringService._ensure_request_params_for_view); the flattened
    # originals are still compared individually.
    "request_params",
})

# Attribution fields: they do not change the prompt bytes, but cache
# namespaces are usually isolated per session/caller, so a mismatch here
# means the two requests may never have shared cache in the first place.
# Compared separately in compare_identity(), not in compare_params().
IDENTITY_FIELDS = ("conversation_id", "session_id", "user_id", "caller_id", "caller_name")

#: Radius (chars) of the context window shown around the first difference.
CONTEXT_RADIUS = 140

#: preview length for differing parameter values in compare responses.
PARAM_VALUE_PREVIEW = 500


def canonical(value: Any) -> str:
    """Canonical JSON serialization used for byte-level comparisons."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def short_hash(value: Any) -> str:
    """First 16 hex chars of the sha256 over the canonical serialization."""
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()[:16]


def cached_tokens(log: dict[str, Any]) -> int:
    """Cached-token count, falling back to upstream usage details."""
    direct = log.get("cached_tokens")
    if isinstance(direct, int):
        return direct
    usage = log.get("upstream_usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    value = details.get("cached_tokens")
    return value if isinstance(value, int) else 0


def first_text_difference(left: str, right: str) -> Optional[int]:
    """Char offset of the first difference between two strings, if any."""
    for index, (char_a, char_b) in enumerate(zip(left, right)):
        if char_a != char_b:
            return index
    return min(len(left), len(right)) if len(left) != len(right) else None


def context_snippet(text: str, index: int, radius: int = CONTEXT_RADIUS) -> str:
    """Readable window around a difference offset (newlines escaped)."""
    return text[max(0, index - radius): index + radius].replace("\n", "\\n")


def short_repr(value: Any, limit: int = PARAM_VALUE_PREVIEW) -> str:
    """Truncated ``repr`` for parameter diff display (never explodes JSON)."""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + f"... (truncated, {len(text)} chars)"


def gcd_nonzero(values: Iterable[int]) -> int:
    """GCD over non-zero values (0 when every value is 0)."""
    result = 0
    for value in values:
        if value:
            result = math.gcd(result, abs(value))
    return result


def hit_rate(log: dict[str, Any]) -> float:
    """Cached / input ratio in percent (0 when input is unknown)."""
    prompt = int(log.get("input_tokens") or 0)
    if not prompt:
        return 0.0
    return cached_tokens(log) / prompt * 100.0


def cache_summary(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Side-by-side cache numbers for the two logs."""
    old_input = int(old.get("input_tokens") or 0)
    new_input = int(new.get("input_tokens") or 0)
    old_cached = cached_tokens(old)
    new_cached = cached_tokens(new)
    values = [old_cached, new_cached]
    old_start = float(old.get("timestamp") or 0)
    old_end = float(old.get("end_timestamp") or old_start)
    new_start = float(new.get("timestamp") or 0)
    return {
        "old": {
            "request_id": old.get("request_id"),
            "model": old.get("model"),
            "timestamp": old.get("timestamp"),
            "input_tokens": old_input,
            "cached_tokens": old_cached,
            "hit_rate": round(old_cached / old_input * 100, 2) if old_input else 0.0,
        },
        "new": {
            "request_id": new.get("request_id"),
            "model": new.get("model"),
            "timestamp": new.get("timestamp"),
            "input_tokens": new_input,
            "cached_tokens": new_cached,
            "hit_rate": round(new_cached / new_input * 100, 2) if new_input else 0.0,
        },
        "cached_delta": new_cached - old_cached,
        "cache_gcd": gcd_nonzero(values),
        "both_divisible_by_128": all(value % 128 == 0 for value in values),
        "start_to_start_gap_s": round(new_start - old_start, 3) if old_start and new_start else None,
        "prev_end_to_new_start_gap_s": round(new_start - old_end, 3) if old_start and new_start else None,
    }


def _message_role(message: Any) -> Any:
    return message.get("role") if isinstance(message, dict) else None


def _message_name(message: Any) -> Any:
    return message.get("name") if isinstance(message, dict) else None


def compare_messages(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Compare request message histories message-by-message.

    Prompt caches are prefix-sensitive: the first differing message is the
    candidate break point, everything after it cannot hit cache even when
    identical.
    """
    old_messages = old.get("request_messages") or []
    new_messages = new.get("request_messages") or []
    common = min(len(old_messages), len(new_messages))
    first_difference: Optional[dict[str, Any]] = None

    for index in range(common):
        old_text = canonical(old_messages[index])
        new_text = canonical(new_messages[index])
        if old_text != new_text:
            offset = first_text_difference(old_text, new_text)
            first_difference = {
                "index": index,
                "old_role": _message_role(old_messages[index]),
                "new_role": _message_role(new_messages[index]),
                "old_sha256": short_hash(old_messages[index]),
                "new_sha256": short_hash(new_messages[index]),
                "canonical_char_offset": offset,
                "old_context": context_snippet(old_text, offset) if offset is not None else None,
                "new_context": context_snippet(new_text, offset) if offset is not None else None,
            }
            break

    strict_append = (
        len(new_messages) >= len(old_messages)
        and old_messages == new_messages[:len(old_messages)]
    )
    appended = []
    if strict_append:
        for index, message in enumerate(new_messages[len(old_messages):], len(old_messages)):
            appended.append({
                "index": index,
                "role": _message_role(message),
                "name": _message_name(message),
                "chars": len(canonical(message)),
                "sha256": short_hash(message),
            })

    return {
        "old_count": len(old_messages),
        "new_count": len(new_messages),
        "common_count": common,
        "common_identical": first_difference is None,
        "first_difference": first_difference,
        "strict_append": strict_append,
        "appended": appended,
    }


def compare_tools(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Compare tool definitions.

    ``status`` is ``identical`` / ``different`` / ``unknown``. ``unknown``
    means neither log records tool definitions (responses-native passthrough
    logs only keep counts/hashes, older logs keep nothing), so a tool change
    cannot be ruled out as the cache-break cause.
    """
    old_tools = old.get("tools")
    new_tools = new.get("tools")
    if old_tools is not None or new_tools is not None:
        old_list = old_tools or []
        new_list = new_tools or []
        same = canonical(old_list) == canonical(new_list)
        result: dict[str, Any] = {
            "status": "identical" if same else "different",
            "source": "tools",
            "old_count": len(old_list),
            "new_count": len(new_list),
            "old_sha256": short_hash(old_list),
            "new_sha256": short_hash(new_list),
        }
        if not same:
            for index, (left, right) in enumerate(zip(old_list, new_list)):
                if canonical(left) != canonical(right):
                    left_fn = left.get("function", {}) if isinstance(left, dict) else {}
                    right_fn = right.get("function", {}) if isinstance(right, dict) else {}
                    result["first_difference"] = {
                        "index": index,
                        "old_name": left_fn.get("name") if isinstance(left_fn, dict) else None,
                        "new_name": right_fn.get("name") if isinstance(right_fn, dict) else None,
                    }
                    break
        return result

    old_sha = old.get("tools_sha256")
    new_sha = new.get("tools_sha256")
    if old_sha is None or new_sha is None:
        return {
            "status": "unknown",
            "source": "none",
            "old_count": old.get("tools_count"),
            "new_count": new.get("tools_count"),
        }
    old_names = old.get("tools_names") or []
    new_names = new.get("tools_names") or []
    return {
        "status": "identical" if old_sha == new_sha else "different",
        "source": "tools_sha256",
        "old_count": old.get("tools_count"),
        "new_count": new.get("tools_count"),
        "old_sha256": old_sha,
        "new_sha256": new_sha,
        "removed": sorted(set(old_names) - set(new_names))[:20],
        "added": sorted(set(new_names) - set(old_names))[:20],
        "same_name_set": set(old_names) == set(new_names),
    }


def compare_params(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Compare all non-volatile top-level fields except messages/tools/identity."""
    keys = sorted((set(old) | set(new)) - VOLATILE_FIELDS - {"request_messages", "tools"} - set(IDENTITY_FIELDS))
    differences = []
    for key in keys:
        if canonical(old.get(key)) != canonical(new.get(key)):
            differences.append({
                "key": key,
                "old": short_repr(old.get(key)),
                "new": short_repr(new.get(key)),
            })
    return {"same": not differences, "differences": differences}


def compare_identity(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Compare attribution fields (session/caller) separately from prompt bytes."""
    fields = []
    for key in IDENTITY_FIELDS:
        old_value, new_value = old.get(key), new.get(key)
        if old_value is None and new_value is None:
            continue
        fields.append({
            "key": key,
            "old": short_repr(old_value),
            "new": short_repr(new_value),
            "same": canonical(old_value) == canonical(new_value),
        })
    return {"same": all(field["same"] for field in fields), "fields": fields}


def infer(messages: dict[str, Any], tools: dict[str, Any],
          params: dict[str, Any], identity: dict[str, Any],
          old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Human-readable conclusions about the cache discontinuity."""
    old_cached = cached_tokens(old)
    new_cached = cached_tokens(new)
    conclusions: list[str] = []
    tools_status = tools.get("status")
    if not identity.get("same", True):
        differing = [f["key"] for f in identity.get("fields", []) if not f["same"]]
        conclusions.append(
            f"两条请求的归属不同（{', '.join(differing)}），缓存空间可能本来就不共享，跨请求命中本就难以期待。"
        )
    if tools_status == "unknown":
        conclusions.append("工具定义未被记录：不能排除工具变化导致缓存断裂。")
    if new_cached < old_cached:
        if messages.get("strict_append") and tools_status is not False and params.get("same"):
            conclusions.append("记录的消息历史没有破坏共享 prompt 前缀。")
            if tools_status == "unknown":
                conclusions.append("但工具定义未记录：已记录消息之外的上游工具变化仍可能是原因。")
            conclusions.append("缓存断裂发生在模型生成之前、上游侧，而非已记录的消息历史内部。")
            conclusions.append("最可能的原因：缓存分片/后端重分配、部分缓存淘汰或提供商侧缓存过期。")
            conclusions.append("非零残留前缀意味着更早的共享前缀块仍在，而靠后的会话相关块已不在。")
            conclusions.append("仅凭日志无法区分分片重分配与淘汰，因为没有记录缓存节点/分片/指纹。")
        else:
            if messages.get("common_identical") and messages.get("first_difference") is None:
                conclusions.append("公共消息前缀逐字节一致，缓存下降来自请求长度、归属或上游侧因素，而非消息内容被改动。")
            else:
                conclusions.append("缓存下降的同时请求内容也发生了变化，上方第一个差异点即为候选断裂位置。")
    else:
        conclusions.append("两条日志之间未检测到缓存回退。")
    return conclusions


def compare(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Full comparison of two request logs (old = earlier, new = later)."""
    summary = cache_summary(old, new)
    messages = compare_messages(old, new)
    tools = compare_tools(old, new)
    params = compare_params(old, new)
    identity = compare_identity(old, new)
    return {
        "old_ref": {"request_id": old.get("request_id"), "model": old.get("model"),
                    "timestamp": old.get("timestamp")},
        "new_ref": {"request_id": new.get("request_id"), "model": new.get("model"),
                    "timestamp": new.get("timestamp")},
        "models_match": old.get("model") == new.get("model"),
        "summary": summary,
        "tps": {"old": compute_tps(old), "new": compute_tps(new)},
        "messages": messages,
        "tools": tools,
        "params": params,
        "identity": identity,
        "inference": infer(messages, tools, params, identity, old, new),
    }


def _positive_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def compute_tps(details: dict[str, Any]) -> dict[str, Any]:
    """Decode / end-to-end tokens-per-second for one request detail.

    Prefers ``timings.output_ms`` (first business event -> finish) for the
    decode speed and always reports the wall-clock end-to-end speed when
    ``duration`` is available. Missing inputs yield ``None`` speeds and
    ``basis == "unavailable"`` instead of raising.
    """
    output_tokens = int(details.get("output_tokens") or 0)
    duration_s = _positive_number(details.get("duration"))
    timings = details.get("timings") if isinstance(details.get("timings"), dict) else {}
    output_s = _positive_number((timings or {}).get("output_ms"))
    if output_s is not None:
        output_s = output_s / 1000
        if output_s <= 0:
            output_s = None
    ttft_s: Optional[float] = None
    ttft_ms = _positive_number((timings or {}).get("first_business_ms"))
    if ttft_ms is not None:
        ttft_s = round(ttft_ms / 1000, 3)

    decode_tps = round(output_tokens / output_s, 2) if output_s and output_tokens else None
    e2e_tps = round(output_tokens / duration_s, 2) if duration_s and output_tokens else None

    if decode_tps is not None:
        basis = "output_ms"
    elif e2e_tps is not None:
        basis = "duration"
    else:
        basis = "unavailable"

    # Non-streaming responses finish almost instantly after the first
    # business event, which makes decode_tps look absurdly high. Flag it so
    # the UI can point at the end-to-end number instead.
    caveat: Optional[str] = None
    if decode_tps is not None and duration_s and output_s and output_s < duration_s / 10:
        caveat = "输出阶段远短于总耗时（多为非流式响应），解码速度仅供参考，以端到端速度为准。"

    return {
        "output_tokens": output_tokens,
        "duration_s": round(duration_s, 3) if duration_s else None,
        "output_s": round(output_s, 3) if output_s else None,
        "ttft_s": ttft_s,
        "decode_tps": decode_tps,
        "e2e_tps": e2e_tps,
        "basis": basis,
        "caveat": caveat,
    }
