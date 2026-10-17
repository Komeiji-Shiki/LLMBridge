"""Compressed conversation cache, scoped by caller and explicit conversation ID.

Only cache records expire. Historical request logs are not deleted here.

磁盘空洞治理：
- 新库创建时启用 incremental auto-vacuum；
- 每小时清理后按页预算执行 incremental_vacuum 并截断 WAL；
- 旧库（auto_vacuum=NONE）需要一次完整 VACUUM，见 compact()。
"""
from contextlib import contextmanager
import gzip
import json
import logging
from pathlib import Path
import sqlite3
import time

logger = logging.getLogger(__name__)

# last_exchange / response:<id> 里的 upstream_request 通常与 request 共用同一份
# messages。存储时只保留一份，读取时再还原，避免每条记录把完整历史存两遍。
_MESSAGES_IN_REQUEST = '_messages_in_request'
# 每小时最多回收的页数（每页 4KB，4096 页约 16MB），避免长时间持锁。
_RECLAIM_PAGES_PER_RUN = 4096


def _dedupe_record(record):
    """上游请求与原始请求的 messages 完全相同时，存储侧去掉重复副本。"""
    if not isinstance(record, dict):
        return record
    request = record.get('request')
    upstream = record.get('upstream_request')
    if not isinstance(request, dict) or not isinstance(upstream, dict):
        return record
    messages = request.get('messages')
    if messages is None or upstream.get('messages') != messages:
        return record
    stored_upstream = dict(upstream)
    stored_upstream.pop('messages', None)
    stored_upstream[_MESSAGES_IN_REQUEST] = True
    stored = dict(record)
    stored['upstream_request'] = stored_upstream
    return stored


def _restore_record(record):
    """还原 _dedupe_record 去掉的 messages，读取方看到的结构保持不变。"""
    if not isinstance(record, dict):
        return record
    upstream = record.get('upstream_request')
    if not isinstance(upstream, dict) or not upstream.get(_MESSAGES_IN_REQUEST):
        return record
    request = record.get('request')
    if not isinstance(request, dict) or 'messages' not in request:
        return record
    restored_upstream = dict(upstream)
    restored_upstream.pop(_MESSAGES_IN_REQUEST, None)
    restored_upstream['messages'] = request.get('messages')
    restored = dict(record)
    restored['upstream_request'] = restored_upstream
    return restored


