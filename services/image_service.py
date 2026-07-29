"""
图片处理服务模块
处理图片下载、优化、上传图床、缓存等功能
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import MutableMapping, Optional, Tuple

import aiohttp
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
    """保存图片数据到文件（异步接口）。

    🔧 性能修复：PIL 的解码/重编码（尤其 optimize=True）是 CPU 密集操作，
    旧版直接跑在事件循环里（只有最轻的 write_bytes 进了线程池，顺序弄反了），
    大图会卡住所有并发流式请求。现在整体丢进线程池执行。
    """
    await asyncio.to_thread(_save_image_data_sync, image_data, url, request_id, CONFIG)


def _save_image_data_sync(image_data, url, request_id, CONFIG):
    """保存图片数据到文件（同步实现，在线程池中运行）"""
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
        
        # 保存文件（已在线程池线程中，直接同步写盘）
        filepath.write_bytes(image_data)
        
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


async def save_downloaded_image_async(image_data, url, request_id, downloaded_urls_set, CONFIG,
                                      downloaded_image_urls=None):
    """保存已下载的图片数据到本地（避免重复保存）"""
    # 避免重复保存
    if url in downloaded_urls_set:
        show_full_urls = CONFIG.get("debug_show_full_urls", False)
        url_display = url if show_full_urls else url[:CONFIG.get("url_display_length", 200)]
        logger.info(f"🎨 图片已存在记录，跳过保存: {url_display}{'...' if not show_full_urls and len(url) > CONFIG.get('url_display_length', 200) else ''}")
        return
    
    try:
        # 直接使用已下载的数据保存，避免重复下载
        await save_image_data(image_data, url, request_id, CONFIG)
        
        # 🔧 修复：写入去重记录。旧版注释称"由调用方处理"但没有任何调用方处理，
        # 去重集合永远为空，图片反复重复落盘，内存监视器也在清理空集合
        downloaded_urls_set.add(url)
        if downloaded_image_urls is not None:
            downloaded_image_urls.append(url)
        
    except Exception as e:
        logger.error(f"❌ 保存图片失败: {type(e).__name__}: {e}")


async def download_image_data_with_retry(
    url: str,
    aiohttp_session: Optional[aiohttp.ClientSession],
    DOWNLOAD_SEMAPHORE: Optional[asyncio.Semaphore],
    MAX_CONCURRENT_DOWNLOADS: int,
    CONFIG: dict
) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """优化的异步图片下载器，带重试和并发控制。

    返回值: (image_data, error, content_type)

    🔧 修复说明（相对旧版）：
    - 紧急会话泄漏：旧版在全局 session 缺失时新建 ClientSession 赋给参数
      变量后从不关闭，每次触发都泄漏连接池与 socket；现在由本函数管理
      生命周期，用完即关。
    - UnboundLocalError 隐患：start_time/timeout_config 在异常分支被引用，
      但旧版在循环体中途才赋值；现在提前初始化。
    - retry_delays 越界：max_retries 配置大于 3 时旧版 retry_delays[retry_count]
      直接 IndexError；现在用末尾值兜底。
    - 热路径逐次十几条 INFO 诊断日志降级为 debug，保留关键告警。
    """
    if not DOWNLOAD_SEMAPHORE:
        DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

    import time as time_module

    last_error = None
    timeout_config = CONFIG.get("download_timeout", {})
    max_retries = timeout_config.get("max_retries", 2)
    retry_delays = [1, 2]

    semaphore_available = DOWNLOAD_SEMAPHORE._value
    logger.debug(
        f"[DOWNLOAD] 准备下载图片: 可用槽 {semaphore_available}/{MAX_CONCURRENT_DOWNLOADS}, "
        f"最大重试 {max_retries}, URL: {url[:100]}...")

    # 🔧 下载延迟机制（避免TCP端口耗尽）
    delay_config = CONFIG.get("download_delay", {})
    if delay_config.get("enabled", False):
        await asyncio.sleep(delay_config.get("delay_seconds", 0.5))

    timeout = aiohttp.ClientTimeout(
        total=timeout_config.get("total", 30),
        connect=timeout_config.get("connect", 5),
        sock_read=timeout_config.get("sock_read", 10)
    )
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Referer': 'https://lmarena.ai/'
    }

    wait_start = time_module.time()
    temp_session: Optional[aiohttp.ClientSession] = None
    try:
        # 使用信号量控制并发
        async with DOWNLOAD_SEMAPHORE:
            wait_time = time_module.time() - wait_start
            if wait_time > 1:
                logger.warning(f"[DOWNLOAD] ⚠️ 等待下载槽耗时: {wait_time:.2f}秒（并发阻塞）")

            session = aiohttp_session
            if session is None or session.closed:
                # 全局会话不可用：创建临时会话（finally 中关闭，不再泄漏）
                # 🔧 旧版这里 check_hostname=False + CERT_NONE 关掉了证书校验，
                # 降级路径反而比正常路径更不安全；保持默认校验即可
                temp_session = aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(limit=100, limit_per_host=30))
                session = temp_session
                logger.warning("[DOWNLOAD] 全局aiohttp会话不可用，已创建临时会话（下载结束后关闭）")

            for retry_count in range(max_retries):
                start_time = time_module.time()
                try:
                    logger.debug(f"[DOWNLOAD] 尝试 #{retry_count + 1}/{max_retries}")
                    async with session.get(
                        url,
                        timeout=timeout,
                        headers=headers,
                        allow_redirects=True
                    ) as response:
                        if response.status == 200:
                            # 🔧 下载硬上限：防止超大文件撑爆内存
                            max_bytes = CONFIG.get("max_image_download_bytes", 20 * 1024 * 1024)
                            content_length = response.headers.get("Content-Length")
                            if content_length and int(content_length) > max_bytes:
                                last_error = f"图片过大 ({int(content_length)} bytes, 上限 {max_bytes})"
                                logger.error(f"[DOWNLOAD] ❌ {last_error}")
                                return None, last_error, None

                            content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
                            # 🔧 Content-Type 校验：不是图片类型直接拒绝
                            if content_type and not content_type.startswith("image/"):
                                last_error = f"非图片 Content-Type: {content_type}"
                                logger.error(f"[DOWNLOAD] ❌ {last_error}")
                                return None, last_error, None

                            # 🔧 分块读取控制：避免无 Content-Length 的流式响应无限膨胀
                            chunks = []
                            total = 0
                            async for chunk in response.content.iter_chunked(65536):
                                total += len(chunk)
                                if total > max_bytes:
                                    last_error = f"图片过大 (已读取 {total} bytes, 上限 {max_bytes})"
                                    logger.error(f"[DOWNLOAD] ❌ {last_error}")
                                    return None, last_error, None
                                chunks.append(chunk)
                            data = b"".join(chunks)
                            download_time = time_module.time() - start_time
                            logger.debug(
                                f"[DOWNLOAD] 完成: {len(data) / 1024:.1f}KB, "
                                f"{download_time:.2f}秒")

                            # 记录慢速下载
                            slow_threshold = CONFIG.get("performance_monitoring", {}).get("slow_threshold_seconds", 10)
                            if download_time > slow_threshold:
                                logger.warning(f"[DOWNLOAD] ⚠️ 下载耗时较长: {download_time:.2f}秒 (阈值: {slow_threshold}秒)")

                            return data, None, content_type
                        last_error = f"HTTP {response.status}"
                        logger.error(f"[DOWNLOAD] ❌ HTTP错误: {response.status}")

                except asyncio.TimeoutError:
                    elapsed = time_module.time() - start_time
                    last_error = f"超时（第{retry_count+1}次尝试）"
                    logger.error(
                        f"[DOWNLOAD] ❌ 超时: 已等待 {elapsed:.2f}秒 "
                        f"(配置总超时 {timeout_config.get('total', 30)}秒)")
                except aiohttp.ClientError as e:
                    elapsed = time_module.time() - start_time
                    last_error = f"网络错误: {str(e)}"
                    logger.error(
                        f"[DOWNLOAD] ❌ 网络错误: {e.__class__.__name__}: {str(e)[:200]} "
                        f"(发生于 {elapsed:.2f}秒后)")
                    if "ssl" in str(e).lower():
                        logger.error("  - 💡 检测到SSL错误，可能是证书问题或防火墙拦截")
                except Exception as e:
                    elapsed = time_module.time() - start_time
                    last_error = str(e)
                    logger.error(
                        f"[DOWNLOAD] ❌ 未知错误: {type(e).__name__}: {e} "
                        f"(发生于 {elapsed:.2f}秒后)")

                # 重试延迟（超出预设表长度时用末尾值，不再 IndexError）
                if retry_count < max_retries - 1:
                    delay = retry_delays[min(retry_count, len(retry_delays) - 1)]
                    logger.debug(f"[DOWNLOAD] 等待{delay}秒后重试...")
                    await asyncio.sleep(delay)
    finally:
        if temp_session is not None:
            await temp_session.close()

    return None, last_error, None



async def process_image_data(
    base64_data: str,
    filename: Optional[str] = None,
    request_id: Optional[str] = None,
    CONFIG: Optional[dict] = None,
    PROCESSED_IMAGE_CACHE: Optional[MutableMapping] = None,
    DISABLED_ENDPOINTS: Optional[dict] = None,
    ROUND_ROBIN_INDEX_REF: Optional[list] = None,  # 使用列表作为可变引用
    FILEBED_RECOVERY_TIME: Optional[int] = None,
    model_image_config: Optional[dict] = None  # 新增：模型级别的图片压缩配置
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
    # 🔧 修复：现役调用点均未注入这些共享状态，导致图床轮询与故障禁用
    # 机制被架空（round_robin/failover 永远从 0 开始、失败端点不会被临时
    # 禁用也无所谓恢复）。未显式传入时回落到 AppState 的全局状态；
    # 参数保留用于测试注入。
    from core.app_state import get_app_state
    _image_state = get_app_state().image
    if DISABLED_ENDPOINTS is None:
        DISABLED_ENDPOINTS = _image_state.DISABLED_ENDPOINTS
    if ROUND_ROBIN_INDEX_REF is None:
        ROUND_ROBIN_INDEX_REF = _image_state.ROUND_ROBIN_INDEX_REF
    if FILEBED_RECOVERY_TIME is None:
        # 🔧 配置接线：filebed_recovery_time_seconds 在 config.jsonc 与管理面板
        # 都可调，旧版只取 AppState 里的硬编码 300，用户改了不生效
        from core.config_loader import get_float_setting
        FILEBED_RECOVERY_TIME = get_float_setting(
            "filebed_recovery_time_seconds", _image_state.FILEBED_RECOVERY_TIME
        )

    if not filename:
        filename = f"image_{uuid.uuid4()}.png"
    
    CONFIG = CONFIG or {}
    req_log = f"[IMG_PROC {request_id[:8] if request_id else 'N/A'}]"
    
    # 读取全局配置
    file_bed_enabled = CONFIG.get("file_bed_enabled", False)
    global_optimization_config = CONFIG.get("image_optimization", {})
    cache_config = CONFIG.get("processed_image_cache", {})
    cache_enabled = cache_config.get("enabled", True)
    
    # 合并模型级别配置（模型配置优先级更高）
    optimization_config = merge_image_config(global_optimization_config, model_image_config or {})
    optimization_enabled = optimization_config.get("enabled", False)
    
    # 如果模型配置中显式启用了压缩，覆盖全局设置
    if model_image_config and model_image_config.get("enabled", False):
        optimization_enabled = True
    
    logger.info(f"{req_log} 开始处理图片: file_bed={file_bed_enabled}, optimization={optimization_enabled}, cache={cache_enabled}")
    if model_image_config:
        logger.info(f"{req_log} 使用模型级别图片配置: {model_image_config}")
    
    # --- 缓存逻辑 ---
    image_hash = None
    cache_key = None
    if cache_enabled and PROCESSED_IMAGE_CACHE is not None:
        image_hash = calculate_image_hash(base64_data)
        current_time = time.time()
        
        # 构建 variant key：缓存键必须包含处理配置，否则同一图片
        # 先后经过 LMArena（产出 URL）和 Direct API（需要 base64）时，
        # 第二条会命中第一条的缓存拿到错误形态
        variant_parts = [
            str(file_bed_enabled),
            json.dumps(optimization_config, sort_keys=True) if optimization_config else '{}'
        ]
        variant_key = hashlib.sha256(':'.join(variant_parts).encode()).hexdigest()[:16]
        cache_key = f"{image_hash}:{variant_key}"
        
        # 检查缓存（TTLCache 自动处理过期）
        cached_data = PROCESSED_IMAGE_CACHE.get(cache_key)
        if cached_data is not None:
            logger.info(f"{req_log} ⚡ 命中缓存 (hash: {image_hash[:8]}..., variant: {variant_key})")
            return cached_data, None
    
    try:
        # 解码base64数据
        image_bytes, image_format, decode_error = decode_base64_image(base64_data)
        if decode_error:
            logger.error(f"{req_log} 解码失败: {decode_error}")
            return base64_data, decode_error
        if image_bytes is None:
            logger.error(f"{req_log} 解码失败: 空数据")
            return base64_data, "图片解码失败：空数据"
        
        # 确定处理后的图片数据和格式
        final_image_data = image_bytes
        final_format = image_format
        
        # 步骤1: 图片优化（如果启用）
        if optimization_enabled:
            logger.info(f"{req_log} 执行图片优化...")
            # 🔧 性能修复：optimize_image 内部是 PIL 重编码（CPU 密集），
            # 丢进线程池执行，避免阻塞事件循环
            optimized_data, optimized_format, opt_error = await asyncio.to_thread(
                optimize_image,
                image_bytes,
                optimization_config,
                image_format
            )
            
            if opt_error:
                logger.warning(f"{req_log} 优化失败: {opt_error}，使用原图")
                # 优化失败，降级使用原图
            else:
                final_image_data = optimized_data or image_bytes
                final_format = optimized_format or image_format
                logger.info(f"{req_log} 优化成功: {len(image_bytes)/1024:.1f}KB -> {len(final_image_data)/1024:.1f}KB")
        else:
            logger.info(f"{req_log} 跳过图片优化（配置已禁用）")
        
        # 步骤2: 根据file_bed_enabled决定输出方式
        if file_bed_enabled:
            logger.info(f"{req_log} 上传到图床...")
            
            # 将图片数据转换为base64 Data URI用于上传
            mime_type = get_mime_type_from_format(final_format or "png")
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
                mime_type = get_mime_type_from_format(final_format or "png")
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
                
                if not upload_error and uploaded_url:
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
            
            if upload_successful and final_url:
                # 存入缓存（TTLCache 自动管理大小和过期）
                if cache_enabled and cache_key and PROCESSED_IMAGE_CACHE is not None:
                    PROCESSED_IMAGE_CACHE[cache_key] = final_url
                    logger.info(f"{req_log} 💾 结果已存入缓存 (hash: {image_hash[:8]}..., variant: {cache_key.split(':')[1] if cache_key else 'N/A'})")
                return final_url, None
            else:
                error_msg = f"所有图床端点均上传失败。最后错误: {last_error}"
                logger.error(f"{req_log} {error_msg}，降级返回base64")
                # 降级：返回base64
                mime_type = get_mime_type_from_format(final_format or "png")
                base64_result = image_to_base64(final_image_data, mime_type)
                
                # 即使失败也要缓存降级结果（TTLCache 自动管理）
                if cache_enabled and cache_key and PROCESSED_IMAGE_CACHE is not None:
                    PROCESSED_IMAGE_CACHE[cache_key] = base64_result
                    logger.info(f"{req_log} 💾 降级结果已存入缓存 (hash: {image_hash[:8]}..., variant: {cache_key.split(':')[1] if cache_key else 'N/A'})")

                return base64_result, None
        
        else:
            # 不使用图床，返回base64
            logger.info(f"{req_log} 转换为base64（图床已禁用）")
            mime_type = get_mime_type_from_format(final_format or "png")
            base64_result = image_to_base64(final_image_data, mime_type)
            logger.info(f"{req_log} Base64转换完成: {len(base64_result)} 字符")
            
            # 存入缓存（TTLCache 自动管理大小和过期）
            if cache_enabled and cache_key and PROCESSED_IMAGE_CACHE is not None:
                PROCESSED_IMAGE_CACHE[cache_key] = base64_result
                logger.info(f"{req_log} 💾 结果已存入缓存 (hash: {image_hash[:8]}..., variant: {cache_key.split(':')[1] if cache_key else 'N/A'})")

            return base64_result, None
    
    except Exception as e:
        error_msg = f"图片处理异常: {type(e).__name__}: {e}"
        logger.error(f"{req_log} {error_msg}", exc_info=True)
        # 降级：返回原始数据
        return base64_data, error_msg


async def preprocess_anthropic_images(
    anthropic_req: dict,
    CONFIG: dict,
    model_image_config: Optional[dict] = None,
    PROCESSED_IMAGE_CACHE: Optional["MutableMapping"] = None,
    request_id: Optional[str] = None,
) -> int:
    """
    对 Anthropic 原生格式请求中的 base64 图片块执行压缩优化（就地修改）。

    适用于 /v1/messages 的 anthropic_native 透传链路：图片以
    {"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}
    块存在，与 OpenAI 格式的 image_url 结构不同，需要单独遍历。
    同时处理 tool_result 块中嵌套的 image 块。

    压缩后同步更新 source.data 与 source.media_type（格式转换后两者必须
    一致，否则上游会因 media_type 与实际数据不符拒绝请求）。
    透传模式下上游只接受 base64，图床上传强制禁用（浅拷贝覆盖，不污染全局 CONFIG）。

    Args:
        anthropic_req: Anthropic 请求体（或透传请求体），messages 将被就地修改
        CONFIG: 全局配置
        model_image_config: 模型级别 image_compression 配置（优先级更高）
        PROCESSED_IMAGE_CACHE: 处理结果缓存
        request_id: 请求ID（用于日志）

    Returns:
        处理成功的图片数量
    """
    global_enabled = (CONFIG or {}).get("image_optimization", {}).get("enabled", False)
    model_enabled = model_image_config.get("enabled", False) if model_image_config else False
    if not (global_enabled or model_enabled):
        return 0

    img_config = {**(CONFIG or {}), "file_bed_enabled": False}
    req_id = request_id or str(uuid.uuid4())[:8]
    req_log = f"[ANTHROPIC_IMG {req_id}]"
    processed_count = 0

    async def _process_image_block(block: dict) -> None:
        nonlocal processed_count
        source = block.get("source")
        if not isinstance(source, dict) or source.get("type") != "base64":
            return
        data = source.get("data")
        if not data or not isinstance(data, str):
            return

        media_type = source.get("media_type") or "image/png"
        data_uri = f"data:{media_type};base64,{data}"

        processed_data, proc_error = await process_image_data(
            base64_data=data_uri,
            filename=f"anthropic_{req_id}_{uuid.uuid4()}.png",
            request_id=req_id,
            CONFIG=img_config,
            PROCESSED_IMAGE_CACHE=PROCESSED_IMAGE_CACHE,
            model_image_config=model_image_config,
        )

        if proc_error:
            logger.warning(f"{req_log} 图片处理警告: {proc_error}，保留原图")
            return

        if isinstance(processed_data, str) and processed_data.startswith("data:") and "," in processed_data:
            header, new_data = processed_data.split(",", 1)
            # "data:image/jpeg;base64" → "image/jpeg"
            new_media_type = header[5:].split(";")[0].strip() or media_type
            source["media_type"] = new_media_type
            source["data"] = new_data
            processed_count += 1

    for message in anthropic_req.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "image":
                await _process_image_block(block)
            elif block_type == "tool_result":
                nested_content = block.get("content")
                if isinstance(nested_content, list):
                    for nested_block in nested_content:
                        if isinstance(nested_block, dict) and nested_block.get("type") == "image":
                            await _process_image_block(nested_block)

    if processed_count:
        logger.info(f"{req_log} Anthropic 原生请求图片预处理完成: {processed_count} 张")
    return processed_count