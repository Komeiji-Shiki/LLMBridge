import gzip
import json
import tarfile
from datetime import date

import pytest
import zstandard

from core import log_pack
from core.exchange_archive import SAME_AS_REQUEST
from modules.monitoring import MonitorConfig, MonitoringService

TODAY = date(2026, 11, 2)
RID = 'abcd1234-0000-4000-8000-000000000001'
RID_PLAIN = 'ffff0000-0000-4000-8000-000000000002'
EXCHANGE_ID = 'a' * 32


def write_log(root, day, hour, rid, *, compressed=True, body='对话内容'):
    directory = root / day / hour
    directory.mkdir(parents=True, exist_ok=True)
    data = json.dumps({'request_id': rid, 'type': 'request_end', 'response_content': body}, ensure_ascii=False).encode()
    if compressed:
        path = directory / f'model_{day}_{hour}00_{rid[:8]}.json.gz'
        path.write_bytes(gzip.compress(data))
    else:
        path = directory / f'model_{day}_{hour}00_{rid[:8]}.json'
        path.write_bytes(data)
    return path


def write_exchange(root, day='20260905'):
    directory = root / 'exchanges' / day / '06'
    directory.mkdir(parents=True, exist_ok=True)
    record = {'request': {'messages': ['历史' * 200]}, 'upstream_request': {'messages': SAME_AS_REQUEST}}
    path = directory / f'{EXCHANGE_ID}.json.gz'
    path.write_bytes(gzip.compress(json.dumps(record, ensure_ascii=False).encode()))
    return path


def test_packed_records_stay_readable_and_sources_are_removed(tmp_path):
    gz = write_log(tmp_path, '20260905', '06', RID)
    plain = write_log(tmp_path, '20260905', '07', RID_PLAIN, compressed=False, body='明文')
    exchange = write_exchange(tmp_path)

    stats = log_pack.pack_old_logs(tmp_path, 14, today=TODAY)

    assert stats['files'] == 3 and stats['deleted'] == 3 and stats['conflicts'] == 0
    assert not gz.exists() and not plain.exists() and not exchange.exists()
    assert not (tmp_path / '20260905').exists()
    assert log_pack.find_packed_request(tmp_path, RID)['response_content'] == '对话内容'
    assert log_pack.find_packed_request(tmp_path, RID_PLAIN)['response_content'] == '明文'
    restored = log_pack.load_packed_exchange(tmp_path, EXCHANGE_ID)
    assert restored['upstream_request']['messages'] == restored['request']['messages']


def test_recent_days_are_left_alone(tmp_path):
    boundary = write_log(tmp_path, '20261019', '06', RID)
    old = write_log(tmp_path, '20261018', '06', RID_PLAIN)
    log_pack.pack_old_logs(tmp_path, 14, today=TODAY)
    assert boundary.exists() and not old.exists()


def test_disabled_when_days_not_positive(tmp_path):
    source = write_log(tmp_path, '20200101', '06', RID)
    assert log_pack.pack_old_logs(tmp_path, 0, today=TODAY)['files'] == 0
    assert source.exists() and not (tmp_path / 'archive').exists()


def test_keep_source_then_second_run_only_deletes(tmp_path):
    source = write_log(tmp_path, '20260905', '06', RID)
    first = log_pack.pack_old_logs(tmp_path, 14, today=TODAY, keep_source=True)
    assert source.exists() and first['parts'] == 1 and first['deleted'] == 0
    second = log_pack.pack_old_logs(tmp_path, 14, today=TODAY)
    assert not source.exists() and second['parts'] == 0 and second['deleted'] == 1
    assert log_pack.find_packed_request(tmp_path, RID)['request_id'] == RID


def test_verification_failure_keeps_sources_and_index_clean(tmp_path, monkeypatch):
    source = write_log(tmp_path, '20260905', '06', RID)

    def fail(path, entries):
        raise log_pack.PackVerifyError('模拟损坏')

    monkeypatch.setattr(log_pack, '_verify_part', fail)
    with pytest.raises(log_pack.PackVerifyError):
        log_pack.pack_old_logs(tmp_path, 14, today=TODAY)
    assert source.exists()
    assert log_pack.find_packed_request(tmp_path, RID) is None
    assert not list((tmp_path / 'archive' / 'logs').glob('*.tar.zst'))


def test_real_corruption_is_detected(tmp_path):
    write_log(tmp_path, '20260905', '06', RID)
    log_pack.pack_old_logs(tmp_path, 14, today=TODAY, keep_source=True)
    part = next((tmp_path / 'archive' / 'logs').glob('*.tar.zst'))
    bad = part.with_name('bad.tar.zst')
    bad.write_bytes(zstandard.ZstdCompressor().compress(b'\0' * 4096))
    entry = ('logs', '20260905', 'x', 'x', part.name, 512, 16, 1, '0' * 64)
    with pytest.raises(log_pack.PackVerifyError):
        log_pack._verify_part(bad, [entry])


def test_changed_file_after_pack_is_not_deleted(tmp_path):
    source = write_log(tmp_path, '20260905', '06', RID)
    log_pack.pack_old_logs(tmp_path, 14, today=TODAY, keep_source=True)
    source.write_bytes(gzip.compress(b'{"request_id":"changed"}'))
    stats = log_pack.pack_old_logs(tmp_path, 14, today=TODAY)
    assert source.exists() and stats['conflicts'] == 1


