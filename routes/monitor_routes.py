"""
监控面板路由
处理监控相关的API端点和WebSocket
"""
import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Optional
import psutil
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from core.app_state import get_app_state
from core.config_loader import CONFIG
from modules.monitoring import monitoring_service, MonitorConfig

logger = logging.getLogger(__name__)
router = APIRouter(tags=["monitor"])

_app_state = get_app_state()


async def monitor_dashboard():
    """返回监控面板HTML页面"""
    try:
        with open('monitor.html', 'r', encoding='utf-8') as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>监控面板文件未找到</h1><p>请确保 monitor.html 文件在正确的位置。</p>",
            status_code=404
        )


async def monitor_websocket(
    websocket: WebSocket,
    monitoring_service,
    browser_ws,
    CONFIG: dict
):
    """监控面板的WebSocket连接"""
    await websocket.accept()
    monitoring_service.add_monitor_client(websocket)

    try:
        # 发送初始数据
        # 🔧 性能修复：使用 async 版本，避免 threading.Lock 阻塞事件循环
        summary = await monitoring_service.get_summary_async()
        await websocket.send_json({
            "type": "initial_data",
            "stats": summary['stats'],
            "model_stats": summary['model_stats'],
            "active_requests": summary['active_requests_list'],
            "browser_connected": browser_ws is not None,
            "mode": {
                "mode": CONFIG.get("id_updater_last_mode", "direct_chat"),
                "target": CONFIG.get("id_updater_battle_target", "A")
            }
        })

        while True:
            # 保持连接
            await websocket.receive_text()

    except WebSocketDisconnect:
        logger.debug("[MONITOR_WS] 监控客户端正常断开")
    except Exception as e:
        logger.warning(f"[MONITOR_WS] 监控连接异常终止: {type(e).__name__}: {e}")
    finally:
        # 🔧 修复：旧版只在 WebSocketDisconnect 分支移除客户端。任何其他异常
        # （初始数据 send_json 失败、连接被中间件关闭、任务被取消等）都会让
        # 这个已死的 WebSocket 永久留在 monitor_clients 里，此后每条监控广播
        # 都要为它跑一次 0.2s 超时的发送，逐步拖慢整个广播链路。
        monitoring_service.remove_monitor_client(websocket)


async def get_monitor_stats(
    monitoring_service,
    browser_ws,
    CONFIG: dict
):
    """获取监控统计数据"""
    # 🔧 A1 修复：使用 async 版本，避免 threading.Lock 阻塞事件循环
    summary = await monitoring_service.get_summary_async()
    
    # 修复：确保字段名与前端一致（successful_requests而不是success_requests）
    stats = summary['stats'].copy() if summary.get('stats') else {}
    if 'success_requests' in stats and 'successful_requests' not in stats:
        stats['successful_requests'] = stats['success_requests']
    
    return {
        "stats": stats,
        "model_stats": summary['model_stats'],
        "browser_connected": browser_ws is not None,
        "mode": {
            "mode": CONFIG.get("id_updater_last_mode", "direct_chat"),
            "target": CONFIG.get("id_updater_battle_target", "A")
        }
    }


async def get_active_requests(monitoring_service):
    """获取活跃请求列表"""
    import asyncio
    return await asyncio.to_thread(monitoring_service.get_active_requests)


async def get_request_logs(limit: int, monitoring_service):
    """获取请求日志"""
    import asyncio
    return await asyncio.to_thread(monitoring_service.log_manager.read_recent_logs, "requests", limit)


async def query_request_logs(monitoring_service, limit: int = 50, offset: int = 0,
                             model: Optional[str] = None, status: Optional[str] = None,
                             search: Optional[str] = None):
    """分页 + 过滤查询请求日志（监控面板增强版）"""
    import asyncio
    result = await asyncio.to_thread(
        monitoring_service.log_manager.query_request_logs,
        limit, offset, model or None, status or None, search or None)
    # 附带汇率：前端展示 CNY 计价模型的费用时可换算出 USD 参考值
    if isinstance(result, dict):
        from core.db_stats import get_exchange_rates
        usd_to_cny, cny_to_usd = get_exchange_rates()
        result['exchange_rate'] = {'USD_TO_CNY': usd_to_cny, 'CNY_TO_USD': cny_to_usd}
    return result


async def get_error_logs(limit: int, monitoring_service):
    """获取错误日志"""  
    import asyncio
    return await asyncio.to_thread(monitoring_service.log_manager.read_recent_logs, "errors", limit)


async def get_recent_data(monitoring_service):
    """获取最近的请求和错误"""
    import asyncio
    return {
        "recent_requests": await asyncio.to_thread(monitoring_service.get_recent_requests, 50),
        "recent_errors": await asyncio.to_thread(monitoring_service.get_recent_errors, 30)
    }


