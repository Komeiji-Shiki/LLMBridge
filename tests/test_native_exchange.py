import asyncio
import json
from unittest.mock import AsyncMock, MagicMock
import pytest
from core.request_context import RequestContext, current_request
from services.native_exchange import forward_native_exchange


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
