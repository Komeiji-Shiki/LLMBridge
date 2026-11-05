"""内部 Chat 消息 → Gemini generateContent 的 contents 和 systemInstruction。"""
import base64
import json
import logging
import mimetypes
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from converters.audio import extract_audio, separate_tool_audio
from converters.media import FILE_PART_TYPES, file_to_gemini
from core.conversion_report import note_loss

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
