"""Claude Code 音频能力协商与二进制上传。"""
from pathlib import PurePosixPath
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request

from core.audio_files import AudioFiles
from core.config_loader import get_setting
from core.request_context import RequestContext, current_request
from services.audio_input import INLINE_AUDIO_BYTES, supports_files_api

router = APIRouter(tags=["audio-files"])


def authenticate_audio(request):
    # GET 能力查询也使用与模型请求相同的认证；不消耗一次模型请求的 RPM。
    from routes.api_routes import _validate_request_api_key
    context = current_request.get()
    token = None
    if context is None:
        context = RequestContext()
        token = current_request.set(context)
    try:
        model = request.query_params.get("model")
        _validate_request_api_key(request, model, consume_rate_limit=False)
        return context.owner_id
    finally:
        if token is not None:
            current_request.reset(token)


@router.get("/v1/bridge/audio/capabilities")
async def audio_capabilities(request: Request):
    authenticate_audio(request)
    from routes.api_routes import MODEL_ENDPOINT_MAP
    model = request.query_params.get("model")
    config = MODEL_ENDPOINT_MAP.get(model)
    endpoints = config if isinstance(config, list) else [config]
    endpoints = [item for item in endpoints if isinstance(item, dict)]
    supported = bool(endpoints) and all(item.get("api_type") == "gemini_native" for item in endpoints)
    return {
        "audio_files_version": 1, "supported": supported,
        "upload_path": "bridge/audio/files",
        "max_upload_bytes": int(get_setting("request_limits.max_body_mb", 256)) * 1024 * 1024,
        "inline_audio_bytes": INLINE_AUDIO_BYTES,
        "files_api": supported and all(supports_files_api(item) for item in endpoints),
    }


@router.post("/v1/bridge/audio/files")
async def upload_audio_file(request: Request):
    owner_id = authenticate_audio(request)
    mime_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if not mime_type.startswith("audio/"):
        raise HTTPException(400, "上传内容必须声明 audio/* MIME 类型")
    filename = PurePosixPath(unquote(request.headers.get("x-audio-filename", "audio")).replace("\\", "/")).name
    return await AudioFiles().upload(
        request.stream(), owner_id, mime_type, filename,
        int(get_setting("request_limits.max_body_mb", 256)) * 1024 * 1024,
    )
