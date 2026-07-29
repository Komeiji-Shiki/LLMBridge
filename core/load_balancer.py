"""
负载均衡模块
处理多标签页的负载均衡和请求分配

公开 API（统一从 AppState 获取状态）：
- select_best_tab()          选择负载最低的标签页
- release_tab(tab_id)        释放标签页请求计数
- reassign_tab_requests(id)  重分配断开标签页的待处理请求

🔧 重构说明：旧版存在"长参数注入版 + AppState 包装版 + api_server 再包一层"
三层同义函数。现在长参数版降级为私有实现（_impl 后缀），仅供本模块内部使用。

🔒 锁策略（修复说明）：
- browser_connections 与 tab_request_counts 统一由 browser_connections_lock
  保护。旧版 release 走独立的 tab_request_counts_lock，与 select/reassign
  用的锁互不排斥，同一份计数被两把锁"保护"等于没有互斥。
- reassign 采用"锁内决策、锁外发送"：锁内只做目标选择与状态更新，
  payload 转换与 WebSocket 发送全部移到锁外，慢客户端不再拖住全局锁。
- 本模块所有公开函数都会自行获取 browser_connections_lock，
  调用方不得在已持有该锁的情况下调用（asyncio.Lock 不可重入）。
"""
import asyncio
import json
import logging
from typing import Tuple
from fastapi import HTTPException, WebSocket
from datetime import datetime

logger = logging.getLogger(__name__)


# ============================================================
# 公开 API（基于 AppState）
# ============================================================

async def select_best_tab() -> Tuple[str, WebSocket]:
    """从 AppState 获取连接状态，选择负载最低的标签页。"""
    from core.app_state import get_app_state
    conn = get_app_state().connection
    return await _select_best_tab_impl(
        conn.browser_connections, conn.browser_connections_lock, conn.tab_request_counts
    )


async def release_tab(tab_id: str) -> None:
    """从 AppState 获取连接状态，释放标签页请求计数。"""
    from core.app_state import get_app_state
    conn = get_app_state().connection
    await _release_tab_impl(tab_id, conn.tab_request_counts, conn.browser_connections_lock)


async def reassign_tab_requests(disconnected_tab_id: str) -> None:
    """从 AppState 获取状态，重分配断开标签页的待处理请求。"""
    from core.app_state import get_app_state
    from core.config_loader import CONFIG
    from services.message_converter import convert_openai_to_lmarena_payload
    state = get_app_state()
    await _reassign_pending_requests_impl(
        disconnected_tab_id,
        state.connection.browser_connections,
        state.connection.browser_connections_lock,
        state.request.response_channels,
        state.request.request_metadata,
        state.connection.tab_request_counts,
        CONFIG,
        convert_openai_to_lmarena_payload,
    )


# ============================================================
# 内部实现
# ============================================================

