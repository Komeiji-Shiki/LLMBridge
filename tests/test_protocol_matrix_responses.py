"""回答方向的协议矩阵：同一轮回答走任意“客户端 × 上游”组合，客户端看到的语义都要一致。"""
import json

import pytest

from protocol_harness import (
    CLIENT_PATHS, CLIENTS, MODEL, READERS, REPLY_BUILDERS, UPSTREAMS, Reply, Turn, build_gateway, exchange,
    gemini_partial_args_reply, interactions_reply,
)

STREAMS = (False, True)
CELLS = [(client, upstream, stream) for client in CLIENTS for upstream in UPSTREAMS for stream in STREAMS]


def cell_id(cell):
    return f'{cell[0]}-{cell[1]}-{"stream" if cell[2] else "json"}'


@pytest.fixture
def gateway(monkeypatch):
    app, monitor = build_gateway(monkeypatch)
    return app, monitor, monkeypatch


def request_for(client, stream):
    if client == 'chat':
        return {'model': MODEL, 'stream': stream, 'messages': [{'role': 'user', 'content': 'q'}]}
    if client == 'anthropic':
        return {'model': MODEL, 'stream': stream, 'max_tokens': 100, 'messages': [{'role': 'user', 'content': 'q'}]}
    return {'model': MODEL, 'stream': stream, 'input': 'q'}


def run(gateway, cell, turn, overrides=None):
    client, upstream, stream = cell
    app, _, monkeypatch = gateway
    reply = REPLY_BUILDERS[upstream](turn, stream)
    response, fake = exchange(app, monkeypatch, upstream, [reply], CLIENT_PATHS[client], request_for(client, stream), overrides)
    return READERS[client](response, stream), response, fake


@pytest.mark.parametrize('cell', CELLS, ids=cell_id)
def test_plain_answer_reaches_every_client(gateway, cell):
    seen, response, _ = run(gateway, cell, Turn(text='你好世界'))
    assert response.status_code == 200, response.text
    assert seen.error is None
    assert (seen.text, seen.stop, seen.calls) == ('你好世界', 'end', [])


@pytest.mark.parametrize('cell', CELLS, ids=cell_id)
def test_usage_reaches_every_client(gateway, cell):
    seen, _, _ = run(gateway, cell, Turn(text='你好', input_tokens=321, output_tokens=45))
    assert (seen.input_tokens, seen.output_tokens) == (321, 45)


@pytest.mark.parametrize('cell', CELLS, ids=cell_id)
def test_parallel_tool_calls_stay_separate_and_stop_as_tool(gateway, cell):
    calls = [('call_a', 'first', {'x': 1}), ('call_b', 'second', {'y': 2})]
    seen, _, _ = run(gateway, cell, Turn(text='查一下', calls=calls, stop='tool'))
    assert seen.error is None
    assert seen.text == '查一下'
    assert seen.calls == calls
    assert seen.stop == 'tool'


@pytest.mark.parametrize('client', CLIENTS)
def test_vertex_argument_fragments_reach_clients_as_whole_calls(gateway, client):
    calls = [('call_a', 'write', {'path': 'poem.txt', 'lines': 12}), ('call_b', 'flag', {'on': True, 'note': None})]
    app, _, monkeypatch = gateway
    reply = gemini_partial_args_reply(Turn(text='写文件', calls=calls, stop='tool'))
    response, _ = exchange(app, monkeypatch, 'gemini', [reply], CLIENT_PATHS[client], request_for(client, True))
    seen = READERS[client](response, True)
    assert seen.error is None
    assert (seen.text, seen.calls, seen.stop) == ('写文件', calls, 'tool')


@pytest.mark.parametrize('cell', CELLS, ids=cell_id)
def test_truncated_answer_reports_length(gateway, cell):
    seen, _, _ = run(gateway, cell, Turn(text='半截', stop='length'))
    assert seen.error is None
    assert (seen.text, seen.stop) == ('半截', 'length')


