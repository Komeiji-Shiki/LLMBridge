"""Chat ↔ Gemini generateContent：请求的 contents、systemInstruction、工具和生成配置，以及回答转回 Chat。"""
import base64
import copy
import json
import logging
import mimetypes
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from converters.audio import extract_audio, separate_tool_audio
from converters.media import FILE_PART_TYPES, file_to_gemini, gemini_code_text
from core.conversion_report import note_loss
from services.protocol_events import gemini_finish_to_openai
from services.provider_capabilities import GEMINI_TOOL_FIELDS
from utils.usage_tokens import MODE_MERGE, compose_chat_usage, rough_token_count

logger = logging.getLogger(__name__)

# Anthropic 的 is_error 转成 Chat 时写在文本开头（见 converters/anthropic_openai.py）。
TOOL_ERROR_PREFIX = "[tool_result_error] "


class GeminiRequestError(ValueError):
    """消息无法转换成合法的 generateContent 请求。"""


def _media_part(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """音频、文件、视频转成 Gemini 的数据块；不是这几类返回 None。"""
    item_type = item.get("type")
    if item_type in ("audio", "input_audio", "audio_url"):
        audio = extract_audio(item)
        if audio and "data" in audio:
            return {"inlineData": {"mimeType": audio["mime_type"], "data": audio["data"]}}
        if audio:
            return {"fileData": {"mimeType": audio["mime_type"], "fileUri": audio["uri"]}}
        return None
    if item_type in FILE_PART_TYPES:
        audio = extract_audio(item)
        if audio and "data" in audio:
            return {"inlineData": {"mimeType": audio["mime_type"], "data": audio["data"]}}
        if audio:
            return {"fileData": {"mimeType": audio["mime_type"], "fileUri": audio["uri"]}}
        part = file_to_gemini(item)
        if part is None:
            note_loss(f"content.{item_type}", "文件引用只在原服务商有效，Gemini 无法读取，已跳过")
        return part
    if item_type == "video_url":
        url = (item.get("video_url") or {}).get("url", "")
        if url.startswith(("http://", "https://")):
            mime_type = mimetypes.guess_type(url.split("?", 1)[0])[0] or "video/mp4"
            return {"fileData": {"mimeType": mime_type, "fileUri": url}}
    return None


async def _tool_message_part(msg: Dict[str, Any], func_name: str, session, media_in_result: bool) -> Dict[str, Any]:
    tool_call_id = msg.get("tool_call_id", "")
    content = msg.get("content", "")
    media_parts: List[Dict[str, Any]] = []

    # content 可能是 str / dict / list（OpenAI 允许 content parts 数组）
    if isinstance(content, dict):
        response_payload = content
    elif isinstance(content, list):
        # 文本写入 response，媒体按 Gemini 协议嵌套在同一次调用的 parts 中。
        text_parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") in ("text", "input_text", "output_text") and item.get("text"):
                    text_parts.append(item["text"])
                elif item.get("type") in ("image_url", "input_image"):
                    image = item.get("image_url") or item.get("url") or ""
                    url = image.get("url", "") if isinstance(image, dict) else image
                    if url.startswith("data:"):
                        header, image_data = url.split(",", 1)
                        mime_type = header[5:].split(";", 1)[0]
                    else:
                        # FunctionResponsePart 仅接受 inlineData，远程图片需先下载。
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as image_response:
                            image_response.raise_for_status()
                            image_data = base64.b64encode(await image_response.read()).decode("ascii")
                            mime_type = image_response.headers.get("Content-Type", "").split(";", 1)[0]
                        if not mime_type.startswith("image/"):
                            mime_type = mimetypes.guess_type(url.split("?", 1)[0])[0] or "image/jpeg"
                    media_parts.append({"inlineData": {"mimeType": mime_type, "data": image_data}})
                elif media_in_result:
                    part = _media_part(item)
                    if part:
                        media_parts.append(part)
                elif item.get("type") == "video_url":
                    note_loss("tool_result.video_url", "旧模型的函数结果不接受视频，已跳过")
            elif isinstance(item, str):
                text_parts.append(item)
        response_payload = {"content": "".join(text_parts)}
    else:
        response_payload = {"content": content if isinstance(content, str) else str(content)}

    text = response_payload.get("content") if isinstance(response_payload.get("content"), str) else ""
    if text.startswith(TOOL_ERROR_PREFIX):
        response_payload = {"error": text[len(TOOL_ERROR_PREFIX):]}

    function_response: Dict[str, Any] = {"name": func_name, "response": response_payload}
    # Gemini 3 系列要求携带 id 关联调用；为空时不携带，兼容旧版 API
    if tool_call_id:
        function_response["id"] = tool_call_id
    if media_parts:
        function_response["parts"] = media_parts
    return {"functionResponse": function_response}


def _user_parts(content: Any) -> List[Dict[str, Any]]:
    parts: List[Dict[str, Any]] = []
    if isinstance(content, str):
        if content:
            parts.append({"text": content})
    elif isinstance(content, list):
        for item in content:
            # content parts 允许裸字符串项
            if isinstance(item, str):
                if item:
                    parts.append({"text": item})
                continue
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")

            if item_type in ("text", "input_text", "output_text"):
                text = item.get("text", "")
                if text:
                    parts.append({"text": text})

            elif item_type == "image_url":
                url = (item.get("image_url") or {}).get("url", "")
                if url.startswith("data:"):
                    try:
                        header, base64_data = url.split(",", 1)
                        mime_type = header.split(";")[0].split(":")[1]
                        parts.append({"inline_data": {"mime_type": mime_type, "data": base64_data}})
                    except Exception as e:
                        logger.warning(f"[GEMINI_NATIVE] 解析base64图片失败: {e}")
                elif url.startswith(("http://", "https://")):
                    # 🔧 按 URL 扩展名推断 MIME（旧版硬编码 image/jpeg，
                    # PNG/WebP 图片会 mime 不匹配被上游拒收/误解析）
                    guessed_mime, _ = mimetypes.guess_type(url.split("?", 1)[0])
                    image_mime = guessed_mime if (guessed_mime or "").startswith("image/") else "image/jpeg"
                    parts.append({"fileData": {"mimeType": image_mime, "fileUri": url}})

            elif item_type in ("audio", "input_audio", "audio_url", "video_url", *FILE_PART_TYPES):
                part = _media_part(item)
                if part:
                    parts.append(part)

            else:
                # 未知类型块：能提取文本则保留，否则记录后丢弃
                fallback_text = item.get("text")
                if isinstance(fallback_text, str) and fallback_text:
                    parts.append({"text": fallback_text})
                else:
                    note_loss(f"content.{item_type}", f"Gemini 无法读取内容块 {item_type}，已跳过")
    # 如果没有任何内容，添加空文本（避免空parts）
    return parts or [{"text": " "}]


def _model_parts(msg: Dict[str, Any], tool_id_to_name: Dict[str, str]) -> List[Dict[str, Any]]:
    content = msg.get("content", "")
    parts: List[Dict[str, Any]] = []
    if isinstance(content, str) and content:
        parts.append({"text": content})
    elif isinstance(content, list):
        # 多模态列表：提取文本部分（旧版直接丢弃）
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                parts.append({"text": item["text"]})

    # 🔧 tool_calls 历史 → functionCall parts
    # （旧版完全不转换，多轮工具调用时 Gemini 看不到自己之前调过什么工具，上下文丢失）
    tool_calls = msg.get("tool_calls")
    for tc in tool_calls if isinstance(tool_calls, list) else []:
        if not isinstance(tc, dict):
            continue
        func = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
        fc_name = func.get("name", "")
        args_raw = func.get("arguments", "{}")
        try:
            fc_args = (json.loads(args_raw) if args_raw.strip() else {}) if isinstance(args_raw, str) else args_raw
        except json.JSONDecodeError:
            fc_args = None
        if not isinstance(fc_args, dict):
            # 客户端回传的历史参数不是 JSON 对象（常见于上一轮被 max_tokens 截断），
            # Gemini 只接受对象，只能按空参数发送
            logger.warning(f"[GEMINI_NATIVE] 历史工具调用 '{fc_name}' 的 arguments 不是 JSON 对象，按空参数发送")
            fc_args = {}
        function_call: Dict[str, Any] = {"name": fc_name, "args": fc_args}
        tc_id = tc.get("id")
        if tc_id:
            function_call["id"] = tc_id
            if fc_name:
                tool_id_to_name[tc_id] = fc_name
        function_part = {"functionCall": function_call}
        signature = tc.get("_thought_signature")
        if signature:
            function_part["thoughtSignature"] = signature
        parts.append(function_part)
    return parts or [{"text": " "}]


def _system_part(content: Any) -> Optional[Dict[str, str]]:
    text_content = ""
    if isinstance(content, str):
        text_content = content
    elif isinstance(content, list):
        # 如果是列表（多模态格式），提取所有文本部分；systemInstruction 仅支持文本
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_content += item.get("text", "")
            elif isinstance(item, str):
                text_content += item
    return {"text": text_content} if text_content else None


async def build_gemini_contents(messages: list, session, media_in_result: bool = False) -> Tuple[list, list]:
    """返回 (contents, systemInstruction 的 parts)。

    media_in_result：模型的函数结果能直接带文件和音视频（Gemini 3 起）；否则移到紧随其后的 user 轮。
    """
    contents: List[Dict[str, Any]] = []
    system_parts: List[Dict[str, str]] = []
    # tool_call id → 函数名映射（assistant 消息先于对应 tool 结果出现，单遍遍历即可建立）
    tool_id_to_name: Dict[str, str] = {}

    source = messages if media_in_result else separate_tool_audio(messages, include_files=True)
    for msg in source:
        role = msg.get("role")
        content = msg.get("content", "")

        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            # functionResponse 必须带上与 functionCall 一致的函数名，id 里推不出来
            func_name = tool_id_to_name.get(tool_call_id)
            if not func_name:
                raise GeminiRequestError(f"tool message references tool_call_id '{tool_call_id}' "
                                         "which has no matching assistant tool_call")
            part = await _tool_message_part(msg, func_name, session, media_in_result)
            # 并行调用的结果必须同在一轮（数量与上一轮 functionCall 一致）
            previous = contents[-1] if contents else None
            if (previous and previous["role"] == "user"
                    and all("functionResponse" in item for item in previous["parts"])):
                previous["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part]})  # Gemini 的工具结果使用 user 角色
        elif role == "system":
            part = _system_part(content)
            if part:
                system_parts.append(part)
        elif role == "user":
            contents.append({"role": "user", "parts": _user_parts(content)})
        elif role == "assistant":
            metadata = msg.get("provider_metadata")
            native_parts = metadata.get("gemini_parts") if isinstance(metadata, dict) else None
            if isinstance(native_parts, list):
                # deepcopy 由调用方的历史恢复负责；这里只读取函数名映射
                contents.append({"role": "model", "parts": json.loads(json.dumps(native_parts))})
                for native in native_parts:
                    call = native.get("functionCall") if isinstance(native, dict) else None
                    if isinstance(call, dict) and call.get("id"):
                        tool_id_to_name[call["id"]] = call.get("name", "")
                continue
            contents.append({"role": "model", "parts": _model_parts(msg, tool_id_to_name)})
    return contents, system_parts


def oai_tools_to_gemini(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    将 OAI 格式的 tools 转换为 Gemini 格式的 tools。

    OAI: [{"type": "function", "function": {"name": "get_weather", "description": "...", "parameters": {...}}}]
    Gemini: [{"functionDeclarations": [{"name": "get_weather", "description": "...", "parametersJsonSchema": {...}}]}]
    """
    gemini_tools = []
    function_declarations = []

    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function":
            native_key = GEMINI_TOOL_FIELDS.get(tool.get('type'))
            if native_key:
                gemini_tools.append({native_key: {key: copy.deepcopy(value) for key, value in tool.items() if key != 'type'}})
            elif any(key in tool for key in GEMINI_TOOL_FIELDS.values()):
                gemini_tools.append(copy.deepcopy(tool))
            continue
        func_def = tool.get("function", {})
        if not isinstance(func_def, dict):
            continue

        declaration = {
            "name": func_def.get("name", ""),
            "description": func_def.get("description", ""),
        }
        # parametersJsonSchema 按 JSON Schema 原样发送，不裁成 parameters 的 OpenAPI 子集
        params = func_def.get("parameters")
        if isinstance(params, dict):
            declaration["parametersJsonSchema"] = copy.deepcopy(params)

        function_declarations.append(declaration)

    if function_declarations:
        gemini_tools.append({"functionDeclarations": function_declarations})

    return gemini_tools


def oai_tool_choice_to_gemini(tool_choice: Any) -> Optional[Dict[str, Any]]:
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
        elif choice_type == "allowed_tools":
            allowed = tool_choice.get("allowed_tools") or {}
            names = [(item.get("function") or {}).get("name") or item.get("name")
                     for item in allowed.get("tools") or [] if isinstance(item, dict)]
            config = {"mode": "ANY" if allowed.get("mode") == "required" else "AUTO"}
            if config["mode"] == "ANY" and names:
                config["allowed_function_names"] = names
            return {"functionCallingConfig": config}

    return {"functionCallingConfig": {"mode": "AUTO"}}


async def build_generate_content_body(
    messages: list,
    session,
    media_in_result: bool = False,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    max_tokens: Optional[int] = None,
    thinking_config: Optional[Dict[str, Any]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[Any] = None,
    response_format: Optional[Dict[str, Any]] = None,
    stop_sequences: Optional[List[str]] = None,
    generation_extras: Optional[Dict[str, Any]] = None,
    store: Optional[bool] = None,
    extra_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构建 generateContent 请求体，消息无法转换时抛 GeminiRequestError。

    store 为 None 时不发送；extra_body（custom_params/extra_body_params）最后合并，同名键以它为准。
    """
    contents, system_parts = await build_gemini_contents(messages, session, media_in_result)
    request_body: Dict[str, Any] = {"contents": contents}
    if system_parts:
        request_body["systemInstruction"] = {"role": "system", "parts": system_parts}

    if tools:
        gemini_tools = oai_tools_to_gemini(tools)
        if gemini_tools:
            request_body["tools"] = gemini_tools
            logger.info(f"[GEMINI_NATIVE] 已转换 {len(tools)} 个 OAI tools → Gemini tools")

    if tool_choice:
        tool_config = oai_tool_choice_to_gemini(tool_choice)
        if tool_config:
            request_body["toolConfig"] = tool_config
            logger.info(f"[GEMINI_NATIVE] 已设置 toolConfig: {tool_config}")

    generation_config: Dict[str, Any] = {}
    if temperature is not None:
        generation_config["temperature"] = temperature
    if top_p is not None:
        generation_config["topP"] = top_p
    if max_tokens is not None:
        generation_config["maxOutputTokens"] = max_tokens
    if thinking_config:
        generation_config["thinkingConfig"] = thinking_config
    if stop_sequences:
        generation_config["stopSequences"] = stop_sequences
    if isinstance(response_format, dict):
        if response_format.get("type") in ("json_object", "json_schema"):
            generation_config["responseMimeType"] = "application/json"
        if response_format.get("type") == "json_schema":
            schema = (response_format.get("json_schema") or {}).get("schema")
            if isinstance(schema, dict):
                generation_config["responseJsonSchema"] = schema
    generation_config.update(generation_extras or {})
    if generation_config:
        request_body["generationConfig"] = generation_config

    if store is not None:
        request_body["store"] = store
    if extra_body:
        request_body.update(extra_body)
        logger.info(f"[GEMINI_NATIVE] 已添加额外请求体字段: {list(extra_body.keys())}")
    return request_body


def _function_call_to_tool_call(function_call: Dict[str, Any], index: int = 0) -> Dict[str, Any]:
    """将 Gemini functionCall 转换为 OAI tool_call 格式。"""
    name = function_call.get("name")
    if not name:
        raise ValueError(f"Gemini 返回的 functionCall 没有 name: {json.dumps(function_call, ensure_ascii=False)[:200]}")
    args = function_call.get("args") or {}
    if not isinstance(args, dict):
        raise ValueError(f"Gemini 返回的 functionCall '{name}' 的 args 不是对象: {type(args).__name__}")

    tool_call = {
        "index": index,
        # Gemini 3 返回 id 字段
        "id": function_call.get("id") or f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False)
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


def _provider_metadata(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {'gemini_parts': copy.deepcopy((candidate.get('content') or {}).get('parts') or []),
            'groundingMetadata': copy.deepcopy(candidate.get('groundingMetadata')),
            'urlContextMetadata': copy.deepcopy(candidate.get('urlContextMetadata'))}


def convert_gemini_to_openai_response(
    gemini_response: Dict[str, Any],
    model: str,
    request_id: str,
    is_stream_chunk: bool = False,
    completion_tokens_mode: str = MODE_MERGE,
    tool_call_indices: Optional[Dict[str, int]] = None,
    seen_sources: Optional[set] = None,
) -> Dict[str, Any]:
    """
    将Gemini原生响应转换为OpenAI格式（支持工具调用转换）

    seen_sources：流式请求里已经发过的来源网址，分块的 groundingMetadata 是累积的。
    """
    # 检查错误
    if "error" in gemini_response:
        return gemini_response

    content = ""
    reasoning_content = ""
    finish_reason = None
    usage = {}
    tool_calls = None
    images = []
    annotations = []

    if "candidates" in gemini_response and len(gemini_response["candidates"]) > 0:
        candidate = gemini_response["candidates"][0]

        if "content" in candidate and "parts" in candidate["content"]:
            for part in candidate["content"]["parts"]:
                if "functionCall" in part:
                    if tool_calls is None:
                        tool_calls = []
                    # index 必须是 tool_calls 内部的连续序号：parts 里混有 text 块时
                    # 用 parts 下标会不连续，客户端按 index 聚合流式工具调用时直接错位
                    call = _function_call_to_tool_call(part["functionCall"], index=len(tool_calls))
                    # 签名属于 Part，不能从 Part.functionCall 中读取。
                    if part.get("thoughtSignature"):
                        call["_thought_signature"] = part["thoughtSignature"]
                    if tool_call_indices is not None:
                        call["index"] = tool_call_indices.setdefault(call["id"], len(tool_call_indices))
                    tool_calls.append(call)
                elif "text" in part:
                    text = part.get("text", "")
                    if part.get("thought", False):
                        reasoning_content += text
                    else:
                        content += text
                elif gemini_code_text(part):
                    content += gemini_code_text(part)
                elif isinstance(part.get("inlineData"), dict) and not part.get("thought"):
                    inline = part["inlineData"]
                    if str(inline.get("mimeType", "")).startswith("image/") and inline.get("data"):
                        images.append({"type": "image_url", "image_url": {
                            "url": f"data:{inline['mimeType']};base64,{inline['data']}"}})

        # 联网搜索的来源：Chat 的 annotations 没有正文位置也能表达出处
        seen = seen_sources if seen_sources is not None else set()
        grounding = candidate.get("groundingMetadata")
        for chunk in (grounding.get("groundingChunks") or []) if isinstance(grounding, dict) else []:
            web = chunk.get("web") if isinstance(chunk, dict) else None
            if isinstance(web, dict) and web.get("uri") and web["uri"] not in seen:
                seen.add(web["uri"])
                annotations.append({"type": "url_citation", "url_citation": {
                    "url": web["uri"], "title": web.get("title") or web["uri"]}})

        if "finishReason" in candidate:
            finish_reason = gemini_finish_to_openai(candidate["finishReason"])

    # 流式分块里的 usageMetadata 是累积快照，下游以最后一个值为准
    if "usageMetadata" in gemini_response:
        metadata = gemini_response["usageMetadata"]
        thoughts_tokens = metadata.get("thoughtsTokenCount", 0)
        if thoughts_tokens == 0 and reasoning_content and not is_stream_chunk:
            # 仅非流式做估算兜底：流式 chunk 的 reasoning_content
            # 是增量，按增量估算会严重低估
            thoughts_tokens = rough_token_count(reasoning_content)

        # 联网和代码执行的工具提示按输入计费，但不算在 promptTokenCount 里。
        prompt_tokens = metadata.get("promptTokenCount", 0) + metadata.get("toolUsePromptTokenCount", 0)
        candidates_tokens = metadata.get("candidatesTokenCount", 0)

        # Gemini 的 candidatesTokenCount 不含思考，相加才是真实总输出；
        # 下发给下游的 completion_tokens 按模型配置决定给总量还是仅正文
        usage = compose_chat_usage(
            prompt_tokens=prompt_tokens,
            output_tokens=candidates_tokens + thoughts_tokens,
            reasoning_tokens=thoughts_tokens,
            cached_tokens=metadata.get("cachedContentTokenCount", 0),
            completion_mode=completion_tokens_mode,
            total_tokens=metadata.get("totalTokenCount", 0),
        )

    if finish_reason == "stop" and (tool_calls or tool_call_indices):
        finish_reason = "tool_calls"

    candidate = (gemini_response.get('candidates') or [{}])[0]
    if is_stream_chunk:
        delta: Dict[str, Any] = {'provider_metadata': _provider_metadata(candidate)}
        if reasoning_content:
            delta["reasoning_content"] = reasoning_content
        if content:
            delta["content"] = content
        if tool_calls:
            delta["tool_calls"] = tool_calls
        if images:
            delta["images"] = images
        if annotations:
            delta["annotations"] = annotations
        return {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason
            }],
            "usage": usage if usage else None
        }

    message: Dict[str, Any] = {"role": "assistant", 'provider_metadata': _provider_metadata(candidate)}
    if reasoning_content:
        message["reasoning_content"] = reasoning_content
    if tool_calls:
        message["tool_calls"] = tool_calls
        message["content"] = content or None
    else:
        message["content"] = content
    if images:
        message["images"] = images
    if annotations:
        message["annotations"] = annotations

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