async def _select_best_tab_impl(
    browser_connections: dict,
    browser_connections_lock: asyncio.Lock,
    tab_request_counts: dict
) -> Tuple[str, WebSocket]:
    """
    选择负载最低的标签页来处理新请求。
    返回 (tab_id, websocket)
    """
    async def _acquire_lock_and_select():
        async with browser_connections_lock:
            if not browser_connections:
                raise HTTPException(status_code=503, detail="没有可用的浏览器连接")

            # 清理已断开连接的标签页计数
            stale_tabs = [tab_id for tab_id in tab_request_counts.keys() if tab_id not in browser_connections]
            for tab_id in stale_tabs:
                del tab_request_counts[tab_id]
                logger.debug(f"[LOAD_BALANCE] 清理已断开标签页 '{tab_id}' 的计数")

            # 确保所有活跃标签页都有计数
            for tab_id in browser_connections.keys():
                if tab_id not in tab_request_counts:
                    tab_request_counts[tab_id] = 0

            # 只从活跃连接中选择负载最低的标签页
            active_tab_loads = {tab_id: tab_request_counts.get(tab_id, 0) for tab_id in browser_connections.keys()}
            best_tab_id = min(active_tab_loads, key=lambda t: active_tab_loads[t])
            best_ws = browser_connections[best_tab_id]

            # 增加该标签页的请求计数
            tab_request_counts[best_tab_id] += 1

            logger.info(f"[LOAD_BALANCE] 选择标签页 '{best_tab_id}' (当前负载: {tab_request_counts[best_tab_id]}/6)")
            logger.debug(f"[LOAD_BALANCE] 所有标签页负载: {tab_request_counts}")

            return best_tab_id, best_ws

    # 🔧 配置接线：config.jsonc / 管理面板暴露了 load_balancer_lock_timeout_seconds，
    # 但此前超时值与日志文案都硬编码为 5 秒，用户改配置不生效
    from core.config_loader import get_float_setting
    from core.constants import TimeoutDefaults
    lock_timeout = get_float_setting(
        "load_balancer_lock_timeout_seconds", TimeoutDefaults.LOAD_BALANCER_LOCK_TIMEOUT
    )

    try:
        # 超时保护，防止死锁
        return await asyncio.wait_for(_acquire_lock_and_select(), timeout=lock_timeout)
    except asyncio.TimeoutError:
        logger.error(f"[LOAD_BALANCE] ❌ 获取 browser_connections_lock 超时（{lock_timeout:g}秒）！可能存在死锁")
        logger.error(f"[LOAD_BALANCE] 当前浏览器连接数: {len(browser_connections)}, 标签页计数: {tab_request_counts}")
        raise HTTPException(status_code=503, detail="服务器负载均衡锁超时，可能存在死锁")


async def _release_tab_impl(tab_id: str, tab_request_counts: dict, lock: asyncio.Lock):
    """释放标签页的请求计数（与 select/reassign 共用 browser_connections_lock）"""
    async with lock:
        if tab_id in tab_request_counts and tab_request_counts[tab_id] > 0:
            tab_request_counts[tab_id] -= 1
            logger.debug(f"[LOAD_BALANCE] 释放标签页 '{tab_id}' 的请求 (剩余负载: {tab_request_counts[tab_id]}/6)")


async def _notify_request_failed(response_channels: dict, request_id: str, message: str) -> None:
    """向响应通道发送失败消息（通道可能已被消费者清理，用 .get 容错）。"""
    queue = response_channels.get(request_id)
    if queue is not None:
        await queue.put({"error": message})
        await queue.put("[DONE]")


