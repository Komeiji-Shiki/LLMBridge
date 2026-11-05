"""上游保存的对象：按创建时的线路和密钥查询、取消、删除和续接，后台任务结束时补记用量。"""
import json

import pytest

from core.request_context import credential_identity
from protocol_harness import MODEL, Reply, Turn, build_gateway, exchange_many, interactions_reply

KEYS = ['key-one', 'key-two', 'key-three']


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    from core.conversation_store import ConversationStore
    monkeypatch.setattr('core.conversation_store.conversation_store', ConversationStore(tmp_path / 'state.db'))
    app, monitor = build_gateway(monkeypatch)
    return app, monitor, monkeypatch


def run(gateway, upstream, replies, calls, **overrides):
    app, _, monkeypatch = gateway
    return exchange_many(app, monkeypatch, upstream, replies, calls, {'api_keys': KEYS, **overrides})


def used_key(request, upstream):
    headers = {name.lower(): value for name, value in request['headers'].items()}
    return headers['x-goog-api-key'] if upstream == 'interactions' else headers['authorization'].removeprefix('Bearer ')


def interaction(object_id, status='completed', usage=None, text='答'):
    return {'id': object_id, 'status': status, 'usage': usage or {},
            'steps': [{'type': 'model_output', 'content': [{'type': 'text', 'text': text}]}] if text else []}


def response_object(object_id, status='completed', usage=None, text='答'):
    output = [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}] if text else []
    return {'id': object_id, 'object': 'response', 'status': status, 'output': output, 'usage': usage}


CREATE = {
    'interactions': ('/v1beta/interactions', lambda **extra: {'model': MODEL, 'input': 'q', **extra}),
    'responses': ('/v1/responses', lambda **extra: {'model': MODEL, 'input': 'q', **extra}),
}
OBJECT_PATH = {'interactions': '/v1beta/interactions/{}', 'responses': '/v1/responses/{}'}
PREVIOUS = {'interactions': 'previous_interaction_id', 'responses': 'previous_response_id'}


def created(upstream, object_id, status='completed', usage=None):
    if upstream == 'interactions':
        return Reply(body=interaction(object_id, status, usage))
    return Reply(body=response_object(object_id, status, usage))


@pytest.mark.parametrize('upstream', ['interactions', 'responses'])
def test_follow_up_requests_reuse_the_creating_key(gateway, upstream):
    path, body = CREATE[upstream]
    object_path = OBJECT_PATH[upstream].format('obj_1')
    calls = [('POST', path, body(), None), ('POST', path, body(**{PREVIOUS[upstream]: 'obj_1'}), None),
             ('POST', path, body(**{PREVIOUS[upstream]: 'obj_1'}), None),
             ('GET', object_path, None, None), ('POST', object_path + '/cancel', None, None),
             ('DELETE', object_path, None, None)]
    replies = [created(upstream, 'obj_1'), created(upstream, 'obj_2'), created(upstream, 'obj_3'),
               created(upstream, 'obj_1'), created(upstream, 'obj_1', 'cancelled'), Reply(body={'deleted': True})]
    responses, fake = run(gateway, upstream, replies, calls)
    assert [response.status_code for response in responses] == [200] * 6
    keys = [used_key(request, upstream) for request in fake.requests]
    assert len(set(keys)) == 1, keys
    upstream_paths = [(request['method'], request['path']) for request in fake.requests[3:]]
    base = '/v1beta/interactions/obj_1' if upstream == 'interactions' else '/v1/responses/obj_1'
    assert upstream_paths == [('GET', base), ('POST', base + '/cancel'), ('DELETE', base)]
    assert all(not request['body'] for request in fake.requests[3:])


@pytest.mark.parametrize('upstream', ['interactions', 'responses'])
def test_without_a_previous_id_keys_still_rotate(gateway, upstream):
    path, body = CREATE[upstream]
    responses, fake = run(gateway, upstream, [created(upstream, 'a'), created(upstream, 'b')],
                          [('POST', path, body(), None), ('POST', path, body(), None)])
    assert len({used_key(request, upstream) for request in fake.requests}) == 2


