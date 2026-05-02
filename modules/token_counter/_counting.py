"""
Token计数模块 - 核心计数功能
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from ._types import (
    get_model_multiplier,
)
from ._tokenizers import (
    get_deepseek_tokenizer,
    get_gemma_tokenizer,
    get_gemini_model,
    get_tiktoken_encoding,
    get_anthropic_count_tokens,
    get_tokenizer_for_model,
)

logger = logging.getLogger(__name__)


def count_text_tokens(text: str, model_name: str = "gpt-4") -> int:
    """计算文本的token数量（优先使用原生tokenizer，否则使用校准系数）"""
    from ._types import _gemini_api_count_failed
    if not text:
        return 0
    tokenizer_type = get_tokenizer_for_model(model_name)
    logger.debug(f"[TOKEN_COUNTER] 模型 {model_name} 配置的 tokenizer 类型: {tokenizer_type}")

    if tokenizer_type == 'deepseek':
        deepseek_tokenizer = get_deepseek_tokenizer()
        if deepseek_tokenizer:
            try:
                tokens = deepseek_tokenizer.encode(text)
                token_count = len(tokens)
                logger.debug(f"[TOKEN_COUNTER] ✅ 使用DeepSeek官方tokenizer（模型: {model_name}）: {token_count} tokens")
                return token_count
            except Exception as e:
                logger.warning(f"[TOKEN_COUNTER] DeepSeek tokenizer失败，回退到tiktoken: {e}")

    elif tokenizer_type == 'google':
        gemma_tokenizer = get_gemma_tokenizer()
        if gemma_tokenizer:
            try:
                tokens = gemma_tokenizer.encode(text)
                token_count = len(tokens)
                logger.debug(f"[TOKEN_COUNTER] ✅ 使用Gemma tokenizer（模型: {model_name}）: {token_count} tokens")
                return token_count
            except Exception as e:
                logger.warning(f"[TOKEN_COUNTER] Gemma tokenizer失败，尝试Gemini官方tokenizer: {e}")
        if not _gemini_api_count_failed:
            gemini_model = get_gemini_model()
            if gemini_model:
                try:
                    result = gemini_model.count_tokens(text)
                    token_count = result.total_tokens
                    logger.debug(f"[TOKEN_COUNTER] ✅ 使用Gemini官方tokenizer（模型: {model_name}）: {token_count} tokens")
                    return token_count
                except Exception as e:
                    logger.warning(f"[TOKEN_COUNTER] Gemini官方tokenizer首次失败: {e}")
                    from . import _types as _tc_types
                    _tc_types._gemini_api_count_failed = True
                    logger.debug(f"[TOKEN_COUNTER] Gemini官方tokenizer已标记为不可用，后续将跳过")

    elif isinstance(tokenizer_type, str) and tokenizer_type.startswith('custom_'):
        custom_name = tokenizer_type[len('custom_'):]
        from ._custom import get_custom_tokenizer
        custom_tokenizer, is_tiktoken_model = get_custom_tokenizer(custom_name)
        if custom_tokenizer:
            try:
                if is_tiktoken_model:
                    tokens = custom_tokenizer.encode(text, allowed_special="all")
                else:
                    tokens = custom_tokenizer.encode(text)
                token_count = len(tokens)
                logger.debug(f"[TOKEN_COUNTER] ✅ 使用自定义tokenizer（{custom_name}, 模型: {model_name}）: {token_count} tokens")
                return token_count
            except Exception as e:
                logger.warning(f"[TOKEN_COUNTER] 自定义tokenizer({custom_name})失败，回退到tiktoken: {e}")
        else:
            logger.warning(f"[TOKEN_COUNTER] 自定义tokenizer不存在或加载失败: {custom_name}，回退到tiktoken")

    elif tokenizer_type == 'anthropic':
        count_tokens_func = get_anthropic_count_tokens()
        if count_tokens_func:
            try:
                token_count = count_tokens_func(text)
                logger.debug(f"[TOKEN_COUNTER] ✅ 使用Anthropic tokenizer（模型: {model_name}）: {token_count} tokens")
                return token_count
            except Exception as e:
                logger.warning(f"[TOKEN_COUNTER] Anthropic tokenizer失败，回退到tiktoken: {e}")

    # 回退到 tiktoken
    multiplier = get_model_multiplier(model_name)
    encoding = get_tiktoken_encoding(model_name)
    if encoding:
        try:
            tokens = encoding.encode(text)
            base_count = len(tokens)
            adjusted_count = int(base_count * multiplier)
            if multiplier != 1.0 and logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"[TOKEN_COUNTER] ✅ 使用Tiktoken（模型: {model_name}, 校准系数{multiplier}）: {base_count} -> {adjusted_count} tokens")
            else:
                logger.debug(f"[TOKEN_COUNTER] ✅ 使用Tiktoken（模型: {model_name}）: {adjusted_count} tokens")
            return adjusted_count
        except Exception as e:
            logger.error(f"[TOKEN_COUNTER] tiktoken编码失败: {e}")

    # 最终回退到字符估算
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    total_chars = len(text)
    if chinese_chars > total_chars * 0.5:
        base_estimate = total_chars // 2
    else:
        base_estimate = total_chars // 4
    return int(base_estimate * multiplier)


def count_messages_tokens(messages: List[Dict[str, Any]], model_name: str = "gpt-4") -> Tuple[int, Dict[str, int]]:
    """计算消息列表的token数量"""
    total_tokens = 0
    details = {
        'messages': 0, 'system': 0, 'user': 0, 'assistant': 0,
        'overhead': 0, 'multiplier': get_model_multiplier(model_name)
    }
    for message in messages:
        role = message.get('role', 'user')
        content = message.get('content', '')
        text_content = ''
        if isinstance(content, str):
            text_content = content
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get('type') == 'text':
                    text_content += part.get('text', '')
        content_tokens = count_text_tokens(text_content, model_name)
        overhead_per_message = int(4 * details['multiplier'])
        message_tokens = content_tokens + overhead_per_message
        total_tokens += message_tokens
        details['messages'] += message_tokens
        details[role] = details.get(role, 0) + content_tokens
    overall_overhead = int((len(messages) * 4 + 3) * details['multiplier'])
    details['overhead'] = overall_overhead
    total_tokens += overall_overhead
    return total_tokens, details


def count_response_tokens(response_text: str, model_name: str = "gpt-4") -> int:
    """计算响应文本的token数量"""
    return count_text_tokens(response_text, model_name)


def smart_token_estimate(text: str) -> Dict[str, Any]:
    """智能token估算，基于多维度分析"""
    if not text:
        return {
            'name': '智能估算', 'token_count': 0, 'method': '空文本',
            'model_hint': '通用估算', 'breakdown': {}
        }
    chinese_chars = 0
    japanese_kana = 0
    korean_chars = 0
    english_words = 0
    digits = 0
    punctuation = 0
    spaces = 0
    special_chars = 0
    other_chars = 0
    current_word = ""
    current_number = ""
    for i, char in enumerate(text):
        code = ord(char)
        if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
            chinese_chars += 1
            if current_word:
                english_words += 1
                current_word = ""
            if current_number:
                digits += len(current_number)
                current_number = ""
        elif 0x3040 <= code <= 0x309F or 0x30A0 <= code <= 0x30FF:
            japanese_kana += 1
            if current_word:
                english_words += 1
                current_word = ""
        elif 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF:
            korean_chars += 1
            if current_word:
                english_words += 1
                current_word = ""
        elif char.isalpha() and code < 128:
            current_word += char
        elif char.isdigit():
            current_number += char
        elif char.isspace():
            spaces += 1
            if current_word:
                english_words += 1
                current_word = ""
            if current_number:
                digits += len(current_number)
                current_number = ""
        elif code < 128 and not char.isalnum():
            punctuation += 1
            if current_word:
                english_words += 1
                current_word = ""
            if current_number:
                digits += len(current_number)
                current_number = ""
        elif code >= 0x1F300:
            special_chars += 1
        else:
            other_chars += 1
    if current_word:
        english_words += 1
    if current_number:
        digits += len(current_number)
    chinese_tokens = int(chinese_chars * 1.7)
    japanese_tokens = int(japanese_kana * 1.0)
    korean_tokens = int(korean_chars * 1.3)
    english_tokens = int(english_words * 1.3)
    digit_tokens = max(1, digits // 3) if digits > 0 else 0
    punctuation_tokens = int(punctuation * 0.8)
    space_tokens = max(0, spaces // 10)
    special_tokens = int(special_chars * 1.5)
    other_tokens = int(other_chars * 1.2)
    token_estimate = (chinese_tokens + japanese_tokens + korean_tokens +
                      english_tokens + digit_tokens + punctuation_tokens +
                      space_tokens + special_tokens + other_tokens)
    token_estimate = max(1, token_estimate)
    breakdown = {}
    if chinese_chars > 0:
        breakdown['中文字符'] = f"{chinese_chars} → ~{chinese_tokens} tokens"
    if japanese_kana > 0:
        breakdown['日文假名'] = f"{japanese_kana} → ~{japanese_tokens} tokens"
    if korean_chars > 0:
        breakdown['韩文字符'] = f"{korean_chars} → ~{korean_tokens} tokens"
    if english_words > 0:
        breakdown['英文单词'] = f"{english_words} → ~{english_tokens} tokens"
    if digits > 0:
        breakdown['数字'] = f"{digits} → ~{digit_tokens} tokens"
    if punctuation > 0:
        breakdown['标点'] = f"{punctuation} → ~{punctuation_tokens} tokens"
    if special_chars > 0:
        breakdown['特殊字符'] = f"{special_chars} → ~{special_tokens} tokens"
    total_cjk = chinese_chars + japanese_kana + korean_chars
    total_chars = len(text)
    if total_cjk > total_chars * 0.5:
        if chinese_chars >= japanese_kana and chinese_chars >= korean_chars:
            method = f"中文为主 (中文{chinese_chars}字, 英文{english_words}词)"
        elif japanese_kana > chinese_chars:
            method = f"日文为主 (假名{japanese_kana}字)"
        else:
            method = f"韩文为主 (韩文{korean_chars}字)"
    elif english_words > total_cjk:
        method = f"英文为主 ({english_words}个单词)"
    else:
        method = f"混合语言 (中{chinese_chars} 英{english_words}词)"
    return {
        'name': '智能估算', 'token_count': token_estimate,
        'method': method, 'model_hint': '基于字符类型的加权估算',
        'breakdown': breakdown, 'note': '此为估算值，实际token数因模型而异'
    }


def estimate_tokens(text: str, model: str = "gpt-4") -> int:
    """便捷函数：估算文本token数"""
    return count_text_tokens(text, model)


def estimate_message_tokens(messages: List[Dict], model: str = "gpt-4") -> int:
    """便捷函数：估算消息token数"""
    total, _ = count_messages_tokens(messages, model)
    return total
