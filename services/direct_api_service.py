"""
Direct API 调用服务
用于直接调用第三方API（如Google、DeepSeek等官方API）
同时保持与LMArena统计系统的集成

支持两种模式：
1. 转换模式：将OpenAI格式转换为目标API格式（默认）
2. 透传模式：完全透传请求和响应，不做任何转换
"""

import asyncio
import aiohttp
import json
import logging
import time
import uuid
import base64
from typing import AsyncGenerator, Optional, Dict, Any, List

from core.config_loader import CONFIG

logger = logging.getLogger(__name__)


class DirectAPIService:
    """Direct API调用服务"""
    
    @staticmethod
    def _convert_oai_tools_to_gemini(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        将 OAI 格式的 tools 转换为 Gemini 格式的 tools。
        
        OAI: [{"type": "function", "function": {"name": "get_weather", "description": "...", "parameters": {...}}}]
        Gemini: [{"functionDeclarations": [{"name": "get_weather", "description": "...", "parameters": {...}}]}]
        """
        gemini_tools = []
        function_declarations = []
        
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") != "function":
                continue
            func_def = tool.get("function", {})
            if not isinstance(func_def, dict):
                continue
            
            declaration = {
                "name": func_def.get("name", ""),
                "description": func_def.get("description", ""),
            }
            
            # 转换 parameters（OAI JSON Schema → Gemini 支持的子集）
            params = func_def.get("parameters")
            if isinstance(params, dict):
                declaration["parameters"] = DirectAPIService._convert_schema_for_gemini(params)
            
            function_declarations.append(declaration)
        
        if function_declarations:
            gemini_tools.append({"functionDeclarations": function_declarations})
        
        return gemini_tools
    
    @staticmethod
    def _convert_schema_for_gemini(schema: Dict[str, Any]) -> Dict[str, Any]:
        """
        将 OAI JSON Schema 转换为 Gemini 支持的格式。
        主要是移除 Gemini 不支持的字段。
        """
        result = {}
        for key, value in schema.items():
            if key == "additionalProperties":
                continue  # Gemini 不支持
            elif key == "properties" and isinstance(value, dict):
                result[key] = {k: DirectAPIService._convert_schema_for_gemini(v) for k, v in value.items()}
            elif key == "items" and isinstance(value, dict):
                result[key] = DirectAPIService._convert_schema_for_gemini(value)
            else:
                result[key] = value
        return result
    
    @staticmethod
    def _convert_oai_tool_choice_to_gemini(tool_choice: Any) -> Optional[Dict[str, Any]]:
        """
        将 OAI tool_choice 转换为 Gemini toolConfig。
        
        OAI: {"type": "function", "function": {"name": "get_weather"}} 或 "auto" / "required" / "none"
        Gemini: {"functionCallingConfig": {"mode": "AUTO"|"ANY"|"NONE", "allowed_function_names": [...]}}
        """
        if not tool_choice:
            return None
        
        if isinstance(tool_choice, str):
            mode_map = {
                "auto": "AUTO",
                "required": "ANY",
                "none": "NONE",
            }
            mode = mode_map.get(tool_choice.lower(), "AUTO")
            return {"functionCallingConfig": {"mode": mode}}
        
        if isinstance(tool_choice, dict):
            choice_type = tool_choice.get("type", "auto")
            
            if choice_type == "function":
                func = tool_choice.get("function", {})
                if isinstance(func, dict) and "name" in func:
                    return {
                        "functionCallingConfig": {
                            "mode": "ANY",
                            "allowed_function_names": [func["name"]]
                        }
                    }
            elif choice_type == "auto":
                return {"functionCallingConfig": {"mode": "AUTO"}}
            elif choice_type == "required":
                return {"functionCallingConfig": {"mode": "ANY"}}
            elif choice_type == "none":
                return {"functionCallingConfig": {"mode": "NONE"}}
        
        return {"functionCallingConfig": {"mode": "AUTO"}}
    
    @staticmethod
    def _gemini_function_call_to_oai_tool_call(
        function_call: Dict[str, Any],
        index: int = 0
    ) -> Dict[str, Any]:
        """
        将 Gemini functionCall 转换为 OAI tool_call 格式。
        """
        name = function_call.get("name", "unknown")
        args = function_call.get("args", {})
        
        # 确保 args 是字符串（OAI 要求）
        if isinstance(args, dict):
            args_str = json.dumps(args, ensure_ascii=False)
        else:
            args_str = str(args)
        
        # Gemini 3 返回 id 字段
        tool_call_id = function_call.get("id") or f"call_{uuid.uuid4().hex[:8]}"
        
        tool_call = {
            "index": index,
            "id": tool_call_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": args_str
            }
        }
        
        # 保留 thoughtSignature（如果有）用于后续对话
        thought_signature = function_call.get("thoughtSignature")
        if thought_signature:
            if isinstance(thought_signature, bytes):
                tool_call["_thought_signature"] = base64.b64encode(thought_signature).decode('utf-8')
            else:
                tool_call["_thought_signature"] = thought_signature
        
        return tool_call
    
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
        endpoint_path: str = "/chat/completions",
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
        # 构建请求URL（支持自定义端点路径）
        endpoint_path = (endpoint_path or "/chat/completions").strip()
        if not endpoint_path.startswith("/"):
            endpoint_path = "/" + endpoint_path
        endpoint = f"{base_url.rstrip('/')}{endpoint_path}"
        
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
            request_body_json = await asyncio.to_thread(
                json.dumps,
                request_body,
                ensure_ascii=False,
                separators=(',', ':')
            )
            async with self.session.post(
                endpoint,
                data=request_body_json.encode('utf-8'),
                headers=headers,
                timeout=aiohttp.ClientTimeout(
                    total=CONFIG.get("api_call_timeout_seconds", 3000),
                    sock_read=CONFIG.get("stream_response_timeout_seconds", 3000),
                    sock_connect=CONFIG.get("download_timeout", {}).get("connect", 30)
                )
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
        
        except asyncio.TimeoutError:
            logger.error(f"[DIRECT_API] 请求超时 (模型: {model})")
            yield {
                "error": {
                    "message": "Request timed out while waiting for API response",
                    "type": "timeout_error"
                }
            }
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
        endpoint_path: str = "/chat/completions",
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
            endpoint_path=endpoint_path,
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
        thinking_config: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
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
            thinking_config: 思维链配置（可选）
            tools: OpenAI 格式的 tools 列表（将转换为 Gemini functionDeclarations）
            tool_choice: OAI tool_choice（将转换为 Gemini toolConfig）
            **kwargs: 其他参数
        
        Yields:
            响应数据块（Gemini原生格式）
        """
        # 构建Gemini API URL
        method = "streamGenerateContent" if stream else "generateContent"
        
        # 修复：流式请求必须添加 alt=sse 参数，让 Gemini 返回标准 SSE 格式
        sse_param = "&alt=sse" if stream else ""
        
        if base_url:
            # 使用自定义地址（如本地反代）
            endpoint = f"{base_url.rstrip('/')}/v1beta/models/{model}:{method}?key={api_key}{sse_param}"
        else:
            # 使用Google官方地址
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:{method}?key={api_key}{sse_param}"
        
        # 转换OpenAI格式消息为Gemini格式
        gemini_contents = []
        system_instruction_parts = []
        
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            
            # 处理 tool 角色消息（工具调用结果）
            if role == "tool":
                tool_call_id = msg.get("tool_call_id", "")
                
                # 根据 tool_call_id 推断函数名
                func_name = tool_call_id.split("_", 1)[-1] if "_" in tool_call_id else "_unknown"
                
                function_response_part = {
                    "functionResponse": {
                        "name": func_name,
                        "response": {"content": content if isinstance(content, str) else str(content)},
                        "id": tool_call_id  # Gemini 3 要求的 id 字段
                    }
                }
                
                if isinstance(content, dict):
                    function_response_part["functionResponse"]["response"] = content
                
                gemini_contents.append({
                    "role": "user",  # Gemini 的工具结果使用 user 角色
                    "parts": [function_response_part]
                })
                continue
            
            if role == "system":
                # 处理系统消息，确保提取纯文本
                text_content = ""
                if isinstance(content, str):
                    text_content = content
                elif isinstance(content, list):
                    # 如果是列表（多模态格式），提取所有文本部分
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text_content += item.get("text", "")
                
                if text_content:
                    system_instruction_parts.append({"text": text_content})
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
                                parts.append({
                                    "fileData": {
                                        "mimeType": "image/jpeg",
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
        
        # 添加系统指令（如果有累积的系统消息）
        if system_instruction_parts:
            request_body["systemInstruction"] = {
                "role": "system",
                "parts": system_instruction_parts
            }
        
        # 添加工具声明
        if tools:
            gemini_tools = DirectAPIService._convert_oai_tools_to_gemini(tools)
            if gemini_tools:
                request_body["tools"] = gemini_tools
                logger.info(f"[GEMINI_NATIVE] 已转换 {len(tools)} 个 OAI tools → Gemini tools")
        
        # 添加 toolConfig（工具调用模式）
        if tool_choice:
            tool_config = DirectAPIService._convert_oai_tool_choice_to_gemini(tool_choice)
            if tool_config:
                request_body["toolConfig"] = tool_config
                logger.info(f"[GEMINI_NATIVE] 已设置 toolConfig: {tool_config}")
        
        # 添加生成配置
        generation_config = {}
        if temperature is not None:
            generation_config["temperature"] = temperature
        if top_p is not None:
            generation_config["topP"] = top_p
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens
            
        if thinking_config:
            generation_config["thinkingConfig"] = thinking_config
        
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
            request_body_json = await asyncio.to_thread(
                json.dumps,
                request_body,
                ensure_ascii=False,
                separators=(',', ':')
            )
            async with self.session.post(
                endpoint,
                data=request_body_json.encode('utf-8'),
                headers={
                    "Content-Type": "application/json"
                },
                timeout=aiohttp.ClientTimeout(
                    total=CONFIG.get("api_call_timeout_seconds", 3000),
                    sock_read=CONFIG.get("stream_response_timeout_seconds", 3000),
                    sock_connect=CONFIG.get("download_timeout", {}).get("connect", 30)
                )
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
        
        except asyncio.TimeoutError:
            logger.error(f"[GEMINI_NATIVE] 请求超时 (模型: {model})")
            yield {
                "error": {
                    "message": "Request timed out while waiting for Gemini API response",
                    "type": "timeout_error"
                }
            }
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
        将Gemini原生响应转换为OpenAI格式（支持工具调用转换）
        """
        # 检查错误
        if "error" in gemini_response:
            return gemini_response
        
        # 提取内容
        content = ""
        reasoning_content = ""
        finish_reason = None
        usage = {}
        tool_calls = None  # 工具调用列表
        
        try:
            if "candidates" in gemini_response and len(gemini_response["candidates"]) > 0:
                candidate = gemini_response["candidates"][0]
                
                if "content" in candidate and "parts" in candidate["content"]:
                    parts = candidate["content"]["parts"]
                    for i, part in enumerate(parts):
                        # 处理函数调用
                        if "functionCall" in part:
                            fc = part["functionCall"]
                            if tool_calls is None:
                                tool_calls = []
                            tool_calls.append(
                                DirectAPIService._gemini_function_call_to_oai_tool_call(fc, index=i)
                            )
                        # 处理文本
                        elif "text" in part:
                            text = part.get("text", "")
                            # 检查是否为思考内容
                            if part.get("thought", False):
                                reasoning_content += text
                            else:
                                content += text
                
                # 提取finish_reason
                if "finishReason" in candidate:
                    gemini_reason = candidate["finishReason"]
                    reason_map = {
                        "STOP": "stop",
                        "MAX_TOKENS": "length",
                        "SAFETY": "content_filter",
                        "RECITATION": "content_filter",
                        "OTHER": "stop",
                        "TOOL_USE": "tool_calls",
                    }
                    finish_reason = reason_map.get(gemini_reason, "stop")
            
            # 提取usage信息
            if "usageMetadata" in gemini_response:
                metadata = gemini_response["usageMetadata"]
                thoughts_tokens = metadata.get("thoughtsTokenCount", 0)
                
                if is_stream_chunk:
                    return {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "reasoning_content": reasoning_content if reasoning_content else None,
                                "content": content if content else None,
                                "tool_calls": tool_calls  # 可能为 None
                            },
                            "finish_reason": finish_reason
                        }],
                        "usage": None
                    }

                if thoughts_tokens == 0 and reasoning_content:
                    thoughts_tokens = len(reasoning_content) // 4
                
                prompt_tokens = metadata.get("promptTokenCount", 0)
                candidates_tokens = metadata.get("candidatesTokenCount", 0)
                completion_tokens = candidates_tokens + thoughts_tokens
                
                usage = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                    "reasoning_tokens": thoughts_tokens if thoughts_tokens > 0 else None
                }
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
            
            delta = response["choices"][0]["delta"]
            if reasoning_content:
                delta["reasoning_content"] = reasoning_content
            if content:
                delta["content"] = content
            if tool_calls:
                delta["tool_calls"] = tool_calls
                
            return response
        else:
            message = {"role": "assistant"}
            
            if reasoning_content:
                message["reasoning_content"] = reasoning_content
            
            # 添加工具调用或正文内容
            if tool_calls:
                message["tool_calls"] = tool_calls
                message["content"] = None  # 工具调用时 content 为 null
            else:
                message["content"] = content
            
            return {
                "id": request_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason or ("tool_calls" if tool_calls else "stop")
                }],
                "usage": usage
            }
    
    async def call_api_passthrough(
        self,
        base_url: str,
        api_key: str,
        request_body: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
        endpoint_path: str = "/chat/completions"
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
        # 构建请求URL（支持自定义端点路径）
        endpoint_path = (endpoint_path or "/chat/completions").strip()
        if not endpoint_path.startswith("/"):
            endpoint_path = "/" + endpoint_path
        endpoint = f"{base_url.rstrip('/')}{endpoint_path}"

        # 构建请求头
        request_headers = {
            "Content-Type": "application/json"
        }

        # API Key 可选：本地反代场景可能无需认证
        if api_key:
            # 默认 OpenAI Bearer 方式
            request_headers["Authorization"] = f"Bearer {api_key}"

            # Anthropic /messages 兼容：自动附加 x-api-key 和版本头
            endpoint_lower = endpoint_path.lower()
            if endpoint_lower.endswith("/messages") or endpoint_lower == "/messages":
                request_headers["x-api-key"] = api_key
                request_headers.setdefault("anthropic-version", "2023-06-01")
        
        # 合并额外的请求头
        if headers:
            request_headers.update(headers)
        
        is_stream = request_body.get("stream", False)
        
        logger.info(f"[DIRECT_API_PASSTHROUGH] 透传模式调用API: {endpoint}")
        logger.info(f"[DIRECT_API_PASSTHROUGH] 模型: {request_body.get('model')}, 流式: {is_stream}")
        
        try:
            request_body_json = await asyncio.to_thread(
                json.dumps,
                request_body,
                ensure_ascii=False,
                separators=(',', ':')
            )
            async with self.session.post(
                endpoint,
                data=request_body_json.encode('utf-8'),
                headers=request_headers,
                timeout=aiohttp.ClientTimeout(
                    total=CONFIG.get("api_call_timeout_seconds", 3000),
                    sock_read=CONFIG.get("stream_response_timeout_seconds", 3000),
                    sock_connect=CONFIG.get("download_timeout", {}).get("connect", 30)
                )
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
                
                # 🔧 关键修复：检查Content-Type，防止Cloudflare等返回HTML被当作JSON/SSE透传
                content_type = response.headers.get('Content-Type', '').lower()
                if 'text/html' in content_type:
                    html_body = await response.text()
                    snippet = html_body[:300].replace('\n', ' ')
                    logger.error(f"[DIRECT_API_PASSTHROUGH] 上游返回HTML而非JSON/SSE: {snippet}...")
                    error_response = {
                        "error": {
                            "message": f"上游返回HTML页面（可能被Cloudflare拦截或WAF阻断），请稍后重试。",
                            "type": "upstream_html_error",
                            "code": 502
                        }
                    }
                    yield json.dumps(error_response).encode('utf-8')
                    return
                
                # 关键修复：按 SSE 行边界读取和转发，正确处理粘包问题
                partial_line = b""
                async for chunk, _ in response.content.iter_chunks():
                    if not chunk:
                        continue
                    
                    # 合并之前的残留数据
                    data = partial_line + chunk
                    
                    # 找到最后一个 \n 的位置，之后的部分可能是不完整的行
                    last_newline_pos = data.rfind(b'\n')
                    
                    if last_newline_pos == -1:
                        # 这一整段都没有换行，可能是被分割的不完整行
                        partial_line = data
                        continue
                    
                    # 保留最后一部分作为下一次的残留
                    partial_line = data[last_newline_pos + 1:]
                    # 处理完整的行
                    lines_data = data[:last_newline_pos + 1]
                    
                    # 按 \n\n (SSE事件分隔符) 分割
                    events = lines_data.split(b'\n\n')
                    
                    for i, event in enumerate(events):
                        if not event.strip():
                            continue
                        
                        # 确保事件以 data: 开头
                        event_str = event.decode('utf-8', errors='replace')
                        
                        # 处理事件中可能的多个 data: 行被粘在一起的情况
                        lines = event_str.split('\n')
                        processed_lines = []
                        
                        for line in lines:
                            line = line.rstrip('\r')
                            # 如果一行里有多个 data: ，需要分割
                            if line.startswith('data:') and 'data:' in line[5:]:
                                pos = 5
                                while True:
                                    next_data_pos = line.find('data:', pos)
                                    if next_data_pos == -1:
                                        break
                                    first_part = line[:next_data_pos].rstrip()
                                    if first_part:
                                        processed_lines.append(first_part)
                                    line = line[next_data_pos:]
                                    pos = 5
                            
                            if line.strip():
                                processed_lines.append(line)
                        
                        # 重建事件
                        if processed_lines:
                            processed_event = '\n'.join(processed_lines)
                            # 确保每个事件以 \n\n 结尾
                            yield (processed_event + '\n\n').encode('utf-8')
                
                # 处理最后残留的数据
                if partial_line.strip():
                    try:
                        final_line = partial_line.decode('utf-8', errors='replace').strip()
                        if final_line:
                            yield (final_line + '\n\n').encode('utf-8')
                    except:
                        pass
        
        except asyncio.TimeoutError:
            logger.error(f"[DIRECT_API_PASSTHROUGH] 请求超时 (模型: {request_body.get('model')})")
            error_response = {
                "error": {
                    "message": "Request timed out while waiting for API response",
                    "type": "timeout_error"
                }
            }
            yield json.dumps(error_response).encode('utf-8')
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
        pricing: Dict[str, Any],
        cached_tokens: int = 0
    ) -> Dict[str, Any]:
        """
        计算API调用成本
        """
        try:
            input_price = pricing.get("input", 0)
            output_price = pricing.get("output", 0)
            cached_input_price = pricing.get("cached_input")  # None 表示未配置
            unit = pricing.get("unit", 1000000)
            currency = pricing.get("currency", "USD")
            
            if cached_input_price is not None:
                # 已配置缓存价格：拆分计算
                uncached_input_tokens = max(0, input_tokens - cached_tokens)
                input_cost = (uncached_input_tokens / unit) * input_price
                cached_cost = (cached_tokens / unit) * cached_input_price
            else:
                # 未配置缓存价格：全部输入token按输入价格计，缓存不单独列出
                input_cost = (input_tokens / unit) * input_price
                cached_cost = 0.0
            output_cost = (output_tokens / unit) * output_price
            total_cost = input_cost + cached_cost + output_cost
            
            return {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_tokens": cached_tokens,
                "total_tokens": input_tokens + output_tokens,
                "input_cost": round(input_cost, 6),
                "cached_cost": round(cached_cost, 6),
                "output_cost": round(output_cost, 6),
                "total_cost": round(total_cost, 6),
                "currency": currency,
                "pricing": {
                    "input_price_per_unit": input_price,
                    "output_price_per_unit": output_price,
                    "cached_input_price_per_unit": cached_input_price,
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


def get_direct_api_service(aiohttp_session: aiohttp.ClientSession = None) -> DirectAPIService:
    """
    获取Direct API服务实例
    """
    global direct_api_service
    if direct_api_service is None:
        direct_api_service = DirectAPIService(aiohttp_session=aiohttp_session)
    elif aiohttp_session is not None and direct_api_service.session != aiohttp_session:
        if direct_api_service._own_session:
            pass
        direct_api_service.session = aiohttp_session
        direct_api_service._own_session = False
    return direct_api_service


def init_direct_api_service(aiohttp_session: aiohttp.ClientSession) -> DirectAPIService:
    """
    初始化Direct API服务实例（应在应用启动时调用）
    """
    global direct_api_service
    direct_api_service = DirectAPIService(aiohttp_session=aiohttp_session)
    logger.info("[DIRECT_API] ✅ Direct API服务已初始化（使用共享连接池）")
    return direct_api_service