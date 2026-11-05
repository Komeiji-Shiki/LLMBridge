"""上游保存对象的查询、取消和删除：按创建时记下的线路和密钥转发，结果原样返回。

路由：
- Interactions：GET/DELETE /v1beta/interactions/{id}、POST /v1beta/interactions/{id}/cancel
- Responses：GET/DELETE /v1/responses/{id}、POST /v1/responses/{id}/cancel、GET /v1/responses/{id}/input_items
"""
import asyncio
import codecs
import json
import logging
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from converters.responses_openai import build_responses_error_response
from core.constants import GeminiDefaults
from core.request_context import credential_identity, current_request
from services import stored_objects
from services.protocol_events import payloads
from services.stored_objects import INTERACTIONS, RESPONSES
from . import api_routes as _chat_api

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stored-objects"])

# 客户端用来访问网关的凭据，不能带给上游
_CLIENT_QUERY_KEYS = ('key',)


def _not_found(kind: str, object_id: str):
    message = f"找不到 {object_id}：它不是经这个网关、用你的 key 创建的，或者创建它的线路和密钥已从配置中删除"
    if kind == RESPONSES:
        return build_responses_error_response(404, message)
    return JSONResponse(status_code=404, content={'error': {'code': 404, 'status': 'NOT_FOUND', 'message': message}})


def _error(kind: str, error: HTTPException):
    if kind == RESPONSES:
        return build_responses_error_response(error.status_code, error.detail)
    return JSONResponse(status_code=error.status_code, content={'error': {'code': error.status_code, 'message': str(error.detail)}})


def _object_path(kind: str, config: dict, object_id: str, suffix: str, query: str) -> str:
    if kind == INTERACTIONS:
        path = f'/interactions/{object_id}{suffix}'
    else:
        base = (config.get('endpoint_path') or '/responses').strip().rstrip('/')
        path = f"{base if base.startswith('/') else '/' + base}/{object_id}{suffix}"
    return path + ('?' + query if query else '')


def _base_url(kind: str, config: dict) -> str:
    return config.get('api_base_url') or (GeminiDefaults.BASE_URL if kind == INTERACTIONS else '')


async def bound_for_request(request: Request, kind: str, object_id: Optional[str], model: Optional[str]):
    """创建请求里带了 previous_*_id 时找回原线路，模型不同就照常选线路。

    先只校验身份（不计频率，正式校验时再计），记录按调用方隔离。
    """
    if not object_id or not isinstance(model, str):
        return None
    _chat_api._validate_request_api_key(request, model, allow_gemini_style=kind == INTERACTIONS,
                                        consume_rate_limit=False)
    context = current_request.get()
    owner = context.owner_id if context and context.authenticated else None
    bound = await asyncio.to_thread(stored_objects.resolve, owner, kind, object_id)
    return bound if bound is not None and bound.alias == model else None


async def _relay(request: Request, kind: str, object_id: str, method: str, suffix: str = ''):
    api = _chat_api
    api._app_state.update_activity()
    gemini_style = kind == INTERACTIONS
    try:
        api._check_verification_cooldown()
        api._validate_request_api_key(request, None, allow_gemini_style=gemini_style, consume_rate_limit=False)
    except HTTPException as error:
        return _error(kind, error)
    context = current_request.get()
    owner = context.owner_id if context and context.authenticated else None
    bound = await asyncio.to_thread(stored_objects.resolve, owner, kind, object_id)
    if bound is None:
        return _not_found(kind, object_id)
    try:
        # 访客 key 的模型权限和频率限制按创建对象时的模型计算
        api._validate_request_api_key(request, bound.alias, allow_gemini_style=gemini_style)
    except HTTPException as error:
        return _error(kind, error)
    if context is not None:
        context.model, context.endpoint = bound.alias, bound.config
        context.credential_fingerprint = credential_identity(bound.key)

    query = urlencode([(name, value) for name, value in request.query_params.multi_items()
                       if name not in _CLIENT_QUERY_KEYS])
    service = api._app_state.server.direct_api_service
    headers = {'x-goog-api-key': bound.key} if kind == INTERACTIONS else None
    try:
        upstream = await service.open_passthrough(
            method, _base_url(kind, bound.config), bound.key, _object_path(kind, bound.config, object_id, suffix, query),
            headers=headers)
    except Exception as error:
        logger.error('[STORED_OBJECTS] 转发 %s %s 失败: %s', method, object_id, error)
        return _error(kind, HTTPException(502, f'上游请求失败: {error}'))

    content_type = upstream.headers.get('Content-Type', 'application/json')
    settles = upstream.status == 200 and suffix in ('', '/cancel') and method != 'DELETE'
    if 'text/event-stream' not in content_type:
        try:
            body = await upstream.read()
        finally:
            upstream.release()
        if 200 <= upstream.status < 300 and method == 'DELETE':
            await asyncio.to_thread(stored_objects.forget, owner, kind, object_id)
        if settles:
            try:
                await stored_objects.settle(context, kind, object_id, bound, json.loads(body), service, api.monitoring_service)
            except ValueError:
                pass
        return Response(content=body, status_code=upstream.status, media_type=content_type.split(';')[0])

    async def stream():
        """重连的流原样转发，看到终态事件时补记用量。"""
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        pending, final = '', None
        try:
            async for chunk in upstream.content.iter_any():
                pending += decoder.decode(chunk).replace('\r\n', '\n')
                *blocks, pending = pending.split('\n\n')
                for block in blocks:
                    for value in payloads(block):
                        candidate = value.get('interaction') or value.get('response') if isinstance(value, dict) else None
                        if isinstance(candidate, dict) and candidate.get('status'):
                            final = candidate
                yield chunk
        finally:
            upstream.release()
            if settles and final is not None:
                await stored_objects.settle(context, kind, object_id, bound, final, service, api.monitoring_service)

    from services.native_exchange import ContextStreamingResponse
    return ContextStreamingResponse(stream(), context=context, status_code=upstream.status, media_type='text/event-stream',
                                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@router.get('/v1beta/interactions/{interaction_id}')
async def get_interaction(interaction_id: str, request: Request):
    return await _relay(request, INTERACTIONS, interaction_id, 'GET')


@router.delete('/v1beta/interactions/{interaction_id}')
async def delete_interaction(interaction_id: str, request: Request):
    return await _relay(request, INTERACTIONS, interaction_id, 'DELETE')


@router.post('/v1beta/interactions/{interaction_id}/cancel')
async def cancel_interaction(interaction_id: str, request: Request):
    return await _relay(request, INTERACTIONS, interaction_id, 'POST', '/cancel')


@router.get('/v1/responses/{response_id}')
async def get_response(response_id: str, request: Request):
    return await _relay(request, RESPONSES, response_id, 'GET')


@router.delete('/v1/responses/{response_id}')
async def delete_response(response_id: str, request: Request):
    return await _relay(request, RESPONSES, response_id, 'DELETE')


@router.post('/v1/responses/{response_id}/cancel')
async def cancel_response(response_id: str, request: Request):
    return await _relay(request, RESPONSES, response_id, 'POST', '/cancel')


@router.get('/v1/responses/{response_id}/input_items')
async def list_response_input_items(response_id: str, request: Request):
    return await _relay(request, RESPONSES, response_id, 'GET', '/input_items')
