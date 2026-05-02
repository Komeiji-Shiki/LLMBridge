# api_server.py - 精简重构版本
# 原4000+行代码已拆分到多个模块

import asyncio
import json
import logging
import os
import queue
import sys
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path
from threading import Lock
from typing import Optional

import aiohttp
import uvicorn
from fastapi import FastAPI, WebSocket, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, Response, HTMLResponse
from fastapi.staticfiles import StaticFiles

# 内部模块导入
from modules.file_uploader import upload_to_file_bed
from modules.monitoring import monitoring_service, MonitorConfig
from modules.image_processor import optimize_image, image_to_base64, get_mime_type_from_format, decode_base64_image
from modules.token_counter import (
    estimate_message_tokens, estimate_tokens, get_token_counter_info,
    get_all_tokenizers_status, calculate_tokens_for_text, compare_tokenizers,
    install_tokenizer_package, add_custom_tokenizer, delete_custom_tokenizer,
    list_custom_tokenizers
)

# Core模块
from core.config_loader import (
    CONFIG, MODEL_NAME_TO_ID_MAP, MODEL_ENDPOINT_MAP, DEFAULT_MODEL_ID,
    CONFIG_FILE_MTIMES, CONFIG_LOCK, MODEL_ROUND_ROBIN_INDEX, MODEL_ROUND_ROBIN_LOCK,
    load_config, load_model_map, load_model_endpoint_map, save_config, _parse_jsonc
)
from core.api_key_manager import api_key_manager
from core.load_balancer import (
    select_best_tab_for_request as _select_best_tab_for_request,
    release_tab_request as _release_tab_request,
    reassign_pending_requests as _reassign_pending_requests
)
from core.db_stats import stats_db
from core.app_state import get_app_state, AppState
from core.constants import ConnectionDefaults, CacheDefaults, TimeoutDefaults

# Services
from services.message_converter import convert_openai_to_lmarena_payload
from services.stream_processor import _process_lmarena_stream, stream_generator, non_stream_response
from services.direct_api_service import DirectAPIService
from services.image_service import (
    calculate_image_hash, save_image_data, save_downloaded_image_async,
    download_image_data_with_retry as _download_image_data_with_retry,
    process_image_data
)

# Utils
from utils.api_helpers import (
    format_openai_chunk, format_openai_finish_chunk,
    format_openai_error_chunk, format_openai_non_stream_response
)

# Routes
from routes import websocket_routes, internal_routes, monitor_routes, admin_routes, api_routes

# Background tasks
from background_tasks import monitors, request_processor

# 基础配置
MODEL_ENDPOINT_MAP_PATH = 'model_endpoint_map.json'
_LOG_QUEUE_LISTENER: Optional[QueueListener] = None

def _configure_async_logging():
    """将控制台日志输出移到后台线程，避免主事件循环被同步日志IO卡住"""
    global _LOG_QUEUE_LISTENER

    root_logger = logging.getLogger()
    if getattr(root_logger, "_lmbridge_async_logging_configured", False):
        return

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    existing_handlers = list(root_logger.handlers)
    log_queue: queue.SimpleQueue = queue.SimpleQueue()
    queue_handler = QueueHandler(log_queue)

    root_logger.handlers = [queue_handler]
    _LOG_QUEUE_LISTENER = QueueListener(log_queue, *existing_handlers, respect_handler_level=True)
    _LOG_QUEUE_LISTENER.start()
    root_logger._lmbridge_async_logging_configured = True

_configure_async_logging()
logger = logging.getLogger(__name__)

# 日志过滤器
class EndpointFilter(logging.Filter):
    """过滤 uvicorn access 日志：抑制监控轮询和恶意扫描的噪音"""
    
    # 恶意扫描路径特征（不区分大小写）
    _SCAN_PATTERNS = (
        '/.env', '/.git/', '/php', '/info.php', '/test.php',
        '/.dockerenv', '/.npmrc', '/.kube/', '/.htpasswd', '/.netrc',
        '/adminer', '/wp-', '/wordpress', '/xmlrpc', '/config.json',
        '/actuator', '/.aws/', '/.ssh/', '/.config',
        '=phpinfo()',   # /?=phpinfo()  /index.php?=phpinfo()
        '%2567%2569%2574',  # URL-encoded .git
    )
    
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        # 抑制监控面板的频繁轮询日志
        if "GET /api/monitor/" in message or "GET /monitor " in message:
            return False
        # 抑制恶意扫描 404 噪音
        msg_lower = message.lower()
        if " 404 " in message and any(p in msg_lower for p in self._SCAN_PATTERNS):
            return False
        return True

logging.getLogger("uvicorn.access").addFilter(EndpointFilter())

# ==================== 全局状态（使用 AppState 管理） ====================
# 所有状态通过 AppState 单例集中管理
# 保留变量别名以维持向后兼容

# 初始化 AppState（单例模式）
_app_state: Optional[AppState] = None

def _get_state() -> AppState:
    """获取应用状态单例（延迟初始化）"""
    global _app_state
    if _app_state is None:
        _app_state = get_app_state()
    return _app_state

# 预初始化 AppState
_app_state = get_app_state()

# ========== 连接状态（通过 AppState.connection 管理）==========
browser_connections = _app_state.connection.browser_connections
browser_connections_lock = _app_state.connection.browser_connections_lock
tab_connection_times = _app_state.connection.tab_connection_times
browser_ws_ref = _app_state.connection.browser_ws_ref
tab_request_counts = _app_state.connection.tab_request_counts
tab_request_counts_lock = _app_state.connection.tab_request_counts_lock

# ========== 请求状态（通过 AppState.request 管理）==========
response_channels = _app_state.request.response_channels
request_metadata = _app_state.request.request_metadata
pending_requests_queue = _app_state.request.pending_requests_queue

# ========== 服务器状态（通过 AppState.server 管理）==========
last_activity_time_ref = _app_state.server.last_activity_time_ref
main_event_loop = _app_state.server.main_event_loop
IS_REFRESHING_FOR_VERIFICATION = _app_state.server.IS_REFRESHING_FOR_VERIFICATION
VERIFICATION_COOLDOWN_UNTIL = _app_state.server.VERIFICATION_COOLDOWN_UNTIL
aiohttp_session = _app_state.server.aiohttp_session
direct_api_service = _app_state.server.direct_api_service
DOWNLOAD_SEMAPHORE = _app_state.server.DOWNLOAD_SEMAPHORE
MAX_CONCURRENT_DOWNLOADS = _app_state.server.MAX_CONCURRENT_DOWNLOADS

# ========== 图片状态（通过 AppState.image 管理）==========
IMAGE_SAVE_DIR = _app_state.image.IMAGE_SAVE_DIR
downloaded_image_urls = _app_state.image.downloaded_image_urls
downloaded_urls_set = _app_state.image.downloaded_urls_set
DISABLED_ENDPOINTS = _app_state.image.DISABLED_ENDPOINTS
ROUND_ROBIN_INDEX = _app_state.image.ROUND_ROBIN_INDEX
FILEBED_RECOVERY_TIME = _app_state.image.FILEBED_RECOVERY_TIME
IMAGE_BASE64_CACHE = _app_state.image.IMAGE_BASE64_CACHE
IMAGE_CACHE_MAX_SIZE = _app_state.image.IMAGE_CACHE_MAX_SIZE
IMAGE_CACHE_TTL = _app_state.image.IMAGE_CACHE_TTL
FILEBED_URL_CACHE = _app_state.image.FILEBED_URL_CACHE
FILEBED_URL_CACHE_TTL = _app_state.image.FILEBED_URL_CACHE_TTL
FILEBED_URL_CACHE_MAX_SIZE = _app_state.image.FILEBED_URL_CACHE_MAX_SIZE
PROCESSED_IMAGE_CACHE = _app_state.image.PROCESSED_IMAGE_CACHE

