import asyncio
import json
from unittest.mock import AsyncMock, MagicMock
import pytest
from core.conversation_store import ConversationStore
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


@pytest.mark.parametrize('stream', [True, False])
@pytest.mark.parametrize('reason', ['STOP', 'MAX_TOKENS', 'PROHIBITED_CONTENT'])
def test_gemini_stop_reason_reaches_monitor_and_storage(tmp_path, monkeypatch, stream, reason):
    from modules import monitoring
    from modules.monitoring_sqlite import SQLiteLogger

    store = SQLiteLogger(tmp_path / 'requests.db')
    log_manager = MagicMock()
    log_manager.write_request_log.side_effect = store.write_request
    monkeypatch.setattr(monitoring, 'LogManager', lambda: log_manager)
    monkeypatch.setattr(monitoring.MonitoringService, '_load_persisted_stats', lambda self: None)
    monitor = monitoring.MonitoringService()
    monitor.broadcast_to_monitors = AsyncMock()
    blocked = reason == 'PROHIBITED_CONTENT'

    async def run():
        context = RequestContext()
        token = current_request.set(context)
        service = MagicMock()
        service.calculate_cost.return_value = {'total_cost': 0, 'currency': 'USD'}
        response = {'candidates': [{'finishReason': reason, 'finishMessage': 'provider explanation'}],
                    'usageMetadata': {'promptTokenCount': 3, 'thoughtsTokenCount': 4}}

        async def upstream(**kwargs):
            if stream:
                thought = {'candidates': [{'content': {'parts': [{'text': 'thinking', 'thought': True}]}}]}
                yield ('data: ' + json.dumps(thought) + '\n\n').encode()
                yield ('data: ' + json.dumps(response) + '\n\n').encode()
            else:
                yield json.dumps(response).encode()

        service.call_api_passthrough = upstream
        config = {'api_type': 'gemini_native', 'api_key': 'test', 'api_base_url': 'https://example.test/v1'}
        try:
            result = await forward_native_exchange({'contents': []}, config, 'gemini', service, monitor,
                                                   stream=stream, context=context)
            if stream and blocked:
                chunks = []
                async for chunk in result.body_iterator:
                    chunks.append(chunk)
                    if b'upstream_generation_error' in chunk:
                        # 客户端读到错误后立即关闭，也不能覆盖真实拦截原因。
                        await result.body_iterator.aclose()
                        break
                raw = b''.join(chunks)
            else:
                raw = b''.join([chunk async for chunk in result.body_iterator]) if stream else result.body
            # 等待现有串行监控队列完成，检查实际缓存和数据库查询结果。
            monitor._event_pool.submit(lambda: None).result(timeout=5)
            record = log_manager.write_request_log.call_args.args[0]
            request_id = record['request_id']
            for saved in (record, monitor.request_details_cache[request_id],
                          monitor.recent_requests[-1], store.get_request_details(request_id),
                          store.query_requests()['items'][0]):
                assert saved['stop_reason'] == reason
                assert saved['status'] == ('failed' if blocked else 'success')
            assert record['success'] is not blocked
            assert record['output_tokens'] == 4
            if blocked:
                assert b'upstream_generation_error' in raw
                assert 'PROHIBITED_CONTENT' in record['error']
                assert 'provider explanation' in record['error']
                if not stream:
                    assert result.status_code == 422
            else:
                assert b'upstream_generation_error' not in raw
        finally:
            current_request.reset(token)

    try:
        asyncio.run(run())
    finally:
        monitor._event_pool.shutdown(wait=True)
        monitor._monitor_pool.shutdown(wait=True)
        store.close()


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


def test_display_messages_interactions_string_keeps_system_and_input():
    assert display_messages_for({'system_instruction': '系统', 'input': '用户问题'}, 'interactions') == [
        {'role': 'system', 'content': '系统'}, {'role': 'user', 'content': '用户问题'}]


