"""
Token计数模块 - 自定义分词器管理
"""

from __future__ import annotations
from core.tokenizer_trust import remote_code_allowed

import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List

from ._types import (
    _custom_tokenizers,
    _custom_tokenizers_config,
    _tiktoken_model_cache,
)

logger = logging.getLogger(__name__)


def install_tokenizer_package(package_name: str) -> Dict[str, Any]:
    import subprocess
    import sys
    allowed_packages = {'tiktoken':'tiktoken','anthropic':'anthropic','transformers':'transformers','google-generativeai':'google-generativeai'}
    if package_name not in allowed_packages:
        return {'success':False,'error':f'不允许安装的包: {package_name}','allowed':list(allowed_packages.keys())}
    try:
        result = subprocess.run([sys.executable,'-m','pip','install',package_name],capture_output=True,text=True,timeout=120)
        if result.returncode==0:
            logger.info(f"[TOKEN_COUNTER] ✅ 成功安装 {package_name}")
            return {'success':True,'package':package_name,'message':f'成功安装 {package_name}','output':result.stdout[-500:] if len(result.stdout)>500 else result.stdout}
        else:
            logger.error(f"[TOKEN_COUNTER] ❌ 安装 {package_name} 失败: {result.stderr}")
            return {'success':False,'package':package_name,'error':result.stderr[-500:] if len(result.stderr)>500 else result.stderr}
    except subprocess.TimeoutExpired:
        return {'success':False,'package':package_name,'error':'安装超时（120秒）'}
    except Exception as e:
        return {'success':False,'package':package_name,'error':str(e)}


def load_custom_tokenizers_config() -> Dict[str, Any]:
    global _custom_tokenizers_config
    if _custom_tokenizers_config is not None:
        return _custom_tokenizers_config
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'custom_tokenizers.json')
    if os.path.exists(config_path):
        try:
            with open(config_path,'r',encoding='utf-8') as f:
                _custom_tokenizers_config = json.load(f)
                logger.info(f"[TOKEN_COUNTER] 已加载 {len(_custom_tokenizers_config)} 个自定义分词器配置")
                return _custom_tokenizers_config
        except Exception as e:
            logger.warning(f"[TOKEN_COUNTER] 加载自定义分词器配置失败: {e}")
    _custom_tokenizers_config = {}
    return _custom_tokenizers_config


def save_custom_tokenizers_config(config: Dict[str, Any]) -> bool:
    global _custom_tokenizers_config
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'custom_tokenizers.json')
    try:
        with open(config_path,'w',encoding='utf-8') as f:
            json.dump(config,f,indent=2,ensure_ascii=False)
        _custom_tokenizers_config = config
        logger.info(f"[TOKEN_COUNTER] 已保存 {len(config)} 个自定义分词器配置")
        return True
    except Exception as e:
        logger.error(f"[TOKEN_COUNTER] 保存自定义分词器配置失败: {e}")
        return False


