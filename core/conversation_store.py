"""Compressed conversation cache, scoped by caller, route and explicit conversation ID.

Every record expires on its own idle time. Historical request logs are not deleted here.

磁盘空洞治理：
- 新库创建时启用 incremental auto-vacuum；
- 每小时清理后分批执行 incremental_vacuum，再截断 WAL；
- 旧库（auto_vacuum=NONE）需要一次完整 VACUUM，见 compact()。
"""
from contextlib import contextmanager
import gzip
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time

from core.request_context import cache_scope

logger = logging.getLogger(__name__)

# PRAGMA user_version 记录的库结构版本。
# 1：清除旧版写入的 last_exchange / response:<id>。它们从未被读取，
#    同样的完整交换永久保存在 logs/exchanges。
# 2：新增 stored_objects，记录上游保存的对象属于哪条线路。
# 3：签名和推理缓存改为在 response_prefixes 里逐条保存、逐条过期。按会话
#    整块保存的 conversations / artifacts 迁入不带会话 ID 的命名空间后删除。
_SCHEMA_VERSION = 3
# 迁移时保留的旧键；其余旧键（含版本 1 清理的交换快照）没有读取方。
_LEGACY_ENTRY_PREFIXES = ('reasoning:', 'anthropic_thinking:')

# 上游保存的对象可以在几周后被查询或续接，记录比会话缓存保留得久。
_OBJECT_RETENTION_SECONDS = 30 * 86400
# 每批回收的页数（每页 4KB，4096 页约 16MB）。批次之间释放写锁，
# 其他写入可以穿插执行；单次清理最多回收 _RECLAIM_MAX_BATCHES 批。
_RECLAIM_PAGES_PER_BATCH = 4096
_RECLAIM_MAX_BATCHES = 256
# TRUNCATE checkpoint 等待读者期间会挡住新写入，只给很短的等待时间。
_CHECKPOINT_BUSY_TIMEOUT_MS = 2000


def interactions_thought_key(summary: str) -> str:
    """Interactions 思考摘要原文对应的签名记录。"""
    return 'interactions-thought:' + hashlib.sha256(summary.encode('utf-8')).hexdigest()


