"""Restore exact OpenRouter reasoning signatures within a scoped conversation."""
import hashlib
import logging
from collections import OrderedDict

logger = logging.getLogger(__name__)


def _text_key(reasoning_text: str) -> str:
    return hashlib.sha256((reasoning_text or '').encode('utf-8')).hexdigest()


def store_reasoning_details(reasoning_text: str, reasoning_details: list) -> None:
    from core.request_context import current_request, endpoint_identity
    from core.conversation_store import conversation_store
    context = current_request.get()
    if not context or not context.authenticated or not reasoning_details or not reasoning_text:
        return
    try:
        session = context.cache_session()
        conversation_store.touch(context.owner_id, session, context.model, endpoint_identity(context.endpoint), context.credential_fingerprint)
        conversation_store.put(context.owner_id, session, 'reasoning:' + _text_key(reasoning_text), reasoning_details)
    except Exception:
        logger.exception('思考签名缓存保存失败，客户端原始响应仍继续返回')


def lookup_reasoning_details(reasoning_text: str):
    from core.request_context import current_request
    from core.conversation_store import conversation_store
    context = current_request.get()
    if not context or not context.authenticated or not reasoning_text:
        return None
    try:
        return conversation_store.get(context.owner_id, context.cache_session(), 'reasoning:' + _text_key(reasoning_text))
    except Exception:
        logger.exception('思考签名缓存读取失败，保持客户端请求原样')
        return None


def store_anthropic_thinking(blocks: list) -> None:
    """按工具调用 ID 保存同一轮的思考块，工具结果回传时要原样带回。

    工具调用 ID 是 Chat 客户端一定会回传的字段，而思考块和 provider_metadata 经常被丢掉。
    """
    from core.request_context import current_request, endpoint_identity
    from core.conversation_store import conversation_store
    context = current_request.get()
    thinking = [block for block in blocks if block.get('type') in ('thinking', 'redacted_thinking')]
    tool_ids = [block.get('id') for block in blocks if block.get('type') == 'tool_use' and block.get('id')]
    if not context or not context.authenticated or not thinking or not tool_ids:
        return
    try:
        session = context.cache_session()
        conversation_store.touch(context.owner_id, session, context.model, endpoint_identity(context.endpoint), context.credential_fingerprint)
        for tool_id in tool_ids:
            conversation_store.put(context.owner_id, session, 'anthropic_thinking:' + tool_id, thinking)
    except Exception:
        logger.exception('思考块保存失败，客户端原始响应仍继续返回')


def restore_anthropic_thinking(messages: list) -> list:
    """给当前工具循环里缺思考块的 assistant 消息补回原始块，返回新的消息列表。

    只处理最后一条 user 消息之后的轮次：更早的思考块 Anthropic 会自行忽略。
    """
    from core.request_context import current_request
    from core.conversation_store import conversation_store
    context = current_request.get()
    if not context or not context.authenticated or not isinstance(messages, list):
        return messages
    start = next((index + 1 for index in range(len(messages) - 1, -1, -1)
                  if isinstance(messages[index], dict) and messages[index].get('role') == 'user'), 0)
    restored = list(messages)
    for index in range(start, len(messages)):
        message = messages[index]
        if not isinstance(message, dict) or message.get('role') != 'assistant' or not message.get('tool_calls'):
            continue
        metadata = message.get('provider_metadata')
        if isinstance(metadata, dict) and metadata.get('anthropic_blocks'):
            continue
        tool_id = next((call.get('id') for call in message['tool_calls'] if isinstance(call, dict) and call.get('id')), None)
        try:
            blocks = conversation_store.get(context.owner_id, context.cache_session(), 'anthropic_thinking:' + tool_id) if tool_id else None
        except Exception:
            logger.exception('思考块读取失败，保持客户端请求原样')
            continue
        if blocks:
            restored[index] = {**message, 'provider_metadata': {**(metadata or {}), 'anthropic_blocks': blocks}}
    return restored


def merge_reasoning_detail_chunks(chunks: list) -> list:
    """合并流式下发的 reasoning_details 增量分片。

    OpenRouter 流式响应会把 reasoning_details 拆成多个 delta 分片：
    文本类字段（text/summary/data）逐段追加，signature 通常在最后的分片才出现。
    按 (index, type, format) 分组合并，输出顺序与分片首次出现顺序一致。
    """
    merged = OrderedDict()
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        key = (chunk.get("index", 0), chunk.get("type"), chunk.get("format"))
        entry = merged.get(key)
        if entry is None:
            merged[key] = dict(chunk)
            continue
        for field in ("text", "summary", "data"):
            increment = chunk.get(field)
            if increment:
                entry[field] = (entry.get(field) or "") + increment
        for field, value in chunk.items():
            if field in ("text", "summary", "data") or value in (None, ""):
                continue
            entry[field] = value
    return list(merged.values())
