"""Losslessly compressed wire archives, kept unless exchange_archive.retention_days is set.

upstream_request 与 request 共享的大字段（整段 messages、tools、system 等）
只保存一份：upstream_request 中该字段换成 SAME_AS_REQUEST 占位，字段顺序
不变。gzip 窗口只有 32KB，数百 KB 的重复历史无法被压缩掉，去重后归档体积
约减半。load_exchange() / restore_exchange() 读取时原样还原。
"""
from datetime import datetime, timedelta
import gzip
import json
import os
from pathlib import Path
import shutil

SAME_AS_REQUEST = {'__same_as_request__': True}
# 短字符串（模型名等）不值得占位，只共享容器和长文本
_MIN_SHARED_TEXT = 256


def _shareable(value):
    return isinstance(value, (list, dict)) or (isinstance(value, str) and len(value) > _MIN_SHARED_TEXT)


def dedupe_exchange(record):
    """返回存储用记录：upstream_request 中与 request 相同的大字段换成占位。"""
    request, upstream = record.get('request'), record.get('upstream_request')
    if not isinstance(request, dict) or not isinstance(upstream, dict):
        return record
    stored, changed = {}, False
    for key, value in upstream.items():
        if _shareable(value) and key in request and request[key] == value:
            stored[key] = SAME_AS_REQUEST
            changed = True
        else:
            stored[key] = value
    return {**record, 'upstream_request': stored} if changed else record


def restore_exchange(record):
    """还原 dedupe_exchange 的占位，读取方看到的结构与原始记录一致。"""
    request, upstream = record.get('request'), record.get('upstream_request')
    if not isinstance(request, dict) or not isinstance(upstream, dict):
        return record
    if not any(value == SAME_AS_REQUEST for value in upstream.values()):
        return record
    restored = {key: request.get(key) if value == SAME_AS_REQUEST else value
                for key, value in upstream.items()}
    return {**record, 'upstream_request': restored}


def save_exchange(context, record, root='logs/exchanges'):
    now = datetime.now()
    directory = Path(root) / now.strftime('%Y%m%d') / now.strftime('%H')
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (context.request_id + '.json.gz')
    temporary = target.with_suffix('.tmp')
    metadata = {'gateway_request_id': context.request_id, 'caller_id': context.owner_id,
                'model': context.model, 'conversation_id': context.session_id, **dedupe_exchange(record)}
    try:
        with gzip.open(temporary, 'wt', encoding='utf-8', compresslevel=6) as handle:
            json.dump(metadata, handle, ensure_ascii=False, separators=(',', ':'))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def load_exchange(path):
    with gzip.open(path, 'rt', encoding='utf-8') as handle:
        return restore_exchange(json.load(handle))


def purge_expired_exchanges(retention_days, root='logs/exchanges', today=None):
    """删除早于 retention_days 天的日期目录；retention_days <= 0 表示永久保留。"""
    try:
        retention_days = int(retention_days or 0)
    except (TypeError, ValueError):
        return 0
    root = Path(root)
    if retention_days <= 0 or not root.is_dir():
        return 0
    cutoff = (today or datetime.now().date()) - timedelta(days=retention_days)
    removed = 0
    for directory in root.iterdir():
        try:
            day = datetime.strptime(directory.name, '%Y%m%d').date()
        except ValueError:
            continue
        if directory.is_dir() and day < cutoff:
            shutil.rmtree(directory)
            removed += 1
    return removed
