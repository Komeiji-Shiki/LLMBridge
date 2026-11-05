"""token 计数和压缩路由：原生上游转发，其余本地估算。"""
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import token_count_api
from routes.token_count_api import router


class FakeService:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def call_api_passthrough(self, **kwargs):
        self.calls.append(kwargs)
        yield json.dumps(self.payload).encode()


@pytest.fixture
def harness(monkeypatch):
    state = SimpleNamespace(config={}, service=FakeService({}))

    async def select(_model):
        return state.config

    async def fake_key(*_args, **_kwargs):
        return 'key'

    api = token_count_api._chat_api
    monkeypatch.setattr(api, '_select_endpoint_config_for_model', select)
    monkeypatch.setattr(api, '_validate_request_api_key', lambda *args, **kwargs: None)
    monkeypatch.setattr(api, '_check_verification_cooldown', lambda: None)
    monkeypatch.setattr(api, '_app_state', SimpleNamespace(
        update_activity=lambda: None, server=SimpleNamespace(direct_api_service=state.service)))
    monkeypatch.setattr(token_count_api, 'get_api_key', fake_key)
    app = FastAPI()
    app.include_router(router)
    state.client = TestClient(app)
    return state


ANTHROPIC = {'model': 'alias', 'system': '你是助手', 'tools': [{'name': 'f', 'input_schema': {'type': 'object'}}],
             'messages': [{'role': 'user', 'content': '你好，世界'}]}
RESPONSES = {'model': 'alias', 'input': '你好，世界'}


def test_anthropic_native_forwards_with_upstream_model(harness):
    harness.config = {'api_type': 'anthropic_native', 'model_id': 'real', 'api_base_url': 'https://u.test/v1'}
    harness.service.payload = {'input_tokens': 42}
    response = harness.client.post('/v1/messages/count_tokens', json={**ANTHROPIC, 'stream': True})
    assert response.status_code == 200 and response.json() == {'input_tokens': 42}
    call = harness.service.calls[0]
    assert call['endpoint_path'] == '/messages/count_tokens' and call['stream_override'] is False
    assert call['request_body']['model'] == 'real' and 'stream' not in call['request_body']


def test_anthropic_falls_back_to_local_when_relay_lacks_the_route(harness):
    harness.config = {'api_type': 'anthropic_native', 'api_base_url': 'https://u.test/v1'}
    harness.service.payload = {'error': {'message': 'not found', 'code': 404}}
    response = harness.client.post('/v1/messages/count_tokens', json=ANTHROPIC)
    assert response.status_code == 200 and response.json()['input_tokens'] > 0


def test_anthropic_real_upstream_errors_are_not_hidden(harness):
    harness.config = {'api_type': 'anthropic_native', 'api_base_url': 'https://u.test/v1'}
    harness.service.payload = {'error': {'message': 'denied', 'code': 401}}
    assert harness.client.post('/v1/messages/count_tokens', json=ANTHROPIC).status_code == 401


def test_other_upstreams_estimate_locally_and_tools_count(harness):
    harness.config = {'api_type': 'openai'}
    bare = harness.client.post('/v1/messages/count_tokens', json={**ANTHROPIC, 'tools': []}).json()['input_tokens']
    with_tools = harness.client.post('/v1/messages/count_tokens', json=ANTHROPIC).json()['input_tokens']
    assert with_tools > bare > 0
    assert not harness.service.calls


def test_responses_input_tokens_forward_and_estimate(harness):
    harness.config = {'api_type': 'responses_native', 'api_base_url': 'https://u.test/v1'}
    harness.service.payload = {'object': 'response.input_tokens', 'input_tokens': 7}
    assert harness.client.post('/v1/responses/input_tokens', json=RESPONSES).json()['input_tokens'] == 7
    assert harness.service.calls[0]['endpoint_path'] == '/responses/input_tokens'
    harness.config = {'api_type': 'openai'}
    local = harness.client.post('/v1/responses/input_tokens', json=RESPONSES).json()
    assert local['object'] == 'response.input_tokens' and local['input_tokens'] > 0


def test_compact_only_works_on_responses_upstream(harness):
    harness.config = {'api_type': 'responses_native', 'api_base_url': 'https://u.test/v1'}
    harness.service.payload = {'object': 'response.compaction', 'output': []}
    assert harness.client.post('/v1/responses/compact', json=RESPONSES).status_code == 200
    assert harness.service.calls[0]['endpoint_path'] == '/responses/compact'
    harness.config = {'api_type': 'gemini_native'}
    refused = harness.client.post('/v1/responses/compact', json=RESPONSES)
    assert refused.status_code == 400 and '服务端压缩' in refused.json()['error']['message']


def test_missing_model_is_a_client_error(harness):
    assert harness.client.post('/v1/messages/count_tokens', json={'messages': []}).status_code == 400
