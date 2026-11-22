"""日志块存储的维护：给旧日志补做块拆分，删除指定任务带的图片。

migrate() 给去重存储上线前写下的日志补做块拆分：散文件就地改写，已打包的归档按天重新打包。
每条记录拆分后序列化、再从块库还原，和原记录完全相等才替换，不相等的原样保留。块的最后
引用日期取记录自己的日期，按天清理日志时和新写入的记录一样回收。可以重复运行，已经拆分
过的记录不会再改。

drop_images() 把消息里含有指定文本的请求（例如批量识图任务）带的图片块换成占位说明，
日志结构不变，然后压缩块库。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import sqlite3
import zlib
from datetime import datetime
from pathlib import Path

from core import log_blobs, log_pack
from utils import fastjson

logger = logging.getLogger(__name__)

_DAY = re.compile(r'\d{8}')
_CODECS = {
    log_pack.KIND_LOGS: (log_blobs.outline_log, log_blobs.hydrate_log),
    log_pack.KIND_EXCHANGES: (log_blobs.outline_exchange, log_blobs.hydrate_exchange),
}
# 短于这个长度的 data URL 不值得换，也避免误伤普通文本
_IMAGE_MIN_CHARS = 1024
_MIME_KEYS = ('media_type', 'mime_type', 'mimeType')
_UPDATE_BATCH = 500


def _loose_days(root: Path, before: str = None):
    for kind, parent in ((log_pack.KIND_LOGS, root), (log_pack.KIND_EXCHANGES, root / 'exchanges')):
        if parent.is_dir():
            for day_dir in sorted(parent.iterdir()):
                if day_dir.is_dir() and _DAY.fullmatch(day_dir.name) and (before is None or day_dir.name < before):
                    yield kind, day_dir


def _loose_files(day_dir: Path):
    """日目录里的每条日志文件：(路径, 原始字节, 解压后的内容, 是否 gzip)。"""
    for path in sorted(day_dir.rglob('*')):
        compressed = path.name.endswith('.json.gz')
        if not (compressed or path.name.endswith('.json')) or not path.is_file():
            continue
        try:
            raw = path.read_bytes()
            payload = gzip.decompress(raw) if compressed else raw
        except (OSError, EOFError, zlib.error) as exc:
            logger.warning(f'[LOG_BLOBS] 跳过无法读取的文件 {path}: {exc}')
            continue
        yield path, raw, payload, compressed


# ---------------------------------------------------------------- 补做块拆分

def _rewriter(root: Path, stats: dict):
    """返回 rewrite(kind, date, payload)：拆分后的新字节，不需要或不能改写时返回 None。"""
    def rewrite(kind, date, payload):
        outline, hydrate = _CODECS[kind]
        try:
            record = json.loads(payload)
        except ValueError:
            return None  # 损坏的压缩文件按原字节归档过，不是 JSON
        if not isinstance(record, dict):
            return None
        try:
            stored = outline(record, root, int(date))
            if stored == record:
                return None
            data = fastjson.dumps(stored)
            # 按读取方的路径从块库还原，确认能一字不差地读回来
            if hydrate(json.loads(data), root) == record:
                return data
            logger.warning(f'[LOG_BLOBS] {kind}/{date} 一条记录还原后与原文不同，保留原文')
        except Exception as exc:
            logger.warning(f'[LOG_BLOBS] {kind}/{date} 一条记录拆分失败，保留原文: {exc}')
        stats['kept'] += 1
        return None
    return rewrite


def _rewrite_loose(root: Path, today: str, stats: dict) -> None:
    rewrite = _rewriter(root, stats)
    # 今天的目录还在写入，而且全是新格式
    for kind, day_dir in _loose_days(root, before=today):
        before = stats['rewritten']
        for path, raw, payload, compressed in _loose_files(day_dir):
            stats['files'] += 1
            stats['source_bytes'] += len(raw)
            data = rewrite(kind, day_dir.name, payload)
            if data is None:
                stats['written_bytes'] += len(raw)
                continue
            data = gzip.compress(data) if compressed else data
            status = path.stat()
            temporary = path.with_name(path.name + '.tmp')
            temporary.write_bytes(data)
            os.replace(temporary, path)
            # 打包按修改时间排列文件，改写后保留原来的时间
            os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns))
            stats['rewritten'] += 1
            stats['written_bytes'] += len(data)
        logger.info(f'[LOG_BLOBS] 散文件 {kind}/{day_dir.name}: {stats["rewritten"] - before} 条改写')


def migrate(root='logs', today=None) -> dict:
    """先改写还没打包的日目录，再重新打包旧日志归档。"""
    root = Path(root)
    today = (today or datetime.now().date()).strftime('%Y%m%d')
    loose = dict(files=0, rewritten=0, kept=0, source_bytes=0, written_bytes=0)
    _rewrite_loose(root, today, loose)
    kept = dict(kept=0)
    packed = log_pack.rewrite_packed(root, _rewriter(root, kept))
    return {'loose': loose, 'packed': {**packed, **kept}}


# ---------------------------------------------------------------- 删除指定任务的图片

def _mime(value: dict) -> str:
    return next((value[key] for key in _MIME_KEYS if isinstance(value.get(key), str)), '')


def _image_refs(value, found: set) -> set:
    """引用处就能看出是图片的块：带图片 data URL 前缀，或者挂在带图片 MIME 的 data 字段上。"""
    if log_blobs.is_ref(value):
        if value.get(log_blobs.PREFIX, '').startswith('data:image/'):
            found.add(value[log_blobs.REF])
    elif isinstance(value, dict):
        mime = _mime(value)
        for key, item in value.items():
            if key == 'data' and mime.startswith('image/') and log_blobs.is_ref(item):
                found.add(item[log_blobs.REF])
            else:
                _image_refs(item, found)
    elif isinstance(value, list):
        for item in value:
            _image_refs(item, found)
    return found


def _strip_images(value, note: str, counter: list):
    """把 data URL 和带图片 MIME 的 base64 字段换成占位说明，返回新值。"""
    if isinstance(value, str):
        if len(value) >= _IMAGE_MIN_CHARS and value.startswith('data:image/'):
            counter[0] += 1
            mime = value[5:].split(';', 1)[0].split(',', 1)[0]
            return f'[图片未保存：{mime}，{note}]'
        return value
    if isinstance(value, list):
        return [_strip_images(item, note, counter) for item in value]
    if isinstance(value, dict):
        mime = _mime(value)
        copied = {}
        for key, item in value.items():
            if key == 'data' and mime.startswith('image/') and isinstance(item, str) and len(item) >= _IMAGE_MIN_CHARS:
                counter[0] += 1
                copied[key] = f'[图片未保存：{mime}，{note}]'
            else:
                copied[key] = _strip_images(item, note, counter)
        return copied
    return value


def _record_payloads(root: Path, kind: str):
    for _, _, payload in log_pack.iter_packed(root, kind):
        yield payload
    for day_kind, day_dir in _loose_days(root):
        if day_kind == kind:
            for _, _, payload, _ in _loose_files(day_dir):
                yield payload


def _owned_images(conn, root: Path, kind: str, needle: bytes, stats: dict):
    """只被含 needle 的消息引用的块 ID，以及其中在引用处就能看出是图片的块。

    还被别的消息或记录直接引用的块不算。
    """
    owned, shared, images = set(), set(), set()
    for _, codec, data in conn.execute('SELECT id, codec, data FROM blobs WHERE kind = ?', (kind,)):
        payload = log_blobs._decompress(codec, data)
        if b'"$blob"' not in payload:
            continue
        value = json.loads(payload)
        if needle in payload:
            log_blobs.collect_refs(value, owned)
            _image_refs(value, images)
        else:
            log_blobs.collect_refs(value, shared)
    for payload in _record_payloads(root, kind):
        try:
            log_blobs.collect_refs(json.loads(payload), shared)
        except ValueError:
            continue
    stats['kept_shared'] += len(owned & shared)
    return sorted(owned - shared), images


def drop_images(root='logs', marker: str = '', dry_run=False) -> dict:
    """消息里含有 marker 的请求带的图片块换成占位说明，结构不变；之后压缩块库。

    占位里留着原块的大小和哈希前缀；块的哈希同时换成新内容的哈希，以后再写入同一张图会
    另存一份，不会引用到占位。网关进程记着自己写过的块哈希，它在这次启动后写过的图要重启
    之后才会另存。
    """
    if not marker:
        raise ValueError('marker 不能为空')
    root = Path(root)
    path = root / 'blobs.db'
    stats = dict(dropped=0, kept_shared=0, freed_bytes=0, db_before=path.stat().st_size, db_after=0)
    conn = sqlite3.connect(path, timeout=60, isolation_level=None)
    try:
        for kind in (log_pack.KIND_LOGS, log_pack.KIND_EXCHANGES):
            candidates, images = _owned_images(conn, root, kind, marker.encode('utf-8'), stats)
            for start in range(0, len(candidates), _UPDATE_BATCH):
                batch = candidates[start:start + _UPDATE_BATCH]
                rows = conn.execute(f'SELECT id, codec, hash, data FROM blobs WHERE id IN ({",".join("?" * len(batch))})',
                                    batch).fetchall()
                updates = []
                for blob_id, codec, digest, data in rows:
                    payload = log_blobs._decompress(codec, data)
                    note = f'原 {len(payload)} 字节，sha256 {digest.hex()[:16]}'
                    content, counter = json.loads(payload), [0]
                    if isinstance(content, str):
                        # 只有 base64 的图片块：读取时前缀接回去，得到 "data:image/png;base64,[图片未保存：…]"
                        if blob_id not in images:
                            continue  # 长文本块
                        stripped = f'[图片未保存：{note}]'
                    else:
                        stripped = _strip_images(content, note, counter)
                        if not counter[0]:
                            continue
                    new = fastjson.dumps(stripped)
                    updates.append((new, len(new), hashlib.sha256(new).digest(), blob_id))
                    stats['dropped'] += 1
                    stats['freed_bytes'] += len(data)
                if updates and not dry_run:
                    conn.execute('BEGIN')
                    conn.executemany('UPDATE blobs SET codec = 0, data = ?, raw_size = ?, hash = ? WHERE id = ?',
                                     updates)
                    conn.execute('COMMIT')
            logger.info(f'[LOG_BLOBS] {kind}: {len(candidates)} 个块只被这类请求引用')
        if stats['dropped'] and not dry_run:
            conn.execute('VACUUM')
    finally:
        conn.close()
    log_blobs.store_for(root).forget()
    stats['db_after'] = path.stat().st_size
    return stats
