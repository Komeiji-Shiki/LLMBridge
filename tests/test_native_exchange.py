import asyncio
import json
from unittest.mock import AsyncMock, MagicMock
import pytest
from core.request_context import RequestContext, current_request
from services.native_exchange import display_messages_for, forward_native_exchange


@pytest.mark.parametrize('provider,protocol,tool', [('deepseek','responses_native','web_search'),
                                                   ('qwen','responses_native','web_extractor'),
                                                   ('openai','responses_native','web_search'),
                                                   ('gemini','gemini_native','google_search')])
@pytest.mark.parametrize('stream', [True, False])
def test_native_tool_forwarding_and_usage(provider, protocol, tool, stream):
    async def run():
        service = MagicMock()
        service.calculate_cost.return_value = {'total_cost': 1, 'currency': 'USD'}
        monitor = MagicMock()
        monitor.broadcast_to_monitors = AsyncMock()
        response = {'candidates': [{'content': {'parts': [{'text': 'answer'}]}, 'finishReason': 'STOP'}],
                    'usageMetadata': {'promptTokenCount': 3, 'candidatesTokenCount': 4}} if provider == 'gemini' else {
                        'id': 'response-id', 'output': [{'type': 'web_search_call', 'id': 'search', 'status': 'completed'}],
                        'usage': {'input_tokens': 3, 'output_tokens': 4}}
        captured = {}
        async def upstream(**kwargs):
            captured.update(kwargs)
            if stream:
                event = response if provider == 'gemini' else {'type': 'response.completed', 'response': response}
                yield ('data: ' + json.dumps(event) + '\n\n').encode()
            else:
                yield json.dumps(response).encode()
        service.call_api_passthrough = upstream
        config = {'provider': provider, 'api_type': protocol, 'api_key': 'test', 'api_base_url': 'https://example.test', 'native_tools': [tool]}
        result = await forward_native_exchange({'input': 'hello'}, config, 'alias', service, monitor, stream=stream)
        if stream:
            raw = b''.join([chunk async for chunk in result.body_iterator])
        else:
            raw = result.body
        assert b'search' in raw if provider != 'gemini' else b'answer' in raw
        assert captured['request_body']['tools']
        if provider == 'gemini':
            assert 'stream' not in captured['request_body']
            assert captured['headers']['x-goog-api-key'] == 'test'
        ended = monitor.request_end.call_args.kwargs
        assert ended['input_tokens'] == 3
        assert ended['output_tokens'] == 4
        assert monitor.request_end.call_count == 1
    asyncio.run(run())


def test_display_messages_gemini_body():
    body = {
        'systemInstruction': {'parts': [{'text': '你是助手'}]},
        'contents': [
            {'role': 'user', 'parts': [{'text': '你好'}]},
            {'role': 'model', 'parts': [{'text': '你好呀'}]},
            {'role': 'user', 'parts': [{'text': '看这张图'}, {'inlineData': {'mimeType': 'image/png', 'data': 'AAAA'}}]},
        ],
        'generationConfig': {'temperature': 0.5},
    }
    assert display_messages_for(body, 'gemini') == [
        {'role': 'system', 'content': '你是助手'},
        {'role': 'user', 'content': '你好'},
        {'role': 'assistant', 'content': '你好呀'},
        {'role': 'user', 'content': [{'type': 'text', 'text': '看这张图'}, {'type': 'text', 'text': '[附件: image/png]'}]},
    ]


def test_display_messages_anthropic_keeps_system():
    body = {'system': '系统提示', 'messages': [{'role': 'user', 'content': 'hi'}]}
    assert display_messages_for(body, 'anthropic') == [
        {'role': 'system', 'content': '系统提示'},
        {'role': 'user', 'content': 'hi'},
    ]


def test_display_messages_responses_body():
    body = {
        'instructions': '系统提示',
        'input': [
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '问题'}]},
            {'type': 'function_call', 'call_id': 'call_1', 'name': 'search', 'arguments': '{"q": 1}'},
            {'type': 'function_call_output', 'call_id': 'call_1', 'output': '结果'},
        ],
    }
    assert display_messages_for(body, 'responses') == [
        {'role': 'system', 'content': '系统提示'},
        {'role': 'user', 'content': '问题'},
        {'role': 'assistant', 'tool_calls': [{'id': 'call_1', 'type': 'function',
                                             'function': {'name': 'search', 'arguments': '{"q": 1}'}}]},
        {'role': 'tool', 'tool_call_id': 'call_1', 'content': '结果'},
    ]


def test_display_messages_interactions_body():
    body = {
        'system_instruction': '系统提示',
        'input': [
            {'type': 'user_input', 'content': [{'type': 'text', 'text': '你好'}]},
            {'type': 'model_output', 'content': [{'type': 'text', 'text': '你好呀'}]},
            {'type': 'function_call', 'id': 'call_1', 'name': 'search', 'arguments': {'q': 1}},
            {'type': 'function_result', 'call_id': 'call_1', 'result': [{'type': 'text', 'text': '结果'}]},
        ],
    }
    messages = display_messages_for(body, 'interactions')
    assert messages[0] == {'role': 'system', 'content': '系统提示'}
    assert messages[1] == {'role': 'user', 'content': '你好'}
    assert messages[2] == {'role': 'assistant', 'content': '你好呀'}
    assert messages[3]['tool_calls'][0]['function']['name'] == 'search'
    assert messages[4] == {'role': 'tool', 'tool_call_id': 'call_1', 'content': '结果'}


def test_display_messages_chat_and_fallback():
    assert display_messages_for({'messages': [{'role': 'user', 'content': 'hi'}]}, 'chat') == [
        {'role': 'user', 'content': 'hi'}]
    assert display_messages_for({'generationConfig': {}}, 'gemini') == [
        {'role': 'user', 'content': {'generationConfig': {}}}]


def test_forward_records_display_messages_for_gemini():
    async def run():
        service = MagicMock()
        service.calculate_cost.return_value = {'total_cost': 0, 'currency': 'USD'}
        monitor = MagicMock()
        monitor.broadcast_to_monitors = AsyncMock()
        response = {'candidates': [{'content': {'parts': [{'text': 'answer'}]}}],
                    'usageMetadata': {'promptTokenCount': 1, 'candidatesTokenCount': 2}}

        async def upstream(**kwargs):
            yield json.dumps(response).encode()
        service.call_api_passthrough = upstream
        config = {'provider': 'gemini', 'api_type': 'gemini_native', 'api_key': 'test',
                  'api_base_url': 'https://example.test'}
        body = {'systemInstruction': {'parts': [{'text': '系统提示'}]},
                'contents': [{'role': 'user', 'parts': [{'text': '你好'}]},
                             {'role': 'model', 'parts': [{'text': '嗨'}]},
                             {'role': 'user', 'parts': [{'text': '问题'}]}]}
        await forward_native_exchange(body, config, 'alias', service, monitor, stream=False)
        started = monitor.request_start.call_args.kwargs
        assert started['messages_count'] == 4
        assert [message['role'] for message in started['messages']] == ['system', 'user', 'assistant', 'user']
        assert monitor.request_end.call_args.kwargs['full_messages'] == started['messages']
    asyncio.run(run())
