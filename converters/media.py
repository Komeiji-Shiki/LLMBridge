"""文件内容块的统一表示。内部一律用 Chat 的 file 块，各协议只和它互转。

    {"type": "file", "file": {"file_data": "data:application/pdf;base64,...", "filename": "a.pdf"}}
    {"type": "file", "file": {"file_url": "https://...", "filename": "a.pdf"}}
    {"type": "file", "file": {"file_id": "file_abc"}}

音频走 converters.audio，图片走 image_url；这里只管文档类文件。
"""
import base64
import binascii
import mimetypes
from typing import Any, Dict, List, Optional

from core.conversion_report import note_loss

FILE_PART_TYPES = ('file', 'input_file', 'document')
_PROVIDERS_WITHOUT_FILES = ('deepseek',)
_DEFAULT_EXTENSIONS = {'application/pdf': '.pdf', 'text/plain': '.txt', 'text/markdown': '.md', 'text/csv': '.csv'}
_TEXT_MIME_TYPES = ('application/json', 'application/xml', 'application/javascript', 'application/x-yaml')


def _parse_data_url(url: str):
    header, separator, encoded = url.partition(',')
    if not separator or not header.endswith(';base64'):
        return None, None
    return header[5:].split(';', 1)[0], encoded


def extract_file(part: Any) -> Optional[Dict[str, str]]:
    """解析 file / input_file / Anthropic document，返回 mime_type、data 或 uri 或 file_id、filename。

    纯文本来源的 document 由 document_text 处理，音频由 extract_audio 处理，这里都返回 None。
    """
    if not isinstance(part, dict) or part.get('type') not in FILE_PART_TYPES:
        return None
    kind = part['type']
    if kind == 'document':
        source = part.get('source')
        if not isinstance(source, dict):
            return None
        filename = part.get('title')
        mime_type = source.get('media_type') or source.get('mime_type')
        if source.get('type') == 'base64' and source.get('data'):
            found = {'mime_type': mime_type or 'application/pdf', 'data': source['data']}
        elif source.get('type') == 'url' and source.get('url'):
            found = {'mime_type': mime_type or mimetypes.guess_type(source['url'].split('?', 1)[0])[0] or 'application/pdf',
                     'uri': source['url']}
        elif source.get('type') == 'file' and source.get('file_id'):
            found = {'file_id': source['file_id']}
        else:
            return None
    else:
        body = part.get('file') if kind == 'file' and isinstance(part.get('file'), dict) else part
        filename = body.get('filename')
        mime_type = body.get('mime_type') or body.get('media_type')
        data, uri, file_id = body.get('file_data'), body.get('file_url'), body.get('file_id')
        if isinstance(data, str) and data:
            if data.startswith('data:'):
                mime_type, data = _parse_data_url(data)
                if data is None:
                    return None
            guessed = mimetypes.guess_type(filename or '')[0]
            found = {'mime_type': mime_type or guessed or 'application/pdf', 'data': data}
        elif isinstance(uri, str) and uri:
            found = {'mime_type': mime_type or mimetypes.guess_type(uri.split('?', 1)[0])[0] or 'application/pdf',
                     'uri': uri}
        elif isinstance(file_id, str) and file_id:
            found = {'file_id': file_id}
        else:
            return None
    if filename:
        found['filename'] = str(filename)
    if str(found.get('mime_type', '')).startswith('audio/'):
        return None
    return found


def document_text(part: Any) -> Optional[str]:
    """Anthropic 的纯文本和内容块 document 没有文件形态，直接展开成文本。"""
    source = part.get('source') if isinstance(part, dict) and part.get('type') == 'document' else None
    if not isinstance(source, dict):
        return None
    if source.get('type') == 'text' and isinstance(source.get('data'), str):
        text = source['data']
    elif source.get('type') == 'content':
        content = source.get('content')
        text = content if isinstance(content, str) else '\n'.join(
            str(block.get('text', '')) for block in content or []
            if isinstance(block, dict) and block.get('type') == 'text')
    else:
        return None
    return f"{part['title']}\n{text}" if part.get('title') else text


def search_result_text(part: Dict[str, Any]) -> str:
    """Anthropic search_result 展开成带出处的文本。"""
    lines = [str(part[key]) for key in ('title', 'source') if part.get(key)]
    for block in part.get('content') or []:
        if isinstance(block, dict) and block.get('type') == 'text':
            lines.append(str(block.get('text', '')))
    return '\n'.join(lines)


def file_part(found: Dict[str, str]) -> Dict[str, Any]:
    """extract_file 的结果转内部 file 块。"""
    body: Dict[str, str] = {}
    if 'data' in found:
        body['file_data'] = f"data:{found['mime_type']};base64,{found['data']}"
    elif 'uri' in found:
        body['file_url'] = found['uri']
    else:
        body['file_id'] = found['file_id']
    if found.get('filename'):
        body['filename'] = found['filename']
    return {'type': 'file', 'file': body}


