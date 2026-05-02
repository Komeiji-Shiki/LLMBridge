"""
全局常量和默认配置
将硬编码值集中管理
"""

from typing import Dict, Any

# ==================== 超时配置 ====================

class TimeoutDefaults:
    """超时相关默认值（秒）"""
    # 流响应超时
    STREAM_RESPONSE_TIMEOUT = 2000
    
    # WebSocket发送超时
    WEBSOCKET_SEND_TIMEOUT = 10.0
    
    # 负载均衡锁超时
    LOAD_BALANCER_LOCK_TIMEOUT = 5.0
    
    # 重试等待超时
    RETRY_TIMEOUT = 120
    
    # 下载超时
    DOWNLOAD_CONNECT_TIMEOUT = 10
    DOWNLOAD_READ_TIMEOUT = 20
    DOWNLOAD_TOTAL_TIMEOUT = 30
    
    # API调用超时
    API_CALL_TIMEOUT = 1200  # 20分钟
    
    # 首个数据块等待超时
    FIRST_CHUNK_TIMEOUT = 600  # 10分钟
    
    # 请求元数据超时（分钟）
    METADATA_TIMEOUT_MINUTES = 60
    
    # 活跃请求超时（分钟）
    ACTIVE_REQUEST_TIMEOUT_MINUTES = 60
    
    # Tokenizer空闲超时（秒）
    TOKENIZER_IDLE_TIMEOUT = 120  # 2分钟空闲即卸载，tokenizer占内存大


class CacheDefaults:
    """缓存相关默认值"""
    # 图片缓存
    IMAGE_CACHE_MAX_SIZE = 50   # base64图片很占内存，大幅降低
    IMAGE_CACHE_TTL = 600       # 10分钟
    
    # 文件床URL缓存
    FILEBED_URL_CACHE_TTL = 300  # 5分钟
    FILEBED_URL_CACHE_MAX_SIZE = 50
    
    # 已处理图片缓存
    PROCESSED_IMAGE_CACHE_MAX_SIZE = 30
    PROCESSED_IMAGE_CACHE_TTL = 600
    
    # Tiktoken缓存
    TIKTOKEN_CACHE_MAX_SIZE = 5
    CUSTOM_TOKENIZERS_MAX_SIZE = 5
    TIKTOKEN_MODEL_CACHE_MAX_SIZE = 5
    
    # 下载历史记录
    DOWNLOADED_URLS_MAX_SIZE = 1000
    URL_HISTORY_MAX = 1000
    URL_HISTORY_KEEP = 200


class ConnectionDefaults:
    """连接相关默认值"""
    # aiohttp连接池
    POOL_TOTAL_LIMIT = 200
    POOL_PER_HOST_LIMIT = 50
    DNS_CACHE_TTL = 300
    KEEPALIVE_TIMEOUT = 30
    
    # 并发控制
    MAX_CONCURRENT_DOWNLOADS = 50
    
    # 最大请求转移次数
    MAX_REQUEST_TRANSFERS = 3


class RetryDefaults:
    """重试相关默认值"""
    # 空响应重试
    EMPTY_RESPONSE_MAX_RETRIES = 5
    EMPTY_RESPONSE_BASE_DELAY_MS = 100
    EMPTY_RESPONSE_MAX_DELAY_MS = 3000
    
    # 下载重试
    DOWNLOAD_MAX_RETRIES = 3
    
    # 文件床恢复时间
    FILEBED_RECOVERY_TIME = 300


class MemoryDefaults:
    """内存管理默认值（MB）"""
    # GC触发阈值
    GC_THRESHOLD_MB = 150  # 更早触发回收（内存大户场景）
    
    # 缓存清理保留数量
    IMAGE_CACHE_KEEP_SIZE = 50


