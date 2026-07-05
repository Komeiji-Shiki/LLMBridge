"""
后台任务注册表

asyncio 事件循环对 Task 只持有弱引用（官方文档明确警告）：
裸调用 asyncio.create_task() 而不保存返回值时，
任务可能在执行途中被垃圾回收，造成后台逻辑静默丢失。

统一使用本模块的 spawn() 创建 fire-and-forget 任务：
- 全局集合持有强引用，任务完成后自动移除
- 任务抛出的未捕获异常会被记录日志，不再被静默吞掉
"""
import asyncio
import logging
from typing import Any, Coroutine, Optional

logger = logging.getLogger(__name__)

# 持有所有进行中后台任务的强引用
_BACKGROUND_TASKS: set = set()


def _on_task_done(task: "asyncio.Task") -> None:
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(f"后台任务 '{task.get_name()}' 异常退出: {exc}", exc_info=exc)


def spawn(coro: Coroutine[Any, Any, Any], *, name: Optional[str] = None) -> "asyncio.Task":
    """创建 fire-and-forget 后台任务并持有强引用，防止被垃圾回收。

    Args:
        coro: 要执行的协程
        name: 可选任务名（便于日志排查）

    Returns:
        创建的 asyncio.Task
    """
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_on_task_done)
    return task


def pending_task_count() -> int:
    """当前仍在运行的后台任务数（用于监控/调试）。"""
    return len(_BACKGROUND_TASKS)
