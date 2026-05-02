"""
通用Token计算服务 - 兼容层

此模块已合并到 modules/token_counter.py
为保持向后兼容，此文件提供导入转发

请在新代码中直接使用:
    from modules.token_counter import (
        calculate_request_tokens,
        calculate_response_tokens,
        calculate_full_usage,
        record_request_end_with_tokens,
        TokenUsageTracker
    )
"""

import logging

logger = logging.getLogger(__name__)

# 发出弃用警告
logger.debug("[TOKEN_SERVICE] 注意: services/token_service.py 已弃用，请直接从 modules/token_counter 导入")

# 从 token_counter 导入所有功能
from modules.token_counter import (
    calculate_request_tokens,
    calculate_response_tokens,
    calculate_full_usage,
    record_request_end_with_tokens,
    TokenUsageTracker,
    # 同时导出底层函数供需要的模块使用
    estimate_tokens,
    estimate_message_tokens,
    count_text_tokens,
    count_messages_tokens,
    get_tokenizer_for_model,
)

# 导出所有符号
__all__ = [
    'calculate_request_tokens',
    'calculate_response_tokens', 
    'calculate_full_usage',
    'record_request_end_with_tokens',
    'TokenUsageTracker',
    'estimate_tokens',
    'estimate_message_tokens',
    'count_text_tokens',
    'count_messages_tokens',
    'get_tokenizer_for_model',
]