"""报告跨协议时无法应用的客户端缓存控制。"""

import json


def _warning(field: str, target: str) -> dict:
    return {"field": field, "applied": False, "message": f"此缓存控制未应用到 {target} 上游。"}


def anthropic_cache_warnings(request: dict) -> list:
    warnings = []

    def blocks(value, path):
        if not isinstance(value, list):
            return
        for index, block in enumerate(value):
            if not isinstance(block, dict):
                continue
            location = f"{path}[{index}]"
            if "cache_control" in block:
                warnings.append(_warning(f"{location}.cache_control", "Responses"))
            if block.get("type") == "tool_result":
                blocks(block.get("content"), f"{location}.content")

    if "cache_control" in request:
        warnings.append(_warning("cache_control", "Responses"))
    blocks(request.get("system"), "system")
    blocks(request.get("tools"), "tools")
    for index, message in enumerate(request.get("messages") or []):
        if isinstance(message, dict):
            blocks(message.get("content"), f"messages[{index}].content")
    return warnings


def responses_cache_warnings(request: dict) -> list:
    warnings = [_warning(field, "Anthropic") for field in
                ("prompt_cache_key", "prompt_cache_retention", "prompt_cache_options") if field in request]
    items = request.get("input")
    if not isinstance(items, list):
        return warnings
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if "prompt_cache_breakpoint" in item:
            warnings.append(_warning(f"input[{index}].prompt_cache_breakpoint", "Anthropic"))
        for field in ("content", "output"):
            parts = item.get(field)
            if isinstance(parts, list):
                for part_index, part in enumerate(parts):
                    if isinstance(part, dict) and "prompt_cache_breakpoint" in part:
                        warnings.append(_warning(f"input[{index}].{field}[{part_index}].prompt_cache_breakpoint", "Anthropic"))
    return warnings


def warning_headers(warnings: list) -> dict:
    return {"X-Bridge-Warnings": json.dumps(warnings, ensure_ascii=True, separators=(",", ":"))} if warnings else {}
