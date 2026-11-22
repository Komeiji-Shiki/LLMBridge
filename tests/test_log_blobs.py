"""日志正文块存储：多轮对话重复的历史和截图只存一份，读回来和原始记录完全一致。"""
import gzip
import json
import os
import sqlite3
from datetime import date

from core import log_blob_migration, log_blobs, log_pack
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


def write_old_log(root, day, entry):
    path = root / day / '06' / f'demo_{day}_0600_{entry["request_id"][:8]}.json.gz'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(entry, ensure_ascii=False).encode()))
    return path


def write_old_exchange(root, day, exchange_id, record):
    path = root / 'exchanges' / day / '06' / f'{exchange_id}.json.gz'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(record, ensure_ascii=False).encode()))
    return path


OLD_ENTRY = {'request_id': 'abcd1234-0000-4000-8000-000000000010', 'model': 'demo', 'request_messages': history(4)}
OLD_EXCHANGE = {'gateway_request_id': 'c' * 32, 'request': {'messages': history(3)},
                'upstream_request': {'messages': {'__same_as_request__': True}}}


def test_migration_rewrites_old_loose_files_in_place(tmp_path):
    log = write_old_log(tmp_path, '20261120', OLD_ENTRY)
    exchange = write_old_exchange(tmp_path, '20261120', 'c' * 32, OLD_EXCHANGE)
    current = write_old_log(tmp_path, '20261122', {**OLD_ENTRY, 'request_id': 'ffff0000'})
    untouched = current.read_bytes()
    os.utime(log, (1795000000, 1795000000))

    stats = log_blob_migration.migrate(tmp_path, today=date(2026, 11, 22))

    assert stats['loose']['rewritten'] == 2 and stats['loose']['kept'] == 0
    assert len(gzip.decompress(log.read_bytes())) < 3000
    assert log.stat().st_mtime == 1795000000  # 打包按修改时间排列
    stored = json.loads(gzip.decompress(log.read_bytes()))
    assert log_blobs.hydrate_log(stored, tmp_path) == OLD_ENTRY
    assert load_exchange(exchange)['request'] == OLD_EXCHANGE['request']
    assert {last_day for _, _, last_day in blob_rows(tmp_path)} == {20261120}
    assert current.read_bytes() == untouched  # 今天的目录还在写入，不碰
    assert log_blob_migration.migrate(tmp_path, today=date(2026, 11, 22))['loose']['rewritten'] == 0


def test_migration_repacks_archived_days(tmp_path, monkeypatch):
    # 每条记录单独一个分块，覆盖重新打包时分块的编号
    monkeypatch.setattr(log_pack, 'PART_BYTES', 1)
    second = {**OLD_ENTRY, 'request_id': 'bcde2345-0000-4000-8000-000000000011', 'request_messages': history(5)}
    write_old_log(tmp_path, '20261101', OLD_ENTRY)
    write_old_log(tmp_path, '20261101', second)
    write_old_exchange(tmp_path, '20261101', 'c' * 32, OLD_EXCHANGE)
    log_pack.pack_old_logs(tmp_path, 3, today=date(2026, 11, 22))
    archive = tmp_path / 'archive'
    old_parts = set(archive.rglob('*.tar.zst'))
    old_bytes = sum(path.stat().st_size for path in old_parts)

    stats = log_blob_migration.migrate(tmp_path, today=date(2026, 11, 22))['packed']

    assert stats['rewritten'] == 3 and stats['days'] == 2 and stats['kept'] == 0
    new_parts = set(archive.rglob('*.tar.zst'))
    assert len(new_parts) == 3 and not new_parts & old_parts
    assert sum(path.stat().st_size for path in new_parts) < old_bytes
    assert log_pack.find_packed_request(tmp_path, OLD_ENTRY['request_id']) == OLD_ENTRY
    assert log_pack.find_packed_request(tmp_path, second['request_id']) == second
    assert log_pack.load_packed_exchange(tmp_path, 'c' * 32)['request'] == OLD_EXCHANGE['request']

    again = log_blob_migration.migrate(tmp_path, today=date(2026, 11, 22))['packed']
    assert again['rewritten'] == 0 and set(archive.rglob('*.tar.zst')) == new_parts


def test_migration_keeps_records_that_do_not_read_back(tmp_path, monkeypatch):
    monkeypatch.setitem(log_blob_migration._CODECS, log_pack.KIND_LOGS,
                        (log_blobs.outline_log, lambda record, root: {**record, 'model': 'changed'}))
    log = write_old_log(tmp_path, '20261120', OLD_ENTRY)
    original = log.read_bytes()

    stats = log_blob_migration.migrate(tmp_path, today=date(2026, 11, 22))

    assert stats['loose']['kept'] == 1 and stats['loose']['rewritten'] == 0
    assert log.read_bytes() == original
