"""Anthropic 工具名改写：请求侧换别名，响应侧（流式/非流式）恢复原名。"""
import copy
import json

from utils.json_unescape import AnthropicSSEToolArgsRewriter
from utils.tool_name_rewrite import (
    apply_tool_name_rewrite,
    describe_restore_map,
    resolve_tool_name_rewrite_map,
    restore_tool_name,
    restore_tool_names_in_response,
)

ENABLED = {'tool_name_rewrite': True}


def _body():
    return {
        'system': [{'type': 'text', 'text': 'Use read_file before editing. my_read_file stays.',
                    'cache_control': {'type': 'ephemeral'}}],
        'tools': [
            {'name': 'read_file', 'description': 'read_file reads a file', 'input_schema': {'type': 'object'}},
            {'name': 'Bash', 'description': 'run', 'input_schema': {'type': 'object'}},
        ],
        'tool_choice': {'type': 'tool', 'name': 'read_file'},
        'messages': [
            {'role': 'user', 'content': 'open it'},
            {'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': 't1', 'name': 'read_file', 'input': {'path': 'a.py'}},
                {'type': 'tool_use', 'id': 't2', 'name': 'Bash', 'input': {}},
            ]},
        ],
    }


def test_map_requires_explicit_switch_and_rejects_ambiguous_aliases():
    assert resolve_tool_name_rewrite_map({}) == {}
    assert resolve_tool_name_rewrite_map({'tool_name_rewrite': 'true'}) == {}
    assert resolve_tool_name_rewrite_map(ENABLED) == {'read_file': 'read_workspace_file'}
    custom = resolve_tool_name_rewrite_map({**ENABLED, 'tool_name_rewrite_map': {
        'a': 'b', 'b': 'c', 'x': 'bad name', 'y': 'b2', 'z': 'b2'}})
    # b 既是别名又是原名、非法名称、重复别名都被忽略
    assert custom == {'b': 'c', 'y': 'b2'}


def test_request_rewrite_covers_tools_choice_history_and_system():
    original = _body()
    body = copy.deepcopy(original)
    restore = apply_tool_name_rewrite(body, ENABLED)

    assert restore == {'read_workspace_file': 'read_file'}
    assert describe_restore_map(restore) == {'read_file': 'read_workspace_file'}
    assert [tool['name'] for tool in body['tools']] == ['read_workspace_file', 'Bash']
    assert body['tools'][0]['description'] == 'read_workspace_file reads a file'
    assert body['tool_choice'] == {'type': 'tool', 'name': 'read_workspace_file'}
    assert [block['name'] for block in body['messages'][1]['content']] == ['read_workspace_file', 'Bash']
    assert body['system'][0]['text'] == 'Use read_workspace_file before editing. my_read_file stays.'
    assert body['system'][0]['cache_control'] == {'type': 'ephemeral'}


def test_rewrite_does_not_mutate_shared_client_objects():
    client = _body()
    body = dict(client)  # 与透传链路一致：浅拷贝请求体，内部对象仍与客户端共享
    apply_tool_name_rewrite(body, ENABLED)
    assert client == _body()


def test_undeclared_tool_leaves_request_untouched():
    body = {'system': 'def read_file(path):\n    return open(path).read()',
            'tools': [{'name': 'Bash', 'input_schema': {}}],
            'messages': [{'role': 'user', 'content': 'call read_file'}]}
    before = copy.deepcopy(body)
    assert apply_tool_name_rewrite(body, ENABLED) == {}
    assert body == before


def test_alias_colliding_with_client_tool_is_skipped():
    body = {'tools': [{'name': 'read_file'}, {'name': 'read_workspace_file'}], 'messages': []}
    assert apply_tool_name_rewrite(body, ENABLED) == {}
    assert [tool['name'] for tool in body['tools']] == ['read_file', 'read_workspace_file']


def test_non_stream_response_restores_original_names():
    response = {'content': [
        {'type': 'text', 'text': 'ok'},
        {'type': 'tool_use', 'id': 't', 'name': 'read_workspace_file', 'input': {}},
        {'type': 'tool_use', 'id': 'u', 'name': 'Bash', 'input': {}},
    ]}
    assert restore_tool_names_in_response(response, {'read_workspace_file': 'read_file'}) is True
    assert [block.get('name') for block in response['content']] == [None, 'read_file', 'Bash']
    assert restore_tool_names_in_response(response, {}) is False
    assert restore_tool_name('read_workspace_file', {'read_workspace_file': 'read_file'}) == 'read_file'
    assert restore_tool_name('Bash', {'read_workspace_file': 'read_file'}) == 'Bash'
    assert restore_tool_name('read_workspace_file', None) == 'read_workspace_file'


def _event(payload, newline=b'\n'):
    return (b'event: ' + payload['type'].encode() + newline
            + b'data: ' + json.dumps(payload).encode() + newline + newline)



def test_stream_restores_tool_use_name_and_keeps_args_rewriting():
    rewriter = AnthropicSSEToolArgsRewriter(tool_name_restore={'read_workspace_file': 'read_file'})
    start = {'type': 'content_block_start', 'index': 1,
             'content_block': {'type': 'tool_use', 'id': 't', 'name': 'read_workspace_file', 'input': {}}}
    other = {'type': 'content_block_start', 'index': 2,
             'content_block': {'type': 'tool_use', 'id': 'u', 'name': 'Bash', 'input': {}}}
    delta = {'type': 'content_block_delta', 'index': 1,
             'delta': {'type': 'input_json_delta', 'partial_json': '{"path": "\\u4e2d.py"}'}}
    stream = _event(start) + _event(other, b'\r\n') + _event(delta)

    # 任意切割 chunk 也要得到相同结果
    output = b''.join(rewriter.feed(stream[i:i + 7]) for i in range(0, len(stream), 7))
    events = [json.loads(line[5:].strip()) for line in output.split(b'\n') if line.startswith(b'data:')]
    assert events[0]['content_block']['name'] == 'read_file'
    assert events[1]['content_block']['name'] == 'Bash'
    assert events[2]['delta']['partial_json'] == '{"path": "中.py"}'
    # 未改写的 CRLF 事件原样转发
    assert _event(other, b'\r\n') in output


def test_stream_without_restore_map_is_byte_identical():
    start = {'type': 'content_block_start', 'index': 0,
             'content_block': {'type': 'tool_use', 'id': 't', 'name': 'read_workspace_file', 'input': {}}}
    raw = _event(start)
    assert AnthropicSSEToolArgsRewriter().feed(raw) == raw