async def _reassign_pending_requests_impl(
    disconnected_tab_id: str,
    browser_connections: dict,
    browser_connections_lock: asyncio.Lock,
    response_channels: dict,
    request_metadata: dict,
    tab_request_counts: dict,
    CONFIG: dict,
    convert_openai_to_lmarena_payload
):
    """当标签页断开时，将其待处理请求重新分配给其他活跃标签页。

    两阶段执行：
    1. 锁内决策：筛选待转移请求、选择目标标签页、更新元数据与计数；
    2. 锁外执行：payload 转换与 WebSocket 发送（网络 IO 不占全局锁），
       发送失败时回滚目标标签页的计数并通知请求方。
    """
    logger.info(f"[REQUEST_REASSIGN] 🔄 开始检查标签页 '{disconnected_tab_id}' 的待处理请求...")

    max_transfers = CONFIG.get("max_request_transfers", 3)
    over_limit: list = []   # 超过最大转移次数的请求 ID
    planned: list = []      # 已在锁内完成决策、待锁外发送的转移任务

    # ---- 阶段 1：锁内决策 ----
    async with browser_connections_lock:
        active_tabs = list(browser_connections.keys())
        if not active_tabs:
            logger.warning(f"[REQUEST_REASSIGN] ⚠️ 没有其他活跃标签页，无法重新分配请求")
            return

        logger.info(f"[REQUEST_REASSIGN] 发现 {len(active_tabs)} 个活跃标签页可用于接收请求")

        for request_id, metadata in list(request_metadata.items()):
            if metadata.get("tab_id") != disconnected_tab_id:
                continue

            transfer_count = metadata.get("transfer_count", 0)
            if transfer_count >= max_transfers:
                over_limit.append(request_id)
                continue

            # 选择当前负载最低的标签页
            best_tab_id = min(active_tabs, key=lambda t: tab_request_counts.get(t, 0))
            target_ws = browser_connections[best_tab_id]

            original_tab_id = metadata.get("original_tab_id", disconnected_tab_id)
            metadata.update({
                "tab_id": best_tab_id,
                "original_tab_id": original_tab_id,
                "transfer_count": transfer_count + 1,
                "last_transfer_time": datetime.now().isoformat(),
                "transfer_allowed": True,
            })
            tab_request_counts[best_tab_id] = tab_request_counts.get(best_tab_id, 0) + 1

            planned.append({
                "request_id": request_id,
                "tab_id": best_tab_id,
                "ws": target_ws,
                "openai_request": metadata.get("openai_request", {}),
                "session_id": metadata.get("session_id"),
                "mode_override": metadata.get("mode_override"),
                "battle_target_override": metadata.get("battle_target_override"),
                "original_tab_id": original_tab_id,
                "transfer_count": transfer_count + 1,
            })

    # ---- 阶段 2：锁外执行 ----
    for request_id in over_limit:
        logger.warning(f"[REQUEST_REASSIGN] ⚠️ 请求 {request_id[:8]} 已达到最大转移次数 ({max_transfers})，标记为失败")
        await _notify_request_failed(
            response_channels, request_id,
            f"Request failed after {max_transfers} transfer attempts"
        )

    if not planned:
        if not over_limit:
            logger.info(f"[REQUEST_REASSIGN] ✅ 标签页 '{disconnected_tab_id}' 没有待处理请求")
        return

    logger.info(f"[REQUEST_REASSIGN] 📦 发现 {len(planned)} 个需要重新分配的请求")

    reassign_success_count = 0
    reassign_fail_count = 0

    for item in planned:
        request_id = item["request_id"]
        try:
            # 重建请求载荷
            # 🔧 修复：旧版本误把 message_id 作为第三个位置参数传入，
            # 会污染 mode_override（转换函数签名没有 message_id 参数）
            lmarena_payload = await convert_openai_to_lmarena_payload(
                item["openai_request"],
                item["session_id"],
                mode_override=item["mode_override"],
                battle_target_override=item["battle_target_override"]
            )

            transfer_message = {
                "request_id": request_id,
                "payload": lmarena_payload,
                "is_transfer": True,  # 标记为转移请求
                "original_tab_id": item["original_tab_id"],
                "transfer_count": item["transfer_count"]
            }

            await item["ws"].send_text(json.dumps(transfer_message, ensure_ascii=False))

            reassign_success_count += 1
            logger.info(f"[REQUEST_REASSIGN] ✅ 请求 {request_id[:8]} 已从 '{disconnected_tab_id}' 转移到 "
                        f"'{item['tab_id']}' (转移次数: {item['transfer_count']}/{max_transfers})")

        except Exception as e:
            reassign_fail_count += 1
            logger.error(f"[REQUEST_REASSIGN] ❌ 转移请求 {request_id[:8]} 失败: {e}", exc_info=True)

            # 回滚锁内已增加的计数，避免目标标签页负载虚高
            await _release_tab_impl(item["tab_id"], tab_request_counts, browser_connections_lock)
            await _notify_request_failed(
                response_channels, request_id,
                f"Request reassignment failed: {str(e)}"
            )

    logger.info(f"[REQUEST_REASSIGN] 📊 重新分配完成: 成功 {reassign_success_count}, 失败 {reassign_fail_count}")