@pytest.mark.parametrize('upstream', ['interactions', 'responses'])
def test_deleted_or_unknown_objects_are_not_forwarded(gateway, upstream):
    path, body = CREATE[upstream]
    object_path = OBJECT_PATH[upstream].format('obj_1')
    calls = [('POST', path, body(), None), ('DELETE', object_path, None, None), ('GET', object_path, None, None),
             ('GET', OBJECT_PATH[upstream].format('never_seen'), None, None)]
    responses, fake = run(gateway, upstream, [created(upstream, 'obj_1'), Reply(body={'deleted': True})], calls)
    assert [response.status_code for response in responses] == [200, 200, 404, 404]
    assert len(fake.requests) == 2
    assert '找不到' in responses[3].json()['error']['message']


@pytest.mark.parametrize('upstream', ['interactions', 'responses'])
def test_stateless_requests_leave_nothing_to_look_up(gateway, upstream):
    path, body = CREATE[upstream]
    responses, fake = run(gateway, upstream, [created(upstream, 'obj_1')],
                          [('POST', path, body(store=False), None), ('GET', OBJECT_PATH[upstream].format('obj_1'), None, None)])
    assert responses[1].status_code == 404 and len(fake.requests) == 1


def test_interactions_query_is_forwarded_without_the_gateway_key(gateway):
    path, body = CREATE['interactions']
    events = interactions_reply(Turn(text='重连后的内容'), True)
    calls = [('POST', path, body(), None),
             ('GET', '/v1beta/interactions/obj_1?stream=true&last_event_id=evt_9&key=gateway-secret', None, None)]
    responses, fake = run(gateway, 'interactions', [created('interactions', 'obj_1'), events], calls)
    assert fake.requests[1]['query'] == {'stream': 'true', 'last_event_id': 'evt_9'}
    assert '重连后的内容' in responses[1].text
    assert responses[1].headers['content-type'].startswith('text/event-stream')


def test_responses_input_items_and_retrieve_query_pass_through(gateway):
    path, body = CREATE['responses']
    items = {'object': 'list', 'data': [{'type': 'message', 'role': 'user', 'content': 'q'}]}
    calls = [('POST', path, body(), None), ('GET', '/v1/responses/obj_1/input_items?limit=5', None, None),
             ('GET', '/v1/responses/obj_1?include=reasoning.encrypted_content', None, None)]
    responses, fake = run(gateway, 'responses', [created('responses', 'obj_1'), Reply(body=items),
                                                 created('responses', 'obj_1')], calls)
    assert responses[1].json() == items
    assert fake.requests[1]['path'] == '/v1/responses/obj_1/input_items' and fake.requests[1]['query'] == {'limit': '5'}
    assert fake.requests[2]['query'] == {'include': 'reasoning.encrypted_content'}


def test_upstream_errors_on_lookup_keep_their_status(gateway):
    path, body = CREATE['responses']
    error = {'error': {'message': '没有这个对象', 'type': 'invalid_request_error', 'code': 'not_found'}}
    responses, _ = run(gateway, 'responses', [created('responses', 'obj_1'), Reply(status=404, body=error)],
                       [('POST', path, body(), None), ('GET', '/v1/responses/obj_1', None, None)])
    assert responses[1].status_code == 404 and responses[1].json() == error


# ---- 后台任务的用量 ----

USAGE = {
    'interactions': {'total_input_tokens': 300, 'total_output_tokens': 40, 'total_thought_tokens': 10, 'total_tokens': 350},
    'responses': {'input_tokens': 300, 'output_tokens': 50, 'total_tokens': 350},
}


def _ends(monitor):
    return [call.kwargs for call in monitor.request_end.call_args_list]


@pytest.mark.parametrize('upstream', ['interactions', 'responses'])
def test_background_usage_is_recorded_once_when_the_result_arrives(gateway, upstream):
    _, monitor, _ = gateway
    path, body = CREATE[upstream]
    object_path = OBJECT_PATH[upstream].format('bg_1')
    pending = 'in_progress' if upstream == 'interactions' else 'queued'
    calls = [('POST', path, body(background=True), None), ('GET', object_path, None, None),
             ('GET', object_path, None, None), ('GET', object_path, None, None)]
    replies = [created(upstream, 'bg_1', pending), created(upstream, 'bg_1', 'in_progress'),
               created(upstream, 'bg_1', 'completed', USAGE[upstream]), created(upstream, 'bg_1', 'completed', USAGE[upstream])]
    run(gateway, upstream, replies, calls, pricing={'input': 1, 'output': 1, 'currency': 'USD'})
    ends = _ends(monitor)
    # 创建时还没有用量，不能按估算记账；结果到达时补记一次，再轮询也不重复
    assert ends[0]['input_tokens'] == 0 and ends[0]['output_tokens'] == 0
    assert len(ends) == 2
    assert (ends[1]['input_tokens'], ends[1]['output_tokens']) == (300, 50)
    assert ends[1]['success'] is True


