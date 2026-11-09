"""并发查询、慢监控连接、日志尾读与关闭行为验证。"""
import asyncio
import gzip
import json
from datetime import datetime
from unittest.mock import AsyncMock

from modules.monitor_broadcast import MonitorBroadcaster
from utils.async_singleflight import AsyncSingleFlight
from utils.jsonl_tail import reverse_lines
from utils.task_registry import cancel_background_tasks, pending_task_count, spawn


def test_singleflight_preserves_query_when_one_waiter_cancels():
    async def run():
        flight = AsyncSingleFlight()
        started, finish = asyncio.Event(), asyncio.Event()
        calls = 0
        async def query():
            nonlocal calls
            calls += 1
            started.set()
            await finish.wait()
            return 42
        waiters = [asyncio.create_task(flight.run('same', query)) for _ in range(20)]
        await started.wait()
        waiters[0].cancel()
        finish.set()
        results = await asyncio.gather(*waiters, return_exceptions=True)
        assert calls == 1
        assert results[1:] == [42] * 19
        flight.invalidate()
        assert await flight.run('same', query) == 42
        assert calls == 2
    asyncio.run(run())


def test_monitor_broadcast_is_ordered_and_bounded():
    async def run():
        class Client:
            def __init__(self):
                self.values = []
                self.close = AsyncMock()
                self.done = asyncio.Event()
            async def send_json(self, value):
                await asyncio.sleep(0)
                self.values.append(value)
                if len(self.values) == 10:
                    self.done.set()
        client = Client()
        clients = {client}
        broadcast = MonitorBroadcaster(clients, timeout=.2)
        for value in range(10):
            broadcast.publish(value)
        assert len(broadcast._workers) == 1
        await asyncio.wait_for(client.done.wait(), timeout=1)
        assert client.values == list(range(10))
        broadcast.remove(client)
        await asyncio.sleep(0)
        slow = Client()
        clients.add(slow)
        for value in range(1000):
            broadcast.publish(value)
        assert slow not in clients
        assert not broadcast._workers
        await asyncio.sleep(.01)
        slow.close.assert_awaited_once_with(code=1013)
        await cancel_background_tasks()
    asyncio.run(run())


def test_shutdown_waits_for_registered_task_cleanup():
    async def run():
        started, stopped = asyncio.Event(), asyncio.Event()
        async def worker():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                stopped.set()
        spawn(worker(), name='test-worker')
        await started.wait()
        await cancel_background_tasks()
        assert stopped.is_set()
        assert pending_task_count() == 0
    asyncio.run(run())


def test_tail_reader_handles_utf8_and_large_lines(tmp_path):
    path = tmp_path / 'events.jsonl'
    values = ['旧记录' * 10000, '完整中文行', '末尾没有换行']
    path.write_text('\n'.join(values), encoding='utf-8')
    assert [line.decode() for line in reverse_lines(path, block_size=7)] == values[::-1]
    assert next(reverse_lines(path, block_size=64)).decode() == values[-1]


def test_hierarchical_error_list_excludes_successful_logs(tmp_path, monkeypatch):
    from modules.monitoring import LogManager, MonitorConfig
    monkeypatch.setattr(MonitorConfig, 'LOG_DIR', tmp_path)
    folder = tmp_path / datetime.now().strftime('%Y%m%d/%H')
    folder.mkdir(parents=True)
    for index, row in enumerate([{'type': 'request_end', 'success': True},
                                  {'type': 'request_end', 'error': 'failed', 'success': False},
                                  {'error': 'legacy failure'}]):
        with gzip.open(folder / f'{index}.json.gz', 'wt', encoding='utf-8') as stream:
            json.dump(row, stream)
    reader = object.__new__(LogManager)
    logs = reader._read_hierarchical_logs('error', limit=20)
    assert len(logs) == 2
    assert all(row.get('error') for row in logs)


