"""进程内存回收辅助。

Python 在 Windows 上释放大对象后，内存页通常不会立刻还给系统（pymalloc
arena 与 CRT 堆都会保留），任务管理器里的工作集因此只涨不落。这里在
GC 之后调用 SetProcessWorkingSetSize，把不再活跃的页换出，让 RSS 回落。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def trim_working_set() -> bool:
    """把当前进程的工作集尽量压回系统（仅 Windows 有效）。

    返回 True 表示调用成功。该操作只换出页面，不释放仍被引用的对象，
    因此不能替代真正的内存治理，只能消除“GC 后 RSS 不回落”的假象。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetProcessWorkingSetSize.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_size_t,
        ]
        kernel32.SetProcessWorkingSetSize.restype = ctypes.c_int

        handle = kernel32.GetCurrentProcess()
        # (SIZE_T)-1 表示由系统重新计算最小/最大工作集，效果等同于清空工作集
        return bool(
            kernel32.SetProcessWorkingSetSize(
                handle, ctypes.c_size_t(-1), ctypes.c_size_t(-1)
            )
        )
    except Exception as exc:
        logger.debug("[MEM_TRIM] 工作集回收失败: %s", exc)
        return False
