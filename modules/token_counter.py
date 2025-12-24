"""
Token计数模块
支持多种模型的精确token计数，使用官方分词器
"""

import logging
from typing import List, Dict, Any, Optional, Tuple
import json
import os

logger = logging.getLogger(__name__)

# 全局变量存储tokenizer实例
_tiktoken_cache = {}
_anthropic_client = None
_gemini_model = None  # Gemini模型实例（用于token计数）
_gemma_tokenizer = None  # Gemma tokenizer（用于Gemini token计数）
_deepseek_tokenizer = None  # DeepSeek tokenizer（用于DeepSeek token计数）

# 默认tokenizer配置（如果config.jsonc中没有配置）
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

# 缓存的tokenizer配置
_tokenizer_config = None

# 模型token倍数校准系数（相对于GPT-4的cl100k_base）
# 基准：GPT-4 = 1.0
MODEL_TOKEN_MULTIPLIERS = {
    # Claude系列：约为GPT-4的1.0倍（使用相同的cl100k_base作为基准）
    'claude': 1.0,
    'claude-3': 1.0,
    'claude-3-opus': 1.0,
    'claude-3-sonnet': 1.0,
    'claude-3-haiku': 1.0,
    'claude-3.5-sonnet': 1.0,
    
    # Gemini系列：约为Claude的0.625倍（即Claude是Gemini的1.6倍）
    # 0.625 = 1 / 1.6
    'gemini': 0.625,
    'gemini-pro': 0.625,
    'gemini-ultra': 0.625,
    'gemini-1.5': 0.625,
    'gemini-2': 0.625,
    
    # GPT系列：基准值
    'gpt-4': 1.0,
    'gpt-3.5': 1.0,
    'gpt-4-turbo': 1.0,
    'chatgpt': 1.0,
}

def get_model_multiplier(model_name: str) -> float:
    """
    获取模型的token倍数校准系数
    
    Args:
        model_name: 模型名称
        
    Returns:
        校准系数（默认1.0）
    """
    if not model_name:
        return 1.0
    
    model_lower = model_name.lower()
    
    # 精确匹配
    if model_lower in MODEL_TOKEN_MULTIPLIERS:
        return MODEL_TOKEN_MULTIPLIERS[model_lower]
    
    # 模糊匹配
    for key, multiplier in MODEL_TOKEN_MULTIPLIERS.items():
        if key in model_lower:
            return multiplier
    
    # 默认返回1.0
    return 1.0

def load_tokenizer_config() -> Dict[str, str]:
    """
    从config.jsonc加载tokenizer配置
    
    Returns:
        tokenizer配置字典
    """
    global _tokenizer_config
    
    if _tokenizer_config is not None:
        return _tokenizer_config
    
    try:
        # 尝试从已加载的CONFIG中获取（避免重复解析JSONC）
        from core.config_loader import CONFIG
        if CONFIG and 'tokenizer_config' in CONFIG:
            _tokenizer_config = CONFIG['tokenizer_config']
            logger.info(f"[TOKEN_COUNTER] 已从CONFIG加载tokenizer配置，共{len(_tokenizer_config)}个模型映射")
            return _tokenizer_config
    except Exception as e:
        logger.debug(f"[TOKEN_COUNTER] 从CONFIG加载失败: {e}")
    
    try:
        # 回退：使用config_loader的_parse_jsonc来正确解析JSONC
        from core.config_loader import _parse_jsonc
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config.jsonc')
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