# Gemini 的安全拦截走错误通道，见下面单独的用例。
REFUSAL_CELLS = [cell for cell in CELLS if cell[1] in ('chat', 'anthropic', 'responses')]


@pytest.mark.parametrize('cell', REFUSAL_CELLS, ids=cell_id)
def test_refusal_is_never_a_blank_success(gateway, cell):
    seen, _, _ = run(gateway, cell, Turn(text='不能回答', stop='refusal'))
    assert seen.error is None
    assert '不能回答' in seen.text
    # Anthropic 客户端收到 refusal 会判整个会话违规，转换出来的回答有内容时按正常结束交付。
    converted_for_anthropic = cell[0] == 'anthropic' and cell[1] != 'anthropic'
    assert seen.stop == ('end' if converted_for_anthropic else 'refusal')


FILTERED_CHAT_CELLS = [('anthropic', 'chat', stream) for stream in STREAMS]


@pytest.mark.parametrize('cell', FILTERED_CHAT_CELLS, ids=cell_id)
def test_anthropic_client_gets_refusal_only_when_nothing_survives(gateway, cell):
    seen, _, _ = run(gateway, cell, Turn(text='', stop='refusal'))
    assert seen.error is None
    assert seen.stop == 'refusal'


@pytest.mark.parametrize('cell', FILTERED_CHAT_CELLS, ids=cell_id)
def test_filtered_tool_call_still_reaches_anthropic_client(gateway, cell):
    seen, _, _ = run(gateway, cell, Turn(calls=[('call_1', 'lookup', {'q': 1})], stop='refusal'))
    assert seen.error is None
    assert seen.stop == 'tool'
    assert seen.calls == [('call_1', 'lookup', {'q': 1})]


@pytest.mark.parametrize('cell', [('chat', 'chat', stream) for stream in STREAMS] + FILTERED_CHAT_CELLS, ids=cell_id)
def test_chat_upstream_filter_reaches_monitor(gateway, cell):
    _, monitor, _ = gateway
    run(gateway, cell, Turn(text='半截', stop='refusal'))
    ended = monitor.request_end.call_args.kwargs
    assert ended['success'] is True
    assert ended['response_message']['finish_reason'] == 'content_filter'


@pytest.mark.parametrize('cell', [cell for cell in CELLS if cell[1] == 'gemini'], ids=cell_id)
def test_gemini_safety_block_is_an_error_not_a_normal_stop(gateway, cell):
    seen, _, _ = run(gateway, cell, Turn(text='', stop='refusal'))
    assert seen.error is not None or seen.stop == 'refusal'
    assert seen.stop != 'end'


# 思考的表达力因上游而异：Chat 上游的 reasoning_content 不是每个客户端都有位置。
@pytest.mark.parametrize('cell', CELLS, ids=cell_id)
def test_reasoning_stays_apart_from_the_answer(gateway, cell):
    seen, _, _ = run(gateway, cell, Turn(reasoning='想一想', text='答案'))
    assert seen.error is None
    assert seen.text == '答案'
    assert seen.reasoning == '想一想'


# ---- 上游报错 ----

def _rate_limit_error(upstream):
    message = '请求太频繁'
    if upstream == 'anthropic':
        return {'type': 'error', 'error': {'type': 'rate_limit_error', 'message': message}}
    if upstream in ('gemini', 'interactions'):
        return {'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED', 'message': message}}
    return {'error': {'type': 'rate_limit_error', 'code': 'rate_limit_exceeded', 'message': message}}


