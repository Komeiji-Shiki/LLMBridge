"""Gemini 路由的失败上报：每次失败只记录一次、必须带原因，客户端也要看得到。"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from converters.anthropic_openai import build_anthropic_streaming_response
from routes._direct_api_gemini import handle_gemini_native_direct
from routes._direct_api_utils import iter_with_keepalive
from services.direct_api_service import DirectAPIService


def chunk(text=None, finish=None, token=None, usage=None):
    candidate = {}
    if text is not None:
        candidate['content'] = {'role': 'model', 'parts': [{'text': text}]}
    if finish:
        candidate['finishReason'] = finish
    if token:
        candidate['continuationToken'] = token
    value = {'candidates': [candidate]}
    if usage:
        value['usageMetadata'] = usage
    return value


def route(upstream, *, stream=True, session=None, pricing=None, config=None, endpoint=None):
    service = DirectAPIService(session or MagicMock())
    if upstream is not None:
        service.call_gemini_native_api = upstream
    monitor = MagicMock()
    monitor.broadcast_to_monitors = AsyncMock()
    call = handle_gemini_native_direct(
        {'messages': [{'role': 'user', 'content': 'hi'}], 'stream': stream}, 'demo', 'demo', 'demo',
        'test-key', None, endpoint or {}, pricing or {}, monitor, service,
        lambda messages, model=None: 7, lambda text, model=None: 3, full_messages=[], CONFIG=config)
    return call, monitor


async def drain(response):
    return [item async for item in response.body_iterator]


def only_end(monitor):
    """一次请求只能落盘一次，否则第二次会被丢弃并留下“未找到请求”告警。"""
    assert monitor.request_end.call_count == 1
    return monitor.request_end.call_args.kwargs


def events(output):
    return [json.loads(item[len('data: '):]) for item in output
            if item.startswith('data: ') and '[DONE]' not in item]


def test_upstream_exception_reaches_both_monitor_and_client():
    async def upstream(**kwargs):
        yield chunk('部分')
        raise RuntimeError('转换炸了')

    async def run():
        call, monitor = route(upstream)
        return await drain(await call), monitor

    output, monitor = asyncio.run(run())
    end = only_end(monitor)
    assert end['success'] is False and end['error'] == '转换炸了'
    assert end['response_content'] == '部分'
    assert events(output)[-2]['error'] == {'message': '转换炸了', 'type': 'internal_error'}
    assert output[-1] == 'data: [DONE]\n\n'


def test_malformed_chunk_is_reported_instead_of_lost():
    async def upstream(**kwargs):
        yield {'candidates': [{'content': {'parts': [{'functionCall': None}]}}]}

    async def run():
        call, monitor = route(upstream)
        return await drain(await call), monitor

    output, monitor = asyncio.run(run())
    assert only_end(monitor)['success'] is False
    assert any(event.get('error', {}).get('type') == 'internal_error' for event in events(output))


def test_first_chunk_timeout_records_once_with_reason():
    async def upstream(**kwargs):
        await asyncio.sleep(30)
        yield chunk('来不及')

    async def run():
        call, monitor = route(upstream, config={'first_chunk_timeout_seconds': 0.01},
                              endpoint={'client_disconnect_probe_interval': 0.01})
        with patch('routes._direct_api_gemini.iter_with_keepalive',
                   lambda iterator, interval: iter_with_keepalive(iterator, 0.02)):
            return await drain(await call), monitor

    output, monitor = asyncio.run(run())
    end = only_end(monitor)
    assert end['success'] is False and '未返回第一个数据块' in end['error']
    assert end['input_tokens'] == 7
    assert events(output)[-1]['error']['type'] == 'timeout'


def test_empty_stream_records_once_with_reason():
    async def upstream(**kwargs):
        return
        yield

    async def run():
        call, monitor = route(upstream)
        return await drain(await call), monitor

    output, monitor = asyncio.run(run())
    assert '空响应' in only_end(monitor)['error']
    assert events(output)[-1]['error']['type'] == 'empty_response'


def test_first_chunk_upstream_error_records_once_with_reason():
    async def upstream(**kwargs):
        yield {'error': {'message': '额度用尽', 'code': 429}}

    async def run():
        call, monitor = route(upstream)
        return await drain(await call), monitor

    output, monitor = asyncio.run(run())
    assert '额度用尽' in only_end(monitor)['error']
    assert output[-1] == 'data: [DONE]\n\n'


def run_until_closed(upstream, chunks_before_close):
    closed = {}

    async def tracked(**kwargs):
        try:
            async for item in upstream():
                yield item
        finally:
            closed['upstream'] = True

    async def run():
        call, monitor = route(tracked)
        body = (await call).body_iterator
        seen = 0
        async for item in body:
            if item.startswith('data: '):
                seen += 1
                if seen == chunks_before_close:
                    break
        await body.aclose()
        # 必须在事件循环收尾前取值：asyncio.run 退出时会自动关闭遗留的生成器
        return monitor, dict(closed)

    return asyncio.run(run())


def test_client_disconnect_mid_stream_is_a_failure_with_reason():
    async def upstream():
        yield chunk('一半')
        yield chunk('另一半', 'STOP')

    monitor, closed = run_until_closed(upstream, 1)
    end = only_end(monitor)
    assert end['success'] is False and end['error'] == 'Client disconnected'
    assert end['response_content'] == '一半'
    assert closed.get('upstream') is True


def test_client_closing_after_finish_reason_is_success():
    async def upstream():
        yield chunk('完整', 'STOP')
        yield chunk('')

    monitor, _ = run_until_closed(upstream, 1)
    end = only_end(monitor)
    assert end['success'] is True and end['error'] is None


def test_client_closing_after_safety_block_stays_a_failure():
    error = {'code': 422, 'reason': 'SAFETY', 'message': 'Gemini 上游终止生成：SAFETY'}

    async def upstream():
        yield chunk('一半', 'SAFETY')
        yield {'error': error}

    # 第二个 data 块就是错误事件，转换层转发后立刻关流
    monitor, _ = run_until_closed(upstream, 2)
    end = only_end(monitor)
    assert end['success'] is False and end['error'] == str(error)
    assert end['response_content'] == '一半'


def test_cost_bills_every_hop_but_tier_follows_single_request():
    usage = {'promptTokenCount': 100, 'candidatesTokenCount': 10, 'thoughtsTokenCount': 0, 'totalTokenCount': 110,
             'billedPromptTokenCount': 300, 'billedCachedTokenCount': 200, 'continuationHops': 2}
    pricing = {'input': 1, 'output': 1, 'cached_input': 0, 'unit': 1,
               'input_tiers': [{'min_input_tokens': 150, 'input': 100}]}

    async def upstream(**kwargs):
        yield chunk('完整', 'STOP', usage=usage)

    async def run():
        call, monitor = route(upstream, pricing=pricing)
        await drain(await call)
        return monitor

    end = only_end(asyncio.run(run()))
    assert end['input_tokens'] == 100
    assert end['cost_info']['input_cost'] == 100
    assert end['cost_info']['output_cost'] == 10


def test_non_stream_upstream_error_keeps_usage_and_reason():
    usage = {'promptTokenCount': 50, 'candidatesTokenCount': 5, 'thoughtsTokenCount': 1}

    async def upstream(**kwargs):
        yield {'error': {'message': '被拦截', 'code': 422}, 'usageMetadata': usage}

    async def run():
        call, monitor = route(upstream, stream=False)
        return await call, monitor

    response, monitor = asyncio.run(run())
    end = only_end(monitor)
    assert response.status_code == 422
    assert '被拦截' in end['error']
    assert end['input_tokens'] == 50 and end['output_tokens'] == 6
    assert end['upstream_usage'] == usage


def test_non_stream_exception_records_reason():
    async def upstream(**kwargs):
        raise RuntimeError('连接炸了')
        yield

    async def run():
        call, monitor = route(upstream, stream=False)
        with pytest.raises(HTTPException) as caught:
            await call
        return caught.value, monitor

    error, monitor = asyncio.run(run())
    end = only_end(monitor)
    assert error.status_code == 500
    assert end['error'] == '连接炸了' and end['input_tokens'] == 7


def test_keepalive_signals_idle_and_cancels_pending_read_on_close():
    async def run():
        state = {}

        async def slow():
            yield 'first'
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                state['cancelled'] = True
                raise
            yield 'never'

        stream = iter_with_keepalive(slow(), 0.01)
        assert await anext(stream) == 'first'
        assert await anext(stream) is None
        await stream.aclose()
        return state

    assert asyncio.run(run())['cancelled'] is True


def test_keepalive_propagates_upstream_errors():
    async def broken():
        raise ValueError('上游错误')
        yield

    async def run():
        return [item async for item in iter_with_keepalive(broken(), 1)]

    with pytest.raises(ValueError):
        asyncio.run(run())


def anthropic_events(session):
    async def run():
        call, monitor = route(None, session=session)
        wrapped = build_anthropic_streaming_response(await call, 'demo')
        raw = [item async for item in wrapped.body_iterator]
        return raw, monitor

    raw, monitor = asyncio.run(run())
    text = ''.join(item.decode() if isinstance(item, bytes) else item for item in raw)
    parsed = [json.loads(line[len('data: '):]) for line in text.splitlines() if line.startswith('data: ')]
    return parsed, monitor


def gemini_session(*hops):
    responses = []
    for hop in hops:
        response = MagicMock(status=200)

        async def stream(events=hop):
            for event in events:
                yield ('data: ' + json.dumps(event) + '\n\n').encode()

        response.content.iter_any = stream
        responses.append(response)
    session = MagicMock()
    session.post.return_value.__aenter__ = AsyncMock(side_effect=responses)
    session.post.return_value.__aexit__ = AsyncMock(return_value=False)
    return session


def test_claude_code_sees_resumed_response_as_one_complete_message():
    usage_1 = {'promptTokenCount': 100, 'candidatesTokenCount': 40, 'totalTokenCount': 140}
    usage_2 = {'promptTokenCount': 100, 'candidatesTokenCount': 2, 'totalTokenCount': 102}
    session = gemini_session(
        [chunk('前半', 'CONTINUATION', 'token-1', usage_1)],
        [chunk('后半', 'STOP', usage=usage_2)],
    )
    parsed, monitor = anthropic_events(session)

    assert session.post.call_count == 2
    text = ''.join(event['delta']['text'] for event in parsed
                   if event.get('type') == 'content_block_delta' and event['delta'].get('type') == 'text_delta')
    assert text == '前半后半'
    assert [event['delta']['stop_reason'] for event in parsed if event.get('type') == 'message_delta'] == ['end_turn']
    assert sum(event.get('type') == 'message_stop' for event in parsed) == 1
    end = only_end(monitor)
    assert end['success'] is True and end['output_tokens'] == 42 and end['input_tokens'] == 100


def test_keepalive_reaches_anthropic_client_during_silence():
    async def upstream(**kwargs):
        await asyncio.sleep(0.1)
        yield chunk('想好了', 'STOP')

    async def run():
        call, _ = route(upstream)
        with patch('routes._direct_api_gemini.iter_with_keepalive',
                   lambda iterator, interval: iter_with_keepalive(iterator, 0.01)):
            wrapped = build_anthropic_streaming_response(await call, 'demo')
            return [item async for item in wrapped.body_iterator]

    raw = asyncio.run(run())
    first_event = next(index for index, item in enumerate(raw) if item.startswith('event:'))
    assert first_event > 1 and all(item == ': keep-alive\n\n' for item in raw[:first_event])
    assert raw[first_event].startswith('event: message_start')


def test_claude_code_sees_unresumable_continuation_as_max_tokens():
    parsed, _ = anthropic_events(gemini_session([chunk('被截断', 'CONTINUATION')]))
    assert [event['delta']['stop_reason'] for event in parsed if event.get('type') == 'message_delta'] == ['max_tokens']


def test_tool_calls_across_hops_stay_distinct():
    call = lambda call_id: {'candidates': [{'content': {'parts': [
        {'functionCall': {'id': call_id, 'name': 'Read', 'args': {'file_path': call_id}}}]}}]}
    first_stop = call('one')
    first_stop['candidates'][0].update(finishReason='CONTINUATION', continuationToken='token-1')
    second_stop = call('two')
    second_stop['candidates'][0]['finishReason'] = 'STOP'

    parsed, monitor = anthropic_events(gemini_session([first_stop], [second_stop]))
    starts = [event['content_block'] for event in parsed if event.get('type') == 'content_block_start'
              and event['content_block'].get('type') == 'tool_use']
    assert [block['id'] for block in starts] == ['one', 'two']
    assert [event['delta']['stop_reason'] for event in parsed if event.get('type') == 'message_delta'] == ['tool_use']
    assert len(only_end(monitor)['response_tool_calls']) == 2


def test_passthrough_first_chunk_error_keeps_upstream_detail():
    from routes._direct_api_passthrough import _handle_passthrough_stream

    async def upstream(**kwargs):
        yield b'data: {"error": {"message": "\xe4\xb8\x8a\xe6\xb8\xb8\xe7\x9c\x9f\xe5\xae\x9e\xe5\x8e\x9f\xe5\x9b\xa0", "code": 503}}\n\n'

    async def run():
        service = MagicMock()
        service.call_api_passthrough = upstream
        service.calculate_cost.return_value = {}
        monitor = MagicMock()
        monitor.broadcast_to_monitors = AsyncMock()
        with patch('routes._direct_api_passthrough.detect_first_chunk_error', return_value=(False, None)), \
                pytest.raises(HTTPException) as caught:
            await _handle_passthrough_stream(
                {}, {'messages': []}, 'request', 'demo', 'demo', 'https://example.test', 'key', {}, {}, None,
                '/chat/completions', monitor, service, lambda messages, model=None: 0, lambda text, model=None: 0,
                [], {})
        return caught.value, monitor

    error, monitor = asyncio.run(run())
    assert error.detail == '上游真实原因'
    assert only_end(monitor)['error'] == '上游真实原因'


def test_cancelled_stream_records_reason_and_reraises():
    async def upstream(**kwargs):
        yield chunk('一半')
        await asyncio.sleep(30)
        yield chunk('永远等不到', 'STOP')

    async def run():
        call, monitor = route(upstream)
        body = (await call).body_iterator
        started = asyncio.Event()

        async def consume():
            async for item in body:
                if item.startswith('data: '):
                    started.set()

        task = asyncio.create_task(consume())
        await started.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return monitor

    end = only_end(asyncio.run(run()))
    assert end['success'] is False and end['error'] == 'Client disconnected'


def test_monitor_never_stores_failure_without_reason(monkeypatch):
    from modules.monitoring import monitoring_service

    writes = MagicMock()
    monkeypatch.setattr(monitoring_service.log_manager, 'write_request_log', writes)
    monitoring_service.request_start('reasonless-failure', 'demo')
    monitoring_service.request_end('reasonless-failure', False)
    monitoring_service._event_pool.submit(lambda: None).result(timeout=5)
    assert writes.call_args.args[0]['error'] == '网关未记录失败原因'
