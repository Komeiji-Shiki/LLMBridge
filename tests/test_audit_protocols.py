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


@pytest.mark.parametrize('stream', [False, True])
def test_google_openai_signatures_round_trip_through_anthropic(stream):
    """Google OAI 签名经 thinking 块回传后，仍绑定原工具调用。"""
    from fastapi.responses import JSONResponse
    from converters.anthropic_openai import convert_openai_non_stream_response_to_anthropic
    from routes._direct_api_passthrough import _prepare_passthrough_request

    calls = [
        {'index': 0, 'id': 'call_1', 'type': 'function',
         'function': {'name': 'get_weather', 'arguments': '{"city":"北京"}'},
         'extra_content': {'google': {'thought_signature': 'sig-call'}}},
        {'index': 1, 'id': 'call_2', 'type': 'function',
         'function': {'name': 'get_weather', 'arguments': '{"city":"上海"}'}}]
    message = {'role': 'assistant', 'reasoning_content': '先查两个城市', 'content': None, 'tool_calls': calls}
    original = copy.deepcopy(message)
    if stream:
        chunks = [
            {'choices': [{'delta': {'reasoning_content': message['reasoning_content']}}]},
            {'choices': [{'delta': {'tool_calls': [
                {**call, 'function': {**call['function'], 'arguments': ''}} for call in calls]}}]},
            {'choices': [{'delta': {'tool_calls': [
                {'index': call['index'], 'function': {'arguments': call['function']['arguments']}}
                for call in calls]}, 'finish_reason': 'tool_calls'}]}]
        blocks = anthropic_stream_blocks(chunks)
    else:
        response = JSONResponse({'choices': [{'message': message, 'finish_reason': 'tool_calls'}]})
        converted = asyncio.run(convert_openai_non_stream_response_to_anthropic(response, 'demo'))
        blocks = json.loads(converted.body)['content']
    assert blocks == TOOL_TURN_BLOCKS
    request = convert_anthropic_to_openai_request({'model': 'demo', 'messages': [
        {'role': 'assistant', 'content': blocks}]})
    prepared = _prepare_passthrough_request(request, 'demo', 'gemini-4-argon', {})
    returned = prepared['messages'][0]
    assert returned['tool_calls'][0]['extra_content'] == calls[0]['extra_content']
    assert 'extra_content' not in returned['tool_calls'][1]
    assert 'provider_metadata' not in returned
    assert message == original
    assert 'extra_content' not in request['messages'][0]['tool_calls'][0]


def test_google_openai_preserves_client_signature_and_legacy_native_signature():
    from routes._direct_api_passthrough import _prepare_passthrough_request

    request = {'messages': [{'role': 'assistant', 'tool_calls': [
        {'id': 'one', 'function': {'name': 'first', 'arguments': '{}'},
         '_thought_signature': 'old', 'extra_content': {'google': {'thought_signature': 'client'}}},
        {'id': 'two', 'function': {'name': 'second', 'arguments': '{}'}, '_thought_signature': 'legacy'}]}]}
    original = copy.deepcopy(request)
    prepared = _prepare_passthrough_request(request, 'demo', 'gemini-4-argon', {})
    calls = prepared['messages'][0]['tool_calls']
    assert calls[0]['extra_content']['google']['thought_signature'] == 'client'
    assert calls[1]['extra_content']['google']['thought_signature'] == 'legacy'
    assert all('_thought_signature' not in call for call in calls)
    assert request == original


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