def load_tiktoken_model_tokenizer(model_path: str, tokenizer_name: str = None):
    global _tiktoken_model_cache
    cache_key = tokenizer_name or model_path
    if cache_key in _tiktoken_model_cache:
        return _tiktoken_model_cache[cache_key]
    try:
        import tiktoken
        from tiktoken.load import load_tiktoken_bpe
        if os.path.isfile(model_path):
            vocab_file = model_path
        elif os.path.isdir(model_path):
            vocab_file = os.path.join(model_path,'tiktoken.model')
            if not os.path.exists(vocab_file):
                logger.warning(f"[TOKEN_COUNTER] tiktoken.model文件不存在: {vocab_file}")
                return None
        else:
            logger.warning(f"[TOKEN_COUNTER] 路径不存在: {model_path}")
            return None
        mergeable_ranks = load_tiktoken_bpe(vocab_file)
        num_base_tokens = len(mergeable_ranks)
        num_reserved_special_tokens = 256
        special_tokens = {f"<|reserved_token_{i}|>":i for i in range(num_base_tokens,num_base_tokens+num_reserved_special_tokens)}
        common_special_tokens = ["[BOS]","[EOS]","<|im_end|>","<|im_user|>","<|im_assistant|>","<|start_header_id|>","<|end_header_id|>","[EOT]","<|im_system|>","<|im_middle|>"]
        for i,token in enumerate(common_special_tokens):
            if token not in special_tokens:
                special_tokens[token] = num_base_tokens + i
        pat_str = "|".join([
            r"""[\p{Han}]+""",
            r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}&&[^\p{Han}]]*[\p{Ll}\p{Lm}\p{Lo}\p{M}&&[^\p{Han}]]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
            r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}&&[^\p{Han}]]+[\p{Ll}\p{Lm}\p{Lo}\p{M}&&[^\p{Han}]]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
            r"""\p{N}{1,3}""",
            r""" ?[^\s\p{L}\p{N}]+[\r\n]*""",
            r"""\s*[\r\n]+""",
            r"""\s+(?!\S)""",
            r"""\s+""",
        ])
        encoding = tiktoken.Encoding(name=os.path.basename(vocab_file),pat_str=pat_str,mergeable_ranks=mergeable_ranks,special_tokens=special_tokens)
        logger.info(f"[TOKEN_COUNTER] ✅ 已加载tiktoken.model分词器: {vocab_file} (词表大小: {num_base_tokens})")
        _tiktoken_model_cache[cache_key] = encoding
        return encoding
    except ImportError as e:
        logger.warning(f"[TOKEN_COUNTER] tiktoken未安装: {e}")
        return None
    except Exception as e:
        logger.error(f"[TOKEN_COUNTER] 加载tiktoken.model失败: {e}",exc_info=True)
        return None


def add_custom_tokenizer(name:str,source_type:str,source:str,display_name:str=None,description:str=None,supported_models:List[str]=None)->Dict[str,Any]:
    global _custom_tokenizers,_tiktoken_model_cache
    if not name or not source_type or not source:
        return {'success':False,'error':'缺少必要参数: name, source_type, source'}
    if not re.match(r'^[a-zA-Z0-9_-]+$',name):
        return {'success':False,'error':'名称只能包含字母、数字、下划线和横杠'}
    config = load_custom_tokenizers_config()
    if name in config:
        return {'success':False,'error':f'分词器 {name} 已存在'}
    tokenizer = None
    is_tiktoken_model = False
    try:
        if source_type=='tiktoken_model':
            is_tiktoken_model = True
            if not os.path.exists(source):
                return {'success':False,'error':f'路径不存在: {source}'}
            tokenizer = load_tiktoken_model_tokenizer(source,name)
            if tokenizer is None:
                return {'success':False,'error':'加载tiktoken.model分词器失败'}
            logger.info(f"[TOKEN_COUNTER] ✅ 成功加载tiktoken.model分词器: {source}")
        else:
            from transformers import AutoTokenizer
            import warnings
            warnings.filterwarnings('ignore')
            if source_type=='huggingface':
                logger.info(f"[TOKEN_COUNTER] 正在从HuggingFace下载分词器: {source}")
                tokenizer = AutoTokenizer.from_pretrained(source,trust_remote_code=remote_code_allowed(source))
                logger.info(f"[TOKEN_COUNTER] ✅ 成功下载分词器: {source}")
            elif source_type=='local':
                if not os.path.exists(source):
                    return {'success':False,'error':f'本地路径不存在: {source}'}
                tokenizer = AutoTokenizer.from_pretrained(source,local_files_only=True,trust_remote_code=remote_code_allowed(source))
            else:
                return {'success':False,'error':f'不支持的来源类型: {source_type}。支持: huggingface, local, tiktoken_model'}
    except ImportError:
        if source_type=='tiktoken_model':
            return {'success':False,'error':'tiktoken库未安装，请先安装: pip install tiktoken'}
        return {'success':False,'error':'transformers库未安装，请先安装: pip install transformers'}
    except Exception as e:
        logger.error(f"[TOKEN_COUNTER] 加载分词器失败: {e}")
        return {'success':False,'error':f'加载分词器失败: {str(e)}'}
    try:
        test_text = "Hello, 你好！This is a test. 这是测试。"
        if is_tiktoken_model:
            tokens = tokenizer.encode(test_text,allowed_special="all")
        else:
            tokens = tokenizer.encode(test_text)
        token_count = len(tokens)
        logger.info(f"[TOKEN_COUNTER] 分词器测试成功: '{test_text}' = {token_count} tokens")
    except Exception as e:
        return {'success':False,'error':f'分词器测试失败: {str(e)}'}
    if is_tiktoken_model:
        _tiktoken_model_cache[name] = tokenizer
    else:
        _custom_tokenizers[name] = tokenizer
    config[name] = {'name':name,'display_name':display_name or name,'source_type':source_type,'source':source,'description':description or f'从 {source} 加载的自定义分词器','supported_models':supported_models or [],'created_at':datetime.now().isoformat()}
    if save_custom_tokenizers_config(config):
        return {'success':True,'name':name,'message':f'成功添加分词器 {display_name or name}','test_result':f'测试文本 "{test_text}" = {token_count} tokens'}
    else:
        return {'success':False,'error':'保存配置失败'}


