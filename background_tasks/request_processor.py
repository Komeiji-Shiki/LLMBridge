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
    """在后台处理暂存队列中的所有请求。"""
    while not pending_requests_queue.empty():
        pending_item = await pending_requests_queue.get()
        future = pending_item["future"]
        request_data = pending_item["request_data"]
        original_request_id = pending_item.get("original_request_id")
        
        if original_request_id:
            logger.info(f"正在恢复请求 {original_request_id[:8]}...")
        else:
            logger.info("正在重试一个暂存的请求...")

        try:
            # 关键修复：重试时传递原始请求ID，以便使用绑定的endpoint
            response = await handle_single_completion_func(request_data, retry_request_id=original_request_id)
            
            # 将成功的结果设置到 future 中，以唤醒等待的客户端
            future.set_result(response)
            
            if original_request_id:
                logger.info(f"✅ 请求 {original_request_id[:8]} 已成功恢复并返回响应。")
            else:
                logger.info("✅ 一个暂存的请求已成功重试并返回响应。")

        except Exception as e:
            logger.error(f"重试暂存请求时发生错误: {e}", exc_info=True)
            # 将错误设置到 future 中，以便客户端知道请求失败了
            future.set_exception(e)
        
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
    os.execv(sys.executable, ['python'] + sys.argv)


def idle_monitor(
    last_activity_time_ref: dict,
    CONFIG: dict,
    restart_server_func
):
    """在后台线程中运行，监控服务器是否空闲。"""
    # 等待，直到 last_activity_time 被首次设置
    while last_activity_time_ref.get('time') is None:
        time.sleep(1)
        
    logger.info("空闲监控线程已启动。")
    
    while True:
        if CONFIG.get("enable_idle_restart", False):
            timeout = CONFIG.get("idle_restart_timeout_seconds", 300)
            
            # 如果超时设置为-1，则禁用重启检查
            if timeout == -1:
                time.sleep(10)
                continue

            idle_time = (datetime.now() - last_activity_time_ref['time']).total_seconds()
            
            if idle_time > timeout:
                logger.info(f"服务器空闲时间 ({idle_time:.0f}s) 已超过阈值 ({timeout}s)。")
                restart_server_func()
                break
                
        # 每 10 秒检查一次
        time.sleep(10)