# ========== 管理面板状态（通过 AppState.admin 管理）==========
ADMIN_CAPTURED_IDS = _app_state.admin.ADMIN_CAPTURED_IDS
ADMIN_CAPTURED_IDS_LOCK = _app_state.admin.ADMIN_CAPTURED_IDS_LOCK

logger.info("[STARTUP] ✅ 全局状态已通过 AppState 初始化")


async def _warmup_admin_cache():
    """预热 admin 首屏缓存，消除重启后的冷启动延迟"""
    await asyncio.sleep(0.5)
    try:
        logger.info("🔥 预热 admin 首屏缓存...")
        # 预热 overview（含 SQLite 汇总查询）
        await admin_routes.get_overview(
            monitoring_service, stats_db, MonitorConfig, browser_ws_ref['ws'],
            browser_connections, browser_connections_lock, tab_connection_times,
            tab_request_counts, CONFIG, MODEL_ENDPOINT_MAP
        )
        # 预热 token_stats（最重的查询：多个 GROUP BY + 成本计算）
        await admin_routes.get_token_stats(
            None, None, None, None, 'day', stats_db,
            monitoring_service, MODEL_ENDPOINT_MAP, estimate_message_tokens, estimate_tokens
        )
        # 预热 request_stats
        await admin_routes.get_request_stats(
            None, None, stats_db, monitoring_service, MonitorConfig
        )
        logger.info("🔥 admin 首屏缓存预热完成")
    except Exception as e:
        logger.warning(f"⚠️ admin 缓存预热失败（不影响使用）: {e}")


# FastAPI生命周期
@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_event_loop, aiohttp_session, DOWNLOAD_SEMAPHORE, MAX_CONCURRENT_DOWNLOADS
    global direct_api_service, last_activity_time_ref
    global IMAGE_BASE64_CACHE, IMAGE_CACHE_MAX_SIZE, IMAGE_CACHE_TTL
    
    main_event_loop = asyncio.get_running_loop()
    load_config()
    
    # 🔧 修复：用 CONFIG 中的值重建 IMAGE_BASE64_CACHE（app_state 中是硬编码默认值）
    from cachetools import TTLCache
    mem_config = CONFIG.get("memory_management", {})
    IMAGE_CACHE_MAX_SIZE = mem_config.get("image_cache_max_size", CacheDefaults.IMAGE_CACHE_MAX_SIZE)
    IMAGE_CACHE_TTL = mem_config.get("image_cache_ttl_seconds", CacheDefaults.IMAGE_CACHE_TTL)
    IMAGE_BASE64_CACHE = TTLCache(maxsize=IMAGE_CACHE_MAX_SIZE, ttl=IMAGE_CACHE_TTL)
    _app_state.image.IMAGE_BASE64_CACHE = IMAGE_BASE64_CACHE
    _app_state.image.IMAGE_CACHE_MAX_SIZE = IMAGE_CACHE_MAX_SIZE
    _app_state.image.IMAGE_CACHE_TTL = IMAGE_CACHE_TTL
    logger.info(f"🔧 IMAGE_BASE64_CACHE 已按配置重建: maxsize={IMAGE_CACHE_MAX_SIZE}, ttl={IMAGE_CACHE_TTL}s")
    
    MAX_CONCURRENT_DOWNLOADS = CONFIG.get("max_concurrent_downloads", 50)
    pool_config = CONFIG.get("connection_pool", {})
    
    connector = aiohttp.TCPConnector(
        limit=pool_config.get("total_limit", 200),
        limit_per_host=pool_config.get("per_host_limit", 50),
        ttl_dns_cache=pool_config.get("dns_cache_ttl", 300),
        force_close=False,
        enable_cleanup_closed=True,
        keepalive_timeout=pool_config.get("keepalive_timeout", 30)
    )
    
    timeout_config = CONFIG.get("download_timeout", {})
    timeout = aiohttp.ClientTimeout(
        total=timeout_config.get("total", 30),
        connect=timeout_config.get("connect", 5),
        sock_read=timeout_config.get("sock_read", 10)
    )
    
    aiohttp_session = aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=True)
    direct_api_service = DirectAPIService(aiohttp_session)
    DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    
    logger.info(f"全局aiohttp会话已创建（优化配置）")
    logger.info(f"✅ Direct API服务已初始化")
    
    mode = CONFIG.get("id_updater_last_mode", "direct_chat")
    target = CONFIG.get("id_updater_battle_target", "A")
    logger.info("="*60)
    logger.info(f"  当前操作模式: {mode.upper()}")
    if mode == 'battle':
        logger.info(f"  - Battle 模式目标: Assistant {target}")
    logger.info("="*60)
    server_port = CONFIG.get("server_port", 5102)
    logger.info(f"📊 监控面板: http://127.0.0.1:{server_port}/admin")
    logger.info("="*60)

    load_model_map()
    load_model_endpoint_map()
    logger.info("服务器启动完成。等待油猴脚本连接...")

    last_activity_time_ref['time'] = datetime.now()
    
    # 启动后台任务
    asyncio.create_task(monitors.memory_monitor(
        CONFIG, DOWNLOAD_SEMAPHORE, MAX_CONCURRENT_DOWNLOADS,
        response_channels, request_metadata, IMAGE_BASE64_CACHE,
        FILEBED_URL_CACHE, FILEBED_URL_CACHE_TTL, downloaded_urls_set, downloaded_image_urls
    ))
    asyncio.create_task(monitors.config_monitor(
        CONFIG, CONFIG_FILE_MTIMES, load_config, load_model_endpoint_map, load_model_map,
        browser_connections, response_channels, MODEL_ENDPOINT_MAP
    ))
    asyncio.create_task(monitors.stale_request_cleaner(monitoring_service, response_channels, request_metadata))
    
    if stats_db.enabled and MODEL_ENDPOINT_MAP:
        # 🔧 A3 修复：用 asyncio.to_thread 包装同步 SQLite 批量操作，不阻塞事件循环
        async def _recalculate_costs_bg():
            try:
                logger.info("="*60)
                logger.info("💰 开始重新计算所有请求的费用...")
                recalculated = await asyncio.to_thread(stats_db.recalculate_costs, MODEL_ENDPOINT_MAP)
                if recalculated:
                    logger.info(f"✅ 费用重算完成: 更新了 {recalculated.get('updated_count', 0)} 条记录")
                logger.info("="*60)
            except Exception as e:
                logger.error(f"❌ 费用重算失败: {e}", exc_info=True)
        asyncio.create_task(_recalculate_costs_bg())
    
    # 🔥 预热 admin 首屏缓存（异步后台，不阻塞启动）
    asyncio.create_task(_warmup_admin_cache())
    
    yield
    
    # 保存 API Key 统计数据
    api_key_manager.save_now()
    
    if direct_api_service:
        await direct_api_service.close()
    if aiohttp_session:
        await aiohttp_session.close()
    logger.info("服务器正在关闭。")
    if _LOG_QUEUE_LISTENER:
        _LOG_QUEUE_LISTENER.stop()