def default_filename(found: Dict[str, str]) -> str:
    mime_type = found.get('mime_type', '')
    extension = _DEFAULT_EXTENSIONS.get(mime_type) or mimetypes.guess_extension(mime_type) or ''
    return found.get('filename') or f'document{extension}'


def file_to_responses(part: Any) -> Optional[Dict[str, Any]]:
    found = extract_file(part)
    if found is None:
        return None
    result: Dict[str, Any] = {'type': 'input_file'}
    if 'data' in found:
        result['file_data'] = f"data:{found['mime_type']};base64,{found['data']}"
        result['filename'] = default_filename(found)
    elif 'uri' in found:
        result['file_url'] = found['uri']
    else:
        result['file_id'] = found['file_id']
    return result


def is_text_mime(mime_type: str) -> bool:
    return bool(mime_type) and (mime_type.startswith('text/') or mime_type in _TEXT_MIME_TYPES)


def file_text(found: Dict[str, str]) -> Optional[str]:
    """文本类文件解码成字符串，其他类型或解码失败返回 None。"""
    if 'data' not in found or not is_text_mime(found.get('mime_type', '')):
        return None
    try:
        return base64.b64decode(found['data'], validate=True).decode('utf-8')
    except (binascii.Error, ValueError):
        return None


def file_to_anthropic(part: Any) -> Optional[Dict[str, Any]]:
    """Anthropic 只收 PDF 和纯文本文档，其他类型返回 None。"""
    found = extract_file(part)
    if found is None:
        return None
    result: Optional[Dict[str, Any]] = None
    text = file_text(found)
    if text is not None:
        result = {'type': 'document', 'source': {'type': 'text', 'media_type': 'text/plain', 'data': text}}
    elif 'data' in found and found.get('mime_type') == 'application/pdf':
        result = {'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': found['data']}}
    elif 'uri' in found and found.get('mime_type') == 'application/pdf':
        result = {'type': 'document', 'source': {'type': 'url', 'url': found['uri']}}
    if result is not None and found.get('filename'):
        result['title'] = found['filename']
    return result


def file_to_gemini(part: Any) -> Optional[Dict[str, Any]]:
    found = extract_file(part)
    if found is None or 'file_id' in found:
        return None
    if 'data' in found:
        return {'inlineData': {'mimeType': found['mime_type'], 'data': found['data']}}
    return {'fileData': {'mimeType': found['mime_type'], 'fileUri': found['uri']}}


def adapt_chat_files(messages: list, provider: str) -> list:
    """Chat 上游的文件兼容处理，不修改传入的消息。

    文本类文件解码成文字，所有上游都能读，也不占文件额度；OpenAI 系之外不一定收 file 块，
    已知不收的官方上游换成占位文字。工具结果只接受文字，文件要放到紧随其后的 user 消息里。
    """
    result: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []

    def flush():
        nonlocal pending
        if pending:
            result.append({"role": "user", "content": pending})
            pending = []

    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list) or not any(
                isinstance(part, dict) and part.get("type") in FILE_PART_TYPES for part in content):
            if not (isinstance(message, dict) and message.get("role") == "tool"):
                flush()
            result.append(message)
            continue
        kept = []
        for part in content:
            found = extract_file(part)
            if found is None:
                kept.append(part)
                continue
            text = file_text(found)
            if text is not None:
                converted = {"type": "text", "text": f"{found['filename']}\n{text}" if found.get("filename") else text}
            elif provider in _PROVIDERS_WITHOUT_FILES:
                note_loss("content.file", f"{provider} 不接受文件内容块，已用文字占位")
                converted = {"type": "text", "text": f"[文件 {found.get('filename', '')} 未发送：当前上游不支持]"}
            else:
                converted = file_part(found)
            if message.get("role") == "tool" and converted["type"] == "file":
                pending.append(converted)
            else:
                kept.append(converted)
        if message.get("role") != "tool":
            flush()
        result.append({**message, "content": kept or ""})
    flush()
    return result


def gemini_code_text(part: Any) -> Optional[str]:
    """Gemini 代码执行的代码和结果转成 Markdown，其他 part 返回 None。

    显示给 Chat 客户端和计算历史指纹必须用同一份文本，否则客户端回传后对不上。
    """
    if not isinstance(part, dict):
        return None
    code = part.get("executableCode")
    if isinstance(code, dict):
        language = str(code.get("language") or "").lower()
        language = "" if language == "language_unspecified" else language
        return f"\n```{language}\n{code.get('code', '')}\n```\n"
    result = part.get("codeExecutionResult")
    if isinstance(result, dict):
        return f"\n```\n{result.get('output', '')}\n```\n"
    return None


def file_to_interactions(part: Any) -> Optional[Dict[str, Any]]:
    found = extract_file(part)
    if found is None or 'file_id' in found:
        return None
    key = 'data' if 'data' in found else 'uri'
    return {'type': 'document', key: found[key], 'mime_type': found['mime_type']}
