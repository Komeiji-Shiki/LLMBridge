"""
请求处理器
处理暂存请求、自动重试、服务器重启等
"""
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime

logger = logging.getLogger(__name__)


async def process_pending_requests(
    pending_requests_queue: asyncio.Queue,
    handle_single_completion_func
):
    """在后台处理暂存队列中的所有请求。

    🔧 修复说明：
    - 使用 get_nowait 循环替代 `while not empty(): await get()`，
      消除判空与取出之间的竞态窗口（其他消费者取走后 await get() 会永久挂起）。
    - future 可能已被客户端超时取消（asyncio.wait_for 超时会 cancel future），
      此时 set_result/set_exception 会抛 InvalidStateError 并炸掉整个处理循环，
      故所有写入前都先检查 future.done()。
    """
    while True:
        try:
            pending_item = pending_requests_queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        future = pending_item["future"]
        request_data = pending_item["request_data"]
        original_request_id = pending_item.get("original_request_id")

        if future.done():
            # 客户端已超时放弃，跳过无谓的重试
            logger.info(f"跳过已放弃的暂存请求 {original_request_id[:8] if original_request_id else '(new)'}")
            continue

        if original_request_id:
            logger.info(f"正在恢复请求 {original_request_id[:8]}...")
        else:
            logger.info("正在重试一个暂存的请求...")

        try:
            # 关键修复：重试时传递原始请求ID，以便使用绑定的endpoint
            response = await handle_single_completion_func(request_data, retry_request_id=original_request_id)

            # 将成功的结果设置到 future 中，以唤醒等待的客户端
            if not future.done():
                future.set_result(response)

            if original_request_id:
                logger.info(f"✅ 请求 {original_request_id[:8]} 已成功恢复并返回响应。")
            else:
                logger.info("✅ 一个暂存的请求已成功重试并返回响应。")

        except Exception as e:
            logger.error(f"重试暂存请求时发生错误: {e}", exc_info=True)
            # 将错误设置到 future 中，以便客户端知道请求失败了
            if not future.done():
                future.set_exception(e)
            # 若存在原通道，也向其投递错误以避免流式生成器挂死等 3000s 超时
            if original_request_id:
                try:
                    from core.app_state import get_app_state
                    _state = get_app_state()
                    queue = _state.response_channels.get(original_request_id)
                    if queue is not None:
                        queue.put_nowait({"error": f"Retry failed: {e}"})
                        queue.put_nowait("[DONE]")
                except Exception:
                    pass

        # 添加短暂的延迟，避免同时发送过多请求
        await asyncio.sleep(1)


def restart_server():
    """优雅地通知客户端刷新，然后重启服务器。"""
    logger.warning("="*60)
    logger.warning("检测到服务器空闲超时，准备自动重启...")
    logger.warning("="*60)
    
    # 延迟几秒以确保消息发送
    time.sleep(3)
    
    # 执行重启
    logger.info("正在重启服务器...")

    # 🔧 os._exit / execv 不执行任何清理逻辑：重启前先把 API Key 使用
    # 统计落盘、冲刷异步日志队列，避免重启丢数据（日志关闭必须最后做）
    try:
        from core.api_key_manager import api_key_manager
        api_key_manager.save_if_dirty()
    except Exception:
        pass
    try:
        from core.logging_config import shutdown_async_logging
        shutdown_async_logging()
    except Exception:
        pass

    if sys.platform == "win32":
        # 🔧 Windows 修复：execv 在 Windows 上并非真正替换进程（新旧进程
        # 短暂并存，端口绑定存在竞争），且 argv 拼接不带引号，解释器路径
        # 含空格时参数会碎。改用 Popen（正确引用参数）+ 显式退出：
        # 新进程完成 Python 启动/导入需要数秒，届时旧进程早已退出释放端口。
        import subprocess
        subprocess.Popen([sys.executable] + sys.argv, close_fds=True)
        os._exit(0)
    # POSIX 上 execv 是原子的进程镜像替换，argv[0] 应为解释器自身路径
    os.execv(sys.executable, [sys.executable] + sys.argv)


CHECK_INTERVAL_SECONDS = 10


async def idle_monitor(
    last_activity_time_ref: dict,
    CONFIG: dict,
    restart_server_func=None
):
    """监控服务器是否空闲，超过阈值则触发重启。

    🔧 修复：这个任务此前从未被启动过——config.jsonc 里有
    enable_idle_restart / idle_restart_timeout_seconds，管理面板的配置表单
    也把它们做成了可编辑项，但没有任何执行者，改了完全没效果。
    现在由 lifespan 通过 spawn() 拉起。

    改为协程而非后台线程：原同步版用 time.sleep 独占一个线程，且读
    CONFIG 时与配置热重载线程无同步；协程版每轮都重新读取配置，
    运行中改 enable_idle_restart 立即生效。
    """
    if restart_server_func is None:
        restart_server_func = restart_server

    # 等待，直到 last_activity_time 被首次设置
    while last_activity_time_ref.get('time') is None:
        await asyncio.sleep(1)

    logger.info("[IDLE_MONITOR] 空闲重启监控任务已启动。")

    while True:
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        try:
            if not CONFIG.get("enable_idle_restart", False):
                continue

            timeout = CONFIG.get("idle_restart_timeout_seconds", 300)
            # -1 表示禁用重启检查
            if not isinstance(timeout, (int, float)) or timeout < 0:
                continue

            last_activity = last_activity_time_ref.get('time')
            if last_activity is None:
                continue

            idle_time = (datetime.now() - last_activity).total_seconds()
            if idle_time > timeout:
                logger.info(f"[IDLE_MONITOR] 服务器空闲 {idle_time:.0f}s 已超过阈值 {timeout}s。")
                restart_server_func()
                return
        except Exception as e:
            logger.error(f"[IDLE_MONITOR] 错误: {e}", exc_info=True)