app = FastAPI(lifespan=lifespan)

# CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Gzip压缩中间件（仅对管理页面/静态资源启用，SSE 流式接口跳过）
# 🔧 性能关键：原版 GZipMiddleware 会缓冲 SSE chunk 直到达到 minimum_size，
# 导致流式响应每次攒 2-3 个 chunk 才 flush，造成"CPU不高但真卡流"。
from starlette.middleware.gzip import GZipMiddleware as _OriginalGZipMiddleware


class SelectiveGZipMiddleware:
    """只对非流式路径启用 GZip 压缩的中间件。
    
    对 /v1/、/ws/ 等 API/WebSocket 路径直接透传，
    只对 /admin、/monitor、/js/、/css/ 等静态/管理页面启用 GZip。
    """
    
    # 跳过 GZip 的路径前缀（流式 API、WebSocket）
    SKIP_PREFIXES = ("/v1/", "/ws/", "/ws", "/internal/", "/auth/")
    
    def __init__(self, app, minimum_size: int = 500):
        self.app = app
        self.gzip_app = _OriginalGZipMiddleware(app, minimum_size=minimum_size)
    
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        
        path = scope.get("path", "")
        
        # 对 API/WebSocket 路径跳过 GZip，避免缓冲 SSE chunk
        if any(path.startswith(p) for p in self.SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return
        
        # 对管理页面/静态资源启用 GZip 压缩
        await self.gzip_app(scope, receive, send)


app.add_middleware(SelectiveGZipMiddleware, minimum_size=500)

# 🔧 C10 修复：静态文件加缓存头，避免每次刷新都重传大文件
class CachedStaticFiles(StaticFiles):
    """带 Cache-Control 头的静态文件服务"""
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            original_send = send
            async def send_with_cache(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    # 添加 1 小时公共缓存
                    headers.append((b"cache-control", b"public, max-age=3600"))
                    message = {**message, "headers": headers}
                await original_send(message)
            await super().__call__(scope, receive, send_with_cache)
        else:
            await super().__call__(scope, receive, send)

app.mount("/js", CachedStaticFiles(directory="js"), name="js")
app.mount("/css", CachedStaticFiles(directory="css"), name="css")

# 负载均衡包装器
async def select_best_tab_for_request():
    return await _select_best_tab_for_request(
        browser_connections, browser_connections_lock, tab_request_counts
    )

async def release_tab_request(tab_id: str):
    await _release_tab_request(tab_id, tab_request_counts, tab_request_counts_lock)

async def reassign_pending_requests(disconnected_tab_id: str, browser_id: str = None):
    await _reassign_pending_requests(
        disconnected_tab_id, browser_connections, browser_connections_lock,
        response_channels, request_metadata, tab_request_counts,
        CONFIG, convert_openai_to_lmarena_payload
    )

# ==================== WebSocket端点 ====================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket_routes.websocket_endpoint(
        websocket=websocket,
        browser_ws_ref=browser_ws_ref,
        browser_connections=browser_connections,
        browser_connections_lock=browser_connections_lock,
        tab_connection_times=tab_connection_times,
        tab_request_counts=tab_request_counts,
        tab_request_counts_lock=tab_request_counts_lock,
        response_channels=response_channels,
        request_metadata=request_metadata,
        pending_requests_queue=pending_requests_queue,
        IS_REFRESHING_FOR_VERIFICATION=IS_REFRESHING_FOR_VERIFICATION,
        VERIFICATION_COOLDOWN_UNTIL=VERIFICATION_COOLDOWN_UNTIL,
        CONFIG=CONFIG,
        monitoring_service=monitoring_service,
        process_pending_requests_func=lambda: request_processor.process_pending_requests(
            pending_requests_queue, handle_single_completion
        ),
        reassign_pending_requests_func=reassign_pending_requests,
        release_tab_request_func=release_tab_request
    )

# ==================== 核心API端点 ====================
# 所有核心API已拆分到 routes/api_routes.py

# 已知扫描/监控机器人 User-Agent 黑名单（不区分大小写）
_BLOCKED_USER_AGENTS = (
    "lmspeedbot",
    "go-http-client",
)


def _is_blocked_user_agent(request: Request) -> bool:
    """检查请求是否来自已知扫描机器人"""
    ua = request.headers.get("user-agent", "").lower()
    return any(blocked in ua for blocked in _BLOCKED_USER_AGENTS)


@app.get("/v1/models")
async def get_models_endpoint(request: Request):
    """提供兼容 OpenAI 的模型列表（根据 API Key 权限过滤）"""
    # 直接拒绝已知扫描机器人，返回空列表，不产生 401 日志噪音
    if _is_blocked_user_agent(request):
        return {"object": "list", "data": []}

    auth_header = request.headers.get("Authorization")
    provided_key = None
    if auth_header and auth_header.startswith("Bearer "):
        provided_key = auth_header.split(" ", 1)[1]

    global_api_key = CONFIG.get("api_key")
    has_guest_keys = api_key_manager.has_keys()

    # 只要配置了任何一种认证，就必须提供有效 key 才能获取模型列表，防止模型名泄露
    if global_api_key or has_guest_keys:
        if not provided_key:
            user_agent = request.headers.get("user-agent", "unknown")
            logger.warning(f"[401-/v1/models] 未提供API Key | 来源: {request.client.host if request.client else '?'} | User-Agent: {user_agent}")
            return JSONResponse(
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
                content={
                    "error": {
                        "message": "Incorrect API key provided. You can find your API key at https://platform.openai.com/account/api-keys.",
                        "type": "invalid_request_error",
                        "param": None,
                        "code": "invalid_api_key"
                    }
                }
            )

        # 管理员 key 通过，返回所有模型
        if global_api_key and provided_key == global_api_key:
            allowed_models = None
        elif has_guest_keys:
            allowed_models = api_key_manager.get_allowed_models(provided_key)
            if allowed_models is None:
                return JSONResponse(
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                    content={
                        "error": {
                            "message": "Incorrect API key provided. You can find your API key at https://platform.openai.com/account/api-keys.",
                            "type": "invalid_request_error",
                            "param": None,
                            "code": "invalid_api_key"
                        }
                    }
                )
            if len(allowed_models) == 0:
                allowed_models = None
        else:
            # 提供了 key 但不匹配，且没有访客 key 系统
            return JSONResponse(
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
                content={
                    "error": {
                        "message": "Incorrect API key provided. You can find your API key at https://platform.openai.com/account/api-keys.",
                        "type": "invalid_request_error",
                        "param": None,
                        "code": "invalid_api_key"
                    }
                }
            )
    else:
        allowed_models = None

    return await api_routes.get_models(MODEL_ENDPOINT_MAP, MODEL_NAME_TO_ID_MAP, allowed_models)


@app.get("/v1beta/models")
async def get_gemini_models_endpoint(request: Request):
    """提供Gemini v1beta格式的模型列表"""
    # 直接拒绝已知扫描机器人，返回空列表，不产生 401 日志噪音
    if _is_blocked_user_agent(request):
        return {"models": []}

    auth_header = request.headers.get("Authorization")
    provided_key = None
    if auth_header and auth_header.startswith("Bearer "):
        provided_key = auth_header.split(" ", 1)[1]

    global_api_key = CONFIG.get("api_key")
    has_guest_keys = api_key_manager.has_keys()

    # 只要配置了任何一种认证，就必须提供有效 key 才能获取模型列表，防止模型名泄露
    if global_api_key or has_guest_keys:
        if not provided_key:
            return JSONResponse(
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
                content={
                    "error": {
                        "message": "Incorrect API key provided.",
                        "type": "invalid_request_error",
                        "param": None,
                        "code": "invalid_api_key"
                    }
                }
            )

        is_valid = False
        if global_api_key and provided_key == global_api_key:
            is_valid = True
        elif has_guest_keys:
            if api_key_manager.get_allowed_models(provided_key) is not None:
                is_valid = True

        if not is_valid:
            return JSONResponse(
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
                content={
                    "error": {
                        "message": "Incorrect API key provided.",
                        "type": "invalid_request_error",
                        "param": None,
                        "code": "invalid_api_key"
                    }
                }
            )

    return await api_routes.get_gemini_models(MODEL_ENDPOINT_MAP)

@app.post("/v1beta/models/{model_name}:generateContent")
@app.post("/v1beta/models/{model_name}:streamGenerateContent")
async def gemini_native_api_endpoint(model_name: str, request: Request):
    """处理Gemini原生API格式的请求"""
    return await api_routes.gemini_native_api(
        model_name=model_name,
        request=request,
        MODEL_ENDPOINT_MAP=MODEL_ENDPOINT_MAP,
        monitoring_service=monitoring_service,
        direct_api_service=direct_api_service,
        last_activity_time_setter=lambda dt: last_activity_time_ref.update({'time': dt}),
        aiohttp_session=aiohttp_session
    )

@app.post("/v1/chat/completions")
async def chat_completions_endpoint(request: Request):
    """处理聊天补全请求"""
    return await api_routes.chat_completions(
        request=request,
        CONFIG=CONFIG,
        MODEL_ENDPOINT_MAP=MODEL_ENDPOINT_MAP,
        MODEL_NAME_TO_ID_MAP=MODEL_NAME_TO_ID_MAP,
        MODEL_ROUND_ROBIN_INDEX=MODEL_ROUND_ROBIN_INDEX,
        MODEL_ROUND_ROBIN_LOCK=MODEL_ROUND_ROBIN_LOCK,
        last_activity_time_setter=lambda dt: last_activity_time_ref.update({'time': dt}),
        VERIFICATION_COOLDOWN_UNTIL=VERIFICATION_COOLDOWN_UNTIL,
        IS_REFRESHING_FOR_VERIFICATION=IS_REFRESHING_FOR_VERIFICATION,
        browser_ws=browser_ws_ref['ws'],
        browser_connections=browser_connections,
        browser_connections_lock=browser_connections_lock,
        tab_request_counts=tab_request_counts,
        response_channels=response_channels,
        request_metadata=request_metadata,
        pending_requests_queue=pending_requests_queue,
        monitoring_service=monitoring_service,
        direct_api_service=direct_api_service,
        aiohttp_session=aiohttp_session,
        IMAGE_BASE64_CACHE=IMAGE_BASE64_CACHE,
        IMAGE_CACHE_MAX_SIZE=IMAGE_CACHE_MAX_SIZE,
        IMAGE_CACHE_TTL=IMAGE_CACHE_TTL,
        save_downloaded_image_async_func=save_downloaded_image_async,
        download_image_data_with_retry_func=_download_image_data_with_retry,
        release_tab_request_func=release_tab_request,
        select_best_tab_for_request_func=select_best_tab_for_request,
        convert_openai_to_lmarena_payload_func=convert_openai_to_lmarena_payload,
        process_lmarena_stream_func=_process_lmarena_stream,
        stream_generator_func=stream_generator,
        non_stream_response_func=non_stream_response,
        format_openai_chunk_func=format_openai_chunk,
        format_openai_finish_chunk_func=format_openai_finish_chunk,
        format_openai_error_chunk_func=format_openai_error_chunk,
        format_openai_non_stream_response_func=format_openai_non_stream_response,
        estimate_message_tokens_func=estimate_message_tokens,
        estimate_tokens_func=estimate_tokens,
        process_image_data_func=process_image_data
    )

@app.post("/v1/messages")
async def anthropic_messages_endpoint(request: Request):
    """处理 Anthropic Claude 兼容消息请求"""
    return await api_routes.anthropic_messages(
        request=request,
        CONFIG=CONFIG,
        MODEL_ENDPOINT_MAP=MODEL_ENDPOINT_MAP,
        MODEL_NAME_TO_ID_MAP=MODEL_NAME_TO_ID_MAP,
        MODEL_ROUND_ROBIN_INDEX=MODEL_ROUND_ROBIN_INDEX,
        MODEL_ROUND_ROBIN_LOCK=MODEL_ROUND_ROBIN_LOCK,
        last_activity_time_setter=lambda dt: last_activity_time_ref.update({'time': dt}),
        VERIFICATION_COOLDOWN_UNTIL=VERIFICATION_COOLDOWN_UNTIL,
        IS_REFRESHING_FOR_VERIFICATION=IS_REFRESHING_FOR_VERIFICATION,
        browser_ws=browser_ws_ref['ws'],
        browser_connections=browser_connections,
        browser_connections_lock=browser_connections_lock,
        tab_request_counts=tab_request_counts,
        response_channels=response_channels,
        request_metadata=request_metadata,
        pending_requests_queue=pending_requests_queue,
        monitoring_service=monitoring_service,
        direct_api_service=direct_api_service,
        aiohttp_session=aiohttp_session,
        IMAGE_BASE64_CACHE=IMAGE_BASE64_CACHE,
        IMAGE_CACHE_MAX_SIZE=IMAGE_CACHE_MAX_SIZE,
        IMAGE_CACHE_TTL=IMAGE_CACHE_TTL,
        save_downloaded_image_async_func=save_downloaded_image_async,
        download_image_data_with_retry_func=_download_image_data_with_retry,
        release_tab_request_func=release_tab_request,
        select_best_tab_for_request_func=select_best_tab_for_request,
        convert_openai_to_lmarena_payload_func=convert_openai_to_lmarena_payload,
        process_lmarena_stream_func=_process_lmarena_stream,
        stream_generator_func=stream_generator,
        non_stream_response_func=non_stream_response,
        format_openai_chunk_func=format_openai_chunk,
        format_openai_finish_chunk_func=format_openai_finish_chunk,
        format_openai_error_chunk_func=format_openai_error_chunk,
        format_openai_non_stream_response_func=format_openai_non_stream_response,
        estimate_message_tokens_func=estimate_message_tokens,
        estimate_tokens_func=estimate_tokens,
        process_image_data_func=process_image_data
    )

# ==================== 内部通信端点 ====================
@app.post("/internal/start_id_capture")
async def start_id_capture_endpoint(request: Request):
    return await internal_routes.start_id_capture(
        request, browser_ws_ref['ws'], ADMIN_CAPTURED_IDS, ADMIN_CAPTURED_IDS_LOCK
    )

@app.post("/internal/receive_captured_ids")
async def receive_captured_ids_endpoint(request: Request):
    return await internal_routes.receive_captured_ids(
        request, ADMIN_CAPTURED_IDS, ADMIN_CAPTURED_IDS_LOCK
    )

@app.post("/update")
async def update_endpoint(request: Request):
    return await internal_routes.receive_captured_ids(
        request, ADMIN_CAPTURED_IDS, ADMIN_CAPTURED_IDS_LOCK
    )

@app.get("/api/admin/capture_status")
async def get_capture_status_endpoint():
    return await internal_routes.get_capture_status(
        ADMIN_CAPTURED_IDS, ADMIN_CAPTURED_IDS_LOCK
    )

@app.post("/api/admin/save_captured_model")
async def save_captured_model_endpoint(request: Request):
    return await internal_routes.save_captured_model(
        request, ADMIN_CAPTURED_IDS, ADMIN_CAPTURED_IDS_LOCK,
        MODEL_ENDPOINT_MAP_PATH, load_model_endpoint_map
    )

# ==================== 管理面板端点 ====================
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard_endpoint():
    return await admin_routes.admin_dashboard()

@app.get("/token_calculator", response_class=HTMLResponse)
async def token_calculator_endpoint():
    """返回Token计算器页面"""
    try:
        with open('token_calculator.html', 'r', encoding='utf-8') as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>Token计算器页面未找到</h1><p>请确保 token_calculator.html 文件在正确的位置。</p>",
            status_code=404
        )

