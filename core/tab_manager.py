from typing import Tuple

from fastapi import WebSocket

from core.load_balancer import (
    select_best_tab_for_request as _select_best_tab_for_request,
    release_tab_request as _release_tab_request,
    reassign_pending_requests as _reassign_pending_requests,
)

from core import global_state as gs
from core.config_loader import CONFIG
from services.message_converter import convert_openai_to_lmarena_payload


async def select_best_tab_for_request() -> Tuple[str, WebSocket]:
    """选择负载最低的标签页来处理新请求"""
    return await _select_best_tab_for_request(
        gs.browser_connections,
        gs.browser_connections_lock,
        gs.tab_request_counts,
    )


async def release_tab_request(tab_id: str):
    """释放标签页的请求计数"""
    await _release_tab_request(tab_id, gs.tab_request_counts, gs.tab_request_counts_lock)


async def reassign_pending_requests(disconnected_tab_id: str, browser_id: str = None):
    """当标签页断开时，将其待处理请求重新分配给其他活跃标签页"""
    await _reassign_pending_requests(
        disconnected_tab_id,
        gs.browser_connections,
        gs.browser_connections_lock,
        gs.response_channels,
        gs.request_metadata,
        gs.tab_request_counts,
        CONFIG,
        convert_openai_to_lmarena_payload,
    )