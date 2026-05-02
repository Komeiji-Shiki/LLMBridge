"""
流数据解析模块
处理来自LMArena的原始流数据解析
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Any, Dict

logger = logging.getLogger(__name__)


@dataclass
class ParsedStreamData:
    """解析后的流数据"""
    content_chunks: List[str] = field(default_factory=list)
    reasoning_chunks: List[str] = field(default_factory=list)
    image_urls: List[str] = field(default_factory=list)
    finish_reason: Optional[str] = None
    usage_info: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    is_cloudflare: bool = False


class StreamPatternMatcher:
    """流数据模式匹配器"""
    
    def __init__(self):
        # 编译正则表达式以提高性能
        self.text_pattern = re.compile(r'[ab]0:"((?:\\.|[^"\\])*)"')
        self.reasoning_pattern = re.compile(r'ag:"((?:\\.|[^"\\])*)"')
        self.image_pattern = re.compile(r'[ab]2:(\[.*?\])')
        self.finish_pattern = re.compile(r'[ab]d:(\{.*?"finishReason".*?\})')
        self.error_pattern = re.compile(r'(\{\s*"error".*?\})', re.DOTALL)
        
        self.cloudflare_patterns = [
            r'<title>Just a moment...</title>',
            r'Enable JavaScript and cookies to continue'
        ]
    
    def extract_text_content(self, buffer: str) -> Tuple[List[str], str]:
        """
        从buffer中提取所有文本内容
        
        Args:
            buffer: 原始数据缓冲区
        
        Returns:
            (提取的文本列表, 剩余buffer)
        """
        contents = []
        while True:
            match = self.text_pattern.search(buffer)
            if not match:
                break
            
            try:
                text = json.loads(f'"{match.group(1)}"')
                if text:
                    contents.append(text)
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"[PARSER] 文本解析失败: {e}")
            
            buffer = buffer[match.end():]
        
        return contents, buffer
    
    def extract_reasoning_content(self, buffer: str) -> Tuple[List[str], str]:
        """
        从buffer中提取所有思维链内容
        
        Args:
            buffer: 原始数据缓冲区
        
        Returns:
            (提取的思维链列表, 剩余buffer)
        """
        contents = []
        while True:
            match = self.reasoning_pattern.search(buffer)
            if not match:
                break
            
            try:
                reasoning = json.loads(f'"{match.group(1)}"')
                if reasoning:
                    contents.append(reasoning)
            except (json.JSONDecodeError, ValueError) as e:
                logger.debug(f"[PARSER] 思维链解析失败: {e}")
            
            buffer = buffer[match.end():]
        
        return contents, buffer
    
    def extract_image_urls(self, buffer: str) -> Tuple[List[Dict[str, Any]], str]:
        """
        从buffer中提取图片信息
        
        Args:
            buffer: 原始数据缓冲区
        
        Returns:
            (图片信息列表, 剩余buffer)
        """
        images = []
        while True:
            match = self.image_pattern.search(buffer)
            if not match:
                break
            
            try:
                image_data_list = json.loads(match.group(1))
                if isinstance(image_data_list, list) and image_data_list:
                    image_info = image_data_list[0]
                    if image_info.get("type") == "image" and "image" in image_info:
                        images.append(image_info)
            except (json.JSONDecodeError, IndexError) as e:
                logger.warning(f"[PARSER] 图片信息解析失败: {e}")
            
            buffer = buffer[match.end():]
        
        return images, buffer
    
    def extract_finish_info(self, buffer: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """
        从buffer中提取结束信息
        
        Args:
            buffer: 原始数据缓冲区
        
        Returns:
            (结束信息字典, 剩余buffer)
        """
        match = self.finish_pattern.search(buffer)
        if not match:
            return None, buffer
        
        try:
            finish_data = json.loads(match.group(1))
            buffer = buffer[match.end():]
            return finish_data, buffer
        except json.JSONDecodeError:
            return None, buffer
    
    def check_error(self, buffer: str) -> Optional[str]:
        """
        检查buffer中是否包含错误信息
        
        Args:
            buffer: 原始数据缓冲区
        
        Returns:
            错误信息或None
        """
        match = self.error_pattern.search(buffer)
        if not match:
            return None
        
        try:
            error_json = json.loads(match.group(1))
            return error_json.get("error", "来自 LMArena 的未知错误")
        except json.JSONDecodeError:
            return None
    
    def check_cloudflare(self, buffer: str) -> bool:
        """
        检查是否遇到Cloudflare人机验证
        
        Args:
            buffer: 原始数据缓冲区
        
        Returns:
            是否是Cloudflare验证页面
        """
        for pattern in self.cloudflare_patterns:
            if re.search(pattern, buffer, re.IGNORECASE):
                return True
        return False
    
    def parse_buffer(self, buffer: str) -> Tuple[ParsedStreamData, str]:
        """
        完整解析buffer
        
        Args:
            buffer: 原始数据缓冲区
        
        Returns:
            (解析结果, 剩余buffer)
        """
        result = ParsedStreamData()
        
        # 检查Cloudflare
        if self.check_cloudflare(buffer):
            result.is_cloudflare = True
            return result, buffer
        
        # 检查错误
        error = self.check_error(buffer)
        if error:
            result.error = error
            return result, buffer
        
        # 提取思维链（优先处理）
        result.reasoning_chunks, buffer = self.extract_reasoning_content(buffer)
        
        # 提取文本内容
        result.content_chunks, buffer = self.extract_text_content(buffer)
        
        # 提取图片
        images, buffer = self.extract_image_urls(buffer)
        result.image_urls = [img.get('image', '') for img in images if img.get('image')]
        
        # 提取结束信息
        finish_info, buffer = self.extract_finish_info(buffer)
        if finish_info:
            result.finish_reason = finish_info.get('finishReason', 'stop')
            result.usage_info = finish_info.get('usage') or finish_info.get('tokenUsage')
        
        return result, buffer


class StreamBuffer:
    """
    流数据缓冲区管理器
    
    用于累积和管理流式数据
    """
    
    def __init__(self):
        self.buffer = ""
        self.matcher = StreamPatternMatcher()
        self.total_chars = 0
        self.chunk_count = 0
    
    def append(self, data: Any) -> None:
        """追加数据到缓冲区"""
        if isinstance(data, list):
            self.buffer += "".join(str(item) for item in data)
        else:
            self.buffer += str(data)
    
    def parse(self) -> ParsedStreamData:
        """解析当前缓冲区"""
        result, self.buffer = self.matcher.parse_buffer(self.buffer)
        
        # 更新统计
        for content in result.content_chunks:
            self.total_chars += len(content)
            self.chunk_count += 1
        
        return result
    
    def get_remaining(self) -> str:
        """获取剩余未处理的数据"""
        return self.buffer
    
    def clear(self) -> None:
        """清空缓冲区"""
        self.buffer = ""
    
    def get_stats(self) -> Dict[str, int]:
        """获取统计信息"""
        return {
            "total_chars": self.total_chars,
            "chunk_count": self.chunk_count,
            "buffer_size": len(self.buffer)
        }


def extract_partial_content(buffer: str) -> Tuple[str, str]:
    """
    从可能被截断的buffer中提取部分内容
    
    用于[DONE]信号后的最终内容提取
    
    Args:
        buffer: 原始数据缓冲区
    
    Returns:
        (提取的内容, 剩余buffer)
    """
    # 尝试匹配可能被截断的内容
    partial_pattern = re.compile(r'[ab]0:"([^"]*?)(?:"|$)')
    match = partial_pattern.search(buffer)
    
    if match and len(match.group(1)) > 0:
        try:
            text = json.loads(f'"{match.group(1)}"')
            return text, buffer[match.end():]
        except (json.JSONDecodeError, ValueError):
            pass
    
    return "", buffer


def is_control_marker(buffer: str) -> bool:
    """
    检查buffer是否只包含控制标记（不应作为内容输出）
    
    Args:
        buffer: 数据缓冲区
    
    Returns:
        是否是控制标记
    """
    control_prefixes = ['a3:', 'ad:', 'b3:', 'bd:', 'ae:', 'be:']
    for prefix in control_prefixes:
        if prefix in buffer:
            return True
    return False


# 导出
__all__ = [
    'ParsedStreamData',
    'StreamPatternMatcher',
    'StreamBuffer',
    'extract_partial_content',
    'is_control_marker',
]