@app.get("/api/admin/models")
async def get_models_config_endpoint():
    return await admin_routes.get_models_config(
        MODEL_ENDPOINT_MAP, MODEL_NAME_TO_ID_MAP, load_model_endpoint_map
    )

@app.post("/api/admin/models")
async def update_model_config_endpoint(request: Request):
    return await admin_routes.update_model_config(request, load_model_endpoint_map)

@app.delete("/api/admin/models/{model_name}")
async def delete_model_config_endpoint(model_name: str):
    return await admin_routes.delete_model_config(model_name, load_model_endpoint_map)

@app.post("/api/admin/models/reorder")
async def reorder_models_endpoint(request: Request):
    return await admin_routes.reorder_models(request, load_model_endpoint_map)

@app.get("/api/admin/config")
async def get_config_endpoint():
    return await admin_routes.get_config(CONFIG)

@app.post("/api/admin/config")
async def update_config_endpoint(request: Request):
    return await admin_routes.update_config(request, _parse_jsonc, load_config)

@app.get("/api/admin/overview")
async def get_overview_endpoint():
    return await admin_routes.get_overview(
        monitoring_service, stats_db, MonitorConfig, browser_ws_ref['ws'],
        browser_connections, browser_connections_lock, tab_connection_times,
        tab_request_counts, CONFIG, MODEL_ENDPOINT_MAP
    )

