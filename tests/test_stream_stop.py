"""监控页手动收尾：上游发完内容却迟迟不结束时，管理员掐掉上游，客户端按正常结束拿到已有内容。"""
import asyncio

import httpx
import pytest
from fastapi import FastAPI

from core import stream_stop
from core.request_context import RequestContext, current_request
from protocol_harness import (
    CLIENT_PATHS, CLIENTS, MODEL, READERS, REPLY_BUILDERS, UPSTREAMS, Reply, Turn, build_gateway, exchange,
)
from routes._direct_api_utils import iter_with_keepalive

# 两端都是 Responses 时走原样透传，不在手动收尾的覆盖范围内
CELLS = [(client, upstream) for client in CLIENTS for upstream in UPSTREAMS
         if (client, upstream) != ('responses', 'responses')]
TURN = Turn(text='先查一下', calls=[('call_1', 'lookup', {'q': 1})], stop='tool')
# 各协议要去掉的结尾事件数：chat 是结束块、用量块和 [DONE]；anthropic 连最后一个块的 stop 也不发；
# interactions 停在最后一个调用的 step.stop 之前，和线上卡住的现场一致
TAIL = {'chat': 3, 'anthropic': 3, 'responses': 1, 'gemini': 1, 'interactions': 2}


@pytest.fixture(autouse=True)
def signals(monkeypatch):
    # 测试里的监控是桩，不会在请求结束时注销登记
    registry = {}
    monkeypatch.setattr(stream_stop, '_signals', registry)
    return registry


@pytest.fixture
def gateway(monkeypatch):
    app, monitor = build_gateway(monkeypatch)
    return app, monitor, monkeypatch


def request_for(client):
    if client == 'chat':
        return {'model': MODEL, 'stream': True, 'messages': [{'role': 'user', 'content': 'q'}]}
    if client == 'anthropic':
        return {'model': MODEL, 'stream': True, 'max_tokens': 100, 'messages': [{'role': 'user', 'content': 'q'}]}
    return {'model': MODEL, 'stream': True, 'input': 'q'}


def stalled_reply(upstream):
    events = REPLY_BUILDERS[upstream](TURN, True).events
    return Reply(events=events[:-TAIL[upstream]], stall=True)


@pytest.mark.parametrize('cell', CELLS, ids='-'.join)
def test_manual_finish_ends_stalled_stream_normally(gateway, cell, signals):
    client, upstream = cell
    app, monitor, monkeypatch = gateway

    async def finish_when_stalled(fake):
        await asyncio.wait_for(fake.stalled.wait(), 10)
        # 让网关把上游已经写出的事件转发完
        await asyncio.sleep(0.2)
        assert len(signals) == 1
        assert stream_stop.request_finish(next(iter(signals)))

    response, _ = exchange(app, monkeypatch, upstream, [stalled_reply(upstream)], CLIENT_PATHS[client],
                           request_for(client), alongside=finish_when_stalled)
    seen = READERS[client](response, True)
    assert response.status_code == 200 and seen.error is None
    assert (seen.text, seen.stop, seen.calls) == ('先查一下', 'tool', [('call_1', 'lookup', {'q': 1})])
    assert monitor.request_end.call_args.kwargs['success'] is True


def test_stop_cancels_pending_read_and_records_manual_finish():
    async def run():
        cancelled = asyncio.Event()

        async def silent():
            yield 'first'
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            yield 'never'

        context = RequestContext()
        token = current_request.set(context)
        try:
            stop = asyncio.Event()
            stream = iter_with_keepalive(silent(), 30, stop=stop)
            assert await anext(stream) == 'first'
            asyncio.get_running_loop().call_later(0.01, stop.set)
            rest = [item async for item in stream]
        finally:
            current_request.reset(token)
        return rest, cancelled.is_set(), context.outcome.get('stop_reason')

    assert asyncio.run(run()) == ([], True, stream_stop.MANUAL_STOP_REASON)


def test_active_list_offers_finish_only_while_registered(monkeypatch):
    from modules.monitoring import monitoring_service

    monkeypatch.setattr(monitoring_service.log_manager, 'write_request_log', lambda entry: None)
    monitoring_service.request_start('stop-active', 'demo')
    monitoring_service._event_pool.submit(lambda: None).result(timeout=5)
    stream_stop.register('stop-active')
    listed = {item['request_id']: item for item in monitoring_service.get_active_requests()}
    assert listed['stop-active']['finishable'] is True

    monitoring_service.request_end('stop-active', True)
    monitoring_service._event_pool.submit(lambda: None).result(timeout=5)
    assert not stream_stop.finishable('stop-active')
    assert not stream_stop.request_finish('stop-active')


def test_finish_endpoint_reports_requests_it_cannot_finish():
    from routes import monitor_routes

    app = FastAPI()
    app.include_router(monitor_routes.router)
    stop = stream_stop.register('stop-endpoint')

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://gateway') as client:
            finished = await client.post('/api/monitor/active/stop-endpoint/finish')
            unknown = await client.post('/api/monitor/active/never-started/finish')
        return finished, unknown

    finished, unknown = asyncio.run(run())
    assert finished.status_code == 200 and stop.is_set()
    assert unknown.status_code == 409
