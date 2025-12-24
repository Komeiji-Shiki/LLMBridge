"""
负载均衡模块
处理多标签页的负载均衡和请求分配
"""
import asyncio
import logging
from typing import Tuple
from fastapi import HTTPException, WebSocket
from datetime import datetime

logger = logging.getLogger(__name__)


async def select_best_tab_for_request(
    browser_connections: dict,
    browser_connections_lock: asyncio.Lock,
    tab_request_counts: dict
) -> Tuple[str, WebSocket]:
    """
    选择负载最低的标签页来处理新请求。
    返回 (tab_id, websocket)
    """
    logger.info(f"[LOCK_DEBUG] 尝试获取 browser_connections_lock...")
    
    # 兼容性修复：使用 asyncio.wait_for 替代 asyncio.timeout (Python 3.11+)
    async def _acquire_lock_and_select():
        async with browser_connections_lock:
            logger.info(f"[LOCK_DEBUG] ✅ 已获取 browser_connections_lock")
            if not browser_connections:
                raise HTTPException(status_code=503, detail="没有可用的浏览器连接")
            
            # 关键修复：清理已断开连接的标签页计数
            stale_tabs = [tab_id for tab_id in tab_request_counts.keys() if tab_id not in browser_connections]
            for tab_id in stale_tabs:
                del tab_request_counts[tab_id]
                logger.debug(f"[LOAD_BALANCE] 清理已断开标签页 '{tab_id}' 的计数")
            
            # 确保所有活跃标签页都有计数
            for tab_id in browser_connections.keys():
                if tab_id not in tab_request_counts:
                    tab_request_counts[tab_id] = 0
            
            # 关键修复：只从活跃连接中选择（而不是从tab_request_counts中选择）
            # 计算每个活跃标签页的当前负载
            active_tab_loads = {tab_id: tab_request_counts.get(tab_id, 0) for tab_id in browser_connections.keys()}
            
            # 选择负载最低的标签页
            best_tab_id = min(active_tab_loads, key=active_tab_loads.get)
            best_ws = browser_connections[best_tab_id]
            
            # 增加该标签页的请求计数
            tab_request_counts[best_tab_id] += 1
            
            logger.info(f"[LOAD_BALANCE] 选择标签页 '{best_tab_id}' (当前负载: {tab_request_counts[best_tab_id]}/6)")
            logger.info(f"[LOAD_BALANCE] 所有标签页负载: {tab_request_counts}")
            logger.info(f"[LOCK_DEBUG] 即将释放 browser_connections_lock")
            
            return best_tab_id, best_ws
    
    try:
        # 添加超时保护，防止死锁（兼容 Python 3.7+）
        return await asyncio.wait_for(_acquire_lock_and_select(), timeout=5.0)
    except asyncio.TimeoutError:
        logger.error(f"[LOCK_DEBUG] ❌ 获取 browser_connections_lock 超时（5秒）！可能存在死锁")
        logger.error(f"[LOCK_DEBUG] 当前浏览器连接数: {len(browser_connections)}")
        logger.error(f"[LOCK_DEBUG] 当前标签页计数: {tab_request_counts}")
        raise HTTPException(status_code=503, detail="服务器负载均衡锁超时，可能存在死锁")


async def release_tab_request(tab_id: str, tab_request_counts: dict, tab_request_counts_lock: asyncio.Lock):
    """释放标签页的请求计数"""
    async with tab_request_counts_lock:
        if tab_id in tab_request_counts and tab_request_counts[tab_id] > 0:
            tab_request_counts[tab_id] -= 1
            logger.debug(f"[LOAD_BALANCE] 释放标签页 '{tab_id}' 的请求 (剩余负载: {tab_request_counts[tab_id]}/6)")


