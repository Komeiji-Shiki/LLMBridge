"""旧日志的分块压缩存储，压缩后仍可按请求 ID 读取单条记录。

结构（均位于日志根目录下）::

    archive/index.db                         每条记录所在的分块、偏移、大小和哈希
    archive/logs/20261002-0001.tar.zst       分层请求日志
    archive/exchanges/20261002-0001.tar.zst  完整原生交换归档

每个分块是一个标准 tar.zst，索引丢失时用任意 zstd/tar 工具都能解开。相邻请求常带着
相同的对话历史，放进同一分块才压得动；分块限制大小则让读取单条时只解压一小段。

删除原文件的前提：分块已落盘、逐条哈希比对通过、索引事务已提交。
"""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import tarfile
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import zstandard

logger = logging.getLogger(__name__)

ARCHIVE_DIR = 'archive'
INDEX_FILE = 'index.db'
PART_SUFFIX = '.tar.zst'
KIND_LOGS = 'logs'
KIND_EXCHANGES = 'exchanges'

# 未打包的日目录每个请求约 1.9MB（带图对话每轮都重存一遍图片），打包后跨请求去重，实测 42 倍；
# 从分块里读单条在 50ms 以内，所以只留最近几天不打包
DEFAULT_COMPRESS_AFTER_DAYS = 3
PART_BYTES = 256 * 1024 * 1024
ZSTD_LEVEL = 9
ZSTD_WINDOW_LOG = 27
_MAX_WINDOW = 1 << 31
_READ_CHUNK = 1024 * 1024
_DAY = re.compile(r'\d{8}')

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS entries (
    kind TEXT NOT NULL, date TEXT NOT NULL, rel TEXT NOT NULL, lookup TEXT NOT NULL,
    part TEXT NOT NULL, data_offset INTEGER NOT NULL, size INTEGER NOT NULL,
    gunzipped INTEGER NOT NULL, sha256 TEXT NOT NULL,
    PRIMARY KEY (kind, date, rel)
);
CREATE INDEX IF NOT EXISTS entries_lookup ON entries (kind, lookup);
'''


class PackBusy(RuntimeError):
    """已有另一个进程在打包。"""


class PackVerifyError(RuntimeError):
    """分块内容与原文件不一致，原文件不会被删除。"""


def _archive_root(root) -> Path:
    return Path(root) / ARCHIVE_DIR


def _lookup_key(kind: str, name: str) -> str:
    stem = name[:-3] if name.endswith('.gz') else name
    stem = stem[:-5] if stem.endswith('.json') else stem
    return stem if kind == KIND_EXCHANGES else stem.rsplit('_', 1)[-1]


def _decode(path_name: str, raw: bytes):
    """返回 (内容, 是否解开过 gzip)。损坏的 .gz 原样保存，不丢任何字节。"""
    if path_name.endswith('.gz'):
        try:
            return gzip.decompress(raw), True
        except (OSError, EOFError, zlib.error):
            return raw, False
    return raw, False


@contextlib.contextmanager
def _exclusive_lock(archive: Path):
    archive.mkdir(parents=True, exist_ok=True)
    handle = open(archive / '.pack.lock', 'a+b')
    try:
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PackBusy('另一个打包进程正在运行') from exc
        yield
    finally:
        handle.close()


def _read_exact(reader, size: int) -> bytes:
    chunks = []
    while size > 0:
        chunk = reader.read(min(size, _READ_CHUNK))
        if not chunk:
            raise PackVerifyError('分块提前结束')
        chunks.append(chunk)
        size -= len(chunk)
    return b''.join(chunks)


def _open_reader(raw):
    return zstandard.ZstdDecompressor(max_window_size=_MAX_WINDOW).stream_reader(raw, read_size=_READ_CHUNK)


def _read_member(part: Path, offset: int, size: int) -> bytes:
    with open(part, 'rb') as raw, _open_reader(raw) as reader:
        reader.seek(offset)
        return _read_exact(reader, size)


class _PartWriter:
    def __init__(self, directory: Path, date: str, kind: str):
        directory.mkdir(parents=True, exist_ok=True)
        numbers = [int(p.name[len(date) + 1:-len(PART_SUFFIX)])
                   for p in directory.glob(f'{date}-*{PART_SUFFIX}')]
        self.path = directory / f'{date}-{max(numbers, default=0) + 1:04d}{PART_SUFFIX}'
        self.temporary = self.path.with_name(self.path.name + '.tmp')
        self.date, self.kind = date, kind
        self.entries, self.sources, self.size = [], [], 0
        params = zstandard.ZstdCompressionParameters.from_level(
            ZSTD_LEVEL, window_log=ZSTD_WINDOW_LOG, enable_ldm=True)
        self._raw = open(self.temporary, 'wb')
        self._zstd = zstandard.ZstdCompressor(compression_params=params).stream_writer(self._raw, closefd=False)
        self._tar = tarfile.open(fileobj=self._zstd, mode='w|', format=tarfile.PAX_FORMAT)

    def add(self, source: Path, rel: str, payload: bytes, gunzipped: bool, digest: str, mtime: float):
        name = rel[:-3] if gunzipped else rel
        info = tarfile.TarInfo(f'{self.kind}/{self.date}/{name}')
        info.size, info.mtime = len(payload), int(mtime)
        self._tar.addfile(info, io.BytesIO(payload))
        padded = (len(payload) + 511) // 512 * 512
        self.entries.append((self.kind, self.date, rel, _lookup_key(self.kind, Path(rel).name), self.path.name,
                             self._tar.offset - padded, len(payload), int(gunzipped), digest))
        self.sources.append(source)
        self.size += len(payload)

    def finish(self) -> int:
        self._tar.close()
        self._zstd.close()
        self._raw.flush()
        os.fsync(self._raw.fileno())
        self._raw.close()
        os.replace(self.temporary, self.path)
        return self.path.stat().st_size

    def abort(self):
        for closer in (self._tar.close, self._zstd.close, self._raw.close):
            with contextlib.suppress(Exception):
                closer()
        self.temporary.unlink(missing_ok=True)


def _verify_part(path: Path, entries) -> None:
    """顺序解压整个分块，逐条比对大小和 SHA-256。"""
    with open(path, 'rb') as raw, _open_reader(raw) as reader:
        position = 0
        for entry in sorted(entries, key=lambda item: item[5]):
            offset, size, digest = entry[5], entry[6], entry[8]
            if offset < position:
                raise PackVerifyError(f'{path.name} 偏移错误: {entry[2]}')
            reader.seek(offset)
            data = _read_exact(reader, size)
            if hashlib.sha256(data).hexdigest() != digest:
                raise PackVerifyError(f'{path.name} 哈希不一致: {entry[2]}')
            position = offset + size


def _remove_orphans(conn, directory: Path, kind: str) -> None:
    """清掉崩溃遗留的临时文件和未写进索引的分块。"""
    if not directory.is_dir():
        return
    known = {row[0] for row in conn.execute('SELECT DISTINCT part FROM entries WHERE kind=?', (kind,))}
    for path in directory.iterdir():
        if path.name.endswith('.tmp') or (path.name.endswith(PART_SUFFIX) and path.name not in known):
            path.unlink(missing_ok=True)


def _delete_sources(paths, day_dir: Path) -> int:
    removed = 0
    for path in paths:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
            removed += 1
    # 只移除已经空了的目录；打包期间新出现的文件原样留到下一轮。
    for directory in sorted((p for p in day_dir.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        with contextlib.suppress(OSError):
            directory.rmdir()
    with contextlib.suppress(OSError):
        day_dir.rmdir()
    return removed


def _pack_day(conn, archive: Path, kind: str, day_dir: Path, keep_source: bool, stats: dict) -> None:
    date = day_dir.name
    directory = archive / kind
    indexed = dict(conn.execute('SELECT rel, sha256 FROM entries WHERE kind=? AND date=?', (kind, date)))
    files = []
    for path in day_dir.rglob('*'):
        with contextlib.suppress(OSError):
            if path.is_file():
                files.append((path.stat().st_mtime, path))
    files.sort(key=lambda item: (item[0], str(item[1])))

    confirmed, writer = [], None

    def close_part():
        nonlocal writer
        if writer is None:
            return
        current, writer = writer, None
        stats['packed_bytes'] += current.finish()
        try:
            _verify_part(current.path, current.entries)
        except Exception:
            current.path.unlink(missing_ok=True)
            raise
        with conn:
            conn.executemany('INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?)', current.entries)
        confirmed.extend(current.sources)
        stats['parts'] += 1

    try:
        for mtime, path in files:
            rel = path.relative_to(day_dir).as_posix()
            try:
                raw = path.read_bytes()
            except OSError as exc:
                logger.warning(f'[LOG_PACK] 跳过无法读取的文件 {path}: {exc}')
                continue
            payload, gunzipped = _decode(path.name, raw)
            digest = hashlib.sha256(payload).hexdigest()
            stats['files'] += 1
            stats['source_bytes'] += len(raw)
            if rel in indexed:
                if indexed[rel] == digest:
                    confirmed.append(path)
                else:
                    stats['conflicts'] += 1
                    logger.warning(f'[LOG_PACK] 文件与已归档内容不同，保留原文件: {path}')
                continue
            if writer is None:
                writer = _PartWriter(directory, date, kind)
            writer.add(path, rel, payload, gunzipped, digest, mtime)
            if writer.size >= PART_BYTES:
                close_part()
        close_part()
    except Exception:
        if writer is not None:
            writer.abort()
        raise
    if not keep_source:
        stats['deleted'] += _delete_sources(confirmed, day_dir)


def _old_days(parent: Path, cutoff: str):
    if not parent.is_dir():
        return []
    return sorted(p for p in parent.iterdir() if p.is_dir() and _DAY.fullmatch(p.name) and p.name < cutoff)


def pack_old_logs(root='logs', older_than_days=DEFAULT_COMPRESS_AFTER_DAYS, today=None, keep_source=False) -> dict:
    """打包 older_than_days 天以前的日目录。可重复执行，已归档的文件不会重复写入。"""
    stats = dict(files=0, source_bytes=0, packed_bytes=0, parts=0, deleted=0, conflicts=0, days=0)
    older_than_days = int(older_than_days)
    if older_than_days <= 0:
        return stats
    root = Path(root)
    archive = _archive_root(root)
    cutoff = ((today or datetime.now().date()) - timedelta(days=older_than_days)).strftime('%Y%m%d')
    with _exclusive_lock(archive):
        conn = sqlite3.connect(archive / INDEX_FILE, timeout=30)
        try:
            conn.executescript(_SCHEMA)
            for kind, parent in ((KIND_LOGS, root), (KIND_EXCHANGES, root / 'exchanges')):
                _remove_orphans(conn, archive / kind, kind)
                for day_dir in _old_days(parent, cutoff):
                    _pack_day(conn, archive, kind, day_dir, keep_source, stats)
                    stats['days'] += 1
        finally:
            conn.close()
    return stats


def purge_packed_before(root, kind: str, cutoff: str) -> int:
    """删除日期早于 cutoff（YYYYMMDD）的归档分块，让保留期也覆盖已打包的数据。

    分块按日存放，整块删除不影响其他日期。先提交索引再删文件：中途崩溃时多出来的
    分块会被下一轮打包当作孤儿清理，不会留下指向空文件的索引。
    """
    archive = _archive_root(root)
    if not (archive / INDEX_FILE).exists():
        return 0
    with _exclusive_lock(archive):
        conn = sqlite3.connect(archive / INDEX_FILE, timeout=30)
        try:
            conn.executescript(_SCHEMA)
            parts = [row[0] for row in conn.execute(
                'SELECT DISTINCT part FROM entries WHERE kind=? AND date<?', (kind, cutoff))]
            with conn:
                conn.execute('DELETE FROM entries WHERE kind=? AND date<?', (kind, cutoff))
        finally:
            conn.close()
        for part in parts:
            (archive / kind / part).unlink(missing_ok=True)
    return len(parts)


def _connect_readonly(root):
    path = _archive_root(root) / INDEX_FILE
    if not path.exists():
        return None
    uri = 'file:' + quote(path.resolve().as_posix()) + '?mode=ro'
    return sqlite3.connect(uri, uri=True, timeout=10)


def _candidates(root, kind: str, lookup: str, date=None):
    conn = _connect_readonly(root)
    if conn is None:
        return []
    try:
        return conn.execute(
            'SELECT part, data_offset, size FROM entries WHERE kind=? AND lookup=? '
            'ORDER BY date = ? DESC, date DESC', (kind, lookup, date or '')).fetchall()
    except sqlite3.OperationalError:
        return []  # 打包任务还没建表
    finally:
        conn.close()


def find_packed_request(root, request_id: str, date=None):
    """按完整请求 ID 读取已压缩的分层日志；date 只影响查找顺序。"""
    short = request_id[:8] if request_id else 'unknown'
    for part, offset, size in _candidates(root, KIND_LOGS, short, date):
        path = _archive_root(root) / KIND_LOGS / part
        record = json.loads(_read_member(path, offset, size))
        if record.get('request_id') == request_id:
            return record
    return None


def load_packed_exchange(root, request_id: str):
    """读取已压缩的完整原生交换归档，并还原去重占位。"""
    from core.exchange_archive import restore_exchange
    for part, offset, size in _candidates(root, KIND_EXCHANGES, request_id):
        path = _archive_root(root) / KIND_EXCHANGES / part
        return restore_exchange(json.loads(_read_member(path, offset, size)))
    return None
