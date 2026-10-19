"""保存思考分隔符切分前的完整内容，供客户端丢弃思考后还原历史。

上游把思考直接写在正文里时，网关按 thinking_separator 拆成思考与正文。
部分客户端只把正文放进后续请求的历史，思考被丢弃；重新发给上游时模型
看到的上一轮内容和它实际生成的不一致。这里按「正文 → 切分前原文」保存
映射，历史里出现只有正文的助手消息时拼回原文，并作为普通正文回传：
上游原本就是把这段内容作为文本生成的，不能补造成没有签名的 thought。
"""
import hashlib
import json
import logging

from core.conversation_store import conversation_store
from core.request_context import current_request, endpoint_identity

logger = logging.getLogger(__name__)

VERSION = 'thinking-split-v1'


def _identity():
    context = current_request.get()
    if not context or not context.authenticated or not context.owner_id:
        return None, None
    value = [VERSION, context.model, endpoint_identity(context.endpoint),
             context.credential_fingerprint]
    scope = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return context.owner_id, scope


def _body_key(body: str) -> str:
    return hashlib.sha256(body.strip().encode('utf-8')).hexdigest()


def remember_split_original(body: str, original: str) -> None:
    """保存一次切分：body 是下发给客户端的正文，original 是上游完整原文。"""
    owner, scope = _identity()
    if (not owner or not isinstance(body, str) or not isinstance(original, str)):
        return
    body = body.strip()
    if not body or not original or len(original) <= len(body):
        return
    try:
        conversation_store.remember_response_prefixes(
            owner, scope, [_body_key(body)],
            {'body': body, 'original': original})
        logger.info(
            f"[THINKING_SPLIT_CACHE] 已保存切分前原文 "
            f"(正文 {len(body)} 字符, 原文 {len(original)} 字符)")
    except Exception:
        logger.exception('思考切分原文缓存保存失败；不影响本次响应')


def _original_from_record(record, body: str):
    if not isinstance(record, dict):
        return None
    original = record.get('original')
    stored_body = record.get('body')
    if not isinstance(original, str) or not original:
        return None
    if not isinstance(stored_body, str) or stored_body != body.strip():
        return None
    return original


def lookup_split_original(body: str):
    """按正文查找切分前原文；没有记录或记录有歧义时返回 None。"""
    owner, scope = _identity()
    if not owner or not isinstance(body, str) or not body.strip():
        return None
    try:
        key = _body_key(body)
        record = conversation_store.response_prefixes(owner, scope, [key]).get(key)
    except Exception:
        logger.exception('思考切分原文缓存读取失败；按客户端原始历史请求上游')
        return None
    return _original_from_record(record, body)


def restore_history_messages(messages):
    """把只剩正文的助手历史拼回切分前原文，返回新的消息列表。

    带原生 parts（签名思考/工具调用）或带 reasoning_content 的历史保持
    原样：前者已经能完整回传，后者交给各自的转换逻辑处理。
    """
    if not isinstance(messages, list):
        return messages
    candidates = []
    for index, msg in enumerate(messages):
        if not isinstance(msg, dict) or msg.get('role') != 'assistant':
            continue
        metadata = msg.get('provider_metadata')
        has_native = isinstance(metadata, dict) and (
            metadata.get('gemini_parts') or metadata.get('interactions_steps'))
        content = msg.get('content')
        if (not has_native and not msg.get('reasoning_content')
                and isinstance(content, str) and content.strip()):
            candidates.append((index, content))

    owner, scope = _identity()
    if not owner or not candidates:
        return messages
    keys = [_body_key(content) for _, content in candidates]
    try:
        records = conversation_store.response_prefixes(owner, scope, keys)
    except Exception:
        logger.exception('思考切分原文缓存读取失败；按客户端原始历史请求上游')
        return messages

    result = list(messages)
    restored = 0
    for index, content in candidates:
        original = _original_from_record(records.get(_body_key(content)), content)
        if original:
            result[index] = {**messages[index], 'content': original}
            restored += 1
    if restored:
        logger.info(f"[THINKING_SPLIT_CACHE] 已从缓存拼回 {restored} 条助手历史")
    return result
