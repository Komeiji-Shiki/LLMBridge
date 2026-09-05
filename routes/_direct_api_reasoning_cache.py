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
