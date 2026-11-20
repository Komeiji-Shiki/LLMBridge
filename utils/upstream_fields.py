"""按端点配置改写发往上游的请求：排除不支持的字段，以及 upstream_rules 改写规则。

upstream_rules 是规则数组，在协议转换之后、发送之前作用于真正发往上游的请求体，按顺序执行：

    {"set": "generation_config.temperature", "value": 0.7}   设值，路径按 . 分隔，数字是数组下标
    {"delete": "tools.*.input_schema.$schema"}                删除，* 匹配这一层的全部元素
    {"drop_tools": ["WebSearch", "mcp__*"]}                   按名称通配删除工具定义
    {"replace": "正则", "with": "替换文本"}                     改对话和系统提示里的文本
    {"replace": "正则", "with": "替换文本", "in": "body"}       改整份 JSON 文本，结果必须仍是合法 JSON

每条规则可以带 "callers": ["调用方 ID 或名称", ...]，只对这些调用方生效。
规则写错时跳过这一条并记日志，不影响请求本身。
"""

import fnmatch
import json
import logging
import re

logger = logging.getLogger(__name__)

# 文本替换只碰这些键下的字符串：对话内容和系统提示。工具定义、图片数据和参数不受影响
_TEXT_KEYS = frozenset({'text', 'content', 'system', 'instructions', 'system_instruction', 'input'})
_SKIP_KEYS = frozenset({'tools', 'tool_choice', 'toolConfig'})
_RULE_KINDS = ('set', 'delete', 'drop_tools', 'replace')


def configured_omissions(config: dict) -> list:
    fields = config.get("omit_request_fields", [])
    if not isinstance(fields, list) or not all(isinstance(field, str) and field for field in fields):
        raise ValueError("omit_request_fields 必须是非空字段名组成的数组。")
    return fields


def omit_configured_fields(body: dict, config: dict) -> dict:
    omitted = [field for field in configured_omissions(config) if field in body]
    for field in omitted:
        del body[field]
    if omitted:
        logger.info("[UPSTREAM_FIELDS] 端点配置不向上游发送字段: %s", ", ".join(omitted))
    return body


def omission_warnings(request: dict, config: dict) -> list:
    return [{"field": field, "applied": False,
             "message": "模型端点配置指定此字段不发送到上游。"}
            for field in configured_omissions(config) if field in request]


# ---------------------------------------------------------------- 改写规则

def validate_rules(rules) -> list:
    """检查规则格式，返回原数组；格式不对时抛 ValueError，供保存配置时提示。"""
    if not isinstance(rules, list):
        raise ValueError('upstream_rules 必须是数组')
    for index, rule in enumerate(rules):
        where = f'upstream_rules 第 {index + 1} 条'
        kinds = [kind for kind in _RULE_KINDS if isinstance(rule, dict) and kind in rule]
        if len(kinds) != 1:
            raise ValueError(f'{where} 必须且只能含 set / delete / drop_tools / replace 之一')
        kind = kinds[0]
        if kind in ('set', 'delete') and not (isinstance(rule[kind], str) and rule[kind]):
            raise ValueError(f'{where} 的路径必须是非空字符串')
        if kind == 'set' and 'value' not in rule:
            raise ValueError(f'{where} 缺少 value')
        if kind == 'drop_tools' and not (isinstance(rule[kind], list) and rule[kind]
                                         and all(isinstance(name, str) and name for name in rule[kind])):
            raise ValueError(f'{where} 的 drop_tools 必须是非空的工具名数组')
        if kind == 'replace':
            try:
                re.compile(rule['replace'])
            except (re.error, TypeError) as error:
                raise ValueError(f'{where} 的正则无效: {error}') from error
            if not isinstance(rule.get('with', ''), str) or rule.get('in', 'text') not in ('text', 'body'):
                raise ValueError(f'{where} 的 with 必须是字符串，in 只能是 text 或 body')
        callers = rule.get('callers')
        if callers is not None and not (isinstance(callers, list) and all(isinstance(c, str) for c in callers)):
            raise ValueError(f'{where} 的 callers 必须是字符串数组')
    return rules


def _split(path: str) -> list:
    return [int(part) if part.isdigit() else part for part in path.split('.')]


def _walk(node, parts):
    """按路径找到父节点，产出 (父节点, 最后一段)；* 展开为这一层的全部元素。"""
    if not parts:
        return
    head, rest = parts[0], parts[1:]
    if head == '*':
        children = list(node.items()) if isinstance(node, dict) else list(enumerate(node)) if isinstance(node, list) else []
        for key, child in children:
            if rest:
                yield from _walk(child, rest)
            else:
                yield node, key
        return
    if not rest:
        yield node, head
        return
    if isinstance(node, dict) and head in node:
        yield from _walk(node[head], rest)
    elif isinstance(node, list) and isinstance(head, int) and head < len(node):
        yield from _walk(node[head], rest)


