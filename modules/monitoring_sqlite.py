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
        self._init_database()
    
    def _init_database(self):
        """初始化SQLite数据库结构"""
        try:
            conn = sqlite3.connect(self.db_path)
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
                    input_cost REAL DEFAULT 0,
                    output_cost REAL DEFAULT 0,
                    total_cost REAL DEFAULT 0,
                    currency TEXT DEFAULT 'USD',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 🔧 数据库迁移：为已存在的表添加成本字段
            try:
                # 检查表结构
                cursor.execute("PRAGMA table_info(requests)")
                columns = [column[1] for column in cursor.fetchall()]
                
                # 如果缺少成本字段，添加它们
                if 'input_cost' not in columns:
                    cursor.execute('ALTER TABLE requests ADD COLUMN input_cost REAL DEFAULT 0')
                    logger.info("✅ 已添加 input_cost 字段")
                
                if 'output_cost' not in columns:
                    cursor.execute('ALTER TABLE requests ADD COLUMN output_cost REAL DEFAULT 0')
                    logger.info("✅ 已添加 output_cost 字段")
                
                if 'total_cost' not in columns:
                    cursor.execute('ALTER TABLE requests ADD COLUMN total_cost REAL DEFAULT 0')
                    logger.info("✅ 已添加 total_cost 字段")
                
                if 'currency' not in columns:
                    cursor.execute('ALTER TABLE requests ADD COLUMN currency TEXT DEFAULT "USD"')
                    logger.info("✅ 已添加 currency 字段")
                    
            except Exception as migration_error:
                logger.warning(f"数据库迁移警告: {migration_error}")
            
            # 创建索引
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_date ON requests(date)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_model ON requests(model)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON requests(timestamp)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_status ON requests(status)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_date_model ON requests(date, model)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_success ON requests(success)')
            
            conn.commit()
            conn.close()
            
            logger.info(f"✅ SQLite数据库已初始化: {self.db_path}")
            
        except Exception as e:
            logger.error(f"初始化SQLite数据库失败: {e}", exc_info=True)
    
    def write_request(self, log_entry: dict):
        """写入请求到SQLite数据库"""
        try:
            # 只写入request_end类型的日志
            if log_entry.get('type') != 'request_end':
                return
            
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 提取数据
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
            input_tokens = log_entry.get('input_tokens', 0)
            output_tokens = log_entry.get('output_tokens', 0)
            total_tokens = input_tokens + output_tokens
            
            # 提取成本信息
            cost_info = log_entry.get('cost_info') or {}
            input_cost = cost_info.get('input_cost', 0.0)
            output_cost = cost_info.get('output_cost', 0.0)
            total_cost = cost_info.get('total_cost', 0.0)
            currency = cost_info.get('currency', 'USD')
            
            # 插入或更新数据
            cursor.execute('''
                INSERT OR REPLACE INTO requests (
                    request_id, timestamp, date, model, status, success,
                    duration, error, mode, session_id, messages_count,
                    input_tokens, output_tokens, total_tokens,
                    input_cost, output_cost, total_cost, currency
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                request_id, timestamp, date, model, status, success,
                duration, error, mode, session_id, messages_count,
                input_tokens, output_tokens, total_tokens,
                input_cost, output_cost, total_cost, currency
            ))
            
            conn.commit()
            conn.close()
            
            logger.debug(f"已写入数据库: {request_id[:8]}")
            
        except Exception as e:
            logger.error(f"写入SQLite数据库失败: {e}", exc_info=True)
    
    def get_token_stats(self, start_date: str = None, end_date: str = None) -> Dict:
        """获取Token统计数据"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 构建WHERE条件
            where_clause = "WHERE 1=1"
            params = []
            
            if start_date:
                where_clause += " AND date >= ?"
                params.append(start_date)
            if end_date:
                where_clause += " AND date <= ?"
                params.append(end_date)
            
            # 获取模型统计
            query = f'''
                SELECT 
                    model,
                    COUNT(*) as request_count,
                    SUM(input_tokens) as input_tokens,
                    SUM(output_tokens) as output_tokens,
                    SUM(total_tokens) as total_tokens
                FROM requests
                {where_clause}
                GROUP BY model
                ORDER BY total_tokens DESC
            '''
            
            cursor.execute(query, params)
            model_stats = []
            for row in cursor.fetchall():
                model_stats.append({
                    'model': row[0],
                    'request_count': row[1],
                    'input_tokens': row[2],
                    'output_tokens': row[3],
                    'total_tokens': row[4]
                })
            
            # 获取每日统计
            query = f'''
                SELECT 
                    date,
                    SUM(input_tokens) as input_tokens,
                    SUM(output_tokens) as output_tokens,
                    SUM(total_tokens) as total_tokens
                FROM requests
                {where_clause}
                GROUP BY date
                ORDER BY date
            '''
            
            cursor.execute(query, params)
            daily_stats = []
            for row in cursor.fetchall():
                daily_stats.append({
                    'date': row[0],
                    'input_tokens': row[1],
                    'output_tokens': row[2],
                    'total_tokens': row[3]
                })
            
            # 获取总计（包括成本，使用COALESCE处理NULL值）
            query = f'''
                SELECT
                    SUM(input_tokens) as total_input,
                    SUM(output_tokens) as total_output,
                    SUM(total_tokens) as total_all,
                    SUM(COALESCE(input_cost, 0)) as total_input_cost,
                    SUM(COALESCE(output_cost, 0)) as total_output_cost,
                    SUM(COALESCE(total_cost, 0)) as total_cost_sum,
                    COALESCE(MAX(currency), 'USD') as currency
                FROM requests
                {where_clause}
            '''
            
            cursor.execute(query, params)
            totals = cursor.fetchone()
            
            conn.close()
            
            return {
                'model_stats': model_stats,
                'daily_stats': daily_stats,
                'total_input_tokens': totals[0] or 0,
                'total_output_tokens': totals[1] or 0,
                'total_tokens': totals[2] or 0,
                'input_cost': totals[3] or 0.0,
                'output_cost': totals[4] or 0.0,
                'total_cost': totals[5] or 0.0,
                'currency': totals[6] or 'USD',
                'models_count': len(model_stats)
            }
            
        except Exception as e:
            logger.error(f"获取Token统计失败: {e}", exc_info=True)
            return None
    
    def get_request_stats(self, start_date: str = None, end_date: str = None) -> Dict:
        """获取请求统计数据"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 构建WHERE条件
            where_clause = "WHERE 1=1"
            params = []
            
            if start_date:
                where_clause += " AND date >= ?"
                params.append(start_date)
            if end_date:
                where_clause += " AND date <= ?"
                params.append(end_date)
            
            # 获取总体统计
            query = f'''
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success,
                    SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failed
                FROM requests
                {where_clause}
            '''
            
            cursor.execute(query, params)
            totals = cursor.fetchone()
            
            # 获取每日统计
            query = f'''
                SELECT 
                    date,
                    COUNT(*) as total,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success,
                    SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failed
                FROM requests
                {where_clause}
                GROUP BY date
                ORDER BY date
            '''
            
            cursor.execute(query, params)
            daily_stats = []
            for row in cursor.fetchall():
                daily_stats.append({
                    'date': row[0],
                    'total': row[1],
                    'success': row[2],
                    'failed': row[3]
                })
            
            conn.close()
            
            return {
                'total_requests': totals[0] or 0,
                'success_requests': totals[1] or 0,
                'failed_requests': totals[2] or 0,
                'daily_stats': daily_stats
            }
            
        except Exception as e:
            logger.error(f"获取请求统计失败: {e}", exc_info=True)
            return None
    
    def get_request_details(self, request_id: str) -> Optional[Dict]:
        """从SQLite数据库获取请求详情"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row  # 使结果可以用列名访问
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT
                    request_id, timestamp, date, model, status, success,
                    duration, error, mode, session_id, messages_count,
                    input_tokens, output_tokens, total_tokens,
                    input_cost, output_cost, total_cost, currency,
                    created_at
                FROM requests
                WHERE request_id = ?
            ''', (request_id,))
            
            row = cursor.fetchone()
            conn.close()
            
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
                    'input_cost': row['input_cost'],
                    'output_cost': row['output_cost'],
                    'total_cost': row['total_cost'],
                    'currency': row['currency']
                }
            
            return None
            
        except Exception as e:
            logger.error(f"获取请求详情失败: {e}", exc_info=True)
            return None