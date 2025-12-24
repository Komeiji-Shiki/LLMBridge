import asyncio
import time
from asyncio import Semaphore
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Optional

from fastapi import WebSocket

# --- 基础配置 ---
MODEL_ENDPOINT_MAP_PATH = "model_endpoint_map.json"

# --- 全局状态与配置 ---
# 支持多标签页并发连接
browser_connections: dict[str, WebSocket] = {}
browser_connections_lock = asyncio.Lock()  # 保护并发访问

# 跟踪标签页连接时间
tab_connection_times: dict[str, float] = {}

# 兼容性：保留browser_ws用于向后兼容（指向第一个连接）
browser_ws: WebSocket | None = None

# response_channels 用于存储每个 API 请求的响应队列。
response_channels: dict[str, asyncio.Queue] = {}

# 请求元数据存储（用于WebSocket重连后恢复请求）
request_metadata: dict[str, dict] = {}

# 跟踪每个标签页的活跃请求数
tab_request_counts: dict[str, int] = {}
tab_request_counts_lock = asyncio.Lock()

# 活动时间与线程/loop记录
last_activity_time = None
idle_monitor_thread = None
main_event_loop = None

# 人机验证状态
IS_REFRESHING_FOR_VERIFICATION = False
VERIFICATION_COOLDOWN_UNTIL = None

# 自动重试队列
pending_requests_queue: asyncio.Queue = asyncio.Queue()

# WebSocket连接锁
ws_lock = asyncio.Lock()

# 全局aiohttp会话（在lifespan里初始化）
aiohttp_session = None

# Direct API服务实例（在lifespan里初始化）
direct_api_service = None

# --- 图片自动下载配置 ---
IMAGE_SAVE_DIR = Path("./downloaded_images")
IMAGE_SAVE_DIR.mkdir(exist_ok=True)

downloaded_image_urls = deque(maxlen=5000)
downloaded_urls_set = set()

# 用于在运行时临时禁用失败的图床端点
DISABLED_ENDPOINTS: dict[str, float] = {}

# 轮询策略的全局索引（图床等场景）
ROUND_ROBIN_INDEX = 0

# 图床恢复时间（秒）
FILEBED_RECOVERY_TIME = 300  # 5分钟

# Admin面板ID捕获
ADMIN_CAPTURED_IDS = {
    "session_id": None,
    "timestamp": None,
    "mode": None,
    "battle_target": None,
}
ADMIN_CAPTURED_IDS_LOCK = Lock()

# 图片Base64缓存
IMAGE_BASE64_CACHE: dict[str, tuple[str, float]] = {}
IMAGE_CACHE_MAX_SIZE = 1000
IMAGE_CACHE_TTL = 3600

# 图床URL缓存
FILEBED_URL_CACHE: dict[str, tuple[str, float]] = {}
FILEBED_URL_CACHE_TTL = 300
FILEBED_URL_CACHE_MAX_SIZE = 500

# 并发下载控制（在lifespan里初始化）
DOWNLOAD_SEMAPHORE: Optional[Semaphore] = None
MAX_CONCURRENT_DOWNLOADS = 50

# 输入图片处理缓存
PROCESSED_IMAGE_CACHE: dict[str, tuple[bytes, float]] = {}


def now_ts() -> float:
    return time.time()