"""
后台监控任务
包含内存监控、配置文件监控、活跃请求清理等
"""
import asyncio
import gc
import logging
import os
import psutil
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


async def stale_request_cleaner(monitoring_service):
    """
    核心修复：定期清理超时的活跃请求
    防止请求因异常而永久卡在"处理中"状态
    """
    logger.info("[STALE_CLEANER] 活跃请求清理任务已启动")
    
    while True:
        try:
            # 每分钟检查一次
            await asyncio.sleep(60)
            
            # 调用监控服务的清理函数
            cleaned_count = monitoring_service.cleanup_stale_requests()
            
            if cleaned_count > 0:
                logger.warning(f"[STALE_CLEANER] ⚠️ 清理了 {cleaned_count} 个超时的活跃请求")
                
                # 广播清理事件到监控面板
                await monitoring_service.broadcast_to_monitors({
                    "type": "stale_requests_cleaned",
                    "count": cleaned_count,
                    "timestamp": time.time()
                })
            else:
                # 正常情况，记录DEBUG日志
                logger.debug(f"[STALE_CLEANER] 检查完成，当前活跃请求: {len(monitoring_service.active_requests)}")
                
        except Exception as e:
            logger.error(f"[STALE_CLEANER] 错误: {e}", exc_info=True)


async def config_monitor(CONFIG, CONFIG_FILE_MTIMES, load_config_func, load_model_endpoint_map_func, load_model_map_func, browser_connections, response_channels, MODEL_ENDPOINT_MAP):
    """定期监控配置文件的变化并报告"""
    logger.info("[CONFIG_MONITOR] 配置文件监控任务已启动")
    
    while True:
        try:
            # 每30秒检查一次配置文件
            await asyncio.sleep(30)
            
            current_time = time.time()
            config_changes = []
            
            # 检查 config.jsonc
            try:
                config_mtime = os.path.getmtime('config.jsonc')
                if config_mtime != CONFIG_FILE_MTIMES.get('config.jsonc', 0):
                    old_mtime = CONFIG_FILE_MTIMES.get('config.jsonc', 0)
                    # 调用 load_config() 会更新 CONFIG_FILE_MTIMES
                    load_config_func()
                    config_changes.append(f"config.jsonc (修改于 {datetime.fromtimestamp(config_mtime).strftime('%H:%M:%S')})")
            except FileNotFoundError:
                pass
            
            # 检查 model_endpoint_map.json
            try:
                map_mtime = os.path.getmtime('model_endpoint_map.json')
                if map_mtime != CONFIG_FILE_MTIMES.get('model_endpoint_map.json', 0):
                    old_mtime = CONFIG_FILE_MTIMES.get('model_endpoint_map.json', 0)
                    # 调用 load_model_endpoint_map() 会更新 CONFIG_FILE_MTIMES
                    load_model_endpoint_map_func()
                    config_changes.append(f"model_endpoint_map.json (修改于 {datetime.fromtimestamp(map_mtime).strftime('%H:%M:%S')})")
            except FileNotFoundError:
                pass
            
            # 检查 models.json
            try:
                models_mtime = os.path.getmtime('models.json')
                if models_mtime != CONFIG_FILE_MTIMES.get('models.json', 0):
                    old_mtime = CONFIG_FILE_MTIMES.get('models.json', 0)
                    # 重新加载 models.json
                    load_model_map_func()
                    CONFIG_FILE_MTIMES['models.json'] = models_mtime
                    config_changes.append(f"models.json (修改于 {datetime.fromtimestamp(models_mtime).strftime('%H:%M:%S')})")
            except FileNotFoundError:
                pass
            
            # 如果有配置变化，报告日志
            if config_changes:
                logger.info(f"[CONFIG_MONITOR] 🔄 检测到配置文件更新: {', '.join(config_changes)}")
                logger.info(f"[CONFIG_MONITOR] ✅ 配置已自动重新加载")
            else:
                # 定期报告状态（类似内存监控）
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
    """优化的内存监控任务"""
    process = psutil.Process(os.getpid())
    last_gc_time = time.time()
    
    while True:
        try:
            # 每分钟检查一次（更频繁的监控）
            await asyncio.sleep(60)
            
            # 获取内存使用情况
            memory_info = process.memory_info()
            memory_mb = memory_info.rss / (1024 * 1024)
            
            # 获取下载并发状态
            active_downloads = MAX_CONCURRENT_DOWNLOADS - DOWNLOAD_SEMAPHORE._value if DOWNLOAD_SEMAPHORE else 0
            
            # 记录内存状态（更详细的信息）
            logger.info(f"[MEM_MONITOR] 内存: {memory_mb:.2f}MB | "
                       f"活跃下载: {active_downloads}/{MAX_CONCURRENT_DOWNLOADS} | "
                       f"响应通道: {len(response_channels)} | "
                       f"请求元数据: {len(request_metadata)} | "
                       f"缓存图片: {len(IMAGE_BASE64_CACHE)} | "
                       f"图床URL缓存: {len(FILEBED_URL_CACHE)} | "
                       f"下载历史: {len(downloaded_urls_set)}")
            
            # 新增：清理过期的图床URL缓存
            if len(FILEBED_URL_CACHE) > 0:
                current_time = time.time()
                expired_hashes = []
                for img_hash, (url, cache_time) in FILEBED_URL_CACHE.items():
                    if current_time - cache_time > FILEBED_URL_CACHE_TTL:
                        expired_hashes.append(img_hash)
                
                if expired_hashes:
                    for img_hash in expired_hashes:
                        del FILEBED_URL_CACHE[img_hash]
                    logger.info(f"[MEM_MONITOR] 清理了 {len(expired_hashes)} 个过期的图床URL缓存")
            
            # 新增：监控和清理超时的请求元数据
            if len(request_metadata) > 10:  # 如果元数据过多，可能有内存泄漏
                logger.warning(f"[MEM_MONITOR] request_metadata数量较多: {len(request_metadata)}")
                logger.warning(f"[MEM_MONITOR] 开始清理超时的请求元数据...")
                
                # 实现超时清理逻辑
                current_time = datetime.now()
                timeout_threshold = CONFIG.get("metadata_timeout_minutes", 30)  # 默认30分钟超时
                stale_request_ids = []
                
                for req_id, metadata in request_metadata.items():
                    created_at_str = metadata.get("created_at")
                    if created_at_str:
                        try:
                            created_at = datetime.fromisoformat(created_at_str)
                            age_minutes = (current_time - created_at).total_seconds() / 60
                            
                            if age_minutes > timeout_threshold:
                                stale_request_ids.append(req_id)
                                logger.info(f"[MEM_MONITOR] 发现超时元数据: {req_id[:8]} (存活: {age_minutes:.1f}分钟)")
                        except (ValueError, TypeError) as e:
                            logger.warning(f"[MEM_MONITOR] 无法解析元数据时间: {req_id[:8]}, 错误: {e}")
                            stale_request_ids.append(req_id)  # 无效时间戳也清理
                
                # 清理超时的元数据
                for req_id in stale_request_ids:
                    del request_metadata[req_id]
                    # 同时清理对应的响应通道（如果还存在）
                    if req_id in response_channels:
                        del response_channels[req_id]
                        logger.debug(f"[MEM_MONITOR] 一并清理响应通道: {req_id[:8]}")
                
                if stale_request_ids:
                    logger.info(f"[MEM_MONITOR] 已清理 {len(stale_request_ids)} 个超时的请求元数据")
                else:
                    logger.info(f"[MEM_MONITOR] 未发现超时元数据，但数量仍然较多，可能是正常情况")
            else:
                logger.debug(f"[MEM_MONITOR] request_metadata: {len(request_metadata)}")
            
            # 从配置读取内存管理阈值
            mem_config = CONFIG.get("memory_management", {})
            gc_threshold = mem_config.get("gc_threshold_mb", 500)
            cache_config = mem_config.get("cache_config", {})
            
            # 根据内存使用情况动态调整
            if memory_mb > gc_threshold:
                current_time = time.time()
                # 防止过于频繁的GC
                if current_time - last_gc_time > 300:  # 5分钟最多GC一次
                    logger.warning(f"[MEM_MONITOR] 触发垃圾回收 (内存: {memory_mb:.2f}MB > {gc_threshold}MB)")
                    
                    # 清理图片缓存
                    cache_max = cache_config.get("image_cache_max_size", 500)
                    cache_keep = cache_config.get("image_cache_keep_size", 200)
                    if len(IMAGE_BASE64_CACHE) > cache_max:
                        # 保留最新的指定数量
                        sorted_items = sorted(IMAGE_BASE64_CACHE.items(),
                                            key=lambda x: x[1][1], reverse=True)
                        IMAGE_BASE64_CACHE.clear()
                        for url, data in sorted_items[:cache_keep]:
                            IMAGE_BASE64_CACHE[url] = data
                        logger.info(f"[MEM_MONITOR] 清理图片缓存: {len(sorted_items)} -> {cache_keep}")
                    
                    # 清理下载记录
                    url_history_max = cache_config.get("url_history_max", 2000)
                    url_history_keep = cache_config.get("url_history_keep", 1000)
                    if len(downloaded_urls_set) > url_history_max:
                        downloaded_urls_set.clear()
                        # 保留最近的记录
                        downloaded_urls_set.update(list(downloaded_image_urls)[-url_history_keep:])
                        logger.info(f"[MEM_MONITOR] 清理下载记录: {url_history_max} -> {url_history_keep}")
                    
                    # 执行垃圾回收
                    gc.collect()
                    last_gc_time = current_time
                    
                    # 强制刷新进程对象并再次检查内存
                    # 核心修复：重新创建Process对象以获取最新内存信息
                    fresh_process = psutil.Process(os.getpid())
                    new_memory_mb = fresh_process.memory_info().rss / (1024 * 1024)
                    logger.info(f"[MEM_MONITOR] GC后内存: {memory_mb:.2f}MB -> {new_memory_mb:.2f}MB "
                               f"(释放: {memory_mb - new_memory_mb:.2f}MB)")
                    
        except Exception as e:
            logger.error(f"[MEM_MONITOR] 错误: {e}")