import asyncio
from contextlib import asynccontextmanager

import aiohttp

from core.logging_config import logger
from core import global_state as gs

from core.config_loader import (
    CONFIG,
    MODEL_ENDPOINT_MAP,
    load_config,
    load_model_map,
    load_model_endpoint_map,
)
from core.db_stats import stats_db
from services.direct_api_service import DirectAPIService


@asynccontextmanager
async def lifespan(app):
    """
    在服务器启动时运行的生命周期函数。
    说明：
    - 这里负责初始化 aiohttp_session / DOWNLOAD_SEMAPHORE / direct_api_service
    - 启动后台任务（内存监控、配置监控、stale清理等）会在后续拆分的 tasks/ 中实现
    """
    gs.main_event_loop = asyncio.get_running_loop()

    load_config()

    # 从配置中读取并发和连接池设置
    gs.MAX_CONCURRENT_DOWNLOADS = CONFIG.get("max_concurrent_downloads", 50)
    pool_config = CONFIG.get("connection_pool", {})

    # 创建自定义SSL上下文（修复CloudFlare R2的SSL连接问题）
    import ssl
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    logger.info("已创建自定义SSL上下文（禁用证书验证以提高连接稳定性）")

    connector = aiohttp.TCPConnector(
        ssl=ssl_context,
        limit=pool_config.get("total_limit", 200),
        limit_per_host=pool_config.get("per_host_limit", 50),
        ttl_dns_cache=pool_config.get("dns_cache_ttl", 300),
        force_close=False,
        enable_cleanup_closed=True,
        keepalive_timeout=pool_config.get("keepalive_timeout", 30),
    )

    timeout_config = CONFIG.get("download_timeout", {})
    timeout = aiohttp.ClientTimeout(
        total=timeout_config.get("total", 30),
        connect=timeout_config.get("connect", 5),
        sock_read=timeout_config.get("sock_read", 10),
    )

    gs.aiohttp_session = aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        trust_env=True,
    )

    gs.direct_api_service = DirectAPIService(gs.aiohttp_session)
    gs.DOWNLOAD_SEMAPHORE = asyncio.Semaphore(gs.MAX_CONCURRENT_DOWNLOADS)

    logger.info("全局aiohttp会话已创建（优化配置）")
    logger.info(f"  - 最大连接数: {pool_config.get('total_limit', 200)}")
    logger.info(f"  - 每主机连接数: {pool_config.get('per_host_limit', 50)}")
    logger.info(f"  - 最大并发下载: {gs.MAX_CONCURRENT_DOWNLOADS}")
    logger.info("Direct API服务已初始化")

    # 打印模式信息（保留原逻辑）
    mode = CONFIG.get("id_updater_last_mode", "direct_chat")
    target = CONFIG.get("id_updater_battle_target", "A")
    logger.info("=" * 60)
    logger.info(f"当前操作模式: {mode.upper()}")
    if mode == "battle":
        logger.info(f"  - Battle 模式目标: Assistant {target}")
    logger.info("(可通过运行 id_updater.py 修改模式)")
    logger.info("=" * 60)
    logger.info("监控面板: http://127.0.0.1:5102/admin")
    logger.info("=" * 60)

    load_model_map()
    load_model_endpoint_map()

    logger.info("服务器启动完成。等待油猴脚本连接...")

    gs.last_activity_time = __import__("datetime").datetime.now()

    # 启动后台任务（后续会提供 tasks/ 对应实现）
    from tasks.background import memory_monitor, config_monitor, stale_request_cleaner
    asyncio.create_task(memory_monitor())
    asyncio.create_task(config_monitor())
    asyncio.create_task(stale_request_cleaner())

    # 启动空闲监控线程（后续会提供 tasks/idle_restart.py）
    if CONFIG.get("enable_idle_restart", False):
        from tasks.idle_restart import start_idle_monitor_thread
        start_idle_monitor_thread()

    # 启动时重新计算费用（如果启用了SQLite并且有计费配置）
    if stats_db.enabled and MODEL_ENDPOINT_MAP:
        try:
            logger.info("=" * 60)
            logger.info("开始重新计算所有请求的费用...")
            recalculated = stats_db.recalculate_costs(MODEL_ENDPOINT_MAP)
            if recalculated:
                logger.info(f"费用重算完成: 更新了 {recalculated.get('updated_count', 0)} 条记录")
                logger.info(f"  - 总成本: {recalculated.get('total_cost', 0):.4f} {recalculated.get('currency', 'USD')}")
            else:
                logger.info("没有需要重算的费用记录")
            logger.info("=" * 60)
        except Exception as e:
            logger.error(f"费用重算失败: {e}", exc_info=True)

    yield

    # 清理资源
    try:
        if gs.direct_api_service:
            await gs.direct_api_service.close()
            logger.info("Direct API服务已关闭")
    finally:
        gs.direct_api_service = None

    try:
        if gs.aiohttp_session:
            await gs.aiohttp_session.close()
            logger.info("全局aiohttp会话已关闭")
    finally:
        gs.aiohttp_session = None

    logger.info("服务器正在关闭。")