class ServerDefaults:
    """服务器默认值"""
    # 默认端口
    DEFAULT_PORT = 5102
    
    # 人机验证冷却时间（秒）
    VERIFICATION_COOLDOWN_SECONDS = 25
    
    # 流结束后等待延迟（秒）
    STREAM_END_WAIT_DELAY = 1.0
    
    # 后台任务检查间隔（秒）
    CONFIG_MONITOR_INTERVAL = 30
    MEMORY_MONITOR_INTERVAL = 60
    STALE_CLEANER_INTERVAL = 60


# ==================== 正则表达式模式 ====================

class StreamPatterns:
    """流解析正则表达式"""
    # 文本内容匹配
    TEXT_PATTERN = r'[ab]0:"((?:\\.|[^"\\])*)"'
    
    # 思维链内容匹配
    REASONING_PATTERN = r'ag:"((?:\\.|[^"\\])*)"'
    
    # 图片URL匹配
    IMAGE_PATTERN = r'[ab]2:(\[.*?\])'
    
    # 结束原因匹配
    FINISH_PATTERN = r'[ab]d:(\{.*?"finishReason".*?\})'
    
    # 错误匹配
    ERROR_PATTERN = r'(\{\s*"error".*?\})'
    
    # Cloudflare验证检测
    CLOUDFLARE_PATTERNS = [
        r'<title>Just a moment...</title>',
        r'Enable JavaScript and cookies to continue'
    ]
    
    # Markdown图片匹配
    MARKDOWN_IMAGE_PATTERN = r'!\[([^\]]*)\]\(([^)]+)\)'


# ==================== Tokenizer配置 ====================

DEFAULT_TOKENIZER_CONFIG: Dict[str, str] = {
    "claude": "anthropic",
    "claude-3": "anthropic",
    "claude-3-opus": "anthropic",
    "claude-3-sonnet": "anthropic",
    "claude-3-haiku": "anthropic",
    "claude-3.5-sonnet": "anthropic",
    "claude-4": "anthropic",
    "gemini": "google",
    "gemini-pro": "google",
    "gemini-ultra": "google",
    "gemini-1.5": "google",
    "gemini-2": "google",
    "gemini-2.5": "google",
    "gemini-3": "google",
    "gpt-4": "tiktoken",
    "gpt-3.5": "tiktoken",
    "gpt-4-turbo": "tiktoken",
    "gpt-4o": "tiktoken",
    "chatgpt": "tiktoken",
    "deepseek": "deepseek",
    "deepseek-chat": "deepseek",
    "deepseek-coder": "deepseek",
    "deepseek-v3": "deepseek"
}

# 模型token倍数校准系数（相对于GPT-4的cl100k_base）
MODEL_TOKEN_MULTIPLIERS: Dict[str, float] = {
    # Claude系列：约为GPT-4的1.0倍
    'claude': 1.0,
    'claude-3': 1.0,
    'claude-3-opus': 1.0,
    'claude-3-sonnet': 1.0,
    'claude-3-haiku': 1.0,
    'claude-3.5-sonnet': 1.0,
    'claude-4': 1.0,
    
    # Gemini系列：约为Claude的0.625倍
    'gemini': 0.625,
    'gemini-pro': 0.625,
    'gemini-ultra': 0.625,
    'gemini-1.5': 0.625,
    'gemini-2': 0.625,
    
    # GPT系列：基准值
    'gpt-4': 1.0,
    'gpt-3.5': 1.0,
    'gpt-4-turbo': 1.0,
    'chatgpt': 1.0,
    
    # DeepSeek系列
    'deepseek': 1.0,
}


# ==================== 图片处理配置 ====================

class ImageDefaults:
    """图片处理默认值"""
    # 最大尺寸
    MAX_WIDTH = 4096
    MAX_HEIGHT = 4096
    
    # 压缩质量
    WEBP_QUALITY = 70
    JPEG_QUALITY = 70
    
    # 默认格式
    DEFAULT_FORMAT = "webp"
    FALLBACK_FORMAT = "jpeg"


# ==================== 日志相关 ====================

