"""
图片处理服务模块
处理图片下载、优化、上传图床、缓存等功能
"""

import asyncio
import hashlib
import logging
import time
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple

import aiohttp
import requests
from PIL import Image

from modules.file_uploader import upload_to_file_bed
from modules.image_processor import (
    optimize_image,
    image_to_base64,
    get_mime_type_from_format,
    decode_base64_image,
    merge_image_config
)

logger = logging.getLogger(__name__)

# --- 图片自动下载配置 ---
IMAGE_SAVE_DIR = Path("./downloaded_images")
IMAGE_SAVE_DIR.mkdir(exist_ok=True)


def calculate_image_hash(base64_data: str) -> str:
    """计算图片内容的SHA256 hash（用于缓存键）"""
    # 移除data URI前缀（如果存在）
    if ',' in base64_data:
        _, data_only = base64_data.split(',', 1)
    else:
        data_only = base64_data
    # 计算hash（使用base64字符串，避免解码开销）
    return hashlib.sha256(data_only.encode('utf-8')).hexdigest()


async def save_image_data(image_data, url, request_id, CONFIG):
    """保存图片数据到文件（异步）"""
    try:
        original_size_kb = len(image_data) / 1024
        
        # 创建日期文件夹
        date_folder = datetime.now().strftime("%Y%m%d")
        date_path = IMAGE_SAVE_DIR / date_folder
        date_path.mkdir(exist_ok=True)
        logger.info(f"📁 使用日期文件夹: {date_folder}")
        
        # 检查是否需要格式转换（本地保存）
        local_format_config = CONFIG.get("local_save_format", {})
        target_ext = 'png'  # 默认扩展名
        
        if local_format_config.get("enabled", False):
            target_format = local_format_config.get("format", "original").lower()
            
            if target_format != "original":
                try:
                    # 打开图片
                    img = Image.open(BytesIO(image_data))
                    
                    # 如果是RGBA模式且要转换为JPEG，需要先转换为RGB
                    if target_format in ['jpeg', 'jpg'] and img.mode in ('RGBA', 'LA', 'P'):
                        # 创建白色背景
                        background = Image.new('RGB', img.size, (255, 255, 255))
                        if img.mode == 'P':
                            img = img.convert('RGBA')
                        background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                        img = background
                    
                    # 保存到BytesIO
                    output = BytesIO()
                    
                    # 根据目标格式保存
                    if target_format == 'png':
                        img.save(output, format='PNG', optimize=True)
                        target_ext = 'png'
                    elif target_format in ['jpeg', 'jpg']:
                        # 本地保存使用高质量
                        jpeg_quality = local_format_config.get("jpeg_quality", 100)
                        img.save(output, format='JPEG', quality=jpeg_quality, optimize=True)
                        target_ext = 'jpg'
                    elif target_format == 'webp':
                        img.save(output, format='WEBP', quality=95, optimize=True)
                        target_ext = 'webp'
                    else:
                        # 不支持的格式，使用原始数据
                        output = BytesIO(image_data)
                        # 从URL推断扩展名
                        if '.jpeg' in url.lower():
                            target_ext = 'jpeg'
                        elif '.jpg' in url.lower():
                            target_ext = 'jpg'
                        elif '.png' in url.lower():
                            target_ext = 'png'
                        elif '.webp' in url.lower():
                            target_ext = 'webp'
                    
                    # 获取转换后的数据
                    image_data = output.getvalue()
                    
                    converted_size_kb = len(image_data) / 1024
                    logger.info(f"🔄 本地保存已转换为 {target_format.upper()} 格式（{original_size_kb:.1f}KB → {converted_size_kb:.1f}KB）")
                    
                except Exception as e:
                    logger.warning(f"⚠️ 本地保存格式转换失败: {e}，使用原始格式")
                    # 从URL推断扩展名
                    if '.jpeg' in url.lower():
                        target_ext = 'jpeg'
                    elif '.jpg' in url.lower():
                        target_ext = 'jpg'
                    elif '.png' in url.lower():
                        target_ext = 'png'
                    elif '.webp' in url.lower():
                        target_ext = 'webp'
            else:
                # 保持原格式，从URL推断扩展名
                if '.jpeg' in url.lower():
                    target_ext = 'jpeg'
                elif '.jpg' in url.lower():
                    target_ext = 'jpg'
                elif '.png' in url.lower():
                    target_ext = 'png'
                elif '.webp' in url.lower():
                    target_ext = 'webp'
        else:
            # 未启用格式转换，从URL推断扩展名
            if '.jpeg' in url.lower():
                target_ext = 'jpeg'
            elif '.jpg' in url.lower():
                target_ext = 'jpg'
            elif '.png' in url.lower():
                target_ext = 'png'
            elif '.webp' in url.lower():
                target_ext = 'webp'
            elif '.' in url:
                possible_ext = url.split('.')[-1].split('?')[0].lower()
                if possible_ext in ['jpg', 'jpeg', 'png', 'gif', 'webp']:
                    target_ext = possible_ext
        
        # 生成文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]  # 添加毫秒
        
        # 使用时间戳和请求ID作为文件名
        filename = f"{timestamp}_{request_id[:8]}.{target_ext}"
        filepath = date_path / filename  # 使用日期文件夹路径
        
        # 异步保存文件
        await asyncio.get_event_loop().run_in_executor(None, filepath.write_bytes, image_data)
        
        # 计算文件大小
        size_kb = len(image_data) / 1024
        size_mb = size_kb / 1024
        
        if size_mb > 1:
            logger.info(f"✅ 图片已保存: {filename} ({size_mb:.2f}MB)")
        else:
            logger.info(f"✅ 图片已保存: {filename} ({size_kb:.1f}KB)")
        
        # 显示完整路径
        logger.info(f"   📁 保存位置: {filepath.absolute()}")
            
    except Exception as e:
        logger.error(f"❌ 保存图片失败: {e}")


