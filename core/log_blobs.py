"""日志正文的内容寻址块存储：多轮对话每轮都带着的历史消息和截图只存一份。

请求日志和交换归档写盘前，outline() 把对话数组（messages / input / contents / tools 等）的
每一项、以及项内的大块内容（截图、长文本）换成 {"$blob": 块 ID}，正文按 sha256 去重存进
日志根目录下的 blobs.db；hydrate() 读取时原样还原，读取方看到的结构和原始记录一致。

两类日志保留期不同，块按 kind 分开去重。每个块记最后一次被引用的日期：按天删除日志时，
最后引用日期早于截止日的块只被已删除的日志引用，可以一并回收，不用扫描归档。
块在日志文件写出之前提交，进程崩溃也不会留下指向不存在的块的引用。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import zstandard

from utils import fastjson

logger = logging.getLogger(__name__)

REF = '$blob'
MISSING = '$blob_missing'
KIND_LOGS = 'logs'
KIND_EXCHANGES = 'exchanges'
LOG_KEYS = ('request_messages',)
EXCHANGE_SECTIONS = ('request', 'upstream_request')
EXCHANGE_KEYS = ('messages', 'input', 'contents', 'tools', 'system', 'instructions',
                 'system_instruction', 'systemInstruction')
# 序列化后不到这个长度的项留在原处：块引用本身也要几十字节
MIN_ITEM_BYTES = 512
# 项内的单块内容先单独成块：消息外层只差一个缓存标记时，里面的截图仍能共用
MIN_PART_BYTES = 8192
_NESTED_KEYS = ('content', 'parts', 'summary')
_MAX_DEPTH = 4
_CODEC_RAW, _CODEC_ZSTD = 0, 1
_ZSTD_LEVEL = 3
# 同一段历史每轮都要查一次，记住最近的哈希省掉查库
_MEMO_LIMIT = 50_000
_READ_BATCH = 500

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS blobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    hash BLOB NOT NULL,
    codec INTEGER NOT NULL,
    raw_size INTEGER NOT NULL,
    last_day INTEGER NOT NULL,
    data BLOB NOT NULL,
    UNIQUE (kind, hash)
);
CREATE INDEX IF NOT EXISTS blobs_age ON blobs (kind, last_day);
'''

_codecs = threading.local()


def _compress(payload: bytes):
    compressor = getattr(_codecs, 'compressor', None)
    if compressor is None:
        compressor = _codecs.compressor = zstandard.ZstdCompressor(level=_ZSTD_LEVEL)
    data = compressor.compress(payload)
    return (_CODEC_ZSTD, data) if len(data) < len(payload) else (_CODEC_RAW, payload)


def _decompress(codec: int, data: bytes) -> bytes:
    if codec == _CODEC_RAW:
        return data
    decompressor = getattr(_codecs, 'decompressor', None)
    if decompressor is None:
        decompressor = _codecs.decompressor = zstandard.ZstdDecompressor()
    return decompressor.decompress(data)


def _today() -> int:
    return int(datetime.now().strftime('%Y%m%d'))


class BlobStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._writer = None
        self._readers = threading.local()
        self._memo = OrderedDict()
        self._touched_day = 0
        self._touched = set()

    def _connect(self, **kwargs):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, **kwargs)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.executescript(_SCHEMA)
        return conn

    def _reader(self):
        conn = getattr(self._readers, 'conn', None)
        if conn is None:
            conn = self._readers.conn = self._connect()
        return conn

    @contextmanager
    def writing(self, kind: str, day: int = None):
        """一条记录的所有块放进同一个事务；返回的 put(payload) 给出块 ID。

        day 是记录所属的日期，默认今天；补拆旧日志时传记录自己的日期，块才能按天回收。
        """
        day = day or _today()
        with self._lock:
            if day != self._touched_day:
                self._touched_day, self._touched = day, set()
            pending = []
            begun = []

            def put(payload):
                # 没有大块的小记录不建库、不开事务
                if not begun:
                    if self._writer is None:
                        self._writer = self._connect(check_same_thread=False)
                    self._writer.execute('BEGIN')
                    begun.append(True)
                return self._put(self._writer, kind, payload, day, pending)

            try:
                yield put
                if begun:
                    self._writer.execute('COMMIT')
            except BaseException:
                if begun:
                    self._writer.execute('ROLLBACK')
                raise
            # 提交之后才记进缓存，回滚的块 ID 不能被后续记录引用
            for key, blob_id in pending:
                self._memo[key] = blob_id
                self._memo.move_to_end(key)
                self._touched.add(blob_id)
            while len(self._memo) > _MEMO_LIMIT:
                self._memo.popitem(last=False)

    def _put(self, conn, kind, payload, day, pending):
        digest = hashlib.sha256(payload).digest()
        key = (kind, digest)
        blob_id = self._memo.get(key)
        if blob_id is None:
            row = conn.execute('SELECT id FROM blobs WHERE kind = ? AND hash = ?', key).fetchone()
            blob_id = row[0] if row else None
        if blob_id is None:
            codec, data = _compress(payload)
            blob_id = conn.execute(
                'INSERT INTO blobs (kind, hash, codec, raw_size, last_day, data) VALUES (?, ?, ?, ?, ?, ?)',
                (kind, digest, codec, len(payload), day, data)).lastrowid
        elif blob_id not in self._touched:
            conn.execute('UPDATE blobs SET last_day = ? WHERE id = ? AND last_day < ?', (day, blob_id, day))
        pending.append((key, blob_id))
        return blob_id

    def load(self, ids) -> dict:
        """按块 ID 取原文，不存在的 ID 不出现在结果里。"""
        ids = list(dict.fromkeys(ids))
        if not ids or not self.path.exists():
            return {}
        found = {}
        conn = self._reader()
        for start in range(0, len(ids), _READ_BATCH):
            batch = ids[start:start + _READ_BATCH]
            rows = conn.execute(
                f'SELECT id, codec, data FROM blobs WHERE id IN ({",".join("?" * len(batch))})', batch)
            for blob_id, codec, data in rows:
                found[blob_id] = _decompress(codec, data)
        return found

    def purge_before(self, kind: str, day: int) -> int:
        """回收最后引用日期早于 day 的块；调用方已删掉这一天之前的全部日志。"""
        if not self.path.exists():
            return 0
        with self._lock:
            if self._writer is None:
                self._writer = self._connect(check_same_thread=False)
            removed = self._writer.execute(
                'DELETE FROM blobs WHERE kind = ? AND last_day < ?', (kind, day)).rowcount
            self._forget()
        return removed

    def forget(self):
        """块库被别处改过（回收、换成占位）之后清掉缓存，后续记录不再引用旧块。"""
        with self._lock:
            self._forget()

    def _forget(self):
        self._memo.clear()
        self._touched = set()


_stores = {}
_stores_lock = threading.Lock()


def store_for(log_root) -> BlobStore:
    """日志根目录对应的块库；同一个目录在进程内共用一个实例。"""
    path = (Path(log_root) / 'blobs.db').resolve()
    with _stores_lock:
        store = _stores.get(path)
        if store is None:
            store = _stores[path] = BlobStore(path)
        return store


# ---------------------------------------------------------------- 拆分与还原

def _shrink(value, put, depth):
    """项内的大块内容先单独成块，返回替换后的副本；没有替换时返回原对象。"""
    if depth >= _MAX_DEPTH or not isinstance(value, dict):
        return value
    copied = None
    for key in _NESTED_KEYS:
        nested = value.get(key)
        if not isinstance(nested, list):
            continue
        parts = []
        for part in nested:
            part = _shrink(part, put, depth + 1)
            payload = fastjson.dumps(part)
            parts.append({REF: put(payload)} if len(payload) >= MIN_PART_BYTES else part)
        copied = copied or dict(value)
        copied[key] = parts
    return copied or value


def _chunk(value, put):
    value = _shrink(value, put, 0)
    payload = fastjson.dumps(value)
    return {REF: put(payload)} if len(payload) >= MIN_ITEM_BYTES else value


def outline(value: dict, keys, put) -> dict:
    """把 keys 对应的对话数组逐项、长文本整段换成块引用，返回新 dict，不改原值。"""
    copied = dict(value)
    for key in keys:
        item = copied.get(key)
        if isinstance(item, list):
            copied[key] = [_chunk(element, put) for element in item]
        elif item is not None:
            copied[key] = _chunk(item, put)
    return copied


def _is_ref(value) -> bool:
    return isinstance(value, dict) and len(value) == 1 and isinstance(value.get(REF), int)


class _Resolver:
    def __init__(self, store: BlobStore):
        self.store = store
        self.cache = {}

    def prefetch(self, values):
        ids = [value[REF] for value in values if _is_ref(value) and value[REF] not in self.cache]
        for blob_id, payload in self.store.load(ids).items():
            self.cache[blob_id] = json.loads(payload)

    def item(self, value, depth=0):
        if _is_ref(value):
            blob_id = value[REF]
            if blob_id not in self.cache:
                self.prefetch([value])
            if blob_id not in self.cache:
                logger.warning(f'[LOG_BLOBS] 日志块 {blob_id} 缺失，原位留下占位')
                return {MISSING: blob_id}
            value = self.cache[blob_id]
        if depth >= _MAX_DEPTH or not isinstance(value, dict):
            return value
        if not any(isinstance(value.get(key), list) for key in _NESTED_KEYS):
            return value
        copied = dict(value)
        for key in _NESTED_KEYS:
            if isinstance(copied.get(key), list):
                self.prefetch(copied[key])
                copied[key] = [self.item(part, depth + 1) for part in copied[key]]
        return copied


def hydrate(value: dict, keys, store: BlobStore) -> dict:
    """还原 outline 的块引用，读取方看到的结构和原始记录一致。"""
    resolver = _Resolver(store)
    copied = dict(value)
    for key in keys:
        item = copied.get(key)
        if isinstance(item, list):
            resolver.prefetch(item)
            copied[key] = [resolver.item(element) for element in item]
        elif item is not None:
            copied[key] = resolver.item(item)
    return copied


# ---------------------------------------------------------------- 两类日志的入口

def outline_log(entry: dict, log_root, day: int = None) -> dict:
    if not any(key in entry for key in LOG_KEYS):
        return entry
    with store_for(log_root).writing(KIND_LOGS, day) as put:
        return outline(entry, LOG_KEYS, put)


def hydrate_log(entry, log_root):
    if not isinstance(entry, dict) or not any(key in entry for key in LOG_KEYS):
        return entry
    return hydrate(entry, LOG_KEYS, store_for(log_root))


def outline_exchange(record: dict, log_root, day: int = None) -> dict:
    sections = [name for name in EXCHANGE_SECTIONS if isinstance(record.get(name), dict)]
    if not sections:
        return record
    copied = dict(record)
    with store_for(log_root).writing(KIND_EXCHANGES, day) as put:
        for name in sections:
            copied[name] = outline(copied[name], EXCHANGE_KEYS, put)
    return copied


def hydrate_exchange(record, log_root):
    if not isinstance(record, dict):
        return record
    copied = dict(record)
    for name in EXCHANGE_SECTIONS:
        if isinstance(copied.get(name), dict):
            copied[name] = hydrate(copied[name], EXCHANGE_KEYS, store_for(log_root))
    return copied
