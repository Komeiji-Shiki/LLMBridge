#!/usr/bin/env python3
"""Compare OpenAI-compatible request logs and locate prompt-cache discontinuities."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

VOLATILE_FIELDS = {
    "type", "timestamp", "end_timestamp", "request_id", "status", "success",
    "duration", "error", "messages_count", "input_tokens", "output_tokens",
    "cached_tokens", "response_content", "response_tool_calls", "reasoning_content",
    "cost_info", "upstream_usage", "streaming",
}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def short_hash(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()[:16]


def load_log(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: JSON top level must be an object")
    return value


def cached_tokens(log: dict[str, Any]) -> int:
    direct = log.get("cached_tokens")
    if isinstance(direct, int):
        return direct
    usage = log.get("upstream_usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    value = details.get("cached_tokens")
    return value if isinstance(value, int) else 0


def first_text_difference(left: str, right: str) -> int | None:
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return index
    return min(len(left), len(right)) if len(left) != len(right) else None


def context(text: str, index: int, radius: int = 140) -> str:
    return text[max(0, index - radius): index + radius].replace("\n", "\\n")


def gcd_nonzero(values: Iterable[int]) -> int:
    result = 0
    for value in values:
        if value:
            result = math.gcd(result, abs(value))
    return result


def compare_messages(old: dict[str, Any], new: dict[str, Any]) -> tuple[bool, int | None]:
    old_messages = old.get("request_messages") or []
    new_messages = new.get("request_messages") or []
    common = min(len(old_messages), len(new_messages))
    first_difference = None

    print("\n[Messages]")
    print(f"old={len(old_messages)}, new={len(new_messages)}, common_count={common}")
    for index in range(common):
        old_text = canonical(old_messages[index])
        new_text = canonical(new_messages[index])
        if old_text != new_text:
            first_difference = index
            offset = first_text_difference(old_text, new_text)
            print(f"FIRST DIFFERENCE: message[{index}], canonical_char={offset}")
            print(f"  old role={old_messages[index].get('role')!r}, sha256={short_hash(old_messages[index])}")
            print(f"  new role={new_messages[index].get('role')!r}, sha256={short_hash(new_messages[index])}")
            if offset is not None:
                print(f"  old context: {context(old_text, offset)}")
                print(f"  new context: {context(new_text, offset)}")
            break

    strict_append = len(new_messages) >= len(old_messages) and old_messages == new_messages[:len(old_messages)]
    if first_difference is None:
        print("common messages are byte-for-byte identical after canonical JSON serialization")
    print(f"new request is a strict append of old messages: {strict_append}")
    if strict_append:
        print("appended messages:")
        for index, message in enumerate(new_messages[len(old_messages):], len(old_messages)):
            print(
                f"  [{index}] role={message.get('role')!r} name={message.get('name')!r} "
                f"chars={len(canonical(message))} sha256={short_hash(message)}"
            )
    return strict_append, first_difference


def compare_tools(old: dict[str, Any], new: dict[str, Any]) -> bool:
    old_tools = old.get("tools") or []
    new_tools = new.get("tools") or []
    same = canonical(old_tools) == canonical(new_tools)
    print("\n[Tools]")
    print(
        f"old={len(old_tools)}, new={len(new_tools)}, identical={same}, "
        f"old_sha256={short_hash(old_tools)}, new_sha256={short_hash(new_tools)}"
    )
    if not same:
        for index, (left, right) in enumerate(zip(old_tools, new_tools)):
            if canonical(left) != canonical(right):
                print(f"FIRST DIFFERENCE: tool[{index}]")
                print(f"  old={left.get('function', {}).get('name')!r}")
                print(f"  new={right.get('function', {}).get('name')!r}")
                break
    return same


def compare_request_parameters(old: dict[str, Any], new: dict[str, Any]) -> bool:
    keys = sorted((set(old) | set(new)) - VOLATILE_FIELDS - {"request_messages", "tools"})
    differences = []
    print("\n[Cache-relevant request parameters]")
    for key in keys:
        if canonical(old.get(key)) != canonical(new.get(key)):
            differences.append(key)
            print(f"DIFFERENT {key}: old={old.get(key)!r}, new={new.get(key)!r}")
    if not differences:
        print("all recorded non-volatile parameters are identical")
    return not differences


def print_cache_summary(old: dict[str, Any], new: dict[str, Any]) -> None:
    old_input = int(old.get("input_tokens") or 0)
    new_input = int(new.get("input_tokens") or 0)
    old_cached = cached_tokens(old)
    new_cached = cached_tokens(new)
    values = [old_cached, new_cached]
    quantum = gcd_nonzero(values)

    print("[Cache summary]")
    print(f"old input={old_input}, cached={old_cached}, hit_rate={(old_cached / old_input * 100) if old_input else 0:.2f}%")
    print(f"new input={new_input}, cached={new_cached}, hit_rate={(new_cached / new_input * 100) if new_input else 0:.2f}%")
    print(f"cached-token delta={new_cached - old_cached:+d}")
    print(f"GCD of the two cache counts={quantum}; both divisible by 128={all(v % 128 == 0 for v in values)}")
    old_start = float(old.get("timestamp") or 0)
    old_end = float(old.get("end_timestamp") or old_start)
    new_start = float(new.get("timestamp") or 0)
    if old_start and new_start:
        print(f"start-to-start gap={new_start - old_start:.3f}s")
        print(f"previous-end-to-new-start gap={new_start - old_end:.3f}s")


def infer(strict_append: bool, tools_same: bool, params_same: bool, old: dict[str, Any], new: dict[str, Any]) -> None:
    old_cached = cached_tokens(old)
    new_cached = cached_tokens(new)
    print("\n[Inference]")
    if strict_append and tools_same and params_same and new_cached < old_cached:
        print("The recorded request did not break the shared prompt prefix.")
        print("The cache discontinuity occurred upstream, before model generation, not inside the logged message history.")
        print("Most likely causes: cache-shard/backend reassignment, partial cache eviction, or provider-side cache expiration.")
        print("A surviving non-zero prefix means an earlier/shared prefix block was present while later conversation-specific blocks were absent.")
        print("The log alone cannot distinguish shard reassignment from eviction because no cache node/shard/fingerprint is recorded.")
    elif new_cached < old_cached:
        print("The cache fell and the request payload also changed. Inspect the first difference above as the candidate break point.")
    else:
        print("No cache regression was detected between these files.")


def timeline(directory: Path, model: str | None) -> None:
    rows = []
    for path in directory.glob("*.json"):
        try:
            log = load_log(path)
        except Exception:
            continue
        if model and log.get("model") != model and log.get("upstream_model") != model:
            continue
        if not log.get("request_messages"):
            continue
        rows.append((float(log.get("timestamp") or 0), path, log))
    rows.sort(key=lambda row: row[0])
    if not rows:
        return

    print("\n[Directory timeline]")
    previous = None
    cache_values = []
    for timestamp, path, log in rows:
        current_cached = cached_tokens(log)
        cache_values.append(current_cached)
        prompt_tokens = int(log.get("input_tokens") or 0)
        time_text = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S.%f")[:-3]
        prefix = "n/a"
        if previous is not None:
            previous_log = previous[2]
            old_messages = previous_log.get("request_messages") or []
            new_messages = log.get("request_messages") or []
            same_user = previous_log.get("user_id") == log.get("user_id")
            append = len(new_messages) >= len(old_messages) and old_messages == new_messages[:len(old_messages)]
            prefix = f"same_user={same_user}, strict_append={append}"
        ratio = current_cached / prompt_tokens * 100 if prompt_tokens else 0
        print(
            f"{time_text} {path.name} messages={len(log.get('request_messages') or [])} "
            f"input={prompt_tokens} cached={current_cached} hit={ratio:.1f}% {prefix}"
        )
        previous = (timestamp, path, log)
    print(f"observed cache-count GCD={gcd_nonzero(cache_values)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old_log", type=Path)
    parser.add_argument("new_log", type=Path)
    parser.add_argument("--timeline-dir", type=Path, help="scan neighboring JSON logs chronologically")
    parser.add_argument("--model", help="optional model filter for --timeline-dir")
    args = parser.parse_args()

    old = load_log(args.old_log)
    new = load_log(args.new_log)
    print(f"OLD: {args.old_log}")
    print(f"NEW: {args.new_log}\n")
    print_cache_summary(old, new)
    strict_append, _ = compare_messages(old, new)
    tools_same = compare_tools(old, new)
    params_same = compare_request_parameters(old, new)
    infer(strict_append, tools_same, params_same, old, new)
    if args.timeline_dir:
        timeline(args.timeline_dir, args.model or str(new.get("model") or ""))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)