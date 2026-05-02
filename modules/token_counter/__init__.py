"""
Token计数模块

支持多种模型的精确token计数，使用官方分词器。
已拆分为多个子模块。
"""

from ._types import (
    TokenizerType, TokenBreakdown, SmartEstimateResult,
    TokenizerMemoryDetail, LoadedTokenizerInfo, MemoryInfo,
    ClearResult, TokenCounterInfo, MessageTokenDetails,
    UsageDict, TokenizerResult, CalculateTokensResult,
    CustomTokenizerConfig, AddTokenizerResult, TokenizerStatus,
    AllTokenizersStatus,
    DEFAULT_TOKENIZER_CONFIG, MODEL_TOKEN_MULTIPLIERS,
    get_model_multiplier, load_tokenizer_config,
)

from ._tokenizers import (
    get_deepseek_tokenizer, get_tokenizer_for_model,
    get_anthropic_count_tokens, get_anthropic_client,
    get_gemma_tokenizer, get_gemini_model, get_tiktoken_encoding,
    get_token_counter_info, clear_tokenizer_cache, get_tokenizer_memory_info,
)

from ._counting import (
    count_text_tokens, count_messages_tokens, count_response_tokens,
    smart_token_estimate, estimate_tokens, estimate_message_tokens,
)

from ._custom import (
    install_tokenizer_package, load_custom_tokenizers_config,
    save_custom_tokenizers_config, load_tiktoken_model_tokenizer,
    add_custom_tokenizer, delete_custom_tokenizer,
    get_custom_tokenizer, list_custom_tokenizers,
)

from ._usage import (
    get_all_tokenizers_status, calculate_tokens_for_text,
    compare_tokenizers, calculate_request_tokens,
    calculate_response_tokens, calculate_full_usage,
    record_request_end_with_tokens, TokenUsageTracker,
    estimate_tokens_async, estimate_message_tokens_async,
    calculate_full_usage_async,
)
