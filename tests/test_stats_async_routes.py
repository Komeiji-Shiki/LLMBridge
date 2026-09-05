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
    token_response = client.get('/api/admin/token_stats')
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
