"""只读扫描 Codex 会话日志，持久化可重建的用量索引。"""

import hashlib
import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime
from pathlib import Path


TOKEN_FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens',
                'reasoning_output_tokens', 'total_tokens', 'cache_write_input_tokens')


def _usage(raw):
    if not isinstance(raw, dict):
        return None
    values = [raw.get(key, 0) for key in TOKEN_FIELDS]
    if any(type(value) is not int or value < 0 for value in values):
        return None
    if 'total_tokens' not in raw:
        values[4] = values[0] + values[2]
    return values


def parse_session(path):
    """逐行处理累计差值；返回的字段不包含提示词、回答或凭据。"""
    model, session, provider = 'unknown', path.stem, ''
    previous = [0] * len(TOKEN_FIELDS)
    seen = set()
    with path.open('rb') as stream:
        for line in stream:
            # 活跃日志尾部可能尚未写完，下次文件变化后重新读取。
            if not line.endswith(b'\n'):
                break
            if not any(marker in line for marker in (b'session_meta', b'turn_context', b'token_count')):
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict) or not isinstance(row.get('payload'), dict):
                continue
            payload = row['payload']
            if row.get('type') == 'session_meta':
                session = str(payload.get('id') or session)
                provider = str(payload.get('model_provider') or provider)
            elif row.get('type') == 'turn_context':
                model = str(payload.get('model') or model)
            elif row.get('type') == 'event_msg' and payload.get('type') == 'token_count':
                info = payload.get('info')
                if not isinstance(info, dict):
                    continue
                cumulative = _usage(info.get('total_token_usage'))
                last = _usage(info.get('last_token_usage'))
                try:
                    timestamp = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00')).timestamp()
                except (KeyError, ValueError, TypeError, AttributeError, OverflowError, OSError):
                    continue
                # 标识不含路径和会话 ID，因此归档、复制、分叉继承的同一事件只计一次。
                event_id = hashlib.sha256(json.dumps(
                    [row['timestamp'], model, cumulative, last], separators=(',', ':')
                ).encode()).hexdigest()
                if event_id in seen:
                    continue
                seen.add(event_id)
                if cumulative is not None:
                    if cumulative[4] < previous[4]:
                        # 压缩上下文后累计值可能下降；有本次用量时才能可靠继续计数。
                        delta = last or [0] * len(TOKEN_FIELDS)
                    else:
                        delta = [max(0, current - old) for current, old in zip(cumulative, previous)]
                    previous = cumulative
                elif last is not None:
                    delta = last
                    previous = [old + value for old, value in zip(previous, last)]
                else:
                    continue
                if not any(delta):
                    continue
                yield (event_id, timestamp, datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d'),
                       model, session, provider, *delta)


