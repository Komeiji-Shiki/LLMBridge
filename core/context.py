"""
依赖注入上下文模块
统一管理服务依赖，简化函数参数传递
"""

import asyncio
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Callable, List, TYPE_CHECKING
from datetime import datetime

from cachetools import TTLCache

if TYPE_CHECKING:
    from fastapi import WebSocket
    import aiohttp


@dataclass
class ServiceContext:
    """服务依赖上下文"""
    monitoring_service: Any = None
    direct_api_service: Any = None
    aiohttp_session: Optional['aiohttp.ClientSession'] = None


@dataclass
class StateContext:
    """状态上下文 - 管理运行时状态"""
    browser_connections: Dict[str, 'WebSocket'] = field(default_factory=dict)
    browser_connections_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tab_connection_times: Dict[str, float] = field(default_factory=dict)
    tab_request_counts: Dict[str, int] = field(default_factory=dict)
    tab_request_counts_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    response_channels: Dict[str, asyncio.Queue] = field(default_factory=dict)
    request_metadata: Dict[str, dict] = field(default_factory=dict)
    pending_requests_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    
    # 验证状态
    IS_REFRESHING_FOR_VERIFICATION: bool = False
    VERIFICATION_COOLDOWN_UNTIL: Optional[float] = None
    
    # 活动时间追踪
    last_activity_time: Optional[datetime] = None
    
    # 兼容性包装
    browser_ws_ref: Dict[str, Any] = field(default_factory=lambda: {'ws': None})
    
    @property
    def browser_ws(self) -> Optional['WebSocket']:
        return self.browser_ws_ref.get('ws')
    
    @browser_ws.setter
    def browser_ws(self, value: Optional['WebSocket']):
        self.browser_ws_ref['ws'] = value
    
    def update_activity(self):
        """更新最后活动时间"""
        self.last_activity_time = datetime.now()


@dataclass
class ConfigContext:
    """配置上下文 - 管理配置数据"""
    CONFIG: Dict[str, Any] = field(default_factory=dict)
    MODEL_ENDPOINT_MAP: Dict[str, Any] = field(default_factory=dict)
    MODEL_NAME_TO_ID_MAP: Dict[str, Any] = field(default_factory=dict)
    MODEL_ROUND_ROBIN_INDEX: Dict[str, int] = field(default_factory=dict)
    MODEL_ROUND_ROBIN_LOCK: Any = None
    
    def __post_init__(self):
        if self.MODEL_ROUND_ROBIN_LOCK is None:
            from threading import Lock
            self.MODEL_ROUND_ROBIN_LOCK = Lock()


@dataclass
class CacheContext:
    """缓存上下文 - 管理各类缓存（使用 TTLCache 自动管理过期和大小）"""
    IMAGE_BASE64_CACHE: Any = field(default_factory=lambda: TTLCache(maxsize=1000, ttl=3600))
    IMAGE_CACHE_MAX_SIZE: int = 1000
    IMAGE_CACHE_TTL: int = 3600
    FILEBED_URL_CACHE: Any = field(default_factory=lambda: TTLCache(maxsize=500, ttl=300))
    FILEBED_URL_CACHE_TTL: int = 300
    FILEBED_URL_CACHE_MAX_SIZE: int = 500
    PROCESSED_IMAGE_CACHE: Any = field(default_factory=lambda: TTLCache(maxsize=200, ttl=3600))


