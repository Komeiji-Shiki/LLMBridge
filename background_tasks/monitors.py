"""
后台监控任务
包含内存监控、配置文件监控、活跃请求清理、日志保留清理等
"""
import asyncio
import gc
import logging
import os
import psutil
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from core.config_loader import get_float_setting
from core.constants import (
    TimeoutDefaults,
    CacheDefaults,
    MemoryDefaults,
    ServerDefaults,
)

logger = logging.getLogger(__name__)

# 🔧 配置接线：管理面板（js/admin-config.js）会把这三个轮询间隔写入
# config.jsonc 的 background_tasks 段，但此前三个任务都直接用 ServerDefaults
# 里的常量，用户调完保存后毫无效果。改为每轮循环读取，同时跟随热重载生效。
_INTERVAL_KEYS = {
    "config_monitor": ("background_tasks.config_monitor_interval", ServerDefaults.CONFIG_MONITOR_INTERVAL),
    "memory_monitor": ("background_tasks.memory_monitor_interval", ServerDefaults.MEMORY_MONITOR_INTERVAL),
    "stale_cleaner": ("background_tasks.stale_cleaner_interval", ServerDefaults.STALE_CLEANER_INTERVAL),
}


def _interval(task: str) -> float:
    """读取指定后台任务的轮询间隔（秒），非法值回退到默认常量。"""
    path, default = _INTERVAL_KEYS[task]
    return get_float_setting(path, default)


def _purge_expired_logs(monitoring_service) -> None:
    """同步清理逻辑（在线程池中执行，不阻塞事件循环）：
    - 删除 logs/ 下超过 MAX_LOG_DAYS 的日期目录（YYYYMMDD 命名，分层 JSON 日志）
    - 删除 SQLite 中超过 MAX_DB_DAYS 的请求记录（保留期更长，避免长期费用统计缩水）
    """
    from modules.monitoring import MonitorConfig

    if MonitorConfig.MAX_LOG_DAYS <= 0 and MonitorConfig.MAX_DB_DAYS <= 0:
        return  # 用户批准永久保留业务日志，不能按日期自动删除。

    cutoff_str = (datetime.now() - timedelta(days=MonitorConfig.MAX_LOG_DAYS)).strftime("%Y%m%d")
    log_dir = Path(MonitorConfig.LOG_DIR)
    removed_dirs = 0

    if log_dir.exists():
        for entry in log_dir.iterdir():
            # 只碰 8 位数字命名的日期目录，requests.db / stats.json 等文件不受影响
            if MonitorConfig.MAX_LOG_DAYS > 0 and entry.is_dir() and re.fullmatch(r"\d{8}", entry.name) and entry.name < cutoff_str:
                shutil.rmtree(entry, ignore_errors=True)
                removed_dirs += 1

    deleted_rows = 0
    sqlite_logger = getattr(monitoring_service.log_manager, "sqlite_logger", None)
    if sqlite_logger is not None and MonitorConfig.MAX_DB_DAYS > 0:
        deleted_rows = sqlite_logger.purge_old_records(MonitorConfig.MAX_DB_DAYS)

    if removed_dirs or deleted_rows:
        logger.info(f"[LOG_CLEANER] 🧹 清理完成: 删除 {removed_dirs} 个过期日期目录, "
                    f"{deleted_rows} 条过期 SQLite 记录")
    else:
        logger.debug("[LOG_CLEANER] 无过期日志需要清理")


async def log_retention_cleaner(monitoring_service):
    """每日日志保留清理任务。

    MonitorConfig 中的 MAX_LOG_DAYS/MAX_DB_DAYS 此前只是定义了策略而没有执行者，
    logs 目录会无限增长（每请求一个 JSON 文件 + SQLite 行）。
    """
    from modules.monitoring import MonitorConfig

    logger.info(f"[LOG_CLEANER] 日志保留清理任务已启动"
                f"（日志文件保留 {MonitorConfig.MAX_LOG_DAYS} 天, SQLite 保留 {MonitorConfig.MAX_DB_DAYS} 天）")

    # 启动后延迟 5 分钟执行首轮，避免与启动期任务（费用重算、缓存预热）抓 IO
    await asyncio.sleep(300)
    while True:
        try:
            await asyncio.to_thread(_purge_expired_logs, monitoring_service)
        except Exception as e:
            logger.error(f"[LOG_CLEANER] 错误: {e}", exc_info=True)
        await asyncio.sleep(24 * 3600)


