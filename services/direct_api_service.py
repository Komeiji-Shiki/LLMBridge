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
import mimetypes
import re
import time
import uuid
import base64
from typing import AsyncGenerator, Optional, Dict, Any, List, Tuple
from urllib.parse import quote

from core.config_loader import CONFIG
from converters.gemini_interactions import build_interactions_request_body
from utils.usage_tokens import MODE_MERGE, compose_chat_usage

logger = logging.getLogger(__name__)


_SSE_EVENT_SEPARATOR = re.compile(rb"\r?\n\r?\n")


def _extract_complete_sse_events(buffer: bytes, search_start: int = 0) -> Tuple[List[bytes], bytes]:
    """从字节缓冲中提取所有完整的 SSE 事件（以空行分隔）。

    SSE 规范中事件之间以空行（LF 或 CRLF）分隔；上游分包可能把一个事件
    从中间切断（例如携带签名的 signature_delta），因此这里只输出已收到
    完整终止符的事件，剩余不完整数据留给后续分包拼全。

    返回 (完整事件列表, 剩余不完整数据)。事件内容原样保留（含 event: 行），
    分隔符统一为 LF 空行。

    search_start: 分隔符扫描起点。🔧 性能修复：调用方持续追加数据时，
    已确认无分隔符的前缀无需重复扫描（旧版每个 TCP 分包都对整个缓冲
    全量正则扫描，超大单事件下退化为 O(n²)）；分隔符最长 4 字节，
    调用方应从剩余缓冲末尾回退 3 字节作为下次起点。
    """
    events: List[bytes] = []
    pos = 0
    for match in _SSE_EVENT_SEPARATOR.finditer(buffer, search_start):
        # bytes() 拷贝：兼容 bytearray 缓冲，事件对外始终是不可变 bytes
        event_bytes = bytes(buffer[pos:match.start()]) + b"\n\n"
        if event_bytes.strip():
            events.append(event_bytes)
        pos = match.end()
    if pos == 0:
        # 🔧 无完整事件：原对象直接返回，不做切片拷贝。调用方用 bytearray
        # 原地追加时，这里的 buffer[0:] 切片会把整个缓冲复制一遍，
        # 大事件（base64 图片）下每个 TCP 分包都全量复制，退化为 O(n²)
        return events, buffer
    return events, buffer[pos:]


def _normalize_error_for_passthrough(error_json: dict, status_code: int = 0) -> dict:
    """将上游返回的错误 JSON 归一化为 OpenAI 兼容格式，并保留原始 HTTP 状态码。

    处理以下情况：
    - {"error": {"message": "...", ...}}  → 保持原样，注入 _http_status
    - {"error": "some string"}            → 包装为 {"error": {"message": "...", ...}}
    - {"message": "...", "code": ...}    → 包装
    """
    if not isinstance(error_json, dict):
        return {
            "error": {
                "message": str(error_json),
                "type": "api_error",
                "code": status_code or 500
            },
            "_http_status": status_code if status_code else 500
        }

    if 'error' in error_json and error_json.get('error') is not None:
        error_val = error_json['error']
        if isinstance(error_val, dict):
            result = dict(error_json)
            # 保留上游原始 HTTP 状态码，供后续重试/冷却判断使用
            if status_code and status_code >= 400:
                result['_http_status'] = status_code
            return result
        # error 是字符串 → 包装
        return {
            "error": {
                "message": str(error_val),
                "type": "api_error",
                "code": status_code or 500
            },
            "_http_status": status_code if status_code else 500
        }

    # 其他格式（如 {"message": "..."}）
    msg = error_json.get('message') or error_json.get('msg') or str(error_json)
    code = error_json.get('code', status_code or 500)
    return {
        "error": {
            "message": msg,
            "type": "api_error",
            "code": code
        },
        "_http_status": status_code if status_code else 500
    }