def _set(body, path, value) -> int:
    parts, changed = _split(path), 0
    # 中间缺的对象自动补上，数组不自动补
    node = body
    for part in parts[:-1]:
        if part == '*':
            break
        if isinstance(node, dict):
            node = node.setdefault(part, {})
        elif isinstance(node, list) and isinstance(part, int) and part < len(node):
            node = node[part]
        else:
            return 0
    for parent, key in _walk(body, parts):
        if isinstance(parent, dict) or (isinstance(parent, list) and isinstance(key, int) and key < len(parent)):
            parent[key] = json.loads(json.dumps(value))
            changed += 1
    return changed


def _delete(body, path) -> int:
    targets = list(_walk(body, _split(path)))
    removed = 0
    # 从后往前删，数组下标不会因为前面删掉元素而错位
    for parent, key in reversed(targets):
        if isinstance(parent, dict) and key in parent:
            del parent[key]
            removed += 1
        elif isinstance(parent, list) and isinstance(key, int) and key < len(parent):
            del parent[key]
            removed += 1
    return removed


def _tool_name(tool) -> str:
    if not isinstance(tool, dict):
        return ''
    function = tool.get('function') if isinstance(tool.get('function'), dict) else {}
    return tool.get('name') or function.get('name') or tool.get('type') or ''


def _drop_tools(body, patterns) -> list:
    tools = body.get('tools')
    if not isinstance(tools, list):
        return []
    matches = lambda name: any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)
    dropped, kept = [], []
    for tool in tools:
        declarations = tool.get('functionDeclarations') if isinstance(tool, dict) else None
        if isinstance(declarations, list):
            # Gemini 把多个函数放在同一个工具里
            remaining = [item for item in declarations if not matches(_tool_name(item))]
            dropped += [_tool_name(item) for item in declarations if matches(_tool_name(item))]
            if remaining:
                kept.append({**tool, 'functionDeclarations': remaining})
            elif len(tool) > 1:
                kept.append({key: value for key, value in tool.items() if key != 'functionDeclarations'})
        elif matches(_tool_name(tool)):
            dropped.append(_tool_name(tool))
        else:
            kept.append(tool)
    if not dropped:
        return []
    if kept:
        body['tools'] = kept
    else:
        del body['tools']
    # 指定使用某个被删工具的 tool_choice 一并去掉，否则上游会拒绝
    choice = body.get('tool_choice')
    if isinstance(choice, dict) and (_tool_name(choice) in dropped or not kept):
        del body['tool_choice']
    return dropped


def _replace_text(node, pattern, replacement, key=None) -> int:
    """原地替换文本，返回发生替换的字符串个数。"""
    count = 0
    if isinstance(node, dict):
        for child_key, value in node.items():
            if child_key in _SKIP_KEYS:
                continue
            if isinstance(value, str):
                if child_key in _TEXT_KEYS:
                    replaced = pattern.sub(replacement, value)
                    if replaced != value:
                        node[child_key] = replaced
                        count += 1
            else:
                count += _replace_text(value, pattern, replacement, child_key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            if isinstance(value, str):
                # 字符串数组只在文本键下才算文本（如 system: ["..."]）
                if key in _TEXT_KEYS:
                    replaced = pattern.sub(replacement, value)
                    if replaced != value:
                        node[index] = replaced
                        count += 1
            else:
                count += _replace_text(value, pattern, replacement, key)
    return count


def _caller_matches(rule, context) -> bool:
    callers = rule.get('callers')
    if not callers:
        return True
    return context is not None and (context.owner_id in callers or context.owner_name in callers)


def apply_upstream_rules(body: dict, config: dict, context=None) -> list:
    """按 config['upstream_rules'] 原地改写 body，返回实际生效的规则说明，没有规则时返回空数组。"""
    rules = (config or {}).get('upstream_rules')
    if not rules:
        return []
    try:
        validate_rules(rules)
    except ValueError as error:
        logger.warning('[UPSTREAM_RULES] 规则格式不对，本次全部跳过: %s', error)
        return []
    applied = []
    for rule in rules:
        if not _caller_matches(rule, context):
            continue
        if 'set' in rule:
            if _set(body, rule['set'], rule['value']):
                applied.append(f"set {rule['set']}")
        elif 'delete' in rule:
            if _delete(body, rule['delete']):
                applied.append(f"delete {rule['delete']}")
        elif 'drop_tools' in rule:
            dropped = _drop_tools(body, rule['drop_tools'])
            if dropped:
                applied.append('drop_tools ' + ', '.join(dropped))
        elif rule.get('in', 'text') == 'body':
            text = json.dumps(body, ensure_ascii=False)
            replaced = re.sub(rule['replace'], rule.get('with', ''), text)
            if replaced == text:
                continue
            try:
                rewritten = json.loads(replaced)
            except ValueError:
                logger.warning('[UPSTREAM_RULES] 整体替换 %r 后不再是合法 JSON，已跳过', rule['replace'])
                continue
            body.clear()
            body.update(rewritten)
            applied.append(f"replace body {rule['replace']}")
        elif _replace_text(body, re.compile(rule['replace']), rule.get('with', '')):
            applied.append(f"replace {rule['replace']}")
    if applied:
        logger.info('[UPSTREAM_RULES] 已按端点规则改写上游请求: %s', '; '.join(applied))
    return applied
