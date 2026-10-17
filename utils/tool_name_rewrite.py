"""按模型开关的 Anthropic 工具名改写。

部分中转站会按工具名识别"已知工具"（例如 read_file），并把客户端声明的
input_schema 替换成自己的参数格式（如只剩 file_path），导致客户端真实的
path / startLine / endLine / files 参数全部丢失。system 提示词无法修复，
因为改写发生在中转站服务端。

开启 tool_name_rewrite 后，仅对客户端在 tools 中声明的原名生效：
- 请求侧：tools[].name、tool_choice.name、历史 assistant tool_use.name
  改为别名；system 与工具描述中独立出现的原名一并替换，避免模型被引导调用
  未声明的原名工具。客户端没声明的工具不会被中转站拦截，system 里的同名
  文本（例如代码片段 def read_file）保持原样。
- 响应侧：tool_use.name 从别名恢复为原名，客户端完全感知不到改写。

配置（endpoint_config）：
  "tool_name_rewrite": true
  "tool_name_rewrite_map": {"read_file": "read_workspace_file"}   # 可选，提供时整体替换默认映射
"""
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 已实测会被中转站按名称拦截改写参数的工具
DEFAULT_TOOL_NAME_REWRITE_MAP: Dict[str, str] = {
    "read_file": "read_workspace_file",
}

# Anthropic 工具名约束：^[a-zA-Z0-9_-]{1,128}$
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def resolve_tool_name_rewrite_map(endpoint_config: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """返回 原名 → 别名 映射；开关未开启或映射无效时返回空字典。"""
    if not isinstance(endpoint_config, dict) or endpoint_config.get("tool_name_rewrite") is not True:
        return {}

    raw_map = endpoint_config.get("tool_name_rewrite_map")
    if raw_map is None:
        raw_map = DEFAULT_TOOL_NAME_REWRITE_MAP
    elif not isinstance(raw_map, dict):
        logger.warning("[TOOL_NAME_REWRITE] tool_name_rewrite_map 必须是对象，已改用默认映射")
        raw_map = DEFAULT_TOOL_NAME_REWRITE_MAP

    mapping: Dict[str, str] = {}
    used_aliases = set()
    for original, alias in raw_map.items():
        valid = (
            isinstance(original, str) and isinstance(alias, str)
            and _TOOL_NAME_PATTERN.match(original) and _TOOL_NAME_PATTERN.match(alias)
            and original != alias
        )
        if not valid:
            logger.warning(f"[TOOL_NAME_REWRITE] 忽略无效映射: {original!r} → {alias!r}")
            continue
        if alias in used_aliases or alias in raw_map:
            # 别名重复或别名本身又是另一个原名，反向恢复会产生歧义
            logger.warning(f"[TOOL_NAME_REWRITE] 忽略有歧义的映射: {original!r} → {alias!r}")
            continue
        used_aliases.add(alias)
        mapping[original] = alias
    return mapping


def _name_pattern(names: List[str]) -> re.Pattern:
    # 仅以 ASCII 标识符字符判断边界：中文紧贴工具名时仍能替换，
    # 而 my_read_file、read_file_v2 这类更长的标识符不会被误伤
    alternation = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9_-])(?:{alternation})(?![A-Za-z0-9_-])")


def _replace_text(text: Any, pattern: re.Pattern, mapping: Dict[str, str]) -> Any:
    if not isinstance(text, str) or not text:
        return text
    return pattern.sub(lambda m: mapping[m.group(0)], text)


def _rewrite_system(system: Any, pattern: re.Pattern, mapping: Dict[str, str]) -> Any:
    if isinstance(system, str):
        return _replace_text(system, pattern, mapping)
    if not isinstance(system, list):
        return system
    changed = False
    blocks = []
    for block in system:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            new_text = _replace_text(block["text"], pattern, mapping)
            if new_text != block["text"]:
                block = {**block, "text": new_text}
                changed = True
        blocks.append(block)
    return blocks if changed else system


def apply_tool_name_rewrite(body: Dict[str, Any], endpoint_config: Optional[Dict[str, Any]],
                            log_tag: str = "TOOL_NAME_REWRITE") -> Dict[str, str]:
    """就地改写 Anthropic 请求体中的工具名，返回 别名 → 原名 的恢复映射。

    只替换被改写字段所在的对象，不修改客户端原始的 tools / messages 对象，
    监控记录中的原始消息保持客户端视角。
    """
    configured = resolve_tool_name_rewrite_map(endpoint_config)
    if not configured or not isinstance(body, dict):
        return {}

    tools = body.get("tools")
    declared = {t.get("name") for t in tools if isinstance(t, dict)} if isinstance(tools, list) else set()

    mapping: Dict[str, str] = {}
    for original, alias in configured.items():
        if original not in declared:
            continue
        if alias in declared:
            logger.warning(f"[{log_tag}] 别名 {alias!r} 与客户端已有工具同名，跳过 {original!r} 的改写")
            continue
        mapping[original] = alias
    if not mapping:
        return {}

    pattern = _name_pattern(list(mapping))

    if isinstance(tools, list):
        new_tools = []
        for tool in tools:
            if isinstance(tool, dict):
                updates = {}
                if tool.get("name") in mapping:
                    updates["name"] = mapping[tool["name"]]
                desc = tool.get("description")
                new_desc = _replace_text(desc, pattern, mapping)
                if new_desc != desc:
                    updates["description"] = new_desc
                if updates:
                    tool = {**tool, **updates}
            new_tools.append(tool)
        body["tools"] = new_tools

    tool_choice = body.get("tool_choice")
    if isinstance(tool_choice, dict) and tool_choice.get("name") in mapping:
        body["tool_choice"] = {**tool_choice, "name": mapping[tool_choice["name"]]}

    if "system" in body:
        body["system"] = _rewrite_system(body["system"], pattern, mapping)

    messages = body.get("messages")
    if isinstance(messages, list):
        new_messages = []
        for msg in messages:
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in mapping
                    for b in content):
                new_content = [
                    {**b, "name": mapping[b["name"]]}
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in mapping
                    else b
                    for b in content
                ]
                msg = {**msg, "content": new_content}
            new_messages.append(msg)
        body["messages"] = new_messages

    logger.info(f"[{log_tag}] 工具名改写已启用: {mapping}")
    return {alias: original for original, alias in mapping.items()}


def restore_tool_names_in_response(response_json: Any, restore_map: Dict[str, str]) -> bool:
    """把 Anthropic 非流式响应 content 中 tool_use 的别名恢复为原名（就地修改）。"""
    if not restore_map or not isinstance(response_json, dict):
        return False
    content = response_json.get("content")
    if not isinstance(content, list):
        return False
    changed = False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in restore_map:
            block["name"] = restore_map[block["name"]]
            changed = True
    return changed


def restore_tool_name(name: Any, restore_map: Optional[Dict[str, str]]) -> Any:
    """单个工具名恢复（流式 content_block_start 使用）。"""
    if restore_map and isinstance(name, str):
        return restore_map.get(name, name)
    return name


def describe_restore_map(restore_map: Dict[str, str]) -> Optional[Dict[str, str]]:
    """监控日志展示用：原名 → 别名。"""
    if not restore_map:
        return None
    return {original: alias for alias, original in restore_map.items()}


__all__ = [
    "DEFAULT_TOOL_NAME_REWRITE_MAP",
    "resolve_tool_name_rewrite_map",
    "apply_tool_name_rewrite",
    "restore_tool_names_in_response",
    "restore_tool_name",
    "describe_restore_map",
]
