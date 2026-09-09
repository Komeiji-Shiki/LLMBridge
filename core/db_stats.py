"""
SQLite数据库统计查询模块
提供高性能的统计数据查询
"""

import asyncio
import sqlite3
import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from core.config_loader import CONFIG

logger = logging.getLogger(__name__)


def get_exchange_rates():
    """从全局配置读取汇率，默认 USD_TO_CNY = 7.2

    返回 (usd_to_cny, cny_to_usd) 元组。
    """
    rate_config = CONFIG.get("exchange_rate", {}) if CONFIG else {}
    usd_to_cny = float(rate_config.get("USD_TO_CNY", 7.2))
    return usd_to_cny, 1.0 / usd_to_cny

DB_PATH = Path("./logs/requests.db")

class StatsDB:
    """统计数据库查询类"""
    
    def __init__(self):
        self.db_path = DB_PATH
        # 🔧 性能：线程本地连接缓存。查询经 asyncio.to_thread 跑在线程池里，
        # 线程复用则连接复用，避免每次查询新建连接 + 3 条 PRAGMA、
        # 16MB 页面缓存每次作废的开销
        self._local = threading.local()
        self.enabled = self.db_path.exists()
        if self.enabled:
            logger.info(f"✅ SQLite数据库已启用: {self.db_path}")
            self._ensure_indexes()
        else:
            logger.warning(f"⚠️ SQLite数据库不存在，将使用JSON日志（建库后自动启用）")

    def _check_enabled(self) -> bool:
        """惰性重检数据库是否就绪。

        🔧 修复：首次运行时 requests.db 可能在本模块导入之后才被
        SQLiteLogger 创建，旧版只在 __init__ 判定一次，会导致统计功能
        直到重启前一直禁用。
        """
        if not self.enabled and self.db_path.exists():
            self.enabled = True
            logger.info(f"✅ SQLite数据库已就绪（惰性启用）: {self.db_path}")
            self._ensure_indexes()
        return self.enabled

    def _ensure_indexes(self):
        """🔧 D11 性能优化：确保复合索引存在，加速 GROUP BY 和范围查询"""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_model_timestamp ON requests(model, timestamp)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_timestamp_success ON requests(timestamp, success)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_currency_cost ON requests(currency, total_cost)')
            conn.commit()
            logger.info("🔧 SQLite 复合索引已就绪")
        except Exception as e:
            logger.warning(f"创建复合索引失败（不影响功能）: {e}")
    
    def _get_connection(self):
        """获取当前线程的数据库连接（线程本地复用，启用 WAL 模式和性能优化）"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(self.db_path)
        # 🔧 D11 性能优化：WAL 模式允许读写并发，不再互斥
        conn.execute("PRAGMA journal_mode=WAL")
        # NORMAL 同步模式：平衡安全和性能（WAL 模式下足够安全）
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-16000")  # 16MB 页面缓存，减少磁盘 IO
        conn.execute("PRAGMA busy_timeout=5000")  # 5秒忙等待，避免与写连接冲突时立即 SQLITE_BUSY
        self._local.conn = conn
        return conn

    def _discard_connection(self):
        """异常后丢弃当前线程的连接（可能已处于不确定状态），下次查询自动重建"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None
    
    
    @staticmethod
    def _parse_time_bound(time_str: str, is_end: bool = False) -> float:
        """解析时间边界字符串为 Unix 时间戳（秒）。
        
        - ISO 8601 格式（如 "2026-07-27T14:30:00"）：直接解析。
        - 纯日期格式（YYYY-MM-DD）：start 返回当天 00:00:00，end 返回下一天 00:00:00。
          配合 SQL 的 timestamp < end_ts（半开区间），确保结束日整天数据不丢。
        """
        try:
            dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
            if is_end and len(time_str) == 10:
                dt += timedelta(days=1)
            return dt.timestamp()
        except (ValueError, AttributeError):
            dt = datetime.strptime(time_str, "%Y-%m-%d")
            if is_end:
                dt = dt + timedelta(days=1)
            return dt.timestamp()

    def get_token_stats(self, start_time=None, end_time=None, model_config=None, rpm_period=None):
        """聚合模型、每日用量和独立时间段速率，保留异步包装接口。"""
        if not self._check_enabled():
            return None
        from core.statistics_queries import query_token_stats
        try:
            return query_token_stats(
                self._get_connection(),
                self._parse_time_bound(start_time) if start_time else None,
                self._parse_time_bound(end_time, True) if end_time else None,
                model_config, rpm_period, get_exchange_rates()[0],
            )
        except Exception as error:
            logger.error("获取 Token 统计失败: %s", error, exc_info=True)
            self._discard_connection()
            return None

    def get_request_summary(self, start_time: Optional[str] = None, end_time: Optional[str] = None) -> Optional[Dict]:
        """
        获取轻量级请求汇总统计（不包含每日聚合）
        
        Args:
            start_time: 开始时间 (ISO 8601)
            end_time: 结束时间 (ISO 8601)
        
        Returns:
            仅包含总请求/成功/失败的字典
        """
        if not self._check_enabled():
            return None
        
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            where_clause = "WHERE 1=1"
            params = []

            if start_time:
                start_ts = self._parse_time_bound(start_time, is_end=False)
                where_clause += " AND timestamp >= ?"
                params.append(start_ts)
            if end_time:
                end_ts = self._parse_time_bound(end_time, is_end=True)
                where_clause += " AND timestamp < ?"
                params.append(end_ts)
            
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
            
            return {
                'total_requests': totals[0] or 0,
                'success_requests': totals[1] or 0,
                'failed_requests': totals[2] or 0
            }
            
        except Exception as e:
            logger.error(f"获取请求汇总统计失败: {e}", exc_info=True)
            self._discard_connection()
            return None

    def get_request_stats(self, start_time: Optional[str] = None, end_time: Optional[str] = None) -> Optional[Dict]:
        """
        获取请求统计数据
        
        Args:
            start_time: 开始时间 (ISO 8601)
            end_time: 结束时间 (ISO 8601)
        
        Returns:
            包含请求统计和每日统计的字典
        """
        if not self._check_enabled():
            return None
        
        try:
            summary = self.get_request_summary(start_time, end_time)
            if summary is None:
                return None

            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 构建WHERE条件
            where_clause = "WHERE 1=1"
            params = []

            if start_time:
                start_ts = self._parse_time_bound(start_time, is_end=False)
                where_clause += " AND timestamp >= ?"
                params.append(start_ts)
            if end_time:
                end_ts = self._parse_time_bound(end_time, is_end=True)
                where_clause += " AND timestamp < ?"
                params.append(end_ts)
            
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
            
            return {
                'total_requests': summary['total_requests'],
                'success_requests': summary['success_requests'],
                'failed_requests': summary['failed_requests'],
                'daily_stats': daily_stats
            }
            
        except Exception as e:
            logger.error(f"获取请求统计失败: {e}", exc_info=True)
            self._discard_connection()
            return None
    
    def merge_models(self, source_models: List[str], target_model: str) -> Optional[Dict]:
        """
        合并多个模型的统计数据到目标模型
        
        Args:
            source_models: 源模型名称列表
            target_model: 目标模型名称
        
        Returns:
            合并结果字典
        """
        if not self._check_enabled():
            return None
        
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 开始事务
            cursor.execute("BEGIN TRANSACTION")
            
            # 更新所有源模型的记录，将model字段改为target_model
            placeholders = ','.join('?' * len(source_models))
            query = f"UPDATE requests SET model = ? WHERE model IN ({placeholders})"
            cursor.execute(query, [target_model] + source_models)
            
            updated_count = cursor.rowcount
            
            # 提交事务
            conn.commit()
            
            logger.info(f"✅ 数据库合并完成: 更新了 {updated_count} 条记录")
            
            return {
                "merged_count": len(source_models),
                "updated_records": updated_count,
                "target_model": target_model
            }
            
        except Exception as e:
            logger.error(f"合并模型统计失败: {e}", exc_info=True)
            try:
                conn.rollback()
            except Exception:
                pass  # conn 可能未定义或已关闭
            self._discard_connection()
            return None
    
    def delete_models(self, models: List[str]) -> Optional[Dict]:
        """
        删除指定模型的所有统计数据
        
        Args:
            models: 要删除的模型名称列表
        
        Returns:
            删除结果字典
        """
        if not self._check_enabled():
            return None
        
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 开始事务
            cursor.execute("BEGIN TRANSACTION")
            
            # 删除指定模型的所有记录
            placeholders = ','.join('?' * len(models))
            query = f"DELETE FROM requests WHERE model IN ({placeholders})"
            cursor.execute(query, models)
            
            deleted_count = cursor.rowcount
            
            # 提交事务
            conn.commit()
            
            logger.info(f"✅ 数据库删除完成: 删除了 {deleted_count} 条记录")
            
            return {
                "deleted_count": len(models),
                "deleted_records": deleted_count,
                "models": models
            }
            
        except Exception as e:
            logger.error(f"删除模型统计失败: {e}", exc_info=True)
            try:
                conn.rollback()
            except Exception:
                pass  # conn 可能未定义或已关闭
            self._discard_connection()
            return None

    def recalculate_costs(self, model_config: dict) -> Optional[Dict]:
        """Compatibility entry point for a read-only current-price comparison."""
        from core.usage_analysis import estimate_current_prices
        if not self.db_path.exists():
            return None
        return estimate_current_prices(self.db_path, model_config)

    async def get_token_stats_async(self, start_time=None, end_time=None, model_config=None, rpm_period=None):
        """在线程池查询 Token 统计，避免阻塞请求事件循环。"""
        return await asyncio.to_thread(self.get_token_stats, start_time, end_time, model_config, rpm_period)

    async def get_request_stats_async(self, start_time=None, end_time=None):
        """在线程池查询请求统计。"""
        return await asyncio.to_thread(self.get_request_stats, start_time, end_time)

    async def get_request_summary_async(self, start_time=None, end_time=None):
        """在线程池查询概览汇总。"""
        return await asyncio.to_thread(self.get_request_summary, start_time, end_time)

# 创建全局实例
stats_db = StatsDB()