def delete_custom_tokenizer(name:str)->Dict[str,Any]:
    global _custom_tokenizers
    config = load_custom_tokenizers_config()
    if name not in config:
        return {'success':False,'error':f'分词器 {name} 不存在'}
    del config[name]
    if name in _custom_tokenizers:
        del _custom_tokenizers[name]
    if save_custom_tokenizers_config(config):
        return {'success':True,'name':name,'message':f'成功删除分词器 {name}'}
    else:
        return {'success':False,'error':'保存配置失败'}


def get_custom_tokenizer(name:str):
    global _custom_tokenizers,_tiktoken_model_cache
    if name in _custom_tokenizers:
        return _custom_tokenizers[name],False
    if name in _tiktoken_model_cache:
        return _tiktoken_model_cache[name],True
    config = load_custom_tokenizers_config()
    if name not in config:
        return None,False
    tokenizer_config = config[name]
    source_type = tokenizer_config.get('source_type','huggingface')
    try:
        if source_type=='tiktoken_model':
            tokenizer = load_tiktoken_model_tokenizer(tokenizer_config['source'],name)
            if tokenizer:
                return tokenizer,True
            return None,True
        from transformers import AutoTokenizer
        import warnings
        warnings.filterwarnings('ignore')
        if source_type=='huggingface':
            tokenizer = AutoTokenizer.from_pretrained(tokenizer_config['source'],trust_remote_code=remote_code_allowed(tokenizer_config['source']))
        elif source_type=='local':
            tokenizer = AutoTokenizer.from_pretrained(tokenizer_config['source'],local_files_only=True,trust_remote_code=remote_code_allowed(tokenizer_config['source']))
        else:
            return None,False
        _custom_tokenizers[name] = tokenizer
        return tokenizer,False
    except Exception as e:
        logger.warning(f"[TOKEN_COUNTER] 加载自定义分词器 {name} 失败: {e}")
        return None,False


def list_custom_tokenizers()->Dict[str,Any]:
    config = load_custom_tokenizers_config()
    result = []
    for name,cfg in config.items():
        tokenizer,is_tiktoken = get_custom_tokenizer(name)
        available = tokenizer is not None
        result.append({'name':cfg.get('name',name),'display_name':cfg.get('display_name',name),'source_type':cfg.get('source_type'),'source':cfg.get('source'),'description':cfg.get('description'),'supported_models':cfg.get('supported_models',[]),'available':available,'is_tiktoken_model':is_tiktoken,'created_at':cfg.get('created_at')})
    return {'count':len(result),'tokenizers':result}
