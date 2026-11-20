"""流式请求的结束记录：各条链路在客户端中途断开时都只记一次，原因统一是 Client disconnected。"""
import pytest

from core import stream_stop
from protocol_harness import CLIENT_PATHS, CLIENTS, MODEL, REPLY_BUILDERS, UPSTREAMS, Reply, Turn, build_gateway, disconnect

CELLS = [(client, upstream) for client in CLIENTS for upstream in UPSTREAMS]
TURN = Turn(reasoning='想一想', text='先写一段比较长的回答', stop='end')


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