async def api_key_stats_saver(interval_seconds: int = 300):
    """周期性把 API Key 使用统计落盘。

    validate_request 属于热路径，只更新内存统计不写盘；
    旧版只在优雅关闭时 save_now()，强杀进程会丢失全部统计。
    """
    from core.api_key_manager import api_key_manager
    logger.info("[APIKEY_SAVER] API Key 统计落盘任务已启动")
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(api_key_manager.save_if_dirty)
        except Exception as e:
            logger.error(f"[APIKEY_SAVER] 落盘失败: {e}", exc_info=True)


async def conversation_cache_cleaner():
    from core.conversation_store import conversation_store
    while True:
        try:
            await asyncio.to_thread(conversation_store.cleanup)
        except Exception:
            logger.exception('会话缓存清理失败')
        await asyncio.sleep(3600)


async def stale_request_cleaner(monitoring_service, response_channels: Optional[dict] = None, request_metadata: Optional[dict] = None):
    """
    核心修复：定期清理超时的活跃请求
    防止请求因异常而永久卡在"处理中"状态
    """
    logger.info("[STALE_CLEANER] 活跃请求清理任务已启动")
    
    while True:
        try:
            await asyncio.sleep(_interval("stale_cleaner"))
            
            cleaned_count, stale_request_ids = monitoring_service.cleanup_stale_requests()
            
            if stale_request_ids and (response_channels is not None or request_metadata is not None):
                for req_id in stale_request_ids:
                    if response_channels is not None:
                        # 先向队列投递错误，避免消费者永久挂起
                        queue = response_channels.get(req_id)
                        if queue is not None:
                            try:
                                queue.put_nowait({"error": "Request cleaned up by stale request cleaner (timeout)"})
                            except Exception:
                                pass
                        response_channels.pop(req_id, None)
                        logger.debug(f"[STALE_CLEANER] 已清理响应通道: {req_id[:8]}")
                    if request_metadata is not None:
                        # 释放标签页计数
                        from core.load_balancer import release_tab
                        tab_id = request_metadata[req_id].get("tab_id") if req_id in request_metadata else None
                        request_metadata.pop(req_id, None)
                        if tab_id:
                            try:
                                await release_tab(tab_id)
                            except Exception as e:
                                logger.debug(f"[STALE_CLEANER] release_tab 失败 (tab={tab_id}): {e}")
                        logger.debug(f"[STALE_CLEANER] 已清理请求元数据: {req_id[:8]}")
            
            if cleaned_count > 0:
                logger.warning(f"[STALE_CLEANER] ⚠️ 清理了 {cleaned_count} 个超时的活跃请求（含关联资源）")
                await monitoring_service.broadcast_to_monitors({
                    "type": "stale_requests_cleaned",
                    "count": cleaned_count,
                    "timestamp": time.time()
                })
            else:
                logger.debug(f"[STALE_CLEANER] 检查完成，当前活跃请求: {len(monitoring_service.active_requests)}")
                
        except Exception as e:
            logger.error(f"[STALE_CLEANER] 错误: {e}", exc_info=True)


