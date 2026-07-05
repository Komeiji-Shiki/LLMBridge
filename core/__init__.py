"""
Core模块 - 核心功能组件

包含：
- config_loader: 配置加载和管理
- load_balancer: 负载均衡
- db_stats: SQLite数据库统计
- app_state: 应用状态管理
- errors: 统一错误处理
- constants: 全局常量和默认配置
"""

from .config_loader import (
    CONFIG,
    MODEL_NAME_TO_ID_MAP,
    MODEL_ENDPOINT_MAP,
    DEFAULT_MODEL_ID,
    CONFIG_FILE_MTIMES,
    CONFIG_LOCK,
    MODEL_ROUND_ROBIN_INDEX,
    MODEL_ROUND_ROBIN_LOCK,
    load_config,
    load_model_map,
    load_model_endpoint_map,
    save_config,
    _parse_jsonc
)

from .load_balancer import (
    select_best_tab,
    release_tab,
    reassign_tab_requests
)

from .db_stats import stats_db

from .app_state import (
    AppState,
    get_app_state,
    reset_app_state,
    ConnectionState,
    RequestState,
    ImageState,
    AdminState,
    ServerState
)

from .errors import (
    APIError,
    BadRequestError,
    AuthenticationError,
    PermissionError,
    NotFoundError,
    RateLimitError,
    ServiceUnavailableError,
    GatewayTimeoutError,
    InternalServerError,
    BridgeError,
    AttachmentError,
    AttachmentTooLargeError,
    VerificationRequiredError,
    BrowserNotConnectedError,
    ModelNotFoundError,
    InvalidSessionError,
    handle_api_error,
    format_upstream_error
)

from .constants import (
    TimeoutDefaults,
    CacheDefaults,
    ConnectionDefaults,
    RetryDefaults,
    MemoryDefaults,
    ServerDefaults,
    StreamPatterns,
    ImageDefaults,
    LogDefaults,
    ConfigPaths,
    DEFAULT_TOKENIZER_CONFIG,
    MODEL_TOKEN_MULTIPLIERS,
    get_default_config
)

__all__ = [
    # config_loader
    'CONFIG',
    'MODEL_NAME_TO_ID_MAP',
    'MODEL_ENDPOINT_MAP',
    'DEFAULT_MODEL_ID',
    'CONFIG_FILE_MTIMES',
    'CONFIG_LOCK',
    'MODEL_ROUND_ROBIN_INDEX',
    'MODEL_ROUND_ROBIN_LOCK',
    'load_config',
    'load_model_map',
    'load_model_endpoint_map',
    'save_config',
    '_parse_jsonc',
    
    # load_balancer
    'select_best_tab',
    'release_tab',
    'reassign_tab_requests',
    
    # db_stats
    'stats_db',
    
    # app_state
    'AppState',
    'get_app_state',
    'reset_app_state',
    'ConnectionState',
    'RequestState',
    'ImageState',
    'AdminState',
    'ServerState',
    
    # errors
    'APIError',
    'BadRequestError',
    'AuthenticationError',
    'PermissionError',
    'NotFoundError',
    'RateLimitError',
    'ServiceUnavailableError',
    'GatewayTimeoutError',
    'InternalServerError',
    'BridgeError',
    'AttachmentError',
    'AttachmentTooLargeError',
    'VerificationRequiredError',
    'BrowserNotConnectedError',
    'ModelNotFoundError',
    'InvalidSessionError',
    'handle_api_error',
    'format_upstream_error',
    
    # constants
    'TimeoutDefaults',
    'CacheDefaults',
    'ConnectionDefaults',
    'RetryDefaults',
    'MemoryDefaults',
    'ServerDefaults',
    'StreamPatterns',
    'ImageDefaults',
    'LogDefaults',
    'ConfigPaths',
    'DEFAULT_TOKENIZER_CONFIG',
    'MODEL_TOKEN_MULTIPLIERS',
    'get_default_config',
]