def get_deepseek_tokenizer():
    """
    获取DeepSeek tokenizer实例
    优先从本地deepseek_v3_tokenizer目录加载
    
    Returns:
        DeepSeek tokenizer实例或None
    """
    global _deepseek_tokenizer
    
    if _deepseek_tokenizer is not None:
        return _deepseek_tokenizer
    
    try:
        from transformers import AutoTokenizer
        import warnings
        
        # 忽略PyTorch/TensorFlow未安装的警告
        warnings.filterwarnings('ignore', message='.*PyTorch.*')
        warnings.filterwarnings('ignore', message='.*TensorFlow.*')
        warnings.filterwarnings('ignore', message='.*Flax.*')
        
        # 获取项目根目录
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        # 本地tokenizer路径
        local_tokenizer_path = os.path.join(project_root, "deepseek_v3_tokenizer")
        
        if os.path.exists(local_tokenizer_path):
            try:
                logger.debug(f"[TOKEN_COUNTER] 尝试从本地加载DeepSeek tokenizer: {local_tokenizer_path}")
                _deepseek_tokenizer = AutoTokenizer.from_pretrained(
                    local_tokenizer_path,
                    local_files_only=True,
                    trust_remote_code=True
                )
                logger.info(f"[TOKEN_COUNTER] ✅ 已从本地加载DeepSeek tokenizer")
                return _deepseek_tokenizer
            except Exception as e:
                logger.warning(f"[TOKEN_COUNTER] 本地加载DeepSeek tokenizer失败: {e}")
        else:
            logger.info(f"[TOKEN_COUNTER] DeepSeek tokenizer目录不存在: {local_tokenizer_path}")
        
        # 如果本地没有，尝试在线下载（可选）
        try:
            logger.debug(f"[TOKEN_COUNTER] 尝试在线下载DeepSeek tokenizer...")
            _deepseek_tokenizer = AutoTokenizer.from_pretrained(
                "deepseek-ai/DeepSeek-V3",
                trust_remote_code=True,
                local_files_only=False
            )
            logger.info(f"[TOKEN_COUNTER] ✅ 已在线加载DeepSeek tokenizer")
            return _deepseek_tokenizer
        except Exception as e:
            logger.debug(f"[TOKEN_COUNTER] DeepSeek tokenizer在线下载失败: {e}")
        
        logger.info("[TOKEN_COUNTER] DeepSeek tokenizer不可用，将使用tiktoken估算")
        logger.info(f"[TOKEN_COUNTER] 提示：可将tokenizer文件放到 {local_tokenizer_path} 目录")
        return None
        
    except ImportError:
        logger.debug("[TOKEN_COUNTER] transformers未安装，使用tiktoken作为替代")
        return None
    except Exception as e:
        logger.debug(f"[TOKEN_COUNTER] DeepSeek tokenizer初始化失败: {e}")
        return None

def get_tokenizer_for_model(model_name: str) -> str:
    """
    获取模型应该使用的tokenizer类型
    
    Args:
        model_name: 模型名称
        
    Returns:
        tokenizer类型: 'anthropic', 'google', 'deepseek', 'tiktoken', 或 'estimate'
    """
    config = load_tokenizer_config()
    model_lower = model_name.lower()
    
    # 精确匹配
    if model_lower in config:
        return config[model_lower]
    
    # 模糊匹配
    for key, tokenizer_type in config.items():
        if key in model_lower:
            return tokenizer_type
    
    # 默认使用tiktoken
    return 'tiktoken'

def get_anthropic_client():
    """
    获取Anthropic客户端实例（用于token计数）
    
    Returns:
        Anthropic客户端或None
    """
    global _anthropic_client
    
    if _anthropic_client is not None:
        return _anthropic_client
    
    try:
        import anthropic
        
        # 创建客户端（不需要API key也能使用count_tokens）
        _anthropic_client = anthropic.Anthropic(api_key="dummy")
        logger.info("[TOKEN_COUNTER] 已加载Anthropic tokenizer")
        return _anthropic_client
        
    except ImportError:
        logger.debug("[TOKEN_COUNTER] anthropic未安装，运行: pip install anthropic")
        return None
    except Exception as e:
        logger.warning(f"[TOKEN_COUNTER] 加载Anthropic tokenizer失败: {e}")
        return None

