"""价格、长上下文、未知模型及查询缓存的行为验证。"""
from datetime import datetime

import pytest

from core.codex_pricing import estimate
from core.codex_usage import CodexUsageIndex
from test_codex_usage import event, metadata, usage, write_log


def test_standard_cache_writes_and_reasoning_are_not_double_charged():
    values = {'input_tokens': 100000, 'cached_tokens': 60000, 'cache_write_tokens': 20000,
              'output_tokens': 1000, 'reasoning_tokens': 900}
    price = estimate('gpt-6-astra', values)
    assert price['input_cost'] == pytest.approx(.45)
    assert price['cached_cost'] == pytest.approx(.06)
    assert price['output_cost'] == pytest.approx(.05)
    assert price['total_cost'] == pytest.approx(.56)
    assert estimate('codex-auto-review', values) is None
    assert estimate('gpt-5.3-codex-spark', values) is None


def test_request_and_session_context_thresholds(tmp_path):
    home = tmp_path / 'home'
    write_log(home / 'sessions/astra.jsonl', metadata('gpt-6-astra') + [
        event(last=usage(272000, 1000, 200000)),
        event(last=usage(300000, 1000, 250000), timestamp='2026-09-08T13:00:00Z')])
    index = CodexUsageIndex(tmp_path / 'usage.db', [home])
    data = index.stats()
    assert data['total_cost'] == pytest.approx(.97 + 1.575)
    assert data['long_context_events'] == 1
    # 同一会话后续进入长上下文，session 级模型的日期子区间也使用该档位。
    write_log(home / 'sessions/5.5.jsonl', metadata('gpt-5.5') + [
        event(last=usage(100000, 1000, 0), timestamp=datetime(2026, 9, 7, 12).isoformat()),
        event(last=usage(300000, 1000, 0), timestamp=datetime(2026, 9, 8, 12).isoformat())])
    data = index.stats('2026-09-07', '2026-09-07', force=True)
    assert data['total_cost'] == pytest.approx(1 + .045)
    assert data['long_context_events'] == 1


def test_unpriced_coverage_and_detached_cached_results(tmp_path, monkeypatch):
    from core import codex_usage_summary
    home = tmp_path / 'home'
    write_log(home / 'sessions/a.jsonl', metadata('unpublished') + [event(usage(100))])
    index = CodexUsageIndex(tmp_path / 'usage.db', [home])
    first = index.stats()
    assert first['pricing']['complete'] is False
    assert first['pricing']['unpriced_models'] == ['unpublished']
    assert first['unpriced_tokens'] == 110
    assert first['model_stats'][0]['total_cost'] is None
    monkeypatch.setattr(codex_usage_summary, 'summarize', lambda *args: pytest.fail('未变化的查询应复用汇总'))
    first['model_stats'].clear()
    assert len(index.stats()['model_stats']) == 1


def test_background_scan_does_not_block_saved_statistics(tmp_path, monkeypatch):
    import asyncio
    import threading
    from routes import admin_usage
    home = tmp_path / 'home'
    write_log(home / 'sessions/a.jsonl', metadata('gpt-6-astra') + [event(usage(100))])
    index = CodexUsageIndex(tmp_path / 'usage.db', [home])
    index.stats()
    index._last_scan = 0
    monkeypatch.setattr(admin_usage, 'codex_usage_index', index)
    release, started = threading.Event(), threading.Event()
    original = index._scan
    def slow_scan(conn, force):
        started.set()
        assert release.wait(2)
        original(conn, force)
    monkeypatch.setattr(index, '_scan', slow_scan)
    bridge = {'model_stats': [], 'daily_stats': [], 'total_tokens': 0, 'total_input_tokens': 0,
              'total_output_tokens': 0, 'total_cached_tokens': 0}
    async def run():
        try:
            result = await admin_usage.selected_usage(bridge, 'all', background=True)
            assert result['total_tokens'] == 110
            assert result['codex_usage']['status']['refreshing'] is True
            assert await asyncio.to_thread(started.wait, 1)
            # 扫描持有独立锁时，已保存统计仍能返回。
            saved = await asyncio.wait_for(asyncio.to_thread(index.stats, refresh=False), .5)
            assert saved['total_tokens'] == 110
        finally:
            release.set()
            await index._refresh_task
        assert not index.schedule_refresh()
    asyncio.run(run())
