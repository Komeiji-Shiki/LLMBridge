#!/usr/bin/env python3
"""Compare OpenAI-compatible request logs and locate prompt-cache discontinuities.

Thin CLI wrapper over :mod:`utils.prompt_cache_compare` (which is also used
by the monitor ``/api/monitor/compare`` route). Output format is kept
compatible with the original standalone script.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.prompt_cache_compare import (  # noqa: E402
    cached_tokens,
    canonical,
    compare,
    compare_identity,
    compare_messages,
    compare_params,
    compare_tools,
    context_snippet as context,
    first_text_difference,
    gcd_nonzero,
    short_hash,
)


def load_log(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: JSON top level must be an object")
    return value


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


def report_messages(old: dict[str, Any], new: dict[str, Any]) -> tuple[bool, Any]:
    result = compare_messages(old, new)
    print("\n[Messages]")
    print(f"old={result['old_count']}, new={result['new_count']}, common_count={result['common_count']}")
    first = result["first_difference"]
    if first is not None:
        print(f"FIRST DIFFERENCE: message[{first['index']}], canonical_char={first['canonical_char_offset']}")
        print(f"  old role={first['old_role']!r}, sha256={first['old_sha256']}")
        print(f"  new role={first['new_role']!r}, sha256={first['new_sha256']}")
        if first["canonical_char_offset"] is not None:
            print(f"  old context: {first['old_context']}")
            print(f"  new context: {first['new_context']}")

    strict_append = result["strict_append"]
    if not result['available']:
        print("UNKNOWN: complete request messages are missing")
    elif first is None:
        print("common messages are byte-for-byte identical after canonical JSON serialization")
    print(f"new request is a strict append of old messages: {strict_append}")
    if strict_append:
        print("appended messages:")
        for message in result["appended"]:
            print(
                f"  [{message['index']}] role={message['role']!r} name={message['name']!r} "
                f"chars={message['chars']} sha256={message['sha256']}"
            )
    return strict_append, first


def report_tools(old: dict[str, Any], new: dict[str, Any]) -> bool | None:
    """Print the tools section; return True/False/None like the original script."""
    result = compare_tools(old, new)
    status = result["status"]
    print("\n[Tools]")
    if result["source"] == "tools":
        same = status == "identical"
        print(
            f"old={result['old_count']}, new={result['new_count']}, identical={same}, "
            f"old_sha256={result['old_sha256']}, new_sha256={result['new_sha256']}"
        )
        if not same and "first_difference" in result:
            first = result["first_difference"]
            print(f"FIRST DIFFERENCE: tool[{first['index']}]")
            print(f"  old={first['old_name']!r}")
            print(f"  new={first['new_name']!r}")
        return same
    if status == "unknown":
        print(
            "UNKNOWN: neither log records tool definitions "
            f"(tools_count old={result.get('old_count')} new={result.get('new_count')}). "
            "Tool changes cannot be ruled out as the cache-break cause; "
            "compare tools_sha256/tools_names in newer logs."
        )
        return None
    same = status == "identical"
    print(
        f"via tools_sha256: identical={same}, "
        f"old_count={result.get('old_count')} new_count={result.get('new_count')}, "
        f"old_sha256={result.get('old_sha256')}, new_sha256={result.get('new_sha256')}"
    )
    if not same:
        old_names = old.get("tools_names") or []
        new_names = new.get("tools_names") or []
        print(f"  old names({len(old_names)}): {old_names[:20]}")
        print(f"  new names({len(new_names)}): {new_names[:20]}")
        print(f"  removed: {sorted(set(old_names) - set(new_names))[:20]}")
        print(f"  added: {sorted(set(new_names) - set(old_names))[:20]}")
        if set(old_names) == set(new_names):
            print("  same name set but different hash: order or schemas changed")
    return same


def report_request_parameters(old: dict[str, Any], new: dict[str, Any]) -> bool:
    result = compare_params(old, new)
    print("\n[Cache-relevant request parameters]")
    for difference in result["differences"]:
        print(f"DIFFERENT {difference['key']}: old={difference['old']}, new={difference['new']}")
    if not result["differences"]:
        print("all recorded non-volatile parameters are identical")
    return result["same"]


def report_identity(old: dict[str, Any], new: dict[str, Any]) -> bool:
    result = compare_identity(old, new)
    print("\n[Identity (session/caller attribution)]")
    if not result["fields"]:
        print("neither log records attribution fields")
        return True
    for field in result["fields"]:
        flag = "SAME" if field["same"] else "DIFFERENT"
        print(f"{flag} {field['key']}: old={old.get(field['key'])!r}, new={new.get(field['key'])!r}")
    if not result["same"]:
        print("NOTE: gateway attribution alone does not identify upstream cache namespaces; "
              "mismatched attribution may mean the two requests never shared cache.")
    return result["same"]


def infer(strict_append: bool | None, tools_same: bool | None, params_same: bool, old: dict[str, Any], new: dict[str, Any]) -> None:
    """Keep the legacy entry point while sharing the monitor's evidence rules."""
    print("\n[Inference]")
    for conclusion in compare(old, new)["inference"]:
        print(conclusion)


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
    report_identity(old, new)
    strict_append, _ = report_messages(old, new)
    tools_same = report_tools(old, new)
    params_same = report_request_parameters(old, new)
    infer(strict_append, tools_same, params_same, old, new)
    # Structured result is available for programmatic use (also powers /api/monitor/compare).
    _structured = compare(old, new)
    assert _structured["messages"]["strict_append"] == strict_append
    if args.timeline_dir:
        timeline(args.timeline_dir, args.model or str(new.get("model") or ""))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
