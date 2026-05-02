"""
Token计数模块 - 分词器加载与缓存管理
"""

from __future__ import annotations

import gc
import logging
import os
import time
from typing import Any, Dict, List, Optional

from ._types import (
    _anthropic_client,
    _custom_tokenizers,
    _deepseek_tokenizer,
    _gemini_model,
    _gemma_tokenizer,
    _tiktoken_cache,
    _tiktoken_model_cache,
    _tokenizer_last_used,
    _TIKTOKEN_CACHE_MAX_SIZE,
    _TOKENIZER_IDLE_TIMEOUT,
    _update_tokenizer_last_used,
    TokenCounterInfo,
    load_tokenizer_config,
)

logger = logging.getLogger(__name__)

_anthropic_count_tokens_func = None


def get_deepseek_tokenizer():
    """获取DeepSeek tokenizer实例"""
    global _deepseek_tokenizer
    if _deepseek_tokenizer is not None:
        _update_tokenizer_last_used('deepseek')
        return _deepseek_tokenizer
    try:
        from transformers import AutoTokenizer
        import warnings
        warnings.filterwarnings('ignore', message='.*PyTorch.*')
        warnings.filterwarnings('ignore', message='.*TensorFlow.*')
        warnings.filterwarnings('ignore', message='.*Flax.*')
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        local_tokenizer_paths = [
            os.path.join(project_root, "deepseek_v3_tokenizer"),
            os.path.join(project_root, "tokenizers", "dsv3.2"),
        ]
        for local_tokenizer_path in local_tokenizer_paths:
            if os.path.exists(local_tokenizer_path):
                try:
                    logger.info(f"[TOKEN_COUNTER] 正在加载DeepSeek tokenizer: {local_tokenizer_path} (约100MB内存)")
                    _deepseek_tokenizer = AutoTokenizer.from_pretrained(
                        local_tokenizer_path, local_files_only=True, trust_remote_code=True
                    )
                    logger.info(f"[TOKEN_COUNTER] ✅ 已从本地加载DeepSeek tokenizer")
                    _update_tokenizer_last_used('deepseek')
                    return _deepseek_tokenizer
                except Exception as e:
                    logger.warning(f"[TOKEN_COUNTER] 本地加载DeepSeek tokenizer失败: {e}")
        logger.info("[TOKEN_COUNTER] DeepSeek tokenizer目录不存在，将使用tiktoken估算")
        return None
    except ImportError:
        logger.debug("[TOKEN_COUNTER] transformers未安装，使用tiktoken作为替代")
        return None
    except Exception as e:
        logger.debug(f"[TOKEN_COUNTER] DeepSeek tokenizer初始化失败: {e}")
        return None


def get_tokenizer_for_model(model_name: str) -> str:
    """获取模型应该使用的tokenizer类型"""
    from ._types import _unmapped_model_warned, _MAX_UNMAPPED_WARNED
    
    def _warn_once(msg: str, model: str):
        """只警告一次，且限制未映射模型名缓存大小"""
        if len(_unmapped_model_warned) >= _MAX_UNMAPPED_WARNED:
            return
        if model not in _unmapped_model_warned:
            _unmapped_model_warned.add(model)
            logger.warning(msg)
    
    if not model_name:
        return 'tiktoken'
    config = load_tokenizer_config()
    model_lower = model_name.lower()
    config_lower = {k.lower(): v for k, v in config.items()}
    if model_lower in config_lower:
        return config_lower[model_lower]
    for key, tokenizer_type in config.items():
        if key.lower() in model_lower:
            return tokenizer_type
    has_gemini = 'gemini' in model_lower
    has_claude = 'claude' in model_lower
    has_deepseek = 'deepseek' in model_lower
    has_gpt = ('gpt' in model_lower) or ('chatgpt' in model_lower)
    provider_hits = int(has_gemini) + int(has_claude) + int(has_deepseek) + int(has_gpt)
    if provider_hits == 1:
        if has_gemini:
            _warn_once(f"[TOKEN_COUNTER] 模型未命中tokenizer_config，自动按名称推断为Google tokenizer: {model_name}", model_name)
            return 'google'
        if has_claude:
            _warn_once(f"[TOKEN_COUNTER] 模型未命中tokenizer_config，自动按名称推断为Anthropic tokenizer: {model_name}", model_name)
            return 'anthropic'
        if has_deepseek:
            _warn_once(f"[TOKEN_COUNTER] 模型未命中tokenizer_config，自动按名称推断为DeepSeek tokenizer: {model_name}", model_name)
            return 'deepseek'
        if has_gpt:
            _warn_once(f"[TOKEN_COUNTER] 模型未命中tokenizer_config，自动按名称推断为Tiktoken: {model_name}", model_name)
            return 'tiktoken'
    _warn_once(f"[TOKEN_COUNTER] 模型未命中tokenizer_config且无法自动推断，默认使用Tiktoken: {model_name}", model_name)
    return 'tiktoken'