@pytest.mark.parametrize("protocol", ["generate_content", "interactions"])
@pytest.mark.parametrize("audio_block", [
    {"type": "audio", "data": "QUJD", "mimeType": "audio/mpeg"},
    {"type": "audio", "source": {"type": "base64", "data": "QUJD", "media_type": "audio/mpeg"}},
    {"type": "text", "text": '1→<graywill_audio mime_type="audio/mpeg">QUJD</graywill_audio>'},
    {"type": "text", "text": '<graywill_audio_ref id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"/>'},
])
def test_anthropic_tool_audio_keeps_parallel_results_before_media(protocol, audio_block):
    """音频经过入口转换后保留字节及 MIME，两个并行工具结果仍先配对完成。"""
    from converters.gemini_interactions import convert_oai_messages_to_interactions

    original = {"model": "demo", "messages": [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "read_1", "name": "Read", "input": {"file_path": "a.mp3"}},
            {"type": "tool_use", "id": "read_2", "name": "Read", "input": {"file_path": "b.txt"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "read_1", "content": [
                {"type": "text", "text": "a.mp3"}, audio_block]},
            {"type": "tool_result", "tool_use_id": "read_2", "content": "普通文件"},
        ]},
    ]}
    before = copy.deepcopy(original)
    messages = convert_anthropic_to_openai_request(original)["messages"]
    if "graywill_audio_ref" in str(audio_block):
        from core.audio_files import AudioFiles
        from core.request_context import RequestContext, current_request
        from services.audio_input import resolve_audio_references
        store = AudioFiles()
        store.root.mkdir(parents=True, exist_ok=True)
        file_id = "a" * 32
        store.paths(file_id)[0].write_bytes(b"ABC")
        store.save_metadata(file_id, {"owner_id": "test-audio", "mime_type": "audio/mpeg", "filename": "a.mp3", "size": 3})
        token = current_request.set(RequestContext(owner_id="test-audio", authenticated=True))
        try:
            messages = asyncio.run(resolve_audio_references(messages, None, "", None, {}))
        finally:
            current_request.reset(token)
    if protocol == "interactions":
        steps, _ = convert_oai_messages_to_interactions(messages)
        assert [step["call_id"] for step in steps if step["type"] == "function_result"] == ["read_1", "read_2"]
        assert [step["type"] for step in steps] == [
            "function_call", "function_call", "function_result", "function_result", "user_input"]
        assert steps[-1]["content"][-1] == {"type": "audio", "data": "QUJD", "mime_type": "audio/mpeg"}
    else:
        async def run():
            session = MagicMock()
            response = MagicMock(status=200)
            response.json = AsyncMock(return_value={"candidates": []})
            session.post.return_value.__aenter__ = AsyncMock(return_value=response)
            session.post.return_value.__aexit__ = AsyncMock(return_value=False)
            results = [item async for item in DirectAPIService(session).call_gemini_native_api(
                "test-key", "demo", messages, stream=False)]
            assert not any("error" in item for item in results)
            return json.loads(session.post.call_args.kwargs["data"])
        body = asyncio.run(run())
        results = body["contents"][1]["parts"]
        assert [part["functionResponse"]["id"] for part in results] == ["read_1", "read_2"]
        assert body["contents"][-1]["parts"][-1] == {"inlineData": {"mimeType": "audio/mpeg", "data": "QUJD"}}
    assert original == before


@pytest.mark.parametrize('stream', [False, True])
def test_responses_reasoning_survives_anthropic_tool_round_trip(stream):
    from fastapi.responses import JSONResponse
    from converters.anthropic_openai import convert_openai_non_stream_response_to_anthropic
    from converters.responses_bridge import _ChatStreamBuilder, convert_chat_request_to_responses, convert_responses_response_to_chat

    reasoning = {'id': 'rs_1', 'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': '先查询'}],
                 'encrypted_content': 'opaque-reasoning'}
    call = {'id': 'fc_1', 'type': 'function_call', 'call_id': 'call_1', 'name': 'lookup', 'arguments': '{"city":"上海"}'}
    native = {'id': 'resp_1', 'status': 'completed', 'model': 'demo', 'output': [reasoning, call],
              'usage': {'input_tokens': 1000, 'output_tokens': 20, 'total_tokens': 1020,
                        'input_tokens_details': {'cached_tokens': 800}}}
    if stream:
        builder = _ChatStreamBuilder('demo')
        events = [
            {'type': 'response.output_item.added', 'output_index': 0, 'item': {'id': 'rs_1', 'type': 'reasoning', 'summary': []}},
            {'type': 'response.reasoning_summary_text.delta', 'delta': '先查询'},
            {'type': 'response.output_item.done', 'output_index': 0, 'item': reasoning},
            {'type': 'response.output_item.added', 'output_index': 1, 'item': {**call, 'arguments': ''}},
            {'type': 'response.function_call_arguments.delta', 'item_id': 'fc_1', 'delta': call['arguments']},
            {'type': 'response.output_item.done', 'output_index': 1, 'item': call},
            {'type': 'response.completed', 'response': native},
        ]
        chunks = [json.loads(chunk.decode()[6:]) for event in events for chunk in builder.process(event)]
        blocks = anthropic_stream_blocks(chunks)
    else:
        chat = convert_responses_response_to_chat(native, 'demo')
        converted = asyncio.run(convert_openai_non_stream_response_to_anthropic(JSONResponse(chat), 'demo'))
        body = json.loads(converted.body)
        assert body['usage']['cache_read_input_tokens'] == 800
        blocks = body['content']

    request = {'model': 'demo', 'messages': [
        {'role': 'user', 'content': '查询天气'}, {'role': 'assistant', 'content': blocks},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call_1',
                                      'content': '连接失败', 'is_error': True}]}]}
    replay = convert_chat_request_to_responses(convert_anthropic_to_openai_request(request))['input']
    assert replay[1] == reasoning
    assert replay[2]['call_id'] == 'call_1'
    assert json.loads(replay[2]['arguments']) == {'city': '上海'}
    assert replay[3]['output'] == '[tool_result_error] 连接失败'


