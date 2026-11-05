"""原生入口的协议矩阵：客户端和上游说同一种协议，网关只换模型和密钥，不改内容。"""
import json

import pytest

from protocol_harness import MODEL, Reply, Turn, build_gateway, exchange, gemini_reply, interactions_reply

STREAMS = (False, True)
INTERACTIONS_PATH = '/v1beta/interactions'


@pytest.fixture
def gateway(monkeypatch):
    app, monitor = build_gateway(monkeypatch)
    return app, monitor, monkeypatch


def _events(text):
    return [json.loads(line[6:]) for line in text.replace('\r\n', '\n').split('\n')
            if line.startswith('data: ') and line[6:].strip() != '[DONE]']


def _body(stream, **extra):
    return {'model': MODEL, 'input': [{'type': 'user_input', 'content': [{'type': 'text', 'text': 'q'}]}],
            'stream': stream, **extra}


@pytest.mark.parametrize('stream', STREAMS)
def test_interactions_request_and_response_pass_through_untouched(gateway, stream):
    app, _, monkeypatch = gateway
    reply = interactions_reply(Turn(reasoning='想', text='答'), stream)
    body = _body(stream, store=True, previous_interaction_id='int_prev', generation_config={'temperature': 0.2})
    response, fake = exchange(app, monkeypatch, 'interactions', [reply], INTERACTIONS_PATH, body,
                              headers={'x-goog-api-key': 'client-key'})
    assert response.status_code == 200
    sent = fake.requests[0]
    assert sent['path'] == '/v1beta/interactions'
    assert sent['body'] == {**body, 'model': 'gemini-3.8-flash'}
    assert sent['headers']['x-goog-api-key'] == 'upstream-secret'
    if stream:
        assert _events(response.text) == reply.events
    else:
        assert response.json() == reply.body


@pytest.mark.parametrize('stream', STREAMS)
def test_native_client_drives_its_own_continuation(gateway, stream):
    app, _, monkeypatch = gateway
    reply = interactions_reply(Turn(text='一段'), stream, status='requires_continuation', token='token-1')
    response, fake = exchange(app, monkeypatch, 'interactions', [reply], INTERACTIONS_PATH,
                              _body(stream, continuation_token='token-0'))
    assert len(fake.requests) == 1
    assert fake.requests[0]['body']['continuation_token'] == 'token-0'
    assert 'token-1' in response.text


@pytest.mark.parametrize('stream', STREAMS)
def test_interactions_accounting_counts_tool_prompts_and_thoughts(gateway, stream):
    app, monitor, monkeypatch = gateway
    usage = {'total_input_tokens': 100, 'total_tool_use_tokens': 20, 'total_output_tokens': 30,
             'total_thought_tokens': 10, 'total_tokens': 160}
    completed = {'id': 'int_up', 'status': 'completed', 'usage': usage,
                 'steps': [{'type': 'model_output', 'content': [{'type': 'text', 'text': '答'}]}]}
    if stream:
        reply = Reply(events=[{'event_type': 'step.start', 'index': 0, 'step': {'type': 'model_output'}},
                              {'event_type': 'step.delta', 'index': 0, 'delta': {'type': 'text', 'text': '答'}},
                              {'event_type': 'step.stop', 'index': 0},
                              {'event_type': 'interaction.completed', 'interaction': completed}])
    else:
        reply = Reply(body=completed)
    exchange(app, monkeypatch, 'interactions', [reply], INTERACTIONS_PATH, _body(stream))
    ended = monitor.request_end.call_args.kwargs
    assert ended['success'] is True
    assert (ended['input_tokens'], ended['output_tokens']) == (120, 40)
    assert ended['response_content'] == '答'


@pytest.mark.parametrize('stream', STREAMS)
def test_failed_interaction_is_recorded_but_left_for_the_client_to_read(gateway, stream):
    app, monitor, monkeypatch = gateway
    reply = interactions_reply(Turn(text='半截'), stream, status='failed', errors=[{'code': 'internal', 'message': '炸了'}])
    response, _ = exchange(app, monkeypatch, 'interactions', [reply], INTERACTIONS_PATH, _body(stream))
    assert '"failed"' in response.text and '炸了' in response.text
    ended = monitor.request_end.call_args.kwargs
    assert ended['success'] is False and '炸了' in ended['error']


@pytest.mark.parametrize('stream', STREAMS)
def test_interactions_rate_limit_keeps_status_and_retry_after(gateway, stream):
    app, monitor, monkeypatch = gateway
    reply = Reply(status=429, body={'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED', 'message': '请求太频繁'}},
                  headers={'Retry-After': '9'})
    response, _ = exchange(app, monkeypatch, 'interactions', [reply], INTERACTIONS_PATH, _body(stream))
    assert response.status_code == 429 and response.headers.get('retry-after') == '9'
    assert '请求太频繁' in response.text
    assert monitor.request_end.call_args.kwargs['success'] is False


def test_endpoint_without_interactions_protocol_is_refused(gateway):
    app, _, monkeypatch = gateway
    response, fake = exchange(app, monkeypatch, 'gemini', [gemini_reply(Turn(text='好'), False)],
                              INTERACTIONS_PATH, _body(False))
    assert response.status_code == 400 and fake.requests == []


@pytest.mark.parametrize('stream', STREAMS)
def test_generate_content_native_passes_through_untouched(gateway, stream):
    app, monitor, monkeypatch = gateway
    reply = gemini_reply(Turn(text='答', input_tokens=50, output_tokens=7), stream)
    path = f'/v1beta/models/{MODEL}:' + ('streamGenerateContent' if stream else 'generateContent')
    body = {'contents': [{'role': 'user', 'parts': [{'text': 'q'}]}], 'generationConfig': {'temperature': 0.2},
            'continuationToken': 'token-0'}
    response, fake = exchange(app, monkeypatch, 'gemini', [reply], path, body)
    assert response.status_code == 200
    sent = fake.requests[0]
    assert sent['path'] == '/v1beta/models/gemini-3.8-flash:' + ('streamGenerateContent' if stream else 'generateContent')
    assert sent['body'] == body
    if stream:
        assert _events(response.text) == reply.events
    else:
        assert response.json() == reply.body
    ended = monitor.request_end.call_args.kwargs
    assert ended['success'] is True and (ended['input_tokens'], ended['output_tokens']) == (50, 7)