async def config_monitor(CONFIG, CONFIG_FILE_MTIMES, load_config_func, load_model_endpoint_map_func, load_model_map_func, browser_connections, response_channels, MODEL_ENDPOINT_MAP):
    """定期监控配置文件的变化并报告"""
    logger.info("[CONFIG_MONITOR] 配置文件监控任务已启动")
    
    while True:
        try:
            await asyncio.sleep(_interval("config_monitor"))
            
            current_time = time.time()
            config_changes = []
            
            # 🔧 配置重载是同步文件 IO + threading.Lock，移入线程池避免卡事件循环
            try:
                config_mtime = os.path.getmtime('config.jsonc')
                if config_mtime != CONFIG_FILE_MTIMES.get('config.jsonc', 0):
                    await asyncio.to_thread(load_config_func)
                    config_changes.append(f"config.jsonc (修改于 {datetime.fromtimestamp(config_mtime).strftime('%H:%M:%S')})")
            except FileNotFoundError:
                pass
            
            try:
                map_mtime = os.path.getmtime('model_endpoint_map.json')
                if map_mtime != CONFIG_FILE_MTIMES.get('model_endpoint_map.json', 0):
                    await asyncio.to_thread(load_model_endpoint_map_func)
                    config_changes.append(f"model_endpoint_map.json (修改于 {datetime.fromtimestamp(map_mtime).strftime('%H:%M:%S')})")
            except FileNotFoundError:
                pass
            
            try:
                models_mtime = os.path.getmtime('models.json')
                if models_mtime != CONFIG_FILE_MTIMES.get('models.json', 0):
                    await asyncio.to_thread(load_model_map_func)
                    CONFIG_FILE_MTIMES['models.json'] = models_mtime
                    config_changes.append(f"models.json (修改于 {datetime.fromtimestamp(models_mtime).strftime('%H:%M:%S')})")
            except FileNotFoundError:
                pass
            
            if config_changes:
                logger.info(f"[CONFIG_MONITOR] 🔄 检测到配置文件更新: {', '.join(config_changes)}")
                logger.info(f"[CONFIG_MONITOR] ✅ 配置已自动重新加载")
            else:
                logger.debug(f"[CONFIG_MONITOR] 配置文件无变化 | "
                           f"browser_connections: {len(browser_connections)} | "
                           f"response_channels: {len(response_channels)} | "
                           f"model_endpoints: {len(MODEL_ENDPOINT_MAP)}")
            
        except Exception as e:
            logger.error(f"[CONFIG_MONITOR] 错误: {e}", exc_info=True)


