"""使用合成日志和真实 SQLite/HTTP 验证导入，测试不读取个人会话。"""

import csv
import io
import json
import shutil
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.codex_usage import CodexUsageIndex, parse_session
from core.claude_code_usage import ClaudeCodeUsageIndex


def usage(input_tokens, output=10, cached=20, reasoning=3):
    return {'input_tokens': input_tokens, 'output_tokens': output, 'cached_input_tokens': cached,
            'reasoning_output_tokens': reasoning, 'total_tokens': input_tokens + output}


def event(total=None, last=None, timestamp='2026-09-08T12:00:00Z'):
    return {'timestamp': timestamp, 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
        'total_token_usage': total, 'last_token_usage': last}}}


def write_log(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')


def metadata(model='demo'):
    return [{'type': 'session_meta', 'payload': {'id': 'session-1', 'model_provider': 'openai'}},
            {'type': 'turn_context', 'payload': {'model': model}}]


@pytest.fixture
def index(tmp_path, monkeypatch):
    from routes import admin_usage
    importer = CodexUsageIndex(tmp_path / 'usage.db', [tmp_path / 'home'])
    monkeypatch.setattr(admin_usage, 'codex_usage_index', importer)
    monkeypatch.setattr(admin_usage, 'claude_code_usage_index', ClaudeCodeUsageIndex(
        tmp_path / 'claude.db', [tmp_path / 'claude']))
    return importer


def test_cumulative_duplicates_models_and_reset(index, tmp_path):
    log = tmp_path / 'home/sessions/a.jsonl'
    first = event(usage(100), usage(100))
    repeated = event(usage(100), usage(100), '2026-09-08T12:01:00Z')
    write_log(log, metadata() + [first, first, repeated,
              {'type': 'turn_context', 'payload': {'model': 'second'}},
              event(usage(150, 20, 40, 6), usage(50), '2026-09-08T12:02:00Z'),
              event(usage(10, 5, 0, 1), usage(10, 5, 0, 1), '2026-09-08T12:03:00Z')])
    data = index.stats()
    assert data['input_tokens'] == 160
    assert data['output_tokens'] == 25
    assert data['total_tokens'] == 185
    assert data['cached_tokens'] == 40
    assert data['reasoning_tokens'] == 7
    assert data['event_count'] == 3
    assert {row['model'] for row in data['model_stats']} == {'demo', 'second'}
    assert index.stats()['total_tokens'] == 185
    assert index.stats(force=True)['total_tokens'] == 185


def test_archive_copy_fork_and_file_rewrite(index, tmp_path):
    home = tmp_path / 'home'
    log = home / 'sessions/a.jsonl'
    rows = metadata() + [event(usage(100), usage(100))]
    write_log(log, rows)
    assert index.stats()['total_tokens'] == 110
    archive = home / 'archived_sessions/a.jsonl'
    archive.parent.mkdir()
    shutil.copyfile(log, archive)
    # 分叉继承同一条历史事件，新增事件才增加统计。
    write_log(home / 'sessions/fork.jsonl', metadata() + [rows[-1],
              event(usage(120, 15), usage(20, 5), '2026-09-08T13:00:00Z')])
    assert index.stats(force=True)['total_tokens'] == 135
    log.unlink()
    assert index.stats(force=True)['total_tokens'] == 135
    write_log(home / 'sessions/fork.jsonl', metadata() + [rows[-1]])
    assert index.stats(force=True)['total_tokens'] == 110
    # 重启服务仍按持久化指纹读取，不重新累加。
    restarted = CodexUsageIndex(index.db_path, [home])
    assert restarted.stats()['total_tokens'] == 110
    assert restarted.stats()['status']['changed_files'] == 0


def test_partial_malformed_and_last_only(index, tmp_path):
    path = tmp_path / 'home/sessions/a.jsonl'
    rows = metadata() + [None, {'payload': None}, event(), event({'input_tokens': 'bad'}),
                         event(last=usage(100))]
    write_log(path, rows)
    tail = json.dumps(event(last=usage(50), timestamp='2026-09-08T13:00:00Z'))
    with path.open('a', encoding='utf-8') as stream:
        stream.write('{broken}\n' + tail[:30])
    assert index.stats()['total_tokens'] == 110
    with path.open('a', encoding='utf-8') as stream:
        stream.write(tail[30:] + '\n')
    assert index.stats(force=True)['total_tokens'] == 170
    assert list(parse_session(path))[0][6:] == (100, 20, 10, 3, 110, 0)


def test_date_end_includes_whole_local_day(index, tmp_path):
    from core.db_stats import StatsDB
    start = datetime(2026, 9, 8, 0).isoformat()
    late = datetime(2026, 9, 8, 23, 59).isoformat()
    next_day = datetime(2026, 9, 9, 0).isoformat()
    write_log(tmp_path / 'home/sessions/a.jsonl', metadata() + [
        event(last=usage(100), timestamp=start), event(last=usage(200), timestamp=late),
        event(last=usage(300), timestamp=next_day)])
    assert index.stats('2026-09-08', '2026-09-08')['total_tokens'] == 320
    assert StatsDB._parse_time_bound('2026-09-08', True) == datetime(2026, 9, 9).timestamp()
    assert index.stats(start, late)['total_tokens'] == 110


def test_read_failure_preserves_index(index, tmp_path, monkeypatch):
    from core import codex_usage
    path = tmp_path / 'home/sessions/a.jsonl'
    write_log(path, metadata() + [event(usage(100))])
    assert index.stats()['total_tokens'] == 110
    def fail(path):
        raise PermissionError('测试读取失败')
    monkeypatch.setattr(codex_usage, 'parse_session', fail)
    data = index.stats(force=True)
    assert data['total_tokens'] == 110
    assert data['status']['errors']


def test_http_sources_csv_and_gateway_totals(index, tmp_path, monkeypatch):
    from core import db_stats
    from modules.monitoring_sqlite import SQLiteLogger
    from routes import admin_routes
    write_log(tmp_path / 'home/sessions/a.jsonl', metadata() + [event(usage(100), usage(100))])
    path = tmp_path / 'requests.db'
    writer = SQLiteLogger(path)
    writer.write_request({'type': 'request_end', 'request_id': 'bridge-row', 'model': 'demo',
                          'timestamp': datetime(2026, 9, 8, 15).timestamp(), 'success': True,
                          'input_tokens': 40, 'output_tokens': 5,
                          'cost_info': {'input_cost': 1, 'total_cost': 1, 'currency': 'USD'}})
    monkeypatch.setattr(db_stats, 'DB_PATH', path)
    database = db_stats.StatsDB()
    monkeypatch.setattr(admin_routes, 'stats_db', database)
    monkeypatch.setattr(admin_routes, '_get_admin_cached_response', AsyncMock(return_value=None))
    monkeypatch.setattr(admin_routes, '_set_admin_cached_response', AsyncMock())
    app = FastAPI()
    app.include_router(admin_routes.router)
    with TestClient(app) as client:
        all_data = client.get('/api/admin/token_stats').json()
        assert all_data['total_tokens'] == 155
        assert all_data['total_cost'] == 1
        assert {row['source'] for row in all_data['model_stats']} == {'bridge', 'codex'}
        assert len(all_data['model_stats']) == 2
        bridge = client.get('/api/admin/token_stats?source=bridge').json()
        assert bridge['total_tokens'] == 45
        assert 'codex_usage' not in bridge
        codex = client.get('/api/admin/token_stats?source=codex').json()
        assert codex['total_tokens'] == 110
        assert codex['model_stats'][0]['total_cost'] is None
        assert codex['cost_scope'] == 'unavailable'
        assert client.get('/api/admin/request_stats').json()['total_requests'] == 1
        for query in ('source=oops', 'start_date=invalid', 'start_date=2026-09-09&end_date=2026-09-08'):
            assert client.get('/api/admin/token_stats?' + query).status_code == 422
        report = client.get('/api/admin/export_report?source=all&start_date=2026-09-08&end_date=2026-09-08')
        assert report.status_code == 200
        rows = list(csv.DictReader(io.StringIO(report.content.decode('utf-8-sig'))))
        assert len(rows) == 2
        external = next(row for row in rows if row['来源'] == 'codex')
        assert external['总Tokens'] == '110'
        assert external['总成本(原币)'] == ''
        assert external['请求数'] == ''
        assert external['推理Tokens'] == '3'
        write_log(tmp_path / 'home/sessions/a.jsonl', metadata('gpt-6-astra') + [event(usage(100), usage(100))])
        priced = client.get('/api/admin/token_stats?source=all&force=true').json()
        assert priced['total_cost'] == pytest.approx(1.00132)
        assert priced['cost_usd']['cached_cost'] == pytest.approx(.00002)
        assert priced['pricing']['complete'] is True
        report = client.get('/api/admin/export_report?source=codex')
        external = list(csv.DictReader(io.StringIO(report.content.decode('utf-8-sig'))))[0]
        assert external['总成本(原币)'] == '0.00132'
        assert external['金额口径'] == '标准API估算'
        assert external['货币'] == 'USD'
        # 正式模型和指定计价别名都应进入 API 汇总与 CSV，保留原始名称。
        assert external['价格核对日期'] == '2026-09-09'
        for model in ('gpt-6-sol', 'codex-auto-review'):
            write_log(tmp_path / 'home/sessions/a.jsonl', metadata(model) + [event(usage(100), usage(100))])
            priced = client.get('/api/admin/token_stats?source=all&force=true').json()
            assert priced['total_cost'] == pytest.approx(1.000264)
            external_model = next(row for row in priced['model_stats'] if row['source'] == 'codex')
            assert external_model['model'] == model
            assert external_model['price_model'] == 'gpt-6-sol'
            assert priced['pricing']['complete'] is True
            assert priced['pricing']['aliases']['codex-auto-review'] == 'gpt-6-sol'
            assert priced['pricing']['rates']['gpt-6-sol']['verified_at'] == '2026-09-27'
            assert priced['pricing']['rates']['gpt-6-astra']['verified_at'] == '2026-09-09'
            report = client.get('/api/admin/export_report?source=codex')
            external = list(csv.DictReader(io.StringIO(report.content.decode('utf-8-sig'))))[0]
            assert external['价格核对日期'] == '2026-09-27'
            assert external['模型'] == model
            assert external['总成本(原币)'] == '0.000264'
    writer.close()


def test_bridge_provider_excluded_only_from_combined_view(index, tmp_path, monkeypatch):
    import asyncio
    from routes import admin_usage
    rows = metadata()
    rows[0]['payload']['model_provider'] = 'local-lmarenabridge'
    write_log(tmp_path / 'home/sessions/a.jsonl', rows + [event(usage(100))])
    monkeypatch.setattr(admin_usage, 'CONFIG', {'codex_usage': {'bridge_providers': ['local-lmarenabridge']}})
    bridge = {'model_stats': [], 'daily_stats': [], 'total_tokens': 45, 'total_input_tokens': 40,
              'total_output_tokens': 5, 'total_cached_tokens': 0}
    combined = asyncio.run(admin_usage.selected_usage(bridge, 'all'))
    assert combined['total_tokens'] == 45
    assert combined['codex_usage']['excluded_usage']['event_count'] == 1
    assert combined['codex_usage']['excluded_usage']['total_tokens'] == 110
    assert combined['codex_usage']['model_stats'] == []
    standalone = asyncio.run(admin_usage.selected_usage(bridge, 'codex'))
    assert standalone['total_tokens'] == 110
    assert standalone['codex_usage']['excluded_usage']['event_count'] == 0
    assert index.stats('2026-09-09', '2026-09-09', exclude_providers=['local-lmarenabridge'])['excluded_usage']['event_count'] == 0


def test_manual_background_refresh_is_incremental_and_does_not_wait(index, tmp_path, monkeypatch):
    import asyncio
    import threading
    from core import codex_usage
    from routes import admin_usage

    changed = tmp_path / 'home/sessions/changed.jsonl'
    unchanged = tmp_path / 'home/sessions/unchanged.jsonl'
    write_log(changed, metadata() + [event(usage(100))])
    write_log(unchanged, metadata() + [event(usage(20), timestamp='2026-09-08T13:00:00Z')])
    assert index.stats()['total_tokens'] == 140
    write_log(changed, metadata() + [event(usage(200))])
    started, release = threading.Event(), threading.Event()
    parsed = []

    def slow_parse(path):
        parsed.append(path)
        started.set()
        assert release.wait(5), '测试必须先收到旧索引统计，再允许扫描完成'
        yield from parse_session(path)

    monkeypatch.setattr(codex_usage, 'parse_session', slow_parse)
    bridge = {'model_stats': [], 'daily_stats': [], 'total_tokens': 45,
              'total_input_tokens': 40, 'total_output_tokens': 5, 'total_cached_tokens': 0}

    async def run():
        try:
            cached = await asyncio.wait_for(admin_usage.selected_usage(
                bridge, 'all', force=True, background=True), timeout=3)
            assert cached['total_tokens'] == 185
            assert cached['codex_usage']['status']['refreshing'] is True
            assert await asyncio.to_thread(started.wait, 3)
            task = index._refresh_task
            assert not task.done()
            assert index.schedule_refresh(immediate=True) is True
            assert index._refresh_task is task
        finally:
            release.set()
            if index._refresh_task is not None:
                await index._refresh_task

        fresh = await admin_usage.selected_usage(bridge, 'all', background=True)
        assert fresh['total_tokens'] == 285
        assert fresh['codex_usage']['status']['refreshing'] is False
        assert fresh['codex_usage']['status']['changed_files'] == 1
        assert parsed == [changed]

    asyncio.run(run())


def claude_event(message_id='message-1', model='claude-opus-5-5', timestamp='2026-09-08T12:00:00Z', **usage):
    return {'type': 'assistant', 'timestamp': timestamp, 'sessionId': 'claude-session',
            'requestId': 'request-' + message_id,
            'message': {'id': message_id, 'model': model, 'usage': usage}}


def test_claude_streaming_copies_cache_ttl_and_prompt_snapshot(index, tmp_path):
    from routes import admin_usage
    importer = admin_usage.claude_code_usage_index
    home = tmp_path / 'claude'
    snapshot = claude_event(input_tokens=150, output_tokens=2)
    partial = claude_event(input_tokens=10, cache_read_input_tokens=100,
                          cache_creation_input_tokens=40, output_tokens=2)
    complete = claude_event(input_tokens=10, cache_read_input_tokens=100,
                           cache_creation_input_tokens=40, output_tokens=20,
                           cache_creation={'ephemeral_5m_input_tokens': 10, 'ephemeral_1h_input_tokens': 30},
                           output_tokens_details={'thinking_tokens': 5})
    write_log(home / 'projects/a.jsonl', [snapshot, partial, complete, complete,
              claude_event('synthetic', '<synthetic>', input_tokens=1000)])
    write_log(home / 'transcripts/copy.jsonl', [partial])
    write_log(home / 'projects/session/subagents/agent.jsonl', [
        claude_event('subagent', input_tokens=5, output_tokens=1)])
    data = importer.stats()
    assert data['event_count'] == 2
    assert data['input_tokens'] == 155
    assert data['output_tokens'] == 21
    assert data['total_tokens'] == 176
    assert data['cached_tokens'] == 100
    assert data['cache_write_tokens'] == 40
    assert data['cache_write_1h_tokens'] == 30
    assert data['reasoning_tokens'] == 5
    assert data['total_cost'] == pytest.approx(.00079)
    assert data['cache_write_cost'] == pytest.approx(.00029)
    assert data['daily_stats'][0]['cache_write_1h_tokens'] == 30


def test_claude_http_source_dates_combined_costs_and_csv(index, tmp_path, monkeypatch):
    from routes import admin_routes, admin_usage
    write_log(tmp_path / 'home/sessions/a.jsonl', metadata('gpt-6-sol') + [event(usage(100))])
    write_log(tmp_path / 'claude/projects/a.jsonl', [
        claude_event(input_tokens=10, cache_read_input_tokens=100, cache_creation_input_tokens=40,
                     cache_creation={'ephemeral_1h_input_tokens': 30, 'ephemeral_5m_input_tokens': 10},
                     output_tokens=20),
        claude_event('next-day', timestamp='2026-09-09T12:00:00Z', input_tokens=5, output_tokens=1),
    ])
    bridge = {'model_stats': [], 'daily_stats': [], 'total_tokens': 45,
              'total_input_tokens': 40, 'total_output_tokens': 5, 'total_cached_tokens': 0,
              'total_cost': 1, 'cost_usd': {'total_cost': 1}}
    monkeypatch.setattr(admin_routes, 'get_token_stats', AsyncMock(return_value=bridge))
    app = FastAPI()
    app.include_router(admin_routes.router)
    with TestClient(app) as client:
        query = 'start_date=2026-09-08&end_date=2026-09-08'
        standalone = client.get('/api/admin/token_stats?source=claude_code&' + query).json()
        assert standalone['total_tokens'] == 170
        assert standalone['total_cost'] == pytest.approx(.00075)
        assert standalone['total_cache_write_1h_tokens'] == 30
        assert {row['source'] for row in standalone['model_stats']} == {'claude_code'}
        assert 'codex_usage' not in standalone
        combined = client.get('/api/admin/token_stats?source=all&' + query).json()
        assert combined['total_tokens'] == 325
        assert combined['total_cost'] == pytest.approx(1.001014)
        assert combined['daily_stats'][0]['estimated_cost_usd'] == pytest.approx(.001014)
        assert combined['cost_breakdown']['claude_code_estimated_usd']['total_cost'] == pytest.approx(.00075)
        report = client.get('/api/admin/export_report?source=claude_code&' + query)
        rows = list(csv.DictReader(io.StringIO(report.content.decode('utf-8-sig'))))
        assert len(rows) == 1
        assert rows[0]['来源'] == 'claude_code'
        assert rows[0]['总Tokens'] == '170'
        assert rows[0]['1小时缓存写入Tokens'] == '30'
        assert rows[0]['总成本(原币)'] == '0.00075'
        assert rows[0]['请求数'] == ''
        assert rows[0]['价格核对日期'] == '2026-10-12'


def test_claude_advisor_unknown_model_and_request_context_tier(index, tmp_path):
    from routes import admin_usage
    write_log(tmp_path / 'claude/projects/a.jsonl', [
        claude_event(model='custom-model', input_tokens=10, output_tokens=2,
                     iterations=[{'type': 'message', 'input_tokens': 10, 'output_tokens': 2},
                                 {'type': 'advisor_message', 'model': 'claude-sonnet-5-5',
                                  'input_tokens': 20, 'output_tokens': 3}]),
        claude_event('haiku', model='claude-haiku-5-5', input_tokens=1,
                     cache_read_input_tokens=100000, output_tokens=10),
    ])
    data = admin_usage.claude_code_usage_index.stats()
    assert data['event_count'] == 3
    assert data['unpriced_tokens'] == 12
    models = {row['model']: row for row in data['model_stats']}
    assert models['custom-model']['total_cost'] is None
    assert models['claude-sonnet-5-5']['total_tokens'] == 23
    assert models['claude-haiku-5-5']['total_cost'] == pytest.approx(.0050255)
