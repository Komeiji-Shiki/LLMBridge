"""业务事件之前的重试：前导事件之后才报出的限流、过载照样重试，客户端只看到重试成功的结果。

重试在 transport 层（services.request_execution.retry_before_output），这里按协议矩阵防回归。
"""
import json

import pytest

from protocol_harness import CLIENT_PATHS, CLIENTS, MODEL, READERS, REPLY_BUILDERS, Reply, Turn, build_gateway, exchange

RETRY = {'auto_retry': {'enabled': True, 'max_retries': 1, 'retry_delay_seconds': 0}}
TURN = Turn(text='重试之后的回答')

# 每个上游先发前导事件（创建通知、message_start、角色块），再报一个可重试的错误
PREAMBLE_THEN_ERROR = {
    'interactions': [
        {'event_type': 'interaction.created', 'interaction': {'id': 'int_up', 'status': 'in_progress'}},
        {'event_type': 'error', 'error': {'code': 'too_many_requests', 'message': 'Resource exhausted.'}},
    ],
    'anthropic': [
        ('message_start', {'type': 'message_start', 'message': {
            'id': 'msg_up', 'type': 'message', 'role': 'assistant', 'content': [], 'usage': {'input_tokens': 5}}}),
        ('ping', {'type': 'ping'}),
        ('error', {'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}}),
    ],
    'chat': [
        {'id': 'c', 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'role': 'assistant'}}]},
        {'error': {'message': 'Rate limited', 'type': 'rate_limit_error', 'code': 429}},
    ],
    'responses': [
        ('response.created', {'type': 'response.created', 'response': {'id': 'resp_up', 'status': 'in_progress'}}),
        ('error', {'type': 'error', 'code': 'rate_limit_exceeded', 'message': 'Rate limited'}),
    ],
}
CELLS = [(client, upstream) for client in CLIENTS for upstream in PREAMBLE_THEN_ERROR]


@pytest.fixture
def gateway(monkeypatch):
    app, monitor = build_gateway(monkeypatch)
    return app, monitor, monkeypatch


def request_for(client):
    if client == 'chat':
        return {'model': MODEL, 'stream': True, 'messages': [{'role': 'user', 'content': 'q'}]}
    if client == 'anthropic':
        return {'model': MODEL, 'stream': True, 'max_tokens': 100, 'messages': [{'role': 'user', 'content': 'q'}]}
    return {'model': MODEL, 'stream': True, 'input': 'q'}


@pytest.mark.parametrize('cell', CELLS, ids='-'.join)
def test_error_after_preamble_is_retried(gateway, cell):
    client, upstream = cell
    app, _, monkeypatch = gateway
    replies = [Reply(events=PREAMBLE_THEN_ERROR[upstream]), REPLY_BUILDERS[upstream](TURN, True)]
    response, fake = exchange(app, monkeypatch, upstream, replies, CLIENT_PATHS[client], request_for(client), RETRY)
    seen = READERS[client](response, True)
    assert response.status_code == 200 and seen.error is None
    assert seen.text == '重试之后的回答'
    assert len(fake.requests) == 2


def test_without_retry_the_error_reaches_the_client(gateway):
    app, _, monkeypatch = gateway
    replies = [Reply(events=PREAMBLE_THEN_ERROR['interactions']), REPLY_BUILDERS['interactions'](TURN, True)]
    response, fake = exchange(app, monkeypatch, 'interactions', replies, CLIENT_PATHS['chat'], request_for('chat'))
    assert 'Resource exhausted' in json.dumps(READERS['chat'](response, True).error or response.text)
    assert len(fake.requests) == 1
