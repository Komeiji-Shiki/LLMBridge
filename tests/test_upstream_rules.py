"""上游请求改写规则：协议转换之后、发送之前按端点配置改写真正发往上游的请求体。"""
import asyncio
import copy
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from core.request_context import RequestContext, current_request
from services.direct_api_service import DirectAPIService
from utils.upstream_fields import apply_upstream_rules, validate_rules


def rewrite(body, rules, context=None):
    body = copy.deepcopy(body)
    applied = apply_upstream_rules(body, {'upstream_rules': rules}, context)
    return body, applied


def test_set_and_delete_follow_paths_and_wildcards():
    body = {'tools': [{'name': 'a', 'input_schema': {'$schema': 'x', 'type': 'object'}},
                      {'name': 'b', 'input_schema': {'$schema': 'y'}}],
            'stream_options': {'include_usage': True}}
    body, applied = rewrite(body, [{'delete': 'tools.*.input_schema.$schema'}, {'delete': 'stream_options'},
                                   {'set': 'generation_config.temperature', 'value': 0.2},
                                   {'set': 'tools.0.name', 'value': 'renamed'}, {'delete': 'missing.path'}])
    assert body == {'tools': [{'name': 'renamed', 'input_schema': {'type': 'object'}}, {'name': 'b', 'input_schema': {}}],
                    'generation_config': {'temperature': 0.2}}
    # 没碰到任何东西的规则不记进生效列表
    assert applied == ['delete tools.*.input_schema.$schema', 'delete stream_options',
                       'set generation_config.temperature', 'set tools.0.name']


def test_delete_wildcard_on_array_removes_every_element():
    body, _ = rewrite({'messages': [{'role': 'user', 'cache_control': 1}, {'role': 'assistant', 'cache_control': 2}]},
                      [{'delete': 'messages.*.cache_control'}])
    assert body == {'messages': [{'role': 'user'}, {'role': 'assistant'}]}


@pytest.mark.parametrize('tools, choice, kept', [
    # OpenAI Chat
    ([{'type': 'function', 'function': {'name': 'WebSearch'}}, {'type': 'function', 'function': {'name': 'Read'}}],
     {'type': 'function', 'function': {'name': 'WebSearch'}}, ['Read']),
    # Anthropic，内置工具按 type 识别
    ([{'name': 'mcp__fs__read'}, {'type': 'web_search_20250305', 'name': 'web_search'}, {'name': 'Bash'}],
     {'type': 'tool', 'name': 'mcp__fs__read'}, ['Bash']),
])
def test_drop_tools_matches_names_and_clears_dangling_tool_choice(tools, choice, kept):
    body, applied = rewrite({'tools': tools, 'tool_choice': choice}, [{'drop_tools': ['WebSearch', 'web_*', 'mcp__*']}])
    assert [tool.get('name') or tool['function']['name'] for tool in body['tools']] == kept
    assert 'tool_choice' not in body and applied[0].startswith('drop_tools ')


def test_drop_tools_inside_gemini_function_declarations():
    body = {'tools': [{'functionDeclarations': [{'name': 'WebSearch'}, {'name': 'Read'}]},
                      {'functionDeclarations': [{'name': 'WebFetch'}], 'googleSearch': {}},
                      {'functionDeclarations': [{'name': 'WebPage'}]}]}
    body, applied = rewrite(body, [{'drop_tools': ['Web*']}])
    assert body['tools'] == [{'functionDeclarations': [{'name': 'Read'}]}, {'googleSearch': {}}]
    assert applied == ['drop_tools WebSearch, WebFetch, WebPage']


def test_dropping_every_tool_removes_tools_and_tool_choice():
    body, _ = rewrite({'tools': [{'name': 'a'}], 'tool_choice': {'type': 'any'}, 'model': 'm'}, [{'drop_tools': ['*']}])
    assert body == {'model': 'm'}


