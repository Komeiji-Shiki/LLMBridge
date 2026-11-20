"""上游请求体压缩：按渠道设置压缩发送，上游拒收就在同一次尝试里改发明文并记住。"""
import asyncio
import gzip
import json

import aiohttp
import brotli
import pytest
import zstandard
from aiohttp import web
from aiohttp.test_utils import TestServer

from core.request_context import RequestContext, current_request
from services import upstream_compression
from services.direct_api_service import DirectAPIService

BODY = {'model': 'm', 'stream': False, 'messages': [{'role': 'user', 'content': '长对话历史 ' * 2000}]}
DECODERS = {'zstd': lambda data: zstandard.ZstdDecompressor().decompress(data), 'br': brotli.decompress,
            'gzip': gzip.decompress, None: lambda data: data}
DECODE_ERROR = {'error': {'code': 400, 'message': 'Invalid JSON payload received. Unexpected token.'}}


@pytest.fixture(autouse=True)
def fresh_memory(monkeypatch):
    monkeypatch.setattr(upstream_compression, '_rejected', {})


def call(endpoint, respond, times=1):
    """对同一个本地假上游依次发 times 次直通请求。

    返回 (上游收到的 [(编码, 解码后的正文)], 最后一次请求的尝试记录, 最后一次客户端拿到的字节)。
    """
    received = []

    async def handle(request):
        encoding = request.headers.get('Content-Encoding')
        body = json.loads(DECODERS[encoding](await request.read()))
        received.append((encoding, body))
        status, payload = respond(encoding, len(received))
        return web.json_response(payload, status=status)

    async def run():
        app = web.Application(handler_args={'auto_decompress': False})
        app.router.add_post('/v1/chat/completions', handle)
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession() as session:
                for _ in range(times):
                    context = RequestContext(model='demo', endpoint={'api_type': 'direct_api', **endpoint})
                    token = current_request.set(context)
                    try:
                        chunks = [chunk async for chunk in DirectAPIService(session).call_api_passthrough(
                            base_url=str(server.make_url('/v1')), api_key='k', request_body=dict(BODY),
                            endpoint_path='/chat/completions')]
                    finally:
                        current_request.reset(token)
        finally:
            await server.close()
        return received, context.attempts, b''.join(chunks)

    return asyncio.run(run())


def ok(encoding, count):
    return 200, {'id': 'r', 'choices': [{'message': {'content': 'hi'}}]}


@pytest.mark.parametrize('algorithm', ['zstd', 'br', 'gzip'])
def test_body_is_compressed_and_upstream_reads_it_back(algorithm):
    received, attempts, _ = call({'upstream_compression': algorithm}, ok)
    assert received == [(algorithm, BODY)]
    report = attempts[-1]['upstream_compression']
    assert report['status'] == 'compressed' and report['sent_bytes'] < report['raw_bytes'] / 5


def test_rejected_compression_is_resent_plain_and_remembered():
    def picky(encoding, count):
        return (400, DECODE_ERROR) if encoding else ok(encoding, count)

    received, attempts, output = call({'upstream_compression': 'zstd'}, picky)
    assert [encoding for encoding, _ in received] == ['zstd', None]
    assert json.loads(output)['choices'][0]['message']['content'] == 'hi'
    assert attempts[-1]['upstream_compression']['status'] == 'fallback_ok'
    # 记住之后同一个地址直接发明文，不再先压缩试一次
    received, attempts, _ = call({'upstream_compression': 'zstd'}, picky, times=2)
    assert [encoding for encoding, _ in received] == ['zstd', None, None]
    assert attempts[-1]['upstream_compression']['status'] == 'remembered_unsupported'


def test_real_400_is_not_resent():
    def too_long(encoding, count):
        return 400, {'error': {'code': 400, 'message': 'prompt is too long: 300000 tokens > 200000 maximum'}}

    received, attempts, output = call({'upstream_compression': 'gzip'}, too_long)
    assert len(received) == 1 and 'prompt is too long' in output.decode()
    assert attempts[-1]['upstream_compression']['status'] == 'compressed'
    assert upstream_compression._rejected == {}


def test_off_and_small_bodies_are_sent_plain(monkeypatch):
    received, attempts, _ = call({}, ok)
    assert received == [(None, BODY)] and 'upstream_compression' not in attempts[-1]
    monkeypatch.setitem(upstream_compression._DEFAULTS, 'min_bytes', 10 ** 7)
    received, attempts, _ = call({'upstream_compression': 'zstd'}, ok)
    assert received == [(None, BODY)] and attempts[-1]['upstream_compression']['status'] == 'below_min'
