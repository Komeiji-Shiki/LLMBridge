"""
流数据解析模块
处理来自LMArena的原始流数据解析

🔧 修复说明（相对旧版）：
- extract_text/reasoning：改用 finditer 单次遍历 + 一次切片，
  旧版在循环里反复 buffer[match.end():] 切片，长响应下是 O(n²)。
- extract_image_urls / extract_finish_info：改用 json.JSONDecoder.raw_decode
  解析，旧版的非贪婪正则 `\\{.*?\\}` 遇到嵌套对象（如 finish 数据里内嵌
  usage 对象）会截断出残缺 JSON 导致解析失败、丢失 finishReason/usage。
- extract_partial_content：旧版 `[^"]*?` 不识别转义引号，内容含 \\" 时
  捕获组以孤立反斜杠结尾，json.loads 必然失败而静默丢内容。
- check_cloudflare：增加流数据标记守卫。真正的 CF 拦截页是整个响应体
  被替换成 HTML，不会含 LMArena 流式前缀；模型正文里出现
  "Just a moment..." 等字样不应误杀整条流。
- is_control_marker：旧版用裸子串 in 判断，正文含 "a3:" 等字样会误判，
  改为要求前缀出现在行首。
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Any, Dict

logger = logging.getLogger(__name__)

_JSON_DECODER = json.JSONDecoder()


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
        # 图片/结束标记只定位前缀，JSON 主体交给 raw_decode（正确处理嵌套结构）
        self.image_prefix_pattern = re.compile(r'[ab]2:')
        self.finish_prefix_pattern = re.compile(r'[ab]d:')
        # 错误 JSON 只认行首（数据开头或换行后）出现的 {"error"：
        # 正常流正文都在 a0:"..." 的转义引号内，不会在行首出现裸 JSON；
        # JSON 主体交给 raw_decode 解析（支持嵌套 error 对象，旧版非贪婪
        # `\\{.*?\\}` 遇到嵌套结构会截断出残缺 JSON 导致错误漏报）
        self.error_prefix_pattern = re.compile(r'(?:^|[\r\n])\s*(\{\s*"error")')

        # 正常 LMArena 流的数据标记（带引号/花括号，降低子串误匹配概率）
        self.stream_marker_pattern = re.compile(r'[ab][02g]:"|[ab]d:\{')

        # 🔧 统一扫描正则：按出现位置依次匹配思维链/正文/JSON载荷标记。
        # 旧版 parse_buffer 按类型分别扫描+独立切片（先思维链后正文），
        # 同一批缓冲里正文出现在最后一个思维链标记之前时会被切片丢弃
        self.unified_pattern = re.compile(
            r'ag:"(?P<reasoning>(?:\\.|[^"\\])*)"'
            r'|[ab]0:"(?P<text>(?:\\.|[^"\\])*)"'
            r'|[ab](?P<json_kind>[2d]):'
        )

        self.cloudflare_patterns = [
            r'<title>Just a moment...</title>',
            r'Enable JavaScript and cookies to continue'
        ]

    # ---- 文本/思维链提取（finditer 单次遍历，O(n)） ----

    def _extract_quoted_contents(self, pattern: re.Pattern, buffer: str,
                                 label: str) -> Tuple[List[str], str]:
        contents: List[str] = []
        last_end = 0
        for match in pattern.finditer(buffer):
            try:
                # strict=False：上游偶发不规范时字符串内可能出现裸控制字符，
                # 严格模式会拒绝解析导致整段文本静默丢失
                text = json.loads(f'"{match.group(1)}"', strict=False)
                if text:
                    contents.append(text)
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"[PARSER] {label}解析失败: {e}")
            last_end = match.end()
        if last_end == 0:
            return contents, buffer
        return contents, buffer[last_end:]

    def extract_text_content(self, buffer: str) -> Tuple[List[str], str]:
        """从buffer中提取所有文本内容，返回 (提取的文本列表, 剩余buffer)"""
        return self._extract_quoted_contents(self.text_pattern, buffer, "文本")

    def extract_reasoning_content(self, buffer: str) -> Tuple[List[str], str]:
        """从buffer中提取所有思维链内容，返回 (提取的思维链列表, 剩余buffer)"""
        return self._extract_quoted_contents(self.reasoning_pattern, buffer, "思维链")

    # ---- JSON 载荷提取（raw_decode，支持嵌套结构） ----

    @staticmethod
    def _try_raw_decode(buffer: str, start: int) -> Tuple[Any, int]:
        """从 start 位置尝试解析一个完整 JSON 值。

        返回 (obj, end)；解析失败返回 (None, -1)。
        """
        try:
            return _JSON_DECODER.raw_decode(buffer, start)
        except (json.JSONDecodeError, ValueError):
            return None, -1

    def extract_image_urls(self, buffer: str) -> Tuple[List[Dict[str, Any]], str]:
        """从buffer中提取图片信息，返回 (图片信息列表, 剩余buffer)"""
        images: List[Dict[str, Any]] = []
        last_end = 0
        search_pos = 0

        while True:
            match = self.image_prefix_pattern.search(buffer, search_pos)
            if not match:
                break
            obj, end = self._try_raw_decode(buffer, match.end())
            if obj is None:
                # 解析失败：该行之后没有换行 → 可能是被 chunk 截断的数据，
                # 停止提取并保留（buffer[last_end:] 包含该段）等待拼全；
                # 否则视为坏数据跳过
                if '\n' not in buffer[match.end():]:
                    break
                logger.warning("[PARSER] 图片信息解析失败（坏数据，已跳过）")
                search_pos = match.end()
                continue
            if isinstance(obj, list) and obj:
                image_info = obj[0]
                if isinstance(image_info, dict) and \
                        image_info.get("type") == "image" and "image" in image_info:
                    images.append(image_info)
            last_end = end
            search_pos = end

        if last_end == 0:
            return images, buffer
        return images, buffer[last_end:]

    def extract_finish_info(self, buffer: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """从buffer中提取结束信息，返回 (结束信息字典, 剩余buffer)"""
        search_pos = 0
        while True:
            match = self.finish_prefix_pattern.search(buffer, search_pos)
            if not match:
                return None, buffer
            obj, end = self._try_raw_decode(buffer, match.end())
            if obj is None:
                # 可能被截断，保留 buffer 等待更多数据
                return None, buffer
            if isinstance(obj, dict) and "finishReason" in obj:
                return obj, buffer[end:]
            # 是合法 JSON 但不是 finish 数据（如其他控制标记），继续向后找
            search_pos = end

    def check_error(self, buffer: str) -> Optional[str]:
        """检查buffer中是否包含错误信息，返回错误信息或None。

        只在行首位置尝试解析完整 JSON，避免模型正文里出现
        `{"error"...}` 字样时误杀整条流。
        """
        for match in self.error_prefix_pattern.finditer(buffer):
            obj, _end = self._try_raw_decode(buffer, match.start(1))
            if isinstance(obj, dict) and "error" in obj:
                return obj.get("error") or "来自 LMArena 的未知错误"
        return None

    def check_cloudflare(self, buffer: str) -> bool:
        """检查是否遇到Cloudflare人机验证页面。

        真正的 CF 拦截发生在 HTTP 层：整个响应体是 HTML，不含 LMArena
        流式数据前缀。如果 buffer 中已出现流数据标记，说明是正常流，
        正文里出现 CF 特征文本（例如模型输出讨论 Cloudflare）不应误判。
        """
        if self.stream_marker_pattern.search(buffer):
            return False
        for pattern in self.cloudflare_patterns:
            if re.search(pattern, buffer, re.IGNORECASE):
                return True
        return False

    def parse_buffer(self, buffer: str) -> Tuple[ParsedStreamData, str]:
        """完整解析buffer，返回 (解析结果, 剩余buffer)

        🔧 交错修复：单遍按位置顺序统一消费所有标记。旧版先提取全部思维链
        再切片，若正文 a0:"..." 位于最后一个 ag:"..." 之前（多段思考模型的
        交错输出），正文会随切片静默丢失；现在任意交错顺序都不丢内容。
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

        consumed_end = 0   # 已成功消费的前缀结束位置
        search_pos = 0     # 扫描位置（跳过坏数据时与 consumed_end 分离）
        n = len(buffer)
        while search_pos < n:
            match = self.unified_pattern.search(buffer, search_pos)
            if not match:
                break

            reasoning_body = match.group('reasoning')
            if reasoning_body is not None:
                try:
                    text = json.loads(f'"{reasoning_body}"', strict=False)
                    if text:
                        result.reasoning_chunks.append(text)
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(f"[PARSER] 思维链解析失败: {e}")
                consumed_end = search_pos = match.end()
                continue

            text_body = match.group('text')
            if text_body is not None:
                try:
                    text = json.loads(f'"{text_body}"', strict=False)
                    if text:
                        result.content_chunks.append(text)
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(f"[PARSER] 文本解析失败: {e}")
                consumed_end = search_pos = match.end()
                continue

            # JSON 载荷标记：[ab]2: 图片 / [ab]d: 结束信息
            obj, end = self._try_raw_decode(buffer, match.end())
            if obj is None:
                if '\n' not in buffer[match.end():]:
                    # 行尾截断：停止扫描，保留该标记及之后的数据等待拼全
                    break
                # 有换行却解析失败：坏数据，跳过该标记继续扫描
                logger.warning("[PARSER] JSON载荷解析失败（坏数据，已跳过）")
                search_pos = match.end()
                continue

            if match.group('json_kind') == '2':
                if isinstance(obj, list) and obj:
                    image_info = obj[0]
                    if isinstance(image_info, dict) and \
                            image_info.get("type") == "image" and image_info.get("image"):
                        result.image_urls.append(image_info["image"])
            else:  # 'd'：结束信息（其他合法 JSON 控制标记直接消费不输出）
                if isinstance(obj, dict) and "finishReason" in obj:
                    result.finish_reason = obj.get('finishReason', 'stop')
                    result.usage_info = obj.get('usage') or obj.get('tokenUsage')
            consumed_end = search_pos = end

        return result, buffer[consumed_end:]


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


