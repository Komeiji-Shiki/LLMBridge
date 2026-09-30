"""Responses WebSocket 网关：一个连接只使用一个模型路由和上游凭据。"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from core.config_loader import get_setting
from core.constants import TimeoutDefaults
from core.model_archive import is_model_archived
from core.request_context import RequestContext, UPSTREAM_HEADER_EXCLUSIONS, current_request
from services.provider_capabilities import apply_native_tool_defaults
from services.responses_websocket_monitor import ResponsesWebSocketMonitor
from . import api_routes as api
from ._direct_api_utils import get_api_key

router = APIRouter(tags=["responses-api"])
logger = logging.getLogger(__name__)


def websocket_url(config: dict) -> str:
    base = str(config.get("api_base_url") or "").rstrip("/")
    path = "/" + str(config.get("endpoint_path") or "responses").lstrip("/")
    parsed = urlsplit(base + path)
    if parsed.scheme not in ("http", "https", "ws", "wss") or not parsed.netloc:
        raise HTTPException(400, "模型没有有效的 Responses 上游地址。")
    return urlunsplit(({"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme), parsed.netloc, parsed.path, parsed.query, ""))


def upstream_headers(websocket: WebSocket, api_key: str) -> dict[str, str]:
    # WebSocket 握手头必须由新连接生成；客户端凭据只用于 Bridge 自身认证。
    headers = {name: value for name, value in websocket.headers.items()
               if name.lower() not in UPSTREAM_HEADER_EXCLUSIONS | {"origin"}
               and not name.lower().startswith("sec-websocket-")}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    return headers


async def send_error(websocket: WebSocket, status: int, message: str, event: dict | None = None, retry_after: str | None = None):
    error = {"type": "error", "status": status,
             "error": {"type": "invalid_request_error" if status < 500 else "api_error", "message": message}}
    if event and event.get("event_id"):
        error["event_id"] = event["event_id"]
    if retry_after:
        error["retry_after"] = retry_after
    try:
        await websocket.send_json(error)
    except (RuntimeError, WebSocketDisconnect):
        pass  # 对端已经关闭时，继续完成本连接清理。


async def receive_event(websocket: WebSocket, limit: int) -> dict:
    frame = await websocket.receive()
    if frame["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(frame.get("code", 1000))
    raw = frame.get("text") if frame.get("text") is not None else frame.get("bytes", b"")
    size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
    if size > limit:
        raise HTTPException(413, "WebSocket 事件超过配置的请求大小上限。")
    try:
        event = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(400, "WebSocket 事件必须是 JSON 对象。") from exc
    if not isinstance(event, dict):
        raise HTTPException(400, "WebSocket 事件必须是 JSON 对象。")
    return event


def prepare_event(event: dict, alias: str, config: dict) -> dict:
    if event.get("type") != "response.create":
        return event
    if event.get("model", alias) != alias:
        raise HTTPException(400, "WebSocket 已绑定当前模型；切换模型时请建立新连接。")
    result = apply_native_tool_defaults(event, config, "responses")
    result["model"] = config.get("target_model_id") or config.get("model_id") or alias
    return result


def rewrite_model(raw: str | bytes, alias: str, target: str) -> str | bytes:
    if alias == target:
        return raw
    try:
        event = json.loads(raw)
    except (ValueError, UnicodeError):
        return raw
    if not isinstance(event, dict):
        return raw
    changed = False
    for item in (event, event.get("response")):
        if isinstance(item, dict) and item.get("model") == target:
            item["model"] = alias
            changed = True
    if not changed:
        return raw
    encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return encoded.encode("utf-8") if isinstance(raw, bytes) else encoded


@router.websocket("/v1/responses")
async def responses_websocket(websocket: WebSocket):
    context = RequestContext()
    token = current_request.set(context)
    accepted = False
    upstream = None
    monitor = None
    tasks = []
    close_code = 1000
    try:
        origin = websocket.headers.get("origin")
        if origin and urlsplit(origin).netloc != websocket.headers.get("host"):
            raise HTTPException(403, "WebSocket 来源与当前服务不一致。")
        api._validate_request_api_key(websocket, None, consume_rate_limit=False)
        api._check_verification_cooldown()
        await websocket.accept()
        accepted = True
        limit = int(get_setting("request_limits.max_body_mb", 256)) * 1024 * 1024
        first = await asyncio.wait_for(receive_event(websocket, limit),
                                       timeout=api.CONFIG.get("first_chunk_timeout_seconds", TimeoutDefaults.FIRST_CHUNK_TIMEOUT))
        alias = first.get("model")
        if first.get("type") != "response.create" or not isinstance(alias, str) or not alias.strip():
            raise HTTPException(400, "首个事件必须是带有模型名的 response.create。")
        api._validate_request_api_key(websocket, alias)
        raw_config = api.MODEL_ENDPOINT_MAP.get(alias)
        if raw_config is not None and is_model_archived(raw_config):
            raise HTTPException(404, "模型不存在。")
        config = await api._select_endpoint_config_for_model(alias)
        if not isinstance(config, dict) or config.get("api_type") != "responses_native":
            raise HTTPException(400, "当前模型需要配置 responses_native 端点才能使用 WebSocket。")
        config = copy.deepcopy(config)
        context.model, context.endpoint = alias, config
        context.request_body = copy.deepcopy(first)
        service = api._app_state.server.direct_api_service
        if not service or not service.session:
            raise HTTPException(503, "Direct API service not initialized")
        key = await get_api_key(alias, config.get("api_keys") or config.get("api_key"),
                                strategy=config.get("api_key_strategy", "round_robin"),
                                cooldown_seconds=int(config.get("api_key_cooldown_seconds") or 172800))
        headers = upstream_headers(websocket, key)
        context.upstream_headers = {name: value for name, value in headers.items()
                                   if name.lower() not in UPSTREAM_HEADER_EXCLUSIONS}
        connect_timeout = api.CONFIG.get("download_timeout", {}).get("connect", TimeoutDefaults.DOWNLOAD_CONNECT_TIMEOUT)
        upstream = await asyncio.wait_for(service.session.ws_connect(websocket_url(config), headers=headers,
                                         heartbeat=20, max_msg_size=limit), timeout=connect_timeout)
        target = config.get("target_model_id") or config.get("model_id") or alias
        monitor = ResponsesWebSocketMonitor(alias, config, service)
        monitor.request(first)
        await upstream.send_json(prepare_event(first, alias, config), dumps=lambda value: json.dumps(value, ensure_ascii=False))

        async def client_to_upstream():
            while True:
                event = await receive_event(websocket, limit)
                try:
                    if event.get("type") in ("response.create", "response.steer"):
                        api._validate_request_api_key(websocket, alias)
                    prepared = prepare_event(event, alias, config)
                except HTTPException as exc:
                    await send_error(websocket, exc.status_code, str(exc.detail), event)
                    if exc.status_code in (401, 403):
                        return 1008
                    continue
                monitor.request(event)
                await upstream.send_json(prepared, dumps=lambda value: json.dumps(value, ensure_ascii=False))

        async def upstream_to_client():
            async for frame in upstream:
                if frame.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    data = rewrite_model(frame.data, alias, target)
                    # 先完整观察事件，终态后的客户端正常关闭仍应记为成功。
                    try:
                        await monitor.event(data)
                    except Exception:
                        logger.warning("Responses WebSocket 统计事件处理失败。")
                    if isinstance(data, bytes):
                        await websocket.send_bytes(data)
                    else:
                        await websocket.send_text(data)
                elif frame.type == aiohttp.WSMsgType.ERROR:
                    return 1011
            code = upstream.close_code
            return code if code in (1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014) else 1011

        tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
        completed, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in completed:
            close_code = task.result() or 1000
    except WebSocketDisconnect:
        pass
    except HTTPException as exc:
        close_code = 1008 if exc.status_code < 500 else 1013
        if accepted:
            await send_error(websocket, exc.status_code, str(exc.detail))
    except aiohttp.WSServerHandshakeError as exc:
        close_code = 1013
        await send_error(websocket, exc.status, "上游 WebSocket 握手失败。", retry_after=exc.headers.get("Retry-After") if exc.headers else None)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        close_code = 1011
        if accepted:
            await send_error(websocket, 502, "上游 WebSocket 连接中断或超时。")
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if upstream is not None:
            await upstream.close()
        if monitor is not None:
            try:
                await monitor.close()
            except Exception:
                logger.warning("Responses WebSocket 统计记录未能完成。")
        try:
            await websocket.close(code=close_code)
        except (RuntimeError, WebSocketDisconnect):
            pass
        current_request.reset(token)
