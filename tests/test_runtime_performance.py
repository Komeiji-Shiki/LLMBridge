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