# 匹配可能被截断的文本前缀：转义感知，孤立的尾部反斜杠不会进入捕获组
_PARTIAL_TEXT_PATTERN = re.compile(r'[ab]0:"((?:\\.|[^"\\])*)')


def extract_partial_content(buffer: str) -> Tuple[str, str]:
    """
    从可能被截断的buffer中提取部分内容

    用于[DONE]信号后的最终内容提取

    Args:
        buffer: 原始数据缓冲区

    Returns:
        (提取的内容, 剩余buffer)
    """
    match = _PARTIAL_TEXT_PATTERN.search(buffer)

    if match and len(match.group(1)) > 0:
        try:
            text = json.loads(f'"{match.group(1)}"', strict=False)
            return text, buffer[match.end():]
        except (json.JSONDecodeError, ValueError):
            pass

    return "", buffer


# 控制标记必须出现在行首（数据开头或换行之后），
# 避免正文里出现 "a3:" 之类的子串被误判；同时排除协议数据前缀残留（a0/a1/a2/ag 等）
_CONTROL_MARKER_PATTERN = re.compile(r'(?:^|[\r\n])(?:a[0-3de]|b[0-3de]|ag):')


def is_control_marker(buffer: str) -> bool:
    """
    检查buffer是否只包含控制标记（不应作为内容输出）

    Args:
        buffer: 数据缓冲区

    Returns:
        是否是控制标记
    """
    return bool(_CONTROL_MARKER_PATTERN.search(buffer))


# 导出
__all__ = [
    'ParsedStreamData',
    'StreamPatternMatcher',
    'StreamBuffer',
    'extract_partial_content',
    'is_control_marker',
]
