"""
服务层模块
包含流处理、格式化、解析等核心服务
"""

from services.stream_formatters import (
    format_openai_chunk,
    format_openai_reasoning_chunk,
    format_openai_finish_chunk,
    format_openai_error_chunk,
    format_openai_non_stream_response,
    generate_response_id,
    StreamChunkBuilder,
)

from services.stream_parsers import (
    ParsedStreamData,
    StreamPatternMatcher,
    StreamBuffer,
    extract_partial_content,
    is_control_marker,
)

# 向后兼容导入
from services.token_service import (
    calculate_request_tokens,
    calculate_response_tokens,
    calculate_full_usage,
    record_request_end_with_tokens,
    TokenUsageTracker,
)

__all__ = [
    # 格式化
    'format_openai_chunk',
    'format_openai_reasoning_chunk',
    'format_openai_finish_chunk',
    'format_openai_error_chunk',
    'format_openai_non_stream_response',
    'generate_response_id',
    'StreamChunkBuilder',
    
    # 解析
    'ParsedStreamData',
    'StreamPatternMatcher',
    'StreamBuffer',
    'extract_partial_content',
    'is_control_marker',
    
    # Token服务
    'calculate_request_tokens',
    'calculate_response_tokens',
    'calculate_full_usage',
    'record_request_end_with_tokens',
    'TokenUsageTracker',
]