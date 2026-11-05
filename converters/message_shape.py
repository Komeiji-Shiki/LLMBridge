"""上游不接受的消息形状统一在这里改写，各协议的转换器只处理规整后的消息。"""
import copy
from typing import Any, Dict, Iterable, List, Optional

from core.conversion_report import note_loss

SYSTEM_ROLES = ('system', 'developer')
# 端点配置 mid_system_mode：inline 留在原位（默认），hoist 并入顶层系统提示（旧行为）。
MID_SYSTEM_HOIST = 'hoist'
_CONTINUE_PROMPT = ('Continue the text below from exactly where it ends. '
                    'Output only the continuation and do not repeat any of it.\n\n')


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(str(part.get('text', '')) for part in content if isinstance(part, dict)
                       and part.get('type') in ('text', 'input_text', 'output_text'))
    return ''


def _as_reminder(text: str) -> str:
    # 已经带标签的提醒（如 Claude Code 的 system-reminder）不再包一层。
    return text if text.lstrip().startswith('<') else f'<system-reminder>\n{text}\n</system-reminder>'


def inline_mid_system(messages: Iterable[Dict[str, Any]], mode: Optional[str] = None,
                      role: str = 'user') -> List[Dict[str, Any]]:
    """开头的系统消息留给顶层字段，对话中段的留在原位。

    搬到顶层会让系统区随提醒变化，整段缓存前缀跟着失效。没有中段系统角色的上游
    转成 user 文本（role='user'），有的上游保留角色（如 Responses 的 developer）。
    提醒落在工具调用和结果之间时顺延到这批结果之后，不破坏配对。
    """
    messages = list(messages)
    if mode == MID_SYSTEM_HOIST:
        return messages
    result, deferred, pending_calls = [], [], set()
    conversation_started = False
    for message in messages:
        kind = message.get('role') if isinstance(message, dict) else None
        if kind in SYSTEM_ROLES and not conversation_started:
            result.append(message)
            continue
        if kind in SYSTEM_ROLES:
            text = message_text(message.get('content'))
            if text.strip():
                content = _as_reminder(text) if role == 'user' else text
                (deferred if pending_calls else result).append({'role': role, 'content': content})
            continue
        conversation_started = True
        result.append(message)
        if kind == 'assistant':
            pending_calls = {call.get('id') for call in message.get('tool_calls') or []
                             if isinstance(call, dict)}
        elif kind == 'tool':
            pending_calls.discard(message.get('tool_call_id'))
        if not pending_calls and deferred:
            result.extend(deferred)
            deferred = []
    result.extend(deferred)
    return result


def rewrite_trailing_prefill(messages: Iterable[Dict[str, Any]], prefill_supported: bool) -> List[Dict[str, Any]]:
    """新模型拒绝以 assistant 结尾的请求：改成续写指令，空的末尾轮次直接去掉。"""
    messages = list(messages)
    if prefill_supported or not messages or not isinstance(messages[-1], dict):
        return messages
    last = messages[-1]
    if last.get('role') != 'assistant' or last.get('tool_calls'):
        return messages
    text = message_text(last.get('content'))
    messages = messages[:-1]
    if text.strip():
        note_loss('messages[-1]', '模型不支持预填充，末尾 assistant 已改为续写指令')
        messages.append({'role': 'user', 'content': _CONTINUE_PROMPT + text})
    return messages


def merge_assistant_items(messages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """合并相邻的 assistant 消息，让同一轮的思考、正文和并行调用落在一条消息上。

    Responses 的 input 把它们拆成多个独立项；Chat 和 Gemini 都要求一轮调用后紧跟全部结果。
    """
    merged: List[Dict[str, Any]] = []
    for message in messages:
        previous = merged[-1] if merged else None
        if not (isinstance(message, dict) and message.get('role') == 'assistant'
                and isinstance(previous, dict) and previous.get('role') == 'assistant'):
            merged.append(message)
            continue
        target = copy.copy(previous)
        previous_text, text = target.get('content'), message.get('content')
        if text:
            if not previous_text:
                target['content'] = text
            elif isinstance(previous_text, str) and isinstance(text, str):
                target['content'] = previous_text + text
            else:
                as_parts = lambda value: value if isinstance(value, list) else [{'type': 'text', 'text': value}]
                target['content'] = [*as_parts(previous_text), *as_parts(text)]
        if message.get('tool_calls'):
            target['tool_calls'] = [*(target.get('tool_calls') or []), *message['tool_calls']]
        reasoning = message.get('reasoning_content')
        if reasoning:
            target['reasoning_content'] = (target.get('reasoning_content') or '') + reasoning
        metadata = message.get('provider_metadata')
        if isinstance(metadata, dict):
            existing = dict(target.get('provider_metadata') or {})
            for key, value in metadata.items():
                existing[key] = [*existing[key], *value] if isinstance(value, list) and isinstance(existing.get(key), list) else value
            target['provider_metadata'] = existing
        merged[-1] = target
    return merged
