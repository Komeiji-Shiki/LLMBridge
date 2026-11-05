"""请求方向的协议矩阵：同一段对话从任意客户端发出，每种上游收到的语义都要一致。"""
import json

import pytest

from protocol_harness import (
    CLIENT_PATHS, CLIENTS, MODEL, READERS, REPLY_BUILDERS, UPSTREAMS, WIRE_READERS, Turn, build_gateway, exchange,
    upstream_body,
)

CELLS = [(client, upstream) for client in CLIENTS for upstream in UPSTREAMS]
SCHEMA = {'type': 'object', 'properties': {'x': {'type': 'number'}}}
PDF = 'JVBERi0xLjQK'


def cell_id(cell):
    return '-'.join(cell)


@pytest.fixture
def gateway(monkeypatch):
    app, _ = build_gateway(monkeypatch)
    return app, monkeypatch


def send(gateway, client, upstream, body, overrides=None, turn=None, stream=False):
    app, monkeypatch = gateway
    reply = REPLY_BUILDERS[upstream](turn or Turn(text='好'), stream)
    response, fake = exchange(app, monkeypatch, upstream, [reply], CLIENT_PATHS[client], {**body, 'stream': stream}, overrides)
    assert response.status_code == 200, response.text
    return response, upstream_body(fake)


# ---- 同一段工具循环对话的三种客户端写法 ----

def tool_loop(client):
    if client == 'chat':
        return {'model': MODEL, 'tools': [{'type': 'function', 'function': {'name': n, 'parameters': SCHEMA}} for n in ('first', 'second')],
                'messages': [{'role': 'system', 'content': '开头'}, {'role': 'user', 'content': '查天气'},
                             {'role': 'system', 'content': '提醒'},
                             {'role': 'assistant', 'content': '我查一下', 'tool_calls': [
                                 {'id': 'call_a', 'type': 'function', 'function': {'name': 'first', 'arguments': '{"x": 1}'}},
                                 {'id': 'call_b', 'type': 'function', 'function': {'name': 'second', 'arguments': '{"y": 2}'}}]},
                             {'role': 'tool', 'tool_call_id': 'call_a', 'content': '晴'},
                             {'role': 'tool', 'tool_call_id': 'call_b', 'content': '失败'},
                             {'role': 'user', 'content': '谢谢'}]}
    if client == 'anthropic':
        return {'model': MODEL, 'max_tokens': 100, 'system': '开头',
                'tools': [{'name': n, 'input_schema': SCHEMA} for n in ('first', 'second')],
                'messages': [{'role': 'user', 'content': '查天气'}, {'role': 'system', 'content': '提醒'},
                             {'role': 'assistant', 'content': [
                                 {'type': 'text', 'text': '我查一下'},
                                 {'type': 'tool_use', 'id': 'call_a', 'name': 'first', 'input': {'x': 1}},
                                 {'type': 'tool_use', 'id': 'call_b', 'name': 'second', 'input': {'y': 2}}]},
                             {'role': 'user', 'content': [
                                 {'type': 'tool_result', 'tool_use_id': 'call_a', 'content': '晴'},
                                 {'type': 'tool_result', 'tool_use_id': 'call_b', 'content': '失败', 'is_error': True},
                                 {'type': 'text', 'text': '谢谢'}]}]}
    return {'model': MODEL, 'instructions': '开头',
            'tools': [{'type': 'function', 'name': n, 'parameters': SCHEMA} for n in ('first', 'second')],
            'input': [{'type': 'message', 'role': 'user', 'content': '查天气'},
                      {'type': 'message', 'role': 'developer', 'content': '提醒'},
                      {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '我查一下'}]},
                      {'type': 'function_call', 'call_id': 'call_a', 'name': 'first', 'arguments': '{"x": 1}'},
                      {'type': 'function_call', 'call_id': 'call_b', 'name': 'second', 'arguments': '{"y": 2}'},
                      {'type': 'function_call_output', 'call_id': 'call_a', 'output': '晴'},
                      {'type': 'function_call_output', 'call_id': 'call_b', 'output': '失败'},
                      {'type': 'message', 'role': 'user', 'content': '谢谢'}]}


def expected_loop(client):
    # Chat 和 Responses 的工具结果没有报错标记，只有 Anthropic 客户端带得过来
    return [('user', 'text', '查天气'), ('reminder', '提醒'), ('assistant', 'text', '我查一下'),
            ('call', 'call_a', 'first', {'x': 1}), ('call', 'call_b', 'second', {'y': 2}),
            ('result', 'call_a', '晴', False), ('result', 'call_b', '失败', client == 'anthropic'),
            ('user', 'text', '谢谢')]