def test_text_replace_only_touches_conversation_text():
    body = {'system': ['你是 Claude Code'], 'instructions': 'Claude Code 说明',
            'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': '用 Claude Code 改'},
                                                      {'type': 'image', 'source': {'data': 'Claude Code'}}]}],
            'tools': [{'name': 'x', 'description': 'Claude Code 工具'}], 'model': 'Claude Code'}
    body, applied = rewrite(body, [{'replace': 'Claude Code', 'with': '助手'}])
    assert body['system'] == ['你是 助手'] and body['instructions'] == '助手 说明'
    assert body['messages'][0]['content'][0]['text'] == '用 助手 改'
    assert body['messages'][0]['content'][1]['source']['data'] == 'Claude Code'
    assert body['tools'][0]['description'] == 'Claude Code 工具' and body['model'] == 'Claude Code'
    assert applied == ['replace Claude Code']


def test_body_replace_keeps_valid_json_and_skips_broken_results():
    body, applied = rewrite({'model': 'gpt-x', 'metadata': {'user': 'gpt-x'}}, [{'replace': 'gpt-x', 'with': 'gpt-y', 'in': 'body'}])
    assert body == {'model': 'gpt-y', 'metadata': {'user': 'gpt-y'}} and applied == ['replace body gpt-x']
    body, applied = rewrite({'model': 'm'}, [{'replace': '"model"', 'with': 'model', 'in': 'body'}])
    assert body == {'model': 'm'} and applied == []


def test_callers_limit_rules_to_listed_owners():
    rules = [{'delete': 'stream_options', 'callers': ['guest-1', '小明']}]
    body = {'stream_options': {}}
    assert rewrite(body, rules)[0] == body
    assert rewrite(body, rules, RequestContext(model='m', owner_id='admin', owner_name='管理员'))[0] == body
    assert rewrite(body, rules, RequestContext(model='m', owner_id='guest-1'))[0] == {}
    assert rewrite(body, rules, RequestContext(model='m', owner_id='guest-2', owner_name='小明'))[0] == {}


@pytest.mark.parametrize('rules', [
    {'delete': 'x'}, [{'delete': 'x', 'set': 'y', 'value': 1}], [{}], [{'set': 'a'}], [{'delete': ''}],
    [{'drop_tools': []}], [{'drop_tools': 'WebSearch'}], [{'replace': '('}], [{'replace': 'a', 'in': 'tools'}],
    [{'replace': 'a', 'with': 1}], [{'delete': 'x', 'callers': 'admin'}],
])
def test_validation_rejects_malformed_rules(rules):
    with pytest.raises(ValueError):
        validate_rules(rules)


def test_malformed_rules_are_skipped_at_request_time():
    body, applied = rewrite({'stream_options': {}}, [{'delete': 'stream_options'}, {'replace': '('}])
    assert body == {'stream_options': {}} and applied == []


def test_rules_apply_to_the_body_sent_upstream_and_are_recorded():
    received = []

    async def handle(request):
        received.append(await request.json())
        return web.json_response({'id': 'r', 'choices': [{'message': {'content': 'hi'}}]})

    async def run():
        app = web.Application()
        app.router.add_post('/v1/chat/completions', handle)
        server = TestServer(app)
        await server.start_server()
        body = {'model': 'm', 'stream': False, 'messages': [{'role': 'user', 'content': 'hi'}],
                'tools': [{'type': 'function', 'function': {'name': 'WebSearch'}}], 'stream_options': {'include_usage': True}}
        original = copy.deepcopy(body)
        context = RequestContext(model='demo', endpoint={'api_type': 'direct_api', 'upstream_rules': [
            {'drop_tools': ['WebSearch']}, {'delete': 'stream_options'}]})
        token = current_request.set(context)
        try:
            async with aiohttp.ClientSession() as session:
                for _ in range(2):
                    async for _chunk in DirectAPIService(session).call_api_passthrough(
                            base_url=str(server.make_url('/v1')), api_key='k', request_body=body,
                            endpoint_path='/chat/completions'):
                        pass
        finally:
            current_request.reset(token)
            await server.close()
        return body, original, context.attempts

    body, original, attempts = asyncio.run(run())
    assert received == [{'model': 'm', 'stream': False, 'messages': [{'role': 'user', 'content': 'hi'}]}] * 2
    # 改的是副本：续传和重试复用的原请求体保持原样
    assert body == original
    assert attempts[-1]['upstream_rules'] == ['drop_tools WebSearch', 'delete stream_options']
    assert json.dumps(attempts, ensure_ascii=False)
