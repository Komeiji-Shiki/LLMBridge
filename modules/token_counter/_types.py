"""
Token计数模块 - 类型定义、常量和配置
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Literal,
    Optional,
    Tuple,
    TypedDict,
    Union,
)

if TYPE_CHECKING:
    import tiktoken
    from anthropic import Anthropic
    from transformers import PreTrainedTokenizer, PreTrainedTokenizerFast

logger = logging.getLogger(__name__)


# ============================================================
# 类型定义
# ============================================================

TokenizerType = Union[Literal['anthropic', 'google', 'deepseek', 'tiktoken', 'estimate'], str]


class TokenBreakdown(TypedDict, total=False):
    """Token分解详情"""
    中文字符: str
    日文假名: str
    韩文字符: str
    英文单词: str
    数字: str
    标点: str
    特殊字符: str


class SmartEstimateResult(TypedDict):
    """智能估算结果"""
    name: str
    token_count: int
    method: str
    model_hint: str
    breakdown: TokenBreakdown
    note: str


class TokenizerMemoryDetail(TypedDict):
    """Tokenizer内存详情"""
    estimated_mb: int
    last_used: float


class LoadedTokenizerInfo(TypedDict):
    """已加载Tokenizer信息"""
    name: str
    key: str
    estimated_mb: int
    idle_minutes: float
    last_used: Optional[float]


class MemoryInfo(TypedDict):
    """内存使用信息"""
    loaded_tokenizers: List[LoadedTokenizerInfo]
    loaded_count: int
    estimated_memory_mb: int
    details: Dict[str, TokenizerMemoryDetail]


class ClearResult(TypedDict):
    """清理结果"""
    cleared: List[str]
    cleared_count: int
    count: int
    remaining: Dict[str, Any]


class TokenCounterInfo(TypedDict):
    """Token计数器信息"""
    tiktoken_available: bool
    cached_models: List[str]
    method: str
    tiktoken_version: str


class MessageTokenDetails(TypedDict):
    """消息Token详情"""
    messages: int
    system: int
    user: int
    assistant: int
    overhead: int
    multiplier: float


class UsageDict(TypedDict):
    """Token使用量字典"""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class TokenizerResult(TypedDict, total=False):
    """单个Tokenizer计算结果"""
    name: str
    token_count: int
    model_hint: str
    error: str
    install_cmd: str
    hint: str
    note: str
    source: str
    source_type: str
    is_custom: bool
    is_tiktoken_model: bool


class CalculateTokensResult(TypedDict):
    """计算Token结果"""
    text_info: Dict[str, int]
    results: Dict[str, TokenizerResult]


class CustomTokenizerConfig(TypedDict, total=False):
    """自定义Tokenizer配置"""
    name: str
    display_name: str
    source_type: str
    source: str
    description: str
    supported_models: List[str]
    created_at: str


class AddTokenizerResult(TypedDict, total=False):
    """添加Tokenizer结果"""
    success: bool
    name: str
    message: str
    test_result: str
    error: str
    allowed: List[str]


class TokenizerStatus(TypedDict, total=False):
    """Tokenizer状态信息"""
    name: str
    available: bool
    version: Optional[str]
    loaded: bool
    install_cmd: str
    description: str
    supported_models: List[str]
    path: Optional[str]


class AllTokenizersStatus(TypedDict):
    """所有Tokenizer状态"""
    tiktoken: TokenizerStatus
    anthropic: TokenizerStatus
    transformers: TokenizerStatus
    google_generativeai: TokenizerStatus
    gemma_local: TokenizerStatus
    deepseek_local: TokenizerStatus
    _memory_info: MemoryInfo


# ============================================================
# 全局变量（带类型注解）
# ============================================================

_tiktoken_cache: Dict[str, Any] = {}
_anthropic_client: Optional[Any] = None
_gemini_model: Optional[Any] = None
_gemini_api_count_failed: bool = False
_gemma_tokenizer: Optional[Any] = None
_deepseek_tokenizer: Optional[Any] = None
_custom_tokenizers: Dict[str, Any] = {}
_custom_tokenizers_config: Optional[Dict[str, CustomTokenizerConfig]] = None
_tiktoken_model_cache: Dict[str, Any] = {}

_tokenizer_last_used: Dict[str, float] = {}

try:
    from core.constants import TimeoutDefaults, CacheDefaults
    _TOKENIZER_IDLE_TIMEOUT = TimeoutDefaults.TOKENIZER_IDLE_TIMEOUT
    _TIKTOKEN_CACHE_MAX_SIZE = CacheDefaults.TIKTOKEN_CACHE_MAX_SIZE
    _CUSTOM_TOKENIZERS_MAX_SIZE = CacheDefaults.CUSTOM_TOKENIZERS_MAX_SIZE
    _TIKTOKEN_MODEL_CACHE_MAX_SIZE = CacheDefaults.TIKTOKEN_MODEL_CACHE_MAX_SIZE
except ImportError:
    _TOKENIZER_IDLE_TIMEOUT = 300
    _TIKTOKEN_CACHE_MAX_SIZE = 5
    _CUSTOM_TOKENIZERS_MAX_SIZE = 3
    _TIKTOKEN_MODEL_CACHE_MAX_SIZE = 3

DEFAULT_TOKENIZER_CONFIG = {
    "claude": "anthropic",
    "claude-3": "anthropic",
    "claude-3-opus": "anthropic",
    "claude-3-sonnet": "anthropic",
    "claude-3-haiku": "anthropic",
    "claude-3.5-sonnet": "anthropic",
    "gemini": "google",
    "gemini-pro": "google",
    "gemini-ultra": "google",
    "gemini-1.5": "google",
    "gemini-2": "google",
    "gpt-4": "tiktoken",
    "gpt-3.5": "tiktoken",
    "gpt-4-turbo": "tiktoken",
    "chatgpt": "tiktoken",
    "deepseek": "deepseek",
    "deepseek-chat": "deepseek",
    "deepseek-coder": "deepseek",
    "deepseek-v3": "deepseek"
}

_tokenizer_config = None
_unmapped_model_warned: set[str] = set()
_MAX_UNMAPPED_WARNED = 500  # 防止未映射模型名无限积累

MODEL_TOKEN_MULTIPLIERS = {
    'claude': 1.0,
    'claude-3': 1.0,
    'claude-3-opus': 1.0,
    'claude-3-sonnet': 1.0,
    'claude-3-haiku': 1.0,
    'claude-3.5-sonnet': 1.0,
    'gemini': 0.625,
    'gemini-pro': 0.625,
    'gemini-ultra': 0.625,
    'gemini-1.5': 0.625,
    'gemini-2': 0.625,
    'gpt-4': 1.0,
    'gpt-3.5': 1.0,
    'gpt-4-turbo': 1.0,
    'chatgpt': 1.0,
}


# ============================================================
# 配置和工具函数
# ============================================================

def get_model_multiplier(model_name: str) -> float:
    if not model_name:
        return 1.0
    model_lower = model_name.lower()
    if model_lower in MODEL_TOKEN_MULTIPLIERS:
        return MODEL_TOKEN_MULTIPLIERS[model_lower]
    for key, multiplier in MODEL_TOKEN_MULTIPLIERS.items():
        if key in model_lower:
            return multiplier
    return 1.0


def load_tokenizer_config() -> Dict[str, str]:
    global _tokenizer_config
    if _tokenizer_config is not None:
        return _tokenizer_config
    try:
        from core.config_loader import CONFIG
        if CONFIG and 'tokenizer_config' in CONFIG:
            _tokenizer_config = CONFIG['tokenizer_config']
            logger.info(f"[TOKEN_COUNTER] 已从CONFIG加载tokenizer配置，共{len(_tokenizer_config)}个模型映射")
            return _tokenizer_config
    except Exception as e:
        logger.debug(f"[TOKEN_COUNTER] 从CONFIG加载失败: {e}")
    try:
        from core.config_loader import _parse_jsonc
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'config.jsonc')
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                content = f.read()
                config = _parse_jsonc(content)
                _tokenizer_config = config.get('tokenizer_config', DEFAULT_TOKENIZER_CONFIG)
                logger.info(f"[TOKEN_COUNTER] 已加载tokenizer配置，共{len(_tokenizer_config)}个模型映射")
                return _tokenizer_config
    except Exception as e:
        logger.warning(f"[TOKEN_COUNTER] 加载tokenizer配置失败，使用默认配置: {e}")
    _tokenizer_config = DEFAULT_TOKENIZER_CONFIG
    return _tokenizer_config


def _update_tokenizer_last_used(tokenizer_name: str):
    _tokenizer_last_used[tokenizer_name] = time.time()
