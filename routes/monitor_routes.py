"""
监控面板路由
处理监控相关的API端点和WebSocket
"""
import logging
import time
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/monitor", tags=["monitor"])


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
        summary = monitoring_service.get_summary()
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
        monitoring_service.remove_monitor_client(websocket)


async def get_monitor_stats(
    monitoring_service,
    browser_ws,
    CONFIG: dict
):
    """获取监控统计数据"""
    summary = monitoring_service.get_summary()
    
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
    return monitoring_service.get_active_requests()


async def get_request_logs(limit: int, monitoring_service):
    """获取请求日志"""
    return monitoring_service.log_manager.read_recent_logs("requests", limit)


async def get_error_logs(limit: int, monitoring_service):
    """获取错误日志"""  
    return monitoring_service.log_manager.read_recent_logs("errors", limit)


async def get_recent_data(monitoring_service):
    """获取最近的请求和错误"""
    return {
        "recent_requests": monitoring_service.get_recent_requests(50),
        "recent_errors": monitoring_service.get_recent_errors(30)
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