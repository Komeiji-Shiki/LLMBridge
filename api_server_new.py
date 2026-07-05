# api_server.py - 精简重构版本
# 原4000+行代码已拆分到多个模块

import asyncio
import logging
import queue
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener
from typing import Optional

import aiohttp
from hypercorn.config import Config as HypercornConfig
from hypercorn.asyncio import serve as hypercorn_serve
from fastapi import FastAPI, WebSocket, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

# 内部模块导入
from modules.monitoring import monitoring_service, MonitorConfig
from utils.task_registry import spawn
from modules.token_counter import (
    estimate_message_tokens, estimate_tokens, get_token_counter_info,
    get_all_tokenizers_status, calculate_tokens_for_text, compare_tokenizers,
    install_tokenizer_package, add_custom_tokenizer, delete_custom_tokenizer,
    list_custom_tokenizers
)

# Core模块
from core.config_loader import (
    CONFIG, MODEL_NAME_TO_ID_MAP, MODEL_ENDPOINT_MAP,
    CONFIG_FILE_MTIMES,
    load_config, load_model_map, load_model_endpoint_map, _parse_jsonc
)
from core.api_key_manager import api_key_manager
from core.db_stats import stats_db
from core.app_state import get_app_state, AppState
from core.constants import CacheDefaults

# Services
from services.direct_api_service import DirectAPIService

# Routes
from routes import websocket_routes, internal_routes, monitor_routes, admin_routes, api_routes

# Background tasks
from background_tasks import monitors

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
    """过滤 HTTP access 日志：抑制监控轮询和恶意扫描的噪音（兼容 uvicorn/hypercorn）"""
    
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

logging.getLogger("hypercorn.access").addFilter(EndpointFilter())
logging.getLogger("hypercorn.error").addFilter(EndpointFilter())

# ==================== 全局状态（AppState 单例统一管理） ====================
# 🔧 重构：删除模块级镜像别名。
# 旧版把 AppState 字段镜像成模块级变量，对标量（布尔/数值）和会被重新赋值的
# 引用（aiohttp_session 等）只是一次性快照，赋值瞬间即与 AppState 脱钩，
# 已经造成过“人机验证状态失效”级别的 bug。现在所有状态统一走 _app_state。

_app_state: AppState = get_app_state()

logger.info("[STARTUP] ✅ 全局状态已通过 AppState 初始化")


async def _warmup_admin_cache():
    """预热 admin 首屏缓存，消除重启后的冷启动延迟"""
    await asyncio.sleep(0.5)
    try:
        logger.info("🔥 预热 admin 首屏缓存...")
        conn = _app_state.connection
        # 预热 overview（含 SQLite 汇总查询）
        await admin_routes.get_overview(
            monitoring_service, stats_db, MonitorConfig, conn.browser_ws_ref['ws'],
            conn.browser_connections, conn.browser_connections_lock, conn.tab_connection_times,
            conn.tab_request_counts, CONFIG, MODEL_ENDPOINT_MAP
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
    server_state = _app_state.server
    image_state = _app_state.image
    request_state = _app_state.request
    conn_state = _app_state.connection

    server_state.main_event_loop = asyncio.get_running_loop()
    load_config()

    # 🔧 用 CONFIG 中的值重建 IMAGE_BASE64_CACHE（app_state 中是硬编码默认值）
    from cachetools import TTLCache
    mem_config = CONFIG.get("memory_management", {})
    image_state.IMAGE_CACHE_MAX_SIZE = mem_config.get("image_cache_max_size", CacheDefaults.IMAGE_CACHE_MAX_SIZE)
    image_state.IMAGE_CACHE_TTL = mem_config.get("image_cache_ttl_seconds", CacheDefaults.IMAGE_CACHE_TTL)
    image_state.IMAGE_BASE64_CACHE = TTLCache(
        maxsize=image_state.IMAGE_CACHE_MAX_SIZE, ttl=image_state.IMAGE_CACHE_TTL
    )
    logger.info(
        f"🔧 IMAGE_BASE64_CACHE 已按配置重建: "
        f"maxsize={image_state.IMAGE_CACHE_MAX_SIZE}, ttl={image_state.IMAGE_CACHE_TTL}s"
    )

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

    # 所有运行时依赖直接写入 AppState，全部路由链路从 AppState 读取
    server_state.aiohttp_session = aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=True)
    server_state.direct_api_service = DirectAPIService(server_state.aiohttp_session)
    server_state.MAX_CONCURRENT_DOWNLOADS = CONFIG.get("max_concurrent_downloads", 50)
    server_state.DOWNLOAD_SEMAPHORE = asyncio.Semaphore(server_state.MAX_CONCURRENT_DOWNLOADS)

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

    server_state.last_activity_time_ref['time'] = datetime.now()
    
    # 启动后台任务（spawn 持有强引用，防止常驻监控任务被垃圾回收）
    spawn(monitors.memory_monitor(
        CONFIG, server_state.DOWNLOAD_SEMAPHORE, server_state.MAX_CONCURRENT_DOWNLOADS,
        request_state.response_channels, request_state.request_metadata, image_state.IMAGE_BASE64_CACHE,
        image_state.FILEBED_URL_CACHE, image_state.FILEBED_URL_CACHE_TTL,
        image_state.downloaded_urls_set, image_state.downloaded_image_urls
    ), name="memory-monitor")
    spawn(monitors.config_monitor(
        CONFIG, CONFIG_FILE_MTIMES, load_config, load_model_endpoint_map, load_model_map,
        conn_state.browser_connections, request_state.response_channels, MODEL_ENDPOINT_MAP
    ), name="config-monitor")
    spawn(monitors.stale_request_cleaner(
        monitoring_service, request_state.response_channels, request_state.request_metadata
    ), name="stale-request-cleaner")
    
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
        spawn(_recalculate_costs_bg(), name="recalculate-costs")
    
    # 🔥 预热 admin 首屏缓存（异步后台，不阻塞启动）
    spawn(_warmup_admin_cache(), name="warmup-admin-cache")
    
    yield
    
    # 保存 API Key 统计数据
    api_key_manager.save_now()
    
    if server_state.direct_api_service:
        await server_state.direct_api_service.close()
    if server_state.aiohttp_session:
        await server_state.aiohttp_session.close()
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