def test_interrupted_stream_records_only_the_remaining_usage(gateway):
    _, monitor, _ = gateway
    partial = Reply(events=[
        {'event_type': 'interaction.created', 'interaction': {'id': 'obj_1', 'status': 'in_progress'}},
        {'event_type': 'step.start', 'index': 0, 'step': {'type': 'model_output'}},
        {'event_type': 'step.delta', 'index': 0, 'delta': {'type': 'text', 'text': '断线前的内容'}},
        {'event_type': 'interaction.status_update', 'status': 'in_progress',
         'usage': {'total_input_tokens': 300, 'total_output_tokens': 10}}])
    final = Reply(body=interaction('obj_1', 'completed', USAGE['interactions']))
    calls = [('POST', '/v1beta/interactions', {'model': MODEL, 'input': 'q', 'stream': True}, None),
             ('GET', '/v1beta/interactions/obj_1', None, None)]
    run(gateway, 'interactions', [partial, final], calls)
    ends = _ends(monitor)
    assert (ends[0]['input_tokens'], ends[0]['output_tokens']) == (300, 10)
    assert (ends[1]['input_tokens'], ends[1]['output_tokens']) == (0, 40)


def test_finished_objects_are_not_billed_again(gateway):
    _, monitor, _ = gateway
    path, body = CREATE['responses']
    calls = [('POST', path, body(), None), ('GET', '/v1/responses/obj_1', None, None)]
    replies = [created('responses', 'obj_1', 'completed', USAGE['responses'])] * 2
    run(gateway, 'responses', replies, calls)
    assert len(_ends(monitor)) == 1


def test_concurrent_polls_claim_the_settlement_only_once(tmp_path):
    from core.conversation_store import ConversationStore
    store = ConversationStore(tmp_path / 'state.db')
    store.remember_object('owner', 'responses', 'obj', {'model': 'm'}, settled=False)
    assert [store.claim_settlement('owner', 'responses', 'obj') for _ in range(3)] == [True, False, False]


# ---- 隔离和配置变更 ----

def test_records_are_scoped_to_the_caller_and_never_hold_the_key(tmp_path, monkeypatch):
    from core import config_loader
    from core.conversation_store import ConversationStore
    from services import stored_objects
    store = ConversationStore(tmp_path / 'state.db')
    monkeypatch.setattr('core.conversation_store.conversation_store', store)
    config = {'api_type': 'responses_native', 'api_base_url': 'https://u.test/v1', 'api_keys': KEYS}
    monkeypatch.setitem(config_loader.MODEL_ENDPOINT_MAP, 'alias', config)
    stored_objects.remember('alice', 'responses', 'obj', 'alias', config, 'key-two', (1, 2, 0), 'completed')
    assert stored_objects.resolve('alice', 'responses', 'obj').key == 'key-two'
    assert stored_objects.resolve('bob', 'responses', 'obj') is None
    with store.connection() as connection:
        raw = connection.execute('SELECT payload FROM stored_objects').fetchone()[0]
    assert 'key-two' not in raw and credential_identity('key-two') in raw
    # 密钥从配置里删掉后不能退回到别的密钥
    monkeypatch.setitem(config_loader.MODEL_ENDPOINT_MAP, 'alias', {**config, 'api_keys': ['key-one']})
    assert stored_objects.resolve('alice', 'responses', 'obj') is None


def test_follow_up_for_another_model_uses_normal_routing(gateway):
    path, body = CREATE['interactions']
    app, _, monkeypatch = gateway
    other = {'model': 'other-model', 'input': 'q', 'previous_interaction_id': 'obj_1'}
    from routes import api_routes
    calls = [('POST', path, body(), None), ('POST', path, other, None)]
    monkeypatch.setitem(api_routes.MODEL_ENDPOINT_MAP, 'other-model', {'api_type': 'gemini_native'})
    responses, fake = run(gateway, 'interactions', [created('interactions', 'obj_1')], calls)
    assert responses[1].status_code == 400 and len(fake.requests) == 1
