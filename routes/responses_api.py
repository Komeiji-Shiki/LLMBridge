"""OpenAI Responses API 兼容路由。"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from converters.responses_openai import (
    ResponsesRequestError,
    build_responses_error_response,
    build_responses_streaming_response,
    collect_chat_stream_response,
    convert_chat_response_to_responses,
    convert_responses_to_chat_request,
    read_response_body_bytes,
)
from core.errors import BadRequestError

from . import api_routes as _chat_api
from ._direct_api_utils import map_upstream_error_to_status_code

logger = logging.getLogger(__name__)
router = APIRouter(tags=["responses-api"])


def _is_stream_response(response: Response) -> bool:
    return isinstance(response, StreamingResponse) or getattr(response, "media_type", "") == "text/event-stream"


def _parse_json_body(body: bytes) -> Dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BadRequestError("上游返回了无法解析的 JSON 响应。", code="invalid_upstream_response").to_http_exception() from exc
    if not isinstance(value, dict):
        raise BadRequestError("上游返回的响应不是 JSON 对象。", code="invalid_upstream_response").to_http_exception()
    return value


async def _read_error_response(response: Response) -> JSONResponse:
    body = await read_response_body_bytes(response)
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {"message": body.decode("utf-8", errors="replace")}
    if isinstance(payload, dict) and payload.get("error") is not None:
        return JSONResponse(status_code=response.status_code, content=payload)
    return build_responses_error_response(response.status_code, payload)


@router.post("/v1/responses")
async def responses_endpoint(request: Request):
    """处理 OpenAI Responses API 请求（第一阶段无状态兼容）。"""
    app_state = _chat_api._app_state
    app_state.update_activity()
    try:
        _chat_api._check_verification_cooldown()
    except HTTPException as exc:
        return build_responses_error_response(exc.status_code, exc.detail)

    try:
        responses_request = await _chat_api._read_request_json_non_blocking(request)
    except json.JSONDecodeError as exc:
        return build_responses_error_response(400, str(exc) or "无效的 JSON 请求体。")
    except HTTPException as exc:
        return build_responses_error_response(exc.status_code, exc.detail)
    except Exception as exc:
        if "Disconnect" in type(exc).__name__:
            return JSONResponse(status_code=499, content={"error": {"message": "Client Disconnected", "type": "api_error"}})
        raise

    try:
        chat_request = convert_responses_to_chat_request(responses_request)
    except ResponsesRequestError as exc:
        return build_responses_error_response(400, str(exc))

    model = chat_request["model"]
    try:
        # 认证在兼容层完成一次，内部调度使用 skip_api_auth，避免重复计 RPM。
        _chat_api._validate_request_api_key(request, model)
        upstream_response = await _chat_api._dispatch_chat_completions_core(
            chat_request,
            request=None,
            skip_api_auth=True,
        )
    except HTTPException as exc:
        logger.warning("[RESPONSES_COMPAT] 上游请求失败: HTTP %s", exc.status_code)
        return build_responses_error_response(exc.status_code, exc.detail)

    response_status = getattr(upstream_response, "status_code", 200)
    if response_status >= 400:
        return await _read_error_response(upstream_response)

    if chat_request.get("stream"):
        return build_responses_streaming_response(
            upstream_response,
            request=responses_request,
            model=model,
        )

    if _is_stream_response(upstream_response):
        chat_response = await collect_chat_stream_response(upstream_response, model=model)
    else:
        try:
            chat_response = _parse_json_body(await read_response_body_bytes(upstream_response))
        except HTTPException as exc:
            return build_responses_error_response(exc.status_code, exc.detail)

    if chat_response.get("error") is not None:
        error_status = map_upstream_error_to_status_code(
            chat_response,
            default_status_code=502,
        )
        return JSONResponse(status_code=error_status, content=chat_response)

    try:
        response_payload = convert_chat_response_to_responses(
            chat_response,
            request=responses_request,
        )
    except ResponsesRequestError as exc:
        logger.error("[RESPONSES_COMPAT] 响应转换失败: %s", exc, exc_info=True)
        return build_responses_error_response(502, str(exc))
    return JSONResponse(status_code=200, content=response_payload)


__all__ = ["router", "responses_endpoint"]