@pytest.mark.parametrize('cell', CELLS, ids=cell_id)
def test_conversation_shape_survives_every_path(gateway, cell):
    """中段提醒留在原位，并行调用紧跟着各自的结果，系统提示只有开头那段。"""
    client, upstream = cell
    _, body = send(gateway, client, upstream, tool_loop(client))
    system, turns = WIRE_READERS[upstream](body)
    assert system == '开头'
    assert [tuple(item) for item in turns] == expected_loop(client)


PRIVATE_KEYS = {'_anthropic_thinking', 'provider_metadata', 'provider_event', '_thought_signature', '_http_status'}


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


@pytest.mark.parametrize('cell', CELLS, ids=cell_id)
def test_private_fields_never_reach_the_upstream(gateway, cell):
    client, upstream = cell
    body = tool_loop(client)
    if client == 'anthropic':
        body = {**body, 'top_k': 5, 'metadata': {'user_id': 'u'}, 'thinking': {'type': 'enabled', 'budget_tokens': 2048}}
    _, wire = send(gateway, client, upstream, body)
    assert not PRIVATE_KEYS & set(_keys(wire))
    if client == 'anthropic' and upstream != 'anthropic':
        assert not {'top_k', 'metadata', 'thinking'} & wire.keys()


# ---- 采样参数和预填充按模型限制处理 ----

STRICT = {'chat': 'gpt-6-astra', 'anthropic': 'claude-opus-4-7', 'responses': 'gpt-6-astra',
          'gemini': 'gemini-3.8-flash', 'interactions': 'gemini-3.8-flash'}
LENIENT = {'chat': 'upstream-chat', 'anthropic': 'claude-sonnet-4-5', 'responses': 'upstream-responses',
           'gemini': 'gemini-2.5-flash-lite'}


def _temperature(upstream, wire):
    if upstream == 'gemini':
        return (wire.get('generationConfig') or {}).get('temperature')
    if upstream == 'interactions':
        return (wire.get('generation_config') or {}).get('temperature')
    return wire.get('temperature')


def _with_temperature(client):
    body = tool_loop(client)
    return {**body, 'temperature': 0.3}


@pytest.mark.parametrize('cell', CELLS, ids=cell_id)
def test_sampling_is_dropped_only_where_the_model_rejects_it(gateway, cell):
    client, upstream = cell
    _, strict = send(gateway, client, upstream, _with_temperature(client), {'model_id': STRICT[upstream]})
    assert _temperature(upstream, strict) is None
    if upstream in LENIENT:
        _, lenient = send(gateway, client, upstream, _with_temperature(client), {'model_id': LENIENT[upstream]})
        assert _temperature(upstream, lenient) == 0.3


def _prefill(client):
    if client == 'chat':
        return {'model': MODEL, 'messages': [{'role': 'user', 'content': '写诗'}, {'role': 'assistant', 'content': '春眠'}]}
    return {'model': MODEL, 'max_tokens': 100, 'messages': [{'role': 'user', 'content': '写诗'},
                                                            {'role': 'assistant', 'content': '春眠'}]}


PREFILL_CELLS = [(client, upstream) for client in ('chat', 'anthropic') for upstream in UPSTREAMS]


@pytest.mark.parametrize('cell', PREFILL_CELLS, ids=cell_id)
def test_trailing_assistant_is_rewritten_only_for_models_that_reject_prefill(gateway, cell):
    client, upstream = cell
    strict_model = {'chat': 'upstream-chat', 'responses': 'upstream-responses'}.get(upstream, STRICT[upstream])
    _, wire = send(gateway, client, upstream, _prefill(client), {'model_id': strict_model})
    _, turns = WIRE_READERS[upstream](wire)
    if upstream in ('anthropic', 'gemini', 'interactions'):
        assert turns[-1][0] == 'user' and '春眠' in turns[-1][2]
    else:
        assert turns[-1] == ('assistant', 'text', '春眠')


# ---- 文件和内置工具 ----

def _with_pdf(client):
    if client == 'chat':
        part = {'type': 'file', 'file': {'file_data': f'data:application/pdf;base64,{PDF}', 'filename': 'a.pdf'}}
        return {'model': MODEL, 'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': '读一下'}, part]}]}
    if client == 'anthropic':
        part = {'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': PDF}}
        return {'model': MODEL, 'max_tokens': 100, 'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': '读一下'}, part]}]}
    part = {'type': 'input_file', 'file_data': f'data:application/pdf;base64,{PDF}', 'filename': 'a.pdf'}
    return {'model': MODEL, 'input': [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '读一下'}, part]}]}


