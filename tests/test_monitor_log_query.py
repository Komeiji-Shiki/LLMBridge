"""真实 SQLite、HTTP 路由与文件回退的日志检索回归。"""
from contextlib import contextmanager
from datetime import datetime
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modules.monitoring_sqlite import SQLiteLogger
from utils.log_query import LogQuery


@pytest.fixture
def log_store(tmp_path):
    store = SQLiteLogger(tmp_path / 'requests.db')
    records = []
    for index, day in enumerate((8, 9, 9, 10)):
        entry = {'type': 'request_end', 'request_id': f'log-{index}',
                 'timestamp': datetime(2026, 9, day, 23, 59, 59).timestamp(),
                 'model': 'model_%' if index < 3 else 'other',
                 'status': 'success' if index % 2 else 'failed', 'success': bool(index % 2),
                 'caller_id': 'caller', 'conversation_id': 'conversation',
                 'cost_info': {'total_cost': 3.5, 'currency': 'CNY'}}
        if index == 2:
            entry.update(reasoning_content='思考' * 30000,
                         request_messages=[{'role': 'assistant', 'tool_calls': [{'function': {'name': 'search'}}]}])
        records.append(entry)
        store.write_request(entry)
    yield store, records
    store.close()


def test_filters_and_feature_flags_across_all_sqlite_read_paths(log_store):
    store, records = log_store
    result = store.query_requests(start_date='2026-09-09', end_date='2026-09-09', search='_%')
    assert result['total'] == 2
    assert [row['request_id'] for row in result['items']] == ['log-2', 'log-1']
    assert store.query_requests(limit=1, offset=1, start_date='2026-09-09', end_date='2026-09-09')['items'][0]['request_id'] == 'log-1'
    for record in (store.get_request_details('log-2'), store.get_recent_requests()[1], result['items'][0]):
        assert record['has_reasoning'] is True and record['has_tool_calls'] is True
        assert record['total_cost'] == 3.5 and record['currency'] == 'CNY'
        assert 'reasoning_content' not in record and 'request_messages' not in record
        assert len(json.dumps(record)) < 2000
    # 替换同一请求时标记也要同步更新，不能留下先前的 True。
    store.write_request({**records[2], 'reasoning_content': '', 'request_messages': []})
    assert store.get_request_details('log-2')['has_reasoning'] is False
    assert store.get_request_details('log-2')['has_tool_calls'] is False


def test_recent_query_avoids_count_and_pagination_uses_consistent_snapshot(log_store, monkeypatch):
    store, _ = log_store
    statements, inserted = [], False
    original = store._read_connection

    def trace(sql):
        nonlocal inserted
        statements.append(sql)
        if 'COUNT(*)' in ''.join(statements) and sql.startswith('SELECT *') and not inserted:
            inserted = True
            store.write_request({'type': 'request_end', 'request_id': 'concurrent',
                                 'timestamp': datetime(2026, 9, 11).timestamp(), 'model': 'other'})

    @contextmanager
    def traced_connection():
        with original() as connection:
            connection.set_trace_callback(trace)
            yield connection

    monkeypatch.setattr(store, '_read_connection', traced_connection)
    assert len(store.get_recent_requests(2)) == 2
    assert not any('COUNT' in sql or 'DISTINCT' in sql for sql in statements)
    result = store.query_requests()
    assert inserted and result['total'] == len(result['items']) == 4
    assert store.query_requests()['total'] == 5


def test_http_filters_validate_bounds_and_skip_unrequested_catalog(log_store, monkeypatch):
    from modules.monitoring import LogManager
    from routes import monitor_routes
    store, _ = log_store
    manager = object.__new__(LogManager)
    manager.sqlite_logger = store
    monkeypatch.setattr(monitor_routes, 'monitoring_service', SimpleNamespace(log_manager=manager))
    catalog = Mock(wraps=store.get_distinct_models)
    monkeypatch.setattr(store, 'get_distinct_models', catalog)
    app = FastAPI()
    app.include_router(monitor_routes.router)
    with TestClient(app) as client:
        response = client.get('/api/monitor/logs/requests/query', params={
            'start_date': '2026-09-09', 'end_date': '2026-09-09', 'status': 'failed', 'include_models': False})
        assert response.status_code == 200
        data = response.json()
        assert data['total'] == 1 and data['items'][0]['request_id'] == 'log-2'
        assert 'models' not in data and data['limited'] is False
        catalog.assert_not_called()
        assert client.get('/api/monitor/logs/requests/query').json()['models'] == ['model_%', 'other']
        for params in ({'limit': -1}, {'offset': -1}, {'limit': 1001}):
            assert client.get('/api/monitor/logs/requests/query', params=params).status_code == 422
        for params in ({'start_date': '20260909'}, {'end_date': '2026-02-30'},
                       {'start_date': '2026-09-10', 'end_date': '2026-09-09'}, {'status': 'unknown'}):
            assert client.get('/api/monitor/logs/requests/query', params=params).status_code == 400


def test_file_fallback_uses_same_filters_and_declares_coverage(log_store):
    from modules.monitoring import LogManager
    store, records = log_store
    manager = object.__new__(LogManager)
    manager.sqlite_logger = Mock(query_requests=Mock(side_effect=sqlite3.OperationalError('unavailable')))
    manager.read_recent_logs = Mock(return_value=records[::-1])
    filters = {'start_date': '2026-09-09', 'end_date': '2026-09-09', 'status': 'failed', 'search': '_%'}
    data = manager.query_request_logs(**filters, include_models=False)
    expected = store.query_requests(**filters)
    assert [row['request_id'] for row in data['items']] == [row['request_id'] for row in expected['items']]
    assert data['limited'] and '1000' in data['notice'] and 'models' not in data
    assert data['items'][0]['has_tool_calls'] is True
    manager.read_recent_logs.assert_called_once_with('requests', 1000)


def test_migration_preserves_historical_cost_and_unknown_flags(tmp_path):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as conn:
        conn.execute('''CREATE TABLE requests (
            request_id TEXT UNIQUE, timestamp REAL, date TEXT, model TEXT, status TEXT, success INTEGER,
            duration REAL, error TEXT, mode TEXT, session_id TEXT, messages_count INTEGER,
            input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, total_cost REAL,
            created_at TEXT)''')
        conn.execute("INSERT INTO requests(request_id, timestamp, date, model, status, success, total_cost) VALUES ('old', 1, '1970-01-01', 'old-model', 'success', 1, 7.25)")
    store = SQLiteLogger(path)
    try:
        result = store.get_request_details('old')
        assert result['total_cost'] == 7.25
        assert result['has_reasoning'] is None and result['has_tool_calls'] is None
        store._init_database()
        assert store.get_recent_requests()[0]['total_cost'] == 7.25
    finally:
        store.close()


@pytest.mark.parametrize('message', [
    {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': '思考'}, {'type': 'tool_use', 'name': 'search'}]},
    {'role': 'assistant', 'reasoning_content': '思考', 'tool_calls': [{'function': {'name': 'search'}}]},
])
def test_native_content_features(message):
    from utils.request_features import request_features
    assert request_features({'response_message': message}) == {'has_reasoning': True, 'has_tool_calls': True}