async def save_downloaded_image_async(image_data, url, request_id, downloaded_urls_set, CONFIG):
    """保存已下载的图片数据到本地（避免重复下载）"""
    # 避免重复保存
    if url in downloaded_urls_set:
        show_full_urls = CONFIG.get("debug_show_full_urls", False)
        url_display = url if show_full_urls else url[:CONFIG.get("url_display_length", 200)]
        logger.info(f"🎨 图片已存在记录，跳过保存: {url_display}{'...' if not show_full_urls and len(url) > CONFIG.get('url_display_length', 200) else ''}")
        return
    
    try:
        # 直接使用已下载的数据保存，避免重复下载
        await save_image_data(image_data, url, request_id, CONFIG)
        
        # 更新已下载记录（由调用方处理）
        
    except Exception as e:
        logger.error(f"❌ 保存图片失败: {type(e).__name__}: {e}")


async def download_image_data_with_retry(
    url: str, 
    aiohttp_session: aiohttp.ClientSession,
    DOWNLOAD_SEMAPHORE: asyncio.Semaphore,
    MAX_CONCURRENT_DOWNLOADS: int,
    CONFIG: dict
) -> Tuple[Optional[bytes], Optional[str]]:
    """优化的异步图片下载器，带重试和并发控制"""
    if not DOWNLOAD_SEMAPHORE:
        DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    
    last_error = None
    max_retries = CONFIG.get("download_timeout", {}).get("max_retries", 2)
    retry_delays = [1, 2]  # 减少重试延迟
    
    # 🔍 诊断日志：并发控制状态
    semaphore_available = DOWNLOAD_SEMAPHORE._value if DOWNLOAD_SEMAPHORE else 0
    logger.info(f"[DOWNLOAD_DEBUG] 准备下载图片")
    logger.info(f"  - 可用下载槽: {semaphore_available}/{MAX_CONCURRENT_DOWNLOADS}")
    logger.info(f"  - 活跃下载: {MAX_CONCURRENT_DOWNLOADS - semaphore_available}")
    logger.info(f"  - 最大重试: {max_retries}")
    logger.info(f"  - URL前100字符: {url[:100]}...")
    
    # 🔧 下载延迟机制（避免TCP端口耗尽）
    delay_config = CONFIG.get("download_delay", {})
    if delay_config.get("enabled", False):
        delay_seconds = delay_config.get("delay_seconds", 0.5)
        logger.info(f"[DOWNLOAD_DEBUG] ⏱️ 延迟 {delay_seconds} 秒后开始（避免并发冲突）")
        await asyncio.sleep(delay_seconds)
    
    # 记录等待信号量的时间
    import time as time_module
    wait_start = time_module.time()
    
    # 使用信号量控制并发
    async with DOWNLOAD_SEMAPHORE:
        wait_time = time_module.time() - wait_start
        if wait_time > 1:
            logger.warning(f"[DOWNLOAD_DEBUG] ⚠️ 等待下载槽耗时: {wait_time:.2f}秒（并发阻塞！）")
        for retry_count in range(max_retries):
            try:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
                    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                    'Referer': 'https://lmarena.ai/'
                }
                
                if not aiohttp_session:
                    # 🔧 创建紧急会话（使用相同的SSL配置）
                    import ssl
                    ssl_context = ssl.create_default_context()
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE
                    connector = aiohttp.TCPConnector(ssl=ssl_context, limit=100, limit_per_host=30)
                    aiohttp_session = aiohttp.ClientSession(connector=connector)
                    logger.warning("[DOWNLOAD_DEBUG] 创建了紧急aiohttp会话（使用自定义SSL上下文）")
                
                # 优化的超时设置（从配置读取）
                timeout_config = CONFIG.get("download_timeout", {})
                timeout = aiohttp.ClientTimeout(
                    total=timeout_config.get("total", 30),
                    connect=timeout_config.get("connect", 5),
                    sock_read=timeout_config.get("sock_read", 10)
                )
                
                # 🔍 诊断日志：超时配置
                logger.info(f"[DOWNLOAD_DEBUG] 重试 #{retry_count + 1}/{max_retries}")
                logger.info(f"  - 连接超时: {timeout_config.get('connect', 5)}秒")
                logger.info(f"  - 读取超时: {timeout_config.get('sock_read', 10)}秒")
                logger.info(f"  - 总超时: {timeout_config.get('total', 30)}秒")
                
                # 添加性能日志
                import time as time_module
                start_time = time_module.time()
                
                # 🔍 诊断日志：连接开始
                logger.info(f"[DOWNLOAD_DEBUG] 开始建立连接...")
                
                async with aiohttp_session.get(
                    url,
                    timeout=timeout,
                    headers=headers,
                    allow_redirects=True
                ) as response:
                    connect_time = time_module.time() - start_time
                    logger.info(f"[DOWNLOAD_DEBUG] 连接建立成功，耗时: {connect_time:.2f}秒")
                    
                    if response.status == 200:
                        logger.info(f"[DOWNLOAD_DEBUG] HTTP 200 OK，开始读取数据...")
                        read_start = time_module.time()
                        data = await response.read()
                        read_time = time_module.time() - read_start
                        download_time = time_module.time() - start_time
                        
                        # 🔍 详细性能分析
                        logger.info(f"[DOWNLOAD_DEBUG] 下载完成")
                        logger.info(f"  - 连接时间: {connect_time:.2f}秒")
                        logger.info(f"  - 读取时间: {read_time:.2f}秒")
                        logger.info(f"  - 总时间: {download_time:.2f}秒")
                        logger.info(f"  - 数据大小: {len(data) / 1024:.1f}KB")
                        logger.info(f"  - 下载速度: {(len(data) / 1024) / download_time:.1f}KB/s")
                        
                        # 记录慢速下载
                        slow_threshold = CONFIG.get("performance_monitoring", {}).get("slow_threshold_seconds", 10)
                        if download_time > slow_threshold:
                            logger.warning(f"[DOWNLOAD] ⚠️ 下载耗时较长: {download_time:.2f}秒 (阈值: {slow_threshold}秒)")
                        
                        return data, None
                    else:
                        last_error = f"HTTP {response.status}"
                        logger.error(f"[DOWNLOAD_DEBUG] ❌ HTTP错误: {response.status}")
                        
            except asyncio.TimeoutError as e:
                elapsed = time_module.time() - start_time
                last_error = f"超时（第{retry_count+1}次尝试）"
                logger.error(f"[DOWNLOAD_DEBUG] ❌ 超时")
                logger.error(f"  - 已等待: {elapsed:.2f}秒")
                logger.error(f"  - 配置总超时: {timeout_config.get('total', 30)}秒")
                logger.error(f"  - 可能原因: 网络慢、服务器响应慢、或数据量大")
            except aiohttp.ClientError as e:
                elapsed = time_module.time() - start_time
                last_error = f"网络错误: {str(e)}"
                logger.error(f"[DOWNLOAD_DEBUG] ❌ 网络错误: {e.__class__.__name__}")
                logger.error(f"  - 错误详情: {str(e)[:200]}")
                logger.error(f"  - 发生时间: {elapsed:.2f}秒后")
                # 🔍 诊断SSL错误
                if "SSL" in str(e) or "ssl" in str(e).lower():
                    logger.error(f"  - 💡 检测到SSL错误，可能是证书问题或防火墙拦截")
            except Exception as e:
                elapsed = time_module.time() - start_time
                last_error = str(e)
                logger.error(f"[DOWNLOAD_DEBUG] ❌ 未知错误: {e}")
                logger.error(f"  - 错误类型: {type(e).__name__}")
                logger.error(f"  - 发生时间: {elapsed:.2f}秒后")
            
            # 重试延迟
            if retry_count < max_retries - 1:
                delay = retry_delays[retry_count]
                logger.info(f"[DOWNLOAD_DEBUG] 等待{delay}秒后重试...")
                await asyncio.sleep(delay)
    
    return None, last_error