class LogDefaults:
    """日志相关默认值"""
    # URL显示长度
    URL_DISPLAY_LENGTH = 200
    
    # 响应内容截断长度
    RESPONSE_CONTENT_MAX_LENGTH = 2000
    REASONING_CONTENT_MAX_LENGTH = 5000


# ==================== 配置文件路径 ====================

class ConfigPaths:
    """配置文件路径"""
    MAIN_CONFIG = 'config.jsonc'
    MODEL_ENDPOINT_MAP = 'model_endpoint_map.json'
    MODELS_JSON = 'models.json'
    CUSTOM_TOKENIZERS = 'custom_tokenizers.json'
    
    # 日志目录
    LOGS_DIR = './logs'
    REQUEST_LOG_FILE = 'requests.jsonl'
    ERROR_LOG_FILE = 'errors.jsonl'
    STATS_FILE = 'stats.json'
    DATABASE_FILE = 'requests.db'
    
    # 图片保存目录
    IMAGE_SAVE_DIR = './downloaded_images'


# ==================== 工具函数 ====================

def get_default_config() -> Dict[str, Any]:
    """获取完整的默认配置"""
    return {
        "version": "1.5.1",
        "server_port": ServerDefaults.DEFAULT_PORT,
        
        # 超时配置
        "stream_response_timeout_seconds": TimeoutDefaults.STREAM_RESPONSE_TIMEOUT,
        "retry_timeout_seconds": TimeoutDefaults.RETRY_TIMEOUT,
        "metadata_timeout_minutes": TimeoutDefaults.METADATA_TIMEOUT_MINUTES,
        
        # 重试配置
        "enable_auto_retry": True,
        "empty_response_retry": {
            "enabled": True,
            "max_retries": RetryDefaults.EMPTY_RESPONSE_MAX_RETRIES,
            "base_delay_ms": RetryDefaults.EMPTY_RESPONSE_BASE_DELAY_MS,
            "max_delay_ms": RetryDefaults.EMPTY_RESPONSE_MAX_DELAY_MS,
            "show_retry_info_to_client": True
        },
        
        # 连接池配置
        "max_concurrent_downloads": ConnectionDefaults.MAX_CONCURRENT_DOWNLOADS,
        "connection_pool": {
            "total_limit": ConnectionDefaults.POOL_TOTAL_LIMIT,
            "per_host_limit": ConnectionDefaults.POOL_PER_HOST_LIMIT,
            "keepalive_timeout": ConnectionDefaults.KEEPALIVE_TIMEOUT,
            "dns_cache_ttl": ConnectionDefaults.DNS_CACHE_TTL
        },
        
        # 下载配置
        "download_timeout": {
            "connect": TimeoutDefaults.DOWNLOAD_CONNECT_TIMEOUT,
            "sock_read": TimeoutDefaults.DOWNLOAD_READ_TIMEOUT,
            "total": TimeoutDefaults.DOWNLOAD_TOTAL_TIMEOUT,
            "max_retries": RetryDefaults.DOWNLOAD_MAX_RETRIES
        },
        
        # 内存管理
        "memory_management": {
            "gc_threshold_mb": MemoryDefaults.GC_THRESHOLD_MB,
            "image_cache_max_size": CacheDefaults.IMAGE_CACHE_MAX_SIZE,
            "image_cache_ttl_seconds": CacheDefaults.IMAGE_CACHE_TTL
        },
        
        # 图片处理
        "image_optimization": {
            "enabled": False,
            "max_width": ImageDefaults.MAX_WIDTH,
            "max_height": ImageDefaults.MAX_HEIGHT,
            "webp_quality": ImageDefaults.WEBP_QUALITY,
            "jpeg_quality": ImageDefaults.JPEG_QUALITY
        },
        
        # 其他
        "use_default_ids_if_mapping_not_found": True,
        "max_request_transfers": ConnectionDefaults.MAX_REQUEST_TRANSFERS,
        "debug_stream_timing": False,
        "debug_show_full_urls": False,
        "url_display_length": LogDefaults.URL_DISPLAY_LENGTH
    }