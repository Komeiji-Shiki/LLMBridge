"""
内部通信路由
处理ID捕获、请求详情等内部端点。
其中 ID 捕获主要服务于已弃用但保留兼容的 LMArena 模式。
"""
import json
import logging
import time
from threading import Lock
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)
router = APIRouter(tags=["internal"])


async def start_id_capture(
    request: Request,
    browser_ws,
    ADMIN_CAPTURED_IDS: dict,
    ADMIN_CAPTURED_IDS_LOCK: Lock
):
    """
    接收来自 id_updater.py 或前端的通知，并通过 WebSocket 指令
    激活油猴脚本的 ID 捕获模式（仅用于已弃用但保留兼容的 LMArena 模式）。
    """
    if not browser_ws:
        logger.warning("ID CAPTURE: 收到激活请求，但没有浏览器连接。")
        raise HTTPException(status_code=503, detail="Browser client not connected.")
    
    try:
        # 尝试从请求体获取参数
        try:
            data = await request.json()
            mode = data.get("mode", "direct_chat")
            battle_target = data.get("battle_target", "A")
        except Exception:
            mode = "direct_chat"
            battle_target = "A"
        
        # 清空之前的捕获数据，准备新的捕获
        with ADMIN_CAPTURED_IDS_LOCK:
            ADMIN_CAPTURED_IDS['session_id'] = None
            ADMIN_CAPTURED_IDS['message_id'] = None
            ADMIN_CAPTURED_IDS['timestamp'] = None
            ADMIN_CAPTURED_IDS['mode'] = mode
            ADMIN_CAPTURED_IDS['battle_target'] = battle_target
        
        logger.info(f"ID CAPTURE: 收到激活请求 (模式: {mode}, 目标: {battle_target})，正在通过 WebSocket 发送指令...")
        
        # 发送包含模式信息的指令
        command = {
            "command": "activate_id_capture",
            "mode": mode,
            "battle_target": battle_target
        }
        
        await browser_ws.send_text(json.dumps(command, ensure_ascii=False))
        logger.info(f"ID CAPTURE: 激活指令已成功发送 (模式: {mode}, 目标: {battle_target})")
        
        return JSONResponse({
            "status": "success",
            "message": f"ID捕获已激活 (模式: {mode}, 目标: {battle_target})",
            "mode": mode,
            "battle_target": battle_target
        })
    except Exception as e:
        logger.error(f"ID CAPTURE: 发送激活指令时出错: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to send command via WebSocket.")


async def receive_captured_ids(
    request: Request,
    ADMIN_CAPTURED_IDS: dict,
    ADMIN_CAPTURED_IDS_LOCK: Lock
):
    """接收油猴脚本捕获到的ID（现在只接收 sessionId）"""
    try:
        data = await request.json()
        session_id = data.get('sessionId')
        
        if not session_id:
            raise HTTPException(status_code=400, detail="Missing sessionId")
        
        # 存储捕获的ID
        with ADMIN_CAPTURED_IDS_LOCK:
            ADMIN_CAPTURED_IDS['session_id'] = session_id
            ADMIN_CAPTURED_IDS['timestamp'] = time.time()
        
        logger.info(f"🎉 Admin面板成功捕获ID:")
        logger.info(f"  - Session ID: {session_id}")
        
        return JSONResponse({
            "status": "success",
            "message": "Session ID captured successfully"
        })
    
    except Exception as e:
        logger.error(f"接收捕获ID时出错: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def get_capture_status(
    ADMIN_CAPTURED_IDS: dict,
    ADMIN_CAPTURED_IDS_LOCK: Lock
):
    """查询ID捕获状态"""
    with ADMIN_CAPTURED_IDS_LOCK:
        has_captured = ADMIN_CAPTURED_IDS['session_id'] is not None
        
        return JSONResponse({
            "captured": has_captured,
            "session_id": ADMIN_CAPTURED_IDS['session_id'],
            "mode": ADMIN_CAPTURED_IDS['mode'],
            "battle_target": ADMIN_CAPTURED_IDS['battle_target'],
            "timestamp": ADMIN_CAPTURED_IDS['timestamp']
        })


async def save_captured_model(
    request: Request,
    ADMIN_CAPTURED_IDS: dict,
    ADMIN_CAPTURED_IDS_LOCK: Lock,
    MODEL_ENDPOINT_MAP_PATH: str,
    load_model_endpoint_map_func
):
    """将捕获的ID保存为模型配置"""
    try:
        data = await request.json()
        model_name = data.get("model_name")
        model_type = data.get("model_type", "text")
        
        if not model_name:
            raise HTTPException(status_code=400, detail="Missing model_name")
        
        # 获取捕获的ID
        with ADMIN_CAPTURED_IDS_LOCK:
            session_id = ADMIN_CAPTURED_IDS['session_id']
            mode = ADMIN_CAPTURED_IDS['mode']
            battle_target = ADMIN_CAPTURED_IDS['battle_target']
        
        if not session_id:
            raise HTTPException(status_code=400, detail="No captured Session ID available")
        
        # 读取现有配置
        with open(MODEL_ENDPOINT_MAP_PATH, 'r', encoding='utf-8') as f:
            endpoint_map = json.load(f)
        
        # 构建配置条目
        entry = {
            "session_id": session_id,
            "mode": mode
        }
        
        if model_type and model_type != "text":
            entry["type"] = model_type
        
        if mode == "battle" and battle_target:
            entry["battle_target"] = battle_target
        
        # 保存配置
        endpoint_map[model_name] = entry
        
        with open(MODEL_ENDPOINT_MAP_PATH, 'w', encoding='utf-8') as f:
            json.dump(endpoint_map, f, indent=2, ensure_ascii=False)
        
        # 重新加载配置
        load_model_endpoint_map_func(force_reload=True)
        
        logger.info(f"✅ 模型 '{model_name}' 配置已保存")
        logger.info(f"  - session_id: {session_id}")
        logger.info(f"  - mode: {mode}")
        if model_type != "text":
            logger.info(f"  - type: {model_type}")
        if mode == "battle":
            logger.info(f"  - battle_target: {battle_target}")
        
        return JSONResponse({
            "status": "success",
            "message": f"模型 {model_name} 配置已保存",
            "model_name": model_name,
            "config": entry
        })
    
    except Exception as e:
        logger.error(f"保存模型配置时出错: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def get_request_details(
    request_id: str,
    monitoring_service
):
    """获取特定请求的详细信息"""
    details = monitoring_service.get_request_details(request_id)
    if details:
        return details
    else:
        raise HTTPException(status_code=404, detail="请求详情未找到")


async def download_logs(
    log_type: str,
    MonitorConfig
):
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


async def get_request_transfer_stats(
    request_metadata: dict
):
    """获取请求转移统计信息"""
    transfer_stats = {
        "total_requests": len(request_metadata),
        "transferred_requests": 0,
        "transfer_details": []
    }
    
    for request_id, metadata in request_metadata.items():
        transfer_count = metadata.get("transfer_count", 0)
        if transfer_count > 0:
            transfer_stats["transferred_requests"] += 1
            transfer_stats["transfer_details"].append({
                "request_id": request_id[:8],
                "original_tab_id": metadata.get("original_tab_id"),
                "current_tab_id": metadata.get("tab_id"),
                "transfer_count": transfer_count,
                "last_transfer_time": metadata.get("last_transfer_time"),
                "model": metadata.get("model_name"),
                "created_at": metadata.get("created_at")
            })
    
    return transfer_stats