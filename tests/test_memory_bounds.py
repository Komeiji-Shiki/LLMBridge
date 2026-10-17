"""内存边界回归：监控快照、图片缓存与轻量分词器加载。

这些约束是长期性的：一旦有人把完整 system/tools 重新塞回监控记录，
或者把本地分词器换回 transformers，进程常驻内存会立刻反弹数百 MB。
"""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from core.bounded_cache import ByteLimitedTTLCache
from core.conversation_store import ConversationStore
from modules.monitoring import _compact_recent_record, _load_stats_json
from modules.token_counter._custom import _evict_tokenizer_cache
from services.logprobs_collector import LogprobsCollector
from utils.monitor_params import build_monitor_request_params

REPOSITORY = Path(__file__).resolve().parents[1]


def _make_tools(count: int, properties: int, description_size: int):
    return [
        {
            'name': f'tool_{index}',
            'description': 'D' * description_size,
            'input_schema': {
                'type': 'object',
                'properties': {
                    f'p{prop}': {'type': 'string', 'description': 'X' * description_size}
                    for prop in range(properties)
                },
                'required': ['p0'],
            },
        }
        for index in range(count)
    ]


def test_monitor_params_are_bounded_and_keep_tool_names():
    params = build_monitor_request_params({
        'system': 'S' * 200_000,
        'tools': _make_tools(15, 8, 200),
        'max_tokens': 8000,
        'stream': True,
    })
    encoded = json.dumps(params, ensure_ascii=False).encode('utf-8')
    assert len(encoded) <= 24 * 1024
    assert len(params['system']) < 1000
    assert [tool['name'] for tool in params['tools']] == [
        f'tool_{index}' for index in range(15)
    ]


def test_monitor_params_survive_extreme_tool_schemas():
    params = build_monitor_request_params({
        'system': 'S' * 200_000,
        'tools': _make_tools(30, 50, 500),
    })
    assert len(json.dumps(params, ensure_ascii=False).encode('utf-8')) <= 24 * 1024
    # 超预算时退化为摘要形态，但工具名必须保留，面板才有可用信息
    assert params['tools']
    assert all('name' in tool for tool in params['tools'] if isinstance(tool, dict))


def test_compact_recent_record_truncates_legacy_large_params():
    record = {
        'request_id': 'legacy',
        'request_params': {'system': 'S' * 100_000, 'tools': _make_tools(10, 5, 100)},
        'response_preview': 'R' * 5000,
    }
    compacted = _compact_recent_record(record)
    assert len(json.dumps(compacted, ensure_ascii=False).encode('utf-8')) < 30 * 1024
    assert len(compacted['response_preview']) <= 1100


def test_oversized_stats_file_skips_recent_requests(tmp_path):
    stats_path = tmp_path / 'stats.json'
    payload = {
        'last_update': 1.0,
        'startup_time': 1.0,
        'model_stats': {'m': {'total': 1, 'success': 1, 'failed': 0,
                              'total_duration': 0.1, 'count_with_duration': 1}},
        'recent_requests': [{'request_id': 'x', 'request_params': {'system': 'S' * (9 * 1024 * 1024)}}],
        'recent_errors': [],
    }
    stats_path.write_text(json.dumps(payload), encoding='utf-8')
    loaded = _load_stats_json(stats_path)
    assert 'model_stats' in loaded
    assert 'recent_requests' not in loaded


def test_byte_limited_cache_evicts_and_skips_oversized_values():
    cache = ByteLimitedTTLCache(maxsize=10, ttl=60, max_bytes=1000)
    cache['a'] = 'x' * 400
    cache['b'] = 'y' * 400
    cache['c'] = 'z' * 400
    assert cache.total_bytes <= 1000
    assert 'a' not in cache  # 最早的条目按 LRU 顺序被驱逐
    assert len(cache) <= 2

    cache['oversized'] = 'q' * 2000
    assert 'oversized' not in cache
    assert cache.total_bytes <= 1000

    cache.clear()
    assert cache.total_bytes == 0