def get_anthropic_count_tokens():
    """获取Anthropic count_tokens函数"""
    try:
        from anthropic import count_tokens
        logger.info("[TOKEN_COUNTER] 已加载Anthropic tokenizer (顶层函数)")
        return count_tokens
    except ImportError:
        try:
            from anthropic.tokenizers import count_tokens
            logger.info("[TOKEN_COUNTER] 已加载Anthropic tokenizer (tokenizers模块)")
            return count_tokens
        except ImportError:
            pass
    except Exception as e:
        logger.debug(f"[TOKEN_COUNTER] Anthropic count_tokens导入失败: {e}")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key="dummy")
        if hasattr(client, 'count_tokens'):
            logger.info("[TOKEN_COUNTER] 已加载Anthropic tokenizer (客户端方法)")
            return client.count_tokens
        if hasattr(anthropic, 'count_tokens'):
            logger.info("[TOKEN_COUNTER] 已加载Anthropic tokenizer (模块函数)")
            return anthropic.count_tokens
    except ImportError:
        logger.debug("[TOKEN_COUNTER] anthropic未安装，运行: pip install anthropic")
    except Exception as e:
        logger.debug(f"[TOKEN_COUNTER] Anthropic tokenizer初始化失败: {e}")
    return None


def get_anthropic_client():
    """兼容旧代码：获取Anthropic客户端实例（已废弃）"""
    global _anthropic_count_tokens_func
    if _anthropic_count_tokens_func is None:
        _anthropic_count_tokens_func = get_anthropic_count_tokens()
    if _anthropic_count_tokens_func:
        class AnthropicWrapper:
            @staticmethod
            def count_tokens(text):
                return _anthropic_count_tokens_func(text)
        return AnthropicWrapper()
    return None


def get_gemma_tokenizer():
    """获取Gemma tokenizer实例"""
    global _gemma_tokenizer
    if _gemma_tokenizer is not None:
        _update_tokenizer_last_used('gemma')
        return _gemma_tokenizer
    try:
        from transformers import AutoTokenizer
        import warnings
        warnings.filterwarnings('ignore', message='.*PyTorch.*')
        warnings.filterwarnings('ignore', message='.*TensorFlow.*')
        warnings.filterwarnings('ignore', message='.*Flax.*')
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        local_tokenizer_paths = [
            os.path.join(project_root, "tokenizers", "gemma3-27b-it"),
            os.path.join(project_root, "tokenizers", "gemma-2b-it"),
            os.path.join(project_root, "tokenizers", "gemma-7b-it"),
            os.path.join(project_root, "tokenizers", "gemma-2b"),
            os.path.join(project_root, "tokenizers", "gemma"),
        ]
        for local_path in local_tokenizer_paths:
            if os.path.exists(local_path):
                try:
                    logger.info(f"[TOKEN_COUNTER] 正在加载Gemma tokenizer: {local_path} (约80MB内存)")
                    _gemma_tokenizer = AutoTokenizer.from_pretrained(
                        local_path, local_files_only=True, trust_remote_code=True
                    )
                    logger.info(f"[TOKEN_COUNTER] ✅ 已从本地加载Gemma tokenizer: {os.path.basename(local_path)}")
                    _update_tokenizer_last_used('gemma')
                    return _gemma_tokenizer
                except Exception as e:
                    logger.debug(f"[TOKEN_COUNTER] 本地加载失败 {local_path}: {e}")
                    continue
        logger.debug("[TOKEN_COUNTER] Gemma tokenizer不可用，将使用tiktoken估算（这是正常的，不影响使用）")
        return None
    except ImportError:
        logger.debug("[TOKEN_COUNTER] transformers未安装，使用tiktoken作为替代")
        return None
    except Exception as e:
        logger.debug(f"[TOKEN_COUNTER] Gemma tokenizer初始化失败: {e}")
        return None


def get_gemini_model():
    """获取Gemini模型实例（用于token计数）"""
    global _gemini_model
    if _gemini_model is not None:
        return _gemini_model
    try:
        import google.generativeai as genai
        api_key = os.environ.get('GOOGLE_API_KEY') or os.environ.get('GEMINI_API_KEY')
        if not api_key:
            try:
                from core.config_loader import CONFIG
                api_key = CONFIG.get('google_api_key')
            except Exception:
                pass
        if not api_key:
            logger.debug("[TOKEN_COUNTER] Google API密钥未配置，将使用Gemma tokenizer作为替代")
            return None
        api_key = str(api_key).strip()
        if len(api_key) < 20:
            logger.debug("[TOKEN_COUNTER] Google API密钥格式看起来无效，将跳过官方Gemini tokenizer并使用Gemma")
            return None
        genai.configure(api_key=api_key)
        _gemini_model = genai.GenerativeModel('gemini-pro')
        logger.info("[TOKEN_COUNTER] 已加载Google Gemini tokenizer")
        return _gemini_model
    except ImportError:
        logger.debug("[TOKEN_COUNTER] google-generativeai未安装，将使用Gemma tokenizer作为替代")
        return None
    except Exception as e:
        logger.debug(f"[TOKEN_COUNTER] Gemini tokenizer不可用，将使用Gemma tokenizer: {e}")
        return None


