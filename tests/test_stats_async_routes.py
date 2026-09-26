"""Exercise real StatsDB wrappers through the administrator HTTP routes."""
import asyncio
import threading
import time
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.mark.parametrize('method,args', [
    ('get_token_stats', ('start', 'end', {'model': {}}, 'hour')),
    ('get_request_stats', ('start', 'end')),
    ('get_request_summary', ('start', 'end')),
])
def test_async_statistics_forward_arguments_off_event_loop(monkeypatch, method, args):
    from core.db_stats import StatsDB
    database = object.__new__(StatsDB)
    event_thread = threading.get_ident()
    def query(*received):
        assert threading.get_ident() != event_thread
        assert received == args
        return {'result': method}
    monkeypatch.setattr(database, method, query)
    assert asyncio.run(getattr(database, method + '_async')(*args)) == {'result': method}


def test_admin_statistics_use_real_sqlite_instead_of_fallback(tmp_path, monkeypatch):
    from core import db_stats
    from modules.monitoring_sqlite import SQLiteLogger
    from routes import admin_routes
    path = tmp_path / 'requests.db'
    writer = SQLiteLogger(path)
    writer.write_request({'type': 'request_end', 'request_id': 'actual-sqlite-row', 'timestamp': time.time(),
                          'model': 'demo', 'success': True, 'input_tokens': 100, 'output_tokens': 20,
                          'cost_info': {'total_cost': 7.25, 'currency': 'USD'}})
    monkeypatch.setattr(db_stats, 'DB_PATH', path)
    database = db_stats.StatsDB()
    monkeypatch.setattr(admin_routes, 'stats_db', database)
    monkeypatch.setattr(admin_routes, '_get_admin_cached_response', AsyncMock(return_value=None))
    monkeypatch.setattr(admin_routes, '_set_admin_cached_response', AsyncMock())
    fallback = AsyncMock(side_effect=AssertionError('SQLite statistics must not fall back'))
    monkeypatch.setattr(admin_routes, '_build_memory_token_stats', fallback)
    app = FastAPI()
    app.include_router(admin_routes.router)
    client = TestClient(app)
    token_response = client.get('/api/admin/token_stats?source=bridge')
    request_response = client.get('/api/admin/request_stats')
    assert token_response.status_code == 200
    assert token_response.json()['total_input_tokens'] == 100
    assert token_response.json()['total_output_tokens'] == 20
    assert request_response.status_code == 200
    assert request_response.json()['total_requests'] == 1
    assert asyncio.run(database.get_request_summary_async())['total_requests'] == 1
    fallback.assert_not_called()
    assert writer.get_request_details('actual-sqlite-row')['total_cost'] == 7.25
    writer.close()


def test_concurrent_token_queries_share_database_work(monkeypatch):
    from types import SimpleNamespace
    from routes import admin_routes
    async def query(*args):
        await asyncio.sleep(.01)
        return {'model_stats': [], 'total_tokens': 123}
    database = SimpleNamespace(enabled=True, get_token_stats_async=AsyncMock(side_effect=query))
    monkeypatch.setattr(admin_routes, '_get_admin_cached_response', AsyncMock(return_value=None))
    monkeypatch.setattr(admin_routes, '_set_admin_cached_response', AsyncMock())
    async def run():
        return await asyncio.gather(*(admin_routes.get_token_stats(
            None, None, None, None, 'day', database, None, {}, None, None) for _ in range(20)))
    assert all(item['total_tokens'] == 123 for item in asyncio.run(run()))
    database.get_token_stats_async.assert_awaited_once()


def test_manual_refresh_bypasses_cached_gateway_statistics(tmp_path, monkeypatch):
    from cachetools import TTLCache
    from core import db_stats
    from modules.monitoring_sqlite import SQLiteLogger
    from routes import admin_routes
    from utils.async_singleflight import AsyncSingleFlight

    path = tmp_path / 'requests.db'
    writer = SQLiteLogger(path)
    monkeypatch.setattr(db_stats, 'DB_PATH', path)
    monkeypatch.setattr(admin_routes, 'stats_db', db_stats.StatsDB())
    # 保持缓存有效，验证刷新确实重新读取数据库，而非恰好等到缓存过期。
    monkeypatch.setattr(admin_routes, '_ADMIN_STATS_CACHE', {
        name: TTLCache(maxsize=256, ttl=600) for name in ('overview', 'token_stats', 'request_stats')})
    monkeypatch.setattr(admin_routes, '_TOKEN_STATS_QUERIES', AsyncSingleFlight())
    app = FastAPI()
    app.include_router(admin_routes.router)

    def record(request_id):
        writer.write_request({'type': 'request_end', 'request_id': request_id, 'timestamp': time.time(),
                              'model': 'demo', 'success': True, 'input_tokens': 100, 'output_tokens': 20})

    try:
        with TestClient(app) as client:
            record('first')
            assert client.get('/api/admin/overview').json()['stats']['total_requests'] == 1
            assert client.get('/api/admin/token_stats?source=bridge').json()['total_tokens'] == 120
            record('second')
            assert client.get('/api/admin/overview').json()['stats']['total_requests'] == 1
            assert client.get('/api/admin/token_stats?source=bridge').json()['total_tokens'] == 120

            assert client.get('/api/admin/overview?force=true').json()['stats']['total_requests'] == 2
            assert client.get('/api/admin/token_stats?source=bridge&force=true&background=true').json()['total_tokens'] == 240
            assert client.get('/api/admin/overview').json()['stats']['total_requests'] == 2
            assert client.get('/api/admin/token_stats?source=bridge').json()['total_tokens'] == 240
    finally:
        writer.close()