async def memory_monitor(
    CONFIG,
    DOWNLOAD_SEMAPHORE,
    MAX_CONCURRENT_DOWNLOADS,
    response_channels,
    request_metadata,
    IMAGE_BASE64_CACHE,
    FILEBED_URL_CACHE,
    FILEBED_URL_CACHE_TTL,
    downloaded_urls_set,
    downloaded_image_urls
):
    """
    分层内存监控任务
    - 轻量清理：每轮都执行（TTLCache expire、下载记录清理、元数据清理）
    - 重量GC：仅在内存超阈值时触发（tokenizer卸载、GC回收）
    """
    process = psutil.Process(os.getpid())
    last_gc_time = time.time()
    
    while True:
        try:
            await asyncio.sleep(_interval("memory_monitor"))

            # psutil 在 Windows 上单次 memory_info() 可能耗时 10-50ms，
            # 与 monitor_routes.get_memory_info 保持一致丢线程池，不占事件循环
            memory_info = await asyncio.to_thread(process.memory_info)
            memory_mb = memory_info.rss / (1024 * 1024)

            active_downloads = MAX_CONCURRENT_DOWNLOADS - DOWNLOAD_SEMAPHORE._value if DOWNLOAD_SEMAPHORE else 0
            
            logger.info(f"[MEM_MONITOR] 内存: {memory_mb:.2f}MB | "
                       f"活跃下载: {active_downloads}/{MAX_CONCURRENT_DOWNLOADS} | "
                       f"响应通道: {len(response_channels)} | "
                       f"请求元数据: {len(request_metadata)} | "
                       f"缓存图片: {len(IMAGE_BASE64_CACHE)} | "
                       f"图床URL缓存: {len(FILEBED_URL_CACHE)} | "
                       f"下载历史: {len(downloaded_urls_set)}")
            
            # ========== 轻量清理（每次检查都执行） ==========
            mem_config = CONFIG.get("memory_management", {})
            cache_config = CONFIG.get("cache_settings", mem_config.get("cache_config", {}))
            
            # TTLCache 过期驱逐
            IMAGE_BASE64_CACHE.expire()
            FILEBED_URL_CACHE.expire()
            
            # 清理超量的 downloaded_urls_set（不等到GC阈值）
            url_history_max = cache_config.get("url_history_max", CacheDefaults.URL_HISTORY_MAX)
            url_history_keep = cache_config.get("url_history_keep", CacheDefaults.URL_HISTORY_KEEP)
            if len(downloaded_urls_set) > url_history_max:
                items_before = len(downloaded_urls_set)
                downloaded_urls_set.clear()
                downloaded_urls_set.update(list(downloaded_image_urls)[-url_history_keep:])
                logger.info(f"[MEM_MONITOR] 🧹 清理下载记录: {items_before} -> {len(downloaded_urls_set)}")
            
            # 监控和清理超时的请求元数据
            if len(request_metadata) > 10:
                current_time = datetime.now()
                timeout_threshold = CONFIG.get("metadata_timeout_minutes", TimeoutDefaults.METADATA_TIMEOUT_MINUTES)
                stale_request_ids = []
                
                for req_id, metadata in request_metadata.items():
                    created_at_str = metadata.get("created_at")
                    if created_at_str:
                        try:
                            created_at = datetime.fromisoformat(created_at_str)
                            age_minutes = (current_time - created_at).total_seconds() / 60
                            if age_minutes > timeout_threshold:
                                stale_request_ids.append(req_id)
                        except (ValueError, TypeError):
                            stale_request_ids.append(req_id)
                
                for req_id in stale_request_ids:
                    request_metadata.pop(req_id, None)
                    if response_channels is not None:
                        response_channels.pop(req_id, None)
                
                if stale_request_ids:
                    logger.info(f"[MEM_MONITOR] 已清理 {len(stale_request_ids)} 个超时的请求元数据")
            
            # ========== 重量GC（仅在超阈值时触发） ==========
            gc_threshold = mem_config.get("gc_threshold_mb", MemoryDefaults.GC_THRESHOLD_MB)
            
            if memory_mb > gc_threshold:
                current_time = time.time()
                # 降低GC冷却时间：2分钟（原为5分钟）
                if current_time - last_gc_time > 120:
                    logger.warning(f"[MEM_MONITOR] 触发垃圾回收 (内存: {memory_mb:.2f}MB > {gc_threshold}MB)")
                    
                    # 强清所有空闲tokenizer
                    try:
                        from modules.token_counter import clear_tokenizer_cache, get_tokenizer_memory_info
                        tokenizer_mem = get_tokenizer_memory_info()
                        if tokenizer_mem['estimated_memory_mb'] > 0:
                            logger.info(f"[MEM_MONITOR] Tokenizer缓存占用约 {tokenizer_mem['estimated_memory_mb']:.0f}MB")
                        clean_result = clear_tokenizer_cache(force=True)
                        if clean_result['count'] > 0:
                            logger.info(f"[MEM_MONITOR] ✅ 强制清理了 {clean_result['count']} 个tokenizer缓存")
                    except ImportError:
                        pass
                    except Exception as e:
                        logger.warning(f"[MEM_MONITOR] 清理tokenizer缓存时出错: {e}")
                    
                    # 图片缓存主动驱逐
                    IMAGE_BASE64_CACHE.expire()
                    if len(IMAGE_BASE64_CACHE) > mem_config.get("image_cache_keep_size", 20) * 2:
                        logger.info(f"[MEM_MONITOR] 🧹 图片缓存仍较大: {len(IMAGE_BASE64_CACHE)} 条")
                    
                    # 执行GC
                    await asyncio.to_thread(gc.collect)
                    last_gc_time = current_time

                    new_info = await asyncio.to_thread(psutil.Process(os.getpid()).memory_info)
                    new_memory_mb = new_info.rss / (1024 * 1024)
                    freed_mb = memory_mb - new_memory_mb
                    
                    logger.info(f"[MEM_MONITOR] GC后内存: {memory_mb:.2f}MB -> {new_memory_mb:.2f}MB "
                               f"(释放: {freed_mb:.2f}MB)")
                    if new_memory_mb > gc_threshold * 1.5:
                        logger.warning(f"[MEM_MONITOR] ⚠️ 内存仍高 ({new_memory_mb:.2f}MB)，可能有大对象泄漏")
                    
        except Exception as e:
            logger.error(f"[MEM_MONITOR] 错误: {e}", exc_info=True)
