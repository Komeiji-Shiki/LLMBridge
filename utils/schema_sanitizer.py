"""JSON Schema 递归引用清洗工具。

背景：
- Codex / Responses API 客户端下发的 tools / text.format / response_format
  中经常带有 ``$ref`` 自引用（例如 ``#/$defs/Node`` 引用自身），OpenAI
  兼容上游（含 MuseSpark 聚合后的 Console Go 等渠道）在 ``strict=true``
  或严格校验模式下会直接 400：

      [invalid_request_error] Recursive JSON schemas are not currently supported

- 本桥接默认是透传模式，原样转发必炸。这里提供集中式的清洗函数：
  检测 ``$ref`` 环、把递归点截断为 ``{"type": "object"}``，并在发生截断时
  把同级 tool 的 ``strict`` 强制置为 ``False``（strict 模式本身就不允许递归）。

设计要点：
- 只处理内存中的深拷贝，不修改调用方传入的原始对象。
- 非递归的 ``$ref`` 会被内联展开（附带 20 层深度兜底），消除后
  上游不再能看到任何递归环；不可解析的外部 ``$ref`` 原样保留。
- 遍历覆盖 properties / items / anyOf / oneOf / allOf / not /
  additionalProperties / patternProperties / prefixItems 等全部常见容器键，
  而不是只处理两三个写死的键，避免漏网。
- 除经典 ``$ref`` 外，同时处理 ``$recursiveRef`` / ``$dynamicRef``
  （新版 JSON Schema 的递归写法），并用对象 id 做别名环检测，
  防止 ``#/$defs/A`` 与 ``#/definitions/A`` 指向同一对象时漏判。
- 仅当真正检测到递归环时才改写 schema；非递归的 ``$ref`` 原样保留——
  内联展开会把共享 ``$defs`` 复制多份，直接推高上游计费 token（实测 24 工具
  下游请求可膨胀数倍）。strict 误报问题由 ``force_all_strict_false_*``
  统一降级解决，不靠改 schema。
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

_RECURSIVE_TRUNCATION_DESC = (
    "Recursive reference truncated for upstream compatibility"
)

# 深度兜底：防止恶意/超大 schema 内联展开时爆栈或体积爆炸。
_MAX_INLINE_DEPTH = 20

# 视为“引用”的键：经典 $ref + 新版递归/动态引用。
_REF_KEYS = ("$ref", "$recursiveRef", "$dynamicRef")


def _resolve_json_pointer(root: Any, ref: str) -> Any:
    """解析本地 JSON Pointer ``#/...``，失败返回 None。

    支持 ``#``（根）、``#/$defs/Foo``、``#/definitions/Foo``、
    ``#/properties/a/properties/b`` 等形式，含 ``~0``/``~1`` 转义与数组下标。
    非本地引用（http(s)://、urn:、空以外的纯文件引用等）一律返回 None，
    交由调用方原样保留。
    """
    if not isinstance(ref, str) or not ref.startswith("#"):
        return None
    if ref == "#":
        return root
    if not ref.startswith("#/"):
        return None
    parts = ref[2:].split("/")
    cur: Any = root
    for raw in parts:
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                idx = int(part)
            except ValueError:
                return None
            if idx < 0 or idx >= len(cur):
                return None
            cur = cur[idx]
        else:
            return None
    return cur


def _get_ref_key(node: Dict[str, Any]) -> str | None:
    """返回节点中的引用键（$ref / $recursiveRef / $dynamicRef），无则返回 None。"""
    for key in _REF_KEYS:
        if isinstance(node.get(key), str):
            return key
    return None


def _collect_refs(node: Any, out: List[str], limit: int = 10) -> None:
    """收集 schema 中的引用值（最多 limit 个），仅用于日志定位。"""
    if len(out) >= limit:
        return
    if isinstance(node, dict):
        ref_key = _get_ref_key(node)
        if ref_key is not None:
            out.append(str(node[ref_key]))
            if len(out) >= limit:
                return
        for value in node.values():
            _collect_refs(value, out, limit)
            if len(out) >= limit:
                return
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, out, limit)
            if len(out) >= limit:
                return


def sanitize_json_schema(schema: Any) -> Tuple[Any, bool]:
    """清洗单个 JSON Schema，返回 ``(清洗后对象, 是否发生递归截断)``。

    输入非 dict/list（例如 None/字符串）时原样返回 ``(原值, False)``。
    """
    if not isinstance(schema, (dict, list)):
        return schema, False
    root = copy.deepcopy(schema)
    had_recursion = [False]

    def _sanitize(node: Any, ref_stack: Tuple[str, ...], obj_stack: Tuple[int, ...], depth: int) -> Any:
        if depth > _MAX_INLINE_DEPTH:
            had_recursion[0] = True
            return {"type": "object", "description": _RECURSIVE_TRUNCATION_DESC}
        if isinstance(node, dict):
            ref_key = _get_ref_key(node)
            if ref_key is not None:
                ref = node[ref_key]
                if ref in ref_stack:
                    had_recursion[0] = True
                    desc = node.get("description") or _RECURSIVE_TRUNCATION_DESC
                    try:
                        desc_str = str(desc)[:200]
                    except Exception:
                        desc_str = _RECURSIVE_TRUNCATION_DESC
                    return {"type": "object", "description": desc_str}
                target = _resolve_json_pointer(root, ref)
                if target is not None:
                    if id(target) in obj_stack:
                        # 不同 ref 字符串指向同一对象形成的别名环。
                        had_recursion[0] = True
                        desc = node.get("description") or _RECURSIVE_TRUNCATION_DESC
                        try:
                            desc_str = str(desc)[:200]
                        except Exception:
                            desc_str = _RECURSIVE_TRUNCATION_DESC
                        return {"type": "object", "description": desc_str}
                    inlined = _sanitize(target, ref_stack + (ref,), obj_stack + (id(target),), depth + 1)
                    if not isinstance(inlined, dict):
                        # 目标不是对象（如 $ref 指向数组/字面量）：退化为通用对象，
                        # 保留原地 siblings，避免丢失 description 等信息。
                        had_recursion[0] = True
                        merged: Dict[str, Any] = {"type": "object"}
                    else:
                        merged = dict(inlined)
                    for key, value in node.items():
                        if key == ref_key:
                            continue
                        if key in _REF_KEYS:
                            # 同节点残留的其他引用键：同样内联清洗，避免漏网。
                            merged[key] = _sanitize(value, ref_stack, obj_stack, depth + 1)
                            continue
                        if key == "description" and key in merged:
                            continue
                        merged[key] = _sanitize(value, ref_stack, obj_stack, depth + 1)
                    # 统一输出为经典 $ref 形态：删掉 $recursiveRef/$dynamicRef 残留，
                    # 部分上游只认识 $ref，残留新关键字同样会被拒。
                    for legacy_key in _REF_KEYS:
                        merged.pop(legacy_key, None)
                    return merged
                # 不可解析的 $ref（外部引用等）：保留引用本身，仅清洗 siblings。
                out: Dict[str, Any] = {}
                for key, value in node.items():
                    if key in _REF_KEYS:
                        out[key] = value
                    else:
                        out[key] = _sanitize(value, ref_stack, obj_stack, depth + 1)
                return out
            return {key: _sanitize(value, ref_stack, obj_stack, depth + 1) for key, value in node.items()}
        if isinstance(node, list):
            return [_sanitize(item, ref_stack, obj_stack, depth + 1) for item in node]
        return node

    # 预置 "#" 压栈，使 "#"(根自引用)在任何嵌套位置都被判定为递归。
    # obj_stack 预置根对象 id，捕获别名环（不同 ref 字符串指向同一对象）。
    result = _sanitize(root, ("#",), (id(root),), 0)
    return result, had_recursion[0]


def _repair_required_arrays(node: Any) -> List[str]:
    """递归补齐 schema 里缺失的 required 键，原地修改，返回补上的键名表。

    部分上游即使 strict=false 也要求：凡有 properties 的对象，其 required
    必须是数组且含全体属性键，否则直接 400
    （"'required' is required ... Missing 'xxx'"）。缺啥补啥。
    """
    added: List[str] = []
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict) and props:
            req = node.get("required")
            if not isinstance(req, list):
                node["required"] = list(props.keys())
                added.extend(props.keys())
            else:
                for key in props.keys():
                    if key not in req:
                        req.append(key)
                        added.append(key)
        for value in node.values():
            added.extend(_repair_required_arrays(value))
    elif isinstance(node, list):
        for item in node:
            added.extend(_repair_required_arrays(item))
    return added


def _deep_clean_schema(schema: Any) -> tuple:
    """深拷贝后清洗单个 schema，返回 (新对象, 是否截断递归, 是否补过 required, 补的键)。"""
    import copy as _copy
    working = _copy.deepcopy(schema)
    cleaned, had = sanitize_json_schema(working)
    target = cleaned if had else working
    added = _repair_required_arrays(target)
    return target, had, bool(added), added


def _downgrade_strict_holders(holders: List[Dict[str, Any]]) -> None:
    """把给定 holder 上的 strict=True 降级为 False（原地）。"""
    for holder in holders:
        if isinstance(holder, dict) and holder.get("strict") is True:
            holder["strict"] = False


def sanitize_chat_tools(tools: Any) -> bool:
    """清洗 Chat Completions 格式 tools，原地修改，返回是否改写过。

    格式：``[{"type": "function", "function": {"name": ..., "parameters": {...}, "strict": ...}}]``
    仅递归截断时返回 True；非递归引用原样保留（省 token，strict 由 force 函数统一降级）。
    """
    if not isinstance(tools, list):
        return False
    modified_any = False
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        func = tool.get("function")
        if not isinstance(func, dict):
            # 容忍扁平写法 {"name": ..., "parameters": ...}
            func = tool
        name = str(func.get("name") or tool.get("name") or "unknown")
        params = func.get("parameters")
        if not isinstance(params, (dict, list)):
            continue
        cleaned_new, had, repaired, added = _deep_clean_schema(params)
        if had or repaired:
            func["parameters"] = cleaned_new
            modified_any = True
            # strict 模式禁止递归：必须降级，否则上游必定 400。
            _downgrade_strict_holders([func, tool])
            refs: List[str] = []
            _collect_refs(params, refs)
            logger.warning(
                "[SCHEMA_SANITIZER] Chat tool '%s' 已处理（递归截断=%s，补required=%s），refs=%s",
                name, had, added, refs,
            )
    return modified_any


def sanitize_responses_tools(tools: Any) -> bool:
    """清洗 Responses 格式 tools，原地修改，返回是否改写过。

    格式：``[{"type": "function", "name": ..., "parameters": {...}, "strict": ...}]``，
    也兼容 ``{"function": {...}}`` 嵌套写法，以及 input_schema/schema/json_schema
    与嵌套 tools 全覆盖（见 sanitize_all_schema_holders_in_responses_tools）。
    """
    return sanitize_all_schema_holders_in_responses_tools(tools)


def sanitize_chat_response_format(response_format: Any) -> bool:
    """清洗 Chat ``response_format``，原地修改，返回是否改写过。

    - ``{"type": "json_schema", "json_schema": {"schema": {...}, "strict": ...}}``
      含递归时：清洗 schema 并把 ``strict`` 置 False；非递归引用原样保留。
    - 其他类型（json_object/text/None）无需处理。
    """
    if not isinstance(response_format, dict):
        return False
    if response_format.get("type") != "json_schema":
        return False
    inner = response_format.get("json_schema")
    if not isinstance(inner, dict):
        return False
    schema = inner.get("schema")
    if not isinstance(schema, (dict, list)):
        return False
    cleaned_new, had, repaired, added = _deep_clean_schema(schema)
    if had or repaired:
        inner["schema"] = cleaned_new
        _downgrade_strict_holders([inner, response_format])
        logger.warning(
            "[SCHEMA_SANITIZER] Chat response_format 已处理（递归截断=%s，补required=%s）",
            had, added,
        )
        return True
    return False


def sanitize_responses_text_format(text: Any) -> bool:
    """清洗 Responses ``text.format``，原地修改，返回是否改写过。

    兼容两种写法：
    - ``{"format": {"type": "json_schema", "schema": {...}, "strict": ...}}``
    - ``{"format": {"type": "json_schema", "json_schema": {"schema": ...}}}``（容错）
    """
    if not isinstance(text, dict):
        return False
    fmt = text.get("format")
    if not isinstance(fmt, dict):
        return False
    if fmt.get("type") != "json_schema":
        return False
    # schema 可能直接在 format 下，也可能包一层 json_schema
    schema_holder: Dict[str, Any] = fmt
    schema_key = "schema"
    if not isinstance(fmt.get("schema"), (dict, list)):
        nested = fmt.get("json_schema")
        if isinstance(nested, dict) and isinstance(nested.get("schema"), (dict, list)):
            schema_holder = nested
            schema_key = "schema"
        else:
            return False
    cleaned_new, had, repaired, added = _deep_clean_schema(schema_holder[schema_key])
    if had or repaired:
        schema_holder[schema_key] = cleaned_new
        _downgrade_strict_holders([fmt, schema_holder])
        logger.warning(
            "[SCHEMA_SANITIZER] Responses text.format 已处理（递归截断=%s，补required=%s）",
            had, added,
        )
        return True
    return False


def _iter_all_tools(tools: Any):
    """产出顶层 tools 及嵌套 tools（含 function 包裹）内的全部 tool 字典。"""
    if not isinstance(tools, list):
        return
    seen = set()
    stack = list(tools)
    while stack:
        node = stack.pop()
        if not isinstance(node, dict) or id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        func = node.get("function")
        if isinstance(func, dict):
            stack.append(func)
        nested = node.get("tools")
        if isinstance(nested, list):
            stack.extend(nested)


def strip_unsupported_search_fields(tools: Any) -> List[str]:
    """去掉非 web_search_preview 工具上的 search_content_types，原地修改，返回动过的工具名。

    上游校验：该字段只允许出现在 web_search_preview 类型工具上，
    其他类型带它直接 400。去掉只是少个搜索过滤条件，不影响调用。
    """
    touched: List[str] = []
    for tool in _iter_all_tools(tools):
        if not isinstance(tool, dict) or "search_content_types" not in tool:
            continue
        if tool.get("type") == "web_search_preview":
            continue
        name = str(tool.get("name") or tool.get("type") or "unknown")
        del tool["search_content_types"]
        touched.append(name)
    if touched:
        logger.warning(
            "[SCHEMA_SANITIZER] 已去掉不支持的 search_content_types，工具=%s",
            touched,
        )
    return touched


def _clean_additional_tools_items(body: Any) -> bool:
    """清洗 input 中 additional_tools 条目内部的工具 schema（顶层 tools 够不着它们）。"""
    if not isinstance(body, dict):
        return False
    changed = False
    input_items = body.get("input")
    if not isinstance(input_items, list):
        return False
    for item in input_items:
        if not (isinstance(item, dict) and item.get("type") == "additional_tools"):
            continue
        inner = item.get("tools")
        if not isinstance(inner, list):
            continue
        if sanitize_all_schema_holders_in_responses_tools(inner):
            changed = True
        for holder_tool in inner:
            if not isinstance(holder_tool, dict):
                continue
            for holder in _iter_schema_holders(holder_tool):
                if isinstance(holder, dict) and holder.get("strict") is True:
                    holder["strict"] = False
                    changed = True
    if changed:
        logger.warning("[SCHEMA_SANITIZER] input 内 additional_tools 已处理")
    return changed


def sanitize_responses_request(body: Any) -> bool:
    """清洗 Responses 请求体（tools + text.format + input 内 additional_tools），原地修改，返回是否改写过。"""
    if not isinstance(body, dict):
        return False
    had = False
    if isinstance(body.get("tools"), list):
        had = sanitize_responses_tools(body["tools"]) or had
    if isinstance(body.get("text"), dict):
        had = sanitize_responses_text_format(body["text"]) or had
    had = _clean_additional_tools_items(body) or had
    if isinstance(body.get("tools"), list):
        if strip_unsupported_search_fields(body["tools"]):
            had = True
    return had


def force_all_strict_false_responses(body: Any) -> int:
    """把 Responses 请求体内所有 function tool / text.format 的 strict=True 置为 False。

    返回降级数量。递归 schema 在非 strict 模式下是允许的，因此这是绕过上游
    “Recursive JSON schemas are not currently supported”最稳的兜底：
    即使清洗逻辑漏掉了某种引用写法，只要 strict 全降级，上游就不再走严格校验。
    原地修改。
    """
    if not isinstance(body, dict):
        return 0
    downgraded = 0
    tools = body.get("tools")
    if isinstance(tools, list):
        # 栈式遍历：覆盖顶层 tool/function 及嵌套 tools（如 MCP 包装工具的 tools[i]）。
        seen = set()
        stack = list(tools)
        while stack:
            node = stack.pop()
            if not isinstance(node, dict) or id(node) in seen:
                continue
            seen.add(id(node))
            if node.get("strict") is True:
                node["strict"] = False
                downgraded += 1
            func = node.get("function")
            if isinstance(func, dict) and id(func) not in seen:
                if func.get("strict") is True:
                    func["strict"] = False
                    downgraded += 1
                seen.add(id(func))
                nested = func.get("tools")
                if isinstance(nested, list):
                    stack.extend(nested)
            nested = node.get("tools")
            if isinstance(nested, list):
                stack.extend(nested)
    text = body.get("text")
    if isinstance(text, dict):
        fmt = text.get("format")
        if isinstance(fmt, dict) and fmt.get("strict") is True:
            fmt["strict"] = False
            downgraded += 1
        nested = fmt.get("json_schema") if isinstance(fmt, dict) else None
        if isinstance(nested, dict) and nested.get("strict") is True:
            nested["strict"] = False
            downgraded += 1
    return downgraded


def force_all_strict_false_chat(body: Any) -> int:
    """把 Chat 请求体内所有 tool / response_format 的 strict=True 置为 False。返回降级数量。"""
    if not isinstance(body, dict):
        return 0
    downgraded = 0
    tools = body.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            holders = [tool]
            func = tool.get("function")
            if isinstance(func, dict):
                holders.append(func)
            for holder in holders:
                if isinstance(holder, dict) and holder.get("strict") is True:
                    holder["strict"] = False
                    downgraded += 1
    rf = body.get("response_format")
    if isinstance(rf, dict) and rf.get("type") == "json_schema":
        inner = rf.get("json_schema")
        if isinstance(inner, dict) and inner.get("strict") is True:
            inner["strict"] = False
            downgraded += 1
    return downgraded


_SCHEMA_HOLDER_KEYS = ("parameters", "input_schema", "schema", "json_schema")


def _iter_schema_holders(tool: Any):
    """产出 tool 内所有可能装 JSON Schema 的 holder（兼容 parameters/input_schema/schema 写法）。"""
    if not isinstance(tool, dict):
        return
    seen = set()
    stack = [tool]
    func = tool.get("function")
    if isinstance(func, dict):
        stack.append(func)
    while stack:
        holder = stack.pop()
        if not isinstance(holder, dict) or id(holder) in seen:
            continue
        seen.add(id(holder))
        yield holder
        nested = holder.get("tools")
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict):
                    stack.append(item)
                    fn = item.get("function")
                    if isinstance(fn, dict):
                        stack.append(fn)


def sanitize_all_schema_holders_in_responses_tools(tools: Any) -> bool:
    """清洗 Responses tools 内所有 schema holder（parameters/input_schema/schema 嵌套全覆盖）。"""
    if not isinstance(tools, list):
        return False
    modified_any = False
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        label = str(tool.get("name") or (tool.get("function") or {}).get("name") if isinstance(tool.get("function"), dict) else tool.get("name") or "unknown")
        for holder in _iter_schema_holders(tool):
            for skey in _SCHEMA_HOLDER_KEYS:
                schema = holder.get(skey)
                if skey == "json_schema" and isinstance(schema, dict) and isinstance(schema.get("schema"), (dict, list)):
                    cleaned_new, had, repaired, added = _deep_clean_schema(schema["schema"])
                    if had or repaired:
                        schema["schema"] = cleaned_new
                        modified_any = True
                        _downgrade_strict_holders([holder, tool])
                        logger.warning(
                            "[SCHEMA_SANITIZER] Responses tool '%s' 的 %s 已处理（递归截断=%s，补required=%s）",
                            label, f"{skey}.schema", had, added,
                        )
                    continue
                if not isinstance(schema, (dict, list)):
                    continue
                cleaned_new, had, repaired, added = _deep_clean_schema(schema)
                if had or repaired:
                    holder[skey] = cleaned_new
                    modified_any = True
                    _downgrade_strict_holders([holder, tool])
                    refs: list = []
                    _collect_refs(schema, refs)
                    logger.warning(
                        "[SCHEMA_SANITIZER] Responses tool '%s' 的 %s 已处理（递归截断=%s，补required=%s），refs=%s",
                        label, skey, had, added, refs,
                    )
    return modified_any


def sanitize_chat_request(body: Any) -> bool:
    """清洗 Chat Completions 请求体（tools + response_format），原地修改，返回是否改写过。"""
    if not isinstance(body, dict):
        return False
    had = False
    if isinstance(body.get("tools"), list):
        had = sanitize_chat_tools(body["tools"]) or had
    if isinstance(body.get("response_format"), dict):
        had = sanitize_chat_response_format(body["response_format"]) or had
    return had


__all__ = [
    "sanitize_json_schema",
    "sanitize_chat_tools",
    "sanitize_responses_tools",
    "sanitize_chat_response_format",
    "sanitize_responses_text_format",
    "sanitize_responses_request",
    "sanitize_chat_request",
]
