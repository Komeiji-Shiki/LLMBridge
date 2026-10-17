"""Compressed conversation cache, scoped by caller and explicit conversation ID.

Only cache records expire. Historical request logs are not deleted here.

磁盘空洞治理：
- 新库创建时启用 incremental auto-vacuum；
- 每小时清理后分批执行 incremental_vacuum，再截断 WAL；
- 旧库（auto_vacuum=NONE）需要一次完整 VACUUM，见 compact()。
"""
from contextlib import contextmanager
import gzip
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

# PRAGMA user_version 记录的库结构版本。
# 1：清除旧版写入的 last_exchange / response:<id>。它们从未被读取，
#    同样的完整交换永久保存在 logs/exchanges。
_SCHEMA_VERSION = 1
# 每批回收的页数（每页 4KB，4096 页约 16MB）。批次之间释放写锁，
# 其他写入可以穿插执行；单次清理最多回收 _RECLAIM_MAX_BATCHES 批。
_RECLAIM_PAGES_PER_BATCH = 4096
_RECLAIM_MAX_BATCHES = 256
# TRUNCATE checkpoint 等待读者期间会挡住新写入，只给很短的等待时间。
_CHECKPOINT_BUSY_TIMEOUT_MS = 2000


class ConversationStore:
    def __init__(self, path='data/conversations.db', idle_seconds=3 * 86400):
        self.path = Path(path)
        self.idle_seconds = idle_seconds
        self._initialize_lock = threading.Lock()

    def _initialize(self, connection):
        """建表与结构迁移；user_version 达到当前版本后不再执行。

        库文件被删除后 user_version 回到 0，下一次连接会自动重建。
        """
        # 新库必须在建表前设置 auto_vacuum；旧库需要一次 VACUUM 才会生效。
        if connection.execute('PRAGMA page_count').fetchone()[0] == 0:
            connection.execute('PRAGMA auto_vacuum=INCREMENTAL')
        connection.execute('PRAGMA journal_mode=WAL')
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
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version < 1:
            deleted = connection.execute(
                "DELETE FROM artifacts WHERE key='last_exchange' OR key LIKE 'response:%'"
            ).rowcount
            if deleted:
                logger.info('[CONV_CACHE] 已清除 %s 条无人读取的旧版交换快照', deleted)
        if version < _SCHEMA_VERSION:
            connection.execute(f'PRAGMA user_version={_SCHEMA_VERSION}')
        connection.commit()

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            # 只读一次库头；建表脚本不再随每次读写重复执行
            if connection.execute('PRAGMA user_version').fetchone()[0] < _SCHEMA_VERSION:
                with self._initialize_lock:
                    self._initialize(connection)
            connection.execute('PRAGMA foreign_keys=ON')
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

    def put(self, owner, session, key, payload):
        compressed = gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode(), compresslevel=6)
        with self.connection() as connection:
            connection.execute('INSERT INTO artifacts VALUES(?,?,?,?) ON CONFLICT(owner,session,key) DO UPDATE SET payload=excluded.payload',
                               (owner, session, key, compressed))

    def get(self, owner, session, key):
        with self.connection() as connection:
            row = connection.execute('''SELECT a.payload FROM artifacts a JOIN conversations c USING(owner,session)
                WHERE a.owner=? AND a.session=? AND a.key=? AND c.touched>?''',
                (owner, session, key, time.time() - self.idle_seconds)).fetchone()
            return json.loads(gzip.decompress(row['payload'])) if row else None

    def cleanup(self):
        cutoff = time.time() - self.idle_seconds
        with self.connection() as connection:
            connection.execute('DELETE FROM response_prefixes WHERE touched<=?', (cutoff,))
            deleted = connection.execute('DELETE FROM conversations WHERE touched<=?', (cutoff,)).rowcount
        self.reclaim()
        return deleted

    def reclaim(self, pages=_RECLAIM_PAGES_PER_BATCH, max_batches=_RECLAIM_MAX_BATCHES):
        """分批回收空闲页并截断 WAL，返回本次回收的页数。

        只有启用了 auto_vacuum 的库才能把空闲页还给文件系统；旧库需要
        先执行一次 compact()，这里只做 WAL 截断。
        """
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            freed = 0
            if connection.execute('PRAGMA auto_vacuum').fetchone()[0]:
                for _ in range(max_batches):
                    before = connection.execute('PRAGMA freelist_count').fetchone()[0]
                    if not before:
                        break
                    # execute() 只单步执行这条 PRAGMA，每次只回收 1 页；
                    # executescript 会把语句执行到底，并在批次结束时提交。
                    connection.executescript(f'PRAGMA incremental_vacuum({int(pages)});')
                    after = connection.execute('PRAGMA freelist_count').fetchone()[0]
                    freed += before - after
                    if after >= before:
                        break
            connection.execute(f'PRAGMA busy_timeout={_CHECKPOINT_BUSY_TIMEOUT_MS}')
            try:
                connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            except sqlite3.Error as exc:
                logger.debug('WAL checkpoint 跳过: %s', exc)
            return freed
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

        副本是复制开始时的快照。替换正式库需要先停掉服务，并同时删除
        旧库的 -wal / -shm 文件，否则残留 WAL 可能被回放到新文件上。
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