async def _iter_sse_json_events(
    response: "aiohttp.ClientResponse",
    tag: str,
    parse_bare_json: bool = False,
) -> AsyncGenerator[Dict[str, Any], None]:
    """把 aiohttp 响应体解析为 SSE JSON 事件流（公共实现）。

    🔧 重构说明：旧版在 call_api / call_gemini_native_api 里各自手写一份
    几乎相同的解析循环，且使用 `async for line in response.content`（readline），
    遇到超长行（图像模型的 base64 数据行可达数 MB）会抛
    "Chunk too big" 导致流中断。现在：
    - 用 iter_any + 增量 UTF-8 解码器 + 手工行缓冲，不受行长度限制，
      也不会在多字节字符中间截断
    - `data: [DONE]` → yield {"done": True} 并结束
    - 带 data 前缀的 JSON 行 → yield 解析后的 dict
    - parse_bare_json=True 时，无 data: 前缀的行也尝试按 JSON 解析（Gemini 兼容）
    """
    import codecs
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    buffer = ""

    def _parse_line(line: str) -> Optional[Dict[str, Any]]:
        """解析单行，返回事件 dict；[DONE] 返回 {"done": True}；无效行返回 None"""
        if line.startswith('data:'):
            data = line[5:].lstrip()
            if data == '[DONE]':
                logger.debug(f"[{tag}] 流式响应结束")
                return {"done": True}
            try:
                return json.loads(data)
            except json.JSONDecodeError as e:
                logger.warning(f"[{tag}] JSON解析失败: {e}, 数据: {data[:100]}")
                return None
        if parse_bare_json:
            # 纯JSON格式（无前缀，Gemini 部分网关会这样返回）
            try:
                return json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(f"[{tag}] JSON解析失败: {e}, 数据: {line[:100]}")
        return None

    async for raw_chunk in response.content.iter_any():
        buffer += decoder.decode(raw_chunk)
        if '\n' not in buffer:
            continue
        # 🔧 性能：一次 split 处理本批所有完整行（旧版逐行 find+切片，
        # 单批行数多时 O(n²)）；最后一段是未完成行，保留到下一批
        *complete_lines, buffer = buffer.split('\n')
        for raw_line in complete_lines:
            line = raw_line.strip()
            if not line:
                continue
            event = _parse_line(line)
            if event is not None:
                yield event
                if event.get("done"):
                    return

    # flush 残留（没有尾随换行符的最后一行）
    buffer += decoder.decode(b"", final=True)
    line = buffer.strip()
    if line:
        event = _parse_line(line)
        if event is not None:
            yield event



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
        主要是移除 Gemini 不支持的字段，以及补齐 Gemini 严格要求但 OAI schema 可能省略的字段。
        """
        result = {}
        for key, value in schema.items():
            if key == "additionalProperties":
                continue  # Gemini 不支持
            elif key == "properties" and isinstance(value, dict):
                result[key] = {k: DirectAPIService._convert_schema_for_gemini(v) for k, v in value.items()}
            elif key == "items":
                # Gemini 严格要求 array 类型必须有 items 字段。
                # OAI schema 中 items 常见形式：
                #   - dict:  {"type": "string", ...}  → 递归转换
                #   - str:   "string"                    → 包装为 {"type": "string"}
                #   - list:  [{...}, ...]               → 取首元素（tuple validation 降级）
                if isinstance(value, dict):
                    result[key] = DirectAPIService._convert_schema_for_gemini(value)
                elif isinstance(value, str):
                    result[key] = {"type": value}
                elif isinstance(value, list):
                    # JSON Schema 支持 tuple validation（items 是数组），但 Gemini 只接受对象形式。
                    # 取第一个元素的 schema 作为降级处理（大多数 case 全元素同构）。
                    result[key] = DirectAPIService._convert_schema_for_gemini(value[0]) if value else {"type": "string"}
                else:
                    result[key] = value
            else:
                result[key] = value
        # 安全兜底：如果 type 是 array 但没有 items，补齐默认 items
        if result.get("type") == "array" and "items" not in result:
            result["items"] = {"type": "string"}
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
                    sock_connect=CONFIG.get("download_timeout", {}).get("connect", 60)
                )
            ) as response:
                # 检查响应状态
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"[DIRECT_API] API调用失败: {response.status} - {error_text}")
                    try:
                        # 尝试将错误解析为JSON，并归一化 error 格式
                        error_json = json.loads(error_text)
                        yield _normalize_error_for_passthrough(
                            error_json, response.status)
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
                    # 流式响应（公共 SSE 解析器，支持超长行与跨 chunk UTF-8）
                    async for chunk in _iter_sse_json_events(response, "DIRECT_API"):
                        yield chunk
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
        extra_body: Optional[Dict[str, Any]] = None,
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
            extra_body: 额外的 Gemini 原生请求体键值对（直接合并到 request_body）。
                使用显式参数而非 **kwargs，消除配置键与命名形参碰撞导致 TypeError 的风险。

        Yields:
            响应数据块（Gemini原生格式）
        """
        # 构建Gemini API URL
        method = "streamGenerateContent" if stream else "generateContent"
        
        # 修复：流式请求必须添加 alt=sse 参数，让 Gemini 返回标准 SSE 格式
        sse_param = "&alt=sse" if stream else ""

        # 🔧 key 拼接进查询串前做 URL 编码，防特殊字符破坏 URL
        key_param = quote(api_key or "", safe="")

        def _redact(text: str) -> str:
            """脱敏：key 以明文与 URL 编码两种形态出现在 URL/异常消息中"""
            if api_key:
                text = text.replace(api_key, '***')
                if key_param != api_key:
                    text = text.replace(key_param, '***')
            return text

        if base_url:
            # 使用自定义地址（如本地反代）
            endpoint = f"{base_url.rstrip('/')}/v1beta/models/{model}:{method}?key={key_param}{sse_param}"
        else:
            # 使用Google官方地址
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:{method}?key={key_param}{sse_param}"
        
        # 转换OpenAI格式消息为Gemini格式
        gemini_contents = []
        system_instruction_parts = []
        # tool_call id → 函数名映射（assistant 消息先于对应 tool 结果出现，
        # 单遍遍历即可建立；旧版从 id 字符串猜函数名，call_xxx 会猜出错误名字）
        tool_id_to_name: Dict[str, str] = {}
        
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            
            # 处理 tool 角色消息（工具调用结果）
            if role == "tool":
                tool_call_id = msg.get("tool_call_id", "")
                
                # 优先用 assistant tool_calls 建立的映射；未命中时回退到旧版推断
                func_name = tool_id_to_name.get(tool_call_id) or (
                    tool_call_id.split("_", 1)[-1] if "_" in tool_call_id else "_unknown")
                
                # content 可能是 str / dict / list（OpenAI 允许 content parts 数组）
                if isinstance(content, dict):
                    response_payload = content
                elif isinstance(content, list):
                    # 旧版直接 str(content) 会产生 Python repr 污染工具结果，
                    # 改为拼接全部文本块
                    text_parts = []
                    for item in content:
                        if isinstance(item, dict):
                            if item.get("type") == "text" and item.get("text"):
                                text_parts.append(item["text"])
                        elif isinstance(item, str):
                            text_parts.append(item)
                    response_payload = {"content": "".join(text_parts)}
                else:
                    response_payload = {"content": content if isinstance(content, str) else str(content)}
                
                function_response_part = {
                    "functionResponse": {
                        "name": func_name,
                        "response": response_payload
                    }
                }
                # Gemini 3 系列要求携带 id 关联调用；为空时不携带，兼容旧版 API
                if tool_call_id:
                    function_response_part["functionResponse"]["id"] = tool_call_id
                
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
                        elif isinstance(item, str):
                            text_content += item
                        else:
                            # Gemini systemInstruction 仅支持文本，非文本块无法表达
                            logger.debug("[GEMINI_NATIVE] systemInstruction 仅支持文本，已忽略非文本块")
                
                if text_content:
                    system_instruction_parts.append({"text": text_content})
            elif role == "user":
                # 处理用户消息，支持多模态内容
                parts = []
                
                # 检查content是否为列表（多模态格式）
                if isinstance(content, list):
                    for item in content:
                        # content parts 允许裸字符串项
                        if isinstance(item, str):
                            if item:
                                parts.append({"text": item})
                            continue
                        if not isinstance(item, dict):
                            continue
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
                                # 🔧 按 URL 扩展名推断 MIME（旧版硬编码 image/jpeg，
                                # PNG/WebP 图片会 mime 不匹配被上游拒收/误解析）
                                guessed_mime, _ = mimetypes.guess_type(url.split("?", 1)[0])
                                image_mime = guessed_mime if (guessed_mime or "").startswith("image/") else "image/jpeg"
                                parts.append({
                                    "fileData": {
                                        "mimeType": image_mime,
                                        "fileUri": url
                                    }
                                })
                                logger.debug(f"[GEMINI_NATIVE] 添加URL图片: {url[:50]}... ({image_mime})")
                        
                        else:
                            # 未知类型块：能提取文本则保留，否则记录警告后丢弃
                            # （旧版静默丢弃，排查无从下手）
                            fallback_text = item.get("text")
                            if isinstance(fallback_text, str) and fallback_text:
                                parts.append({"text": fallback_text})
                            else:
                                logger.warning(f"[GEMINI_NATIVE] 跳过不支持的内容块类型: {item_type}")
                
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
                elif isinstance(content, list):
                    # 多模态列表：提取文本部分（旧版直接丢弃）
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                            parts.append({"text": item["text"]})
                
                # 🔧 tool_calls 历史 → functionCall parts
                # （旧版完全不转换，多轮工具调用时 Gemini 看不到自己
                # 之前调过什么工具，上下文丢失）
                tool_calls = msg.get("tool_calls")
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        if not isinstance(tc, dict):
                            continue
                        func = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
                        fc_name = func.get("name", "")
                        args_raw = func.get("arguments", "{}")
                        try:
                            fc_args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                        except json.JSONDecodeError:
                            fc_args = {}
                        function_call: Dict[str, Any] = {
                            "name": fc_name,
                            "args": fc_args if isinstance(fc_args, dict) else {}
                        }
                        tc_id = tc.get("id")
                        if tc_id:
                            function_call["id"] = tc_id
                            if fc_name:
                                tool_id_to_name[tc_id] = fc_name
                        parts.append({"functionCall": function_call})
                
                if not parts:
                    parts.append({"text": " "})
                
                gemini_contents.append({
                    "role": "model",  # Gemini使用"model"而不是"assistant"
                    "parts": parts
                })
        
        # 构建请求体
        request_body: Dict[str, Any] = {
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
        
        # 合并额外的 Gemini 原生请求体字段（如 custom_params/extra_body_params）。
        # 🔧 安全性：以显式 extra_body 参数传入，避免配置键与命名形参碰撞导致 TypeError 500
        if extra_body:
            request_body.update(extra_body)
            logger.info(f"[GEMINI_NATIVE] 已添加额外请求体字段: {list(extra_body.keys())}")
        
        logger.info(f"[GEMINI_NATIVE] 调用Gemini原生API: {_redact(endpoint)}")
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
                    sock_connect=CONFIG.get("download_timeout", {}).get("connect", 60)
                )
            ) as response:
                # 检查响应状态
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"[GEMINI_NATIVE] API调用失败: {response.status} - {error_text}")
                    try:
                        error_json = json.loads(error_text)
                        yield _normalize_error_for_passthrough(
                            error_json, response.status)
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
                    # 流式响应（公共 SSE 解析器；Gemini 兼容无前缀的裸 JSON 行）
                    async for chunk in _iter_sse_json_events(response, "GEMINI_NATIVE", parse_bare_json=True):
                        yield chunk
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
            # 🔧 脱敏：Gemini 的 key 拼在 URL 查询串里，部分 aiohttp 异常
            # （InvalidURL 等）的 str() 会携带完整 URL，不脱敏就直接把 key
            # 泄漏进日志和客户端错误响应
            safe_err = _redact(str(e))
            logger.error(f"[GEMINI_NATIVE] 网络请求失败: {safe_err}")
            yield {
                "error": {
                    "message": f"Network error: {safe_err}",
                    "type": "network_error"
                }
            }
        except Exception as e:
            safe_err = _redact(str(e))
            logger.error(f"[GEMINI_NATIVE] 未知错误: {safe_err}", exc_info=True)
            yield {
                "error": {
                    "message": f"Unexpected error: {safe_err}",
                    "type": "internal_error"
                }
            }

    async def call_gemini_interactions_api(
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
        response_format: Optional[Any] = None,
        stop_sequences: Optional[List[str]] = None,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """调用 Gemini Interactions API（新协议 /v1beta/interactions）。

        与 call_gemini_native_api 平行：OpenAI 消息在内部转换为 interactions
        无状态步骤数组（store=false，签名前缀匹配注入由转换器完成），
        流式请求带 alt=sse，事件逐个 yield（含 event_type 字段的 data JSON）。

        Args:
            api_key: Google API密钥
            model: 上游模型ID
            messages: OpenAI格式的消息列表（需要转换）
            stream: 是否流式响应
            temperature: 温度参数
            top_p: top_p参数（interactions 未确认支持，转换器忽略）
            max_tokens: 最大token数
            base_url: 自定义API地址（主机根，如 https://generativelanguage.googleapis.com
                或 http://127.0.0.1:7861；缺省使用Google官方地址）
            thinking_config: 思维链配置（thinkingLevel 映射为 thinking_level）
            tools: OpenAI 格式的 tools 列表（转换为 Interactions 扁平工具定义）
            tool_choice: OAI tool_choice（仅 "none" 受支持，不传 tools）
            response_format: OpenAI 结构化输出配置
            stop_sequences: OpenAI stop 列表，映射为 generation_config.stop_sequences
            extra_body: 额外的请求体键值对（直接合并到 request_body）

        Yields:
            流式：interactions SSE 事件 dict；非流式：interaction 对象 dict；
            出错时：_normalize_error_for_passthrough 格式的错误 dict
        """
        # Interactions 官方使用 x-goog-api-key 请求头。配置可以填写主机根地址，
        # 也可以填写已经带 /v1beta 的地址，统一避免重复版本路径。
        sse_param = "?alt=sse" if stream else ""

        def _redact(text: str) -> str:
            """脱敏异常消息中的 API key。"""
            return text.replace(api_key, "***") if api_key else text

        configured_base = (base_url or "https://generativelanguage.googleapis.com").rstrip("/")
        if configured_base.endswith("/v1beta"):
            endpoint = f"{configured_base}/interactions{sse_param}"
        else:
            endpoint = f"{configured_base}/v1beta/interactions{sse_param}"

        request_body = build_interactions_request_body(
            model=model,
            messages=messages,
            stream=stream,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            thinking_config=thinking_config,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            stop_sequences=stop_sequences,
            extra_body=extra_body,
        )

        logger.info(f"[GEMINI_INTERACTIONS] 调用Interactions API: {_redact(endpoint)}")
        logger.info(f"[GEMINI_INTERACTIONS] 模型: {model}, 流式: {stream}, 步骤数: {len(request_body.get('input', []))}")
        if request_body.get("system_instruction"):
            logger.info(f"[GEMINI_INTERACTIONS] system_instruction: {len(request_body['system_instruction'])} 字符")
        if request_body.get("generation_config"):
            logger.info(f"[GEMINI_INTERACTIONS] generation_config: {request_body['generation_config']}")

        try:
            request_body_json = await asyncio.to_thread(
                json.dumps,
                request_body,
                ensure_ascii=False,
                separators=(',', ':')
            )
            headers = {
                "Content-Type": "application/json"
            }
            if api_key:
                headers["x-goog-api-key"] = api_key
            async with self.session.post(
                endpoint,
                data=request_body_json.encode('utf-8'),
                headers=headers,
                timeout=aiohttp.ClientTimeout(
                    total=CONFIG.get("api_call_timeout_seconds", 3000),
                    sock_read=CONFIG.get("stream_response_timeout_seconds", 3000),
                    sock_connect=CONFIG.get("download_timeout", {}).get("connect", 60)
                )
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"[GEMINI_INTERACTIONS] API调用失败: {response.status} - {error_text}")
                    try:
                        error_json = json.loads(error_text)
                        yield _normalize_error_for_passthrough(
                            error_json, response.status)
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
                    # 流式：复用公共 SSE 解析器。interactions 官方是 event:+data: 双行，
                    # event: 行会被跳过；parse_bare_json=True 兼容反代发裸 JSON 行。
                    async for event in _iter_sse_json_events(
                        response, "GEMINI_INTERACTIONS", parse_bare_json=True):
                        yield event
                else:
                    response_data = await response.json()
                    yield response_data

        except asyncio.TimeoutError:
            logger.error(f"[GEMINI_INTERACTIONS] 请求超时 (模型: {model})")
            yield {
                "error": {
                    "message": "Request timed out while waiting for Gemini Interactions API response",
                    "type": "timeout_error"
                }
            }
        except aiohttp.ClientError as e:
            safe_err = _redact(str(e))
            logger.error(f"[GEMINI_INTERACTIONS] 网络请求失败: {safe_err}")
            yield {
                "error": {
                    "message": f"Network error: {safe_err}",
                    "type": "network_error"
                }
            }
        except Exception as e:
            safe_err = _redact(str(e))
            logger.error(f"[GEMINI_INTERACTIONS] 未知错误: {safe_err}", exc_info=True)
            yield {
                "error": {
                    "message": f"Unexpected error: {safe_err}",
                    "type": "internal_error"
                }
            }

    def convert_gemini_response_to_openai(
        self,
        gemini_response: Dict[str, Any],
        model: str,
        request_id: str,
        is_stream_chunk: bool = False,
        completion_tokens_mode: str = MODE_MERGE
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
                    for part in parts:
                        # 处理函数调用
                        if "functionCall" in part:
                            fc = part["functionCall"]
                            if tool_calls is None:
                                tool_calls = []
                            # 🔧 修复：index 必须是 tool_calls 内部的连续序号。
                            # 旧版用 parts 的下标，parts 里混有 text 块时会得到
                            # 不连续的 index（如 [text, functionCall] → index=1），
                            # 客户端按 index 聚合流式工具调用时直接错位。
                            tool_calls.append(
                                DirectAPIService._gemini_function_call_to_oai_tool_call(
                                    fc, index=len(tool_calls))
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

                # 🔧 修复：旧版流式 chunk 在此提前 return "usage": None，把上游
                # 给的精确 token 计数整个丢弃，下游只能退化为本地 tokenizer
                # 估算。现在流式 chunk 与非流式共用下方的 usage 计算逻辑
                # （Gemini 流式分块中的 usageMetadata 是累积快照，
                # 下游以最后一个值为准）。
                if thoughts_tokens == 0 and reasoning_content and not is_stream_chunk:
                    # 仅非流式做估算兜底：流式 chunk 的 reasoning_content
                    # 是增量，按增量估算会严重低估
                    thoughts_tokens = len(reasoning_content) // 4
                
                prompt_tokens = metadata.get("promptTokenCount", 0)
                candidates_tokens = metadata.get("candidatesTokenCount", 0)

                # Gemini 的 candidatesTokenCount 不含思考，相加才是真实总输出；
                # 下发给下游的 completion_tokens 按模型配置决定给总量还是仅正文
                usage = compose_chat_usage(
                    prompt_tokens=prompt_tokens,
                    output_tokens=candidates_tokens + thoughts_tokens,
                    reasoning_tokens=thoughts_tokens,
                    completion_mode=completion_tokens_mode,
                    total_tokens=metadata.get("totalTokenCount", 0),
                )
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
            message: Dict[str, Any] = {"role": "assistant"}
            
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
    
    @staticmethod
    def _format_error_bytes(error_response: dict, is_stream: bool) -> bytes:
        """将错误响应格式化为合适的字节输出。
        流式模式：包装为 SSE 格式（data: + [DONE]），确保流式处理器能正确转发。
        非流式模式：纯 JSON，调用方直接拼接后解析。
        """
        error_json_str = json.dumps(error_response, ensure_ascii=False)
        if is_stream:
            return f"data: {error_json_str}\n\ndata: [DONE]\n\n".encode('utf-8')
        else:
            return error_json_str.encode('utf-8')

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

        # ── 诊断：检查图片 media_type ──
        for msg_idx, msg in enumerate(request_body.get("messages", [])):
            content = msg.get("content")
            if isinstance(content, list):
                for blk_idx, block in enumerate(content):
                    if isinstance(block, dict) and block.get("type") == "image":
                        src = block.get("source", {})
                        mt = src.get("media_type", "N/A")
                        dl = len(src.get("data", ""))
                        logger.info(
                            f"[DIRECT_API_PASSTHROUGH] 消息[{msg_idx}] 图片块[{blk_idx}]: "
                            f"source.type={src.get('type')!r}, media_type={mt!r}, data_len={dl}")
                        if ";" in str(mt):
                            logger.error(
                                f"[DIRECT_API_PASSTHROUGH] ❌ 检测到异常的 media_type: {mt!r} "
                                f"(包含分号，将导致上游400错误)")

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
                    sock_connect=CONFIG.get("download_timeout", {}).get("connect", 60)
                )
            ) as response:
                # 检查响应状态
                if response.status != 200:
                    error_body = await response.read()
                    error_text = error_body.decode(errors='ignore')
                    logger.error(f"[DIRECT_API_PASSTHROUGH] API调用失败: {response.status} - {error_text}")
                    
                    try:
                        # 检查原始错误是否为有效JSON，并归一化 error 格式
                        error_json = json.loads(error_text)
                        normalized = _normalize_error_for_passthrough(
                            error_json, response.status)
                        yield self._format_error_bytes(normalized, is_stream)
                    except json.JSONDecodeError:
                        # 如果不是JSON，则封装成OpenAI兼容的错误格式
                        error_response = {
                            "error": {
                                "message": error_text,
                                "type": "api_error",
                                "code": response.status
                            }
                        }
                        yield self._format_error_bytes(error_response, is_stream)
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
                    yield self._format_error_bytes(error_response, is_stream)
                    return
                if is_stream:
                    # 按 SSE 事件边界（空行）缓冲转发：一个事件必须完整送达。
                    # 旧实现按"最后一个换行符"切分并给每个片段强补 \n\n 终止符，
                    # 会把被 TCP 分包切断的事件拆成两个残缺事件。携带思维链
                    # 签名的 signature_delta 一旦被切断，客户端便拿不到完整签名，
                    # 下一轮回传历史时会被上游 400 拒绝（signature: undefined）。
                    # 🔧 bytearray 缓冲：bytes 的 += 每次全量复制（CPython 仅对
                    # str 有原地优化），bytearray 的 += 是真正的原地追加
                    buffer = bytearray()
                    scan_start = 0
                    async for chunk, _ in response.content.iter_chunks():
                        if not chunk:
                            continue
                        buffer += chunk
                        events, buffer = _extract_complete_sse_events(buffer, scan_start)
                        # 剩余缓冲已确认无完整分隔符；下次从末尾回退 3 字节
                        # 开始扫（分隔符最长 \r\n\r\n = 4 字节，防跨包切断）
                        scan_start = max(0, len(buffer) - 3)
                        for event_bytes in events:
                            yield event_bytes

                    # 流结束后输出残留数据（上游未按规范以空行收尾的最后一段）
                    if buffer.strip():
                        yield bytes(buffer.rstrip(b"\r\n")) + b"\n\n"
                else:
                    # 🔧 非流式 JSON：原样透传字节，不做 SSE 事件切分。
                    # 旧版非流式也走事件切分：大 JSON 在 buffer 中 O(n²) 拼接，
                    # 还会被追加 \n\n、CRLF 被改写 —— 正是下游
                    # json.loads(text[:e.pos]) 截断 hack 存在的根源。
                    async for chunk, _ in response.content.iter_chunks():
                        if chunk:
                            yield chunk

        except asyncio.TimeoutError:
            logger.error(f"[DIRECT_API_PASSTHROUGH] 请求超时 (模型: {request_body.get('model')})")
            error_response = {
                "error": {
                    "message": "Request timed out while waiting for API response",
                    "type": "timeout_error"
                }
            }
            yield self._format_error_bytes(error_response, is_stream)
        except aiohttp.ClientError as e:
            logger.error(f"[DIRECT_API_PASSTHROUGH] 网络请求失败: {e}")
            error_response = {
                "error": {
                    "message": f"Network error: {str(e)}",
                    "type": "network_error"
                }
            }
            yield self._format_error_bytes(error_response, is_stream)
        except Exception as e:
            logger.error(f"[DIRECT_API_PASSTHROUGH] 未知错误: {e}", exc_info=True)
            error_response = {
                "error": {
                    "message": f"Unexpected error: {str(e)}",
                    "type": "internal_error"
                }
            }
            yield self._format_error_bytes(error_response, is_stream)
    
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
    elif aiohttp_session is not None and direct_api_service.session is not aiohttp_session:
        # 注意：同步函数无法 await 关闭旧的自建 session，直接切换为共享连接池；
        # 正常启动流程会先调 init_direct_api_service，不会走到这个分支
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