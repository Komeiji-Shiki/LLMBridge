"""统一解析客户端及工具结果中的音频，供 Gemini 两种上游协议复用。"""
import mimetypes
import re
from typing import Any, Dict, Iterable, Iterator, Optional


_AUDIO_ENVELOPE = re.compile(
    r'<graywill_audio mime_type="(audio/[^"<>\s]+)">([A-Za-z0-9+/=]+)</graywill_audio>'
    r'|<graywill_audio_ref id="([a-f0-9]{32})"\s*/>'
)


def extract_audio(block: Any) -> Optional[Dict[str, str]]:
    """返回音频的 MIME 类型与 base64 数据或 URI，不读取本地路径。"""
    if not isinstance(block, dict):
        return None
    block_type = block.get("type")
    if block_type not in ("audio", "input_audio", "audio_url", "document"):
        return None

    if block_type == "input_audio":
        source = block.get("input_audio")
    elif block_type == "audio_url":
        source = block.get("audio_url")
        if isinstance(source, str):
            source = {"url": source}
    else:
        # MCP 音频直接使用 data/mimeType，附件则可能使用 source。
        source = block.get("source", block)
    if not isinstance(source, dict):
        return None

    if isinstance(source.get("file_id"), str) and re.fullmatch(r"[a-f0-9]{32}", source["file_id"]):
        return {"file_id": source["file_id"]}
    mime_type = (source.get("mime_type") or source.get("media_type")
                 or source.get("mimeType") or block.get("mime_type")
                 or block.get("media_type") or block.get("mimeType"))
    data = source.get("data")
    uri = source.get("url") or source.get("uri")
    if isinstance(uri, str) and uri.startswith("data:"):
        header, separator, encoded = uri.partition(",")
        if not separator or not header.endswith(";base64"):
            return None
        mime_type = header[5:].split(";", 1)[0]
        data, uri = encoded, None
    if not mime_type and block_type == "input_audio":
        # 沿用既有 Interactions 输入约定；显式 MIME 类型优先于 format。
        audio_format = source.get("format") or "wav"
        mime_type = audio_format if audio_format.startswith("audio/") else f"audio/{audio_format}"
    if not mime_type and isinstance(uri, str):
        mime_type = mimetypes.guess_type(uri.split("?", 1)[0])[0]
    if not isinstance(mime_type, str) or not mime_type.startswith("audio/"):
        return None
    if isinstance(data, str) and data:
        return {"mime_type": mime_type, "data": data}
    if isinstance(uri, str) and uri.startswith(("http://", "https://", "gs://")):
        return {"mime_type": mime_type, "uri": uri}
    return None


def audio_to_openai(block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """音频内容块转为网关内部使用的 OpenAI 内容块，保留原始 MIME 类型。"""
    audio = extract_audio(block)
    if audio is None:
        return None
    if "file_id" in audio:
        return {"type": "input_audio", "input_audio": {"file_id": audio["file_id"]}}
    if "data" in audio:
        return {"type": "input_audio", "input_audio": {
            "data": audio["data"], "format": audio["mime_type"].split("/", 1)[1],
            "mime_type": audio["mime_type"],
        }}
    return {"type": "audio_url", "audio_url": {
        "url": audio["uri"], "mime_type": audio["mime_type"],
    }}


def split_audio_text(text: str):
    """还原 Claude Code Read 的音频封装；普通文字及文件信息保持原序。"""
    if "<graywill_audio" not in text:
        return [{"type": "text", "text": text}]
    parts = []
    position = 0
    for match in _AUDIO_ENVELOPE.finditer(text):
        if match.start() > position:
            parts.append({"type": "text", "text": text[position:match.start()]})
        block = {"type": "audio", "file_id": match[3]} if match[3] else {
            "type": "audio", "mime_type": match[1], "data": match[2],
        }
        parts.append(audio_to_openai(block))
        position = match.end()
    if position < len(text):
        parts.append({"type": "text", "text": text[position:]})
    return parts


def separate_tool_audio(messages: Iterable[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    """先补齐一批工具结果，再把音频作为用户内容提交，避免非法 function_result。"""
    pending_audio = []
    for message in messages:
        if message.get("role") != "tool":
            if pending_audio:
                yield {"role": "user", "content": pending_audio}
                pending_audio = []
            yield message
            continue
        content = message.get("content")
        if not isinstance(content, list):
            yield message
            continue
        remaining = []
        for block in content:
            if extract_audio(block):
                pending_audio.extend([
                    {"type": "text", "text": f"以下音频来自工具调用 {message.get('tool_call_id', '')}："},
                    block,
                ])
            else:
                remaining.append(block)
        yield {**message, "content": remaining or ""}
    if pending_audio:
        yield {"role": "user", "content": pending_audio}
