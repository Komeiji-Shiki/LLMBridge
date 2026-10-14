import asyncio

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from core.request_context import RequestContext, current_request
from core.request_middleware import GatewayRequestMiddleware
from services.direct_api_service import DirectAPIService


class _FakeContent:
    async def iter_chunks(self):
        yield b'{"ok":true}', False


class _FakeResponse:
    status = 200
    headers = {"Content-Type": "application/json"}
    content = _FakeContent()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return {"ok": True}


class _FakeSession:
    def __init__(self):
        self.kwargs = None

    def post(self, endpoint, **kwargs):
        self.kwargs = kwargs
        return _FakeResponse()


def _collect_passthrough(context):
    async def run():
        session = _FakeSession()
        service = DirectAPIService(session)
        token = current_request.set(context)
        try:
            body = b''.join([
                chunk async for chunk in service.call_api_passthrough(
                    base_url='https://upstream.example/v1',
                    api_key='upstream-key',
                    request_body={'model': 'demo'},
                )
            ])
            return body, session.kwargs['headers']
        finally:
            current_request.reset(token)

    return asyncio.run(run())


def test_passthrough_forwards_client_end_to_end_headers():
    body, headers = _collect_passthrough(RequestContext(upstream_headers={
        'x-opencode-session': 'session-from-client',
        'x-provider-trace': 'trace-from-client',
        'user-agent': 'client-agent/1.0',
        'accept': 'application/json',
    }))

    assert body == b'{"ok":true}'
    assert headers['x-opencode-session'] == 'session-from-client'
    assert headers['x-provider-trace'] == 'trace-from-client'
    assert headers['user-agent'] == 'client-agent/1.0'
    assert headers['accept'] == 'application/json'
    assert headers['Authorization'] == 'Bearer upstream-key'


def test_passthrough_does_not_invent_missing_opencode_session():
    _, headers = _collect_passthrough(RequestContext())

    assert 'x-opencode-session' not in headers


@pytest.mark.parametrize('upstream_method', [None, 'call_gemini_native_api', 'call_gemini_interactions_api'])
def test_gateway_context_excludes_proxy_and_local_auth_headers(upstream_method):
    app = FastAPI()
    app.add_middleware(GatewayRequestMiddleware)

    @app.post('/v1/responses')
    async def route(request: Request):
        if upstream_method:
            # 直接捕获转换调用最终交给 HTTP 客户端的请求头，覆盖异步上下文传递。
            session = _FakeSession()
            service = DirectAPIService(session)
            result = [item async for item in getattr(service, upstream_method)(
                api_key='upstream-key', model='demo',
                messages=[{'role': 'user', 'content': 'hello'}],
                base_url='https://upstream.example/v1',
            )]
            assert result == [{'ok': True}]
            return session.kwargs['headers']
        return current_request.get().upstream_headers

    response = TestClient(app).post('/v1/responses', headers={
        'X-OpenCode-Session': 'session-from-client',
        'X-Provider-Trace': 'trace-from-client',
        'User-Agent': 'client-agent/1.0',
        'Authorization': 'Bearer local-key',
        'X-Goog-Api-Key': 'local-google-key',
        'Cookie': 'secret=do-not-forward',
        'X-Bridge-Session-ID': 'local-session',
    })

    assert response.status_code == 200
    captured = response.json()
    assert captured['x-opencode-session'] == 'session-from-client'
    assert captured['x-provider-trace'] == 'trace-from-client'
    assert captured['user-agent'] == 'client-agent/1.0'
    assert 'accept' in captured
    assert 'accept-encoding' in captured
    for name in ('authorization', 'cookie', 'x-bridge-session-id'):
        assert name not in captured
    if upstream_method == 'call_gemini_interactions_api':
        assert captured['x-goog-api-key'] == 'upstream-key'
    else:
        assert 'x-goog-api-key' not in captured
