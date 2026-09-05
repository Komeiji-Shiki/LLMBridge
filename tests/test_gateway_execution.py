import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from core.request_context import RequestContext, current_request
from core.auth_observer import observe_authenticated_request
from core.conversation_store import ConversationStore
from services.request_execution import retry_before_output


def test_auth_observer_does_not_turn_failure_into_success():
    def denied(*args, **kwargs):
        raise PermissionError('denied')
    context = RequestContext()
    token = current_request.set(context)
    try:
        with pytest.raises(PermissionError):
            observe_authenticated_request(denied)(MagicMock(), 'model')
        assert not context.authenticated
        assert context.owner_id == 'unattributed'
    finally:
        current_request.reset(token)


def test_conversation_isolation_and_idle_expiry(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / 'state.db')
    monkeypatch.setattr('core.conversation_store.time.time', lambda: 1000000)
    store.touch('a', 'same', 'm', 'endpoint')
    store.put('a', 'same', 'signature', {'data': 'secret'})
    assert store.get('b', 'same', 'signature') is None
    reloaded = ConversationStore(tmp_path / 'state.db')
    assert reloaded.get('a', 'same', 'signature') == {'data': 'secret'}
    monkeypatch.setattr('core.conversation_store.time.time', lambda: 1000000 + 2 * 86400)
    store.touch('a', 'same', 'm', 'endpoint')
    monkeypatch.setattr('core.conversation_store.time.time', lambda: 1000000 + 4 * 86400)
    assert store.cleanup() == 0
    monkeypatch.setattr('core.conversation_store.time.time', lambda: 1000000 + 5 * 86400)
    assert store.get('a', 'same', 'signature') is None
    assert store.cleanup() == 1


@pytest.mark.parametrize('business', [False, True])
def test_retry_holds_prelude_but_never_replays_business_events(business):
    calls = []
    @retry_before_output
    async def upstream(api_key='', request_body=None):
        calls.append(api_key)
        yield b'data: {"type":"response.created","response":{"id":"start"}}\n\n'
        if business:
            yield b'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
        if len(calls) == 1:
            yield b'data: {"error":{"code":503,"message":"busy"}}\n\n'
        else:
            yield b'data: {"type":"response.completed","response":{"id":"end"}}\n\n'
    async def run():
        context = RequestContext(model='test', endpoint={'auto_retry': {'enabled': True, 'max_retries': 1, 'retry_delay_seconds': 0}})
        token = current_request.set(context)
        try:
            return b''.join([chunk async for chunk in upstream(request_body={'stream': True})])
        finally:
            current_request.reset(token)
    result = asyncio.run(run())
    assert len(calls) == (1 if business else 2)
    assert result.count(b'response.created') == 1
    assert (b'503' in result) == business


def test_request_cache_requires_auth_and_excludes_auth_headers(tmp_path, monkeypatch):
    import base64
    import gzip
    import json
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from core.request_middleware import GatewayRequestMiddleware
    from routes import api_routes
    store = ConversationStore(tmp_path / 'cache.db')
    monkeypatch.setattr('core.request_middleware.conversation_store', store)
    monkeypatch.setattr(api_routes, 'CONFIG', {'api_key': 'do-not-store-this-secret'})
    monkeypatch.setattr(api_routes.api_key_manager, 'has_keys', lambda: False)
    app = FastAPI()
    app.add_middleware(GatewayRequestMiddleware)
    @app.post('/v1/chat/completions')
    async def route(request: Request):
        api_routes._validate_request_api_key(request, 'demo')
        context = current_request.get()
        context.model = 'demo'
        context.endpoint = {'api_type': 'direct_api', 'api_base_url': 'https://example.test'}
        context.request_body = await request.json()
        return {'id': 'reply', 'choices': [{'message': {'content': 'hello'}}]}
    client = TestClient(app)
    assert client.post('/v1/chat/completions', json={'model': 'demo'}).status_code == 401
    assert not store.path.exists()
    response = client.post('/v1/chat/completions', json={'model': 'demo'}, headers={
        'Authorization': 'Bearer do-not-store-this-secret', 'X-Bridge-Session-ID': 'conversation'})
    assert response.status_code == 200
    assert response.headers['x-bridge-session-id'] == 'conversation'
    saved = store.find_response('admin', 'reply')
    assert saved is not None
    assert 'do-not-store-this-secret' not in json.dumps(saved['payload'])
    assert json.loads(gzip.decompress(base64.b64decode(saved['payload']['wire_gzip_base64'])))['id'] == 'reply'
    assert store.find_response('guest', 'reply') is None
    assert current_request.get() is None