def test_logprobs_collector_bounds_stream_buffers():
    collector = LogprobsCollector(
        request_id='bounded',
        model_name='deepseek',
        target_model_id='deepseek',
        display_name='DeepSeek',
        cfg={
            'record_raw_stream': True,
            'top_logprobs': 20,
            'max_raw_events': 3,
            'max_raw_bytes': 100_000,
            'max_logprob_tokens': 5,
        },
        strip_from_client=False,
    )
    for index in range(10):
        collector.capture_stream_event({
            'choices': [{
                'index': 0,
                'delta': {'content': 'x'},
                'logprobs': {
                    'content': [
                        {'token': f't{index}-{item}', 'logprob': -0.1, 'top_logprobs': []}
                        for item in range(3)
                    ],
                },
            }],
        })

    assert len(collector.raw_stream_events) <= 3
    assert collector._raw_truncated is True
    assert len(collector.content_logprobs) <= 5
    assert collector._logprobs_truncated is True
    raw_record = collector._build_raw_record()
    assert raw_record['truncated'] is True


def test_tokenizer_cache_is_lru_bounded(monkeypatch):
    cache = {'a': object(), 'b': object(), 'c': object()}
    monkeypatch.setattr(
        'modules.token_counter._custom._tokenizer_last_used',
        {'custom_a': 1.0, 'custom_b': 2.0, 'custom_c': 3.0},
    )
    _evict_tokenizer_cache(cache, 'custom_', 2)
    assert set(cache) == {'b', 'c'}


def _fill_store(store, sessions=40, size=20000):
    import os
    for index in range(sessions):
        store.touch('owner', f's{index}', 'm', 'endpoint')
        store.put('owner', f's{index}', 'signatures', {'blob': os.urandom(size).hex()})


def _freelist(path):
    import sqlite3
    connection = sqlite3.connect(path)
    try:
        return connection.execute('PRAGMA freelist_count').fetchone()[0]
    finally:
        connection.close()


def test_conversation_store_purges_legacy_exchange_snapshots(tmp_path):
    import gzip
    import sqlite3

    path = tmp_path / 'conversations.db'
    legacy = sqlite3.connect(path)
    legacy.executescript('''
        CREATE TABLE conversations (
            owner TEXT NOT NULL, session TEXT NOT NULL, model TEXT NOT NULL,
            endpoint TEXT NOT NULL, credential TEXT NOT NULL DEFAULT '',
            touched REAL NOT NULL, PRIMARY KEY(owner, session));
        CREATE TABLE artifacts (
            owner TEXT NOT NULL, session TEXT NOT NULL, key TEXT NOT NULL, payload BLOB NOT NULL,
            PRIMARY KEY(owner, session, key));
    ''')
    payload = gzip.compress(json.dumps({'kept': True}).encode())
    legacy.execute("INSERT INTO conversations VALUES('owner','session','m','endpoint','',9e18)")
    legacy.executemany('INSERT INTO artifacts VALUES(?,?,?,?)', [
        ('owner', 'session', key, payload)
        for key in ('last_exchange', 'response:abc', 'signatures', 'reasoning:x')
    ])
    legacy.commit()
    legacy.close()

    store = ConversationStore(path)
    assert store.get('owner', 'session', 'signatures') == {'kept': True}
    connection = sqlite3.connect(path)
    try:
        keys = {row[0] for row in connection.execute('SELECT key FROM artifacts')}
        version = connection.execute('PRAGMA user_version').fetchone()[0]
    finally:
        connection.close()
    assert keys == {'signatures', 'reasoning:x'}
    assert version >= 1