@pytest.mark.parametrize('cell', CELLS, ids=cell_id)
def test_rate_limit_keeps_status_message_and_retry_hints(gateway, cell):
    client, upstream, stream = cell
    app, _, monkeypatch = gateway
    reply = Reply(status=429, body=_rate_limit_error(upstream),
                  headers={'Retry-After': '7', 'x-should-retry': 'true', 'anthropic-ratelimit-unified-status': 'rejected'})
    response, _ = exchange(app, monkeypatch, upstream, [reply], CLIENT_PATHS[client], request_for(client, stream))
    seen = READERS[client](response, stream)
    assert seen.error is not None and '请求太频繁' in seen.error
    assert response.headers.get('retry-after') == '7'
    assert response.headers.get('x-should-retry') == 'true'
    assert response.headers.get('anthropic-ratelimit-unified-status') == 'rejected'
    # 上游立刻报错时响应还没开始，流式请求也要拿到真实状态码
    assert response.status_code == 429


@pytest.mark.parametrize('upstream', ['gemini', 'interactions', 'chat', 'responses'])
@pytest.mark.parametrize('stream', STREAMS)
def test_overflow_error_triggers_anthropic_client_recovery(gateway, upstream, stream):
    app, _, monkeypatch = gateway
    wording = 'The input token count (2000000) exceeds the maximum number of tokens allowed (1048576).'
    reply = Reply(status=400, body={'error': {'code': 400, 'status': 'INVALID_ARGUMENT', 'message': wording}})
    response, _ = exchange(app, monkeypatch, upstream, [reply], CLIENT_PATHS['anthropic'], request_for('anthropic', stream))
    assert response.status_code == 400
    error = response.json()['error']
    assert error['type'] == 'invalid_request_error' and error['message'].startswith('prompt is too long')


@pytest.mark.parametrize('stream', STREAMS)
@pytest.mark.parametrize('status, kind', [(429, 'rate_limit_error'), (529, 'overloaded_error')])
def test_anthropic_passthrough_keeps_the_upstream_status(gateway, stream, status, kind):
    """Claude Code 按状态码决定是否重试，这条路径此前会把它们全变成 502 或 200。"""
    app, _, monkeypatch = gateway
    reply = Reply(status=status, body={'type': 'error', 'error': {'type': kind, 'message': '上游原话'}})
    response, _ = exchange(app, monkeypatch, 'anthropic', [reply], CLIENT_PATHS['anthropic'], request_for('anthropic', stream))
    assert response.status_code == status
    assert response.json()['error'] == {'type': kind, 'message': '上游原话'}


@pytest.mark.parametrize('stream', STREAMS)
def test_anthropic_passthrough_reports_upstream_auth_failure_as_502(gateway, stream):
    """上游 401 是网关凭据的问题；原样返回 401 时 Claude Code 会当作自己的 Key 失效，直接放弃重试。"""
    app, _, monkeypatch = gateway
    reply = Reply(status=401, body={'type': 'error', 'error': {'type': 'authentication_error', 'message': '上游原话'}})
    response, _ = exchange(app, monkeypatch, 'anthropic', [reply], CLIENT_PATHS['anthropic'], request_for('anthropic', stream))
    assert response.status_code == 502
    error = response.json()['error']
    assert error['type'] == 'api_error' and '上游原话' in error['message']


def test_streaming_error_after_the_wait_window_still_reaches_the_client(gateway):
    """上游超过等待窗口才报错时响应已经开始，只能走流内错误。"""
    from routes import _direct_api_utils
    from unittest.mock import patch
    app, _, monkeypatch = gateway
    original = _direct_api_utils.peek_stream

    async def no_wait(iterator, interval, wait=0.001):
        return await original(iterator, interval, wait=0.001)

    reply = Reply(status=429, body=_rate_limit_error('gemini'))
    with patch('routes._direct_api_gemini.peek_stream', no_wait):
        response, _ = exchange(app, monkeypatch, 'gemini', [reply], CLIENT_PATHS['chat'], request_for('chat', True))
    seen = READERS['chat'](response, True)
    assert seen.error is not None and '请求太频繁' in seen.error


