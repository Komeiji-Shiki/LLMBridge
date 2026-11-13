"""清理发往 Gemini 的系统提示词中的 Claude Agent SDK 身份句。"""
import logging
import re

from services.provider_capabilities import model_family

logger = logging.getLogger(__name__)

_CLAUDE_AGENT_SDK_PREFIX = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
_CLAUDE_AGENT_SDK_LINE = re.compile(
    r'^[ \t]*' + re.escape(_CLAUDE_AGENT_SDK_PREFIX) + r'(?=\s|$)[ \t]*(?:\r?\n)?', re.MULTILINE)


def _targets_gemini(config: dict, alias: str) -> bool:
    if config.get('model_family'):
        return model_family(config) == 'gemini'
    target = str(config.get('model_id') or alias).lower()
    # 有些中转把 Claude 命名为 gemini-claude-*，Gemma 也能使用 Gemini 原生协议。
    if 'claude-' in target or 'gemma-' in target:
        return False
    return config.get('api_type') == 'gemini_native' or model_family(config, target) == 'gemini'


def _strip_instruction(instruction):
    if isinstance(instruction, str):
        if _CLAUDE_AGENT_SDK_PREFIX in instruction:
            return _CLAUDE_AGENT_SDK_LINE.sub('', instruction)
        return instruction
    if isinstance(instruction, dict) and isinstance(instruction.get('parts'), list):
        parts = instruction['parts']
        cleaned = _strip_instruction(parts)
        if cleaned != parts:
            return {**instruction, 'parts': cleaned} if cleaned else None
    if not isinstance(instruction, list):
        return instruction

    # Claude Code 的计费头可能在身份句前面。只删除命中的文本，保留其他块及其属性。
    cleaned = []
    for block in instruction:
        value = block.get('text') if isinstance(block, dict) else None
        text = _strip_instruction(value) if isinstance(value, str) else value
        if text == value:
            cleaned.append(block)
        elif text.strip():
            cleaned.append({**block, 'text': text})
    return cleaned


def strip_claude_agent_sdk_prefix(body: dict, config: dict, alias: str, *,
                                  fields=('systemInstruction', 'system_instruction')) -> None:
    """就地清理系统提示，固定身份句可识别经中转转发的 Claude Code 请求。"""
    if not any(field in body for field in fields) or not _targets_gemini(config, alias):
        return

    for field in fields:
        original = body.get(field)
        cleaned = _strip_instruction(original)
        if cleaned == original:
            continue
        if cleaned and (not isinstance(cleaned, str) or cleaned.strip()):
            body[field] = cleaned
        else:
            del body[field]
        logger.info('[SYSTEM_PROMPT] 已移除 Gemini 系统提示中的 Claude Agent SDK 身份句')
