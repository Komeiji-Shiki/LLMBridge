"""
Direct API 调用服务
用于直接调用第三方API（如Google、DeepSeek等官方API）
同时保持与LMArena统计系统的集成

支持两种模式：
1. 转换模式：将OpenAI格式转换为目标API格式（默认）
2. 透传模式：完全透传请求和响应，不做任何转换
"""

import aiohttp
import json
import logging
import time
from typing import AsyncGenerator, Optional, Dict, Any

logger = logging.getLogger(__name__)


class DirectAPIService:
    """Direct API调用服务"""
    
    @staticmethod
    def split_thinking_content(content: str, separator: str) -> tuple:
        """
        根据分隔符将内容分为思考部分和正文部分
        
        Args:
            content: 完整的响应内容
            separator: 分隔符字符串
        
        Returns:
            (reasoning_content, main_content) 元组
        """
        if not separator or separator not in content:
            return "", content
        
        # 找到分隔符的位置
        separator_index = content.find(separator)
        
        # 分隔符之前的是思考内容
        reasoning_content = content[:separator_index].strip()
        
        # 分隔符之后的是正文（不包括分隔符本身）
        main_content = content[separator_index + len(separator):].strip()
        
        return reasoning_content, main_content
    
    def __init__(self, aiohttp_session: aiohttp.ClientSession = None):
        """
        初始化Direct API服务
        
        Args:
            aiohttp_session: 共享的aiohttp会话（可选）
        """
        self.session = aiohttp_session
        self._own_session = False
        
        if not self.session:
            self.session = aiohttp.ClientSession()
            self._own_session = True
    
    async def close(self):
        """关闭服务"""
        if self._own_session and self.session:
            await self.session.close()
    
    async def call_api(
        self,
        base_url: str,
        api_key: str,
        model: str,
        messages: list,
        stream: bool = False,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        调用第三方API
        
        Args:
            base_url: API基础URL（如 https://api.openai.com/v1）
            api_key: API密钥
            model: 模型名称
            messages: OpenAI格式的消息列表
            stream: 是否流式响应
            temperature: 温度参数
            top_p: top_p参数
            max_tokens: 最大token数
            **kwargs: 其他参数
        
        Yields:
            响应数据块
        """
        # 构建请求URL
        endpoint = f"{base_url.rstrip('/')}/chat/completions"
        
        # 构建请求体
        request_body = {
            "model": model,
            "messages": messages,
            "stream": stream
        }
        
        # 添加可选参数
        if temperature is not None:
            request_body["temperature"] = temperature
        if top_p is not None:
            request_body["top_p"] = top_p
        if max_tokens is not None:
            request_body["max_tokens"] = max_tokens
        
        # 合并其他参数
        request_body.update(kwargs)
        
        # 构建请求头
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        logger.info(f"[DIRECT_API] 调用API: {endpoint}")
        logger.info(f"[DIRECT_API] 模型: {model}, 流式: {stream}")
        
        try:
            async with self.session.post(
                endpoint,
                json=request_body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=1200)  # 20分钟超时
            ) as response:
                # 检查响应状态
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"[DIRECT_API] API调用失败: {response.status} - {error_text}")
                    try:
                        # 尝试将错误解析为JSON，如果可以，就直接返回原始的JSON错误
                        error_json = json.loads(error_text)
                        yield error_json
                    except json.JSONDecodeError:
                        # 如果不是JSON，就封装成一个OpenAI风格的错误格式
                        yield {
                            "error": {
                                "message": error_text,
                                "type": "api_error",
                                "code": response.status
                            }
                        }
                    return
                
                if stream:
                    # 流式响应
                    async for line in response.content:
                        line = line.decode('utf-8').strip()
                        
                        if not line:
                            continue
                        
                        # 处理SSE格式
                        if line.startswith('data: '):
                            data = line[6:]  # 移除 "data: " 前缀
                            
                            if data == '[DONE]':
                                logger.debug("[DIRECT_API] 流式响应结束")
                                yield {"done": True}
                                break
                            
                            try:
                                chunk = json.loads(data)
                                yield chunk
                            except json.JSONDecodeError as e:
                                logger.warning(f"[DIRECT_API] JSON解析失败: {e}, 数据: {data[:100]}")
                                continue
                else:
                    # 非流式响应
                    response_data = await response.json()
                    yield response_data
        
        except aiohttp.ClientError as e:
            logger.error(f"[DIRECT_API] 网络请求失败: {e}")
            yield {
                "error": {
                    "message": f"Network error: {str(e)}",
                    "type": "network_error"
                }
            }
        except Exception as e:
            logger.error(f"[DIRECT_API] 未知错误: {e}", exc_info=True)
            yield {
                "error": {
                    "message": f"Unexpected error: {str(e)}",
                    "type": "internal_error"
                }
            }
    
    async def call_api_non_stream(
        self,
        base_url: str,
        api_key: str,
        model: str,
        messages: list,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        调用第三方API（非流式）
        
        Returns:
            完整的响应字典
        """
        async for chunk in self.call_api(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=messages,
            stream=False,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            **kwargs
        ):
            return chunk
        
        return {"error": True, "message": "No response received"}
    
    def extract_content_from_response(self, response: Dict[str, Any]) -> str:
        """
        从API响应中提取内容
        
        Args:
            response: API响应字典
        
        Returns:
            提取的内容文本
        """
        try:
            if "choices" in response and len(response["choices"]) > 0:
                choice = response["choices"][0]
                
                # 流式响应
                if "delta" in choice:
                    return choice["delta"].get("content", "")
                
                # 非流式响应
                if "message" in choice:
                    return choice["message"].get("content", "")
            
            return ""
        except Exception as e:
            logger.warning(f"[DIRECT_API] 内容提取失败: {e}")
            return ""
    
    def extract_usage_from_response(self, response: Dict[str, Any]) -> Dict[str, int]:
        """
        从API响应中提取token使用情况
        
        Args:
            response: API响应字典
        
        Returns:
            包含input_tokens和output_tokens的字典
        """
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0
        }
        
        try:
            if "usage" in response:
                usage_data = response["usage"]
                usage["input_tokens"] = usage_data.get("prompt_tokens", 0)
                usage["output_tokens"] = usage_data.get("completion_tokens", 0)
                usage["total_tokens"] = usage_data.get("total_tokens", 0)
        except Exception as e:
            logger.warning(f"[DIRECT_API] Token使用信息提取失败: {e}")
        
        return usage
    
    def get_finish_reason(self, response: Dict[str, Any]) -> str:
        """
        从API响应中提取完成原因
        
        Args:
            response: API响应字典
        
        Returns:
            完成原因（stop, length, content_filter等）
        """
        try:
            if "choices" in response and len(response["choices"]) > 0:
                choice = response["choices"][0]
                return choice.get("finish_reason", "stop")
        except Exception as e:
            logger.warning(f"[DIRECT_API] 完成原因提取失败: {e}")
        
        return "stop"
    
    async def call_gemini_native_api(
        self,
        api_key: str,
        model: str,
        messages: list,
        stream: bool = False,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        base_url: Optional[str] = None,
        **kwargs
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        调用Gemini原生API（非OpenAI兼容格式）
        
        Args:
            api_key: Google API密钥
            model: 模型名称（如gemini-2.5-pro）
            messages: OpenAI格式的消息列表（需要转换）
            stream: 是否流式响应
            temperature: 温度参数
            top_p: top_p参数
            max_tokens: 最大token数
            base_url: 自定义API地址（可选，默认使用Google官方地址）
            **kwargs: 其他参数
        
        Yields:
            响应数据块（Gemini原生格式）
        """
        # 构建Gemini API URL
        method = "streamGenerateContent" if stream else "generateContent"
        
        if base_url:
            # 使用自定义地址（如本地反代）
            endpoint = f"{base_url.rstrip('/')}/v1beta/models/{model}:{method}?key={api_key}"
        else:
            # 使用Google官方地址
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:{method}?key={api_key}"
        
        # 转换OpenAI格式消息为Gemini格式
        gemini_contents = []
        system_instruction = None
        
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            
            if role == "system":
                # Gemini使用systemInstruction字段
                system_instruction = {"parts": [{"text": content}]}
            elif role == "user":
                # 处理用户消息，支持多模态内容
                parts = []
                
                # 检查content是否为列表（多模态格式）
                if isinstance(content, list):
                    for item in content:
                        item_type = item.get("type")
                        
                        if item_type == "text":
                            # 文本内容
                            text = item.get("text", "")
                            if text:
                                parts.append({"text": text})
                        
                        elif item_type == "image_url":
                            # 图片内容
                            image_url_data = item.get("image_url", {})
                            url = image_url_data.get("url", "")
                            
                            if url.startswith("data:"):
                                # Base64格式图片
                                # 格式: data:image/jpeg;base64,/9j/4AAQ...
                                try:
                                    # 提取MIME类型和base64数据
                                    header, base64_data = url.split(",", 1)
                                    mime_type = header.split(";")[0].split(":")[1]
                                    
                                    parts.append({
                                        "inline_data": {
                                            "mime_type": mime_type,
                                            "data": base64_data
                                        }
                                    })
                                    logger.debug(f"[GEMINI_NATIVE] 添加base64图片: {mime_type}")
                                except Exception as e:
                                    logger.warning(f"[GEMINI_NATIVE] 解析base64图片失败: {e}")
                            
                            elif url.startswith("http://") or url.startswith("https://"):
                                # HTTP URL格式图片
                                # Gemini需要使用fileData格式
                                parts.append({
                                    "fileData": {
                                        "mimeType": "image/jpeg",  # 默认JPEG，可以从URL推断
                                        "fileUri": url
                                    }
                                })
                                logger.debug(f"[GEMINI_NATIVE] 添加URL图片: {url[:50]}...")
                
                elif isinstance(content, str):
                    # 纯文本格式
                    if content:
                        parts.append({"text": content})
                
                # 如果没有任何内容，添加空文本（避免空parts）
                if not parts:
                    parts.append({"text": " "})
                
                gemini_contents.append({
                    "role": "user",
                    "parts": parts
                })
            
            elif role == "assistant":
                # 处理助手消息
                parts = []
                
                if isinstance(content, str) and content:
                    parts.append({"text": content})
                elif not content:
                    parts.append({"text": " "})
                
                gemini_contents.append({
                    "role": "model",  # Gemini使用"model"而不是"assistant"
                    "parts": parts
                })
        
        # 构建请求体
        request_body = {
            "contents": gemini_contents
        }
        
        # 添加系统指令
        if system_instruction:
            request_body["systemInstruction"] = system_instruction
        
        # 添加生成配置
        generation_config = {}
        if temperature is not None:
            generation_config["temperature"] = temperature
        if top_p is not None:
            generation_config["topP"] = top_p
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens
        
        if generation_config:
            request_body["generationConfig"] = generation_config
        
        # 合并额外的参数（如custom_params中的其他参数）
        if kwargs:
            request_body.update(kwargs)
            logger.info(f"[GEMINI_NATIVE] 已添加额外参数: {kwargs}")
        
        logger.info(f"[GEMINI_NATIVE] 调用Gemini原生API: {endpoint.replace(api_key, '***')}")
        logger.info(f"[GEMINI_NATIVE] 模型: {model}, 流式: {stream}")
        if temperature is not None:
            logger.info(f"[GEMINI_NATIVE] temperature: {temperature}")
        if top_p is not None:
            logger.info(f"[GEMINI_NATIVE] topP: {top_p}")
        if max_tokens is not None:
            logger.info(f"[GEMINI_NATIVE] maxOutputTokens: {max_tokens}")
        
        try:
            async with self.session.post(
                endpoint,
                json=request_body,
                timeout=aiohttp.ClientTimeout(total=1200)
            ) as response:
                # 检查响应状态
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"[GEMINI_NATIVE] API调用失败: {response.status} - {error_text}")
                    try:
                        error_json = json.loads(error_text)
                        yield error_json
                    except json.JSONDecodeError:
                        yield {
                            "error": {
                                "message": error_text,
                                "type": "api_error",
                                "code": response.status
                            }
                        }
                    return
                
                if stream:
                    # 流式响应
                    async for line in response.content:
                        line = line.decode('utf-8').strip()
                        
                        if not line:
                            continue
                        
                        # 处理SSE格式（带data:前缀）
                        if line.startswith('data: '):
                            data = line[6:]  # 移除 "data: " 前缀
                            
                            if data == '[DONE]':
                                logger.debug("[GEMINI_NATIVE] 流式响应结束")
                                yield {"done": True}
                                break
                            
                            try:
                                chunk = json.loads(data)
                                yield chunk
                            except json.JSONDecodeError as e:
                                logger.warning(f"[GEMINI_NATIVE] JSON解析失败: {e}, 数据: {data[:100]}")
                                continue
                        else:
                            # 纯JSON格式（无data:前缀）
                            try:
                                chunk = json.loads(line)
                                yield chunk
                            except json.JSONDecodeError as e:
                                logger.warning(f"[GEMINI_NATIVE] JSON解析失败: {e}, 数据: {line[:100]}")
                                continue
                else:
                    # 非流式响应
                    response_data = await response.json()
                    yield response_data
        
        except aiohttp.ClientError as e:
            logger.error(f"[GEMINI_NATIVE] 网络请求失败: {e}")
            yield {
                "error": {
                    "message": f"Network error: {str(e)}",
                    "type": "network_error"
                }
            }
        except Exception as e:
            logger.error(f"[GEMINI_NATIVE] 未知错误: {e}", exc_info=True)
            yield {
                "error": {
                    "message": f"Unexpected error: {str(e)}",
                    "type": "internal_error"
                }
            }
    
    def convert_gemini_response_to_openai(
        self,
        gemini_response: Dict[str, Any],
        model: str,
        request_id: str,
        is_stream_chunk: bool = False
    ) -> Dict[str, Any]:
        """
        将Gemini原生响应转换为OpenAI格式
        
        Args:
            gemini_response: Gemini原生响应
            model: 模型名称
            request_id: 请求ID
            is_stream_chunk: 是否为流式块
        
        Returns:
            OpenAI格式的响应
        """
        # 检查错误
        if "error" in gemini_response:
            return gemini_response
        
        # 提取内容
        content = ""
        reasoning_content = ""  # 思考内容
        finish_reason = None
        usage = {}
        
        try:
            if "candidates" in gemini_response and len(gemini_response["candidates"]) > 0:
                candidate = gemini_response["candidates"][0]
                
                # 提取文本内容，区分思考和正文
                if "content" in candidate and "parts" in candidate["content"]:
                    parts = candidate["content"]["parts"]
                    for part in parts:
                        if "text" in part:
                            text = part.get("text", "")
                            # 检查是否为思考内容
                            if part.get("thought", False):
                                reasoning_content += text
                            else:
                                content += text
                
                # 提取finish_reason
                if "finishReason" in candidate:
                    gemini_reason = candidate["finishReason"]
                    # 映射Gemini的finishReason到OpenAI格式
                    reason_map = {
                        "STOP": "stop",
                        "MAX_TOKENS": "length",
                        "SAFETY": "content_filter",
                        "RECITATION": "content_filter",
                        "OTHER": "stop"
                    }
                    finish_reason = reason_map.get(gemini_reason, "stop")
            
            # 提取usage信息
            if "usageMetadata" in gemini_response:
                metadata = gemini_response["usageMetadata"]
                
                # 获取思考token数（如果有）
                thoughts_tokens = metadata.get("thoughtsTokenCount", 0)
                
                # 如果没有提供thoughtsTokenCount但有思考内容，进行估算
                if thoughts_tokens == 0 and reasoning_content:
                    # 简单估算：每4个字符约等于1个token
                    thoughts_tokens = len(reasoning_content) // 4
                    logger.info(f"[GEMINI_NATIVE] 估算思考token数: {thoughts_tokens} (基于 {len(reasoning_content)} 字符)")
                
                # 构建usage信息
                prompt_tokens = metadata.get("promptTokenCount", 0)
                candidates_tokens = metadata.get("candidatesTokenCount", 0)
                
                # 思考token应该计入输出token中
                completion_tokens = candidates_tokens + thoughts_tokens
                total_tokens = prompt_tokens + completion_tokens
                
                usage = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens
                }
                
                # 如果有思考token，添加到usage中供参考
                if thoughts_tokens > 0:
                    usage["reasoning_tokens"] = thoughts_tokens
                    logger.info(f"[GEMINI_NATIVE] Token统计 - 输入: {prompt_tokens}, 思考: {thoughts_tokens}, 输出: {candidates_tokens}, 总输出: {completion_tokens}")
        except Exception as e:
            logger.error(f"[GEMINI_NATIVE] 响应转换失败: {e}", exc_info=True)
        
        # 构建OpenAI格式响应
        if is_stream_chunk:
            response = {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": finish_reason
                }],
                "usage": usage if usage else None
            }
            
            # 添加内容到delta
            if reasoning_content:
                response["choices"][0]["delta"]["reasoning_content"] = reasoning_content
            if content:
                response["choices"][0]["delta"]["content"] = content
                
            return response
        else:
            message = {"role": "assistant"}
            
            # 添加思考内容（如果有）
            if reasoning_content:
                message["reasoning_content"] = reasoning_content
            
            # 添加正文内容
            message["content"] = content
            
            return {
                "id": request_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason or "stop"
                }],
                "usage": usage
            }
    
    async def call_api_passthrough(
        self,
        base_url: str,
        api_key: str,
        request_body: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None
    ) -> AsyncGenerator[bytes, None]:
        """
        透传模式：完全透传请求和响应
        
        Args:
            base_url: API基础URL
            api_key: API密钥
            request_body: 原始请求体（不做任何转换）
            headers: 额外的请求头（可选）
        
        Yields:
            原始响应字节流
        """
        # 构建请求URL
        endpoint = f"{base_url.rstrip('/')}/chat/completions"
        
        # 构建请求头
        request_headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        # 合并额外的请求头
        if headers:
            request_headers.update(headers)
        
        is_stream = request_body.get("stream", False)
        
        logger.info(f"[DIRECT_API_PASSTHROUGH] 透传模式调用API: {endpoint}")
        logger.info(f"[DIRECT_API_PASSTHROUGH] 模型: {request_body.get('model')}, 流式: {is_stream}")
        
        try:
            async with self.session.post(
                endpoint,
                json=request_body,
                headers=request_headers,
                timeout=aiohttp.ClientTimeout(total=1200)
            ) as response:
                # 检查响应状态
                if response.status != 200:
                    error_body = await response.read()
                    error_text = error_body.decode(errors='ignore')
                    logger.error(f"[DIRECT_API_PASSTHROUGH] API调用失败: {response.status} - {error_text}")
                    
                    try:
                        # 检查原始错误是否为有效JSON
                        json.loads(error_text)
                        # 如果是，直接透传原始错误
                        yield error_body
                    except json.JSONDecodeError:
                        # 如果不是JSON，则封装成OpenAI兼容的错误格式
                        error_response = {
                            "error": {
                                "message": error_text,
                                "type": "api_error",
                                "code": response.status
                            }
                        }
                        yield json.dumps(error_response).encode('utf-8')
                    return
                
                # 逐块读取并转发原始响应
                async for chunk in response.content.iter_any():
                    if chunk:
                        yield chunk
        
        except aiohttp.ClientError as e:
            logger.error(f"[DIRECT_API_PASSTHROUGH] 网络请求失败: {e}")
            error_response = {
                "error": {
                    "message": f"Network error: {str(e)}",
                    "type": "network_error"
                }
            }
            yield json.dumps(error_response).encode('utf-8')
        except Exception as e:
            logger.error(f"[DIRECT_API_PASSTHROUGH] 未知错误: {e}", exc_info=True)
            error_response = {
                "error": {
                    "message": f"Unexpected error: {str(e)}",
                    "type": "internal_error"
                }
            }
            yield json.dumps(error_response).encode('utf-8')
    
    def calculate_cost(
        self,
        input_tokens: int,
        output_tokens: int,
        pricing: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        计算API调用成本
        
        Args:
            input_tokens: 输入token数
            output_tokens: 输出token数
            pricing: 定价配置字典，包含：
                - input: 输入token单价
                - output: 输出token单价
                - unit: 计价单位（如1000000表示每百万token）
                - currency: 货币单位
        
        Returns:
            包含成本信息的字典
        """
        try:
            input_price = pricing.get("input", 0)
            output_price = pricing.get("output", 0)
            unit = pricing.get("unit", 1000000)
            currency = pricing.get("currency", "USD")
            
            # 计算成本
            input_cost = (input_tokens / unit) * input_price
            output_cost = (output_tokens / unit) * output_price
            total_cost = input_cost + output_cost
            
            return {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "input_cost": round(input_cost, 6),
                "output_cost": round(output_cost, 6),
                "total_cost": round(total_cost, 6),
                "currency": currency,
                "pricing": {
                    "input_price_per_unit": input_price,
                    "output_price_per_unit": output_price,
                    "unit": unit
                }
            }
        except Exception as e:
            logger.error(f"[DIRECT_API] 成本计算失败: {e}")
            return {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "error": str(e)
            }


# 全局服务实例（将在api_server.py中初始化）
direct_api_service: Optional[DirectAPIService] = None


def get_direct_api_service() -> DirectAPIService:
    """获取Direct API服务实例"""
    global direct_api_service
    if direct_api_service is None:
        direct_api_service = DirectAPIService()
    return direct_api_service