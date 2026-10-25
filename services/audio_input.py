"""在调用 Gemini 之前解析文件引用，二进制内容不进入客户端文本历史及本地文字计数。"""
import asyncio
import base64
from datetime import datetime, timezone
import json
from urllib.parse import urlsplit

from fastapi import HTTPException

from converters.audio import extract_audio
from core.audio_files import AudioFiles
from core.request_context import current_request, credential_identity, endpoint_identity


# 音频文档给出的内联限制包含整个请求；文件上传本身沿用网关已有的请求体上限。
INLINE_AUDIO_BYTES = 20_000_000


def supports_files_api(config):
    base = config.get("api_base_url") or "https://generativelanguage.googleapis.com/v1beta"
    return config.get("audio_files_api", urlsplit(base).hostname == "generativelanguage.googleapis.com") is True


async def _json_response(response, operation):
    if response.status >= 400:
        raise HTTPException(response.status, f"{operation}失败：{(await response.text())[:500]}")
    return await response.json()


async def _uploaded_uri(store, file_id, metadata, session, api_key, base_url, config):
    cache_key = endpoint_identity(config) + credential_identity(api_key)
    cached = metadata.get("upstream_files", {}).get(cache_key)
    now = datetime.now(timezone.utc)
    if cached and cached.get("expirationTime"):
        expires = datetime.fromisoformat(cached["expirationTime"].replace("Z", "+00:00"))
        if expires > now and cached.get("state") == "ACTIVE":
            return cached["uri"]

    base = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    prefix, version = base.rsplit("/", 1)
    if version not in ("v1", "v1beta"):
        raise HTTPException(400, "Files API 基础地址必须包含 /v1 或 /v1beta")
    headers = {
        "x-goog-api-key": api_key, "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(metadata["size"]),
        "X-Goog-Upload-Header-Content-Type": metadata["mime_type"],
    }
    async with session.post(f"{prefix}/upload/{version}/files", headers=headers,
                            json={"file": {"displayName": metadata["filename"]}}) as response:
        if response.status >= 400:
            await _json_response(response, "创建音频上传")
        upload_url = response.headers.get("X-Goog-Upload-URL")
        if not upload_url:
            raise HTTPException(502, "上游 Files API 没有返回上传地址")
    data_path, _ = store.paths(file_id)
    with data_path.open("rb") as stream:
        async with session.post(upload_url, data=stream, headers={
            "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize",
        }) as response:
            uploaded = (await _json_response(response, "上传音频"))["file"]
    # 音频一般会立即可用；需要处理时等待同一个文件，不重新上传。
    for _ in range(60):
        if uploaded.get("state") != "PROCESSING":
            break
        await asyncio.sleep(1)
        async with session.get(f'{base}/{uploaded["name"]}', headers={"x-goog-api-key": api_key}) as response:
            uploaded = await _json_response(response, "检查音频处理状态")
    if uploaded.get("state") not in (None, "ACTIVE"):
        raise HTTPException(502, "上游音频文件尚未就绪或处理失败")
    metadata.setdefault("upstream_files", {})[cache_key] = uploaded
    await asyncio.to_thread(store.save_metadata, file_id, metadata)
    return uploaded["uri"]


async def resolve_audio_references(messages, session, api_key, base_url, config):
    references = {}
    occurrences = {}
    context = current_request.get()
    store = AudioFiles()
    for message in messages:
        content = message.get("content")
        for part in content if isinstance(content, list) else []:
            audio = extract_audio(part)
            if audio and "file_id" in audio:
                occurrences[audio["file_id"]] = occurrences.get(audio["file_id"], 0) + 1
            if audio and "file_id" in audio and audio["file_id"] not in references:
                if context is None or not context.authenticated:
                    raise HTTPException(401, "音频文件引用需要经过身份验证")
                file_id = audio["file_id"]
                references[file_id] = await asyncio.to_thread(store.metadata, file_id, context.owner_id)
    if not references:
        return messages

    # base64 只在最后构造媒体请求时生成，估算时按传输字节计算，不能当成文字 Token。
    inline_size = len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
    inline_size += sum(((info["size"] + 2) // 3 * 4 + 128) * occurrences[file_id]
                       for file_id, info in references.items())
    use_files = inline_size > INLINE_AUDIO_BYTES
    if use_files and not supports_files_api(config):
        raise HTTPException(413, "音频及对话超过当前端点的内联大小限制；请使用支持 Files API 的端点，或明确选择压缩、截取或分段")
    resolved = {}
    for file_id, metadata in references.items():
        if use_files:
            uri = await _uploaded_uri(store, file_id, metadata, session, api_key, base_url, config)
            resolved[file_id] = {"type": "audio_url", "audio_url": {
                "url": uri, "mime_type": metadata["mime_type"],
            }}
        else:
            data_path, _ = store.paths(file_id)
            data = await asyncio.to_thread(lambda: base64.b64encode(data_path.read_bytes()).decode("ascii"))
            resolved[file_id] = {"type": "input_audio", "input_audio": {
                "data": data, "mime_type": metadata["mime_type"],
                "format": metadata["mime_type"].split("/", 1)[1],
            }}
    result = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            result.append(message)
            continue
        parts = []
        for part in content:
            audio = extract_audio(part)
            parts.append(resolved[audio["file_id"]] if audio and "file_id" in audio else part)
        result.append({**message, "content": parts})
    return result