def get_gemma_tokenizer():
    """
    获取Gemma tokenizer实例（用于Gemini token计数的替代方案）
    优先从本地tokenizers目录加载
    
    Returns:
        Gemma tokenizer实例或None
    """
    global _gemma_tokenizer
    
    if _gemma_tokenizer is not None:
        return _gemma_tokenizer
    
    try:
        from transformers import AutoTokenizer
        import warnings
        
        # 忽略PyTorch/TensorFlow未安装的警告（tokenizer不需要这些）
        warnings.filterwarnings('ignore', message='.*PyTorch.*')
        warnings.filterwarnings('ignore', message='.*TensorFlow.*')
        warnings.filterwarnings('ignore', message='.*Flax.*')
        
        # 获取项目根目录
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        # 优先尝试本地tokenizers目录
        local_tokenizer_paths = [
            os.path.join(project_root, "tokenizers", "gemma3-27b-it"),  # Gemma 3
            os.path.join(project_root, "tokenizers", "gemma-2b-it"),
            os.path.join(project_root, "tokenizers", "gemma-7b-it"),
            os.path.join(project_root, "tokenizers", "gemma-2b"),
            os.path.join(project_root, "tokenizers", "gemma"),  # 通用文件夹名
        ]
        
        # 先尝试本地路径
        for local_path in local_tokenizer_paths:
            if os.path.exists(local_path):
                try:
                    logger.debug(f"[TOKEN_COUNTER] 尝试从本地加载: {local_path}")
                    _gemma_tokenizer = AutoTokenizer.from_pretrained(
                        local_path,
                        local_files_only=True,
                        trust_remote_code=True
                    )
                    logger.info(f"[TOKEN_COUNTER] 已从本地加载Gemma tokenizer: {os.path.basename(local_path)}")
                    return _gemma_tokenizer
                except Exception as e:
                    logger.debug(f"[TOKEN_COUNTER] 本地加载失败 {local_path}: {e}")
                    continue
        
        # 如果本地没有，尝试在线下载（可选）
        online_options = [
            ("google/gemma-2b-it", "Gemma 2B IT"),
            ("google/gemma-7b-it", "Gemma 7B IT"),
        ]
        
        last_error = None
        for model_name, display_name in online_options:
            try:
                logger.debug(f"[TOKEN_COUNTER] 尝试在线下载 {display_name}...")
                _gemma_tokenizer = AutoTokenizer.from_pretrained(
                    model_name,
                    trust_remote_code=True,
                    local_files_only=False
                )
                logger.info(f"[TOKEN_COUNTER] 已加载Gemma tokenizer: {display_name}（用于Gemini token计数）")
                return _gemma_tokenizer
            except Exception as e:
                last_error = str(e)
                logger.debug(f"[TOKEN_COUNTER] {display_name} 下载失败: {type(e).__name__}")
                continue
        
        # 所有选项都失败
        logger.info("[TOKEN_COUNTER] Gemma tokenizer不可用，将使用tiktoken（这是正常的，不影响使用）")
        logger.info(f"[TOKEN_COUNTER] 提示：可将tokenizer文件放到 {os.path.join(project_root, 'tokenizers', 'gemma')} 目录")
        return None
        
    except ImportError:
        logger.debug("[TOKEN_COUNTER] transformers未安装，使用tiktoken作为替代")
        return None
    except Exception as e:
        logger.debug(f"[TOKEN_COUNTER] Gemma tokenizer初始化失败: {e}")
        return None

