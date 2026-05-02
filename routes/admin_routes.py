"""
管理面板路由
处理模型配置、系统概览、Token统计等管理功能
"""
import asyncio
import json
import logging
import time
import aiohttp
from datetime import datetime
from pathlib import Path
from core.config_loader import CONFIG
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.responses import Response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])

# 🔧 性能修复：改为 asyncio.Lock，避免在 async 函数中阻塞事件循环
_ADMIN_STATS_CACHE_LOCK = asyncio.Lock()
_ADMIN_STATS_CACHE = {
    "overview": {},
    "request_stats": {},
    "token_stats": {}
}
_ADMIN_STATS_CACHE_TTL_SECONDS = {
    "overview": 10.0,
    "request_stats": 15.0,
    "token_stats": 15.0
}


def _build_admin_cache_key(*parts) -> str:
    return json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)


async def _get_admin_cached_response(cache_name: str, cache_key: str):
    now = time.time()
    ttl = _ADMIN_STATS_CACHE_TTL_SECONDS[cache_name]

    async with _ADMIN_STATS_CACHE_LOCK:
        bucket = _ADMIN_STATS_CACHE[cache_name]
        entry = bucket.get(cache_key)
        if not entry:
            return None

        if now - entry["timestamp"] > ttl:
            del bucket[cache_key]
            return None

        return entry["value"]


async def _set_admin_cached_response(cache_name: str, cache_key: str, value):
    async with _ADMIN_STATS_CACHE_LOCK:
        _ADMIN_STATS_CACHE[cache_name][cache_key] = {
            "timestamp": time.time(),
            "value": value
        }


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
    """更新模型端点配置（支持模型名称重命名）"""
    try:
        data = await request.json()
        model_name = data.get("model_name")
        old_model_name = data.get("old_model_name")
        config = data.get("config")
        
        if not model_name or config is None:
            raise HTTPException(status_code=400, detail="缺少必要参数")
        
        # 读取现有配置
        with open('model_endpoint_map.json', 'r', encoding='utf-8') as f:
            current_config = json.load(f)
        
        if old_model_name:
            if old_model_name not in current_config:
                raise HTTPException(status_code=404, detail=f"原模型 {old_model_name} 不存在")
            
            if old_model_name != model_name and model_name in current_config:
                raise HTTPException(status_code=409, detail=f"模型 {model_name} 已存在，请使用其他名称")
            
            if old_model_name != model_name:
                # 保持原有顺序，在旧位置替换为新名称
                updated_config = {}
                for existing_name, existing_config in current_config.items():
                    if existing_name == old_model_name:
                        updated_config[model_name] = config
                    else:
                        updated_config[existing_name] = existing_config
                current_config = updated_config
                message = f"模型 {old_model_name} 已重命名为 {model_name}"
            else:
                current_config[model_name] = config
                message = f"模型 {model_name} 配置已更新"
        else:
            # 新增或直接覆盖
            current_config[model_name] = config
            message = f"模型 {model_name} 配置已更新"
        
        # 写入文件
        with open('model_endpoint_map.json', 'w', encoding='utf-8') as f:
            json.dump(current_config, f, indent=2, ensure_ascii=False)
        
        # 重新加载配置
        load_model_endpoint_map_func()
        
        return {"status": "success", "message": message}
    except HTTPException:
        raise
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
    # 🔧 A1 修复：使用 async 版本，避免 threading.Lock 阻塞事件循环
    summary = await monitoring_service.get_summary_async()
    
    # 请求统计：优先走缓存，避免重启后首屏冷查询
    stats_from_source = await _get_admin_cached_response("overview", "stats")
    if stats_from_source is None:
        stats_from_source = summary['stats']  # 默认使用内存统计作为后备
        
        try:
            if stats_db.enabled:
                db_stats = await stats_db.get_request_summary_async()
                if db_stats:
                    stats_from_source = {
                        "total_requests": db_stats.get('total_requests', 0),
                        "success_requests": db_stats.get('success_requests', 0),
                        "failed_requests": db_stats.get('failed_requests', 0)
                    }
                    logger.debug(f"[OVERVIEW] ✅ 从SQLite轻量汇总读取")
                else:
                    logger.warning(f"[OVERVIEW] SQLite查询失败，尝试从stats.json读取")
                    raise Exception("SQLite查询失败")
            else:
                stats_path = MonitorConfig.LOG_DIR / MonitorConfig.STATS_FILE
                if stats_path.exists():
                    with open(stats_path, 'r', encoding='utf-8') as f:
                        stats_data = json.load(f)
                    stats_from_source = {
                        "total_requests": stats_data.get('total_requests_all_time', 0),
                        "success_requests": stats_data.get('total_success_all_time', 0),
                        "failed_requests": stats_data.get('total_failed_all_time', 0)
                    }
                else:
                    logger.warning(f"[OVERVIEW] stats.json不存在，使用内存统计")
        except Exception as e:
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
                else:
                    logger.error(f"[OVERVIEW] 所有数据源都失败，使用内存统计: {e}")
            except Exception as fallback_error:
                logger.error(f"[OVERVIEW] stats.json读取也失败，使用内存统计: {fallback_error}")
        
        await _set_admin_cached_response("overview", "stats", stats_from_source)
    
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