def test_message_token_count_does_not_rescan_the_vocabulary(monkeypatch):
    from modules.token_counter import _counting
    from modules.token_counter._tokenizers import LocalFastTokenizer

    class Backend:
        vocab_scans = 0
        def encode(self, text, add_special_tokens=True):
            return type('Encoding', (), {'ids': text.split()})()
        def get_vocab_size(self):
            Backend.vocab_scans += 1
            return 262144

    tokenizer = LocalFastTokenizer('gemma', Backend())
    monkeypatch.setattr(_counting, 'get_tokenizer_for_model', lambda model: 'google')
    monkeypatch.setattr(_counting, 'get_gemma_tokenizer', lambda: tokenizer)
    messages = [{'role': 'user', 'content': 'one two three'}] * 50
    total, _ = _counting.count_messages_tokens(messages, 'gemini-test')
    assert total >= 150
    assert Backend.vocab_scans == 0
    assert len(tokenizer) == len(tokenizer) == 262144
    assert Backend.vocab_scans == 1


def test_heavy_gc_waits_for_growth_when_resident_memory_exceeds_threshold():
    from background_tasks.monitors import should_run_heavy_gc
    assert not should_run_heavy_gc(140, 150, None)
    assert should_run_heavy_gc(330, 150, None)
    assert not should_run_heavy_gc(340, 150, 320)
    assert should_run_heavy_gc(410, 150, 320)


def test_fastjson_matches_stdlib_and_falls_back_for_unsupported_values():
    from utils import fastjson
    def stdlib(value):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    for value in ({'text': '中文😀', 'items': [1, 2.5, None, True, ('a', 'b')]},
                  {'beyond_64_bit': 2 ** 70},
                  {1: 'non-string key'}):
        assert fastjson.dumps(value) == stdlib(value)
    assert json.loads(fastjson.dumps({'big': 2 ** 70}))['big'] == 2 ** 70


def test_request_log_is_written_as_utf8_bytes_and_read_back(tmp_path, monkeypatch):
    from modules.monitoring import LogManager, MonitorConfig
    monkeypatch.setattr(MonitorConfig, 'LOG_DIR', tmp_path)
    writer = object.__new__(LogManager)
    entry = {'type': 'request_end', 'request_id': 'abcdef123456', 'model': 'demo', 'timestamp': 1794200272.0,
             'success': True, 'request_messages': [{'role': 'user', 'content': '你好😀'}]}
    writer._write_hierarchical_log(entry)
    [path] = tmp_path.rglob('*.json.gz')
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        assert json.load(stream) == entry


def test_prepare_time_is_split_into_stages_and_missing_marks_fold_forward():
    from core.request_context import RequestContext
    context = RequestContext()
    context.started = 100.0
    context.marks.update({'body_received': 100.5, 'recorded': 100.75, 'upstream_start': 101.0,
                          'upstream_post': 101.25, 'first_byte': 103.0})
    timings = context.snapshot()
    # 这条链路没打 parsed，解析时间并进 route
    assert timings['prepare_stages'] == {'receive_ms': 500.0, 'route_ms': 250.0, 'convert_ms': 250.0}
    assert sum(timings['prepare_stages'].values()) == timings['prepare_ms'] == 1000.0
    assert timings['upstream_build_ms'] == 250.0
    assert timings['upstream_wait_ms'] == 2000.0


def test_loop_watchdog_reports_a_blocked_event_loop(monkeypatch, caplog):
    import time
    from utils import loop_watchdog
    monkeypatch.setattr(loop_watchdog, 'BEAT_SECONDS', 0.01)
    monkeypatch.setattr(loop_watchdog, 'STALL_SECONDS', 0.2)
    monkeypatch.setattr(loop_watchdog, 'stats', {'stalls': 0, 'max_stall_ms': 0.0, 'last_stall_at': None, 'last_stall_ms': 0.0})

    def hog_the_loop():
        time.sleep(0.5)

    async def run():
        beat = asyncio.create_task(loop_watchdog.heartbeat())
        await asyncio.sleep(0.1)
        hog_the_loop()
        await asyncio.sleep(0.3)
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)
    with caplog.at_level('WARNING', logger='utils.loop_watchdog'):
        asyncio.run(run())
    assert loop_watchdog.stats['stalls'] == 1
    assert loop_watchdog.stats['max_stall_ms'] >= 400
    assert 'hog_the_loop' in caplog.text
