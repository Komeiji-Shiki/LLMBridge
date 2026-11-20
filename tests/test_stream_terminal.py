"""流式请求的结束记录：各条链路在客户端中途断开时都只记一次，原因统一是 Client disconnected。"""
import pytest

from core import stream_stop
from protocol_harness import CLIENT_PATHS, CLIENTS, MODEL, REPLY_BUILDERS, UPSTREAMS, Reply, Turn, build_gateway, disconnect

CELLS = [(client, upstream) for client in CLIENTS for upstream in UPSTREAMS]
TURN = Turn(reasoning='想一想', text='先写一段比较长的回答', stop='end')
# 各协议结尾事件的个数，与手动收尾测试一致
TAIL = {'chat': 3, 'anthropic': 3, 'responses': 1, 'gemini': 1, 'interactions': 2}


@pytest.fixture(autouse=True)
def signals(monkeypatch):
    monkeypatch.setattr(stream_stop, '_signals', {})


def request_for(client):
    if client == 'chat':
        return {'model': MODEL, 'stream': True, 'messages': [{'role': 'user', 'content': 'q'}]}
    if client == 'anthropic':
        return {'model': MODEL, 'stream': True, 'max_tokens': 100, 'messages': [{'role': 'user', 'content': 'q'}]}
    return {'model': MODEL, 'stream': True, 'input': 'q'}


@pytest.mark.parametrize('cell', CELLS, ids='-'.join)
def test_client_disconnect_mid_stream_is_recorded_once(monkeypatch, cell):
    client, upstream = cell
    app, monitor = build_gateway(monkeypatch)
    # 上游发到一半停住，客户端收到第一块就走
    events = REPLY_BUILDERS[upstream](TURN, True).events
    received, _ = disconnect(app, monkeypatch, upstream, Reply(events=events[:3], stall=True),
                             CLIENT_PATHS[client], request_for(client))
    assert received
    assert monitor.request_end.call_count == 1, monitor.request_end.call_args_list
    kwargs = monitor.request_end.call_args.kwargs
    assert kwargs['success'] is False
    assert kwargs['error'].startswith('Client disconnected'), kwargs['error']


def _fast_heartbeat(monkeypatch, seconds):
    from core import request_middleware
    monkeypatch.setattr(request_middleware, 'resolve_heartbeat_interval', lambda endpoint_config=None: seconds)


def test_heartbeat_waits_for_event_boundary(monkeypatch):
    """上游停在半个事件中间时不插心跳，事件写完之后再补。"""
    import asyncio
    import httpx
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse
    from core.request_middleware import GatewayRequestMiddleware

    async def body():
        yield b'data: {"half": '
        await asyncio.sleep(0.4)
        yield b'1}\n\n'
        await asyncio.sleep(0.4)
        yield b'data: [DONE]\n\n'

    app = FastAPI()
    app.add_middleware(GatewayRequestMiddleware)
    app.post('/v1/stream')(lambda: StreamingResponse(body(), media_type='text/event-stream'))
    _fast_heartbeat(monkeypatch, 0.1)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway') as client:
            return (await client.post('/v1/stream')).text

    text = asyncio.run(run())
    first, rest = text.split('\n\n', 1)
    assert first == 'data: {"half": 1}'
    assert rest.startswith(': keep-alive\n\n') and rest.endswith('data: [DONE]\n\n')


@pytest.mark.parametrize('cell', CELLS, ids='-'.join)
def test_silent_upstream_gets_keepalives_and_content_survives(monkeypatch, cell):
    from protocol_harness import READERS, exchange
    client, upstream = cell
    app, monitor = build_gateway(monkeypatch)
    _fast_heartbeat(monkeypatch, 0.1)
    events = REPLY_BUILDERS[upstream](TURN, True).events
    # 内容发完、结束事件之前停住：响应早已开始，只能靠心跳保活
    response, _ = exchange(app, monkeypatch, upstream, [Reply(events=events, pause=(len(events) - TAIL[upstream], 0.6))],
                           CLIENT_PATHS[client], request_for(client))
    seen = READERS[client](response, True)
    assert (seen.text, seen.error) == ('先写一段比较长的回答', None)
    lines = response.text.splitlines()
    assert any(line.startswith(':') or line == 'event: ping' for line in lines), response.text[:400]
    assert monitor.request_end.call_args.kwargs['success'] is True
