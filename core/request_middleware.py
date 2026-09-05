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
from core.request_context import RequestContext, current_request, endpoint_identity
from core.conversation_store import conversation_store
from services.protocol_events import payloads, is_business_event

logger = logging.getLogger(__name__)


class GatewayRequestMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if scope['type'] != 'http' or scope.get('method') != 'POST' or not (path.startswith(('/v1/', '/v1beta/')) or path == '/api/admin/playground/run'):
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        session = headers.get(b'x-bridge-session-id', b'').decode('ascii', errors='replace')
        if session and not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', session):
            return await JSONResponse({'error': {'message': 'Invalid X-Bridge-Session-ID'}}, 400)(scope, receive, send)
        context = RequestContext()
        if session:
            context.session_id, context.explicit_session = session, True
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
            return message

        async def observe(message):
            nonlocal status, streaming, pending, response_object
            if message['type'] == 'http.response.start':
                status = message['status']
                response_headers = list(message.get('headers', []))
                streaming = b'text/event-stream' in dict(response_headers).get(b'content-type', b'')
                response_headers = [(name, value) for name, value in response_headers if name.lower() not in (b'x-bridge-session-id', b'x-bridge-request-id')]
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
                        session_key = context.cache_session()
                        record = {'request': context.request_body, 'upstream_request': context.upstream_request, 'response': response_object, 'status': status,
                                  'external_session_id': context.session_id,
                                  'wire_gzip_base64': base64.b64encode(wire_archive).decode(),
                                  'timings': context.snapshot()}
                        from core.exchange_archive import save_exchange
                        save_exchange(context, record)
                        conversation_store.touch(context.owner_id, session_key, context.model,
                                                 endpoint_identity(context.endpoint), context.credential_fingerprint)
                        conversation_store.put(context.owner_id, session_key, 'last_exchange', record)
                        if context.artifacts:
                            conversation_store.put(context.owner_id, session_key, 'signatures', context.artifacts)
                        if isinstance(response_object, dict) and response_object.get('id'):
                            conversation_store.put(context.owner_id, session_key, 'response:' + response_object['id'], record)
                    await asyncio.shield(asyncio.to_thread(persist))
            except Exception:
                logger.exception('会话缓存保存失败；完整请求日志仍由独立记录器保存')
            finally:
                archive.close()
                current_request.reset(token)