@pytest.mark.parametrize('client, upstream', [
    ('responses', 'gemini'), ('responses', 'interactions'), ('responses', 'chat'), ('responses', 'anthropic'),
    ('chat', 'gemini'), ('chat', 'interactions'), ('chat', 'responses'), ('chat', 'anthropic'),
])
@pytest.mark.parametrize('stream', STREAMS)
def test_overflow_error_is_translated_for_openai_style_clients(gateway, client, upstream, stream):
    app, _, monkeypatch = gateway
    wording = 'The input token count (2000000) exceeds the maximum number of tokens allowed (1048576).'
    reply = Reply(status=400, body={'error': {'code': 400, 'status': 'INVALID_ARGUMENT', 'message': wording}})
    response, _ = exchange(app, monkeypatch, upstream, [reply], CLIENT_PATHS[client], request_for(client, stream))
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'context_length_exceeded'


# ---- 流中途失败 ----

def _failing_stream(upstream):
    """先给出一段正文，再失败。"""
    if upstream == 'chat':
        return [{'id': 'x', 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'content': '半截'}, 'finish_reason': None}]},
                {'error': {'message': '上游炸了', 'code': 500}}]
    if upstream == 'anthropic':
        return [('message_start', {'type': 'message_start', 'message': {'id': 'm', 'type': 'message', 'role': 'assistant', 'content': [], 'usage': {'input_tokens': 1, 'output_tokens': 0}}}),
                ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}}),
                ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': '半截'}}),
                ('error', {'type': 'error', 'error': {'type': 'api_error', 'message': '上游炸了'}})]
    if upstream == 'responses':
        return [('response.created', {'type': 'response.created', 'response': {'id': 'r', 'status': 'in_progress', 'output': []}}),
                ('response.output_text.delta', {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0, 'delta': '半截'}),
                ('response.failed', {'type': 'response.failed', 'response': {'id': 'r', 'status': 'failed', 'output': [], 'error': {'code': 'server_error', 'message': '上游炸了'}}})]
    if upstream == 'gemini':
        return [{'candidates': [{'content': {'role': 'model', 'parts': [{'text': '半截'}]}}]},
                {'error': {'code': 500, 'message': '上游炸了'}}]
    return [{'event_type': 'step.start', 'index': 0, 'step': {'type': 'model_output'}},
            {'event_type': 'step.delta', 'index': 0, 'delta': {'type': 'text', 'text': '半截'}},
            {'event_type': 'step.stop', 'index': 0},
            {'event_type': 'interaction.completed', 'interaction': {'status': 'failed', 'errors': [{'code': 'internal', 'message': '上游炸了'}]}}]


@pytest.mark.parametrize('client', CLIENTS)
@pytest.mark.parametrize('upstream', UPSTREAMS)
def test_failure_after_output_is_never_disguised_as_success(gateway, client, upstream):
    app, _, monkeypatch = gateway
    response, _ = exchange(app, monkeypatch, upstream, [Reply(events=_failing_stream(upstream))],
                           CLIENT_PATHS[client], request_for(client, True))
    seen = READERS[client](response, True)
    assert seen.stop != 'end'
    assert '上游炸了' in response.text
    assert seen.error is not None


@pytest.mark.parametrize('client', CLIENTS)
@pytest.mark.parametrize('stream', STREAMS)
def test_failed_interaction_reaches_every_client_as_an_error(gateway, client, stream):
    app, _, monkeypatch = gateway
    reply = interactions_reply(Turn(text='半截'), stream, status='failed', errors=[{'code': 'internal', 'message': '上游炸了'}])
    response, _ = exchange(app, monkeypatch, 'interactions', [reply], CLIENT_PATHS[client], request_for(client, stream))
    seen = READERS[client](response, stream)
    assert seen.error is not None and '上游炸了' in seen.error
    assert seen.stop != 'end'


# ---- 长输出续传：端到端 ----

