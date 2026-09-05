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


@pytest.mark.parametrize('stream', [False, True])
def test_gemini_request_returns_signature_and_generation_options(stream):
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
            stream=stream, response_format={'type': 'json_schema', 'json_schema': {'schema': SCHEMA}},
            stop_sequences=['END'])]
        assert not any('error' in item for item in result)
        body = json.loads(session.post.call_args.kwargs['data'])
        assert body['contents'][0]['parts'][1]['thoughtSignature'] == 'signed'
        assert body['contents'][1]['parts'][0]['functionResponse']['id'] == 'one'
        assert body['generationConfig']['responseJsonSchema'] == SCHEMA
        assert body['generationConfig']['responseMimeType'] == 'application/json'
        assert body['generationConfig']['stopSequences'] == ['END']
        assert assistant == original
    asyncio.run(run())


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
