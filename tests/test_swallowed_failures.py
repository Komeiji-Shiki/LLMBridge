"""收尾和旁路逻辑的失败不能影响主流程，但也不能无声无息：必须留下记录或有回退。"""
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from core.request_context import RequestContext, current_request
from services.direct_api_service import DirectAPIService
from services.stream_processor import _handle_client_disconnect, _handle_non_stream_error, _handle_stream_error


def test_signature_archive_failure_is_logged_and_request_still_runs(caplog):
    upstream = MagicMock(status=200)
    upstream.json = AsyncMock(return_value={'candidates': [{'content': {'parts': [{'text': 'ok'}]}, 'finishReason': 'STOP'}]})
    session = MagicMock()
    session.post.return_value.__aenter__ = AsyncMock(return_value=upstream)
    session.post.return_value.__aexit__ = AsyncMock(return_value=False)

    async def run():
        context = RequestContext(authenticated=True)
        token = current_request.set(context)
        try:
            return [item async for item in DirectAPIService(session).call_gemini_native_api(
                'test-key', 'demo', [{'role': 'user', 'content': 'hi'}], stream=False)]
        finally:
            current_request.reset(token)

    with caplog.at_level(logging.WARNING), patch('core.conversation_store.conversation_store.get',
                                                 side_effect=OSError('签名库被锁')):
        result = asyncio.run(run())
    assert 'error' not in result[0]
    assert any('签名库被锁' in record.getMessage() for record in caplog.records)


def broken_estimator(*args, **kwargs):
    raise RuntimeError('分词器不可用')


MESSAGES = [{'role': 'user', 'content': '你好，世界' * 20}]


def monitor():
    service = MagicMock()
    service.broadcast_to_monitors = AsyncMock()
    return service


def test_stream_error_falls_back_to_rough_input_estimate():
    service = monitor()
    asyncio.run(_handle_stream_error('req12345', 'boom', ['答'], [], 'demo', service,
                                     broken_estimator, broken_estimator, None, full_messages=MESSAGES))
    end = service.request_end.call_args.kwargs
    assert end['input_tokens'] > 0 and end['output_tokens'] > 0


def test_non_stream_error_falls_back_to_rough_input_estimate():
    service = monitor()
    asyncio.run(_handle_non_stream_error('req12345', 'boom', ['答'], [], 'demo', service,
                                         broken_estimator, broken_estimator, MagicMock, full_messages=MESSAGES))
    end = service.request_end.call_args.kwargs
    assert end['input_tokens'] > 0 and end['output_tokens'] > 0


def test_client_disconnect_falls_back_to_rough_input_estimate():
    service = monitor()
    with patch('services.stream_processor._send_cancel_to_browser', new=AsyncMock()):
        asyncio.run(_handle_client_disconnect('req12345', RuntimeError('gone'), ['答'], [], 'demo', service,
                                              broken_estimator, broken_estimator, {}, {}, full_messages=MESSAGES))
    end = service.request_end.call_args.kwargs
    assert end['input_tokens'] > 0 and end['output_tokens'] > 0
