"""
Token计数模块 - 用量追踪、状态显示与异步封装
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from ._types import (
    _anthropic_client,
    _deepseek_tokenizer,
    _gemini_model,
    _gemma_tokenizer,
    _tiktoken_cache,
    UsageDict,
)
from ._tokenizers import (
    get_tokenizer_for_model,
    get_tokenizer_memory_info,
)
from ._counting import (
    estimate_tokens,
    estimate_message_tokens,
    count_text_tokens,
    smart_token_estimate,
)
from ._custom import (
    load_custom_tokenizers_config,
    get_custom_tokenizer,
)

logger = logging.getLogger(__name__)


def get_all_tokenizers_status() -> Dict[str, Any]:
    """获取所有分词器的详细状态信息"""
    status = {
        'tiktoken': {'name':'Tiktoken (OpenAI)','available':False,'version':None,'loaded':False,'install_cmd':'pip install tiktoken','description':'OpenAI官方分词器，适用于GPT系列模型（轻量级，约5MB）','supported_models':['gpt-4','gpt-3.5-turbo','gpt-4-turbo','chatgpt']},
        'anthropic': {'name':'Anthropic','available':False,'version':None,'loaded':False,'install_cmd':'pip install anthropic','description':'Anthropic官方分词器，适用于Claude系列模型','supported_models':['claude-3-opus','claude-3-sonnet','claude-3-haiku','claude-3.5-sonnet']},
        'transformers': {'name':'Transformers (HuggingFace)','available':False,'version':None,'loaded':False,'install_cmd':'pip install transformers','description':'用于加载Gemma/DeepSeek等本地分词器','supported_models':['gemini (via Gemma)','deepseek']},
        'google_generativeai': {'name':'Google Generative AI','available':False,'version':None,'loaded':False,'install_cmd':'pip install google-generativeai','description':'Google官方API，支持Gemini模型token计数（需要API Key）','supported_models':['gemini-pro','gemini-1.5','gemini-2']},
        'gemma_local': {'name':'Gemma Tokenizer (本地)','available':False,'path':None,'loaded':_gemma_tokenizer is not None,'description':'本地Gemma分词器文件','supported_models':['gemini系列']},
        'deepseek_local': {'name':'DeepSeek Tokenizer (本地)','available':False,'path':None,'loaded':_deepseek_tokenizer is not None,'description':'本地DeepSeek分词器文件','supported_models':['deepseek-chat','deepseek-coder','deepseek-v3']}
    }
    try:
        import tiktoken
        status['tiktoken']['available'] = True
        status['tiktoken']['version'] = getattr(tiktoken,'__version__','unknown')
        status['tiktoken']['loaded'] = len(_tiktoken_cache) > 0
    except ImportError:
        pass
    try:
        import anthropic
        status['anthropic']['available'] = True
        status['anthropic']['version'] = getattr(anthropic,'__version__','unknown')
        status['anthropic']['loaded'] = _anthropic_client is not None
    except ImportError:
        pass
    try:
        import transformers
        status['transformers']['available'] = True
        status['transformers']['version'] = getattr(transformers,'__version__','unknown')
    except ImportError:
        pass
    try:
        import google.generativeai as genai
        status['google_generativeai']['available'] = True
        status['google_generativeai']['version'] = getattr(genai,'__version__','unknown')
        status['google_generativeai']['loaded'] = _gemini_model is not None
    except ImportError:
        pass
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    gemma_paths = [os.path.join(project_root,"tokenizers","gemma3-27b-it"),os.path.join(project_root,"tokenizers","gemma-2b-it"),os.path.join(project_root,"tokenizers","gemma")]
    for path in gemma_paths:
        if os.path.exists(path) and (os.path.exists(os.path.join(path,"tokenizer.json")) or os.path.exists(os.path.join(path,"tokenizer_config.json"))):
            status['gemma_local']['available'] = True
            status['gemma_local']['path'] = path
            break
    status['gemma_local']['loaded'] = _gemma_tokenizer is not None
    deepseek_paths = [os.path.join(project_root,"deepseek_v3_tokenizer"),os.path.join(project_root,"tokenizers","dsv3.2")]
    for deepseek_path in deepseek_paths:
        if os.path.exists(deepseek_path) and (os.path.exists(os.path.join(deepseek_path,"tokenizer.json")) or os.path.exists(os.path.join(deepseek_path,"tokenizer_config.json"))):
            status['deepseek_local']['available'] = True
            status['deepseek_local']['path'] = deepseek_path
            break
    status['deepseek_local']['loaded'] = _deepseek_tokenizer is not None
    status['_memory_info'] = get_tokenizer_memory_info()
    return status


def calculate_tokens_for_text(text: str, tokenizers: List[str] = None) -> Dict[str, Any]:
    """使用多种分词器计算文本的token数量"""
    if not text:
        return {'error':'文本为空','results':{}}
    results = {}
    text_info = {'char_count':len(text),'word_count':len(text.split()),'chinese_char_count':sum(1 for c in text if '\u4e00'<=c<='\u9fff'),'line_count':text.count('\n')+1}
    if tokenizers is None or 'tiktoken_cl100k' in tokenizers:
        try:
            import tiktoken
            results['tiktoken_cl100k'] = {'name':'Tiktoken (cl100k_base)','token_count':len(tiktoken.get_encoding('cl100k_base').encode(text)),'model_hint':'GPT-4, GPT-3.5-turbo'}
        except ImportError:
            results['tiktoken_cl100k'] = {'error':'未安装tiktoken','install_cmd':'pip install tiktoken'}
        except Exception as e:
            results['tiktoken_cl100k'] = {'error':str(e)}
    if tokenizers is None or 'tiktoken_o200k' in tokenizers:
        try:
            import tiktoken
            results['tiktoken_o200k'] = {'name':'Tiktoken (o200k_base)','token_count':len(tiktoken.get_encoding('o200k_base').encode(text)),'model_hint':'GPT-4o'}
        except ImportError:
            results['tiktoken_o200k'] = {'error':'未安装tiktoken','install_cmd':'pip install tiktoken'}
        except Exception as e:
            results['tiktoken_o200k'] = {'error':str(e)}
    if tokenizers is None or 'anthropic' in tokenizers:
        try:
            import anthropic
            results['anthropic'] = {'name':'Anthropic (Claude)','token_count':anthropic.Anthropic(api_key="dummy").count_tokens(text),'model_hint':'Claude-3系列'}
        except ImportError:
            results['anthropic'] = {'error':'未安装anthropic','install_cmd':'pip install anthropic'}
        except Exception as e:
            try:
                import tiktoken
                results['anthropic'] = {'name':'Anthropic (Claude) - 估算','token_count':len(tiktoken.get_encoding('cl100k_base').encode(text)),'model_hint':'Claude-3系列 (使用tiktoken估算)','note':'使用tiktoken cl100k_base作为近似'}
            except:
                results['anthropic'] = {'error':str(e)}
    if tokenizers is None or 'gemma' in tokenizers:
        from ._tokenizers import get_gemma_tokenizer
        gt = get_gemma_tokenizer()
        if gt:
            try:
                results['gemma'] = {'name':'Gemma Tokenizer','token_count':len(gt.encode(text)),'model_hint':'Gemini系列'}
            except Exception as e:
                results['gemma'] = {'error':str(e)}
        else:
            results['gemma'] = {'error':'未加载Gemma tokenizer','hint':'需要transformers库和本地tokenizer文件'}
    if tokenizers is None or 'deepseek' in tokenizers:
        from ._tokenizers import get_deepseek_tokenizer
        dt = get_deepseek_tokenizer()
        if dt:
            try:
                results['deepseek'] = {'name':'DeepSeek Tokenizer','token_count':len(dt.encode(text)),'model_hint':'DeepSeek系列'}
            except Exception as e:
                results['deepseek'] = {'error':str(e)}
        else:
            results['deepseek'] = {'error':'未加载DeepSeek tokenizer','hint':'需要transformers库和本地tokenizer文件'}
    if tokenizers is None or 'estimate' in tokenizers:
        results['estimate'] = smart_token_estimate(text)
    for name, cfg in load_custom_tokenizers_config().items():
        if tokenizers is not None and f'custom_{name}' not in tokenizers and name not in tokenizers:
            continue
        ct, is_tiktoken = get_custom_tokenizer(name)
        if ct:
            try:
                tokens = ct.encode(text,allowed_special="all") if is_tiktoken else ct.encode(text)
                results[f'custom_{name}'] = {'name':cfg.get('display_name',name),'token_count':len(tokens),'model_hint':', '.join(cfg.get('supported_models',[])) or '自定义分词器','source':cfg.get('source'),'source_type':cfg.get('source_type'),'is_custom':True,'is_tiktoken_model':is_tiktoken}
            except Exception as e:
                results[f'custom_{name}'] = {'name':cfg.get('display_name',name),'error':str(e),'is_custom':True}
        else:
            results[f'custom_{name}'] = {'name':cfg.get('display_name',name),'error':'分词器加载失败','source':cfg.get('source'),'is_custom':True}
    return {'text_info':text_info,'results':results}


def compare_tokenizers(text:str,tokenizer1:str,tokenizer2:str)->Dict[str,Any]:
    r = calculate_tokens_for_text(text,[tokenizer1,tokenizer2])
    r1 = r['results'].get(tokenizer1,{})
    r2 = r['results'].get(tokenizer2,{})
    c1 = r1.get('token_count',0) if 'error' not in r1 else None
    c2 = r2.get('token_count',0) if 'error' not in r2 else None
    comp = {'text_info':r['text_info'],'tokenizer1':{'id':tokenizer1,**r1},'tokenizer2':{'id':tokenizer2,**r2}}
    if c1 is not None and c2 is not None:
        d = c1-c2
        rat = c1/c2 if c2>0 else 0
        comp['difference'] = {'absolute':d,'ratio':round(rat,4),'percentage':f"{(rat-1)*100:.2f}%" if rat>0 else "N/A"}
    return comp


def calculate_request_tokens(messages:List[Dict[str,Any]],model:str,monitoring_service=None,request_id:str=None)->int:
    t = 0
    if not messages and monitoring_service and request_id and hasattr(monitoring_service,'active_requests') and request_id in monitoring_service.active_requests:
        ri = monitoring_service.active_requests[request_id]
        if ri.request_messages:
            messages = ri.request_messages
    if not messages:
        return 0
    try:
        t = estimate_message_tokens(messages,model)
    except Exception as e:
        logger.warning(f"[TOKEN_SERVICE] token计算失败: {e}")
        for msg in messages:
            if isinstance(msg,dict) and 'content' in msg:
                c = msg.get('content','')
                if isinstance(c,str):
                    t += len(c)//4
                elif isinstance(c,list):
                    for p in c:
                        if isinstance(p,dict) and p.get('type')=='text':
                            t += len(p.get('text',''))//4
    return t


def calculate_response_tokens(response_content:str,model:str)->int:
    if not response_content:
        return 0
    try:
        return estimate_tokens(response_content,model)
    except Exception as e:
        logger.warning(f"[TOKEN_SERVICE] token计算输出失败: {e}")
        return len(response_content)//4


def calculate_full_usage(messages:List[Dict[str,Any]],response_content:str,model:str,lmarena_usage:Dict[str,int]=None,monitoring_service=None,request_id:str=None)->Dict[str,int]:
    if lmarena_usage:
        i = lmarena_usage.get('inputTokens',0) or lmarena_usage.get('prompt_tokens',0)
        o = lmarena_usage.get('outputTokens',0) or lmarena_usage.get('completion_tokens',0)
    else:
        i = calculate_request_tokens(messages,model,monitoring_service,request_id)
        o = calculate_response_tokens(response_content,model)
    return {"prompt_tokens":i,"completion_tokens":o,"total_tokens":i+o}


def record_request_end_with_tokens(monitoring_service,request_id:str,success:bool,messages=None,response_content=None,reasoning_content=None,model=None,lmarena_usage=None,error=None)->Tuple[int,int]:
    if not model and hasattr(monitoring_service,'active_requests') and request_id in monitoring_service.active_requests:
        model = getattr(monitoring_service.active_requests[request_id],'model',None)
    usage = calculate_full_usage(messages=messages,response_content=response_content or "",model=model or "",lmarena_usage=lmarena_usage,monitoring_service=monitoring_service,request_id=request_id) if model else {"prompt_tokens":0,"completion_tokens":0}
    monitoring_service.request_end(request_id,success=success,error=error,response_content=response_content,reasoning_content=reasoning_content,input_tokens=usage["prompt_tokens"],output_tokens=usage["completion_tokens"])
    return usage["prompt_tokens"],usage["completion_tokens"]


class TokenUsageTracker:
    def __init__(self,model:str,monitoring_service=None,request_id=None):
        self.model=model;self.monitoring_service=monitoring_service;self.request_id=request_id
        self.collected_content=[];self.reasoning_content=[];self.lmarena_usage=None;self.input_messages=[]
    def add_content(self,c):
        if c: self.collected_content.append(c)
    def add_reasoning(self,r):
        if r: self.reasoning_content.append(r)
    def set_lmarena_usage(self,u):
        self.lmarena_usage=u
    def set_input_messages(self,m):
        self.input_messages=m
    def get_full_response(self):
        return "".join(self.collected_content)
    def get_full_reasoning(self):
        return "".join(self.reasoning_content) if self.reasoning_content else None
    def calculate_usage(self):
        return calculate_full_usage(messages=self.input_messages,response_content=self.get_full_response(),model=self.model,lmarena_usage=self.lmarena_usage,monitoring_service=self.monitoring_service,request_id=self.request_id)
    def record_end(self,success,error=None):
        return record_request_end_with_tokens(monitoring_service=self.monitoring_service,request_id=self.request_id,success=success,messages=self.input_messages,response_content=self.get_full_response(),reasoning_content=self.get_full_reasoning(),model=self.model,lmarena_usage=self.lmarena_usage,error=error)


async def estimate_tokens_async(text:str,model:str="gpt-4")->int:
    if not text: return 0
    return await asyncio.to_thread(count_text_tokens,text,model)

async def estimate_message_tokens_async(messages:List[Dict],model:str="gpt-4")->int:
    if not messages: return 0
    return await asyncio.to_thread(estimate_message_tokens,messages,model)

async def calculate_full_usage_async(messages,response_content,model,lmarena_usage=None,monitoring_service=None,request_id=None)->Dict[str,int]:
    if lmarena_usage:
        i = lmarena_usage.get('inputTokens',0) or lmarena_usage.get('prompt_tokens',0)
        o = lmarena_usage.get('outputTokens',0) or lmarena_usage.get('completion_tokens',0)
    else:
        i,o = await asyncio.gather(estimate_message_tokens_async(messages or [],model),estimate_tokens_async(response_content or "",model))
    return {"prompt_tokens":i,"completion_tokens":o,"total_tokens":i+o}