def get_tiktoken_encoding(model_name: str):
    """获取tiktoken编码器"""
    global _tiktoken_cache
    try:
        import tiktoken
        if model_name in _tiktoken_cache:
            _update_tokenizer_last_used(f'tiktoken_{model_name}')
            return _tiktoken_cache[model_name]
        if len(_tiktoken_cache) >= _TIKTOKEN_CACHE_MAX_SIZE:
            current_time = time.time()
            oldest_key = None
            oldest_time = current_time
            for key in _tiktoken_cache:
                last_used = _tokenizer_last_used.get(f'tiktoken_{key}', 0)
                if last_used < oldest_time:
                    oldest_time = last_used
                    oldest_key = key
            if oldest_key and oldest_key in _tiktoken_cache:
                del _tiktoken_cache[oldest_key]
                logger.info(f"[TOKEN_COUNTER] 🧹 LRU清理tiktoken缓存: {oldest_key}")
        encoding_name = 'cl100k_base'
        encoding = tiktoken.get_encoding(encoding_name)
        _tiktoken_cache[model_name] = encoding
        _update_tokenizer_last_used(f'tiktoken_{model_name}')
        logger.info(f"[TOKEN_COUNTER] 为模型 '{model_name}' 加载tokenizer: {encoding_name}")
        return encoding
    except ImportError:
        logger.warning("[TOKEN_COUNTER] tiktoken未安装，将使用估算方法")
        return None
    except Exception as e:
        logger.error(f"[TOKEN_COUNTER] 加载tiktoken失败: {e}")
        return None


def get_token_counter_info() -> Dict[str, Any]:
    """获取token计数器信息"""
    info: dict = {
        'tiktoken_available': False,
        'cached_models': list(_tiktoken_cache.keys()),
        'method': 'estimation'
    }
    try:
        import tiktoken
        info['tiktoken_available'] = True
        info['tiktoken_version'] = tiktoken.__version__ if hasattr(tiktoken, '__version__') else 'unknown'
        info['method'] = 'tiktoken'
    except ImportError:
        pass
    return info


def clear_tokenizer_cache(tokenizer_name: str = None, force: bool = False) -> Dict[str, Any]:
    """清理tokenizer缓存以释放内存"""
    global _gemma_tokenizer, _deepseek_tokenizer, _custom_tokenizers
    global _tiktoken_model_cache, _tiktoken_cache, _anthropic_client
    cleared = []
    from ._types import _unmapped_model_warned, _MAX_UNMAPPED_WARNED
    current_time = time.time()
    if tokenizer_name:
        if tokenizer_name == 'gemma' and _gemma_tokenizer is not None:
            _gemma_tokenizer = None
            cleared.append('gemma')
        elif tokenizer_name == 'deepseek' and _deepseek_tokenizer is not None:
            _deepseek_tokenizer = None
            cleared.append('deepseek')
        elif tokenizer_name in _custom_tokenizers:
            del _custom_tokenizers[tokenizer_name]
            cleared.append(f'custom_{tokenizer_name}')
        elif tokenizer_name in _tiktoken_model_cache:
            del _tiktoken_model_cache[tokenizer_name]
            cleared.append(f'tiktoken_model_{tokenizer_name}')
    else:
        if _gemma_tokenizer is not None:
            last_used = _tokenizer_last_used.get('gemma', 0)
            if force or (current_time - last_used > _TOKENIZER_IDLE_TIMEOUT):
                _gemma_tokenizer = None
                cleared.append('gemma')
                logger.info("[TOKEN_COUNTER] 🧹 清理空闲的Gemma tokenizer")
        if _deepseek_tokenizer is not None:
            last_used = _tokenizer_last_used.get('deepseek', 0)
            if force or (current_time - last_used > _TOKENIZER_IDLE_TIMEOUT):
                _deepseek_tokenizer = None
                cleared.append('deepseek')
                logger.info("[TOKEN_COUNTER] 🧹 清理空闲的DeepSeek tokenizer")
        to_remove = []
        for name in _custom_tokenizers:
            last_used = _tokenizer_last_used.get(f'custom_{name}', 0)
            if force or (current_time - last_used > _TOKENIZER_IDLE_TIMEOUT):
                to_remove.append(name)
        for name in to_remove:
            del _custom_tokenizers[name]
            cleared.append(f'custom_{name}')
            logger.info(f"[TOKEN_COUNTER] 🧹 清理空闲的自定义tokenizer: {name}")
        to_remove = []
        for name in _tiktoken_model_cache:
            last_used = _tokenizer_last_used.get(f'tiktoken_model_{name}', 0)
            if force or (current_time - last_used > _TOKENIZER_IDLE_TIMEOUT):
                to_remove.append(name)
        for name in to_remove:
            del _tiktoken_model_cache[name]
            cleared.append(f'tiktoken_model_{name}')
    if cleared:
        gc.collect()
        logger.info(f"[TOKEN_COUNTER] 🧹 已清理 {len(cleared)} 个tokenizer缓存，执行GC")
    
    # 清理未映射模型名警告缓存（防止无限增长）
    if len(_unmapped_model_warned) > _MAX_UNMAPPED_WARNED:
        _unmapped_model_warned.clear()
        logger.info("[TOKEN_COUNTER] 🧹 已清理未映射模型名警告缓存")
    
    return {
        'cleared': cleared,
        'cleared_count': len(cleared),
        'count': len(cleared),
        'remaining': {
            'gemma': _gemma_tokenizer is not None,
            'deepseek': _deepseek_tokenizer is not None,
            'custom': list(_custom_tokenizers.keys()),
            'tiktoken_model': list(_tiktoken_model_cache.keys()),
            'tiktoken': list(_tiktoken_cache.keys())
        }
    }


