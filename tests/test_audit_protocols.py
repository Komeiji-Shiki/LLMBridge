"""Provider contracts checked against in-memory HTTP payloads."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
from converters.anthropic_openai import convert_anthropic_to_openai_request
from routes._direct_api_anthropic import convert_openai_to_anthropic_request, apply_thinking_config
from routes.api_routes import _apply_native_thinking_config
from services.direct_api_service import DirectAPIService
from services.sse import iter_sse_json_events

SCHEMA = {'type': 'object', 'properties': {'answer': {'type': 'string'}},
          'required': ['answer'], 'additionalProperties': False}


def test_anthropic_structured_output_strict_and_parallel_round_trip():
    original = {'model': 'demo', 'messages': [{'role': 'user', 'content': 'hello'}],
                'tools': [{'type': 'function', 'function': {'name': 'lookup', 'parameters': SCHEMA, 'strict': True}}],
                'parallel_tool_calls': False,
                'response_format': {'type': 'json_schema', 'json_schema': {'name': 'response', 'schema': SCHEMA}}}
    native = convert_openai_to_anthropic_request(original)
    assert native['tools'][0]['strict'] is True
    assert native['tool_choice']['disable_parallel_tool_use'] is True
    assert native['output_config']['format']['schema'] == SCHEMA
    native['output_config']['effort'] = 'high'
    back = convert_anthropic_to_openai_request(native)
    assert back['tools'][0]['function']['strict'] is True
    assert back['parallel_tool_calls'] is False
    assert back['response_format'] == original['response_format']
    assert back['reasoning_effort'] == 'high'


@pytest.mark.parametrize('apply', [apply_thinking_config, _apply_native_thinking_config])
@pytest.mark.parametrize('config', [
    {'enable_thinking': False}, {'enable_thinking': 'adaptive'},
    {'enable_thinking': 'adaptive', 'thinking_effort': 'high'},
    {'enable_thinking': True, 'reasoning_effort': 'medium'},
    {'enable_thinking': True, 'thinking_budget': 2048}])
def test_thinking_never_erases_structured_output(apply, config):
    body = {'max_tokens': 8192, 'output_config': {'format': {'type': 'json_schema', 'schema': SCHEMA}, 'effort': 'low'}}
    apply(body, config)
    assert body['output_config']['format']['schema'] == SCHEMA


def native_response(call_id='one'):
    return {'candidates': [{'content': {'parts': [
        {'text': 'Calling a tool'}, {'functionCall': {'id': call_id, 'name': 'lookup', 'args': {'answer': 'yes'}},
                                   'thoughtSignature': 'signed'}]}, 'finishReason': 'STOP'}]}


def test_gemini_call_signature_text_and_finish_reason():
    service = DirectAPIService(MagicMock())
    choice = service.convert_gemini_response_to_openai(native_response(), 'demo', 'request')['choices'][0]
    assert choice['message']['content'] == 'Calling a tool'
    assert choice['finish_reason'] == 'tool_calls'
    assert choice['message']['tool_calls'][0]['_thought_signature'] == 'signed'


@pytest.mark.parametrize('stream', [False, True])
def test_gemini_cache_hits_reach_anthropic_usage(stream):
    from converters.anthropic_openai import convert_openai_usage_to_anthropic
    native = {**native_response(), 'usageMetadata': {
        'promptTokenCount': 1000, 'cachedContentTokenCount': 800,
        'candidatesTokenCount': 20, 'thoughtsTokenCount': 30, 'totalTokenCount': 1050}}
    usage = DirectAPIService(MagicMock()).convert_gemini_response_to_openai(
        native, 'demo', 'r', is_stream_chunk=stream)['usage']
    assert usage['prompt_tokens_details'] == {'cached_tokens': 800}
    assert convert_openai_usage_to_anthropic(usage) == {
        'input_tokens': 200, 'cache_creation_input_tokens': 0,
        'cache_read_input_tokens': 800, 'output_tokens': 50}


def test_gemini_stream_tool_indices_remain_distinct_across_chunks():
    service = DirectAPIService(MagicMock())
    indices = {}
    for index, call_id in enumerate(['one', 'two']):
        result = service.convert_gemini_response_to_openai(native_response(call_id), 'demo', 'r',
                                                          is_stream_chunk=True, tool_call_indices=indices)
        assert result['choices'][0]['delta']['tool_calls'][0]['index'] == index
    end = service.convert_gemini_response_to_openai({'candidates': [{'finishReason': 'STOP'}]}, 'demo', 'r',
                                                   is_stream_chunk=True, tool_call_indices=indices)
    assert end['choices'][0]['finish_reason'] == 'tool_calls'


def gemini_chunk(parts, finish=None):
    candidate = {'content': {'role': 'model', 'parts': parts}}
    if finish:
        candidate['finishReason'] = finish
    return {'candidates': [candidate]}


# 真实形态：思考增量 → 并行调用（只有第一个 functionCall 带签名）
GEMINI_TOOL_TURN = [
    gemini_chunk([{'thought': True, 'text': '先查'}]),
    gemini_chunk([{'thought': True, 'text': '两个城市'}]),
    gemini_chunk([{'functionCall': {'id': 'call_1', 'name': 'get_weather', 'args': {'city': '北京'}},
                   'thoughtSignature': 'sig-call'},
                  {'functionCall': {'id': 'call_2', 'name': 'get_weather', 'args': {'city': '上海'}}}], 'STOP'),
]
# 正文结尾的纯签名分片
GEMINI_TEXT_TURN = [
    gemini_chunk([{'text': '北京晴，'}]),
    gemini_chunk([{'text': '上海雨。'}]),
    gemini_chunk([{'text': '', 'thoughtSignature': 'sig-text'}], 'STOP'),
]


def openai_stream_chunks(gemini_chunks):
    service, indices = DirectAPIService(MagicMock()), {}
    return [service.convert_gemini_response_to_openai(chunk, 'demo', 'r', is_stream_chunk=True,
                                                      tool_call_indices=indices) for chunk in gemini_chunks]


def anthropic_stream_blocks(openai_chunks):
    """经 Anthropic SSE 转换后，按客户端 SDK 的方式累积出 content blocks。"""
    from fastapi.responses import StreamingResponse
    from converters.anthropic_openai import build_anthropic_streaming_response

    async def body():
        for chunk in openai_chunks:
            yield f'data: {json.dumps(chunk, ensure_ascii=False)}\n\n'
        yield 'data: [DONE]\n\n'

    async def run():
        response = build_anthropic_streaming_response(StreamingResponse(body()), 'demo')
        return ''.join([part async for part in response.body_iterator])

    blocks, keys = {}, {'thinking_delta': 'thinking', 'signature_delta': 'signature',
                        'text_delta': 'text', 'input_json_delta': 'partial_json'}
    for line in asyncio.run(run()).splitlines():
        event = json.loads(line[5:]) if line.startswith('data:') else {}
        if event.get('type') == 'content_block_start':
            blocks[event['index']] = dict(event['content_block'])
        elif event.get('type') == 'content_block_delta':
            key = keys[event['delta']['type']]
            block = blocks[event['index']]
            block[key] = block.get(key, '') + event['delta'][key]
    result = [blocks[index] for index in sorted(blocks)]
    for block in result:
        if block['type'] == 'tool_use':
            block['input'] = json.loads(block.pop('partial_json', '') or '{}')
    return result


TOOL_TURN_BLOCKS = [
    {'type': 'thinking', 'thinking': '先查两个城市', 'signature': 'gemini.v1.call.sig-call'},
    {'type': 'tool_use', 'id': 'call_1', 'name': 'get_weather', 'input': {'city': '北京'}},
    {'type': 'tool_use', 'id': 'call_2', 'name': 'get_weather', 'input': {'city': '上海'}},
]
TEXT_TURN_BLOCKS = [
    {'type': 'text', 'text': '北京晴，上海雨。'},
    {'type': 'thinking', 'thinking': '', 'signature': 'gemini.v1.text.sig-text'},
]


@pytest.mark.parametrize('gemini_chunks, expected', [
    (GEMINI_TOOL_TURN, TOOL_TURN_BLOCKS), (GEMINI_TEXT_TURN, TEXT_TURN_BLOCKS)])
def test_gemini_signatures_stream_to_anthropic_thinking_blocks(gemini_chunks, expected):
    assert anthropic_stream_blocks(openai_stream_chunks(gemini_chunks)) == expected


@pytest.mark.parametrize('gemini_chunks, expected', [
    (GEMINI_TOOL_TURN, TOOL_TURN_BLOCKS), (GEMINI_TEXT_TURN, TEXT_TURN_BLOCKS)])
def test_gemini_signatures_non_stream_match_stream_blocks(gemini_chunks, expected):
    from fastapi.responses import JSONResponse
    from converters.anthropic_openai import convert_openai_non_stream_response_to_anthropic
    parts = [part for chunk in gemini_chunks for part in chunk['candidates'][0]['content']['parts']]
    openai = DirectAPIService(MagicMock()).convert_gemini_response_to_openai(
        gemini_chunk(parts, 'STOP'), 'demo', 'r')
    response = asyncio.run(convert_openai_non_stream_response_to_anthropic(JSONResponse(openai), 'demo'))
    assert json.loads(response.body)['content'] == expected


def gemini_request_body(messages):
    async def run():
        session = MagicMock()
        response = MagicMock(status=200)
        response.json = AsyncMock(return_value={'candidates': []})
        session.post.return_value.__aenter__ = AsyncMock(return_value=response)
        session.post.return_value.__aexit__ = AsyncMock(return_value=False)
        [item async for item in DirectAPIService(session).call_gemini_native_api('key', 'demo', messages)]
        return json.loads(session.post.call_args.kwargs['data'])
    return asyncio.run(run())


def test_anthropic_thinking_signatures_restore_gemini_parts_in_place():
    anthropic = {'model': 'demo', 'messages': [
        {'role': 'user', 'content': '北京和上海天气？'},
        {'role': 'assistant', 'content': TOOL_TURN_BLOCKS},
        {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'call_1', 'content': '晴'},
            {'type': 'tool_result', 'tool_use_id': 'call_2', 'content': '雨'}]},
        {'role': 'assistant', 'content': TEXT_TURN_BLOCKS},
        {'role': 'user', 'content': '谢谢'}]}
    contents = gemini_request_body(convert_anthropic_to_openai_request(anthropic)['messages'])['contents']
    assert contents[1] == {'role': 'model', 'parts': [
        {'text': '先查两个城市', 'thought': True},
        {'functionCall': {'name': 'get_weather', 'args': {'city': '北京'}, 'id': 'call_1'},
         'thoughtSignature': 'sig-call'},
        {'functionCall': {'name': 'get_weather', 'args': {'city': '上海'}, 'id': 'call_2'}}]}
    # 并行调用的结果同在一轮
    assert [part['functionResponse']['id'] for part in contents[2]['parts']] == ['call_1', 'call_2']
    assert contents[3] == {'role': 'model', 'parts': [{'text': '北京晴，上海雨。', 'thoughtSignature': 'sig-text'}]}


def test_foreign_thinking_signatures_never_reach_gemini():
    from converters.anthropic_openai import sanitize_anthropic_thinking_blocks
    claude_turn = [{'type': 'thinking', 'thinking': 'Claude 的思考', 'signature': 'EqQBCkYIBRgCKkCx'},
                   {'type': 'text', 'text': '好'}]
    openai = convert_anthropic_to_openai_request({'model': 'demo', 'messages': [
        {'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': claude_turn}]})
    assert 'provider_metadata' not in openai['messages'][1]
    # 反过来切回 Claude 时，Gemini 签名也不能交给 Anthropic 校验
    cleaned = sanitize_anthropic_thinking_blocks([{'role': 'assistant', 'content': TOOL_TURN_BLOCKS}])
    assert cleaned[0]['content'] == TOOL_TURN_BLOCKS[1:]


def test_anthropic_responses_report_requested_model():
    """Claude Code 按 message.model 判断签名归属，回报显示名会让重启后的 CLI 剥掉签名思考块。"""
    from fastapi.responses import JSONResponse, StreamingResponse
    from converters.anthropic_openai import (build_anthropic_streaming_response,
                                              convert_openai_non_stream_response_to_anthropic)
    chunk = {'model': 'display-name', 'choices': [{'index': 0, 'delta': {'content': '好'}}]}

    async def body():
        yield f'data: {json.dumps(chunk)}\n\n'
        yield 'data: [DONE]\n\n'

    async def run():
        response = build_anthropic_streaming_response(StreamingResponse(body()), 'gemini-4-argon-sjj')
        return ''.join([part async for part in response.body_iterator])

    starts = [json.loads(line[5:]) for line in asyncio.run(run()).splitlines()
              if line.startswith('data:') and '"message_start"' in line]
    assert starts[0]['message']['model'] == 'gemini-4-argon-sjj'
    openai = {'model': 'display-name', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '好'}}]}
    response = asyncio.run(convert_openai_non_stream_response_to_anthropic(JSONResponse(openai), 'gemini-4-argon-sjj'))
    assert json.loads(response.body)['model'] == 'gemini-4-argon-sjj'


def test_flat_stream_chunk_with_text_and_tool_call_keeps_both():
    chunk = {'choices': [{'index': 0, 'delta': {
        'reasoning_content': '想想', 'content': '我来查',
        'tool_calls': [{'index': 0, 'id': 'c1', 'type': 'function',
                        'function': {'name': 'lookup', 'arguments': '{"q": 1}'}}]}}]}
    assert anthropic_stream_blocks([chunk]) == [
        {'type': 'thinking', 'thinking': '想想'}, {'type': 'text', 'text': '我来查'},
        {'type': 'tool_use', 'id': 'c1', 'name': 'lookup', 'input': {'q': 1}}]


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('base_url, expected_base', [
    (None, 'https://generativelanguage.googleapis.com/v1beta'),
    ('http://localhost:8317', 'http://localhost:8317'),
    ('http://localhost:8317/v1beta', 'http://localhost:8317/v1beta'),
    ('http://localhost:8317/v1beta/', 'http://localhost:8317/v1beta'),
])
def test_gemini_request_returns_signature_and_generation_options(stream, base_url, expected_base):
    async def run():
        session = MagicMock()
        response = MagicMock(status=200)
        response.json = AsyncMock(return_value={'candidates': []})
        async def chunks():
            yield b'data: {"candidates": []}\n\n'
        response.content.iter_any = chunks
        session.post.return_value.__aenter__ = AsyncMock(return_value=response)
        session.post.return_value.__aexit__ = AsyncMock(return_value=False)
        service = DirectAPIService(session)
        assistant = service.convert_gemini_response_to_openai(native_response(), 'demo', 'r')['choices'][0]['message']
        original = copy.deepcopy(assistant)
        result = [item async for item in service.call_gemini_native_api(
            'test-key', 'demo', [assistant, {'role': 'tool', 'tool_call_id': 'one', 'content': 'ok'}],
            stream=stream, base_url=base_url,
            response_format={'type': 'json_schema', 'json_schema': {'schema': SCHEMA}},
            stop_sequences=['END'])]
        assert not any('error' in item for item in result)
        method = 'streamGenerateContent' if stream else 'generateContent'
        suffix = '?alt=sse' if stream else ''
        assert session.post.call_args.args[0] == f'{expected_base}/models/demo:{method}{suffix}'
        assert session.post.call_args.kwargs['headers']['x-goog-api-key'] == 'test-key'
        body = json.loads(session.post.call_args.kwargs['data'])
        assert body['contents'][0]['parts'][1]['thoughtSignature'] == 'signed'
        assert body['contents'][1]['parts'][0]['functionResponse']['id'] == 'one'
        assert body['generationConfig']['responseJsonSchema'] == SCHEMA
        assert body['generationConfig']['responseMimeType'] == 'application/json'
        assert body['generationConfig']['stopSequences'] == ['END']
        assert assistant == original
    asyncio.run(run())


@pytest.mark.parametrize('stream', [False, True])
def test_gemini_generation_reports_original_block_reason(stream):
    from core.request_context import RequestContext, current_request

    async def run():
        context = RequestContext()
        token = current_request.set(context)
        session = MagicMock()
        response = MagicMock(status=200)
        blocked = {'candidates': [{'finishReason': 'PROHIBITED_CONTENT', 'finishMessage': 'provider explanation'}]}
        response.json = AsyncMock(return_value=blocked)

        async def chunks():
            yield ('data: ' + json.dumps(blocked) + '\n\n').encode()

        response.content.iter_any = chunks
        session.post.return_value.__aenter__ = AsyncMock(return_value=response)
        session.post.return_value.__aexit__ = AsyncMock(return_value=False)
        try:
            result = [item async for item in DirectAPIService(session).call_gemini_native_api(
                'test-key', 'demo', [{'role': 'user', 'content': 'hello'}], stream=stream)]
            assert result[-1]['error']['reason'] == 'PROHIBITED_CONTENT'
            assert 'provider explanation' in result[-1]['error']['message']
            assert context.outcome['stop_reason'] == 'PROHIBITED_CONTENT'
        finally:
            current_request.reset(token)

    asyncio.run(run())


def test_gemini_stream_termination_keeps_final_chunk_content_and_usage():
    """终止块里的最后一段文本和用量要先交给调用方，错误作为独立事件随后发出。"""
    from core.request_context import RequestContext, current_request

    async def run():
        context = RequestContext()
        token = current_request.set(context)
        session = MagicMock()
        response = MagicMock(status=200)
        final = {'candidates': [{'content': {'parts': [{'text': 'tail'}]}, 'finishReason': 'RECITATION'}],
                 'usageMetadata': {'promptTokenCount': 7, 'candidatesTokenCount': 3}}

        async def chunks():
            yield ('data: ' + json.dumps(final) + '\n\n').encode()

        response.content.iter_any = chunks
        session.post.return_value.__aenter__ = AsyncMock(return_value=response)
        session.post.return_value.__aexit__ = AsyncMock(return_value=False)
        try:
            result = [item async for item in DirectAPIService(session).call_gemini_native_api(
                'test-key', 'demo', [{'role': 'user', 'content': 'hello'}], stream=True)]
            assert result[0] == final
            assert result[1] == {'error': result[1]['error']}
            assert result[1]['error']['reason'] == 'RECITATION'
        finally:
            current_request.reset(token)

    asyncio.run(run())


def test_gemini_termination_ignores_malformed_candidates():
    from services.protocol_events import gemini_termination
    assert gemini_termination({'candidates': ['oops']}) == (None, None)
    assert gemini_termination({'candidates': [], 'promptFeedback': 'bad'}) == (None, None)
    reason, error = gemini_termination({'promptFeedback': {'blockReason': 'SAFETY'}})
    assert reason == 'SAFETY' and error['code'] == 422


def test_gemini_empty_upstream_error_reports_http_status():
    async def run():
        session = MagicMock()
        response = MagicMock(status=404)
        response.text = AsyncMock(return_value='')
        session.post.return_value.__aenter__ = AsyncMock(return_value=response)
        session.post.return_value.__aexit__ = AsyncMock(return_value=False)
        service = DirectAPIService(session)
        return [item async for item in service.call_gemini_native_api(
            'test-key', 'demo', [{'role': 'user', 'content': 'hello'}], stream=True)]

    result = asyncio.run(run())
    assert result[0]['error']['code'] == 404
    assert 'HTTP 404' in result[0]['error']['message']


@pytest.mark.parametrize('chunk_size', [1, 2, 13, 1000])
def test_sse_multiline_unicode_and_done(chunk_size):
    async def run():
        raw = 'event: message\r\ndata: {"text":\r\ndata: "你好"}\r\n\r\ndata: []\n\ndata: [DONE]\n\ndata: {"ignored":true}\n\n'.encode()
        async def chunks():
            for offset in range(0, len(raw), chunk_size):
                yield raw[offset:offset + chunk_size]
        response = SimpleNamespace(content=SimpleNamespace(iter_any=chunks))
        return [item async for item in iter_sse_json_events(response, 'test')]
    assert asyncio.run(run()) == [{'text': '你好'}, {'done': True}]


@pytest.mark.parametrize('raw,bare', [(b'data: {"ok":true}', False), (b'{"ok":true}', True)])
def test_sse_flushes_final_unterminated_event(raw, bare):
    async def run():
        async def chunks():
            yield raw
        response = SimpleNamespace(content=SimpleNamespace(iter_any=chunks))
        return [item async for item in iter_sse_json_events(response, 'test', parse_bare_json=bare)]
    assert asyncio.run(run()) == [{'ok': True}]


@pytest.mark.parametrize('stream', [False, True])
def test_responses_content_filter_is_an_incomplete_result(stream):
    from converters.responses_openai import convert_chat_response_to_responses, build_responses_streaming_response
    from fastapi.responses import Response
    request = {'model': 'demo', 'input': 'hello'}
    chat = {'model': 'demo', 'choices': [{'index': 0, 'finish_reason': 'content_filter',
                                        'message': {'role': 'assistant', 'content': ''}}]}
    if stream:
        async def run():
            source = Response(content='data: ' + json.dumps(chat) + '\n\ndata: [DONE]\n\n', media_type='text/event-stream')
            result = build_responses_streaming_response(source, request=request, model='demo')
            chunks = [chunk async for chunk in result.body_iterator]
            text = ''.join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks)
            return [json.loads(line[5:]) for line in text.splitlines() if line.startswith('data:')][-1]['response']
        result = asyncio.run(run())
    else:
        result = convert_chat_response_to_responses(chat, request)
    assert result['status'] == 'incomplete'
    assert result['incomplete_details'] == {'reason': 'content_filter'}