def _holders(value, needle, key=None):
    """直接存放 needle 的字段名。"""
    if isinstance(value, dict):
        for name, item in value.items():
            yield from _holders(item, needle, name)
    elif isinstance(value, list):
        for item in value:
            yield from _holders(item, needle, key)
    elif isinstance(value, str) and needle in value:
        yield key


@pytest.mark.parametrize('cell', CELLS, ids=cell_id)
def test_pdf_arrives_as_a_file_never_as_text(gateway, cell):
    client, upstream = cell
    _, wire = send(gateway, client, upstream, _with_pdf(client))
    holders = list(_holders(wire, PDF))
    assert holders and set(holders) <= {'data', 'file_data'}


def _tool_schemas(upstream, wire):
    tools = wire.get('tools') or []
    if upstream == 'chat':
        return {tool['function']['name']: tool['function'].get('parameters') for tool in tools}
    if upstream == 'gemini':
        return {decl['name']: decl.get('parametersJsonSchema') or decl.get('parameters')
                for tool in tools for decl in tool.get('functionDeclarations') or []}
    return {tool.get('name'): tool.get('parameters') for tool in tools}


@pytest.mark.parametrize('upstream', UPSTREAMS)
def test_anthropic_builtin_tools_keep_a_real_schema(gateway, upstream):
    body = {'model': MODEL, 'max_tokens': 100, 'messages': [{'role': 'user', 'content': 'ls'}],
            'tools': [{'type': 'bash_20250124', 'name': 'bash'}, {'type': 'text_editor_20250728', 'name': 'str_replace_based_edit_tool'}]}
    _, wire = send(gateway, 'anthropic', upstream, body)
    if upstream == 'anthropic':
        assert [tool['type'] for tool in wire['tools']] == ['bash_20250124', 'text_editor_20250728']
        return
    schemas = _tool_schemas(upstream, wire)
    assert 'command' in schemas['bash']['properties']
    assert 'path' in schemas['str_replace_based_edit_tool']['properties']


# ---- Responses 客户端特有的输入项 ----

@pytest.mark.parametrize('upstream', [name for name in UPSTREAMS if name != 'responses'])
def test_responses_history_items_never_become_empty_messages(gateway, upstream):
    body = {'model': MODEL, 'input': [
        {'type': 'reasoning', 'id': 'rs_1', 'summary': [{'type': 'summary_text', 'text': '想过了'}]},
        {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '答'}]},
        {'type': 'compaction', 'id': 'cmp_1', 'encrypted_content': 'opaque'},
        {'type': 'message', 'role': 'user', 'content': 'q'}]}
    _, wire = send(gateway, 'responses', upstream, body)
    _, turns = WIRE_READERS[upstream](wire)
    assert [tuple(item) for item in turns] == [('assistant', 'text', '答'), ('user', 'text', 'q')]


@pytest.mark.parametrize('upstream', [name for name in UPSTREAMS if name != 'responses'])
def test_stored_item_reference_is_refused_instead_of_dropped(gateway, upstream):
    """引用的内容存在 OpenAI 那边，无状态转换取不到，丢掉会让模型少一段上下文。"""
    app, monkeypatch = gateway
    body = {'model': MODEL, 'input': [{'type': 'item_reference', 'id': 'msg_old'},
                                      {'type': 'message', 'role': 'user', 'content': 'q'}]}
    response, fake = exchange(app, monkeypatch, upstream, [REPLY_BUILDERS[upstream](Turn(text='好'), False)],
                              CLIENT_PATHS['responses'], body)
    assert response.status_code == 400 and 'item_reference' in response.json()['error']['message']
    assert fake.requests == []


@pytest.mark.parametrize('upstream', [name for name in UPSTREAMS if name != 'responses'])
@pytest.mark.parametrize('stream', [False, True])
def test_responses_custom_tool_round_trips(gateway, upstream, stream):
    body = {'model': MODEL, 'input': 'q', 'tools': [{'type': 'custom', 'name': 'apply_patch', 'description': '改文件'}]}
    turn = Turn(calls=[('call_p', 'apply_patch', {'input': '*** diff'})], stop='tool')
    response, wire = send(gateway, 'responses', upstream, body, turn=turn, stream=stream)
    assert 'apply_patch' in _tool_schemas(upstream, wire)
    if stream:
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        output = next(event['response'] for event in events if event.get('type') == 'response.completed')['output']
    else:
        output = response.json()['output']
    call = next(item for item in output if item['type'] in ('custom_tool_call', 'function_call'))
    assert call['type'] == 'custom_tool_call' and call['input'] == '*** diff' and call['name'] == 'apply_patch'
