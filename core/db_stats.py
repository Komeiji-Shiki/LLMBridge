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
            return datetime.fromisoformat(time_str.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            dt = datetime.strptime(time_str, "%Y-%m-%d")
            if is_end:
                dt = dt + timedelta(days=1)
            return dt.timestamp()

    def get_token_stats(self, start_time: Optional[str] = None, end_time: Optional[str] = None, model_config: Optional[dict] = None, rpm_period: Optional[str] = None) -> Optional[Dict]:
        """
        获取Token统计数据
        
        Args:
            start_time: 开始时间 (ISO 8601格式或YYYY-MM-DD日期格式)，用于筛选统计数据
            end_time: 结束时间 (ISO 8601格式或YYYY-MM-DD日期格式)，用于筛选统计数据
            model_config: 模型配置字典，用于获取display_name
            rpm_period: RPM/TPM计算的时间周期，'day'=24小时, 'hour'=1小时。独立于统计筛选器
        
        Returns:
            包含模型统计和每日统计的字典
        """
        if not self._check_enabled():
            return None
        
        try:
            import time as time_module
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 构建WHERE条件（用于Token统计、成本统计、每日趋势等）
            where_clause = "WHERE 1=1"
            params = []
            
            if start_time:
                start_ts = self._parse_time_bound(start_time, is_end=False)
                where_clause += " AND timestamp >= ?"
                params.append(start_ts)
            
            if end_time:
                # 半开区间：结束日+1天 00:00:00，配合 timestamp < ? 不漏掉结束日数据
                end_ts = self._parse_time_bound(end_time, is_end=True)
                where_clause += " AND timestamp < ?"
                params.append(end_ts)
            
            # RPM/TPM 使用独立的时间范围（不受日期筛选器影响）
            now_ts = time_module.time()
            if rpm_period == 'hour':
                rpm_start_ts = now_ts - (60 * 60)  # 1小时前
                rpm_minutes = 60.0
            else:  # 默认 'day'
                rpm_start_ts = now_ts - (24 * 60 * 60)  # 24小时前
                rpm_minutes = 1440.0
            
            # 获取模型统计（包含成本信息和缓存命中）
            # 🔧 修复：不能用 MAX(currency) 判定模型货币
            # 少量异常失败行（如 cost_info 缺失时写成 USD）会污染整模型的显示货币
            query = f'''
                SELECT
                    model,
                    COUNT(*) as request_count,
                    SUM(input_tokens) as input_tokens,
                    SUM(output_tokens) as output_tokens,
                    SUM(total_tokens) as total_tokens,
                    SUM(COALESCE(cached_tokens, 0)) as cached_tokens,
                    SUM(COALESCE(cached_cost, 0)) as cached_cost,
                    SUM(COALESCE(input_cost, 0)) as input_cost,
                    SUM(COALESCE(output_cost, 0)) as output_cost,
                    SUM(COALESCE(total_cost, 0)) as total_cost,
                    SUM(CASE WHEN currency = 'USD' THEN 1 ELSE 0 END) as usd_rows,
                    SUM(CASE WHEN currency = 'CNY' THEN 1 ELSE 0 END) as cny_rows,
                    SUM(CASE WHEN currency = 'USD' AND COALESCE(total_cost, 0) > 0 THEN 1 ELSE 0 END) as usd_cost_rows,
                    SUM(CASE WHEN currency = 'CNY' AND COALESCE(total_cost, 0) > 0 THEN 1 ELSE 0 END) as cny_cost_rows,
                    MAX(currency) as fallback_currency
                FROM requests
                {where_clause}
                GROUP BY model
                ORDER BY total_tokens DESC
            '''
            
            cursor.execute(query, params)
            model_rows = cursor.fetchall()
            
            # 为每个模型单独查询 rpm_period 时间范围内的数据（用于计算 RPM/TPM）
            rpm_data = {}
            rpm_query = '''
                SELECT model, COUNT(*) as req_count, SUM(total_tokens) as total_tokens
                FROM requests
                WHERE timestamp >= ? AND timestamp <= ?
                GROUP BY model
            '''
            cursor.execute(rpm_query, [rpm_start_ts, now_ts])
            for rpm_row in cursor.fetchall():
                rpm_data[rpm_row[0]] = {
                    'request_count': rpm_row[1],
                    'total_tokens': rpm_row[2] or 0
                }

            rate_stats = {
                'period': 'hour' if rpm_period == 'hour' else 'day',
                'minutes': rpm_minutes,
                'request_count': sum(item['request_count'] for item in rpm_data.values()),
                'total_tokens': sum(item['total_tokens'] for item in rpm_data.values())
            }
            
            model_stats = []
            # 同一模型可能在历史上切换过货币，原币金额不能直接相加。
            # 与每日汇总一致，展示时按当前配置汇率转换，不改写历史记录。
            USD_TO_CNY, CNY_TO_USD = get_exchange_rates()
            cursor.execute(f'''
                SELECT model, COALESCE(currency, 'USD'),
                       SUM(COALESCE(cached_cost, 0)), SUM(COALESCE(input_cost, 0)),
                       SUM(COALESCE(output_cost, 0)), SUM(COALESCE(total_cost, 0))
                FROM requests {where_clause}
                GROUP BY model, COALESCE(currency, 'USD')
            ''', params)
            model_costs = {}
            for cost_row in cursor.fetchall():
                model_costs.setdefault(cost_row[0], []).append(cost_row[1:])
            for row in model_rows:
                model_name = row[0]
                display_name = model_name  # 默认使用model_name
                
                config_currency = None

                # 尝试从配置中获取 display_name 和 pricing.currency
                if model_config and model_name in model_config:
                    config = model_config[model_name]
                    # 处理列表配置（取第一个）
                    if isinstance(config, list) and config:
                        config = config[0]
                    # 提取 display_name / pricing.currency
                    if isinstance(config, dict):
                        display_name = config.get('display_name', model_name)
                        pricing = config.get('pricing')
                        if isinstance(pricing, dict):
                            config_currency = pricing.get('currency')

                usd_rows = row[10] or 0
                cny_rows = row[11] or 0
                usd_cost_rows = row[12] or 0
                cny_cost_rows = row[13] or 0
                fallback_currency = row[14]

                # 🔧 修复：优先使用模型配置中的货币；否则根据“有实际成本的记录”推断
                if config_currency:
                    display_currency = config_currency
                elif usd_cost_rows != cny_cost_rows:
                    display_currency = 'USD' if usd_cost_rows > cny_cost_rows else 'CNY'
                elif usd_rows != cny_rows:
                    display_currency = 'USD' if usd_rows > cny_rows else 'CNY'
                else:
                    display_currency = fallback_currency or 'USD'
                
                # 计算RPM和TPM（使用 rpm_period 时间范围的数据，独立于统计筛选器）
                rpm = 0.0
                tpm = 0.0
                
                model_rpm_data = rpm_data.get(model_name)
                if model_rpm_data and rpm_minutes > 0:
                    rpm = model_rpm_data['request_count'] / rpm_minutes
                    tpm = model_rpm_data['total_tokens'] / rpm_minutes
                
                converted_costs = [0.0] * 4
                for currency, *amounts in model_costs.get(model_name, []):
                    factor = (USD_TO_CNY if currency == 'USD' and display_currency == 'CNY'
                              else CNY_TO_USD if currency == 'CNY' and display_currency == 'USD' else 1.0)
                    for index, amount in enumerate(amounts):
                        converted_costs[index] += amount * factor
                model_stats.append({
                    'model': model_name,
                    'display_name': display_name,
                    'request_count': row[1],
                    'input_tokens': row[2],
                    'output_tokens': row[3],
                    'total_tokens': row[4],
                    'cached_tokens': row[5],  # 🔧 新增：缓存命中tokens
                    'cached_cost': converted_costs[0],
                    'input_cost': converted_costs[1],
                    'output_cost': converted_costs[2],
                    'total_cost': converted_costs[3],
                    'currency': display_currency,
                    'rpm': round(rpm, 2),
                    'tpm': round(tpm, 2)
                })
            
            # 获取每日统计
            # 汇率（从配置读取，默认 7.2）
            USD_TO_CNY, CNY_TO_USD = get_exchange_rates()

            query = f'''
                SELECT 
                    date,
                    SUM(input_tokens) as input_tokens,
                    SUM(output_tokens) as output_tokens,
                    SUM(total_tokens) as total_tokens,
                    SUM(COALESCE(cached_tokens, 0)) as cached_tokens,
                    SUM(CASE WHEN currency = 'USD' THEN COALESCE(total_cost, 0) ELSE 0 END) as cost_usd,
                    SUM(CASE WHEN currency = 'CNY' THEN COALESCE(total_cost, 0) ELSE 0 END) as cost_cny
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
                    'total_tokens': row[3],
                    'cached_tokens': row[4],
                    # 成本：CNY 按汇率换算为 USD 统一展示
                    'cost_usd': round((row[5] or 0) + (row[6] or 0) * CNY_TO_USD, 6),
                    'cost_cny': round((row[5] or 0) * USD_TO_CNY + (row[6] or 0), 6)
                })
            
            # 获取总计 Token
            query = f'''
                SELECT
                    SUM(input_tokens) as total_input,
                    SUM(output_tokens) as total_output,
                    SUM(total_tokens) as total_all,
                    SUM(COALESCE(cached_tokens, 0)) as total_cached
                FROM requests
                {where_clause}
            '''
            cursor.execute(query, params)
            token_totals = cursor.fetchone()
            
            # 按货币分组汇总成本
            query = f'''
                SELECT
                    COALESCE(currency, 'USD') as curr,
                    SUM(COALESCE(input_cost, 0)) as input_cost,
                    SUM(COALESCE(output_cost, 0)) as output_cost,
                    SUM(COALESCE(total_cost, 0)) as total_cost
                FROM requests
                {where_clause}
                GROUP BY COALESCE(currency, 'USD')
            '''
            cursor.execute(query, params)
            cost_rows = cursor.fetchall()

            # 按货币整理原始成本（汇率沿用上方从配置读取的值，避免硬编码不一致）
            cost_by_currency = {}
            for row in cost_rows:
                curr = row[0] or 'USD'
                cost_by_currency[curr] = {
                    'input_cost': row[1] or 0.0,
                    'output_cost': row[2] or 0.0,
                    'total_cost': row[3] or 0.0
                }
            
            # 统一换算为 USD 总计
            total_input_cost_usd = 0.0
            total_output_cost_usd = 0.0
            total_cost_usd = 0.0
            # 统一换算为 CNY 总计
            total_input_cost_cny = 0.0
            total_output_cost_cny = 0.0
            total_cost_cny = 0.0
            
            for curr, costs in cost_by_currency.items():
                if curr == 'USD':
                    total_input_cost_usd += costs['input_cost']
                    total_output_cost_usd += costs['output_cost']
                    total_cost_usd += costs['total_cost']
                    total_input_cost_cny += costs['input_cost'] * USD_TO_CNY
                    total_output_cost_cny += costs['output_cost'] * USD_TO_CNY
                    total_cost_cny += costs['total_cost'] * USD_TO_CNY
                elif curr == 'CNY':
                    total_input_cost_usd += costs['input_cost'] * CNY_TO_USD
                    total_output_cost_usd += costs['output_cost'] * CNY_TO_USD
                    total_cost_usd += costs['total_cost'] * CNY_TO_USD
                    total_input_cost_cny += costs['input_cost']
                    total_output_cost_cny += costs['output_cost']
                    total_cost_cny += costs['total_cost']
                else:
                    # 未知货币按 USD 处理
                    total_input_cost_usd += costs['input_cost']
                    total_output_cost_usd += costs['output_cost']
                    total_cost_usd += costs['total_cost']
                    total_input_cost_cny += costs['input_cost'] * USD_TO_CNY
                    total_output_cost_cny += costs['output_cost'] * USD_TO_CNY
                    total_cost_cny += costs['total_cost'] * USD_TO_CNY
            
            return {
                'model_stats': model_stats,
                'daily_stats': daily_stats,
                'total_input_tokens': token_totals[0] or 0,
                'total_output_tokens': token_totals[1] or 0,
                'total_tokens': token_totals[2] or 0,
                'total_cached_tokens': token_totals[3] or 0,
                # 兼容旧字段（默认 USD）
                'input_cost': total_input_cost_usd,
                'output_cost': total_output_cost_usd,
                'total_cost': total_cost_usd,
                'currency': 'USD',
                # 新增：按货币换算的总计
                'cost_usd': {
                    'input_cost': round(total_input_cost_usd, 6),
                    'output_cost': round(total_output_cost_usd, 6),
                    'total_cost': round(total_cost_usd, 6)
                },
                'cost_cny': {
                    'input_cost': round(total_input_cost_cny, 6),
                    'output_cost': round(total_output_cost_cny, 6),
                    'total_cost': round(total_cost_cny, 6)
                },
                'cost_by_currency': cost_by_currency,
                'exchange_rate': {'USD_TO_CNY': USD_TO_CNY, 'CNY_TO_USD': CNY_TO_USD},
                'rate_stats': rate_stats,
                'models_count': len(model_stats)
            }
            
        except Exception as e:
            logger.error(f"获取Token统计失败: {e}", exc_info=True)
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

# 创建全局实例
stats_db = StatsDB()
