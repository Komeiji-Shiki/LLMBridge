"""
OpenAI流格式化工具模块
将响应格式化为OpenAI兼容的SSE格式
"""

import json
import time
import uuid
from typing import Optional, Dict, Any


def format_openai_chunk(content: str, model: str, response_id: str) -> str:
    """
    格式化为OpenAI流式响应块
    
    Args:
        content: 响应内容片段
        model: 模型名称
        response_id: 响应ID
    
    Returns:
        SSE格式的数据字符串
    """
    chunk = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": content},
            "finish_reason": None
        }]
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def format_openai_reasoning_chunk(reasoning: str, model: str, response_id: str) -> str:
    """
    格式化为OpenAI思维链流式响应块
    
    Args:
        reasoning: 思维链内容片段
        model: 模型名称
        response_id: 响应ID
    
    Returns:
        SSE格式的数据字符串
    """
    chunk = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"reasoning_content": reasoning},
            "finish_reason": None
        }]
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def format_openai_finish_chunk(
    model: str, 
    response_id: str, 
    reason: str = 'stop',
    usage: Optional[Dict[str, int]] = None
) -> str:
    """
    格式化为OpenAI流式结束块
    
    Args:
        model: 模型名称
        response_id: 响应ID
        reason: 结束原因
        usage: token使用信息
    
    Returns:
        SSE格式的数据字符串（包含[DONE]）
    """
    chunk = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {},
            "finish_reason": reason
        }]
    }
    
    if usage:
        chunk["usage"] = usage
    
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n"


def format_openai_error_chunk(error: str, model: str, response_id: str) -> str:
    """
    格式化为OpenAI流式错误块
    
    Args:
        error: 错误信息
        model: 模型名称
        response_id: 响应ID
    
    Returns:
        SSE格式的数据字符串
    """
    chunk = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": f"\n\n[Error]: {error}"},
            "finish_reason": None
        }]
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def format_openai_non_stream_response(
    content: str, 
    model: str, 
    response_id: str,
    reason: str = 'stop',
    usage: Optional[Dict[str, int]] = None,
    reasoning_content: Optional[str] = None
) -> Dict[str, Any]:
    """
    格式化为OpenAI非流式响应
    
    Args:
        content: 响应内容
        model: 模型名称
        response_id: 响应ID
        reason: 结束原因
        usage: token使用信息
        reasoning_content: 思维链内容
    
    Returns:
        OpenAI格式的响应字典
    """
    message = {
        "role": "assistant",
        "content": content
    }
    
    if reasoning_content:
        message["reasoning_content"] = reasoning_content
    
    response = {
        "id": response_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": reason,
        }],
        "usage": usage or {
            "prompt_tokens": 0,
            "completion_tokens": len(content) // 4,
            "total_tokens": len(content) // 4,
        },
    }
    
    return response


def generate_response_id() -> str:
    """生成唯一的响应ID"""
    return f"chatcmpl-{uuid.uuid4()}"


class StreamChunkBuilder:
    """
    流式响应块构建器
    
    用于简化流式响应的构建过程
    """
    
    def __init__(self, model: str, response_id: str = None):
        self.model = model
        self.response_id = response_id or generate_response_id()
    
    def content(self, text: str) -> str:
        """构建内容块"""
        return format_openai_chunk(text, self.model, self.response_id)
    
    def reasoning(self, text: str) -> str:
        """构建思维链块"""
        return format_openai_reasoning_chunk(text, self.model, self.response_id)
    
    def finish(self, reason: str = 'stop', usage: Dict[str, int] = None) -> str:
        """构建结束块"""
        return format_openai_finish_chunk(self.model, self.response_id, reason, usage)
    
    def error(self, message: str) -> str:
        """构建错误块"""
        return format_openai_error_chunk(message, self.model, self.response_id)


# 导出
__all__ = [
    'format_openai_chunk',
    'format_openai_reasoning_chunk',
    'format_openai_finish_chunk',
    'format_openai_error_chunk',
    'format_openai_non_stream_response',
    'generate_response_id',
    'StreamChunkBuilder',
]