@pytest.mark.parametrize('mode', ['json', 'stream', 'force_stream'])
def test_anthropic_reasoning_survives_responses_tool_round_trip(mode):
    from converters.cache_warnings import responses_cache_warnings
    from converters.responses_openai import build_responses_streaming_response, collect_chat_stream_response, convert_chat_response_to_responses, convert_responses_to_chat_request
    from routes._direct_api_anthropic import build_openai_stream_from_anthropic, convert_anthropic_response_to_openai

    thinking = {'type': 'thinking', 'thinking': '先查询', 'signature': 'claude-signature'}
    tool = {'type': 'tool_use', 'id': 'toolu_1', 'name': 'lookup', 'input': {'city': '上海'}}
    usage = {'input_tokens': 100, 'cache_read_input_tokens': 800, 'cache_creation_input_tokens': 100, 'output_tokens': 20}
    warnings = responses_cache_warnings({'prompt_cache_key': 'group'})
    if mode == 'json':
        chat = convert_anthropic_response_to_openai({'content': [thinking, tool], 'stop_reason': 'tool_use', 'usage': usage}, 'demo', 'r')
        result = convert_chat_response_to_responses(chat, {'model': 'demo'}, warnings)
    else:
        events = [
            {'type': 'message_start', 'message': {'usage': usage}},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'thinking_delta', 'thinking': thinking['thinking']}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'signature_delta', 'signature': thinking['signature']}},
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'content_block_start', 'index': 1, 'content_block': {**tool, 'input': {}}},
            {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '{"city":"上海"}'}},
            {'type': 'content_block_stop', 'index': 1},
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'cache_read_input_tokens': 800, 'output_tokens': 20}},
            {'type': 'message_stop'},
        ]

        async def run():
            async def source():
                for event in events:
                    yield ('event: ' + event['type'] + '\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode()
            monitor = MagicMock()
            monitor.broadcast_to_monitors = AsyncMock()
            chat_stream = build_openai_stream_from_anthropic(
                source(), 'demo', 'r', monitor, {}, {}, None, lambda *args: 0, {'messages': []}, [])
            if mode == 'force_stream':
                collected = await collect_chat_stream_response(chat_stream, 'demo')
                return convert_chat_response_to_responses(collected, {'model': 'demo'}, warnings)
            response = build_responses_streaming_response(chat_stream, {'model': 'demo'}, 'demo', warnings)
            assert 'x-bridge-warnings' in response.headers
            raw = b''.join([chunk async for chunk in response.body_iterator])
            payloads = [json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith('data: ')]
            return next(item['response'] for item in payloads if item['type'] == 'response.completed')
        result = asyncio.run(run())

    assert result['bridge_warnings'][0]['field'] == 'prompt_cache_key'
    assert result['usage']['input_tokens'] == 1000
    assert result['usage']['input_tokens_details']['cached_tokens'] == 800
    request = {'model': 'demo', 'reasoning': {'effort': 'high'}, 'input': [{'role': 'user', 'content': '查询天气'}, *result['output'],
                {'type': 'function_call_output', 'call_id': 'toolu_1', 'output': '晴'}]}
    replay = convert_openai_to_anthropic_request(convert_responses_to_chat_request(request))
    replay = apply_thinking_config(replay, {'enable_thinking': True, 'reasoning_effort': 'low'})
    assert replay['output_config']['effort'] == 'high'
    assert replay['messages'][1]['content'] == [thinking, tool]
    assert replay['messages'][2]['content'][0]['tool_use_id'] == 'toolu_1'


def test_anthropic_cache_breakpoint_warning_is_returned():
    from fastapi.responses import JSONResponse, StreamingResponse
    from converters.anthropic_openai import build_anthropic_streaming_response, convert_openai_non_stream_response_to_anthropic
    from converters.cache_warnings import anthropic_cache_warnings

    warnings = anthropic_cache_warnings({'system': [{'type': 'text', 'text': 'system', 'cache_control': {'type': 'ephemeral'}}]})
    chat = {'choices': [{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}]}
    response = asyncio.run(convert_openai_non_stream_response_to_anthropic(JSONResponse(chat), 'demo', warnings))
    assert json.loads(response.body)['bridge_warnings'][0]['field'] == 'system[0].cache_control'
    assert json.loads(response.headers['x-bridge-warnings'])[0]['applied'] is False

    async def stream():
        async def source():
            yield b'data: {"choices": [{"delta": {"content": "ok"}}]}\n\ndata: [DONE]\n\n'
        response = build_anthropic_streaming_response(StreamingResponse(source()), 'demo', warnings, usage_pending=True)
        assert response.headers['x-bridge-usage-event'] == 'message_delta'
        raw = ''.join([part async for part in response.body_iterator])
        return [json.loads(line[6:]) for line in raw.splitlines() if line.startswith('data: ')]

    events = asyncio.run(stream())
    assert events[0]['message']['bridge_usage_pending'] is True
    assert next(event for event in events if event['type'] == 'message_delta')['bridge_usage_pending'] is False