@dataclass
class FunctionContext:
    """函数引用上下文 - 避免循环导入"""
    # 负载均衡函数
    select_best_tab_for_request: Optional[Callable] = None
    release_tab_request: Optional[Callable] = None
    
    # 消息转换函数
    convert_openai_to_lmarena_payload: Optional[Callable] = None
    
    # 流处理函数
    process_lmarena_stream: Optional[Callable] = None
    stream_generator: Optional[Callable] = None
    non_stream_response: Optional[Callable] = None
    
    # 格式化函数
    format_openai_chunk: Optional[Callable] = None
    format_openai_finish_chunk: Optional[Callable] = None
    format_openai_error_chunk: Optional[Callable] = None
    format_openai_non_stream_response: Optional[Callable] = None
    
    # Token估算函数
    estimate_message_tokens: Optional[Callable] = None
    estimate_tokens: Optional[Callable] = None
    
    # 图片处理函数
    process_image_data: Optional[Callable] = None
    save_downloaded_image_async: Optional[Callable] = None
    download_image_data_with_retry: Optional[Callable] = None


@dataclass
class RequestContext:
    """
    请求上下文 - 聚合所有依赖
    
    这是传递给 chat_completions 等函数的主要上下文对象，
    将原来40+个参数减少为一个对象。
    """
    services: ServiceContext = field(default_factory=ServiceContext)
    state: StateContext = field(default_factory=StateContext)
    config: ConfigContext = field(default_factory=ConfigContext)
    cache: CacheContext = field(default_factory=CacheContext)
    functions: FunctionContext = field(default_factory=FunctionContext)
    
    # 便捷访问器
    @property
    def CONFIG(self) -> Dict[str, Any]:
        return self.config.CONFIG
    
    @property
    def MODEL_ENDPOINT_MAP(self) -> Dict[str, Any]:
        return self.config.MODEL_ENDPOINT_MAP
    
    @property
    def monitoring_service(self):
        return self.services.monitoring_service
    
    @property
    def direct_api_service(self):
        return self.services.direct_api_service
    
    @property
    def browser_connections(self) -> Dict[str, Any]:
        return self.state.browser_connections
    
    @property
    def response_channels(self) -> Dict[str, asyncio.Queue]:
        return self.state.response_channels
    
    @property
    def request_metadata(self) -> Dict[str, dict]:
        return self.state.request_metadata


# 全局请求上下文单例
_request_context: Optional[RequestContext] = None


def get_request_context() -> RequestContext:
    """
    获取请求上下文单例
    
    Returns:
        RequestContext 实例
    """
    global _request_context
    if _request_context is None:
        _request_context = RequestContext()
    return _request_context


def init_request_context(
    config: Dict[str, Any],
    model_endpoint_map: Dict[str, Any],
    model_name_to_id_map: Dict[str, Any],
    monitoring_service: Any,
    direct_api_service: Any,
    aiohttp_session: Any
) -> RequestContext:
    """
    初始化请求上下文（应在应用启动时调用）
    
    Args:
        config: 全局配置
        model_endpoint_map: 模型端点映射
        model_name_to_id_map: 模型名称到ID映射
        monitoring_service: 监控服务
        direct_api_service: Direct API服务
        aiohttp_session: aiohttp会话
    
    Returns:
        初始化后的 RequestContext
    """
    global _request_context
    
    ctx = get_request_context()
    
    # 初始化配置
    ctx.config.CONFIG.clear()
    ctx.config.CONFIG.update(config)
    ctx.config.MODEL_ENDPOINT_MAP.clear()
    ctx.config.MODEL_ENDPOINT_MAP.update(model_endpoint_map)
    ctx.config.MODEL_NAME_TO_ID_MAP.clear()
    ctx.config.MODEL_NAME_TO_ID_MAP.update(model_name_to_id_map)
    
    # 初始化服务
    ctx.services.monitoring_service = monitoring_service
    ctx.services.direct_api_service = direct_api_service
    ctx.services.aiohttp_session = aiohttp_session
    
    # 初始化缓存配置
    ctx.cache.IMAGE_CACHE_MAX_SIZE = config.get("memory_management", {}).get("image_cache_max_size", 1000)
    ctx.cache.IMAGE_CACHE_TTL = config.get("memory_management", {}).get("image_cache_ttl_seconds", 3600)
    
    return ctx


def reset_request_context():
    """重置请求上下文（主要用于测试）"""
    global _request_context
    _request_context = None