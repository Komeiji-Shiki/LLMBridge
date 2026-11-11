"""按渠道设置清理 Gemini 系统提示词中的客户端身份前缀。"""
import logging

logger = logging.getLogger(__name__)

_CLAUDE_AGENT_SDK_PREFIX = "You are a Claude agent, built on Anthropic's Claude Agent SDK."


def strip_claude_agent_sdk_prefix(body: dict, config: dict) -> dict:
    """只删除系统提示开头的固定句子，保留其余文本和内容块属性。"""
    if not config.get('strip_claude_agent_sdk_prefix'):
        return body

    # generateContent 接受两种字段拼写；Interactions 的同名字段直接使用字符串。
    for field in ('systemInstruction', 'system_instruction'):
        instruction = body.get(field)
        if isinstance(instruction, str):
            text = instruction
            parts = None
        elif isinstance(instruction, dict) and isinstance(instruction.get('parts'), list):
            parts = instruction['parts']
            for index, part in enumerate(parts):
                text = part.get('text') if isinstance(part, dict) else None
                if not isinstance(text, str) or text.strip():
                    break
            else:
                continue
        else:
            continue

        if not isinstance(text, str) or not text.lstrip().startswith(_CLAUDE_AGENT_SDK_PREFIX):
            continue
        remaining = text.lstrip()[len(_CLAUDE_AGENT_SDK_PREFIX):].lstrip()
        if parts is None:
            if remaining:
                body[field] = remaining
            else:
                del body[field]
        else:
            # 句子单独占一块时删除该块，避免发送空 text；其余块维持原顺序。
            updated = ([{**parts[index], 'text': remaining}] if remaining else []) + parts[index + 1:]
            if updated:
                body[field] = {**instruction, 'parts': updated}
            else:
                del body[field]
        logger.info('[SYSTEM_PROMPT] 已移除 Gemini 系统提示开头的 Claude Agent SDK 身份句')
    return body