def get_tokenizer_memory_info() -> Dict[str, Any]:
    """获取tokenizer内存使用信息（不主动加载任何tokenizer）"""
    import sys
    current_time = time.time()
    loaded_tokenizers = []
    estimated_memory_mb = 0
    details = {}
    if _gemma_tokenizer is not None:
        estimated_mb = 80
        last_used = _tokenizer_last_used.get('gemma', current_time)
        idle_minutes = (current_time - last_used) / 60
        loaded_tokenizers.append({
            'name': 'Gemma', 'key': 'gemma', 'estimated_mb': estimated_mb,
            'idle_minutes': idle_minutes, 'last_used': last_used
        })
        details['gemma'] = {'estimated_mb': estimated_mb, 'last_used': last_used}
        estimated_memory_mb += estimated_mb
    if _deepseek_tokenizer is not None:
        estimated_mb = 100
        last_used = _tokenizer_last_used.get('deepseek', current_time)
        idle_minutes = (current_time - last_used) / 60
        loaded_tokenizers.append({
            'name': 'DeepSeek', 'key': 'deepseek', 'estimated_mb': estimated_mb,
            'idle_minutes': idle_minutes, 'last_used': last_used
        })
        details['deepseek'] = {'estimated_mb': estimated_mb, 'last_used': last_used}
        estimated_memory_mb += estimated_mb
    for name, tokenizer in _custom_tokenizers.items():
        estimated_mb = 50
        last_used = _tokenizer_last_used.get(f'custom_{name}', current_time)
        idle_minutes = (current_time - last_used) / 60
        loaded_tokenizers.append({
            'name': f'Custom: {name}', 'key': f'custom_{name}',
            'estimated_mb': estimated_mb, 'idle_minutes': idle_minutes, 'last_used': last_used
        })
        details[f'custom_{name}'] = {'estimated_mb': estimated_mb, 'last_used': last_used}
        estimated_memory_mb += estimated_mb
    for name in _tiktoken_cache:
        estimated_mb = 5
        loaded_tokenizers.append({
            'name': f'Tiktoken: {name}', 'key': f'tiktoken_{name}',
            'estimated_mb': estimated_mb, 'idle_minutes': 0, 'last_used': None
        })
        estimated_memory_mb += estimated_mb
    for name in _tiktoken_model_cache:
        estimated_mb = 20
        last_used = _tokenizer_last_used.get(f'tiktoken_model_{name}', current_time)
        idle_minutes = (current_time - last_used) / 60
        loaded_tokenizers.append({
            'name': f'TiktokenModel: {name}', 'key': f'tiktoken_model_{name}',
            'estimated_mb': estimated_mb, 'idle_minutes': idle_minutes, 'last_used': last_used
        })
        details[f'tiktoken_model_{name}'] = {'estimated_mb': estimated_mb, 'last_used': last_used}
        estimated_memory_mb += estimated_mb
    return {
        'loaded_tokenizers': loaded_tokenizers,
        'loaded_count': len(loaded_tokenizers),
        'estimated_memory_mb': estimated_memory_mb,
        'details': details
    }
