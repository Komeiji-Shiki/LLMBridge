"""
管理面板路由
处理模型配置、系统概览、Token统计等管理功能
"""
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])


async def admin_dashboard():
    """返回管理界面HTML页面"""
    try:
        with open('admin.html', 'r', encoding='utf-8') as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>管理界面文件未找到</h1><p>请确保 admin.html 文件在正确的位置。</p>",
            status_code=404
        )


async def get_models_config(
    MODEL_ENDPOINT_MAP: dict,
    MODEL_NAME_TO_ID_MAP: dict,
    load_model_endpoint_map_func
):
    """获取所有模型配置"""
    load_model_endpoint_map_func()
    return {
        "model_endpoint_map": MODEL_ENDPOINT_MAP,
        "models": MODEL_NAME_TO_ID_MAP
    }


async def update_model_config(
    request: Request,
    load_model_endpoint_map_func
):
    """更新模型端点配置"""
    try:
        data = await request.json()
        model_name = data.get("model_name")
        config = data.get("config")
        
        if not model_name or not config:
            raise HTTPException(status_code=400, detail="缺少必要参数")
        
        # 读取现有配置
        with open('model_endpoint_map.json', 'r', encoding='utf-8') as f:
            current_config = json.load(f)
        
        # 更新配置
        current_config[model_name] = config
        
        # 写入文件
        with open('model_endpoint_map.json', 'w', encoding='utf-8') as f:
            json.dump(current_config, f, indent=2, ensure_ascii=False)
        
        # 重新加载配置
        load_model_endpoint_map_func()
        
        return {"status": "success", "message": f"模型 {model_name} 配置已更新"}
    except Exception as e:
        logger.error(f"更新模型配置失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def delete_model_config(
    model_name: str,
    load_model_endpoint_map_func
):
    """删除模型端点配置"""
    try:
        # 读取现有配置
        with open('model_endpoint_map.json', 'r', encoding='utf-8') as f:
            current_config = json.load(f)
        
        if model_name not in current_config:
            raise HTTPException(status_code=404, detail=f"模型 {model_name} 不存在")
        
        # 删除配置
        del current_config[model_name]
        
        # 写入文件
        with open('model_endpoint_map.json', 'w', encoding='utf-8') as f:
            json.dump(current_config, f, indent=2, ensure_ascii=False)
        
        # 重新加载配置
        load_model_endpoint_map_func()
        
        return {"status": "success", "message": f"模型 {model_name} 已删除"}
    except Exception as e:
        logger.error(f"删除模型配置失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def reorder_models(
    request: Request,
    load_model_endpoint_map_func
):
    """重新排序模型端点配置"""
    try:
        data = await request.json()
        new_order = data.get("order")
        
        if not new_order or not isinstance(new_order, list):
            raise HTTPException(status_code=400, detail="缺少有效的order参数")
        
        # 读取现有配置
        with open('model_endpoint_map.json', 'r', encoding='utf-8') as f:
            current_config = json.load(f)
        
        # 验证所有模型名称都存在
        for model_name in new_order:
            if model_name not in current_config:
                raise HTTPException(status_code=400, detail=f"模型 {model_name} 不存在于配置中")
        
        # 检查是否有遗漏的模型
        if set(new_order) != set(current_config.keys()):
            missing = set(current_config.keys()) - set(new_order)
            raise HTTPException(status_code=400, detail=f"顺序列表缺少以下模型: {', '.join(missing)}")
        
        # 创建新的有序字典
        reordered_config = {}
        for model_name in new_order:
            reordered_config[model_name] = current_config[model_name]
        
        # 写入文件
        with open('model_endpoint_map.json', 'w', encoding='utf-8') as f:
            json.dump(reordered_config, f, indent=2, ensure_ascii=False)
        
        # 重新加载配置
        load_model_endpoint_map_func()
        
        logger.info(f"✅ 模型顺序已更新: {' -> '.join(new_order)}")
        
        return {
            "status": "success",
            "message": f"已重新排序 {len(new_order)} 个模型",
            "order": new_order
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"重新排序模型失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def get_config(CONFIG: dict):
    """获取config.jsonc配置"""
    try:
        with open('config.jsonc', 'r', encoding='utf-8') as f:
            content = f.read()
        return {"content": content, "config": CONFIG}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def update_config(
    request: Request,
    _parse_jsonc_func,
    load_config_func
):
    """更新config.jsonc配置"""
    try:
        data = await request.json()
        content = data.get("content")
        
        if not content:
            raise HTTPException(status_code=400, detail="缺少配置内容")
        
        # 验证JSON格式
        try:
            _parse_jsonc_func(content)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"配置格式错误: {e}")
        
        # 写入文件
        with open('config.jsonc', 'w', encoding='utf-8') as f:
            f.write(content)
        
        # 重新加载配置
        load_config_func(force_reload=True)
        
        return {"status": "success", "message": "配置已更新"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新配置失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def get_overview(
    monitoring_service,
    stats_db,
    MonitorConfig,
    browser_ws,
    browser_connections: dict,
    browser_connections_lock,
    tab_connection_times: dict,
    tab_request_counts: dict,
    CONFIG: dict,
    MODEL_ENDPOINT_MAP: dict
):
    """获取系统概览信息"""
    # 获取监控统计
    summary = monitoring_service.get_summary()
    
    # 核心修复：与请求趋势使用相同的数据源优先级
    stats_from_source = summary['stats']  # 默认使用内存统计作为后备
    
    try:
        # 优先使用SQLite数据库（与/api/admin/request_stats保持一致）
        if stats_db.enabled:
            db_stats = stats_db.get_request_stats()
            if db_stats:
                stats_from_source = {
                    "total_requests": db_stats.get('total_requests', 0),
                    "success_requests": db_stats.get('success_requests', 0),
                    "failed_requests": db_stats.get('failed_requests', 0)
                }
                logger.debug(f"[OVERVIEW] ✅ 从SQLite读取: 总数={stats_from_source['total_requests']}, 成功={stats_from_source['success_requests']}, 失败={stats_from_source['failed_requests']}")
            else:
                logger.warning(f"[OVERVIEW] SQLite查询失败，尝试从stats.json读取")
                raise Exception("SQLite查询失败")
        else:
            # 回退到stats.json
            stats_path = MonitorConfig.LOG_DIR / MonitorConfig.STATS_FILE
            
            if stats_path.exists():
                with open(stats_path, 'r', encoding='utf-8') as f:
                    stats_data = json.load(f)
                
                # 使用stats.json中的总体统计
                stats_from_source = {
                    "total_requests": stats_data.get('total_requests_all_time', 0),
                    "success_requests": stats_data.get('total_success_all_time', 0),
                    "failed_requests": stats_data.get('total_failed_all_time', 0)
                }
                
                logger.debug(f"[OVERVIEW] 从stats.json读取: 总数={stats_from_source['total_requests']}, 成功={stats_from_source['success_requests']}, 失败={stats_from_source['failed_requests']}")
            else:
                logger.warning(f"[OVERVIEW] stats.json不存在，使用内存统计")
    except Exception as e:
        # 最终回退：尝试从stats.json读取
        try:
            stats_path = MonitorConfig.LOG_DIR / MonitorConfig.STATS_FILE
            if stats_path.exists():
                with open(stats_path, 'r', encoding='utf-8') as f:
                    stats_data = json.load(f)
                
                stats_from_source = {
                    "total_requests": stats_data.get('total_requests_all_time', 0),
                    "success_requests": stats_data.get('total_success_all_time', 0),
                    "failed_requests": stats_data.get('total_failed_all_time', 0)
                }
                logger.debug(f"[OVERVIEW] 回退到stats.json: {stats_from_source}")
            else:
                logger.error(f"[OVERVIEW] 所有数据源都失败，使用内存统计: {e}")
        except Exception as fallback_error:
            logger.error(f"[OVERVIEW] stats.json读取也失败，使用内存统计: {fallback_error}")
    
    # 获取标签页信息
    async with browser_connections_lock:
        tabs_info = []
        current_time = time.time()
        
        for tab_id, ws in browser_connections.items():
            connection_start = tab_connection_times.get(tab_id, current_time)
            connected_duration = current_time - connection_start
            load = tab_request_counts.get(tab_id, 0)
            
            tabs_info.append({
                "tab_id": tab_id,
                "connected": ws.client_state.name == 'CONNECTED' if ws else False,
                "active_requests": load,
                "connected_duration": connected_duration
            })
    
    return {
        "browser_connected": browser_ws is not None,
        "total_tabs": len(browser_connections),
        "tabs": tabs_info,
        "stats": stats_from_source,  # 使用与请求趋势相同的数据源
        "model_stats": summary['model_stats'],
        "active_requests": summary['active_requests_list'],
        "mode": {
            "mode": CONFIG.get("id_updater_last_mode", "direct_chat"),
            "target": CONFIG.get("id_updater_battle_target", "A")
        },
        "total_models": len(MODEL_ENDPOINT_MAP)
    }


async def get_tokenizer_info_api(get_token_counter_info_func):
    """获取tokenizer信息"""
    try:
        info = get_token_counter_info_func()
        return info
    except Exception as e:
        logger.error(f"获取tokenizer信息失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def get_tokenizer_mappings(_parse_jsonc_func):
    """获取所有tokenizer映射配置"""
    try:
        # 读取config.jsonc
        with open('config.jsonc', 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 解析JSONC
        config = _parse_jsonc_func(content)
        
        # 获取tokenizer_config，如果不存在则返回空字典
        tokenizer_config = config.get('tokenizer_config', {})
        
        return tokenizer_config
    except Exception as e:
        logger.error(f"获取tokenizer映射失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def update_all_tokenizer_mappings(
    request: Request,
    _parse_jsonc_func,
    load_config_func
):
    """批量更新所有模型的tokenizer配置"""
    try:
        data = await request.json()
        tokenizer_config = data.get("tokenizer_config")
        
        if not tokenizer_config or not isinstance(tokenizer_config, dict):
            raise HTTPException(status_code=400, detail="缺少有效的tokenizer_config参数")
        
        # 读取当前配置
        with open('config.jsonc', 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 解析JSONC
        config = _parse_jsonc_func(content)
        
        # 更新tokenizer_config
        config['tokenizer_config'] = tokenizer_config
        
        # 写回文件
        with open('config.jsonc', 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        
        # 重新加载配置
        load_config_func(force_reload=True)
        
        logger.info(f"✅ 已批量保存 {len(tokenizer_config)} 个模型的tokenizer配置")
        for model, tokenizer in tokenizer_config.items():
            logger.info(f"  - {model}: {tokenizer}")
        
        return {
            "status": "success",
            "message": f"已保存 {len(tokenizer_config)} 个模型的分词器配置",
            "count": len(tokenizer_config)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新tokenizer配置失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def merge_model_stats(
    request: Request,
    stats_db
):
    """合并多个模型的统计数据"""
    try:
        data = await request.json()
        source_models = data.get("source_models", [])
        target_model = data.get("target_model", "")
        
        if not source_models or len(source_models) < 2:
            raise HTTPException(status_code=400, detail="至少需要选择2个模型进行合并")
        
        if not target_model:
            raise HTTPException(status_code=400, detail="缺少目标模型名称")
        
        # 调用数据库合并函数
        if stats_db.enabled:
            result = stats_db.merge_models(source_models, target_model)
            if result:
                logger.info(f"✅ 成功合并 {len(source_models)} 个模型到 '{target_model}'")
                return {
                    "status": "success",
                    "message": f"已合并 {len(source_models)} 个模型",
                    "merged_count": result.get("merged_count", len(source_models)),
                    "target_model": target_model
                }
            else:
                raise HTTPException(status_code=500, detail="数据库合并操作失败")
        else:
            raise HTTPException(status_code=503, detail="SQLite数据库未启用，无法合并统计数据")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"合并模型统计失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def delete_model_stats(
    request: Request,
    stats_db
):
    """删除指定模型的统计数据"""
    try:
        data = await request.json()
        models = data.get("models", [])
        
        if not models:
            raise HTTPException(status_code=400, detail="未指定要删除的模型")
        
        # 调用数据库删除函数
        if stats_db.enabled:
            result = stats_db.delete_models(models)
            if result:
                logger.info(f"✅ 成功删除 {len(models)} 个模型的统计数据")
                return {
                    "status": "success",
                    "message": f"已删除 {len(models)} 个模型的统计数据",
                    "deleted_count": result.get("deleted_count", len(models)),
                    "models": models
                }
            else:
                raise HTTPException(status_code=500, detail="数据库删除操作失败")
        else:
            raise HTTPException(status_code=503, detail="SQLite数据库未启用，无法删除统计数据")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除模型统计失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def get_request_stats(
    start_time: str,
    end_time: str,
    stats_db,
    monitoring_service,
    MonitorConfig
):
    """获取请求次数统计，支持日期范围过滤"""
    try:
        # 优先使用SQLite数据库
        if stats_db.enabled:
            db_stats = stats_db.get_request_stats(start_time, end_time)
            if db_stats:
                logger.info(f"[REQUEST_STATS] ✅ 从SQLite读取统计数据")
                return db_stats
            else:
                logger.warning(f"[REQUEST_STATS] SQLite查询失败，回退到JSON日志")
        
        # 回退：使用JSON日志（原有逻辑）
        logger.info(f"[REQUEST_STATS] 从JSON日志读取统计数据")
        
        # 从stats.json读取总体统计
        stats_path = MonitorConfig.LOG_DIR / MonitorConfig.STATS_FILE
        total_requests = 0
        success_requests = 0
        failed_requests = 0
        
        if stats_path.exists() and not (start_time or end_time):
            # 如果没有日期过滤，直接使用stats.json的数据
            with open(stats_path, 'r', encoding='utf-8') as f:
                stats_data = json.load(f)
            
            total_requests = stats_data.get('total_requests_all_time', 0)
            success_requests = stats_data.get('total_success_all_time', 0)
            failed_requests = stats_data.get('total_failed_all_time', 0)
            
            logger.info(f"[REQUEST_STATS] 从stats.json读取: 总数={total_requests}, 成功={success_requests}, 失败={failed_requests}")
        
        # 按日期聚合请求统计（用于趋势图）
        recent_logs = monitoring_service.log_manager.read_recent_logs("requests", limit=10000)
        logger.info(f"[REQUEST_STATS] 读取到 {len(recent_logs)} 条请求日志用于趋势分析")
        
        # 日期过滤
        if start_time or end_time:
            filtered_logs = []
            for log_entry in recent_logs:
                timestamp = log_entry.get('timestamp', 0)
                if timestamp:
                    log_date = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d')
                    
                    if start_time and log_date < start_time:
                        continue
                    if end_time and log_date > end_time:
                        continue
                    
                    filtered_logs.append(log_entry)
            
            recent_logs = filtered_logs
            logger.info(f"[REQUEST_STATS] 日期过滤后剩余 {len(recent_logs)} 条记录")
            
            # 重新计算过滤后的总数
            total_requests = len(recent_logs)
            success_requests = sum(1 for log in recent_logs if log.get('success', True))
            failed_requests = total_requests - success_requests
        
        # 按日期聚合
        daily_request_stats = {}
        for log_entry in recent_logs:
            timestamp = log_entry.get('timestamp', 0)
            if not timestamp:
                continue
            
            success = log_entry.get('success', True)
            date_str = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d')
            
            if date_str not in daily_request_stats:
                daily_request_stats[date_str] = {
                    'date': date_str,
                    'total': 0,
                    'success': 0,
                    'failed': 0
                }
            
            daily_request_stats[date_str]['total'] += 1
            if success:
                daily_request_stats[date_str]['success'] += 1
            else:
                daily_request_stats[date_str]['failed'] += 1
        
        # 转换为列表并按日期排序
        daily_stats_list = list(daily_request_stats.values())
        daily_stats_list.sort(key=lambda x: x['date'])
        
        return {
            "daily_stats": daily_stats_list,
            "total_requests": total_requests,
            "success_requests": success_requests,
            "failed_requests": failed_requests
        }
    except Exception as e:
        logger.error(f"获取请求统计失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def get_token_stats(
    start_date: str,
    end_date: str,
    start_time: str,
    end_time: str,
    stats_db,
    monitoring_service,
    MODEL_ENDPOINT_MAP: dict,
    estimate_message_tokens_func,
    estimate_tokens_func
):
    """获取token用量统计，支持日期范围过滤"""
    try:
        # 兼容性：支持start_date/end_date（前端）和start_time/end_time（SQLite）
        filter_start = start_time or start_date
        filter_end = end_time or end_date
        
        # 优先使用SQLite数据库
        if stats_db.enabled:
            db_stats = stats_db.get_token_stats(filter_start, filter_end, MODEL_ENDPOINT_MAP)
            if db_stats:
                logger.info(f"[TOKEN_STATS] ✅ 从SQLite读取统计数据")
                return db_stats
            else:
                logger.warning(f"[TOKEN_STATS] SQLite查询失败，回退到JSON日志")
        
        # 回退：使用JSON日志
        logger.info(f"[TOKEN_STATS] 从JSON日志读取统计数据")
        
        # 获取模型统计数据
        model_stats_list = monitoring_service.get_model_stats()
        
        # 从日志中读取token数据
        recent_logs = monitoring_service.log_manager.read_recent_logs("requests", limit=10000)
        
        # 日期过滤
        if filter_start or filter_end:
            filtered_logs = []
            start_ts = datetime.fromisoformat(filter_start.replace("Z", "+00:00")).timestamp() if filter_start else None
            end_ts = datetime.fromisoformat(filter_end.replace("Z", "+00:00")).timestamp() if filter_end else None
            
            for log_entry in recent_logs:
                timestamp = log_entry.get('timestamp', 0)
                if timestamp:
                    if start_ts and timestamp < start_ts:
                        continue
                    if end_ts and timestamp > end_ts:
                        continue
                    
                    filtered_logs.append(log_entry)
            
            recent_logs = filtered_logs
            logger.info(f"时间过滤后剩余 {len(recent_logs)} 条记录")
        
        # 按模型聚合token统计
        model_token_stats = {}
        total_input_tokens = 0
        total_output_tokens = 0
        
        # 按日期聚合token统计
        daily_token_stats = {}
        
        for log_entry in recent_logs:
            model = log_entry.get('model', 'unknown')
            input_tokens = log_entry.get('input_tokens', 0)
            output_tokens = log_entry.get('output_tokens', 0)
            
            # 按模型统计
            if model not in model_token_stats:
                model_token_stats[model] = {
                    'model': model,
                    'input_tokens': 0,
                    'output_tokens': 0,
                    'total_tokens': 0,
                    'request_count': 0
                }
            
            model_token_stats[model]['input_tokens'] += input_tokens
            model_token_stats[model]['output_tokens'] += output_tokens
            model_token_stats[model]['total_tokens'] += (input_tokens + output_tokens)
            model_token_stats[model]['request_count'] += 1
            
            total_input_tokens += input_tokens
            total_output_tokens += output_tokens
            
            # 按日期统计
            timestamp = log_entry.get('timestamp', 0)
            if timestamp:
                date_str = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d')
                if date_str not in daily_token_stats:
                    daily_token_stats[date_str] = {
                        'date': date_str,
                        'input_tokens': 0,
                        'output_tokens': 0,
                        'total_tokens': 0
                    }
                
                daily_token_stats[date_str]['input_tokens'] += input_tokens
                daily_token_stats[date_str]['output_tokens'] += output_tokens
                daily_token_stats[date_str]['total_tokens'] += (input_tokens + output_tokens)
        
        # 转换为列表并按总token数排序
        stats_list = list(model_token_stats.values())
        stats_list.sort(key=lambda x: x['total_tokens'], reverse=True)
        
        # 转换每日统计为列表并按日期排序
        daily_stats_list = list(daily_token_stats.values())
        daily_stats_list.sort(key=lambda x: x['date'])
        
        return {
            "model_stats": stats_list,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_input_tokens + total_output_tokens,
            "models_count": len(model_token_stats),
            "daily_stats": daily_stats_list
        }
    except Exception as e:
        logger.error(f"获取token统计失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))