@pytest.mark.parametrize('client', CLIENTS)
@pytest.mark.parametrize('stream', STREAMS)
def test_long_decode_hops_read_as_one_answer(gateway, client, stream):
    app, monitor, monkeypatch = gateway
    replies = [interactions_reply(Turn(text='第一跳', input_tokens=100, output_tokens=50), stream,
                                  status='requires_continuation', token='token-1'),
               interactions_reply(Turn(text='第二跳', input_tokens=160, output_tokens=30), stream)]
    response, fake = exchange(app, monkeypatch, 'interactions', replies, CLIENT_PATHS[client], request_for(client, stream),
                              {'pricing': {'input': 1, 'output': 1, 'currency': 'USD'}})
    seen = READERS[client](response, stream)
    assert seen.error is None
    assert (seen.text, seen.stop) == ('第一跳第二跳', 'end')
    assert (seen.input_tokens, seen.output_tokens) == (100, 80)
    assert [request['body'].get('continuation_token') for request in fake.requests] == [None, 'token-1']
    assert fake.requests[1]['body']['input'] == fake.requests[0]['body']['input']


@pytest.mark.parametrize('stream', STREAMS)
def test_long_decode_cost_uses_every_billed_hop(gateway, stream):
    app, monitor, monkeypatch = gateway
    replies = [interactions_reply(Turn(text='一', input_tokens=100, output_tokens=50), stream,
                                  status='requires_continuation', token='t1'),
               interactions_reply(Turn(text='二', input_tokens=160, output_tokens=30), stream)]
    exchange(app, monkeypatch, 'interactions', replies, CLIENT_PATHS['chat'], request_for('chat', stream))
    ended = monitor.request_end.call_args.kwargs
    assert ended['input_tokens'] == 100 and ended['output_tokens'] == 80
    assert ended['upstream_usage']['billedPromptTokenCount'] == 260


def test_long_decode_stream_keeps_failure_accounting(gateway):
    app, monitor, monkeypatch = gateway
    replies = [interactions_reply(Turn(text='已输出', input_tokens=100, output_tokens=50), True,
                                  status='requires_continuation', token='t1'),
               Reply(status=503, body={'error': {'code': 503, 'message': '过载'}})]
    response, _ = exchange(app, monkeypatch, 'interactions', replies, CLIENT_PATHS['chat'], request_for('chat', True))
    seen = READERS['chat'](response, True)
    assert seen.text == '已输出' and '过载' in seen.error
    ended = monitor.request_end.call_args.kwargs
    assert ended['success'] is False
    assert ended['input_tokens'] == 100 and ended['output_tokens'] == 50
    assert ended['upstream_usage']['billedPromptTokenCount'] == 100


# ---- generateContent 客户端 ----

@pytest.mark.parametrize('stream', STREAMS)
def test_generate_content_client_drives_its_own_continuation_over_interactions(gateway, stream):
    app, _, monkeypatch = gateway
    path = f'/v1beta/models/{MODEL}:' + ('streamGenerateContent' if stream else 'generateContent')
    reply = interactions_reply(Turn(text='一段'), stream, status='requires_continuation', token='token-1')
    response, fake = exchange(app, monkeypatch, 'interactions', [reply], path,
                              {'contents': [{'role': 'user', 'parts': [{'text': 'q'}]}], 'continuationToken': 'token-0'})
    assert response.status_code == 200
    assert fake.requests[0]['body']['continuation_token'] == 'token-0'
    assert '"CONTINUATION"' in response.text.replace(' ', '') or 'CONTINUATION' in response.text
    assert 'token-1' in response.text
    assert len(fake.requests) == 1


def test_warnings_header_reports_what_was_lost(gateway):
    app, _, monkeypatch = gateway
    body = {**request_for('chat', False), 'temperature': 0.5}
    response, fake = exchange(app, monkeypatch, 'interactions',
                              [REPLY_BUILDERS['interactions'](Turn(text='好'), False)], CLIENT_PATHS['chat'], body)
    assert 'temperature' not in json.dumps(fake.requests[0]['body'])
    assert 'temperature' in json.loads(response.headers['x-bridge-warnings'])[0]['field']