async def process_image_data(
    base64_data: str,
    filename: str = None,
    request_id: str = None,
    CONFIG: dict = None,
    PROCESSED_IMAGE_CACHE: dict = None,
    DISABLED_ENDPOINTS: dict = None,
    ROUND_ROBIN_INDEX_REF: list = None,  # 使用列表作为可变引用
    FILEBED_RECOVERY_TIME: int = 300,
    model_image_config: dict = None  # 新增：模型级别的图片压缩配置
) -> Tuple[str, Optional[str]]:
    """
    统一的图片处理函数，根据配置决定处理流程
    
    支持四种配置组合：
    1. file_bed_enabled=True + image_optimization.enabled=True: 优化 -> 图床URL
    2. file_bed_enabled=True + image_optimization.enabled=False: 原图 -> 图床URL
    3. file_bed_enabled=False + image_optimization.enabled=True: 优化 -> base64
    4. file_bed_enabled=False + image_optimization.enabled=False: 原图 -> base64
    
    Args:
        base64_data: 原始base64图片数据（可以是Data URI格式）
        filename: 可选的文件名
        request_id: 请求ID（用于日志）
        CONFIG: 配置字典
        PROCESSED_IMAGE_CACHE: 处理图片缓存字典
        DISABLED_ENDPOINTS: 禁用端点字典
        ROUND_ROBIN_INDEX_REF: 轮询索引引用（列表形式）
        FILEBED_RECOVERY_TIME: 图床恢复时间
        model_image_config: 模型级别的图片压缩配置，优先级高于全局配置
            示例: {
                "enabled": true,
                "convert_png_to_jpg": true,
                "target_format": "jpg",
                "quality": 80,
                "target_size_kb": 500,
                "max_width": 1920,
                "max_height": 1080
            }
        
    Returns:
        (处理后的数据, 错误信息)
        - 如果成功，返回 (URL或base64字符串, None)
        - 如果失败，返回 (原始数据, 错误消息)
    """
    if not filename:
        filename = f"image_{uuid.uuid4()}.png"
    
    req_log = f"[IMG_PROC {request_id[:8] if request_id else 'N/A'}]"
    
    # 读取全局配置
    file_bed_enabled = CONFIG.get("file_bed_enabled", False)
    global_optimization_config = CONFIG.get("image_optimization", {})
    cache_config = CONFIG.get("processed_image_cache", {})
    cache_enabled = cache_config.get("enabled", True)
    
    # 合并模型级别配置（模型配置优先级更高）
    optimization_config = merge_image_config(global_optimization_config, model_image_config)
    optimization_enabled = optimization_config.get("enabled", False)
    
    # 如果模型配置中显式启用了压缩，覆盖全局设置
    if model_image_config and model_image_config.get("enabled", False):
        optimization_enabled = True
    
    logger.info(f"{req_log} 开始处理图片: file_bed={file_bed_enabled}, optimization={optimization_enabled}, cache={cache_enabled}")
    if model_image_config:
        logger.info(f"{req_log} 使用模型级别图片配置: {model_image_config}")
    
    # --- 缓存逻辑 ---
    image_hash = None
    if cache_enabled and PROCESSED_IMAGE_CACHE is not None:
        image_hash = calculate_image_hash(base64_data)
        current_time = time.time()
        
        # 检查缓存
        if image_hash in PROCESSED_IMAGE_CACHE:
            cached_data, cache_time = PROCESSED_IMAGE_CACHE[image_hash]
            ttl = cache_config.get("ttl_seconds", 3600)
            if current_time - cache_time < ttl:
                logger.info(f"{req_log} ⚡ 命中缓存 (hash: {image_hash[:8]}...)")
                return cached_data, None
            else:
                logger.info(f"{req_log} 缓存已过期 (hash: {image_hash[:8]}...)")
                del PROCESSED_IMAGE_CACHE[image_hash]
    
    try:
        # 解码base64数据
        image_bytes, image_format, decode_error = decode_base64_image(base64_data)
        if decode_error:
            logger.error(f"{req_log} 解码失败: {decode_error}")
            return base64_data, decode_error
        
        # 确定处理后的图片数据和格式
        final_image_data = image_bytes
        final_format = image_format
        
        # 步骤1: 图片优化（如果启用）
        if optimization_enabled:
            logger.info(f"{req_log} 执行图片优化...")
            optimized_data, optimized_format, opt_error = optimize_image(
                image_bytes, 
                optimization_config,
                image_format
            )
            
            if opt_error:
                logger.warning(f"{req_log} 优化失败: {opt_error}，使用原图")
                # 优化失败，降级使用原图
            else:
                final_image_data = optimized_data
                final_format = optimized_format
                logger.info(f"{req_log} 优化成功: {len(image_bytes)/1024:.1f}KB -> {len(final_image_data)/1024:.1f}KB")
        else:
            logger.info(f"{req_log} 跳过图片优化（配置已禁用）")
        
        # 步骤2: 根据file_bed_enabled决定输出方式
        if file_bed_enabled:
            logger.info(f"{req_log} 上传到图床...")
            
            # 将图片数据转换为base64 Data URI用于上传
            mime_type = get_mime_type_from_format(final_format)
            upload_base64 = image_to_base64(final_image_data, mime_type)
            
            # 获取活跃的图床端点
            all_endpoints = CONFIG.get("file_bed_endpoints", [])
            current_time = time.time()
            
            # 自动恢复超时的端点
            if DISABLED_ENDPOINTS is not None:
                endpoints_to_recover = []
                for endpoint_name, disable_time in list(DISABLED_ENDPOINTS.items()):
                    if current_time - disable_time > FILEBED_RECOVERY_TIME:
                        endpoints_to_recover.append(endpoint_name)
                
                for endpoint_name in endpoints_to_recover:
                    del DISABLED_ENDPOINTS[endpoint_name]
                    logger.info(f"{req_log} 图床端点 '{endpoint_name}' 已自动恢复")
                
                active_endpoints = [ep for ep in all_endpoints if ep.get("enabled") and ep.get("name") not in DISABLED_ENDPOINTS]
            else:
                active_endpoints = [ep for ep in all_endpoints if ep.get("enabled")]
            
            if not active_endpoints:
                error_msg = "没有可用的图床端点"
                logger.error(f"{req_log} {error_msg}，降级返回base64")
                # 降级：返回base64
                mime_type = get_mime_type_from_format(final_format)
                return image_to_base64(final_image_data, mime_type), None
            
            # 根据策略选择端点
            import random
            strategy = CONFIG.get("file_bed_selection_strategy", "random")
            
            if ROUND_ROBIN_INDEX_REF and len(ROUND_ROBIN_INDEX_REF) > 0:
                ROUND_ROBIN_INDEX = ROUND_ROBIN_INDEX_REF[0]
                
                if strategy == "failover":
                    start_index = ROUND_ROBIN_INDEX % len(active_endpoints)
                    endpoints_to_try = active_endpoints[start_index:] + active_endpoints[:start_index]
                elif strategy == "round_robin":
                    start_index = ROUND_ROBIN_INDEX % len(active_endpoints)
                    endpoints_to_try = active_endpoints[start_index:] + active_endpoints[:start_index]
                    ROUND_ROBIN_INDEX_REF[0] = ROUND_ROBIN_INDEX + 1
                else:  # random
                    endpoints_to_try = random.sample(active_endpoints, len(active_endpoints))
            else:
                # 没有轮询索引引用，使用随机策略
                endpoints_to_try = random.sample(active_endpoints, len(active_endpoints))
            
            # 尝试上传
            upload_successful = False
            last_error = None
            final_url = None
            
            for i, endpoint in enumerate(endpoints_to_try):
                endpoint_name = endpoint.get("name", "Unknown")
                
                if DISABLED_ENDPOINTS is not None and endpoint_name in DISABLED_ENDPOINTS:
                    continue
                
                logger.info(f"{req_log} 尝试上传到 '{endpoint_name}'...")
                
                uploaded_url, upload_error = await upload_to_file_bed(
                    file_name=filename,
                    file_data=upload_base64,
                    endpoint=endpoint
                )
                
                if not upload_error:
                    final_url = uploaded_url
                    upload_successful = True
                    logger.info(f"{req_log} 上传成功到 '{endpoint_name}': {uploaded_url[:100]}...")
                    break
                else:
                    logger.warning(f"{req_log} 上传失败到 '{endpoint_name}': {upload_error}")
                    if DISABLED_ENDPOINTS is not None:
                        DISABLED_ENDPOINTS[endpoint_name] = time.time()
                    last_error = upload_error
                    
                    if strategy == "failover" and i == 0 and ROUND_ROBIN_INDEX_REF:
                        ROUND_ROBIN_INDEX_REF[0] += 1
                        logger.info(f"{req_log} [Failover] 默认图床失败，切换到下一个")
            
            if upload_successful:
                # 存入缓存
                if cache_enabled and image_hash and PROCESSED_IMAGE_CACHE is not None:
                    PROCESSED_IMAGE_CACHE[image_hash] = (final_url, time.time())
                    logger.info(f"{req_log} 💾 结果已存入缓存 (hash: {image_hash[:8]}...)")
                    # 检查缓存大小
                    max_size = cache_config.get("max_size", 200)
                    if len(PROCESSED_IMAGE_CACHE) > max_size:
                        oldest_hash = next(iter(PROCESSED_IMAGE_CACHE))
                        del PROCESSED_IMAGE_CACHE[oldest_hash]
                        logger.info(f"{req_log} 🧹 缓存已满，移除最旧条目")
                return final_url, None
            else:
                error_msg = f"所有图床端点均上传失败。最后错误: {last_error}"
                logger.error(f"{req_log} {error_msg}，降级返回base64")
                # 降级：返回base64
                mime_type = get_mime_type_from_format(final_format)
                base64_result = image_to_base64(final_image_data, mime_type)
                
                # 即使失败也要缓存降级结果
                if cache_enabled and image_hash and PROCESSED_IMAGE_CACHE is not None:
                    PROCESSED_IMAGE_CACHE[image_hash] = (base64_result, time.time())
                    logger.info(f"{req_log} 💾 降级结果已存入缓存 (hash: {image_hash[:8]}...)")

                return base64_result, None
        
        else:
            # 不使用图床，返回base64
            logger.info(f"{req_log} 转换为base64（图床已禁用）")
            mime_type = get_mime_type_from_format(final_format)
            base64_result = image_to_base64(final_image_data, mime_type)
            logger.info(f"{req_log} Base64转换完成: {len(base64_result)} 字符")
            
            # 存入缓存
            if cache_enabled and image_hash and PROCESSED_IMAGE_CACHE is not None:
                PROCESSED_IMAGE_CACHE[image_hash] = (base64_result, time.time())
                logger.info(f"{req_log} 💾 结果已存入缓存 (hash: {image_hash[:8]}...)")
                # 检查缓存大小
                max_size = cache_config.get("max_size", 200)
                if len(PROCESSED_IMAGE_CACHE) > max_size:
                    # 简单地移除最旧的条目 (LRU-like)
                    oldest_hash = next(iter(PROCESSED_IMAGE_CACHE))
                    del PROCESSED_IMAGE_CACHE[oldest_hash]
                    logger.info(f"{req_log} 🧹 缓存已满，移除最旧条目")

            return base64_result, None
    
    except Exception as e:
        error_msg = f"图片处理异常: {type(e).__name__}: {e}"
        logger.error(f"{req_log} {error_msg}", exc_info=True)
        # 降级：返回原始数据
        return base64_data, error_msg