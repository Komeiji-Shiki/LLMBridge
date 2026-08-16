# api_server_new.py - LMArena Bridge 主入口
#
# ⚠️ 维护状态说明：LMArena 功能已经不再维护（油猴脚本桥接 / Battle 模式 /
# 会话 ID 捕获等），无需考虑相关代码维护。目前项目重点是中转 API（Direct API：
# OpenAI 兼容透传 / Anthropic 原生 / Gemini 原生转发）。
#
# 🔧 重构说明：端点定义已全部下沉到 routes/ 各模块（APIRouter 模式），
# 本文件只负责三件事：
#   1. 应用装配（中间件、静态资源、路由挂载）
#   2. 生命周期管理（lifespan：连接池、后台任务、优雅关闭）
#   3. 服务器启动（Hypercorn / HTTP/2）

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime

import aiohttp
from hypercorn.config import Config as HypercornConfig
from hypercorn.asyncio import serve as hypercorn_serve
from fastapi import FastAPI

# 日志配置必须在内部模块导入之前完成，保证所有模块的日志都走异步队列
from core.logging_config import (
    configure_async_logging, install_access_log_filters, shutdown_async_logging
)

configure_async_logging()
install_access_log_filters()
logger = logging.getLogger(__name__)

# 内部模块导入
from modules.monitoring import monitoring_service  # noqa: E402
from utils.task_registry import spawn  # noqa: E402

# Core模块
from core.config_loader import (  # noqa: E402
    CONFIG, MODEL_ENDPOINT_MAP, CONFIG_FILE_MTIMES,
    load_config, load_model_map, load_model_endpoint_map
)
from core.api_key_manager import api_key_manager  # noqa: E402
from core.db_stats import stats_db  # noqa: E402
from core.app_state import get_app_state, AppState  # noqa: E402
from core.constants import CacheDefaults  # noqa: E402
from core.middleware import (  # noqa: E402
    SelectiveGZipMiddleware, SelectiveCORSMiddleware,
    CachedStaticFiles, WebAccessKeyMiddleware
)

# Services
from services.direct_api_service import DirectAPIService  # noqa: E402

# Routes（每个模块提供自己的 APIRouter）
from routes import (  # noqa: E402
    api_routes, responses_api, models_api, websocket_routes, internal_routes,
    monitor_routes, admin_routes, auth_routes, apikey_routes
)

# Background tasks
from background_tasks import monitors  # noqa: E402

# ==================== 全局状态（AppState 单例统一管理） ====================
# 🔧 重构：不使用模块级镜像别名。
# 旧版把 AppState 字段镜像成模块级变量，对标量（布尔/数值）和会被重新赋值的
# 引用（aiohttp_session 等）只是一次性快照，赋值瞬间即与 AppState 脱钩，
# 已经造成过“人机验证状态失效”级别的 bug。现在所有状态统一走 _app_state。

_app_state: AppState = get_app_state()

logger.info("[STARTUP] ✅ 全局状态已通过 AppState 初始化")


# ==================== FastAPI 生命周期 ====================
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
        limit_per_host=pool_config.get("per_host_limit", 128),
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
    spawn(monitors.log_retention_cleaner(monitoring_service), name="log-retention-cleaner")
    # 🔧 API Key 使用统计周期落盘（旧版只在优雅关闭时保存，强杀丢全部统计）
    spawn(monitors.api_key_stats_saver(), name="apikey-stats-saver")
    # 🔧 空闲重启监控：config.jsonc 与管理面板都暴露了 enable_idle_restart，
    # 但此前没有任何执行者，配置改了不生效
    from background_tasks.request_processor import idle_monitor
    spawn(idle_monitor(server_state.last_activity_time_ref, CONFIG), name="idle-monitor")

    # 🔧 模型自动归档后台任务：每 24 小时检查一次 auto_archive 配置，
    # 启用时自动归档超过 days 天未调用的模型（手动触发走管理面板接口）
    async def _auto_archive_loop():
        first_run = True
        while True:
            try:
                aa_cfg = CONFIG.get("auto_archive") or {}
                # 首次循环：仅当 run_on_startup=true 时立即扫描；
                # 之后每个周期只要启用就扫描（配置变更时管理面板已立即执行一次）
                if aa_cfg.get("enabled") and (not first_run or aa_cfg.get("run_on_startup")):
                    days = int(aa_cfg.get("days", 30) or 30)
                    await admin_routes.run_auto_archive_task(days)
                first_run = False
            except Exception as e:
                logger.error(f"[MODEL_ARCHIVE] 后台自动归档任务失败: {e}", exc_info=True)
            await asyncio.sleep(24 * 3600)

    spawn(_auto_archive_loop(), name="auto-archive-loop")

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
    spawn(admin_routes.warmup_admin_cache(), name="warmup-admin-cache")

    yield

    # 保存 API Key 统计数据
    api_key_manager.save_now()

    if server_state.direct_api_service:
        await server_state.direct_api_service.close()
    if server_state.aiohttp_session:
        await server_state.aiohttp_session.close()
    logger.info("服务器正在关闭。")
    shutdown_async_logging()


app = FastAPI(lifespan=lifespan)

# ==================== 中间件 ====================
# add_middleware 后添加者在外层，实际执行顺序：
# WebAccessKey（鉴权，最外层）→ SelectiveGZip（压缩）→ CORS → 路由
# 🔧 安全修复：CORS 通配只对公开 API 生效（/v1、/v1beta、/internal），
# 管理/监控接口不下发跨域许可头，恶意网页无法跨域读取管理数据
app.add_middleware(
    SelectiveCORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SelectiveGZipMiddleware, minimum_size=500)
app.add_middleware(WebAccessKeyMiddleware)

# ==================== 静态资源（带缓存头） ====================
app.mount("/js", CachedStaticFiles(directory="js"), name="js")
app.mount("/css", CachedStaticFiles(directory="css"), name="css")

# ==================== 路由挂载 ====================
app.include_router(websocket_routes.router)   # /ws（油猴脚本连接）
app.include_router(api_routes.router)         # /v1/chat/completions、/v1/messages、Gemini 原生
app.include_router(responses_api.router)      # /v1/responses
app.include_router(models_api.router)         # /v1/models、/v1beta/models
app.include_router(internal_routes.router)    # /internal/*、/update、ID 捕获
app.include_router(admin_routes.router)       # /admin、/token_calculator、/api/admin/*
app.include_router(monitor_routes.router)     # /monitor、/ws/monitor、/api/monitor/*
app.include_router(auth_routes.router)        # /login、/auth/*
app.include_router(apikey_routes.router)      # /api/admin/api_keys*

# ==================== 主程序入口 ====================
if __name__ == "__main__":
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
        # ⚠️ 代价：Windows 的 select() 有 512 个 socket 的硬上限，并发连接数
        # （客户端 + 上游 + WebSocket）超过后会抛 "too many file descriptors
        # in select()"。本地桥接场景够用；高并发部署请改用 Linux 或等待
        # 上游 IOCP bug 修复后恢复 ProactorEventLoop
        if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            logger.info("🔄 已启用 WindowsSelectorEventLoop 以避免 IOCP accept bug")

    # 先加载配置以获取端口号
    # 🔧 鉴权 fail-closed：配置解析失败时 load_config 会抛出，绝不能带病启动——
    # 空配置会让鉴权中间件把所有受保护路径当作「未配置密钥」直接放行
    try:
        load_config(force_reload=True)
    except Exception as e:
        logger.critical(f"❌ 无法加载配置文件 config.jsonc，拒绝启动: {e}")
        sys.exit(1)
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