def test_display_messages_gemini_preserves_tool_features():
    from utils.request_features import request_features
    body = {'contents': [
        {'role': 'model', 'parts': [
            {'text': '查询'}, {'functionCall': {'id': 'c1', 'name': 'lookup', 'args': {'q': 1}},
                             'thoughtSignature': 'sig'}]},
        {'role': 'user', 'parts': [
            {'functionResponse': {'id': 'c1', 'name': 'lookup', 'response': {'answer': '结果'}}}]}]}
    messages = display_messages_for(body, 'gemini')
    assert messages[0]['content'] == '查询'
    assert messages[0]['tool_calls'][0]['function'] == {'name': 'lookup', 'arguments': '{"q": 1}'}
    assert messages[0]['reasoning_signature'] == 'sig'
    assert messages[1] == {'role': 'tool', 'tool_call_id': 'c1', 'name': 'lookup',
                           'content': '{"answer": "结果"}'}
    assert request_features({'request_messages': messages})['has_tool_calls'] is True


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


def test_display_messages_gemini_thoughts_and_signatures():
    body = {'contents': [
        {'role': 'model', 'parts': [{'thought': True, 'text': '想一下'}, {'text': '答案'},
                                    {'text': '', 'thoughtSignature': 'sig-1'}]},
        {'role': 'user', 'parts': [{'text': '继续'}]},
    ]}
    messages = display_messages_for(body, 'gemini')
    assert messages[0]['content'] == '答案'
    assert messages[0]['reasoning_content'] == '想一下'
    assert messages[0]['reasoning_signature'] == 'sig-1'
    assert messages[1] == {'role': 'user', 'content': '继续'}


@pytest.mark.parametrize('stream', [True, False])
def test_forward_native_gemini_restores_thought_signature(stream, monkeypatch, tmp_path):
    async def run():
        store = ConversationStore(tmp_path / 'conversations.db')
        monkeypatch.setattr('services.gemini_history.conversation_store', store)
        service = MagicMock()
        service.calculate_cost.return_value = {'total_cost': 0, 'currency': 'USD'}
        monitor = MagicMock()
        monitor.broadcast_to_monitors = AsyncMock()
        captured = []

        full_response = {'candidates': [{'content': {'role': 'model',
                                                     'parts': [{'thought': True, 'text': '想了半天'},
                                                               {'text': '你好呀'},
                                                               {'text': '', 'thoughtSignature': 'sig-1'}]},
                                    'finishReason': 'STOP'}],
                         'usageMetadata': {'promptTokenCount': 3, 'candidatesTokenCount': 4}}
        events = [
            {'candidates': [{'content': {'role': 'model', 'parts': [{'thought': True, 'text': '想了半天'}]}}]},
            {'candidates': [{'content': {'role': 'model', 'parts': [{'text': '你好呀'}]}}]},
            {'candidates': [{'content': {'role': 'model', 'parts': [{'text': '', 'thoughtSignature': 'sig-1'}]},
                             'finishReason': 'STOP'}],
             'usageMetadata': {'promptTokenCount': 3, 'candidatesTokenCount': 4}},
        ] if stream else [full_response]

        async def upstream(**kwargs):
            captured.append(kwargs['request_body'])
            for event in events:
                if stream:
                    yield ('data: ' + json.dumps(event) + '\n\n').encode()
                else:
                    yield json.dumps(event).encode()
        service.call_api_passthrough = upstream
        config = {'provider': 'gemini', 'api_type': 'gemini_native', 'api_key': 'test',
                  'api_base_url': 'https://example.test'}

        async def send(contents):
            token = current_request.set(RequestContext(authenticated=True, owner_id='owner'))
            try:
                result = await forward_native_exchange({'contents': contents}, config, 'alias',
                                                       service, monitor, stream=stream)
                if stream:
                    _ = b''.join([chunk async for chunk in result.body_iterator])
            finally:
                current_request.reset(token)

        await send([{'role': 'user', 'parts': [{'text': '你好'}]}])
        await send([{'role': 'user', 'parts': [{'text': '你好'}]},
                    {'role': 'model', 'parts': [{'thought': True, 'text': '想了半天'}, {'text': '你好呀'}]},
                    {'role': 'user', 'parts': [{'text': '继续'}]}])
        restored_parts = captured[-1]['contents'][1]['parts']
        assert restored_parts == full_response['candidates'][0]['content']['parts']
        params = monitor.request_start.call_args.kwargs['params']
        assert params['restored_thought_signatures'] == 1
    asyncio.run(run())
