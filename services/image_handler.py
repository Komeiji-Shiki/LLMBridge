"""
图片处理模块
处理LMArena返回的图片，支持Base64转换和本地保存
"""

import asyncio
import base64
import logging
import mimetypes
import time
from typing import Optional, Tuple, Dict, Any, Callable, Awaitable

from utils.task_registry import spawn

logger = logging.getLogger(__name__)


class ImageProcessor:
    """图片处理器"""
    
    def __init__(
        self,
        config: Dict[str, Any],
        image_cache: Any,
        cache_max_size: int = 100,
        cache_ttl: int = 3600,
        download_func: Optional[Callable[[str], Awaitable[Tuple[Optional[bytes], Optional[str]]]]] = None,
        save_func: Optional[Callable[[bytes, str, str], Awaitable[None]]] = None,
    ):
        """
        初始化图片处理器
        
        Args:
            config: 配置字典
            image_cache: 图片缓存（TTLCache，{url: markdown_base64}，过期由 TTLCache 自动管理）
            cache_max_size: 缓存最大大小
            cache_ttl: 缓存过期时间（秒）
            download_func: 下载函数
            save_func: 保存函数
        """
        self.config = config
        self.image_cache = image_cache
        self.cache_max_size = cache_max_size
        self.cache_ttl = cache_ttl
        self.download_func = download_func
        self.save_func = save_func
    
    def _get_return_mode(self) -> str:
        """获取图片返回模式"""
        return_format_config = self.config.get("image_return_format", {})
        return return_format_config.get("mode", "base64")
    
    def _should_save_locally(self) -> bool:
        """是否需要本地保存"""
        return self.config.get("save_images_locally", True)
    
    def _log_url(self, image_url: str, prefix: str = "📥") -> None:
        """记录图片URL日志"""
        show_full_urls = self.config.get("debug_show_full_urls", False)
        if show_full_urls:
            logger.info(f"{prefix} LMArena返回图片URL（完整）: {image_url}")
        else:
            display_length = self.config.get("url_display_length", 200)
            if len(image_url) <= display_length:
                logger.info(f"{prefix} LMArena返回图片URL: {image_url}")
            else:
                logger.info(f"{prefix} LMArena返回图片URL: {image_url[:display_length]}...")
                logger.debug(f"   完整URL: {image_url}")
    
    def _cleanup_cache(self) -> None:
        """清理过期缓存（TTLCache 自动管理，此方法保留为空操作）"""
        pass
    
    def _get_from_cache(self, image_url: str) -> Optional[str]:
        """从缓存获取图片（TTLCache 自动处理过期）"""
        cached_data = self.image_cache.get(image_url)
        if cached_data is not None:
            logger.info(f"  ⚡ 从缓存获取图片Base64")
            return cached_data
        return None
    
    def _save_to_cache(self, image_url: str, markdown_image: str) -> None:
        """保存到缓存（TTLCache 自动管理大小和过期）"""
        self.image_cache[image_url] = markdown_image
    
    async def _download_image(self, image_url: str) -> Tuple[Optional[bytes], Optional[str]]:
        """下载图片"""
        if not self.download_func:
            return None, "Download function not configured"
        return await self.download_func(image_url)
    
    async def _save_image(self, image_data: bytes, image_url: str, request_id: str) -> None:
        """保存图片到本地"""
        if self.save_func:
            await self.save_func(image_data, image_url, request_id)
    
    def _convert_to_base64(self, image_data: bytes, image_url: str) -> str:
        """转换为Base64格式的Markdown图片"""
        content_type = mimetypes.guess_type(image_url)[0] or 'image/png'
        image_base64 = base64.b64encode(image_data).decode('ascii')
        data_url = f"data:{content_type};base64,{image_base64}"
        return f"![Image]({data_url})"
    
    async def process_image_url(
        self, 
        image_url: str, 
        request_id: str
    ) -> Tuple[str, bool]:
        """
        处理图片URL，返回适当格式的内容
        
        Args:
            image_url: 图片URL
            request_id: 请求ID
        
        Returns:
            (输出内容, 是否需要继续处理)
        """
        process_start_time = time.time()
        self._log_url(image_url)
        
        return_mode = self._get_return_mode()
        save_locally = self._should_save_locally()
        
        logger.info(f"[IMG_PROCESS] 开始处理图片")
        logger.info(f"  - 返回模式: {return_mode}")
        logger.info(f"  - 本地保存: {save_locally}")
        
        # URL模式：立即返回
        if return_mode == "url":
            return await self._process_url_mode(image_url, request_id, save_locally)
        
        # Base64模式：需要下载并转换
        return await self._process_base64_mode(image_url, request_id, save_locally, process_start_time)
    
    async def _process_url_mode(
        self, 
        image_url: str, 
        request_id: str, 
        save_locally: bool
    ) -> Tuple[str, bool]:
        """URL模式处理"""
        logger.info(f"[IMG_PROCESS] URL模式 - 立即返回URL给客户端")
        
        # 如果需要保存到本地，创建后台任务
        if save_locally and self.download_func and self.save_func:
            logger.info(f"[IMG_PROCESS] 启动后台任务异步下载并保存图片")
            
            async def async_download_and_save():
                try:
                    download_start = time.time()
                    img_data, err = await self._download_image(image_url)
                    download_time = time.time() - download_start
                    
                    if img_data:
                        logger.info(f"[IMG_PROCESS] 后台下载成功，耗时: {download_time:.2f}秒")
                        await self._save_image(img_data, image_url, request_id)
                        logger.info(f"[IMG_PROCESS] 图片已保存到本地")
                    else:
                        logger.error(f"[IMG_PROCESS] 后台下载失败: {err}")
                except Exception as e:
                    logger.error(f"[IMG_PROCESS] 后台任务异常: {e}")
            
            spawn(async_download_and_save(), name="img-bg-download")
        elif not save_locally:
            logger.info(f"[IMG_PROCESS] save_images_locally=false，跳过下载")
        
        return f"![Image]({image_url})", True  # continue=True
    
    async def _process_base64_mode(
        self, 
        image_url: str, 
        request_id: str, 
        save_locally: bool,
        process_start_time: float
    ) -> Tuple[str, bool]:
        """Base64模式处理"""
        logger.info(f"[IMG_PROCESS] Base64模式 - 需要下载图片进行转换")
        
        # 检查缓存
        self._cleanup_cache()
        cached = self._get_from_cache(image_url)
        if cached:
            return cached, False  # 从缓存获取，不需要继续
        
        # 下载图片
        download_start_time = time.time()
        image_data, download_error = await self._download_image(image_url)
        download_time = time.time() - download_start_time
        logger.info(f"[IMG_PROCESS] 图片下载完成，耗时: {download_time:.2f}秒")
        
        # 保存到本地
        if save_locally and image_data:
            logger.info(f"[IMG_PROCESS] 异步保存图片到本地")
            spawn(self._save_image(image_data, image_url, request_id), name="img-save-local")
        elif not save_locally:
            logger.info(f"[IMG_PROCESS] save_images_locally=false，跳过本地保存")
        
        # Base64转换
        if image_data:
            markdown_image = self._convert_to_base64(image_data, image_url)
            self._save_to_cache(image_url, markdown_image)
            
            total_time = time.time() - process_start_time
            logger.info(f"[IMG_PROCESS] Base64转换完成，总耗时: {total_time:.2f}秒")
            
            return markdown_image, False
        else:
            # 下载失败，降级返回URL
            logger.error(f"[IMG_PROCESS] ❌ 图片下载失败 ({download_error})，降级返回原始URL")
            total_time = time.time() - process_start_time
            logger.info(f"[IMG_PROCESS] 处理完成（失败降级），总耗时: {total_time:.2f}秒")
            return f"![Image]({image_url})", False


