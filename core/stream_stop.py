"""监控页手动收尾卡住的流式请求。

流式链路开始读上游时按监控请求 ID 登记；管理员在监控页点“收尾”后，读取循环不再等上游，
正在等的那一块被取消、上游连接随之释放，已经转发的内容由各链路按正常结束补齐结尾交给客户端。
请求结束统计时注销。
"""
import asyncio

from core.request_context import current_request

MANUAL_STOP_REASON = 'manual_finish'
# 客户端中途断开时各条链路统一记的失败原因（可带后缀说明断在哪）；统计按这个前缀把它和上游失败分开
CLIENT_DISCONNECTED = 'Client disconnected'

_signals: dict = {}


def register(request_id: str) -> asyncio.Event:
    return _signals.setdefault(request_id, asyncio.Event())


def release(request_id: str) -> None:
    _signals.pop(request_id, None)


def request_finish(request_id: str) -> bool:
    signal = _signals.get(request_id)
    if signal is None:
        return False
    signal.set()
    return True


def finishable(request_id: str) -> bool:
    return request_id in _signals


def mark_finished() -> None:
    """停止原因记为手动收尾：结尾是网关补的，监控记录要能和上游自己结束区分开。"""
    context = current_request.get()
    if context is not None:
        context.outcome['stop_reason'] = MANUAL_STOP_REASON
