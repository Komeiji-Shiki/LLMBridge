"""模型可用性时间线：按模型、按时间段统计 requests.db 里的成功和失败。"""
import itertools
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.statistics_queries import query_availability
from modules.monitoring_sqlite import SQLiteLogger

NOW = 1_800_000_000 + 3 * 600 + 120      # 当前这一格开始 2 分钟
_ids = itertools.count()


def write(logger, model, seconds_ago, success=True, error=None):
    logger.write_request({'type': 'request_end', 'request_id': f'r{next(_ids)}', 'timestamp': NOW - seconds_ago,
                          'model': model, 'status': 'success' if success else 'failed', 'success': success,
                          **({'error': error} if error else {})})


def test_buckets_align_to_the_clock_and_split_failures_from_client_disconnects(tmp_path):
    logger = SQLiteLogger(tmp_path / 'requests.db')
    write(logger, 'neon', 60)
    write(logger, 'neon', 90, False, str({'message': 'Interaction failed', 'type': 'upstream_error'}))
    write(logger, 'neon', 100, False, str({'message': 'Interaction failed', 'type': 'upstream_error'}))
    write(logger, 'neon', 110, False, 'Resource exhausted\nretry later')
    write(logger, 'neon', 115, False, 'Client disconnected: CancelledError')
    write(logger, 'neon', 700)
    write(logger, 'carbon', 1300, False, 'Client disconnected (non-stream)')
    write(logger, 'carbon', 6 * 3600)              # 窗口之外
    logger.close()
    with sqlite3.connect(tmp_path / 'requests.db') as conn:
        result = query_availability(conn, NOW, 600, 36)

    assert result['start'] == NOW - 120 - 35 * 600 and result['buckets'] == 36
    neon, carbon = result['models']
    assert (neon['model'], neon['total'], neon['success'], neon['failed'], neon['cancelled']) == ('neon', 6, 2, 3, 1)
    assert neon['cells'] == [
        {'slot': 34, 'total': 1, 'success': 1, 'failed': 0, 'cancelled': 0},
        {'slot': 35, 'total': 5, 'success': 1, 'failed': 3, 'cancelled': 1,
         'error': 'Interaction failed', 'error_count': 2},
    ]
    # 只有客户端断开的格子不算失败，也不附错误
    assert carbon['cells'] == [{'slot': 33, 'total': 1, 'success': 0, 'failed': 0, 'cancelled': 1}]


def test_route_reports_missing_database_and_validates_range(monkeypatch):
    from routes import monitor_routes

    calls = []

    async def availability(hours, bucket_minutes):
        calls.append((hours, bucket_minutes))
        return None if hours == 1 else {'models': []}

    monkeypatch.setattr(monitor_routes.stats_db, 'get_availability_async', availability)
    app = FastAPI()
    app.include_router(monitor_routes.router)
    client = TestClient(app)
    assert client.get('/api/monitor/availability').json() == {'models': []}
    assert client.get('/api/monitor/availability?hours=1').status_code == 503
    assert client.get('/api/monitor/availability?hours=100').status_code == 422
    assert calls == [(6, 10), (1, 10)]
