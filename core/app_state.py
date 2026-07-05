"""
应用状态管理模块
封装全局变量为状态类，提高代码可维护性
"""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Optional, Dict, Any

from cachetools import TTLCache
from fastapi import WebSocket

logger = logging.getLogger(__name__)


@dataclass
class ConnectionState:
    """连接状态管理"""
    browser_connections: Dict[str, WebSocket] = field(default_factory=dict)
    browser_connections_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tab_connection_times: Dict[str, float] = field(default_factory=dict)
    tab_request_counts: Dict[str, int] = field(default_factory=dict)
    tab_request_counts_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    
    # 兼容性包装
    browser_ws_ref: Dict[str, Any] = field(default_factory=lambda: {'ws': None})


@dataclass
class RequestState:
    """请求状态管理"""
    response_channels: Dict[str, asyncio.Queue] = field(default_factory=dict)
    request_metadata: Dict[str, dict] = field(default_factory=dict)
    pending_requests_queue: asyncio.Queue = field(default_factory=asyncio.Queue)


@dataclass  
class ImageState:
    """图片处理状态"""
    IMAGE_SAVE_DIR: Path = field(default_factory=lambda: Path("./downloaded_images"))
    downloaded_image_urls: deque = field(default_factory=lambda: deque(maxlen=5000))
    downloaded_urls_set: set = field(default_factory=set)
    
    # 缓存 - 使用 TTLCache 自动管理过期和大小限制
    IMAGE_BASE64_CACHE: Any = field(default_factory=lambda: TTLCache(maxsize=50, ttl=600))
    IMAGE_CACHE_MAX_SIZE: int = 50
    IMAGE_CACHE_TTL: int = 600
    PROCESSED_IMAGE_CACHE: Any = field(default_factory=lambda: TTLCache(maxsize=30, ttl=600))
    
    # 文件床
    FILEBED_URL_CACHE: Any = field(default_factory=lambda: TTLCache(maxsize=50, ttl=300))
    FILEBED_URL_CACHE_TTL: int = 300
    FILEBED_URL_CACHE_MAX_SIZE: int = 500
    DISABLED_ENDPOINTS: Dict[str, Any] = field(default_factory=dict)
    ROUND_ROBIN_INDEX: int = 0
    FILEBED_RECOVERY_TIME: int = 300
    
    def __post_init__(self):
        self.IMAGE_SAVE_DIR.mkdir(exist_ok=True)


@dataclass
class AdminState:
    """管理面板状态"""
    ADMIN_CAPTURED_IDS: Dict[str, Any] = field(default_factory=lambda: {
        'session_id': None,
        'timestamp': None,
        'mode': None,
        'battle_target': None
    })
    ADMIN_CAPTURED_IDS_LOCK: Lock = field(default_factory=Lock)


@dataclass
class ServerState:
    """服务器状态"""
    main_event_loop: Optional[asyncio.AbstractEventLoop] = None
    aiohttp_session: Any = None
    direct_api_service: Any = None
    
    # 验证状态
    IS_REFRESHING_FOR_VERIFICATION: bool = False
    VERIFICATION_COOLDOWN_UNTIL: Optional[float] = None
    
    # 活动时间
    last_activity_time_ref: Dict[str, Any] = field(default_factory=lambda: {'time': None})
    
    # 下载控制
    DOWNLOAD_SEMAPHORE: Optional[asyncio.Semaphore] = None
    MAX_CONCURRENT_DOWNLOADS: int = 50
    
    def update_last_activity(self):
        """更新最后活动时间"""
        self.last_activity_time_ref['time'] = datetime.now()


class AppState:
    """
    应用全局状态管理器
    
    将分散的全局变量封装到一个统一的状态对象中，
    提高代码可维护性和可测试性。
    """
    
    def __init__(self):
        self.connection = ConnectionState()
        self.request = RequestState()
        self.image = ImageState()
        self.admin = AdminState()
        self.server = ServerState()
        
        logger.info("[APP_STATE] 应用状态管理器已初始化")
    
    # ========== 便捷访问器（保持向后兼容） ==========
    
    @property
    def browser_connections(self) -> Dict[str, WebSocket]:
        return self.connection.browser_connections
    
    @property
    def browser_connections_lock(self) -> asyncio.Lock:
        return self.connection.browser_connections_lock
    
    @property
    def response_channels(self) -> Dict[str, asyncio.Queue]:
        return self.request.response_channels
    
    @property
    def request_metadata(self) -> Dict[str, dict]:
        return self.request.request_metadata
    
    @property
    def pending_requests_queue(self) -> asyncio.Queue:
        return self.request.pending_requests_queue
    
    @property
    def browser_ws(self) -> Optional[WebSocket]:
        return self.connection.browser_ws_ref.get('ws')
    
    @browser_ws.setter
    def browser_ws(self, value: Optional[WebSocket]):
        self.connection.browser_ws_ref['ws'] = value
    
    @property
    def tab_request_counts(self) -> Dict[str, int]:
        return self.connection.tab_request_counts
    
    @property
    def IMAGE_BASE64_CACHE(self) -> TTLCache:
        return self.image.IMAGE_BASE64_CACHE
    
    def update_activity(self):
        """更新最后活动时间"""
        self.server.update_last_activity()
    
    def get_last_activity_time(self) -> Optional[datetime]:
        """获取最后活动时间"""
        return self.server.last_activity_time_ref.get('time')


# 全局单例实例
_app_state: Optional[AppState] = None


def get_app_state() -> AppState:
    """
    获取应用状态单例
    
    Returns:
        AppState实例
    """
    global _app_state
    if _app_state is None:
        _app_state = AppState()
    return _app_state


def reset_app_state():
    """
    重置应用状态（主要用于测试）
    """
    global _app_state
    _app_state = None