def get_gemini_model():
    """
    获取Gemini模型实例（用于token计数）
    
    Returns:
        Gemini模型实例或None
    """
    global _gemini_model
    
    if _gemini_model is not None:
        return _gemini_model
    
    try:
        import google.generativeai as genai
        
        # 检查是否有API密钥
        api_key = os.environ.get('GOOGLE_API_KEY')
        if not api_key:
            # 尝试从config中获取
            try:
                from core.config_loader import CONFIG
                api_key = CONFIG.get('google_api_key') or CONFIG.get('api_key')
            except:
                pass
        
        if not api_key:
            logger.debug("[TOKEN_COUNTER] Google API密钥未配置，将使用Gemma tokenizer作为替代")
            return None
        
        # 配置API密钥
        genai.configure(api_key=api_key)
        
        # 创建一个用于token计数的模型实例
        # 使用gemini-pro作为默认模型
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
    """
    获取tiktoken编码器
    
    Args:
        model_name: 模型名称
        
    Returns:
        tiktoken编码器实例
    """
    try:
        import tiktoken
        
        # 缓存tokenizer实例
        if model_name in _tiktoken_cache:
            return _tiktoken_cache[model_name]
        
        # 根据模型名称选择合适的编码器
        encoding_name = None
        
        # GPT-4系列
        if any(x in model_name.lower() for x in ['gpt-4', 'gpt4']):
            encoding_name = 'cl100k_base'
        # GPT-3.5系列
        elif any(x in model_name.lower() for x in ['gpt-3.5', 'gpt3.5', 'turbo']):
            encoding_name = 'cl100k_base'
        # Claude系列也可以用cl100k_base作为近似
        elif 'claude' in model_name.lower():
            encoding_name = 'cl100k_base'
        # Gemini系列也使用cl100k_base作为近似
        elif 'gemini' in model_name.lower():
            encoding_name = 'cl100k_base'
        # 默认使用cl100k_base
        else:
            encoding_name = 'cl100k_base'
        
        encoding = tiktoken.get_encoding(encoding_name)
        _tiktoken_cache[model_name] = encoding
        
        logger.info(f"[TOKEN_COUNTER] 为模型 '{model_name}' 加载tokenizer: {encoding_name}")
        return encoding
        
    except ImportError:
        logger.warning("[TOKEN_COUNTER] tiktoken未安装，将使用估算方法")
        return None
    except Exception as e:
        logger.error(f"[TOKEN_COUNTER] 加载tiktoken失败: {e}")
        return None

def count_text_tokens(text: str, model_name: str = "gpt-4") -> int:
    """
    计算文本的token数量（优先使用原生tokenizer，否则使用校准系数）
    
    Args:
        text: 要计算的文本
        model_name: 模型名称
        
    Returns:
        token数量
    """
    if not text:
        return 0
    
    # 🔧 新增：对于DeepSeek模型，优先使用官方tokenizer
    if 'deepseek' in model_name.lower():
        deepseek_tokenizer = get_deepseek_tokenizer()
        if deepseek_tokenizer:
            try:
                tokens = deepseek_tokenizer.encode(text)
                token_count = len(tokens)
                logger.info(f"[TOKEN_COUNTER] ✅ 使用DeepSeek官方tokenizer（模型: {model_name}）: {token_count} tokens")
                return token_count
            except Exception as e:
                logger.warning(f"[TOKEN_COUNTER] DeepSeek tokenizer失败: {e}")
    
    # 对于Gemini模型，优先尝试使用官方tokenizer，然后是Gemma tokenizer
    if 'gemini' in model_name.lower():
        # 1. 优先尝试Google官方tokenizer（需要API密钥）
        gemini_model = get_gemini_model()
        if gemini_model:
            try:
                result = gemini_model.count_tokens(text)
                token_count = result.total_tokens
                logger.info(f"[TOKEN_COUNTER] ✅ 使用Gemini官方tokenizer（模型: {model_name}）: {token_count} tokens")
                return token_count
            except Exception as e:
                logger.warning(f"[TOKEN_COUNTER] Gemini官方tokenizer失败: {e}")
        
        # 2. 尝试使用Gemma tokenizer（Hugging Face transformers）
        gemma_tokenizer = get_gemma_tokenizer()
        if gemma_tokenizer:
            try:
                tokens = gemma_tokenizer.encode(text)
                token_count = len(tokens)
                logger.info(f"[TOKEN_COUNTER] ✅ 使用Gemma tokenizer（模型: {model_name}）: {token_count} tokens")
                return token_count
            except Exception as e:
                logger.warning(f"[TOKEN_COUNTER] Gemma tokenizer失败: {e}")
    
    # 获取模型的校准系数
    multiplier = get_model_multiplier(model_name)
    
    # 尝试使用tiktoken
    encoding = get_tiktoken_encoding(model_name)
    if encoding:
        try:
            tokens = encoding.encode(text)
            base_count = len(tokens)
            # 应用校准系数
            adjusted_count = int(base_count * multiplier)
            
            if multiplier != 1.0:
                logger.info(f"[TOKEN_COUNTER] ✅ 使用Tiktoken（模型: {model_name}, 校准系数{multiplier}）: {base_count} -> {adjusted_count} tokens")
            else:
                logger.info(f"[TOKEN_COUNTER] ✅ 使用Tiktoken（模型: {model_name}）: {adjusted_count} tokens")
            
            return adjusted_count
        except Exception as e:
            logger.error(f"[TOKEN_COUNTER] tiktoken编码失败: {e}")
    
    # 回退到估算（字符数÷4对英文较准，÷2对中文较准）
    # 检测是否主要是中文
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    total_chars = len(text)
    
    if chinese_chars > total_chars * 0.5:
        # 主要是中文，使用÷2
        base_estimate = total_chars // 2
    else:
        # 主要是英文或混合，使用÷4
        base_estimate = total_chars // 4
    
    # 应用校准系数
    return int(base_estimate * multiplier)

