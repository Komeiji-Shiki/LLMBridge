"""本地用量索引的文件扫描、SQLite 缓存与后台刷新。"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path


class LocalUsageIndex:
    """由来源提供解析和汇总，其余扫描、缓存与后台任务共用。"""

    label = '本地工具'
    source = 'local'
    folders = ()
    token_fields = ('input_tokens', 'cached_tokens', 'output_tokens',
                    'reasoning_tokens', 'total_tokens', 'cache_write_tokens')

    def __init__(self, db_path, homes=None):
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
        columns = ', '.join(f'{field} INTEGER' for field in self.token_fields)
        conn.executescript(f'''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER);
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS events(
                path TEXT, event_id TEXT, timestamp REAL, date TEXT, model TEXT,
                session TEXT, provider TEXT, {columns}, PRIMARY KEY(path, event_id));
            CREATE INDEX IF NOT EXISTS events_id ON events(event_id);
        ''')
        self.create_event_view(conn)
        self._schema_ready = True
        return conn

    def _scan(self, conn, force, immediate=False):
        if not force and not immediate and time.monotonic() - self._last_scan < 60:
            return
        homes = self.homes if self.homes is not None else self.discover_homes()
        known = {row['path']: (row['size'], row['mtime']) for row in conn.execute('SELECT * FROM files')}
        found, errors, changed = set(), [], 0
        roots = []
        for home in homes:
            for folder in self.folders:
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
                                marks = ','.join('?' for _ in range(7 + len(self.token_fields)))
                                conn.executemany(f'INSERT OR IGNORE INTO events VALUES ({marks})',
                                                 ((key, *event) for event in self.parse_session(path)))
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

    def refresh(self, force=False, *, immediate=False):
        with self._scan_lock:
            conn = self._connect()
            try:
                self._scan(conn, force, immediate)
            finally:
                conn.close()

    def schedule_refresh(self, *, immediate=False):
        """首页先读已保存索引，后台单独扫描；同一时刻最多一个扫描任务。"""
        from utils.task_registry import spawn
        if self._scan_lock.locked() or self._refresh_task and not self._refresh_task.done():
            return True
        if not immediate and self._status and time.monotonic() - self._last_scan < 60:
            return False
        async def run():
            try:
                # 手动刷新跳过扫描间隔，但仍复用未变化文件的索引。
                await asyncio.to_thread(self.refresh, immediate=immediate)
            except Exception:
                logging.getLogger(__name__).exception('%s 后台扫描失败', self.label)
                with self._lock:
                    self._status['errors'] = [{'error': '后台扫描失败，请查看服务日志'}]
                    self._last_scan = time.monotonic()
        self._refresh_task = spawn(run(), name=f'{self.source}-usage-refresh')
        return True

    def stats(self, start=None, end=None, force=False, exclude_providers=(), refresh=True):
        from core.db_stats import StatsDB
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
                    self._cache[key] = self.summarize(conn, start_ts, end_ts, exclude_providers)
                    if len(self._cache) > 64:
                        self._cache.popitem(last=False)
                self._cache.move_to_end(key)
                return {**deepcopy(self._cache[key]), 'status': deepcopy(self._status)}
            finally:
                if conn is not None:
                    conn.close()


    def create_event_view(self, conn):
        conn.execute("""
            CREATE VIEW IF NOT EXISTS unique_events AS
                SELECT * FROM (SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY event_id ORDER BY path) AS copy_number FROM events)
                WHERE copy_number = 1
        """)