@app.get("/api/admin/tokenizer_info")
async def get_tokenizer_info_endpoint():
    return await admin_routes.get_tokenizer_info_api(get_token_counter_info)

@app.get("/api/admin/tokenizer_mappings")
async def get_tokenizer_mappings_endpoint():
    return await admin_routes.get_tokenizer_mappings(_parse_jsonc)

@app.post("/api/admin/tokenizer_mappings")
async def update_tokenizer_mappings_endpoint(request: Request):
    return await admin_routes.update_all_tokenizer_mappings(request, _parse_jsonc, load_config)

@app.get("/api/admin/tokenizers_status")
async def get_all_tokenizers_status_endpoint():
    """获取所有分词器的详细状态"""
    return await admin_routes.get_all_tokenizers_status_api(get_all_tokenizers_status)

@app.post("/api/admin/calculate_tokens")
async def calculate_tokens_endpoint(request: Request):
    """计算文本的token数量"""
    return await admin_routes.calculate_tokens_api(request, calculate_tokens_for_text)

@app.post("/api/admin/compare_tokenizers")
async def compare_tokenizers_endpoint(request: Request):
    """对比两种分词器的结果"""
    return await admin_routes.compare_tokenizers_api(request, compare_tokenizers)

@app.post("/api/admin/install_tokenizer")
async def install_tokenizer_endpoint(request: Request):
    """安装分词器包"""
    return await admin_routes.install_tokenizer_api(
        request, install_tokenizer_package, get_all_tokenizers_status
    )

@app.post("/api/admin/custom_tokenizers")
async def add_custom_tokenizer_endpoint(request: Request):
    """添加自定义分词器"""
    return await admin_routes.add_custom_tokenizer_api(request, add_custom_tokenizer)

@app.delete("/api/admin/custom_tokenizers/{name}")
async def delete_custom_tokenizer_endpoint(name: str):
    """删除自定义分词器"""
    return await admin_routes.delete_custom_tokenizer_api(name, delete_custom_tokenizer)

@app.get("/api/admin/custom_tokenizers")
async def list_custom_tokenizers_endpoint():
    """列出所有自定义分词器"""
    return await admin_routes.list_custom_tokenizers_api(list_custom_tokenizers)

@app.post("/api/admin/test_model_keys")
async def test_model_keys_endpoint(request: Request):
    """测试单个模型配置中的所有 API Key（并行请求）"""
    return await admin_routes.test_model_keys(request, direct_api_service)

@app.get("/api/admin/token_stats")
async def get_token_stats_endpoint(start_date: str = None, end_date: str = None, start_time: str = None, end_time: str = None, rpm_period: str = None):
    return await admin_routes.get_token_stats(
        start_date, end_date, start_time, end_time, rpm_period, stats_db,
        monitoring_service, MODEL_ENDPOINT_MAP, estimate_message_tokens, estimate_tokens
    )


@app.get("/api/admin/export_report")
async def export_report_endpoint(start_date: str = None, end_date: str = None):
    return await admin_routes.export_report(
        stats_db, monitoring_service, MODEL_ENDPOINT_MAP, start_date, end_date
    )


@app.get("/api/admin/request_stats")
async def get_request_stats_endpoint(start_time: str = None, end_time: str = None):
    return await admin_routes.get_request_stats(
        start_time, end_time, stats_db, monitoring_service, MonitorConfig
    )

@app.post("/api/admin/merge_model_stats")
async def merge_model_stats_endpoint(request: Request):
    return await admin_routes.merge_model_stats(request, stats_db)

@app.post("/api/admin/delete_model_stats")
async def delete_model_stats_endpoint(request: Request):
    return await admin_routes.delete_model_stats(request, stats_db)

# ==================== 监控面板端点 ====================
@app.get("/monitor", response_class=HTMLResponse)
async def monitor_dashboard_endpoint():
    return await monitor_routes.monitor_dashboard()

@app.websocket("/ws/monitor")
async def monitor_websocket_endpoint(websocket: WebSocket):
    await monitor_routes.monitor_websocket(
        websocket, monitoring_service, browser_ws_ref['ws'], CONFIG
    )

@app.get("/api/monitor/stats")
async def get_monitor_stats_endpoint():
    return await monitor_routes.get_monitor_stats(
        monitoring_service, browser_ws_ref['ws'], CONFIG
    )

@app.get("/api/monitor/active")
async def get_active_requests_endpoint():
    return await monitor_routes.get_active_requests(monitoring_service)

@app.get("/api/monitor/logs/requests")
async def get_request_logs_endpoint(limit: int = 50):
    return await monitor_routes.get_request_logs(limit, monitoring_service)

@app.get("/api/monitor/logs/errors")
async def get_error_logs_endpoint(limit: int = 30):
    return await monitor_routes.get_error_logs(limit, monitoring_service)

@app.get("/api/monitor/recent")
async def get_recent_data_endpoint():
    return await monitor_routes.get_recent_data(monitoring_service)

