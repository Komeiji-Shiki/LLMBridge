"""
监控模块 - SQLite扩展
为monitoring.py添加SQLite数据库支持
"""

import json
import time
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

class SQLiteLogger:
    """SQLite日志管理器"""
    
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._write_lock = __import__('threading').Lock()  # 仅保护写操作串行化，不阻塞读
        self._init_database()
    
    def _get_connection(self) -> sqlite3.Connection:
        """获取持久连接（复用），启用 WAL 模式实现读写并发"""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            # 🔧 核心修复：WAL 模式允许读写并发，不再互斥
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA cache_size=-8000")  # 8MB 缓存
            self._conn.execute("PRAGMA busy_timeout=5000")  # 5秒忙等待，避免立即SQLITE_BUSY
        return self._conn
    
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
    
    def write_request(self, log_entry: dict):
        """写入请求到SQLite数据库"""
        if log_entry.get('type') != 'request_end':
            return
        
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
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
            
            cost_info = log_entry.get('cost_info') or {}
            input_cost = cost_info.get('input_cost', 0.0)
            output_cost = cost_info.get('output_cost', 0.0)
            cached_cost = cost_info.get('cached_cost', 0.0)
            total_cost = cost_info.get('total_cost', 0.0)
            currency = cost_info.get('currency') or None  # 失败请求不写货币，避免污染统计
            
            # 🔧 用写锁串行化 SQLite 写操作（WAL 模式下写之间仍需互斥，但不阻塞读）
            with self._write_lock:
                cursor.execute('''
                    INSERT OR REPLACE INTO requests (
                        request_id, timestamp, date, model, status, success,
                        duration, error, mode, session_id, messages_count,
                        input_tokens, output_tokens, total_tokens, cached_tokens,
                        input_cost, output_cost, cached_cost, total_cost, currency
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    request_id, timestamp, date, model, status, success,
                    duration, error, mode, session_id, messages_count,
                    input_tokens, output_tokens, total_tokens, cached_tokens,
                    input_cost, output_cost, cached_cost, total_cost, currency
                ))
                conn.commit()
            
            logger.debug(f"已写入数据库: {str(request_id or '')[:8]}")
            
        except Exception as e:
            logger.error(f"写入SQLite数据库失败: {e}", exc_info=True)
    
    
    def get_request_details(self, request_id: str) -> Optional[Dict]:
        """从SQLite数据库获取请求详情"""
        try:
            conn = self._get_connection()
            conn.row_factory = sqlite3.Row  # 使结果可以用列名访问
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT
                    request_id, timestamp, date, model, status, success,
                    duration, error, mode, session_id, messages_count,
                    input_tokens, output_tokens, total_tokens,
                    cached_tokens, cached_cost,
                    input_cost, output_cost, total_cost, currency,
                    created_at
                FROM requests
                WHERE request_id = ?
            ''', (request_id,))
            
            row = cursor.fetchone()
            
            if row:
                return {
                    'request_id': row['request_id'],
                    'timestamp': row['timestamp'],
                    'date': row['date'],
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
                    'currency': row['currency']
                }
            
            return None
            
        except Exception as e:
            logger.error(f"获取请求详情失败: {e}", exc_info=True)
            return None
    
    def get_recent_requests(self, limit: int = 50) -> List[Dict]:
        """从SQLite快速获取最近的N条请求摘要（走timestamp索引，O(log n + limit)）"""
        result = self.query_requests(limit=limit, offset=0)
        return result.get('items', [])

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
        }

    def query_requests(self, limit: int = 50, offset: int = 0,
                       model: Optional[str] = None, status: Optional[str] = None,
                       search: Optional[str] = None) -> Dict:
        """分页 + 过滤查询请求日志。

        Args:
            limit/offset: 分页参数
            model: 按模型名精确过滤
            status: 'success' / 'failed'（其他值忽略）
            search: 在 request_id / model / error 中模糊搜索

        Returns:
            {'total': 总条数, 'items': 日志列表}
        """
        try:
            conn = self._get_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            where_clauses = []
            params: list = []
            if model:
                where_clauses.append("model = ?")
                params.append(model)
            if status == 'success':
                where_clauses.append("success = 1")
            elif status == 'failed':
                where_clauses.append("success = 0")
            if search:
                like = f"%{search}%"
                where_clauses.append("(request_id LIKE ? OR model LIKE ? OR error LIKE ?)")
                params.extend([like, like, like])

            where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            cursor.execute(f"SELECT COUNT(*) FROM requests{where_sql}", params)
            total = cursor.fetchone()[0]

            cursor.execute(f'''
                SELECT
                    request_id, timestamp, date, model, status, success,
                    duration, error, mode, session_id, messages_count,
                    input_tokens, output_tokens, total_tokens,
                    cached_tokens, cached_cost,
                    input_cost, output_cost, total_cost, currency
                FROM requests{where_sql}
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            ''', params + [limit, offset])

            items = [self._row_to_request_dict(row) for row in cursor.fetchall()]
            return {'total': total, 'items': items}

        except Exception as e:
            logger.error(f"过滤查询请求日志失败: {e}", exc_info=True)
            return {'total': 0, 'items': []}

    def get_distinct_models(self) -> List[str]:
        """获取日志中出现过的所有模型名（用于前端筛选下拉）"""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT model FROM requests ORDER BY model")
            return [row[0] for row in cursor.fetchall() if row[0]]
        except Exception as e:
            logger.error(f"获取模型列表失败: {e}", exc_info=True)
            return []