def interactions_turn_key(call_id: str) -> str:
    """以首个函数调用 ID 保存的整轮 Interactions 步骤。"""
    return 'interactions-turn:' + call_id


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
            CREATE TABLE IF NOT EXISTS response_prefixes (
                owner TEXT NOT NULL, scope TEXT NOT NULL, prefix TEXT NOT NULL,
                payload BLOB, digest TEXT NOT NULL, touched REAL NOT NULL,
                PRIMARY KEY(owner, scope, prefix));
            CREATE INDEX IF NOT EXISTS idx_response_prefixes_idle ON response_prefixes(touched);
            CREATE TABLE IF NOT EXISTS stored_objects (
                owner TEXT NOT NULL, kind TEXT NOT NULL, object_id TEXT NOT NULL,
                payload TEXT NOT NULL, settled INTEGER NOT NULL, touched REAL NOT NULL,
                PRIMARY KEY(owner, kind, object_id));
            CREATE INDEX IF NOT EXISTS idx_stored_objects_idle ON stored_objects(touched);
        ''')
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version < 3:
            self._migrate_session_artifacts(connection)
        if version < _SCHEMA_VERSION:
            connection.execute(f'PRAGMA user_version={_SCHEMA_VERSION}')
        connection.commit()

    def _migrate_session_artifacts(self, connection):
        """把旧版按会话整块保存的签名拆成逐条记录，进行中的对话升级后不丢历史签名。"""
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'conversations', 'artifacts'} <= tables:
            return
        cutoff = time.time() - self.idle_seconds
        entries = []
        for row in connection.execute('''SELECT a.owner, a.key, a.payload, c.model, c.endpoint, c.credential, c.touched
                FROM artifacts a JOIN conversations c USING(owner, session) WHERE c.touched>?''', (cutoff,)):
            owner, key, payload, model, endpoint, credential, touched = row
            scope = cache_scope(None, model, endpoint, credential)
            try:
                value = json.loads(gzip.decompress(payload))
            except (OSError, ValueError):
                continue
            if key == 'signatures' and isinstance(value, dict):
                for text, fragments in (value.get('gemini_thoughts') or {}).items():
                    entries.append((owner, scope, interactions_thought_key(text), fragments, touched))
                for call_id, steps in (value.get('interaction_turns') or {}).items():
                    entries.append((owner, scope, interactions_turn_key(call_id), steps, touched))
            elif key.startswith(_LEGACY_ENTRY_PREFIXES):
                entries.append((owner, scope, key, value, touched))
        # 按时间顺序写入，同一个键在不同会话里内容不同时照常标记为歧义。
        for owner, scope, key, value, touched in sorted(entries, key=lambda entry: entry[4]):
            self._upsert_prefixes(connection, owner, scope, [key], value, touched)
        connection.execute('DROP TABLE artifacts')
        connection.execute('DROP TABLE conversations')
        if entries:
            logger.info('[CONV_CACHE] 已把 %s 条按会话保存的签名迁入共用命名空间', len(entries))

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

    def cleanup(self):
        cutoff = time.time() - self.idle_seconds
        with self.connection() as connection:
            deleted = connection.execute('DELETE FROM response_prefixes WHERE touched<=?', (cutoff,)).rowcount
            connection.execute('DELETE FROM stored_objects WHERE touched<=?', (time.time() - _OBJECT_RETENTION_SECONDS,))
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

    def remember_response_prefixes(self, owner, scope, prefixes, output):
        """Store one output under several keys in one transaction.

        A conflicting output makes that key ambiguous until it expires.
        """
        self.remember_entries(owner, scope, [(prefixes, output)])

    def remember_entries(self, owner, scope, entries):
        """Store several (keys, output) pairs in one transaction."""
        now = time.time()
        with self.connection() as connection:
            for prefixes, output in entries:
                self._upsert_prefixes(connection, owner, scope, prefixes, output, now)

    def _upsert_prefixes(self, connection, owner, scope, prefixes, output, touched):
        raw = json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        digest = hashlib.sha256(raw).hexdigest()
        payload = gzip.compress(raw, compresslevel=6)
        expired = time.time() - self.idle_seconds
        connection.executemany('''INSERT INTO response_prefixes VALUES(?,?,?,?,?,?)
            ON CONFLICT(owner,scope,prefix) DO UPDATE SET
                payload=CASE WHEN response_prefixes.touched<=? THEN excluded.payload
                             WHEN response_prefixes.digest=excluded.digest THEN response_prefixes.payload
                             ELSE NULL END,
                digest=CASE WHEN response_prefixes.touched<=? THEN excluded.digest
                            WHEN response_prefixes.digest=excluded.digest THEN response_prefixes.digest
                            ELSE '' END,
                touched=excluded.touched''',
            [(owner, scope, prefix, payload, digest, touched, expired, expired)
             for prefix in dict.fromkeys(prefixes)])

    def remember_object(self, owner, kind, object_id, payload, settled):
        """记下上游对象属于哪条线路。同一个 id 再次出现时覆盖。"""
        with self.connection() as connection:
            connection.execute('''INSERT INTO stored_objects VALUES(?,?,?,?,?,?)
                ON CONFLICT(owner,kind,object_id) DO UPDATE SET payload=excluded.payload,
                    settled=excluded.settled, touched=excluded.touched''',
                (owner, kind, object_id, json.dumps(payload, ensure_ascii=False), int(bool(settled)), time.time()))

    def stored_object(self, owner, kind, object_id):
        now = time.time()
        with self.connection() as connection:
            row = connection.execute('SELECT payload FROM stored_objects WHERE owner=? AND kind=? AND object_id=? AND touched>?',
                                     (owner, kind, object_id, now - _OBJECT_RETENTION_SECONDS)).fetchone()
            if row is None:
                return None
            connection.execute('UPDATE stored_objects SET touched=? WHERE owner=? AND kind=? AND object_id=?',
                               (now, owner, kind, object_id))
            return json.loads(row['payload'])

    def claim_settlement(self, owner, kind, object_id):
        """原子地认领补记用量；并发的两次轮询只有一次返回 True。"""
        with self.connection() as connection:
            return connection.execute('UPDATE stored_objects SET settled=1 WHERE owner=? AND kind=? AND object_id=? AND settled=0',
                                      (owner, kind, object_id)).rowcount == 1

    def forget_object(self, owner, kind, object_id):
        with self.connection() as connection:
            connection.execute('DELETE FROM stored_objects WHERE owner=? AND kind=? AND object_id=?', (owner, kind, object_id))

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