@app.get("/api/monitor/performance")
async def get_performance_metrics_endpoint():
    return await monitor_routes.get_performance_metrics(
        CONFIG, DOWNLOAD_SEMAPHORE, MAX_CONCURRENT_DOWNLOADS,
        aiohttp_session, IMAGE_BASE64_CACHE, IMAGE_CACHE_MAX_SIZE,
        downloaded_urls_set, response_channels, DISABLED_ENDPOINTS
    )

@app.get("/api/monitor/tabs")
async def get_tab_connections_endpoint():
    return await monitor_routes.get_tab_connections(
        browser_connections, browser_connections_lock, tab_connection_times,
        tab_request_counts
    )

@app.get("/api/monitor/memory")
async def get_memory_info_endpoint():
    """获取服务器内存使用信息"""
    return await monitor_routes.get_memory_info()

@app.post("/api/monitor/clear_tokenizer_cache")
async def clear_tokenizer_cache_endpoint(force: bool = False):
    """清理tokenizer缓存"""
    return await monitor_routes.clear_tokenizer_cache_api(force)

@app.get("/api/request/{request_id}")
async def get_request_details_endpoint(request_id: str):
    """获取特定请求的详细信息"""
    # 🔧 性能修复：get_request_details 内部有同步文件 I/O（遍历日志目录、读取 SQLite/JSONL）
    # 必须用 asyncio.to_thread 包装，否则会阻塞事件循环导致流式响应卡顿
    details = await asyncio.to_thread(monitoring_service.get_request_details, request_id)
    if details:
        return details
    else:
        raise HTTPException(status_code=404, detail="请求详情未找到")

@app.get("/api/logs/download")
async def download_logs_endpoint(log_type: str = "requests"):
    """下载日志文件"""
    from fastapi.responses import FileResponse
    from modules.monitoring import MonitorConfig
    
    if log_type == "requests":
        log_path = MonitorConfig.LOG_DIR / MonitorConfig.REQUEST_LOG_FILE
    elif log_type == "errors":
        log_path = MonitorConfig.LOG_DIR / MonitorConfig.ERROR_LOG_FILE
    else:
        raise HTTPException(status_code=400, detail="无效的日志类型")
    
    if not log_path.exists():
        raise HTTPException(status_code=404, detail="日志文件不存在")
    
    return FileResponse(
        path=str(log_path),
        filename=f"{log_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl",
        media_type="application/json"
    )

# ==================== 辅助函数 ====================
async def handle_single_completion(openai_req: dict, retry_request_id: str = None):
    """
    处理单个聊天补全请求
    
    用于从pending_requests_queue中处理待处理的请求
    
    Args:
        openai_req: OpenAI格式的请求数据
        retry_request_id: 重试请求ID（可选）
    
    Returns:
        处理结果
    """
    from fastapi import Request
    from starlette.requests import Request as StarletteRequest
    from starlette.datastructures import Headers
    import io
    
    # 构造一个模拟的FastAPI Request对象
    # 这是一个简化实现，主要用于内部重试逻辑
    try:
        # 直接调用 api_routes.chat_completions 的核心逻辑
        # 由于这个函数是从队列中处理请求，我们需要模拟请求上下文
        
        model = openai_req.get("model", "unknown")
        logger.info(f"[HANDLE_SINGLE] 处理待处理请求: 模型={model}, 重试ID={retry_request_id}")
        
        # 获取模型配置
        endpoint_config = MODEL_ENDPOINT_MAP.get(model)
        if not endpoint_config:
            logger.error(f"[HANDLE_SINGLE] 模型 '{model}' 未找到配置")
            return {"error": f"Model '{model}' not configured"}
        
        # 处理多端点情况
        if isinstance(endpoint_config, list) and endpoint_config:
            endpoint_config = endpoint_config[0]
        
        # 检查是否为Direct API模式
        api_type = endpoint_config.get("api_type") if isinstance(endpoint_config, dict) else None
        
        if api_type in ["direct_api", "passthrough", "gemini_native"]:
            # Direct API模式 - 使用direct_api_service
            if direct_api_service:
                from routes.direct_api_handler import handle_direct_api_request
                result = await handle_direct_api_request(
                    openai_req=openai_req,
                    model_name=model,
                    endpoint_config=endpoint_config,
                    CONFIG=CONFIG,
                    PROCESSED_IMAGE_CACHE=IMAGE_BASE64_CACHE,
                    monitoring_service=monitoring_service,
                    direct_api_service=direct_api_service,
                    estimate_message_tokens_func=estimate_message_tokens,
                    estimate_tokens_func=estimate_tokens,
                    process_image_data_func=process_image_data,
                    full_messages=openai_req.get("messages", []),
                )
                return result
            else:
                logger.error("[HANDLE_SINGLE] direct_api_service 未初始化")
                return {"error": "Direct API service not initialized"}
        else:
            # LMArena模式 - 需要WebSocket连接
            if not browser_connections:
                logger.warning("[HANDLE_SINGLE] 没有可用的浏览器连接")
                # 重新放回队列
                await pending_requests_queue.put({
                    "openai_req": openai_req,
                    "retry_request_id": retry_request_id
                })
                return {"error": "No browser connections available", "queued": True}
            
            # 选择最佳标签页
            tab_id = await select_best_tab_for_request()
            if not tab_id:
                logger.warning("[HANDLE_SINGLE] 无法获取可用标签页")
                return {"error": "No available tabs"}
            
            # 转换请求格式
            session_id = CONFIG.get("session_id")
            lmarena_payload = await convert_openai_to_lmarena_payload(openai_req, session_id)
            
            # 创建响应通道
            request_id = retry_request_id or str(uuid.uuid4())
            response_queue = asyncio.Queue()
            response_channels[request_id] = response_queue
            request_metadata[request_id] = {
                "tab_id": tab_id,
                "model": model,
                "start_time": time.time()
            }
            
            # 发送请求到浏览器
            ws = browser_connections[tab_id]
            await ws.send_text(json.dumps({
                "command": "send_message",
                "request_id": request_id,
                "payload": lmarena_payload
            }))
            
            logger.info(f"[HANDLE_SINGLE] 请求已发送到标签页 {tab_id}: {request_id[:8]}")
            
            # 等待响应（这里简化处理，实际应该是流式的）
            # 完整的流式处理在 stream_generator 和 non_stream_response 中
            return {"request_id": request_id, "status": "processing"}
            
    except Exception as e:
        logger.error(f"[HANDLE_SINGLE] 处理请求时发生错误: {e}", exc_info=True)
        return {"error": str(e)}

# ==================== Web访问密钥验证 ====================
from starlette.responses import RedirectResponse
from urllib.parse import unquote