class ConversationStore:
    def __init__(self, path='data/conversations.db', idle_seconds=3 * 86400):
        self.path = Path(path)
        self.idle_seconds = idle_seconds

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            # 新库必须在建表前设置 auto_vacuum；旧库需要一次 VACUUM 才会生效。
            is_new_database = connection.execute(
                'PRAGMA page_count'
            ).fetchone()[0] == 0
            if is_new_database:
                connection.execute('PRAGMA auto_vacuum=INCREMENTAL')
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA foreign_keys=ON')
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS conversations (
                    owner TEXT NOT NULL, session TEXT NOT NULL, model TEXT NOT NULL,
                    endpoint TEXT NOT NULL, credential TEXT NOT NULL DEFAULT '',
                    touched REAL NOT NULL, PRIMARY KEY(owner, session));
                CREATE TABLE IF NOT EXISTS artifacts (
                    owner TEXT NOT NULL, session TEXT NOT NULL, key TEXT NOT NULL, payload BLOB NOT NULL,
                    PRIMARY KEY(owner, session, key),
                    FOREIGN KEY(owner, session) REFERENCES conversations(owner, session) ON DELETE CASCADE);
                CREATE INDEX IF NOT EXISTS idx_conversations_idle ON conversations(touched);
                CREATE TABLE IF NOT EXISTS response_prefixes (
                    owner TEXT NOT NULL, scope TEXT NOT NULL, prefix TEXT NOT NULL,
                    payload BLOB, digest TEXT NOT NULL, touched REAL NOT NULL,
                    PRIMARY KEY(owner, scope, prefix));
                CREATE INDEX IF NOT EXISTS idx_response_prefixes_idle ON response_prefixes(touched);
            ''')
            yield connection
            connection.commit()
        finally:
            connection.close()

    def touch(self, owner, session, model, endpoint, credential=''):
        now = time.time()
        with self.connection() as connection:
            row = connection.execute('SELECT * FROM conversations WHERE owner=? AND session=?', (owner, session)).fetchone()
            if row and row['touched'] <= now - self.idle_seconds:
                connection.execute('DELETE FROM conversations WHERE owner=? AND session=?', (owner, session))
                row = None
            if row and (row['model'] != model or row['endpoint'] != endpoint):
                raise ValueError('同一会话不能切换模型或上游；请创建新会话')
            if row:
                connection.execute('UPDATE conversations SET touched=?, credential=CASE WHEN ? != ? THEN ? ELSE credential END WHERE owner=? AND session=?',
                                   (now, credential, '', credential, owner, session))
            else:
                connection.execute('INSERT INTO conversations VALUES(?,?,?,?,?,?)', (owner, session, model, endpoint, credential, now))

    def binding(self, owner, session):
        with self.connection() as connection:
            row = connection.execute('SELECT * FROM conversations WHERE owner=? AND session=? AND touched>?',
                                     (owner, session, time.time() - self.idle_seconds)).fetchone()
            return dict(row) if row else None

    def put(self, owner, session, key, payload):
        payload = _dedupe_record(payload)
        compressed = gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode(), compresslevel=6)
        with self.connection() as connection:
            connection.execute('INSERT INTO artifacts VALUES(?,?,?,?) ON CONFLICT(owner,session,key) DO UPDATE SET payload=excluded.payload',
                               (owner, session, key, compressed))

    def get(self, owner, session, key):
        with self.connection() as connection:
            row = connection.execute('''SELECT a.payload FROM artifacts a JOIN conversations c USING(owner,session)
                WHERE a.owner=? AND a.session=? AND a.key=? AND c.touched>?''',
                (owner, session, key, time.time() - self.idle_seconds)).fetchone()
            return _restore_record(json.loads(gzip.decompress(row['payload']))) if row else None

    def find_response(self, owner, response_id):
        with self.connection() as connection:
            row = connection.execute('''SELECT c.*, a.payload FROM artifacts a JOIN conversations c USING(owner,session)
                WHERE a.owner=? AND a.key=? AND c.touched>? ORDER BY c.touched DESC LIMIT 1''',
                (owner, 'response:' + response_id, time.time() - self.idle_seconds)).fetchone()
            if row:
                result = dict(row)
                result['payload'] = _restore_record(
                    json.loads(gzip.decompress(result['payload']))
                )
                return result

    def cleanup(self):
        with self.connection() as connection:
            connection.execute('DELETE FROM response_prefixes WHERE touched<=?', (time.time() - self.idle_seconds,))
            deleted = connection.execute('DELETE FROM conversations WHERE touched<=?', (time.time() - self.idle_seconds,)).rowcount
        self.reclaim()
        return deleted

    def reclaim(self, pages=_RECLAIM_PAGES_PER_RUN):
        """回收空洞并截断 WAL。

        只有启用了 auto_vacuum 的库才能真正把尾页还给文件系统；旧库返回
        auto_vacuum=0，需要先执行一次 compact()。
        """
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.execute('PRAGMA busy_timeout=30000')
            auto_vacuum = connection.execute('PRAGMA auto_vacuum').fetchone()[0]
            if auto_vacuum:
                # 不能放在事务里执行
                connection.commit()
                connection.execute(f'PRAGMA incremental_vacuum({int(pages)})')
            try:
                connection.commit()
                connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            except sqlite3.Error as exc:
                logger.debug('WAL checkpoint 跳过: %s', exc)
            return auto_vacuum
        finally:
            connection.close()

    def storage_stats(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            page_size = connection.execute('PRAGMA page_size').fetchone()[0]
            page_count = connection.execute('PRAGMA page_count').fetchone()[0]
            free_pages = connection.execute('PRAGMA freelist_count').fetchone()[0]
            auto_vacuum = connection.execute('PRAGMA auto_vacuum').fetchone()[0]
            return {
                'file_bytes': self.path.stat().st_size if self.path.exists() else 0,
                'page_size': page_size,
                'page_count': page_count,
                'free_pages': free_pages,
                'free_bytes': free_pages * page_size,
                'used_bytes': (page_count - free_pages) * page_size,
                'auto_vacuum': auto_vacuum,
            }
        finally:
            connection.close()

    def compact(self):
        """完整 VACUUM：回收全部空洞并让 auto_vacuum=INCREMENTAL 生效。

        会短暂独占数据库写入；调用方应在可控时机（如重启前）执行。
        """
        connection = sqlite3.connect(self.path, timeout=60)
        try:
            connection.execute('PRAGMA busy_timeout=60000')
            connection.execute('PRAGMA auto_vacuum=INCREMENTAL')
            connection.execute('VACUUM')
            connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            connection.close()

    def compact_copy(self, target):
        """在线生成压缩副本（VACUUM INTO），不阻塞现有写入。

        副本是复制开始时的快照；替换正式库需要先停掉服务。
        """
        target = Path(target)
        if target.exists():
            target.unlink()
        connection = sqlite3.connect(self.path, timeout=60)
        try:
            connection.execute('PRAGMA busy_timeout=60000')
            # VACUUM INTO 会把连接上的 auto_vacuum 设置带到副本
            connection.execute('PRAGMA auto_vacuum=INCREMENTAL')
            connection.execute('VACUUM INTO ?', (str(target),))
        finally:
            connection.close()
        return target

    def remember_response_prefix(self, owner, scope, prefix, output):
        """A conflicting output makes the prefix ambiguous until it expires."""
        import hashlib
        raw = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        digest = hashlib.sha256(raw).hexdigest()
        now = time.time()
        with self.connection() as connection:
            connection.execute('''INSERT INTO response_prefixes VALUES(?,?,?,?,?,?)
                ON CONFLICT(owner,scope,prefix) DO UPDATE SET
                    payload=CASE WHEN response_prefixes.touched<=? THEN excluded.payload
                                 WHEN response_prefixes.digest=excluded.digest THEN response_prefixes.payload
                                 ELSE NULL END,
                    digest=CASE WHEN response_prefixes.touched<=? THEN excluded.digest
                                WHEN response_prefixes.digest=excluded.digest THEN response_prefixes.digest
                                ELSE '' END,
                    touched=excluded.touched''',
                (owner, scope, prefix, gzip.compress(raw, compresslevel=6), digest, now,
                 now - self.idle_seconds, now - self.idle_seconds))

    def response_prefixes(self, owner, scope, prefixes):
        """Fetch exact assistant boundaries in one transaction, without scanning logs."""
        result = {}
        now = time.time()
        with self.connection() as connection:
            for prefix in prefixes:
                row = connection.execute('''SELECT payload FROM response_prefixes
                    WHERE owner=? AND scope=? AND prefix=? AND touched>?''',
                    (owner, scope, prefix, now - self.idle_seconds)).fetchone()
                if row is not None:
                    # Touch ambiguous entries too: they must not become valid again
                    # merely because the client keeps using one side of a conflict.
                    connection.execute('UPDATE response_prefixes SET touched=? WHERE owner=? AND scope=? AND prefix=?',
                                       (now, owner, scope, prefix))
                    if row['payload'] is not None:
                        result[prefix] = json.loads(gzip.decompress(row['payload']))
        return result


conversation_store = ConversationStore()