def test_corrupt_gzip_is_preserved_byte_for_byte(tmp_path):
    directory = tmp_path / '20260905' / '06'
    directory.mkdir(parents=True)
    broken = directory / 'broken_20260905_0600_00000000.json.gz'
    broken.write_bytes(b'not gzip')
    log_pack.pack_old_logs(tmp_path, 14, today=TODAY)
    part = next((tmp_path / 'archive' / 'logs').glob('*.tar.zst'))
    with open(part, 'rb') as raw, log_pack._open_reader(raw) as reader, tarfile.open(fileobj=reader, mode='r|') as tar:
        member = next(iter(tar))
        assert member.name.endswith('.json.gz') and tar.extractfile(member).read() == b'not gzip'


def test_part_is_a_standard_tar_zst_with_correct_offsets(tmp_path):
    write_log(tmp_path, '20260905', '06', RID)
    write_log(tmp_path, '20260905', '07', RID_PLAIN, compressed=False)
    log_pack.pack_old_logs(tmp_path, 14, today=TODAY)
    part = next((tmp_path / 'archive' / 'logs').glob('*.tar.zst'))
    with open(part, 'rb') as raw, log_pack._open_reader(raw) as reader, tarfile.open(fileobj=reader, mode='r|') as tar:
        names = [member.name for member in tar]
    assert sorted(names) == [f'logs/20260905/06/model_20260905_0600_{RID[:8]}.json',
                             f'logs/20260905/07/model_20260905_0700_{RID_PLAIN[:8]}.json']


def test_large_day_splits_into_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(log_pack, 'PART_BYTES', 64)
    write_log(tmp_path, '20260905', '06', RID, body='x' * 200)
    write_log(tmp_path, '20260905', '07', RID_PLAIN, body='y' * 200)
    assert log_pack.pack_old_logs(tmp_path, 14, today=TODAY)['parts'] == 2
    assert log_pack.find_packed_request(tmp_path, RID_PLAIN)['response_content'] == 'y' * 200


def test_short_id_collision_returns_exact_request(tmp_path):
    other = RID[:8] + '-ffff-4000-8000-00000000000f'
    write_log(tmp_path, '20260904', '06', other, body='别的请求')
    write_log(tmp_path, '20260905', '06', RID)
    log_pack.pack_old_logs(tmp_path, 14, today=TODAY)
    assert log_pack.find_packed_request(tmp_path, RID)['response_content'] == '对话内容'
    assert log_pack.find_packed_request(tmp_path, other)['response_content'] == '别的请求'


def test_missing_archive_returns_none(tmp_path):
    assert log_pack.find_packed_request(tmp_path, RID) is None
    assert log_pack.load_packed_exchange(tmp_path, EXCHANGE_ID) is None


def test_monitoring_falls_back_to_pack(tmp_path, monkeypatch):
    import time
    from datetime import datetime
    monkeypatch.setattr(MonitorConfig, 'LOG_DIR', tmp_path)
    write_log(tmp_path, '20260905', '06', RID)
    log_pack.pack_old_logs(tmp_path, 14, today=TODAY)
    service = object.__new__(MonitoringService)
    timestamp = time.mktime(datetime(2026, 9, 5, 6, 30).timetuple())
    assert service._find_request_in_hierarchical_logs_fast(RID, timestamp)['response_content'] == '对话内容'
    assert service._find_request_in_hierarchical_logs(RID)['response_content'] == '对话内容'
    assert service._find_request_in_hierarchical_logs('00000000-none') is None


def test_background_pack_runs_under_selector_event_loop(tmp_path, monkeypatch):
    import asyncio
    from background_tasks import monitors
    monkeypatch.setattr(MonitorConfig, 'LOG_DIR', tmp_path)
    source = write_log(tmp_path, '20200101', '06', RID)
    loop = asyncio.SelectorEventLoop()
    try:
        loop.run_until_complete(monitors._pack_old_logs())
    finally:
        loop.close()
    assert not source.exists()
    assert log_pack.find_packed_request(tmp_path, RID)['request_id'] == RID


def test_loose_file_still_wins_over_pack(tmp_path, monkeypatch):
    import time
    from datetime import datetime
    monkeypatch.setattr(MonitorConfig, 'LOG_DIR', tmp_path)
    write_log(tmp_path, '20260905', '06', RID, body='散文件')
    log_pack.pack_old_logs(tmp_path, 14, today=TODAY, keep_source=True)
    service = object.__new__(MonitoringService)
    timestamp = time.mktime(datetime(2026, 9, 5, 6, 30).timetuple())
    assert service._find_request_in_hierarchical_logs_fast(RID, timestamp)['response_content'] == '散文件'


def test_purge_packed_before_removes_expired_parts_only(tmp_path):
    from core.log_pack import KIND_LOGS, _archive_root, find_packed_request, pack_old_logs, purge_packed_before
    for day, request_id in (('20260101', 'aaaaaaaa11111111'), ('20260301', 'bbbbbbbb22222222')):
        directory = tmp_path / day / '00'
        directory.mkdir(parents=True)
        (directory / f'm_{day}_0000_{request_id[:8]}.json').write_text(
            json.dumps({'request_id': request_id}), encoding='utf-8')
    pack_old_logs(tmp_path, 14, today=date(2026, 6, 1))
    assert purge_packed_before(tmp_path, KIND_LOGS, '20260201') == 1
    assert find_packed_request(tmp_path, 'aaaaaaaa11111111') is None
    assert find_packed_request(tmp_path, 'bbbbbbbb22222222') == {'request_id': 'bbbbbbbb22222222'}
    assert [path.name for path in (_archive_root(tmp_path) / KIND_LOGS).iterdir()] == ['20260301-0001.tar.zst']
    assert purge_packed_before(tmp_path / 'missing', KIND_LOGS, '20260201') == 0