class WebAccessKeyMiddleware:
    """Web界面访问密钥验证中间件
    
    🔧 性能关键：使用纯 ASGI 实现而非 BaseHTTPMiddleware。
    BaseHTTPMiddleware 会在内部创建协程桥接队列，把 StreamingResponse 的每个 chunk
    都经过 put→事件循环调度→get 的流程，导致 SSE 流式响应严重卡顿（CPU 不高但延迟大）。
    纯 ASGI 中间件对不需要验证的路径实现零开销透传（直接 await self.app(scope, receive, send)）。
    """
    
    PROTECTED_PATHS = ("/admin", "/monitor", "/token_calculator", "/api/admin", "/api/monitor", "/ws/monitor")
    EXCLUDED_PATHS = ("/login", "/auth/")
    
    def __init__(self, app):
        self.app = app
    
    async def __call__(self, scope, receive, send):
        # 非 HTTP/WebSocket 请求直接透传（如 lifespan）
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        
        path: str = scope.get("path", "")
        
        # 快速路径：绝大多数请求（如 /v1/chat/completions）不在保护列表中，零开销透传
        needs_auth = any(path.startswith(p) for p in self.PROTECTED_PATHS)
        if not needs_auth:
            await self.app(scope, receive, send)
            return
        
        is_excluded = any(path.startswith(p) for p in self.EXCLUDED_PATHS)
        if is_excluded:
            await self.app(scope, receive, send)
            return
        
        # 需要验证：检查配置
        web_key = CONFIG.get("web_access_key", "")
        if not web_key:
            await self.app(scope, receive, send)
            return
        
        # 从 ASGI scope headers 中提取提交的密钥
        submitted_key = self._extract_key_from_scope(scope)
        
        if submitted_key == web_key:
            await self.app(scope, receive, send)
            return
        
        # ---- 验证失败 ----
        if scope["type"] == "websocket":
            # WebSocket 握手阶段直接关闭；Uvicorn 会将未 accept 的 close 映射为握手失败
            await send({
                "type": "websocket.close",
                "code": 1008,
                "reason": "需要Web访问密钥验证"
            })
            return
        
        # HTTP 请求
        if path in ("/admin", "/monitor", "/token_calculator"):
            response = RedirectResponse(url=f"/login?next={path}", status_code=303)
        else:
            response = JSONResponse(
                status_code=401,
                content={"error": "Unauthorized", "message": "需要Web访问密钥验证"}
            )
        await response(scope, receive, send)
    
    @staticmethod
    def _extract_key_from_scope(scope) -> str:
        """从 ASGI scope 的 headers 中提取 web_access_key"""
        cookie_value = ""
        x_key_value = ""
        
        for name, value in scope.get("headers", []):
            if name == b"cookie":
                cookie_value = value.decode("latin-1")
            elif name == b"x-web-access-key":
                x_key_value = value.decode("latin-1")
        
        # 优先从 cookie 提取
        if cookie_value:
            for part in cookie_value.split(";"):
                part = part.strip()
                if part.startswith("web_access_key="):
                    return unquote(part[len("web_access_key="):])
        
        return unquote(x_key_value) if x_key_value else ""


# 添加中间件（在CORS之后）
app.add_middleware(WebAccessKeyMiddleware)

