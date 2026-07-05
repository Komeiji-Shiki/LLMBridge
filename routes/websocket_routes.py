"""
WebSocket路由处理
处理来自油猴脚本的WebSocket连接

重构说明：
- 连接/请求状态从 AppState 单例获取，不再使用长参数链注入
- 人机验证状态统一读写 AppState.server（旧版重新绑定函数参数是无效操作）
"""
import asyncio
import json
import logging
import time
from fastapi import WebSocket, WebSocketDisconnect

from core.config_loader import CONFIG
from core.app_state import get_app_state
from core.load_balancer import reassign_tab_requests
from modules.monitoring import monitoring_service
from utils.task_registry import spawn

logger = logging.getLogger(__name__)

_app_state = get_app_state()


def _schedule_pending_requests() -> None:
    """在后台处理暂存队列（浏览器重连后的自动重试）。"""
    from background_tasks.request_processor import process_pending_requests
    from routes.api_routes import handle_single_completion

    spawn(process_pending_requests(
        _app_state.pending_requests_queue,
        handle_single_completion
    ), name="pending-requests")


async def websocket_endpoint(websocket: WebSocket):
    """处理来自油猴脚本的 WebSocket 连接（支持多标签页）。"""
    browser_ws_ref = _app_state.connection.browser_ws_ref
    browser_connections = _app_state.browser_connections
    browser_connections_lock = _app_state.browser_connections_lock
    tab_connection_times = _app_state.connection.tab_connection_times
    tab_request_counts = _app_state.connection.tab_request_counts
    tab_request_counts_lock = _app_state.connection.tab_request_counts_lock
    response_channels = _app_state.response_channels
    request_metadata = _app_state.request_metadata
    pending_requests_queue = _app_state.pending_requests_queue
    server_state = _app_state.server

    await websocket.accept()

    # 等待第一条消息（可能包含标签页ID）
    tab_id = "default"  # 默认标签页ID（向后兼容）
    first_message_handled = False
    first_real_message = None

    try:
        # 设置3秒超时等待可能的标签页ID
        init_message_str = await asyncio.wait_for(websocket.receive_text(), timeout=3.0)
        init_message = json.loads(init_message_str)

        # 检查是否包含tab_id
        if "tab_id" in init_message:
            tab_id = init_message["tab_id"]
            first_message_handled = True
            logger.info(f"[WS_CONN] 📋 收到标签页ID: {tab_id}")
        else:
            # 旧版本脚本，没有发送tab_id，这条消息需要在后面处理
            logger.warning(f"[WS_CONN] ⚠️ 未检测到tab_id，使用默认值（可能是旧版本脚本）")
            # 暂存这条消息，稍后处理
            first_real_message = init_message_str
    except asyncio.TimeoutError:
        logger.warning(f"[WS_CONN] ⚠️ 等待tab_id超时，使用默认值（可能是旧版本脚本）")
    except json.JSONDecodeError:
        logger.warning(f"[WS_CONN] ⚠️ 无法解析初始化消息，使用默认tab_id")

    # 使用锁保护WebSocket连接的修改
    async with browser_connections_lock:
        # 检查是否已有相同tab_id的连接
        if tab_id in browser_connections:
            logger.warning(f"[WS_CONN] 标签页 {tab_id} 已存在连接，将被新连接替换")

        browser_connections[tab_id] = websocket
        # 记录连接时间
        tab_connection_times[tab_id] = time.time()

        # 兼容性：将第一个连接设置为browser_ws
        if not browser_ws_ref['ws'] or tab_id == "default":
            browser_ws_ref['ws'] = websocket

        # 只要有新的连接建立，就意味着人机验证流程已结束（或从未开始）
        # 🔧 修复：直接写 AppState.server（旧版重新绑定函数参数，实际不生效）
        if server_state.IS_REFRESHING_FOR_VERIFICATION or server_state.VERIFICATION_COOLDOWN_UNTIL is not None:
            logger.info("✅ 新的 WebSocket 连接已建立，人机验证状态和冷却已自动重置。")
            server_state.IS_REFRESHING_FOR_VERIFICATION = False
            server_state.VERIFICATION_COOLDOWN_UNTIL = None

        # 计算并发能力
        concurrent_capacity = len(browser_connections) * 6
        logger.info("=" * 80)
        logger.info(f"✅ 标签页 '{tab_id}' 已成功连接 WebSocket")
        logger.info(f"📊 当前连接状态:")
        logger.info(f"  - 活跃标签页数: {len(browser_connections)}")
        logger.info(f"  - 理论最大并发: {concurrent_capacity} 个请求 (每标签页6个)")
        logger.info(f"  - 未处理请求数: {len(response_channels)}")

        # 并发限制提示
        if len(browser_connections) == 1:
            logger.warning(f"⚠️  注意：单标签页模式，浏览器HTTP/1.1限制并发为6个请求")
            logger.warning(f"💡 如需更高并发，请打开额外的LMArena标签页并运行油猴脚本")
        else:
            logger.info(f"✅ 多标签页模式已激活！当前支持 {concurrent_capacity} 个并发请求")

        logger.info("=" * 80)

    # 广播浏览器连接状态到监控面板
    await monitoring_service.broadcast_to_monitors({
        "type": "browser_status",
        "connected": True
    })

    # 广播标签页状态更新
    await monitoring_service.broadcast_to_monitors({
        "type": "tab_connection",
        "action": "connected",
        "tab_id": tab_id,
        "total_tabs": len(browser_connections),
        "total_capacity": len(browser_connections) * 6
    })

    # 🔧 启动心跳任务：定期向客户端发送心跳，检测静默断开
    async def _heartbeat_loop():
        """后台心跳：定期发送消息检测客户端是否还活着"""
        interval = CONFIG.get("websocket_heartbeat_interval", 60)
        while True:
            try:
                await asyncio.sleep(interval)
                await asyncio.wait_for(
                    websocket.send_json({
                        "type": "heartbeat",
                        "timestamp": time.time()
                    }),
                    timeout=10.0
                )
                logger.debug(f"[WS_HEARTBEAT] 💓 标签页 '{tab_id}' 心跳正常")
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"[WS_HEARTBEAT] 💔 标签页 '{tab_id}' 心跳失败: {type(e).__name__}: {e}")
                # 心跳失败 → 客户端已静默断开，关闭 WebSocket 触发 finally 清理
                try:
                    await websocket.close(code=1001, reason="Heartbeat failed")
                except Exception:
                    pass
                return

    heartbeat_task = asyncio.create_task(_heartbeat_loop())

    # 处理所有待恢复的请求
    if CONFIG.get("enable_auto_retry", False):
        # 1. 首先处理pending_requests_queue中的请求
        if not pending_requests_queue.empty():
            logger.info(f"检测到 {pending_requests_queue.qsize()} 个暂存的请求，将在后台自动重试...")
            _schedule_pending_requests()

        # 2. 然后处理response_channels中未完成的请求
        if len(response_channels) > 0:
            logger.info(f"[REQUEST_RECOVERY] 检测到 {len(response_channels)} 个未完成的请求，准备恢复...")

            # 获取所有未完成请求的ID
            pending_request_ids = list(response_channels.keys())

            for request_id in pending_request_ids:
                # 尝试从多个来源获取请求数据
                request_data = None

                # 来源1：request_metadata（新增的存储）
                if request_id in request_metadata:
                    request_data = request_metadata[request_id]["openai_request"]
                    logger.info(f"[REQUEST_RECOVERY] 从request_metadata恢复请求 {request_id[:8]}")

                # 来源2：monitoring_service.active_requests（备用）
                elif hasattr(monitoring_service, 'active_requests') and request_id in monitoring_service.active_requests:
                    active_req = monitoring_service.active_requests[request_id]
                    # 重建OpenAI请求格式（RequestInfo 内存中仅保留预览与参数，消息全量不可恢复）
                    req_params = active_req.request_params or {}
                    request_data = {
                        "model": active_req.model,
                        "messages": [],
                        "stream": req_params.get("streaming", False),
                        "temperature": req_params.get("temperature"),
                        "top_p": req_params.get("top_p"),
                        "max_tokens": req_params.get("max_tokens"),
                    }
                    logger.info(f"[REQUEST_RECOVERY] 从monitoring_service恢复请求 {request_id[:8]}")
                else:
                    logger.warning(f"[REQUEST_RECOVERY] ⚠️ 无法恢复请求 {request_id[:8]}：找不到原始数据")
                    # 清理这个无法恢复的请求
                    queue = response_channels.get(request_id)
                    if queue is not None:
                        await queue.put({"error": "Request data lost during reconnection"})
                        await queue.put("[DONE]")
                    continue

                # 如果成功获取到请求数据，将其加入重试队列
                if request_data:
                    # 创建一个新的future来等待重试结果
                    future = asyncio.get_event_loop().create_future()

                    # 将请求放入pending队列
                    await pending_requests_queue.put({
                        "future": future,
                        "request_data": request_data,
                        "original_request_id": request_id  # 保留原始请求ID用于追踪
                    })

                    logger.info(f"[REQUEST_RECOVERY] ✅ 请求 {request_id[:8]} 已加入重试队列")

            # 启动恢复处理
            if not pending_requests_queue.empty():
                logger.info(f"[REQUEST_RECOVERY] 开始处理 {pending_requests_queue.qsize()} 个恢复的请求...")
                _schedule_pending_requests()
            else:
                logger.info(f"[REQUEST_RECOVERY] 没有可恢复的请求")

    try:
        # 如果第一条消息未被处理（旧版本脚本），需要先处理它
        if not first_message_handled and first_real_message is not None:
            message = json.loads(first_real_message)

            request_id = message.get("request_id")
            data = message.get("data")

            if request_id and data is not None:
                queue = response_channels.get(request_id)
                if queue is None:
                    logger.warning(f"[WS_MSG] 收到未知或已关闭请求的响应: {request_id}")
                else:
                    await queue.put(data)

        while True:
            # 等待并接收来自油猴脚本的消息
            message_str = await websocket.receive_text()
            message = json.loads(message_str)

            request_id = message.get("request_id")
            data = message.get("data")

            if not request_id or data is None:
                logger.warning(f"[WS_MSG] 收到来自浏览器的无效消息: {message}")
                continue

            # 诊断：记录WebSocket消息
            if CONFIG.get("debug_stream_timing", False):
                current_time = time.time()
                data_preview = str(data)[:200] if data else "None"
                logger.debug(f"[WS_MSG] 时间: {current_time:.3f}, 请求ID: {request_id[:8]}, 数据预览: {data_preview}...")

                # 如果是字符串数据，检查是否包含多个文本块
                if isinstance(data, str) and 'a0:"' in data:
                    import re
                    text_pattern = re.compile(r'[ab]0:"((?:\\.|[^"\\])*)"')
                    matches = text_pattern.findall(data)
                    logger.debug(f"[WS_MSG] 单个WebSocket消息中包含 {len(matches)} 个文本块")
                    if len(matches) > 5:
                        logger.warning(f"⚠️ 检测到严重的流式数据累积！单个WebSocket消息包含了 {len(matches)} 个文本块")
                        logger.warning(f"   这可能影响流式响应的实时性")

            # 核心修复：实现智能路由 - 允许任何活跃标签页响应转移的请求
            queue = response_channels.get(request_id)
            if queue is not None:
                # 检查请求元数据以验证来源（同样用 .get 避免 TOCTOU）
                metadata = request_metadata.get(request_id)
                if metadata is not None:
                    expected_tab_id = metadata.get("tab_id")
                    transfer_allowed = metadata.get("transfer_allowed", True)

                    # 如果允许转移，任何标签页都可以响应
                    if transfer_allowed:
                        if tab_id != expected_tab_id:
                            logger.info(f"[WS_MSG_ROUTE] ✅ 请求 {request_id[:8]} 允许跨标签页路由: "
                                        f"期望 '{expected_tab_id}' -> 实际 '{tab_id}'")
                        await queue.put(data)
                    else:
                        # 严格验证tab_id匹配
                        if tab_id == expected_tab_id:
                            await queue.put(data)
                        else:
                            logger.warning(f"[WS_MSG_ROUTE] ⚠️ 请求 {request_id[:8]} 不允许跨标签页路由: "
                                           f"期望 '{expected_tab_id}' != 实际 '{tab_id}'，消息被拒绝")
                else:
                    # 没有元数据，直接放入（向后兼容）
                    await queue.put(data)
            else:
                logger.warning(f"[WS_MSG] 收到未知或已关闭请求的响应: {request_id}")

    except WebSocketDisconnect:
        logger.warning(f"❌ 标签页 '{tab_id}' 已断开连接。")
    except Exception as e:
        logger.error(f"[WS_ERROR] 标签页 '{tab_id}' WebSocket处理时发生错误: {e}", exc_info=True)
    finally:
        # 取消心跳任务
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except (asyncio.CancelledError, Exception):
            pass

        # 核心修复：在标签页断开时执行请求重分配
        logger.info(f"[WS_DISCONNECT] 📋 标签页 '{tab_id}' 开始断连清理流程...")

        # 修复1：立即释放该标签页的所有请求计数
        async with tab_request_counts_lock:
            if tab_id in tab_request_counts:
                pending_count = tab_request_counts[tab_id]
                if pending_count > 0:
                    logger.warning(f"[WS_DISCONNECT] ⚠️ 标签页 '{tab_id}' 断开时仍有 {pending_count} 个活跃请求")
                del tab_request_counts[tab_id]
                logger.info(f"[WS_DISCONNECT] 已清理标签页 '{tab_id}' 的请求计数")

        async with browser_connections_lock:
            # 移除断开的标签页连接
            if tab_id in browser_connections:
                del browser_connections[tab_id]
                logger.info(f"[WS_CONN] 标签页 '{tab_id}' 已移除")

            # 移除连接时间记录
            if tab_id in tab_connection_times:
                del tab_connection_times[tab_id]

            # 更新browser_ws（向后兼容）
            if browser_connections:
                # 如果还有其他连接，使用第一个
                browser_ws_ref['ws'] = list(browser_connections.values())[0]
                logger.info(f"[WS_CONN] browser_ws已更新为剩余的{len(browser_connections)}个连接中的第一个")
            else:
                browser_ws_ref['ws'] = None
                logger.info(f"[WS_CONN] 所有标签页已断开")

            # 计算剩余并发能力
            remaining_capacity = len(browser_connections) * 6
            logger.info(f"[WS_CONN] 剩余活跃标签页: {len(browser_connections)}")
            logger.info(f"[WS_CONN] 剩余并发能力: {remaining_capacity} 个请求")
            logger.info(f"[WS_CONN] 未处理请求数: {len(response_channels)}")

            # 核心修复2：如果还有其他活跃标签页，则重新分配请求
            if browser_connections:
                logger.info(f"[WS_DISCONNECT] 🔄 检测到 {len(browser_connections)} 个活跃标签页，开始请求重分配...")
                try:
                    await reassign_tab_requests(tab_id)
                except Exception as reassign_error:
                    logger.error(f"[WS_DISCONNECT] ❌ 请求重分配失败: {reassign_error}", exc_info=True)
            else:
                logger.warning(f"[WS_DISCONNECT] ⚠️ 没有其他活跃标签页，无法重新分配请求")

        # 广播浏览器断开状态到监控面板
        await monitoring_service.broadcast_to_monitors({
            "type": "browser_status",
            "connected": len(browser_connections) > 0
        })

        # 广播标签页状态更新
        await monitoring_service.broadcast_to_monitors({
            "type": "tab_connection",
            "action": "disconnected",
            "tab_id": tab_id,
            "total_tabs": len(browser_connections),
            "total_capacity": len(browser_connections) * 6
        })

        # 如果禁用了自动重试，则像以前一样清理通道
        if not CONFIG.get("enable_auto_retry", False):
            # 清理所有等待的响应通道，以防请求被挂起
            for queue in response_channels.values():
                await queue.put({"error": "Browser disconnected during operation"})
            response_channels.clear()
            logger.info("WebSocket 连接已清理（自动重试已禁用）。")
        else:
            # 🔧 根因3修复：即使启用了自动重试，也要向属于已断开标签页的请求发送信号
            # 否则这些请求会卡在 queue.get() 直到超时
            if not browser_connections:
                # 所有标签页都断了，且自动重试已启用 — 让请求等待重连
                logger.info("WebSocket 连接已关闭（自动重试已启用，请求将等待重连）。")
            else:
                # 还有其他标签页，只通知属于断开标签页的请求
                notified_count = 0
                for req_id, metadata in list(request_metadata.items()):
                    if metadata.get("tab_id") == tab_id:
                        queue = response_channels.get(req_id)
                        if queue is None:
                            continue
                        # 如果请求允许转移，跳过（reassign已处理）
                        if metadata.get("transfer_allowed", True):
                            continue
                        # 不允许转移的请求，直接报错
                        await queue.put({"error": f"Tab '{tab_id}' disconnected and request is not transferable"})
                        notified_count += 1
                if notified_count > 0:
                    logger.warning(f"[WS_DISCONNECT] 🔧 已通知 {notified_count} 个不可转移的请求")
