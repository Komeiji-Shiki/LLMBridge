"""
监控模块 - SQLite扩展
为monitoring.py添加SQLite数据库支持
"""

import json
import time
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from core.request_metadata import migrate_metadata, write_metadata, read_metadata
from utils.log_query import LogQuery
from utils.request_features import request_features

logger = logging.getLogger(__name__)

class SQLiteLogger:
    """SQLite日志管理器"""
    
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._write_lock = __import__('threading').Lock()  # 仅保护写操作串行化，不阻塞读
        self._backup_before_metadata_migration()
        self._init_database()

    def _backup_before_metadata_migration(self):
        if not self.db_path.exists() or not self.db_path.stat().st_size:
            return
        with sqlite3.connect(str(self.db_path)) as source:
            columns = {row[1] for row in source.execute('PRAGMA table_info(requests)')}
            from core.request_metadata import COLUMNS
            if columns and not set(COLUMNS).issubset(columns):
                backup_path = self.db_path.with_name(self.db_path.name + '.before-request-metadata.bak')
                if not backup_path.exists():
                    with sqlite3.connect(str(backup_path)) as backup:
                        source.backup(backup)
    
    def _get_connection(self) -> sqlite3.Connection:
        """获取写路径持久连接（仅供持有 _write_lock 的写操作与初始化使用）。

        🔧 并发修复说明：WAL 的读写并发只在**不同连接**之间成立。
        旧版读写共用这一个连接，读仍会被写事务阻塞，且多线程交错使用
        同一连接存在语句/事务状态互相污染的风险。现在读路径改用
        _read_connection() 的独立短连接，真正实现读写并发。
        """
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            # WAL 模式：写连接与读连接（其他连接）之间不再互斥
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA cache_size=-8000")  # 8MB 缓存
            self._conn.execute("PRAGMA busy_timeout=5000")  # 5秒忙等待，避免立即SQLITE_BUSY
            self._conn.row_factory = sqlite3.Row
        return self._conn

    @contextmanager
    def _read_connection(self):
        """读路径独立短连接（用完即关）。

        WAL 模式下不同连接之间读写真正并发：读不阻塞写、写不阻塞读。
        本地文件建连开销微小，监控查询频率低，每次新建比跨线程复用
        更简单可靠（sqlite3 连接对象本身不保证多线程安全）。
        """
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()
    
    def _init_database(self):
        """初始化SQLite数据库结构"""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 创建请求表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT UNIQUE NOT NULL,
                    timestamp REAL NOT NULL,
                    date TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    success BOOLEAN NOT NULL,
                    duration REAL,
                    error TEXT,
                    mode TEXT,
                    session_id TEXT,
                    messages_count INTEGER DEFAULT 0,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    cached_tokens INTEGER DEFAULT 0,
                    input_cost REAL DEFAULT 0,
                    output_cost REAL DEFAULT 0,
                    total_cost REAL DEFAULT 0,
                    cached_cost REAL DEFAULT 0,
                    currency TEXT DEFAULT 'USD',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 数据库迁移
            try:
                cursor.execute("PRAGMA table_info(requests)")
                columns = [column[1] for column in cursor.fetchall()]
                
                migrations = [
                    ('input_cost', 'ALTER TABLE requests ADD COLUMN input_cost REAL DEFAULT 0'),
                    ('output_cost', 'ALTER TABLE requests ADD COLUMN output_cost REAL DEFAULT 0'),
                    ('total_cost', 'ALTER TABLE requests ADD COLUMN total_cost REAL DEFAULT 0'),
                    ('currency', 'ALTER TABLE requests ADD COLUMN currency TEXT DEFAULT "USD"'),
                    ('cached_tokens', 'ALTER TABLE requests ADD COLUMN cached_tokens INTEGER DEFAULT 0'),
                    ('cached_cost', 'ALTER TABLE requests ADD COLUMN cached_cost REAL DEFAULT 0'),
                    ('upstream_usage', 'ALTER TABLE requests ADD COLUMN upstream_usage TEXT'),
                    ('system_fingerprint', 'ALTER TABLE requests ADD COLUMN system_fingerprint TEXT'),
                    ('stop_reason', 'ALTER TABLE requests ADD COLUMN stop_reason TEXT'),
                    ('has_reasoning', 'ALTER TABLE requests ADD COLUMN has_reasoning INTEGER'),
                    ('has_tool_calls', 'ALTER TABLE requests ADD COLUMN has_tool_calls INTEGER'),
                ]
                for col_name, ddl in migrations:
                    if col_name not in columns:
                        cursor.execute(ddl)
                        logger.info(f"✅ 已添加 {col_name} 字段")
            except Exception as migration_error:
                logger.warning(f"数据库迁移警告: {migration_error}")
            
            # 创建索引
            for idx_sql in [
                'CREATE INDEX IF NOT EXISTS idx_date ON requests(date)',
                'CREATE INDEX IF NOT EXISTS idx_model ON requests(model)',
                'CREATE INDEX IF NOT EXISTS idx_timestamp ON requests(timestamp)',
                'CREATE INDEX IF NOT EXISTS idx_status ON requests(status)',
                'CREATE INDEX IF NOT EXISTS idx_date_model ON requests(date, model)',
                'CREATE INDEX IF NOT EXISTS idx_success ON requests(success)',
            ]:
                cursor.execute(idx_sql)
            
            migrate_metadata(conn)
            conn.commit()
            logger.info(f"✅ SQLite数据库已初始化: {self.db_path}")
            
        except Exception as e:
            logger.error(f"初始化SQLite数据库失败: {e}", exc_info=True)
    
    def close(self):
        """关闭数据库连接"""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
    
    def purge_old_records(self, max_days: int) -> int:
        """删除 timestamp 早于 max_days 天前的请求记录，返回删除行数。

        由每日日志保留清理任务调用（background_tasks.monitors.log_retention_cleaner）。
        """
        if not max_days or max_days <= 0:
            return 0
        try:
            cutoff_ts = time.time() - max_days * 86400
            with self._write_lock:
                conn = self._get_connection()
                cursor = conn.execute("DELETE FROM requests WHERE timestamp < ?", (cutoff_ts,))
                conn.commit()
                deleted = cursor.rowcount or 0
                if deleted:
                    # 🔧 批量删除后截断 WAL，避免 -wal 文件随每日清理持续膨胀
                    try:
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except Exception as ckpt_err:
                        logger.debug(f"[SQLITE] WAL checkpoint 失败（不影响功能）: {ckpt_err}")
            if deleted:
                logger.info(f"[SQLITE] 🧹 已清理 {deleted} 条超过 {max_days} 天的历史记录")
            return deleted
        except Exception as e:
            logger.error(f"[SQLITE] 清理过期记录失败: {e}")
            return 0
    
    def write_request(self, log_entry: dict):
        """写入请求到SQLite数据库"""
        if log_entry.get('type') != 'request_end':
            return
        
        try:
            request_id = log_entry.get('request_id')
            timestamp = log_entry.get('timestamp', time.time())
            date = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d')
            model = log_entry.get('model', 'unknown')
            status = log_entry.get('status', 'unknown')
            success = log_entry.get('success', status == 'success')
            duration = log_entry.get('duration')
            error = log_entry.get('error')
            mode = log_entry.get('mode')
            session_id = log_entry.get('session_id')
            messages_count = log_entry.get('messages_count', 0)
            # ⚠️ 键存在但值为 None 时 get 的默认值不生效，统一归零避免 None 相加崩溃
            input_tokens = log_entry.get('input_tokens') or 0
            output_tokens = log_entry.get('output_tokens') or 0
            total_tokens = input_tokens + output_tokens
            cached_tokens = log_entry.get('cached_tokens') or 0

            # 上游返回的原生 usage（原样序列化为 JSON 字符串存储）
            upstream_usage = log_entry.get('upstream_usage')
            upstream_usage_json = None
            if isinstance(upstream_usage, dict) and upstream_usage:
                try:
                    upstream_usage_json = json.dumps(upstream_usage, ensure_ascii=False)
                except (TypeError, ValueError):
                    upstream_usage_json = None

            # 上游返回的 system_fingerprint（DeepSeek 等 OpenAI 兼容 API 的顶层字段）
            system_fingerprint = log_entry.get('system_fingerprint')

            # 停止原因（Anthropic stop_reason / OpenAI finish_reason，来自 cost_info 或顶层）
            stop_reason = log_entry.get('stop_reason') or (log_entry.get('cost_info') or {}).get('stop_reason')

            cost_info = log_entry.get('cost_info') or {}
            input_cost = cost_info.get('input_cost', 0.0)
            output_cost = cost_info.get('output_cost', 0.0)
            cached_cost = cost_info.get('cached_cost', 0.0)
            total_cost = cost_info.get('total_cost', 0.0)
            currency = cost_info.get('currency') or None  # 失败请求不写货币，避免污染统计
            features = request_features(log_entry)
            
            # 🔧 用写锁串行化 SQLite 写操作（连接获取也在锁内，避免首次建连竞态）
            with self._write_lock:
                conn = self._get_connection()
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO requests (
                        request_id, timestamp, date, model, status, success,
                        duration, error, mode, session_id, messages_count,
                        input_tokens, output_tokens, total_tokens, cached_tokens,
                        input_cost, output_cost, cached_cost, total_cost, currency,
                        upstream_usage, system_fingerprint, stop_reason, has_reasoning, has_tool_calls
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    request_id, timestamp, date, model, status, success,
                    duration, error, mode, session_id, messages_count,
                    input_tokens, output_tokens, total_tokens, cached_tokens,
                    input_cost, output_cost, cached_cost, total_cost, currency,
                    upstream_usage_json, system_fingerprint, stop_reason,
                    features['has_reasoning'], features['has_tool_calls']
                ))
                write_metadata(conn, log_entry['request_id'], log_entry)
                conn.commit()
            
            logger.debug(f"已写入数据库: {str(request_id or '')[:8]}")
            
        except Exception as e:
            logger.error(f"写入SQLite数据库失败: {e}", exc_info=True)
    
    
    def get_request_details(self, request_id: str) -> Optional[Dict]:
        """从SQLite数据库获取请求详情（独立读连接，不与写路径互斥）"""
        try:
            with self._read_connection() as conn:
                cursor = conn.cursor()

                cursor.execute('SELECT * FROM requests WHERE request_id = ?', (request_id,))
                row = cursor.fetchone()
            if row:
                result = self._row_to_request_dict(row)
                result.pop('type')
                result['date'] = row['date']
                return result

            return None
            
        except Exception as e:
            logger.error(f"获取请求详情失败: {e}", exc_info=True)
            return None
    
    def get_recent_requests(self, limit: int = 50) -> List[Dict]:
        """从SQLite快速获取最近的N条请求摘要（走timestamp索引，O(log n + limit)）"""
        LogQuery(limit=limit)
        with self._read_connection() as conn:
            rows = conn.execute('SELECT * FROM requests ORDER BY timestamp DESC, rowid DESC LIMIT ?', (limit,))
            return [self._row_to_request_dict(row) for row in rows]

    @staticmethod
    def _parse_upstream_usage(raw) -> Optional[Dict]:
        """反序列化 upstream_usage 列（存储为 JSON 字符串，旧数据为 NULL）"""
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _row_to_request_dict(row) -> Dict:
        """将 SQLite Row 映射为日志条目字典（与分层日志格式对齐）"""
        return {
            'type': 'request_end',
            'request_id': row['request_id'],
            'timestamp': row['timestamp'],
            'model': row['model'],
            'status': row['status'],
            'success': bool(row['success']),
            'duration': row['duration'],
            'error': row['error'],
            'mode': row['mode'],
            'session_id': row['session_id'],
            'messages_count': row['messages_count'],
            'input_tokens': row['input_tokens'],
            'output_tokens': row['output_tokens'],
            'total_tokens': row['total_tokens'],
            'cached_tokens': row['cached_tokens'] or 0,
            'cached_cost': row['cached_cost'] or 0.0,
            'input_cost': row['input_cost'],
            'output_cost': row['output_cost'],
            'total_cost': row['total_cost'],
            'currency': row['currency'],
            'upstream_usage': SQLiteLogger._parse_upstream_usage(row['upstream_usage']),
            'system_fingerprint': row['system_fingerprint'],
            'stop_reason': row['stop_reason'],
            # 旧记录没有这些标记时保留未知状态，不扫描历史正文来补算。
            'has_reasoning': bool(row['has_reasoning']) if row['has_reasoning'] is not None else None,
            'has_tool_calls': bool(row['has_tool_calls']) if row['has_tool_calls'] is not None else None,
            **read_metadata(row),
        }

    def query_requests(self, limit: int = 50, offset: int = 0,
                       model: Optional[str] = None, status: Optional[str] = None,
                       search: Optional[str] = None, start_date=None, end_date=None) -> Dict:
        """同一读取快照内统计与分页，时间相同时按写入顺序稳定排序。"""
        query = LogQuery(limit, offset, model, status, search, start_date, end_date)
        where_sql, params = query.sql()
        with self._read_connection() as conn:
            conn.execute('BEGIN')
            total = conn.execute(f'SELECT COUNT(*) FROM requests{where_sql}', params).fetchone()[0]
            rows = conn.execute(
                f'SELECT * FROM requests{where_sql} ORDER BY timestamp DESC, rowid DESC LIMIT ? OFFSET ?',
                params + [limit, offset])
            items = [self._row_to_request_dict(row) for row in rows]
        # 查询失败交给 LogManager 选择文件回退，不能伪装成没有记录。
        return {'total': total, 'items': items}

    def get_distinct_models(self) -> List[str]:
        """获取日志中出现过的所有模型名（用于前端筛选下拉）"""
        try:
            with self._read_connection() as conn:
                cursor = conn.execute("SELECT DISTINCT model FROM requests ORDER BY model")
                return [row[0] for row in cursor.fetchall() if row[0]]
        except Exception as e:
            logger.error(f"获取模型列表失败: {e}", exc_info=True)
            return []
