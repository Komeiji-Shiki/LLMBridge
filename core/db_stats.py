"""
SQLite数据库统计查询模块
提供高性能的统计数据查询
"""

import sqlite3
import logging
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

DB_PATH = Path("./logs/requests.db")

class StatsDB:
    """统计数据库查询类"""
    
    def __init__(self):
        self.db_path = DB_PATH
        self.enabled = self.db_path.exists()
        if self.enabled:
            logger.info(f"✅ SQLite数据库已启用: {self.db_path}")
        else:
            logger.warning(f"⚠️ SQLite数据库不存在，将使用JSON日志")
    
    def _get_connection(self):
        """获取数据库连接"""
        return sqlite3.connect(self.db_path)
    
    def get_token_stats(self, start_time: str = None, end_time: str = None, model_config: dict = None) -> Dict:
        """
        获取Token统计数据
        
        Args:
            start_time: 开始时间 (ISO 8601格式或YYYY-MM-DD日期格式)
            end_time: 结束时间 (ISO 8601格式或YYYY-MM-DD日期格式)
            model_config: 模型配置字典，用于获取display_name
        
        Returns:
            包含模型统计和每日统计的字典
        """
        if not self.enabled:
            return None
        
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 构建WHERE条件
            where_clause = "WHERE 1=1"
            params = []
            
            # 记录查询的时间范围（用于RPM/TPM计算）
            start_ts = None
            end_ts = None
            
            if start_time:
                # 尝试解析为ISO 8601时间戳，如果失败则作为日期处理
                try:
                    start_ts = datetime.fromisoformat(start_time.replace("Z", "+00:00")).timestamp()
                except (ValueError, AttributeError):
                    # 作为日期处理（YYYY-MM-DD），设置为当天00:00:00
                    start_ts = datetime.strptime(start_time, "%Y-%m-%d").timestamp()
                
                where_clause += " AND timestamp >= ?"
                params.append(start_ts)
            
            if end_time:
                # 尝试解析为ISO 8601时间戳，如果失败则作为日期处理
                try:
                    end_ts = datetime.fromisoformat(end_time.replace("Z", "+00:00")).timestamp()
                except (ValueError, AttributeError):
                    # 作为日期处理（YYYY-MM-DD），设置为当天23:59:59
                    end_ts = datetime.strptime(end_time, "%Y-%m-%d").replace(
                        hour=23, minute=59, second=59
                    ).timestamp()
                
                where_clause += " AND timestamp <= ?"
                params.append(end_ts)
            
            # 如果没有提供时间范围，默认使用最近24小时
            if not start_time and not end_time:
                import time
                end_ts = time.time()
                start_ts = end_ts - (24 * 60 * 60)  # 24小时前
            
            # 获取模型统计（包含成本信息）
            query = f'''
                SELECT
                    model,
                    COUNT(*) as request_count,
                    SUM(input_tokens) as input_tokens,
                    SUM(output_tokens) as output_tokens,
                    SUM(total_tokens) as total_tokens,
                    SUM(COALESCE(input_cost, 0)) as input_cost,
                    SUM(COALESCE(output_cost, 0)) as output_cost,
                    SUM(COALESCE(total_cost, 0)) as total_cost,
                    COALESCE(MAX(currency), 'USD') as currency
                FROM requests
                {where_clause}
                GROUP BY model
                ORDER BY total_tokens DESC
            '''
            
            cursor.execute(query, params)
            model_stats = []
            for row in cursor.fetchall():
                model_name = row[0]
                display_name = model_name  # 默认使用model_name
                
                # 尝试从配置中获取display_name
                if model_config and model_name in model_config:
                    config = model_config[model_name]
                    # 处理列表配置（取第一个）
                    if isinstance(config, list) and config:
                        config = config[0]
                    # 提取display_name
                    if isinstance(config, dict):
                        display_name = config.get('display_name', model_name)
                
                # 计算RPM和TPM（基于查询的时间范围）
                rpm = 0.0
                tpm = 0.0
                
                if start_ts and end_ts:
                    # 使用查询指定的时间范围（更准确）
                    time_span_minutes = (end_ts - start_ts) / 60.0
                    if time_span_minutes > 0:
                        rpm = row[1] / time_span_minutes  # requests / minutes
                        tpm = row[4] / time_span_minutes  # tokens / minutes
                # 如果没有指定时间范围，RPM/TPM为0（因为无法确定时间跨度）
                
                model_stats.append({
                    'model': model_name,
                    'display_name': display_name,
                    'request_count': row[1],
                    'input_tokens': row[2],
                    'output_tokens': row[3],
                    'total_tokens': row[4],
                    'input_cost': row[5],
                    'output_cost': row[6],
                    'total_cost': row[7],
                    'currency': row[8],
                    'rpm': round(rpm, 2),
                    'tpm': round(tpm, 2)
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
            
            # 获取总计（包括成本，使用COALESCE处理NULL值确保向后兼容）
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
    
    def get_request_stats(self, start_time: str = None, end_time: str = None) -> Dict:
        """
        获取请求统计数据
        
        Args:
            start_time: 开始时间 (ISO 8601)
            end_time: 结束时间 (ISO 8601)
        
        Returns:
            包含请求统计和每日统计的字典
        """
        if not self.enabled:
            return None
        
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 构建WHERE条件
            where_clause = "WHERE 1=1"
            params = []

            if start_time:
                start_ts = datetime.fromisoformat(start_time.replace("Z", "+00:00")).timestamp()
                where_clause += " AND timestamp >= ?"
                params.append(start_ts)
            if end_time:
                end_ts = datetime.fromisoformat(end_time.replace("Z", "+00:00")).timestamp()
                where_clause += " AND timestamp <= ?"
                params.append(end_ts)
            
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
    
    def merge_models(self, source_models: List[str], target_model: str) -> Dict:
        """
        合并多个模型的统计数据到目标模型
        
        Args:
            source_models: 源模型名称列表
            target_model: 目标模型名称
        
        Returns:
            合并结果字典
        """
        if not self.enabled:
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
            conn.close()
            
            logger.info(f"✅ 数据库合并完成: 更新了 {updated_count} 条记录")
            
            return {
                "merged_count": len(source_models),
                "updated_records": updated_count,
                "target_model": target_model
            }
            
        except Exception as e:
            logger.error(f"合并模型统计失败: {e}", exc_info=True)
            if conn:
                conn.rollback()
                conn.close()
            return None
    
    def delete_models(self, models: List[str]) -> Dict:
        """
        删除指定模型的所有统计数据
        
        Args:
            models: 要删除的模型名称列表
        
        Returns:
            删除结果字典
        """
        if not self.enabled:
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
            conn.close()
            
            logger.info(f"✅ 数据库删除完成: 删除了 {deleted_count} 条记录")
            
            return {
                "deleted_count": len(models),
                "deleted_records": deleted_count,
                "models": models
            }
            
        except Exception as e:
            logger.error(f"删除模型统计失败: {e}", exc_info=True)
            if conn:
                conn.rollback()
                conn.close()
            return None

    def recalculate_costs(self, model_config: dict) -> Dict:
        """
        重新计算所有请求的费用（启动时调用）
        
        Args:
            model_config: 模型配置字典，包含计费信息
        
        Returns:
            重算结果字典
        """
        if not self.enabled:
            return None
        
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # 获取所有有计费配置的模型
            pricing_models = {}
            for model_name, config in model_config.items():
                # 处理列表配置（取第一个）
                if isinstance(config, list) and config:
                    config = config[0]
                
                # 提取pricing配置
                if isinstance(config, dict) and 'pricing' in config:
                    pricing_models[model_name] = config['pricing']
            
            if not pricing_models:
                logger.info("💰 没有配置计费的模型，跳过费用重算")
                return None
            
            logger.info(f"💰 找到 {len(pricing_models)} 个配置了计费的模型")
            
            # 开始事务
            cursor.execute("BEGIN TRANSACTION")
            
            updated_count = 0
            total_cost_sum_usd = 0.0  # 🔧 统一换算为USD
            cny_to_usd_rate = 0.14  # 🔧 CNY到USD的汇率（约7:1）
            
            # 逐个模型重算费用
            for model_name, pricing in pricing_models.items():
                input_price = float(pricing.get('input', 0))  # 🔧 强制转换为浮点数
                output_price = float(pricing.get('output', 0))  # 🔧 强制转换为浮点数
                unit = float(pricing.get('unit', 1000000))  # 🔧 强制转换为浮点数
                model_currency = pricing.get('currency', 'USD')
                
                # 更新该模型的所有记录（使用浮点数运算）
                query = '''
                    UPDATE requests
                    SET
                        input_cost = (input_tokens * ?) / ?,
                        output_cost = (output_tokens * ?) / ?,
                        total_cost = ((input_tokens * ?) + (output_tokens * ?)) / ?,
                        currency = ?
                    WHERE model = ?
                '''
                
                cursor.execute(query, (
                    input_price, unit,
                    output_price, unit,
                    input_price, output_price, unit,
                    model_currency,
                    model_name
                ))
                
                model_updated = cursor.rowcount
                updated_count += model_updated
                
                # 计算该模型的总成本
                cursor.execute(
                    "SELECT SUM(total_cost) FROM requests WHERE model = ?",
                    (model_name,)
                )
                model_total = cursor.fetchone()[0] or 0
                
                # 🔧 将CNY换算为USD后累加
                if model_currency == 'CNY':
                    model_total_usd = model_total * cny_to_usd_rate
                    total_cost_sum_usd += model_total_usd
                    logger.info(f"  ✅ {model_name}: 更新 {model_updated} 条记录, 总成本: {model_total:.4f} {model_currency} (≈ {model_total_usd:.4f} USD)")
                else:
                    total_cost_sum_usd += model_total
                    logger.info(f"  ✅ {model_name}: 更新 {model_updated} 条记录, 总成本: {model_total:.4f} {model_currency}")
            
            # 提交事务
            conn.commit()
            conn.close()
            
            return {
                "updated_count": updated_count,
                "total_cost": total_cost_sum_usd,  # 🔧 返回USD总和
                "currency": "USD",  # 🔧 统一显示为USD
                "models_count": len(pricing_models)
            }
            
        except Exception as e:
            logger.error(f"重算费用失败: {e}", exc_info=True)
            if conn:
                conn.rollback()
                conn.close()
            return None

# 创建全局实例
stats_db = StatsDB()