# ==================== WebSocket端点 ====================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """油猴脚本 WebSocket 入口。

    🔧 修复：websocket_routes.websocket_endpoint 已重构为从 AppState 自取依赖，
    只接受 websocket 单参数。旧版传入 18 个关键字参数会直接 TypeError，
    导致浏览器永远无法建立连接。
    """
    await websocket_routes.websocket_endpoint(websocket)

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

    # Anthropic/Claude 客户端常用 x-api-key；与 Bearer 等效
    if not provided_key:
        x_api_key = request.headers.get("x-api-key")
        if x_api_key:
            provided_key = x_api_key.strip()

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
    server_state = _app_state.server
    return await api_routes.gemini_native_api(
        model_name=model_name,
        request=request,
        MODEL_ENDPOINT_MAP=MODEL_ENDPOINT_MAP,
        monitoring_service=monitoring_service,
        direct_api_service=server_state.direct_api_service,
        last_activity_time_setter=lambda dt: server_state.last_activity_time_ref.update({'time': dt}),
        aiohttp_session=server_state.aiohttp_session
    )

@app.post("/v1/chat/completions")
async def chat_completions_endpoint(request: Request):
    """处理聊天补全请求"""
    return await api_routes.chat_completions(request)

@app.post("/v1/messages")
async def anthropic_messages_endpoint(request: Request):
    """处理 Anthropic Claude 兼容消息请求"""
    return await api_routes.anthropic_messages(request)

# ==================== 内部通信端点 ====================
@app.post("/internal/start_id_capture")
async def start_id_capture_endpoint(request: Request):
    admin_state = _app_state.admin
    return await internal_routes.start_id_capture(
        request, _app_state.connection.browser_ws_ref['ws'],
        admin_state.ADMIN_CAPTURED_IDS, admin_state.ADMIN_CAPTURED_IDS_LOCK
    )

@app.post("/internal/receive_captured_ids")
async def receive_captured_ids_endpoint(request: Request):
    admin_state = _app_state.admin
    return await internal_routes.receive_captured_ids(
        request, admin_state.ADMIN_CAPTURED_IDS, admin_state.ADMIN_CAPTURED_IDS_LOCK
    )

@app.post("/update")
async def update_endpoint(request: Request):
    admin_state = _app_state.admin
    return await internal_routes.receive_captured_ids(
        request, admin_state.ADMIN_CAPTURED_IDS, admin_state.ADMIN_CAPTURED_IDS_LOCK
    )

@app.get("/api/admin/capture_status")
async def get_capture_status_endpoint():
    admin_state = _app_state.admin
    return await internal_routes.get_capture_status(
        admin_state.ADMIN_CAPTURED_IDS, admin_state.ADMIN_CAPTURED_IDS_LOCK
    )

@app.post("/api/admin/save_captured_model")
async def save_captured_model_endpoint(request: Request):
    admin_state = _app_state.admin
    return await internal_routes.save_captured_model(
        request, admin_state.ADMIN_CAPTURED_IDS, admin_state.ADMIN_CAPTURED_IDS_LOCK,
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

@app.post("/api/admin/models/delete")
async def delete_model_config_endpoint(request: Request):
    return await admin_routes.delete_model_config(request, load_model_endpoint_map)

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
    conn = _app_state.connection
    return await admin_routes.get_overview(
        monitoring_service, stats_db, MonitorConfig, conn.browser_ws_ref['ws'],
        conn.browser_connections, conn.browser_connections_lock, conn.tab_connection_times,
        conn.tab_request_counts, CONFIG, MODEL_ENDPOINT_MAP
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
    return await admin_routes.test_model_keys(request, _app_state.server.direct_api_service)

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
        websocket, monitoring_service, _app_state.connection.browser_ws_ref['ws'], CONFIG
    )

@app.get("/api/monitor/stats")
async def get_monitor_stats_endpoint():
    return await monitor_routes.get_monitor_stats(
        monitoring_service, _app_state.connection.browser_ws_ref['ws'], CONFIG
    )

@app.get("/api/monitor/active")
async def get_active_requests_endpoint():
    return await monitor_routes.get_active_requests(monitoring_service)

@app.get("/api/monitor/logs/requests")
async def get_request_logs_endpoint(limit: int = 50):
    return await monitor_routes.get_request_logs(limit, monitoring_service)

@app.get("/api/monitor/logs/requests/query")
async def query_request_logs_endpoint(limit: int = 50, offset: int = 0,
                                      model: Optional[str] = None,
                                      status: Optional[str] = None,
                                      search: Optional[str] = None):
    """分页 + 过滤查询请求日志（返回 {total, items, models}）"""
    return await monitor_routes.query_request_logs(
        monitoring_service, limit, offset, model, status, search)

@app.get("/api/monitor/logs/errors")
async def get_error_logs_endpoint(limit: int = 30):
    return await monitor_routes.get_error_logs(limit, monitoring_service)

@app.get("/api/monitor/recent")
async def get_recent_data_endpoint():
    return await monitor_routes.get_recent_data(monitoring_service)

@app.get("/api/monitor/performance")
async def get_performance_metrics_endpoint():
    # 🔧 修复：旧版按位置传参且顺序与函数签名完全错位（CONFIG 被当成
    # MAX_CONCURRENT_DOWNLOADS、int 被当成 aiohttp_session 等），改用关键字传参
    server_state = _app_state.server
    image_state = _app_state.image
    return await monitor_routes.get_performance_metrics(
        MAX_CONCURRENT_DOWNLOADS=server_state.MAX_CONCURRENT_DOWNLOADS,
        DOWNLOAD_SEMAPHORE=server_state.DOWNLOAD_SEMAPHORE,
        aiohttp_session=server_state.aiohttp_session,
        IMAGE_BASE64_CACHE=image_state.IMAGE_BASE64_CACHE,
        IMAGE_CACHE_MAX_SIZE=image_state.IMAGE_CACHE_MAX_SIZE,
        downloaded_urls_set=image_state.downloaded_urls_set,
        response_channels=_app_state.request.response_channels,
        DISABLED_ENDPOINTS=image_state.DISABLED_ENDPOINTS,
        CONFIG=CONFIG,
    )

@app.get("/api/monitor/tabs")
async def get_tab_connections_endpoint():
    conn = _app_state.connection
    return await monitor_routes.get_tab_connections(
        conn.browser_connections, conn.browser_connections_lock, conn.tab_connection_times,
        conn.tab_request_counts
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
            # WebSocket 握手阶段直接关闭；ASGI 服务器会将未 accept 的 close 映射为握手失败
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
async def login_page():
    """返回登录页面（HTML 已外置到 login.html，next 参数由页面 JS 读取并校验）"""
    try:
        with open('login.html', 'r', encoding='utf-8') as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>登录页面未找到</h1><p>请确保 login.html 文件在服务器根目录。</p>",
            status_code=404
        )


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
    
    logger.info(f"🚀 LMArena Bridge v2.0 API 服务器正在启动（Hypercorn / HTTP/2）...")
    logger.info(f"   - 端口: {api_port}")
    logger.info(f"   - 协议: HTTP/1.1 + HTTP/2 (h2c)")
    
    # 检查是否配置了web访问密钥
    if CONFIG.get("web_access_key"):
        logger.info(f"   - Web访问保护: ✅ 已启用")
    else:
        logger.info(f"   - Web访问保护: ❌ 未配置（任何人可访问管理面板）")
    
    # Hypercorn 配置
    hc_config = HypercornConfig()
    hc_config.keep_alive_timeout = CONFIG.get("connection_pool", {}).get("keepalive_timeout", 30)
    hc_config.graceful_timeout = CONFIG.get("timeout_graceful_shutdown", 10)
    
    if enable_ipv6:
        hc_config.bind = [f"0.0.0.0:{api_port}", f"[::]:{api_port}"]
        logger.info(f"   - 双栈模式: ✅ IPv4 + IPv6")
    else:
        hc_config.bind = [f"0.0.0.0:{api_port}"]
    
    logger.info(f"   - IPv4访问: http://127.0.0.1:{api_port}")
    if enable_ipv6:
        logger.info(f"   - IPv6访问: http://[::1]:{api_port}")
    
    def _run():
        """启动 Hypercorn（asyncio 模式，支持 HTTP/2 h2c）"""
        asyncio.run(hypercorn_serve(app, hc_config))
    
    try:
        _run()
    except KeyboardInterrupt:
        logger.info("服务器已收到中断信号，正在关闭...")
    except Exception as e:
        logger.error(f"服务器启动失败: {e}", exc_info=True)
        sys.exit(1)