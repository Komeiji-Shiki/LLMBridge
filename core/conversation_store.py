"""Compressed conversation cache, scoped by caller and explicit conversation ID.

Only cache records expire. Historical request logs are not deleted here.
"""
from contextlib import contextmanager
import gzip
import json
from pathlib import Path
import sqlite3
import time


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

    def find_response(self, owner, response_id):
        with self.connection() as connection:
            row = connection.execute('''SELECT c.*, a.payload FROM artifacts a JOIN conversations c USING(owner,session)
                WHERE a.owner=? AND a.key=? AND c.touched>? ORDER BY c.touched DESC LIMIT 1''',
                (owner, 'response:' + response_id, time.time() - self.idle_seconds)).fetchone()
            if row:
                result = dict(row)
                result['payload'] = json.loads(gzip.decompress(result['payload']))
                return result

    def cleanup(self):
        with self.connection() as connection:
            return connection.execute('DELETE FROM conversations WHERE touched<=?', (time.time() - self.idle_seconds,)).rowcount


conversation_store = ConversationStore()
