"""日志正文块存储：多轮对话重复的历史和截图只存一份，读回来和原始记录完全一致。"""
import gzip
import json
import sqlite3
from datetime import date

from core import log_blobs, log_pack
from core.exchange_archive import load_exchange, save_exchange
from core.request_context import RequestContext
from modules.monitoring import MonitorConfig, _load_log_file, monitoring_service

TODAY = date(2026, 11, 21)
SCREENSHOT = 'data:image/png;base64,' + 'iVBORw0KGgo' * 3000


def screenshot_turn(cache_marker=False):
    """Claude Code 读截图的工具结果；缓存标记只在最新一轮上，下一轮就被拿掉。"""
    block = {'type': 'image_url', 'image_url': {'url': SCREENSHOT}}
    message = {'role': 'tool', 'tool_call_id': 'call_1', 'content': [block, {'type': 'text', 'text': '截图'}]}
    if cache_marker:
        message['cache_control'] = {'type': 'ephemeral'}
    return message


def history(turns):
    messages = [{'role': 'system', 'content': '系统提示' * 300}, screenshot_turn()]
    for turn in range(turns):
        messages.append({'role': 'user', 'content': f'第 {turn} 轮问题' + '细节' * 300})
        messages.append({'role': 'assistant', 'content': '好'})
    return messages


def blob_rows(root):
    with sqlite3.connect(root / 'blobs.db') as conn:
        return conn.execute('SELECT kind, raw_size, last_day FROM blobs').fetchall()


def test_outlined_log_reads_back_identical(tmp_path):
    entry = {'request_id': 'r1', 'request_messages': history(3) + [screenshot_turn(cache_marker=True)]}
    stored = log_blobs.outline_log(entry, tmp_path)

    assert len(json.dumps(stored)) < 2000
    assert log_blobs.hydrate_log(stored, tmp_path) == entry
    # 外层只差缓存标记的两条消息各存一份，里面的截图只存一份
    assert sum(size > len(SCREENSHOT) for _, size, _ in blob_rows(tmp_path)) == 1


def test_later_turns_only_store_new_messages(tmp_path):
    log_blobs.outline_log({'request_messages': history(5)}, tmp_path)
    before = len(blob_rows(tmp_path))
    log_blobs.outline_log({'request_messages': history(6)}, tmp_path)
    assert len(blob_rows(tmp_path)) == before + 1


def test_small_records_never_create_the_store(tmp_path):
    entry = {'request_messages': [{'role': 'user', 'content': 'hi'}]}
    assert log_blobs.outline_log(entry, tmp_path) == entry
    assert not (tmp_path / 'blobs.db').exists()


def test_purge_keeps_blobs_still_referenced_recently(tmp_path, monkeypatch):
    monkeypatch.setattr(log_blobs, '_today', lambda: 20261101)
    log_blobs.outline_log({'request_messages': history(1)}, tmp_path)
    monkeypatch.setattr(log_blobs, '_today', lambda: 20261120)
    # 新的一天又引用了同一段历史：最后引用日期跟着前移，按 11-10 截止也不能删
    shared = log_blobs.outline_log({'request_messages': history(1)[:2]}, tmp_path)
    store = log_blobs.store_for(tmp_path)

    removed = store.purge_before(log_blobs.KIND_LOGS, 20261110)

    assert removed == 1  # 只有没再被引用的那条提问
    assert log_blobs.hydrate_log(shared, tmp_path) == {'request_messages': history(1)[:2]}
    # 回收后缓存也要清掉，否则后续记录会引用已删除的块
    again = log_blobs.outline_log({'request_messages': history(1)}, tmp_path)
    assert log_blobs.hydrate_log(again, tmp_path) == {'request_messages': history(1)}


def test_missing_blob_leaves_placeholder(tmp_path):
    stored = log_blobs.outline_log({'request_messages': history(1)}, tmp_path)
    log_blobs.store_for(tmp_path).purge_before(log_blobs.KIND_LOGS, 99991231)
    restored = log_blobs.hydrate_log(stored, tmp_path)
    assert all(log_blobs.MISSING in message for message in restored['request_messages'] if isinstance(message, dict)
               and len(message) == 1)


def test_monitoring_writes_outlined_files_and_reads_them_back(tmp_path, monkeypatch):
    monkeypatch.setattr(MonitorConfig, 'LOG_DIR', tmp_path)
    entry = {'type': 'request_end', 'timestamp': 1795200000.0, 'request_id': 'abcd1234-0000-4000-8000-000000000009',
             'model': 'demo', 'request_messages': history(4)}
    monitoring_service.log_manager._write_hierarchical_log(entry)
    path = next(tmp_path.glob('*/*/*.json.gz'))

    assert len(gzip.decompress(path.read_bytes())) < 3000
    assert _load_log_file(path) == entry


def test_exchange_archive_and_pack_restore_full_records(tmp_path):
    context = RequestContext(request_id='b' * 32, owner_id='admin', model='demo')
    record = {'request': {'messages': history(2), 'tools': [{'name': 'Read', 'schema': 'x' * 2000}]},
              'upstream_request': {'input': history(2)[1:], 'system_instruction': '系统提示' * 300}}
    path = save_exchange(context, record, tmp_path / 'exchanges')

    assert len(gzip.decompress(path.read_bytes())) < 3000
    loaded = load_exchange(path)
    assert loaded['request'] == record['request'] and loaded['upstream_request'] == record['upstream_request']

    # 打包进旧日志归档后仍从同一个块库还原
    day = path.parent.parent.name
    log_pack.pack_old_logs(tmp_path, 1, today=date(int(day[:4]), int(day[4:6]), int(day[6:])).replace(year=2099))
    packed = log_pack.load_packed_exchange(tmp_path, 'b' * 32)
    assert packed['upstream_request'] == record['upstream_request']