# ==================== 登录页面和验证API ====================
@app.get("/login", response_class=HTMLResponse)
async def login_page(next: str = "/admin"):
    """返回登录页面"""
    html_content = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>访问验证 - LMArena Bridge</title>
    <style>
        :root {{
            --bg-deep: linear-gradient(180deg, #090e16 0%, #0a1220 100%);
            --surface: #0e1a2d;
            --line-strong: #223650;
            --text-main: #d9e5ff;
            --text-dim: #8fa0bf;
            --accent: #2aa8ff;
            --accent-soft: rgba(42,168,255,0.35);
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'SF Pro Display', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg-deep);
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .login-box {{
            background: var(--surface);
            border: 1px solid var(--line-strong);
            border-radius: 12px;
            padding: 40px;
            width: 100%;
            max-width: 400px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.5);
        }}
        h1 {{
            color: var(--accent);
            text-align: center;
            margin-bottom: 10px;
            font-size: 1.8rem;
        }}
        .subtitle {{
            text-align: center;
            color: var(--text-dim);
            margin-bottom: 30px;
            font-size: 0.9rem;
        }}
        .form-group {{
            margin-bottom: 20px;
        }}
        label {{
            display: block;
            margin-bottom: 8px;
            color: var(--text-dim);
            font-size: 0.9rem;
        }}
        input {{
            width: 100%;
            padding: 12px 15px;
            background: #0b1422;
            border: 1px solid var(--line-strong);
            border-radius: 6px;
            color: var(--text-main);
            font-size: 1rem;
        }}
        input:focus {{
            outline: none;
            border-color: var(--accent);
            box-shadow: 0 0 0 3px rgba(42, 168, 255, 0.1);
        }}
        button {{
            width: 100%;
            padding: 14px;
            background: rgba(42, 168, 255, 0.2);
            border: 1px solid var(--accent-soft);
            border-radius: 6px;
            color: var(--accent);
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }}
        button:hover {{
            background: rgba(42, 168, 255, 0.3);
        }}
        .error {{
            background: rgba(239, 68, 68, 0.1);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: #ef4444;
            padding: 12px;
            border-radius: 6px;
            margin-bottom: 20px;
            display: none;
        }}
        .error.show {{ display: block; }}
        .success {{
            background: rgba(16, 185, 129, 0.1);
            border: 1px solid rgba(16, 185, 129, 0.3);
            color: #10b981;
            padding: 12px;
            border-radius: 6px;
            margin-bottom: 20px;
            display: none;
        }}
        .success.show {{ display: block; }}
    </style>
</head>
<body>
    <div class="login-box">
        <h1>🔐 访问验证</h1>
        <p class="subtitle">请输入Web访问密钥</p>
        <div id="error" class="error"></div>
        <div id="success" class="success"></div>
        <form onsubmit="return submitKey()">
            <div class="form-group">
                <label for="key">访问密钥</label>
                <input type="password" id="key" placeholder="请输入密钥..." autofocus>
            </div>
            <button type="submit">🚀 验证并进入</button>
        </form>
    </div>
    <script>
        function submitKey() {{
            var key = document.getElementById('key').value;
            var errorDiv = document.getElementById('error');
            var btn = document.querySelector('button');
            var successDiv = document.getElementById('success');
            
            if (!key) {{
                errorDiv.textContent = '请输入密钥';
                errorDiv.className = 'error show';
                return false;
            }}
            
            btn.disabled = true;
            btn.textContent = '验证中...';
            errorDiv.className = 'error';
            successDiv.className = 'success';
            
            var xhr = new XMLHttpRequest();
            xhr.open('POST', '/auth/verify', true);
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.onreadystatechange = function() {{
                if (xhr.readyState === 4) {{
                    try {{
                        var data = JSON.parse(xhr.responseText);
                        if (data.success) {{
                            document.cookie = "web_access_key=" + encodeURIComponent(key) + "; path=/; max-age=86400";
                            successDiv.textContent = '✅ 验证成功！点击下方按钮进入';
                            successDiv.className = 'success show';
                            btn.textContent = '👉 进入管理面板';
                            btn.disabled = false;
                            btn.onclick = function() {{ window.location.href = '/admin'; return false; }};
                        }} else {{
                            errorDiv.textContent = data.message || '密钥错误';
                            errorDiv.className = 'error show';
                            btn.disabled = false;
                            btn.textContent = '🚀 验证并进入';
                        }}
                    }} catch (e) {{
                        errorDiv.textContent = '验证失败: ' + (xhr.status === 0 ? '网络错误' : xhr.statusText);
                        errorDiv.className = 'error show';
                        btn.disabled = false;
                        btn.textContent = '🚀 验证并进入';
                    }}
                }}
            }};
            xhr.send(JSON.stringify({{ key: key }}));
            return false;
        }}
    </script>
</body>
</html>'''
    return HTMLResponse(content=html_content)

@app.post("/auth/verify")
async def verify_web_key(request: Request):
    """验证Web访问密钥"""
    try:
        data = await request.json()
        submitted_key = data.get("key", "")
        web_key = CONFIG.get("web_access_key", "")
        
        if not web_key:
            return {"success": True, "message": "未配置密钥，无需验证"}
        
        if submitted_key == web_key:
            return {"success": True, "message": "验证成功"}
        else:
            return {"success": False, "message": "密钥错误"}
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.get("/auth/check")
async def check_web_auth(request: Request):
    """检查当前是否已通过Web验证"""
    web_key = CONFIG.get("web_access_key", "")
    
    if not web_key:
        return {"authenticated": True, "reason": "no_key_configured"}
    
    submitted_key = request.cookies.get("web_access_key", "")
    if submitted_key == web_key:
        return {"authenticated": True, "reason": "valid_key"}
    else:
        return {"authenticated": False, "reason": "invalid_or_missing_key"}

# ==================== API Key 管理端点 ====================
@app.get("/api/admin/api_keys")
async def list_api_keys_endpoint():
    """列出所有 API Key（需要管理员权限，由 WebAccessKeyMiddleware 保护）"""
    keys = api_key_manager.list_keys()
    return {"keys": keys, "total": len(keys)}

@app.post("/api/admin/api_keys")
async def create_api_key_endpoint(request: Request):
    """创建新的 API Key"""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无效的 JSON 请求体")
    
    name = data.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="API Key 名称不能为空")
    
    allowed_models = data.get("allowed_models", [])
    rpm_limit = data.get("rpm_limit", 0)
    description = data.get("description", "")
    enabled = data.get("enabled", True)
    
    # 验证 allowed_models 是否为列表
    if not isinstance(allowed_models, list):
        raise HTTPException(status_code=400, detail="allowed_models 必须是一个列表")
    
    # 验证 rpm_limit 是否为非负整数
    try:
        rpm_limit = int(rpm_limit)
        if rpm_limit < 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="rpm_limit 必须是非负整数")
    
    key_info = api_key_manager.create_key(
        name=name,
        allowed_models=allowed_models,
        rpm_limit=rpm_limit,
        enabled=enabled,
        description=description,
    )
    
    return {"success": True, "key": key_info, "message": "API Key 创建成功。请保存好 secret，它只会显示一次！"}

@app.put("/api/admin/api_keys/{key_id}")
async def update_api_key_endpoint(key_id: str, request: Request):
    """更新 API Key 配置"""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无效的 JSON 请求体")
    
    result = api_key_manager.update_key(key_id, data)
    if result is None:
        raise HTTPException(status_code=404, detail=f"API Key '{key_id}' 不存在")
    
    return {"success": True, "key": result}

@app.delete("/api/admin/api_keys/{key_id}")
async def delete_api_key_endpoint(key_id: str):
    """删除 API Key"""
    if api_key_manager.delete_key(key_id):
        return {"success": True, "message": f"API Key '{key_id}' 已删除"}
    else:
        raise HTTPException(status_code=404, detail=f"API Key '{key_id}' 不存在")

@app.get("/api/admin/api_keys/{key_id}")
async def get_api_key_endpoint(key_id: str):
    """获取单个 API Key 的详细信息"""
    key_info = api_key_manager.get_key_info(key_id)
    if key_info is None:
        raise HTTPException(status_code=404, detail=f"API Key '{key_id}' 不存在")
    return {"key": key_info}

@app.post("/api/admin/api_keys/reload")
async def reload_api_keys_endpoint():
    """重新加载 API Key 配置"""
    api_key_manager.reload()
    keys = api_key_manager.list_keys()
    return {"success": True, "message": f"已重新加载 {len(keys)} 个 API Key", "total": len(keys)}

# ==================== 主程序入口 ====================
if __name__ == "__main__":
    import socket
    import sys
    
    # ===== Windows 平台兼容性修复 =====
    # Python 3.11 在 Windows 上的 ProactorEventLoop (IOCP) 存在已知 bug:
    # 当 accept() 时客户端突然断开，WinError 64 (ERROR_NETNAME_DELETED)
    # 未被正确捕获，导致 accept 循环终止，服务器停止接受新连接。
    # 切换到 SelectorEventLoop 可彻底避免此问题。
    # 参考: https://github.com/python/cpython/issues/110947
    if sys.platform == "win32":
        py_ver = sys.version_info
        if py_ver < (3, 12, 0):
            logger.warning("=" * 60)
            logger.warning("⚠️ 检测到 Windows + Python %d.%d.%d", py_ver.major, py_ver.minor, py_ver.micro)
            logger.warning("   该组合存在 IOCP accept bug (WinError 64)，会导致服务器假死。")
            logger.warning("   强烈建议升级到 Python 3.12+。")
            logger.warning("=" * 60)
        # 无论版本，Windows 下统一切换为 SelectorEventLoop 以根除问题
        if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            logger.info("🔄 已启用 WindowsSelectorEventLoop 以避免 IOCP accept bug")
    
    # 先加载配置以获取端口号
    load_config(force_reload=True)
    api_port = CONFIG.get("server_port", 5102)
    enable_ipv6 = CONFIG.get("enable_ipv6", True)
    
    logger.info(f"🚀 LMArena Bridge v2.0 API 服务器正在启动（重构版）...")
    logger.info(f"   - 端口: {api_port}")
    
    # 检查是否配置了web访问密钥
    if CONFIG.get("web_access_key"):
        logger.info(f"   - Web访问保护: ✅ 已启用")
    else:
        logger.info(f"   - Web访问保护: ❌ 未配置（任何人可访问管理面板）")
    
    async def run_with_dual_stack():
        """使用双栈模式运行服务器（IPv4 + IPv6）"""
        # Windows 下使用两个独立 socket，避免 IPV6_V6ONLY=0 的兼容性问题
        if sys.platform == "win32":
            sock_v4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock_v4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock_v4.bind(("0.0.0.0", api_port))
            sock_v4.listen(128)
            sock_v4.setblocking(False)
            
            sock_v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            sock_v6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # 纯 IPv6，不映射 IPv4
            sock_v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock_v6.bind(("::", api_port))
            sock_v6.listen(128)
            sock_v6.setblocking(False)
            
            config = uvicorn.Config(app, host="::", port=api_port)
            server = uvicorn.Server(config)
            await server.serve(sockets=[sock_v4, sock_v6])
        else:
            # Linux/macOS 上使用单 socket 双栈模式
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            sock.bind(("::", api_port))
            sock.listen(128)
            sock.setblocking(False)
            
            config = uvicorn.Config(app, host="::", port=api_port)
            server = uvicorn.Server(config)
            await server.serve(sockets=[sock])
    
    if enable_ipv6:
        # 尝试使用双栈模式
        try:
            logger.info(f"   - 双栈模式: ✅ IPv4 + IPv6")
            logger.info(f"   - IPv4访问: http://127.0.0.1:{api_port}")
            logger.info(f"   - IPv6访问: http://[::1]:{api_port}")
            asyncio.run(run_with_dual_stack())
        except Exception as e:
            logger.warning(f"   - 双栈模式失败: {e}")
            logger.info(f"   - 回退到仅IPv4模式")
            logger.info(f"   - IPv4访问: http://127.0.0.1:{api_port}")
            uvicorn.run(app, host="0.0.0.0", port=api_port)
    else:
        logger.info(f"   - IPv4访问: http://127.0.0.1:{api_port}")
        uvicorn.run(app, host="0.0.0.0", port=api_port)