async def get_performance_metrics(
    MAX_CONCURRENT_DOWNLOADS: int,
    DOWNLOAD_SEMAPHORE,
    aiohttp_session,
    IMAGE_BASE64_CACHE: dict,
    IMAGE_CACHE_MAX_SIZE: int,
    downloaded_urls_set: set,
    response_channels: dict,
    DISABLED_ENDPOINTS: dict,
    CONFIG: dict
):
    """获取性能指标"""
    metrics = {
        "download_semaphore": {
            "max_concurrent": MAX_CONCURRENT_DOWNLOADS,
            "current_active": MAX_CONCURRENT_DOWNLOADS - DOWNLOAD_SEMAPHORE._value if DOWNLOAD_SEMAPHORE else 0,
            "available": DOWNLOAD_SEMAPHORE._value if DOWNLOAD_SEMAPHORE else MAX_CONCURRENT_DOWNLOADS
        },
        "aiohttp_session": {
            "connector_limit": aiohttp_session.connector.limit if aiohttp_session else 0,
            "connector_limit_per_host": aiohttp_session.connector.limit_per_host if aiohttp_session else 0,
            "connector_active": len(aiohttp_session.connector._conns) if aiohttp_session and hasattr(aiohttp_session.connector, '_conns') else 0
        },
        "cache_stats": {
            "image_cache_size": len(IMAGE_BASE64_CACHE),
            "image_cache_max": IMAGE_CACHE_MAX_SIZE,
            "downloaded_urls": len(downloaded_urls_set),
            "response_channels": len(response_channels),
            "disabled_endpoints": len(DISABLED_ENDPOINTS)
        },
        "config": {
            "max_concurrent_downloads": CONFIG.get("max_concurrent_downloads", 50),
            "download_timeout": CONFIG.get("download_timeout", {}),
            "connection_pool": CONFIG.get("connection_pool", {}),
            "memory_management": CONFIG.get("memory_management", {})
        }
    }
    return metrics


async def get_tab_connections(
    browser_connections: dict,
    browser_connections_lock,
    tab_connection_times: dict,
    tab_request_counts: dict
):
    """获取标签页连接状态"""
    async with browser_connections_lock:
        tabs_info = []
        current_time = time.time()
        
        for tab_id, ws in browser_connections.items():
            # 计算该标签页的连接时长
            connection_start = tab_connection_times.get(tab_id, current_time)
            connected_duration = current_time - connection_start
            
            # 获取该标签页的请求负载
            load = tab_request_counts.get(tab_id, 0)
            
            tabs_info.append({
                "tab_id": tab_id,
                "connected": ws.client_state.name == 'CONNECTED' if ws else False,
                "active_requests": load,
                "max_concurrent": 6,  # 浏览器HTTP/1.1限制
                "load_percentage": (load / 6) * 100 if load < 6 else 100,
                "status": "busy" if load >= 6 else "available",
                "connected_duration": connected_duration,
                "connected_at": connection_start
            })
        
        return {
            "total_tabs": len(browser_connections),
            "total_capacity": len(browser_connections) * 6,
            "total_active_requests": sum(tab_request_counts.values()),
            "tabs": tabs_info
        }


def _get_memory_info_sync():
    """同步版内存信息收集（在线程池中执行）"""
    process = psutil.Process(os.getpid())
    memory_info = process.memory_info()
    system_memory = psutil.virtual_memory()
    
    result = {
        "process": {
            "rss_mb": memory_info.rss / (1024 * 1024),
            "vms_mb": memory_info.vms / (1024 * 1024),
            "percent": process.memory_percent(),
            "pid": os.getpid()
        },
        "system": {
            "total_gb": system_memory.total / (1024 * 1024 * 1024),
            "available_gb": system_memory.available / (1024 * 1024 * 1024),
            "percent": system_memory.percent
        },
        "tokenizers": None
    }
    
    try:
        from modules.token_counter import get_tokenizer_memory_info
        result["tokenizers"] = get_tokenizer_memory_info()
    except ImportError:
        result["tokenizers"] = {"error": "token_counter模块不可用"}
    except Exception as e:
        result["tokenizers"] = {"error": str(e)}
    
    return result


async def get_memory_info():
    """获取服务器内存使用信息
    
    🔧 A5 修复：psutil 系统调用在 Windows 上可能耗时 10-50ms，
    用 asyncio.to_thread 避免阻塞事件循环。
    """
    import asyncio
    try:
        return await asyncio.to_thread(_get_memory_info_sync)
    except Exception as e:
        logger.error(f"获取内存信息失败: {e}", exc_info=True)
        return {"error": str(e)}


