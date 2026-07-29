"""
Direct API - OpenRouter reasoning_details 缓存

Anthropic 系模型经 OpenRouter 输出的思考链（thinking 块）带有加密签名（signature），
下一轮对话必须把 reasoning_details 原样回传，Anthropic 才能通过
"thinking blocks cannot be modified" 校验。而 OpenAI 兼容客户端（酒馆等）
只会保存 reasoning_content 纯文本，签名在客户端侧必然丢失。

本模块维护进程内缓存：思考全文(hash) → reasoning_details（含签名）。
- 响应侧：流式分片合并 / 非流式整体提取后写入缓存；
- 请求侧：assistant 消息带思考文本回传时按文本 hash 命中，恢复原始签名。

未命中（文本被用户编辑 / 服务重启）时由调用方决定降级策略
（Anthropic 系剥离思考字段，避免上游 400）。
"""
import hashlib
import threading
from collections import OrderedDict

# 缓存条目上限（FIFO 淘汰，单条 reasoning_details 通常仅几 KB）
_MAX_ENTRIES = 500

_lock = threading.Lock()
_cache: "OrderedDict[str, list]" = OrderedDict()


def _text_key(reasoning_text: str) -> str:
    """思考全文 → 缓存键。strip 归一化以容忍客户端保存时的首尾空白差异。"""
    normalized = (reasoning_text or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def store_reasoning_details(reasoning_text: str, reasoning_details: list) -> None:
    """缓存一轮响应的 reasoning_details（键为思考全文 hash）。"""
    if not reasoning_details or not (reasoning_text or "").strip():
        return
    key = _text_key(reasoning_text)
    with _lock:
        _cache[key] = reasoning_details
        _cache.move_to_end(key)
        while len(_cache) > _MAX_ENTRIES:
            _cache.popitem(last=False)


def lookup_reasoning_details(reasoning_text: str):
    """按思考全文查询缓存的 reasoning_details，未命中返回 None。"""
    if not (reasoning_text or "").strip():
        return None
    key = _text_key(reasoning_text)
    with _lock:
        details = _cache.get(key)
        if details is not None:
            _cache.move_to_end(key)
        return details


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