def count_messages_tokens(messages: List[Dict[str, Any]], model_name: str = "gpt-4") -> Tuple[int, Dict[str, int]]:
    """
    计算消息列表的token数量（包含消息格式的开销，考虑模型校准系数）
    
    Args:
        messages: OpenAI格式的消息列表
        model_name: 模型名称
        
    Returns:
        (总token数, 详细统计字典)
    """
    total_tokens = 0
    details = {
        'messages': 0,
        'system': 0,
        'user': 0,
        'assistant': 0,
        'overhead': 0,
        'multiplier': get_model_multiplier(model_name)
    }
    
    # 消息格式开销（根据OpenAI的计算方式）
    # 每条消息：<|start|>role\ncontent<|end|>\n = 约4个token
    # 整个对话：<|start|>assistant<|message|> = 约3个token
    
    for message in messages:
        role = message.get('role', 'user')
        content = message.get('content', '')
        
        # 处理多模态内容
        text_content = ''
        if isinstance(content, str):
            text_content = content
        elif isinstance(content, list):
            # 提取文本部分
            for part in content:
                if isinstance(part, dict) and part.get('type') == 'text':
                    text_content += part.get('text', '')
        
        # 计算内容token数（已包含校准系数）
        content_tokens = count_text_tokens(text_content, model_name)
        
        # 添加消息格式开销（约4个token每条消息，也需要应用校准系数）
        overhead_per_message = int(4 * details['multiplier'])
        message_tokens = content_tokens + overhead_per_message
        
        total_tokens += message_tokens
        details['messages'] += message_tokens
        details[role] = details.get(role, 0) + content_tokens
    
    # 添加整体对话开销（也应用校准系数）
    overall_overhead = int((len(messages) * 4 + 3) * details['multiplier'])
    details['overhead'] = overall_overhead
    total_tokens += overall_overhead
    
    return total_tokens, details

def count_response_tokens(response_text: str, model_name: str = "gpt-4") -> int:
    """
    计算响应文本的token数量
    
    Args:
        response_text: 响应文本
        model_name: 模型名称
        
    Returns:
        token数量
    """
    return count_text_tokens(response_text, model_name)

def get_token_counter_info() -> Dict[str, Any]:
    """
    获取token计数器信息
    
    Returns:
        计数器信息字典
    """
    info = {
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

# 导出的便捷函数
def estimate_tokens(text: str, model: str = "gpt-4") -> int:
    """
    便捷函数：估算文本token数
    
    Args:
        text: 文本内容
        model: 模型名称
        
    Returns:
        token数量
    """
    return count_text_tokens(text, model)

def estimate_message_tokens(messages: List[Dict], model: str = "gpt-4") -> int:
    """
    便捷函数：估算消息token数
    
    Args:
        messages: 消息列表
        model: 模型名称
        
    Returns:
        总token数
    """
    total, _ = count_messages_tokens(messages, model)
    return total