def discover_homes():
    """支持主目录、额外目录以及 JetBrains 的 Codex 缓存目录。"""
    paths = [Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')]
    paths.extend(Path(value).expanduser() for value in os.environ.get('CODEX_USAGE_HOMES', '').split(os.pathsep) if value)
    bases = [Path.home() / 'Library' / 'Caches' / 'JetBrains']
    bases.extend(Path(os.environ[key]) / 'JetBrains' for key in ('LOCALAPPDATA', 'APPDATA') if os.environ.get(key))
    for base in bases:
        if base.is_dir():
            paths.extend(base.glob('*/aia/codex'))
    return list(dict.fromkeys(path.resolve() for path in paths))


class CodexUsageIndex:
    def __init__(self, db_path=Path('logs/codex_usage.db'), homes=None):
        self.db_path = Path(db_path)
        self.homes = homes
        self._lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._refresh_task = None
        self._schema_ready = False
        self._last_scan = 0
        self._status = {}
        self._cache = OrderedDict()

    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        if self._schema_ready:
            return conn
        conn.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER);
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS events(
                path TEXT, event_id TEXT, timestamp REAL, date TEXT, model TEXT,
                session TEXT, provider TEXT, input_tokens INTEGER, cached_tokens INTEGER,
                output_tokens INTEGER, reasoning_tokens INTEGER, total_tokens INTEGER,
                cache_write_tokens INTEGER, PRIMARY KEY(path, event_id));
            CREATE INDEX IF NOT EXISTS events_id ON events(event_id);
            CREATE VIEW IF NOT EXISTS unique_events AS
                SELECT * FROM (SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY event_id ORDER BY path) AS copy_number FROM events)
                WHERE copy_number = 1;
        ''')
        self._schema_ready = True
        return conn

    def _scan(self, conn, force):
        if not force and time.monotonic() - self._last_scan < 60:
            return
        homes = self.homes if self.homes is not None else discover_homes()
        known = {row['path']: (row['size'], row['mtime']) for row in conn.execute('SELECT * FROM files')}
        found, errors, changed = set(), [], 0
        roots = []
        for home in homes:
            for folder in ('sessions', 'archived_sessions'):
                root = Path(home) / folder
                roots.append(str(root))
                if not root.exists():
                    continue
                def scan_error(error):
                    errors.append({'path': str(error.filename), 'error': str(error)})
                for directory, _, names in os.walk(root, onerror=scan_error):
                    for name in names:
                        if not name.endswith('.jsonl'):
                            continue
                        path = Path(directory) / name
                        key = str(path.resolve())
                        found.add(key)
                        try:
                            stat = path.stat()
                            fingerprint = (stat.st_size, stat.st_mtime_ns)
                            if known.get(key) == fingerprint and not force:
                                continue
                            # 单文件事务，读取失败时保留上一次成功的索引。
                            with conn:
                                conn.execute('DELETE FROM events WHERE path = ?', (key,))
                                conn.executemany('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                                 ((key, *event) for event in parse_session(path)))
                                conn.execute('INSERT OR REPLACE INTO files VALUES (?,?,?)', (key, *fingerprint))
                            changed += 1
                        except (OSError, sqlite3.Error) as error:
                            errors.append({'path': key, 'error': str(error)})
        # 目录读取失败不能当成日志删除；成功扫描时移除失效文件，归档副本由事件标识去重。
        if not errors:
            with conn:
                for key in known.keys() - found:
                    conn.execute('DELETE FROM events WHERE path = ?', (key,))
                    conn.execute('DELETE FROM files WHERE path = ?', (key,))
                    changed += 1
        status = {'scanned_at': time.time(), 'files': len(found), 'changed_files': changed,
                  'directories': roots, 'errors': errors, 'available': bool(found)}
        with conn:
            conn.execute('INSERT OR REPLACE INTO metadata VALUES (?, ?)', ('status', json.dumps(status)))
        with self._lock:
            if changed:
                self._cache.clear()
            self._status = status
            self._last_scan = time.monotonic()

    def refresh(self, force=False):
        with self._scan_lock:
            conn = self._connect()
            try:
                self._scan(conn, force)
            finally:
                conn.close()

    def schedule_refresh(self):
        """首页先读已保存索引，后台单独扫描；同一时刻最多一个扫描任务。"""
        from utils.task_registry import spawn
        if self._scan_lock.locked() or self._refresh_task and not self._refresh_task.done():
            return True
        if self._status and time.monotonic() - self._last_scan < 60:
            return False
        async def run():
            try:
                await asyncio.to_thread(self.refresh)
            except Exception:
                logging.getLogger(__name__).exception('Codex 后台扫描失败')
                with self._lock:
                    self._status['errors'] = [{'error': '后台扫描失败，请查看服务日志'}]
                    self._last_scan = time.monotonic()
        self._refresh_task = spawn(run(), name='codex-usage-refresh')
        return True

    def stats(self, start=None, end=None, force=False, exclude_providers=(), refresh=True):
        from core.db_stats import StatsDB
        from core.codex_usage_summary import summarize
        start_ts = StatsDB._parse_time_bound(start) if start else None
        end_ts = StatsDB._parse_time_bound(end, True) if end else None
        key = (start_ts, end_ts, tuple(sorted(exclude_providers)))
        if refresh and (force or not self._status or time.monotonic() - self._last_scan >= 60):
            self.refresh(force)
        with self._lock:
            conn = None
            try:
                if not self._status:
                    conn = self._connect()
                    saved = conn.execute("SELECT value FROM metadata WHERE key='status'").fetchone()
                    count = conn.execute('SELECT COUNT(*) FROM files').fetchone()[0]
                    self._status = json.loads(saved[0]) if saved else {'available': bool(count), 'files': count,
                                                                    'scanned_at': None, 'errors': []}
                if key not in self._cache:
                    conn = conn or self._connect()
                    self._cache[key] = summarize(conn, start_ts, end_ts, exclude_providers)
                    if len(self._cache) > 64:
                        self._cache.popitem(last=False)
                self._cache.move_to_end(key)
                return {**deepcopy(self._cache[key]), 'status': deepcopy(self._status)}
            finally:
                if conn is not None:
                    conn.close()


codex_usage_index = CodexUsageIndex()