def test_reclaim_returns_free_pages_to_filesystem(tmp_path):
    store = ConversationStore(tmp_path / 'conversations.db')
    _fill_store(store)
    with store.connection() as connection:
        connection.execute('DELETE FROM conversations')
    before = _freelist(store.path)
    assert before > 100

    # 小批次也要回收全部空闲页（execute() 单步执行时每批只回收 1 页）
    assert store.reclaim(pages=64) == before
    assert _freelist(store.path) == 0
    assert store.storage_stats()['free_bytes'] == 0


def test_reclaim_respects_batch_budget(tmp_path):
    store = ConversationStore(tmp_path / 'conversations.db')
    _fill_store(store)
    with store.connection() as connection:
        connection.execute('DELETE FROM conversations')
    before = _freelist(store.path)
    assert store.reclaim(pages=10, max_batches=2) == 20
    assert _freelist(store.path) == before - 20


def test_compact_copy_is_valid_and_enables_incremental_vacuum(tmp_path):
    store = ConversationStore(tmp_path / 'conversations.db')
    store.touch('owner', 'session', 'm', 'endpoint')
    store.put('owner', 'session', 'signatures', {'data': 'x' * 5000})

    target = store.compact_copy(tmp_path / 'compacted.db')
    copied = ConversationStore(target)
    stats = copied.storage_stats()
    assert stats['auto_vacuum'] == 2
    assert stats['free_bytes'] == 0
    assert copied.get('owner', 'session', 'signatures') == {'data': 'x' * 5000}


def test_exchange_archive_stores_shared_request_fields_once(tmp_path):
    import gzip
    from types import SimpleNamespace
    from core.exchange_archive import SAME_AS_REQUEST, load_exchange, save_exchange

    messages = [{'role': 'user', 'content': 'hello ' * 200}]
    tools = [{'name': 'Bash', 'description': 'run'}]
    record = {
        'request': {'model': 'alias', 'messages': messages, 'tools': tools},
        'upstream_request': {'model': 'upstream', 'tools': tools, 'messages': messages, 'stream': True},
        'response': {'id': 'r1'},
    }
    context = SimpleNamespace(request_id='a' * 32, owner_id='owner', model='alias', session_id='s')
    path = save_exchange(context, record, root=tmp_path)

    stored = json.loads(gzip.decompress(path.read_bytes()))
    assert stored['upstream_request'] == {
        'model': 'upstream', 'tools': SAME_AS_REQUEST, 'messages': SAME_AS_REQUEST, 'stream': True}
    restored = load_exchange(path)
    assert restored['upstream_request'] == record['upstream_request']
    assert list(restored['upstream_request']) == ['model', 'tools', 'messages', 'stream']


def test_exchange_archive_retention_only_removes_old_date_directories(tmp_path):
    from datetime import date
    from core.exchange_archive import purge_expired_exchanges

    for name in ('20261001', '20261010', '20261012', '20261018', 'notes'):
        (tmp_path / name).mkdir()
    assert purge_expired_exchanges(0, root=tmp_path, today=date(2026, 10, 18)) == 0
    assert purge_expired_exchanges(7, root=tmp_path, today=date(2026, 10, 18)) == 2
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ['20261012', '20261018', 'notes']


def test_local_tokenizer_does_not_import_transformers_or_torch():
    tokenizer_path = REPOSITORY / 'deepseek_v3_tokenizer'
    if not (tokenizer_path / 'tokenizer.json').exists():
        pytest.skip('本地 DeepSeek tokenizer 不存在')

    script = (
        'import sys;'
        f'sys.path.insert(0, {str(REPOSITORY)!r});'
        'from modules.token_counter._tokenizers import _load_local_fast_tokenizer;'
        f'tokenizer = _load_local_fast_tokenizer({str(tokenizer_path)!r});'
        'assert tokenizer is not None;'
        "assert len(tokenizer.encode('Hello world, this is a test.')) == 8;"
        "assert not any(name.split('.')[0] == 'torch' for name in sys.modules);"
        "assert not any(name.split('.')[0] == 'transformers' for name in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, '-c', script],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