class CloudflareHandler:
    """Cloudflare人机验证处理器

    验证状态（是否正在刷新/冷却截止时间）统一读写 AppState.server，
    保证多个并发流之间共享同一份全局状态（旧版每流一个实例各自
    持有副本，冷却机制实际失效）。
    """

    # Cloudflare检测模式
    PATTERNS = [
        r'<title>Just a moment...</title>',
        r'Enable JavaScript and cookies to continue'
    ]

    def __init__(self, browser_connections: Optional[Dict[str, Any]] = None):
        """
        初始化Cloudflare处理器

        Args:
            browser_connections: 浏览器WebSocket连接字典（缺省取 AppState）
        """
        from core.app_state import get_app_state
        app_state = get_app_state()
        self._server_state = app_state.server
        self.browser_connections = (
            browser_connections
            if browser_connections is not None
            else app_state.connection.browser_connections
        )

    @property
    def is_refreshing(self) -> bool:
        return self._server_state.IS_REFRESHING_FOR_VERIFICATION

    @is_refreshing.setter
    def is_refreshing(self, value: bool):
        self._server_state.IS_REFRESHING_FOR_VERIFICATION = value

    @property
    def cooldown_until(self) -> Optional[float]:
        return self._server_state.VERIFICATION_COOLDOWN_UNTIL

    @cooldown_until.setter
    def cooldown_until(self, value: Optional[float]):
        self._server_state.VERIFICATION_COOLDOWN_UNTIL = value
    
    async def handle_verification(self, request_id: str) -> str:
        """
        处理人机验证
        
        Args:
            request_id: 请求ID
        
        Returns:
            返回给客户端的消息
        """
        import json
        
        if not self.is_refreshing:
            logger.warning(f"PROCESSOR [ID: {request_id[:8]}]: 首次检测到人机验证，将发送刷新指令并启动25秒冷却。")
            self.is_refreshing = True
            self.cooldown_until = time.time() + 25
            
            # 发送刷新指令
            if self.browser_connections:
                first_ws = list(self.browser_connections.values())[0]
                spawn(first_ws.send_text(json.dumps({"command": "refresh"}, ensure_ascii=False)), name="cf-refresh")
            
            # 启动冷却重置任务
            async def reset_status():
                await asyncio.sleep(25)
                self.is_refreshing = False
                self.cooldown_until = None
                logger.info("⏰ 人机验证冷却期已结束，系统已恢复正常。")
            
            spawn(reset_status(), name="cf-cooldown-reset")
            return "检测到人机验证，已发送刷新指令。系统将冷却25秒，请稍后重试。"
        else:
            # 计算剩余冷却时间
            if self.cooldown_until:
                remaining = max(0, int(self.cooldown_until - time.time()))
                if remaining > 0:
                    logger.info(f"PROCESSOR [ID: {request_id[:8]}]: 检测到人机验证，冷却中（剩余{remaining}秒）。")
                    return f"正在等待人机验证冷却完成...（剩余 {remaining} 秒）"
            logger.info(f"PROCESSOR [ID: {request_id[:8]}]: 检测到人机验证，但已在刷新中，将等待。")
            return "正在等待人机验证完成..."


# 导出
__all__ = [
    'ImageProcessor',
    'CloudflareHandler',
]