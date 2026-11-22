"""给去重存储上线前写下的日志补做块拆分：散文件就地改写，已打包的归档按天重新打包。

每条记录拆分后序列化、再从块库还原，和原记录完全相等才替换，不相等的原样保留。块的
最后引用日期取记录自己的日期，按天清理日志时和新写入的记录一样回收。可以重复运行，
已经拆分过的记录不会再改。
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
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


def _loose_days(root: Path, today: str):
    for kind, parent in ((log_pack.KIND_LOGS, root), (log_pack.KIND_EXCHANGES, root / 'exchanges')):
        if parent.is_dir():
            for day_dir in sorted(parent.iterdir()):
                # 今天的目录还在写入，而且全是新格式
                if day_dir.is_dir() and _DAY.fullmatch(day_dir.name) and day_dir.name < today:
                    yield kind, day_dir


def _rewrite_loose(root: Path, today: str, stats: dict) -> None:
    rewrite = _rewriter(root, stats)
    for kind, day_dir in _loose_days(root, today):
        before = stats['rewritten']
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