async def get_all_tokenizers_status_api(get_all_tokenizers_status_func):
    """获取所有分词器的详细状态"""
    try:
        status = get_all_tokenizers_status_func()
        return status
    except Exception as e:
        logger.error(f"获取分词器状态失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def calculate_tokens_api(
    request: Request,
    calculate_tokens_for_text_func
):
    """计算文本的token数量"""
    try:
        data = await request.json()
        text = data.get("text", "")
        tokenizers = data.get("tokenizers", None)  # 可选：指定使用哪些分词器
        
        if not text:
            raise HTTPException(status_code=400, detail="缺少text参数")
        
        result = calculate_tokens_for_text_func(text, tokenizers)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"计算token失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def compare_tokenizers_api(
    request: Request,
    compare_tokenizers_func
):
    """对比两种分词器的结果"""
    try:
        data = await request.json()
        text = data.get("text", "")
        tokenizer1 = data.get("tokenizer1", "tiktoken_cl100k")
        tokenizer2 = data.get("tokenizer2", "estimate")
        
        if not text:
            raise HTTPException(status_code=400, detail="缺少text参数")
        
        result = compare_tokenizers_func(text, tokenizer1, tokenizer2)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"对比分词器失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def install_tokenizer_api(
    request: Request,
    install_tokenizer_package_func,
    get_all_tokenizers_status_func
):
    """安装分词器包"""
    try:
        data = await request.json()
        package_name = data.get("package", "")
        
        if not package_name:
            raise HTTPException(status_code=400, detail="缺少package参数")
        
        # 执行安装
        result = install_tokenizer_package_func(package_name)
        
        if result['success']:
            # 安装成功后获取最新状态
            new_status = get_all_tokenizers_status_func()
            result['tokenizers_status'] = new_status
            logger.info(f"✅ 分词器 {package_name} 安装成功")
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"安装分词器失败: {e}", exc_info=True)
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
    cache_key = _build_admin_cache_key(start_time, end_time, stats_db.enabled)
    cached_response = await _get_admin_cached_response("request_stats", cache_key)
    if cached_response is not None:
        logger.debug("[REQUEST_STATS] 命中短时缓存")
        return cached_response

    try:
        # 优先使用SQLite数据库
        if stats_db.enabled:
            db_stats = await stats_db.get_request_stats_async(start_time, end_time)
            if db_stats:
                logger.info(f"[REQUEST_STATS] ✅ 从SQLite读取统计数据")
                await _set_admin_cached_response("request_stats", cache_key, db_stats)
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
        # 🔧 性能修复：read_recent_logs 内部有同步文件 I/O，用 asyncio.to_thread 避免阻塞事件循环
        recent_logs = await asyncio.to_thread(monitoring_service.log_manager.read_recent_logs, "requests", 10000)
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
        
        result = {
            "daily_stats": daily_stats_list,
            "total_requests": total_requests,
            "success_requests": success_requests,
            "failed_requests": failed_requests
        }
        await _set_admin_cached_response("request_stats", cache_key, result)
        return result
    except Exception as e:
        logger.error(f"获取请求统计失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def get_token_stats(
    start_date: str,
    end_date: str,
    start_time: str,
    end_time: str,
    rpm_period: str,
    stats_db,
    monitoring_service,
    MODEL_ENDPOINT_MAP: dict,
    estimate_message_tokens_func,
    estimate_tokens_func
):
    """获取token用量统计，支持日期范围过滤
    
    Args:
        rpm_period: RPM/TPM 计算的时间周期，'day'=24小时, 'hour'=1小时。仅影响 RPM/TPM 计算，不影响其他统计。
    """
    # 兼容性：支持start_date/end_date（前端）和start_time/end_time（SQLite）
    filter_start = start_time or start_date
    filter_end = end_time or end_date

    cache_key = _build_admin_cache_key(filter_start, filter_end, rpm_period, stats_db.enabled)
    cached_response = await _get_admin_cached_response("token_stats", cache_key)
    if cached_response is not None:
        logger.debug("[TOKEN_STATS] 命中短时缓存")
        return cached_response

    try:
        # 优先使用SQLite数据库
        if stats_db.enabled:
            db_stats = await stats_db.get_token_stats_async(filter_start, filter_end, MODEL_ENDPOINT_MAP, rpm_period)
            if db_stats:
                logger.info(f"[TOKEN_STATS] ✅ 从SQLite读取统计数据")
                await _set_admin_cached_response("token_stats", cache_key, db_stats)
                return db_stats
            else:
                logger.warning(f"[TOKEN_STATS] SQLite查询失败，回退到JSON日志")
        
    except Exception as e:
        # 简化回退：只返回内存中的模型统计概览（无详细token聚合）
        model_stats_list = await monitoring_service.get_model_stats_async()
        stats_list = []
        for s in model_stats_list:
            stats_list.append({
                'model': s['model'],
                'display_name': s['model'],
                'request_count': s['total_requests'],
                'input_tokens': 0,
                'output_tokens': 0,
                'cached_tokens': 0,
                'cached_cost': 0,
                'total_tokens': 0,
                'input_cost': 0,
                'output_cost': 0,
                'total_cost': 0,
                'currency': 'USD',
                'rpm': 0,
                'tpm': 0
            })
        stats_list.sort(key=lambda x: x['request_count'], reverse=True)

        result = {
            "model_stats": stats_list,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cached_tokens": 0,
            "total_tokens": 0,
            "models_count": len(stats_list),
            "daily_stats": []
        }
        await _set_admin_cached_response("token_stats", cache_key, result)
        return result
        logger.error(f"获取token统计失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))



async def export_report(
    stats_db,
    monitoring_service,
    MODEL_ENDPOINT_MAP: dict,
    start_date: str = None,
    end_date: str = None
):
    """导出Token使用/成本报告为CSV文件"""
    import csv
    import io

    try:
        # 优先使用SQLite
        if stats_db.enabled:
            db_stats = await stats_db.get_token_stats_async(start_date, end_date, MODEL_ENDPOINT_MAP, 'day')
            if db_stats and db_stats.get('model_stats'):
                model_stats = db_stats['model_stats']
                output = io.StringIO()
                writer = csv.writer(output)
                # 写入表头
                writer.writerow([
                    '模型', '请求数', '输入Tokens', '输出Tokens', '缓存命中Tokens',
                    '总Tokens', '输入成本(USD)', '缓存成本(USD)', '输出成本(USD)', '总成本(USD)',
                    '货币', '平均Token/请求'
                ])
                for stat in model_stats:
                    cached_cost = 0.0
                    # 缓存成本 = cached_tokens * cached_input_price / unit
                    # 这里从后端数据推算（input_cost 已经按新公式计算过）
                    writer.writerow([
                        stat.get('display_name', stat.get('model', '')),
                        stat.get('request_count', 0),
                        stat.get('input_tokens', 0),
                        stat.get('output_tokens', 0),
                        stat.get('cached_tokens', 0),
                        stat.get('total_tokens', 0),
                        round(stat.get('input_cost', 0), 6),
                        round(stat.get('cached_cost', 0), 6),
                        round(stat.get('output_cost', 0), 6),
                        round(stat.get('total_cost', 0), 6),
                        stat.get('currency', 'USD'),
                        stat.get('request_count', 0) > 0 and round(stat.get('total_tokens', 0) / stat.get('request_count', 0)) or 0
                    ])

                csv_content = output.getvalue()
                output.close()
                return Response(
                    content=csv_content,
                    media_type="text/csv; charset=utf-8-sig",
                    headers={"Content-Disposition": "attachment; filename=token_report.csv"}
                )

        # 回退：从内存中的模型统计导出
        # 🔧 性能修复：用 asyncio.to_thread 包装，避免 threading.Lock 阻塞事件循环
        model_stats = await asyncio.to_thread(monitoring_service.get_model_stats)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            '模型', '总请求数', '成功请求数', '失败请求数', '平均耗时(ms)'
        ])
        for stat in model_stats:
            writer.writerow([
                stat.get('model', ''),
                stat.get('total_requests', 0),
                stat.get('success_requests', 0),
                stat.get('failed_requests', 0),
                round(stat.get('avg_duration', 0) * 1000, 2)
            ])
        csv_content = output.getvalue()
        output.close()
        return Response(
            content=csv_content,
            media_type="text/csv; charset=utf-8-sig",
            headers={"Content-Disposition": "attachment; filename=token_report.csv"}
        )
    except Exception as e:
        logger.error(f"导出报告失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def add_custom_tokenizer_api(
    request: Request,
    add_custom_tokenizer_func
):
    """添加自定义分词器"""
    try:
        data = await request.json()
        name = data.get("name", "")
        source_type = data.get("source_type", "huggingface")
        source = data.get("source", "")
        display_name = data.get("display_name", None)
        description = data.get("description", None)
        supported_models = data.get("supported_models", [])
        
        if not name or not source:
            raise HTTPException(status_code=400, detail="缺少必要参数: name, source")
        
        result = add_custom_tokenizer_func(
            name=name,
            source_type=source_type,
            source=source,
            display_name=display_name,
            description=description,
            supported_models=supported_models
        )
        
        if result['success']:
            logger.info(f"✅ 自定义分词器 {name} 添加成功")
        else:
            logger.warning(f"❌ 自定义分词器 {name} 添加失败: {result.get('error')}")
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"添加自定义分词器失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def delete_custom_tokenizer_api(
    name: str,
    delete_custom_tokenizer_func
):
    """删除自定义分词器"""
    try:
        if not name:
            raise HTTPException(status_code=400, detail="缺少分词器名称")
        
        result = delete_custom_tokenizer_func(name)
        
        if result['success']:
            logger.info(f"✅ 自定义分词器 {name} 删除成功")
        else:
            logger.warning(f"❌ 自定义分词器 {name} 删除失败: {result.get('error')}")
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除自定义分词器失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def list_custom_tokenizers_api(list_custom_tokenizers_func):
    """列出所有自定义分词器"""
    try:
        result = list_custom_tokenizers_func()
        return result
    except Exception as e:
        logger.error(f"列出自定义分词器失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def test_model_keys(
    request: Request,
    direct_api_service,
):
    """测试单个模型配置中的所有 API Key 是否有效（并行发送请求）
    
    前端从编辑对话框中读取当前配置，POST 到此接口。
    对每个 key 分别发送一次测试请求。
    """
    data = await request.json()
    
    api_keys = data.get("api_keys", [])
    api_base_url = data.get("api_base_url", "").strip()
    model_id = data.get("model_id", "unknown")
    api_type = data.get("api_type", "direct_api")
    endpoint_path = data.get("endpoint_path", "/chat/completions")
    if endpoint_path and not endpoint_path.startswith("/"):
        endpoint_path = "/" + endpoint_path
    
    is_gemini = api_type == "gemini_native"
    
    if not api_keys:
        return {"status": "info", "message": "没有配置任何 API Key", "results": []}
    
    async def _test_one_key(key: str, index: int):
        """测试单个 key"""
        result = {
            "index": index,
            "key_preview": key[:6] + "..." + key[-4:] if len(key) > 10 else "***",
            "status": "unknown",
            "error": None,
            "response_time_ms": None,
        }
        
        try:
            t0 = time.time()
            
            if is_gemini:
                gen = direct_api_service.call_gemini_native_api(
                    api_key=key,
                    model=model_id,
                    messages=[{"role": "user", "content": "Hi"}],
                    stream=False,
                    base_url=api_base_url if api_base_url else None,
                    thinking_config={"includeThoughts": False},
                )
                resp = None
                async for chunk in gen:
                    resp = chunk
                    break
                
                ms = round((time.time() - t0) * 1000, 1)
                result["response_time_ms"] = ms
                
                if resp and "error" not in resp:
                    result["status"] = "ok"
                else:
                    err = resp.get("error", {}) if resp else "无响应"
                    if isinstance(err, dict):
                        err = str(err.get("message", str(err)))
                    result["status"] = "error"
                    result["error"] = str(err)[:200]
            else:
                url = f"{api_base_url.rstrip('/')}{endpoint_path}"
                body = {"model": model_id, "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 5}
                headers = {"Content-Type": "application/json"}
                if key:
                    headers["Authorization"] = f"Bearer {key}"
                
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as sess:
                    async with sess.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=30, connect=10)) as resp:
                        ms = round((time.time() - t0) * 1000, 1)
                        result["response_time_ms"] = ms
                        if resp.status == 200:
                            result["status"] = "ok"
                        else:
                            try:
                                ed = await resp.json()
                                em = str(ed.get("error", {}).get("message", ed.get("detail", str(resp.status))))
                            except:
                                em = f"HTTP {resp.status}"
                            result["status"] = "error"
                            result["error"] = em[:200]
        except asyncio.TimeoutError:
            result["status"] = "timeout"
            result["error"] = "请求超时（30s）"
        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)[:200]
        
        return result
    
    tasks = [_test_one_key(k, i) for i, k in enumerate(api_keys)]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    
    results = []
    for i, r in enumerate(raw_results):
        if isinstance(r, Exception):
            results.append({"index": i, "key_preview": "???", "status": "error", "error": str(r)[:200], "response_time_ms": None})
        else:
            results.append(r)
    
    ok = sum(1 for r in results if r["status"] == "ok")
    err = sum(1 for r in results if r["status"] == "error")
    to = sum(1 for r in results if r["status"] == "timeout")
    
    return {
        "status": "done",
        "total": len(results),
        "ok": ok,
        "error": err,
        "timeout": to,
        "results": results,
        "message": f"测试完成: {ok}/{len(results)} 个 Key 有效"
    }