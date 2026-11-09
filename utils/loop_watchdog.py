"""事件循环停顿看门狗。

循环里的心跳协程定时打点；看门狗线程发现心跳超时，就把当时各线程的调用栈写进日志。
停顿不一定是循环线程自己在忙：别的线程长时间占着 GIL 时循环同样动不了
（本地 token 估算每条消息统计一遍词表就是这样被发现的），所以其他线程的栈也一起记。
"""
import asyncio
import logging
import sys
import threading
import time
import traceback

logger = logging.getLogger(__name__)

BEAT_SECONDS = 0.1
STALL_SECONDS = 1.0
_STACK_DEPTH = 8

stats = {'stalls': 0, 'max_stall_ms': 0.0, 'last_stall_at': None, 'last_stall_ms': 0.0}
_last_beat = 0.0
_loop_thread_id = None
_stop = threading.Event()
_thread = None


def _format_stacks(frames, loop_thread_id):
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    blocks = []
    # 循环线程排最前，其余线程只看不在空等的
    for thread_id, frame in sorted(frames.items(), key=lambda item: item[0] != loop_thread_id):
        if thread_id == threading.get_ident():
            continue
        stack = traceback.extract_stack(frame)[-_STACK_DEPTH:]
        if thread_id != loop_thread_id and stack and stack[-1].name in ('wait', '_worker', 'get', 'select', '_poll'):
            continue
        label = '事件循环线程' if thread_id == loop_thread_id else f"线程 {names.get(thread_id, thread_id)}"
        blocks.append(label + '\n' + ''.join(traceback.format_list(stack)).rstrip())
    return '\n'.join(blocks)


def _watch():
    reported_beat = None
    stalled_stacks = ''
    while not _stop.wait(STALL_SECONDS / 4):
        beat = _last_beat
        if not beat:
            continue
        stalled = time.monotonic() - beat
        if stalled >= STALL_SECONDS and reported_beat != beat:
            reported_beat = beat
            stalled_stacks = _format_stacks(sys._current_frames(), _loop_thread_id)
        elif reported_beat is not None and beat != reported_beat:
            # 心跳恢复后再写日志，这时才知道一共停了多久
            duration_ms = round((beat - reported_beat) * 1000, 1)
            stats['stalls'] += 1
            stats['last_stall_ms'] = duration_ms
            stats['max_stall_ms'] = max(stats['max_stall_ms'], duration_ms)
            stats['last_stall_at'] = time.time()
            logger.warning('[LOOP_WATCHDOG] 事件循环停顿 %.1f 秒，停顿期间的调用栈：\n%s', duration_ms / 1000, stalled_stacks)
            reported_beat = None


async def heartbeat():
    """在事件循环里运行；同时负责启动和停止看门狗线程。"""
    global _last_beat, _loop_thread_id, _thread
    _loop_thread_id = threading.get_ident()
    _stop.clear()
    _thread = threading.Thread(target=_watch, name='loop-watchdog', daemon=True)
    _thread.start()
    try:
        while True:
            _last_beat = time.monotonic()
            await asyncio.sleep(BEAT_SECONDS)
    finally:
        _last_beat = 0.0
        _stop.set()
