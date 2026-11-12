"""Keep request context alive until a streamed HTTP response has finished."""
import asyncio
import base64
import codecs
import gzip
import io
import json
import logging
import re
from starlette.responses import JSONResponse
from core.config_loader import get_setting
from core.conversion_report import WARNINGS_HEADER, warnings_header_value
from core.request_context import CLIENT_TRACE_HEADERS, RequestContext, UPSTREAM_HEADER_EXCLUSIONS, current_request
from core.conversation_store import conversation_store
from services.protocol_events import payloads, is_business_event

logger = logging.getLogger(__name__)
_STORED_OBJECT_PATH = re.compile(r'^/(v1/responses|v1beta/interactions)/[^/]+(/input_items)?$')

def _should_capture_upstream_header(name: str) -> bool:
    """保留端到端请求头，只过滤代理控制和不可复用的传输头。"""
    return name.lower() not in UPSTREAM_HEADER_EXCLUSIONS


class GatewayRequestMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if path.startswith('/v1/bridge/audio/'):
            return await self.app(scope, receive, send)
        method = scope.get('method')
        # 上游保存对象的查询和删除也要识别调用方，记录按调用方隔离
        tracked = method == 'POST' or (method in ('GET', 'DELETE') and _STORED_OBJECT_PATH.match(path))
        if scope['type'] != 'http' or not tracked or not (path.startswith(('/v1/', '/v1beta/')) or path == '/api/admin/playground/run'):
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        session = headers.get(b'x-bridge-session-id', b'').decode('ascii', errors='replace')
        if session and not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', session):
            return await JSONResponse({'error': {'message': 'Invalid X-Bridge-Session-ID'}}, 400)(scope, receive, send)
        context = RequestContext()
        context.upstream_headers = {
            name.decode('ascii').lower(): value.decode('latin-1')
            for name, value in headers.items()
            if _should_capture_upstream_header(name.decode('ascii'))
        }
        if session:
            context.session_id, context.explicit_session = session, True
        else:
            # Claude Code 的会话 ID 只作为日志里的对话标识，不参与缓存隔离：回退、分叉和
            # 恢复会换一个新 ID，历史却原样回传，按它隔离会让新会话找不回之前的思考签名。
            client_session = headers.get(b'x-claude-code-session-id', b'').decode('ascii', errors='replace')
            if re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', client_session):
                context.session_id = client_session
        trace = {field: context.upstream_headers[name] for name, field in CLIENT_TRACE_HEADERS.items()
                 if name in context.upstream_headers}
        if trace:
            context.outcome['client_trace'] = trace
        token = current_request.set(context)
        archive = io.BytesIO()
        compressor = gzip.GzipFile(fileobj=archive, mode='wb', compresslevel=6, mtime=0)
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        pending = ''
        response_object = None
        status = 500
        streaming = False
        received = 0
        limit = int(get_setting('request_limits.max_body_mb', 256)) * 1024 * 1024

        async def receive_checked():
            nonlocal received
            message = await receive()
            received += len(message.get('body', b''))
            if received > limit:
                from fastapi import HTTPException
                raise HTTPException(413, '请求体超过配置的大小上限')
            if message['type'] == 'http.request' and not message.get('more_body'):
                context.mark('body_received')
            return message

        async def observe(message):
            nonlocal status, streaming, pending, response_object
            if message['type'] == 'http.response.start':
                status = message['status']
                response_headers = list(message.get('headers', []))
                streaming = b'text/event-stream' in dict(response_headers).get(b'content-type', b'')
                warnings_name = WARNINGS_HEADER.encode()
                existing_warnings = next((value for name, value in response_headers if name.lower() == warnings_name), None)
                response_headers = [(name, value) for name, value in response_headers
                                    if name.lower() not in (b'x-bridge-session-id', b'x-bridge-request-id', warnings_name)]
                warnings_value = warnings_header_value(existing_warnings)
                if warnings_value:
                    response_headers.append((warnings_name, warnings_value))
                # 限流和重试提示是上游对客户端说的话，转换协议也不能吞掉。
                present = {name.lower() for name, _ in response_headers}
                response_headers.extend((name.encode(), value.encode('latin-1', 'replace'))
                                        for name, value in context.upstream_response_headers.items()
                                        if name.encode() not in present)
                response_headers.extend([(b'x-bridge-session-id', context.session_id.encode()),
                                         (b'x-bridge-request-id', context.request_id.encode())])
                message = {**message, 'headers': response_headers}
            elif message['type'] == 'http.response.body':
                body = message.get('body', b'')
                if len(body) > 65536:
                    write_task = asyncio.create_task(asyncio.to_thread(compressor.write, body))
                    try:
                        await asyncio.shield(write_task)
                    except asyncio.CancelledError:
                        await write_task
                        raise
                else:
                    compressor.write(body)
                pending += decoder.decode(body)
                blocks = []
                if streaming:
                    pending = pending.replace('\r\n', '\n')
                    *blocks, pending = pending.split('\n\n')
                elif not message.get('more_body'):
                    blocks, pending = [pending], ''
                for block in blocks:
                    for value in payloads(block):
                        if is_business_event(value):
                            context.mark('first_business')
                        if isinstance(value, dict):
                            if value.get('type') in ('response.completed', 'response.incomplete'):
                                response_object = value.get('response')
                            elif value.get('event_type') in ('interaction.complete', 'interaction.completed'):
                                response_object = value.get('interaction')
                            elif not streaming:
                                response_object = value
                if not message.get('more_body'):
                    context.mark('finished')
            await send(message)

        try:
            await self.app(scope, receive_checked, observe)
        finally:
            context.mark('finished')
            compressor.close()
            wire_archive = archive.getvalue()
            try:
                if context.authenticated and context.endpoint and context.model:
                    def persist():
                        record = {'request': context.request_body, 'upstream_request': context.upstream_request, 'response': response_object, 'status': status,
                                  'external_session_id': context.session_id,
                                  'wire_gzip_base64': base64.b64encode(wire_archive).decode(),
                                  'timings': context.snapshot()}
                        from core.exchange_archive import save_exchange
                        # 完整交换只写入归档；会话库仅保存后续请求会读取的状态
                        save_exchange(context, record)
                        pending = context.artifacts.get('pending_signatures')
                        if pending:
                            conversation_store.remember_entries(context.owner_id, context.cache_session(), pending)
                    await asyncio.shield(asyncio.to_thread(persist))
            except Exception:
                logger.exception('会话缓存保存失败；完整请求日志仍由独立记录器保存')
            finally:
                archive.close()
                current_request.reset(token)