async def reassign_pending_requests(
    disconnected_tab_id: str,
    browser_connections: dict,
    browser_connections_lock: asyncio.Lock,
    response_channels: dict,
    request_metadata: dict,
    tab_request_counts: dict,
    CONFIG: dict,
    convert_openai_to_lmarena_payload
):
    """
    核心修复：当标签页断开时，将其待处理请求重新分配给其他活跃标签页
    
    Args:
        disconnected_tab_id: 断开连接的标签页ID
        browser_connections: 浏览器连接字典
        browser_connections_lock: 浏览器连接锁
        response_channels: 响应通道字典
        request_metadata: 请求元数据字典
        tab_request_counts: 标签页请求计数字典
        CONFIG: 配置字典
        convert_openai_to_lmarena_payload: 转换函数
    """
    logger.info(f"[REQUEST_REASSIGN] 🔄 开始检查标签页 '{disconnected_tab_id}' 的待处理请求...")
    
    async with browser_connections_lock:
        # 检查是否还有其他活跃标签页
        active_tabs = list(browser_connections.keys())
        
        if not active_tabs:
            logger.warning(f"[REQUEST_REASSIGN] ⚠️ 没有其他活跃标签页，无法重新分配请求")
            return
        
        logger.info(f"[REQUEST_REASSIGN] 发现 {len(active_tabs)} 个活跃标签页可用于接收请求")
        
        # 查找所有属于断开标签页的待处理请求
        requests_to_reassign = []
        for request_id, metadata in list(request_metadata.items()):
            if metadata.get("tab_id") == disconnected_tab_id:
                # 检查是否允许转移
                transfer_count = metadata.get("transfer_count", 0)
                max_transfers = CONFIG.get("max_request_transfers", 3)
                
                if transfer_count >= max_transfers:
                    logger.warning(f"[REQUEST_REASSIGN] ⚠️ 请求 {request_id[:8]} 已达到最大转移次数 ({max_transfers})，标记为失败")
                    # 向响应通道发送错误
                    if request_id in response_channels:
                        await response_channels[request_id].put({
                            "error": f"Request failed after {max_transfers} transfer attempts"
                        })
                        await response_channels[request_id].put("[DONE]")
                    continue
                
                requests_to_reassign.append((request_id, metadata))
        
        if not requests_to_reassign:
            logger.info(f"[REQUEST_REASSIGN] ✅ 标签页 '{disconnected_tab_id}' 没有待处理请求")
            return
        
        logger.info(f"[REQUEST_REASSIGN] 📦 发现 {len(requests_to_reassign)} 个需要重新分配的请求")
        
        # 重新分配每个请求
        reassign_success_count = 0
        reassign_fail_count = 0
        
        for request_id, metadata in requests_to_reassign:
            try:
                # 选择最佳标签页（负载最低）
                best_tab_id = None
                min_load = float('inf')
                
                for tab_id in active_tabs:
                    current_load = tab_request_counts.get(tab_id, 0)
                    if current_load < min_load:
                        min_load = current_load
                        best_tab_id = tab_id
                
                if not best_tab_id:
                    logger.error(f"[REQUEST_REASSIGN] ❌ 无法为请求 {request_id[:8]} 找到目标标签页")
                    reassign_fail_count += 1
                    continue
                
                target_ws = browser_connections[best_tab_id]
                
                # 更新元数据
                original_tab_id = metadata.get("original_tab_id", disconnected_tab_id)
                transfer_count = metadata.get("transfer_count", 0)
                
                request_metadata[request_id].update({
                    "tab_id": best_tab_id,
                    "original_tab_id": original_tab_id,
                    "transfer_count": transfer_count + 1,
                    "last_transfer_time": datetime.now().isoformat(),
                    "transfer_allowed": True
                })
                
                # 重建请求载荷
                openai_request = metadata.get("openai_request", {})
                session_id = metadata.get("session_id")
                message_id = metadata.get("message_id")
                mode_override = metadata.get("mode_override")
                battle_target_override = metadata.get("battle_target_override")
                
                # 转换为LMArena格式
                lmarena_payload = await convert_openai_to_lmarena_payload(
                    openai_request,
                    session_id,
                    message_id,
                    mode_override=mode_override,
                    battle_target_override=battle_target_override
                )
                
                # 构建WebSocket消息
                import json
                transfer_message = {
                    "request_id": request_id,
                    "payload": lmarena_payload,
                    "is_transfer": True,  # 标记为转移请求
                    "original_tab_id": original_tab_id,
                    "transfer_count": transfer_count + 1
                }
                
                # 发送到目标标签页
                await target_ws.send_text(json.dumps(transfer_message, ensure_ascii=False))
                
                # 更新请求计数
                tab_request_counts[best_tab_id] = tab_request_counts.get(best_tab_id, 0) + 1
                
                reassign_success_count += 1
                logger.info(f"[REQUEST_REASSIGN] ✅ 请求 {request_id[:8]} 已从 '{disconnected_tab_id}' 转移到 '{best_tab_id}' (转移次数: {transfer_count + 1}/{CONFIG.get('max_request_transfers', 3)})")
                
            except Exception as e:
                logger.error(f"[REQUEST_REASSIGN] ❌ 转移请求 {request_id[:8]} 失败: {e}", exc_info=True)
                reassign_fail_count += 1
                
                # 向响应通道发送错误
                if request_id in response_channels:
                    await response_channels[request_id].put({
                        "error": f"Request reassignment failed: {str(e)}"
                    })
                    await response_channels[request_id].put("[DONE]")
        
        logger.info(f"[REQUEST_REASSIGN] 📊 重新分配完成: 成功 {reassign_success_count}, 失败 {reassign_fail_count}")