async def clear_tokenizer_cache_api(force: bool = False):
    """清理tokenizer缓存"""
    try:
        from modules.token_counter import clear_tokenizer_cache, get_tokenizer_memory_info
        
        # 先获取清理前的状态
        before = get_tokenizer_memory_info()
        
        # 执行清理
        result = clear_tokenizer_cache(force=force)
        
        # 获取清理后的状态
        after = get_tokenizer_memory_info()
        
        result["memory_before_mb"] = before["estimated_memory_mb"]
        result["memory_after_mb"] = after["estimated_memory_mb"]
        result["memory_freed_mb"] = before["estimated_memory_mb"] - after["estimated_memory_mb"]
        
        return result
        
    except ImportError:
        return {"error": "token_counter模块不可用"}
    except Exception as e:
        logger.error(f"清理tokenizer缓存失败: {e}", exc_info=True)
        return {"error": str(e)}


# ============================================================================
# 端点注册（依赖从 AppState / 模块单例自取）
# ============================================================================

@router.get("/monitor", response_class=HTMLResponse)
async def monitor_dashboard_endpoint():
    return await monitor_dashboard()


@router.websocket("/ws/monitor")
async def monitor_websocket_endpoint(websocket: WebSocket):
    await monitor_websocket(
        websocket, monitoring_service, _app_state.connection.browser_ws_ref['ws'], CONFIG
    )


@router.get("/api/monitor/stats")
async def get_monitor_stats_endpoint():
    return await get_monitor_stats(
        monitoring_service, _app_state.connection.browser_ws_ref['ws'], CONFIG
    )


@router.get("/api/monitor/active")
async def get_active_requests_endpoint():
    return await get_active_requests(monitoring_service)


@router.get("/api/monitor/logs/requests")
async def get_request_logs_endpoint(limit: int = 50):
    return await get_request_logs(limit, monitoring_service)


@router.get("/api/monitor/logs/requests/query")
async def query_request_logs_endpoint(limit: int = 50, offset: int = 0,
                                      model: Optional[str] = None,
                                      status: Optional[str] = None,
                                      search: Optional[str] = None):
    """分页 + 过滤查询请求日志（返回 {total, items, models}）"""
    return await query_request_logs(
        monitoring_service, limit, offset, model, status, search)


@router.get("/api/monitor/logs/errors")
async def get_error_logs_endpoint(limit: int = 30):
    return await get_error_logs(limit, monitoring_service)


@router.get("/api/monitor/recent")
async def get_recent_data_endpoint():
    return await get_recent_data(monitoring_service)


@router.get("/api/monitor/performance")
async def get_performance_metrics_endpoint():
    server_state = _app_state.server
    image_state = _app_state.image
    return await get_performance_metrics(
        MAX_CONCURRENT_DOWNLOADS=server_state.MAX_CONCURRENT_DOWNLOADS,
        DOWNLOAD_SEMAPHORE=server_state.DOWNLOAD_SEMAPHORE,
        aiohttp_session=server_state.aiohttp_session,
        IMAGE_BASE64_CACHE=image_state.IMAGE_BASE64_CACHE,
        IMAGE_CACHE_MAX_SIZE=image_state.IMAGE_CACHE_MAX_SIZE,
        downloaded_urls_set=image_state.downloaded_urls_set,
        response_channels=_app_state.request.response_channels,
        DISABLED_ENDPOINTS=image_state.DISABLED_ENDPOINTS,
        CONFIG=CONFIG,
    )


@router.get("/api/monitor/tabs")
async def get_tab_connections_endpoint():
    conn = _app_state.connection
    return await get_tab_connections(
        conn.browser_connections, conn.browser_connections_lock, conn.tab_connection_times,
        conn.tab_request_counts
    )


@router.get("/api/monitor/memory")
async def get_memory_info_endpoint():
    """获取服务器内存使用信息"""
    return await get_memory_info()


@router.post("/api/monitor/clear_tokenizer_cache")
async def clear_tokenizer_cache_endpoint(force: bool = False):
    """清理tokenizer缓存"""
    return await clear_tokenizer_cache_api(force)


@router.get("/api/request/{request_id}")
async def get_request_details_endpoint(request_id: str):
    """获取特定请求的详细信息"""
    # 🔧 性能修复：get_request_details 内部有同步文件 I/O（遍历日志目录、读取 SQLite/JSONL）
    # 必须用 asyncio.to_thread 包装，否则会阻塞事件循环导致流式响应卡顿
    details = await asyncio.to_thread(monitoring_service.get_request_details, request_id)
    if details:
        return details
    else:
        raise HTTPException(status_code=404, detail="请求详情未找到")


@router.get("/api/logs/download")
async def download_logs_endpoint(log_type: str = "requests"):
    """下载日志文件"""
    if log_type == "requests":
        log_path = MonitorConfig.LOG_DIR / MonitorConfig.REQUEST_LOG_FILE
    elif log_type == "errors":
        log_path = MonitorConfig.LOG_DIR / MonitorConfig.ERROR_LOG_FILE
    else:
        raise HTTPException(status_code=400, detail="无效的日志类型")

    if not log_path.exists():
        raise HTTPException(status_code=404, detail="日志文件不存在")

    return FileResponse(
        path=str(log_path),
        filename=f"{log_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl",
        media_type="application/json"
    )