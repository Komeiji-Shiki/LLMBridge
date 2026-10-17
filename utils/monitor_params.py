"""监控请求参数快照工具。

监控面板需要展示请求参数，但请求参数里最容易膨胀的是 system 提示词与
tools 定义（Claude Code 单条请求的 system + tools 可达数百 KB）。这些内容
原样留在内存里会随 recent_requests 线性增长，因此统一在这里做有界压缩：

- 字符串超长截断；
- 列表/字典限长、限深；
- system 只保留预览；
- tools 只保留名称、描述预览和参数名；
- 最后按序列化后的字节数做兜底裁剪。

压缩后的快照只用于监控展示；请求日志与 SQLite 不依赖这里的完整内容。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


_DEFAULT_EXCLUDED_KEYS = {"messages", "contents", "model"}
MONITOR_PARAM_EXCLUDED_KEYS = set(_DEFAULT_EXCLUDED_KEYS)

_SENSITIVE_EXACT_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "password",
    "passwd",
    "secret",
    "cookie",
}
_SENSITIVE_SUFFIXES = ("_api_key", "_secret", "_password", "_token")

# 单个字段 / 容器的保留上限。Claude Code 的 tools 定义通常有十几项，
# 每项 schema 数十 KB；这里把 schema 压到“参数名 + 类型”级别。
_MAX_STRING_CHARS = 2000
_MAX_SYSTEM_PREVIEW_CHARS = 800
_MAX_TOOL_DESCRIPTION_CHARS = 120
_MAX_SCHEMA_DESCRIPTION_CHARS = 80
_MAX_LIST_ITEMS = 100
_MAX_DICT_ITEMS = 100
_MAX_DEPTH = 6
_MAX_PARAM_BYTES = 24 * 1024
_MAX_TOOLS_BYTES = 12 * 1024
# Gemini 把函数声明放在 functionDeclarations（REST 驼峰）/ function_declarations 列表里
_TOOL_DECLARATION_KEYS = ("functionDeclarations", "function_declarations")


def _is_sensitive_key(key: Any) -> bool:
    key_text = str(key).lower()
    if key_text in _SENSITIVE_EXACT_KEYS:
        return True
    if key_text.endswith(_SENSITIVE_SUFFIXES):
        return True
    return key_text in {"access_token", "refresh_token", "id_token"}


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…[truncated {len(text) - limit} chars]"


def _compact_value(value: Any, *, depth: int = 0, key: Any = None) -> Any:
    """把任意值压缩成可 JSON 序列化、体量有界的结构。"""
    if key is not None and _is_sensitive_key(key):
        return "[REDACTED]"

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        return _truncate_text(value, _MAX_STRING_CHARS)

    if isinstance(value, bytes):
        return f"<bytes len={len(value)}>"

    if isinstance(value, dict):
        if depth >= _MAX_DEPTH:
            return f"<dict keys={len(value)} omitted>"
        result = {}
        for index, (item_key, item_value) in enumerate(value.items()):
            if index >= _MAX_DICT_ITEMS:
                result["_truncated_keys"] = len(value) - _MAX_DICT_ITEMS
                break
            result[str(item_key)] = _compact_value(
                item_value, depth=depth + 1, key=item_key
            )
        return result

    if isinstance(value, (list, tuple)):
        if depth >= _MAX_DEPTH:
            return f"<list len={len(value)} omitted>"
        result = [
            _compact_value(item, depth=depth + 1)
            for item in value[:_MAX_LIST_ITEMS]
        ]
        if len(value) > _MAX_LIST_ITEMS:
            result.append(f"…[+{len(value) - _MAX_LIST_ITEMS} items]")
        return result

    return _truncate_text(str(value), _MAX_STRING_CHARS)


def _compact_system(value: Any) -> Any:
    """system 提示词只保留可读预览，避免整份提示词常驻监控内存。"""
    if isinstance(value, str):
        return _truncate_text(value, _MAX_SYSTEM_PREVIEW_CHARS)
    if isinstance(value, list):
        blocks = []
        for block in value[:_MAX_LIST_ITEMS]:
            if isinstance(block, dict):
                compact_block = {}
                for block_key, block_value in block.items():
                    if block_key in ("text", "content") and isinstance(block_value, str):
                        compact_block[block_key] = _truncate_text(
                            block_value, _MAX_SYSTEM_PREVIEW_CHARS
                        )
                    else:
                        compact_block[str(block_key)] = _compact_value(
                            block_value, depth=3, key=block_key
                        )
                blocks.append(compact_block)
            else:
                blocks.append(_compact_value(block, depth=3))
        if len(value) > _MAX_LIST_ITEMS:
            blocks.append(f"…[+{len(value) - _MAX_LIST_ITEMS} blocks]")
        return blocks
    return _compact_value(value)


def _compact_schema(schema: dict) -> dict:
    """保留工具参数的结构骨架，去掉冗长的描述与嵌套默认值。"""
    result: dict = {}
    schema_type = schema.get("type")
    if schema_type is not None:
        result["type"] = schema_type

    properties = schema.get("properties")
    if isinstance(properties, dict):
        compact_properties = {}
        for index, (name, prop) in enumerate(properties.items()):
            if index >= _MAX_DICT_ITEMS:
                compact_properties["_truncated"] = len(properties) - _MAX_DICT_ITEMS
                break
            if isinstance(prop, dict):
                compact_prop = {}
                for prop_key in ("type", "format", "enum"):
                    if prop_key in prop:
                        compact_prop[prop_key] = _compact_value(
                            prop[prop_key], depth=4, key=prop_key
                        )
                description = prop.get("description")
                if isinstance(description, str) and description:
                    compact_prop["description"] = _truncate_text(
                        description, _MAX_SCHEMA_DESCRIPTION_CHARS
                    )
                items = prop.get("items")
                if isinstance(items, dict) and "type" in items:
                    compact_prop["items"] = {"type": items.get("type")}
                compact_properties[name] = compact_prop
            else:
                compact_properties[name] = _compact_value(prop, depth=4)
        result["properties"] = compact_properties

    required = schema.get("required")
    if isinstance(required, list):
        result["required"] = _compact_value(required, depth=4)

    if not result:
        result["type"] = "object"
    return result


def _compact_wrapped_tools(tool: dict, compact_one) -> dict:
    """OpenAI Chat 把定义包在 function 里，Gemini 用 functionDeclarations 列表。"""
    wrapped = {}
    if isinstance(tool.get("function"), dict):
        wrapped["function"] = compact_one(tool["function"])
    for list_key in _TOOL_DECLARATION_KEYS:
        declarations = tool.get(list_key)
        if isinstance(declarations, list):
            wrapped[list_key] = [compact_one(item) for item in declarations[:_MAX_LIST_ITEMS]]
            if len(declarations) > _MAX_LIST_ITEMS:
                wrapped[list_key].append(f"…[+{len(declarations) - _MAX_LIST_ITEMS} items]")
    return wrapped


def _compact_builtin_tool(tool: dict) -> dict:
    """googleSearch / codeExecution 这类内置工具只有配置键，没有名称和参数。"""
    return {
        str(key): _compact_value(value, depth=3, key=key)
        for key, value in list(tool.items())[:_MAX_DICT_ITEMS]
    }


def _compact_tool(tool: Any) -> Any:
    if not isinstance(tool, dict):
        return _compact_value(tool, depth=2)
    compact_tool = {}
    for tool_key in ("type", "name", "cache_control"):
        if tool_key in tool:
            compact_tool[tool_key] = _compact_value(
                tool[tool_key], depth=2, key=tool_key
            )
    compact_tool.update(_compact_wrapped_tools(tool, _compact_tool))
    description = tool.get("description")
    if isinstance(description, str) and description:
        compact_tool["description"] = _truncate_text(
            description, _MAX_TOOL_DESCRIPTION_CHARS
        )
    # OpenAI 用 parameters，Anthropic 用 input_schema，Gemini 用 parametersJsonSchema
    for schema_key in ("parameters", "input_schema", "parametersJsonSchema"):
        schema = tool.get(schema_key)
        if isinstance(schema, dict):
            compact_tool[schema_key] = _compact_schema(schema)
            break
    return compact_tool or _compact_builtin_tool(tool)


def _compact_tool_summary(tool: Any) -> Any:
    """tools 总预算超限时的极简形态：只保留名称、描述和参数数量。"""
    if not isinstance(tool, dict):
        return _compact_value(tool, depth=2)
    result = {}
    for tool_key in ("type", "name"):
        if tool_key in tool:
            result[tool_key] = _compact_value(tool[tool_key], depth=2, key=tool_key)
    result.update(_compact_wrapped_tools(tool, _compact_tool_summary))
    description = tool.get("description")
    if isinstance(description, str) and description:
        result["description"] = _truncate_text(
            description, _MAX_TOOL_DESCRIPTION_CHARS
        )
    for schema_key in ("parameters", "input_schema", "parametersJsonSchema"):
        schema = tool.get(schema_key)
        if isinstance(schema, dict):
            properties = schema.get("properties")
            result[schema_key] = {
                "type": schema.get("type", "object"),
                "property_count": len(properties) if isinstance(properties, dict) else 0,
            }
            break
    return result or {str(key): "…" for key in list(tool)[:_MAX_DICT_ITEMS]}


def summarize_tools(tools: list) -> dict:
    """完整 tools 定义的摘要：数量、规范哈希与名称序列。

    顺序参与哈希（不排序）：工具顺序变化同样会破坏上游缓存前缀。
    """
    raw = json.dumps(tools, ensure_ascii=False, separators=(",", ":"), default=str)
    return {
        "tools_count": len(tools),
        "tools_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "tools_names": _tool_names(tools),
    }


def _tool_names(tools: list) -> list:
    names = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        name = function.get("name") if isinstance(function, dict) else tool.get("name")
        if isinstance(name, str) and name:
            names.append(name)
            continue
        declarations = next((tool[key] for key in _TOOL_DECLARATION_KEYS
                             if isinstance(tool.get(key), list)), None)
        if declarations is not None:
            names.extend(item["name"] for item in declarations
                         if isinstance(item, dict) and isinstance(item.get("name"), str))
        elif isinstance(tool.get("type"), str):
            names.append(tool["type"])
        elif tool:
            names.append(str(next(iter(tool))))
    return names


def _fingerprint(value: Any) -> str:
    """完整值的 sha256（敏感键先脱敏，避免哈希可被用来校验猜测的密钥）。"""
    raw = json.dumps(_redact_sensitive(value), ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "[REDACTED]" if _is_sensitive_key(key) else _redact_sensitive(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _add_compacted(params: dict, key: Any, value: Any) -> None:
    """写入压缩后的字段；压缩有损时附带完整内容的指纹。

    提示词缓存对比依赖这些指纹：tools 描述或 system 在预览截断之后的
    改动，压缩快照看不出来，只能靠完整内容的哈希发现。已经携带指纹的
    快照（monitoring 对 build 结果的二次压缩）保留原指纹。
    """
    if key == "system":
        compact = _compact_system(value)
    elif key == "tools":
        compact = _compact_tools(value)
    else:
        compact = _compact_value(value, depth=1, key=key)
    params[key] = compact
    if key == "tools" and isinstance(value, list) and compact != value:
        for summary_key, summary_value in summarize_tools(value).items():
            params.setdefault(summary_key, summary_value)
    elif (isinstance(value, (str, list, dict)) and not _is_sensitive_key(key)
          and compact != value):
        params.setdefault(f"{key}_sha256", _fingerprint(value))


def _compact_tools(value: Any) -> Any:
    """tools 定义只保留名称、描述预览与参数结构，并受总字节预算约束。"""
    if not isinstance(value, list):
        return _compact_value(value, depth=1)

    tools = [_compact_tool(tool) for tool in value[:_MAX_LIST_ITEMS]]
    if len(value) > _MAX_LIST_ITEMS:
        tools.append(f"…[+{len(value) - _MAX_LIST_ITEMS} tools]")

    # 工具 schema 可能非常大：先退化为只保留名称/描述/参数数量，
    # 仍超预算则缩短列表，保证 tools 字段不会在最终裁剪中被整体丢弃。
    if _json_size(tools) > _MAX_TOOLS_BYTES:
        tools = [
            _compact_tool_summary(tool) for tool in value[:_MAX_LIST_ITEMS]
        ]
        if len(value) > _MAX_LIST_ITEMS:
            tools.append(f"…[+{len(value) - _MAX_LIST_ITEMS} tools]")

    if _json_size(tools) > _MAX_TOOLS_BYTES and tools:
        keep = max(1, len(tools) // 2)
        tools = tools[:keep]
        if len(value) > keep:
            tools.append(f"…[+{len(value) - keep} tools]")
    return tools


def _json_size(value: Any) -> int:
    try:
        return len(
            json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        )
    except (TypeError, ValueError):
        return _MAX_PARAM_BYTES + 1


def _shrink_to_budget(params: dict, max_bytes: int = _MAX_PARAM_BYTES) -> dict:
    """兜底：按大小从大到小丢弃字段，保证单条快照不超过预算。"""
    if _json_size(params) <= max_bytes:
        return params

    sizes = {key: _json_size(value) for key, value in params.items()}
    result = dict(params)
    dropped = []
    for key, _ in sorted(sizes.items(), key=lambda item: item[1], reverse=True):
        if _json_size(result) <= max_bytes:
            break
        result.pop(key, None)
        dropped.append(key)
    if dropped:
        result["_dropped_fields"] = dropped
    return result


def compact_monitor_params(params: Any, *, max_bytes: int = _MAX_PARAM_BYTES) -> dict:
    """压缩一个已经构建好的参数快照（供监控入口统一调用）。"""
    if not isinstance(params, dict):
        return {}
    compacted = {}
    for key, value in params.items():
        if key in MONITOR_PARAM_EXCLUDED_KEYS:
            continue
        _add_compacted(compacted, key, value)
    return _shrink_to_budget(compacted, max_bytes=max_bytes)


def build_monitor_request_params(
    request_body: dict | None,
    *,
    extra: dict | None = None,
    exclude_keys: Iterable[str] | None = None,
    max_bytes: int = _MAX_PARAM_BYTES,
) -> dict:
    """构建监控面板用的有界请求参数快照。

    默认排除 messages / contents / model：消息正文已经通过 request_messages
    单独记录，model 已在监控记录顶层记录；其余请求体字段（tools、thinkingConfig、
    response_format、stream_options、自定义参数等）压缩后保留。
    """
    excluded = set(MONITOR_PARAM_EXCLUDED_KEYS)
    if exclude_keys:
        excluded.update(exclude_keys)

    params = {}
    if isinstance(request_body, dict):
        for key, value in request_body.items():
            if key in excluded:
                continue
            _add_compacted(params, key, value)

    if "stream" in params and "streaming" not in params:
        params["streaming"] = bool(params.get("stream"))

    if extra:
        for key, value in extra.items():
            params[key] = _compact_value(value, depth=1, key=key)

    return _shrink_to_budget(params, max_bytes=max_bytes)
