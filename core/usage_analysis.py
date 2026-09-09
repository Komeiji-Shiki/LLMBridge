"""Read-only usage views; current-price estimates never modify historical rows."""
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
import sqlite3
import json
from utils.api_pricing import calculate_api_cost, cache_write_usage


@contextmanager
def read_database(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def date_filter(start=None, end=None):
    conditions, values = [], []
    if start:
        conditions.append('timestamp>=?')
        values.append(datetime.combine(date.fromisoformat(start), time.min).timestamp())
    if end:
        conditions.append('timestamp<?')
        values.append(datetime.combine(date.fromisoformat(end) + timedelta(days=1), time.min).timestamp())
    if start and end and start > end:
        raise ValueError('开始日期不能晚于结束日期')
    return (' WHERE ' + ' AND '.join(conditions) if conditions else ''), values


def usage_by_caller(path, start=None, end=None):
    where, params = date_filter(start, end)
    with read_database(path) as connection:
        columns = {row[1] for row in connection.execute('PRAGMA table_info(requests)')}
        caller = "COALESCE(caller_id,'unattributed')" if 'caller_id' in columns else "'unattributed'"
        name = "MAX(caller_name)" if 'caller_name' in columns else "'历史未归属'"
        rows = connection.execute(f'''SELECT {caller} AS caller_id, {name} AS caller_name, currency,
            COUNT(*) AS requests, SUM(CASE WHEN success THEN 0 ELSE 1 END) AS failures,
            SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
            SUM(cached_tokens) AS cached_tokens, SUM(total_cost) AS total_cost, MAX(timestamp) AS last_used
            FROM requests {where} GROUP BY {caller}, currency ORDER BY requests DESC''', params).fetchall()
        return {'items': [dict(row) for row in rows], 'grouped_by': ['caller_id', 'currency'], 'read_only': True}


def estimate_current_prices(path, model_config, start=None, end=None):
    where, params = date_filter(start, end)
    with read_database(path) as connection:
        columns = {row[1] for row in connection.execute('PRAGMA table_info(requests)')}
        write_fields = ','.join(
            f'COALESCE({key},0) AS {key}' if key in columns else f'0 AS {key}'
            for key in ('cache_write_tokens', 'cache_write_1h_tokens'))
        cache_mode = 'cache_mode' if 'cache_mode' in columns else 'NULL AS cache_mode'
        usage_field = 'upstream_usage' if 'upstream_usage' in columns else 'NULL AS upstream_usage'
        rows = connection.execute(f'''SELECT model, currency, input_tokens, output_tokens,
            cached_tokens, total_cost AS historical_cost, {write_fields}, {cache_mode}, {usage_field}
            FROM requests {where} ORDER BY model''', params).fetchall()
    by_alias = {}
    for alias, raw in model_config.items():
        config = raw[0] if isinstance(raw, list) and raw else raw
        if isinstance(config, dict):
            by_alias[alias] = config
            by_alias.setdefault(config.get('display_name') or alias, config)
    items = {}
    for row in rows:
        row = dict(row)
        # 旧账单不回写；有原始 usage 的旧记录可以在只读对比中恢复写入量。
        if not row['cache_write_tokens'] and row['upstream_usage']:
            try:
                row['cache_write_tokens'], row['cache_write_1h_tokens'] = cache_write_usage(json.loads(row['upstream_usage']))
            except (TypeError, ValueError):
                pass
        key = row['model'], row['currency']
        item = items.setdefault(key, {'model': row['model'], 'currency': row['currency'],
                                     'requests': 0, 'current_estimate': None, 'current_currency': None,
                                     **dict.fromkeys(('input_tokens', 'output_tokens', 'cached_tokens',
                                                      'cache_write_tokens', 'cache_write_1h_tokens',
                                                      'historical_cost'), 0)})
        item['requests'] += 1
        for field in ('input_tokens', 'output_tokens', 'cached_tokens', 'cache_write_tokens',
                      'cache_write_1h_tokens', 'historical_cost'):
            item[field] += row[field] or 0
        config = by_alias.get(row['model'])
        pricing = config.get('pricing') if config else None
        if isinstance(pricing, dict) and pricing:
            inputs, outputs, cached = row['input_tokens'] or 0, row['output_tokens'] or 0, row['cached_tokens'] or 0
            cost = calculate_api_cost(inputs, outputs, pricing, cached,
                                      row['cache_write_tokens'], row['cache_write_1h_tokens'],
                                      explicit_cache=row['cache_mode'] == 'explicit')
            item['current_currency'] = cost['currency']
            for target, field in (('current_estimate', 'total_cost'),
                                  ('current_cache_write_cost', 'cache_write_cost'),
                                  ('current_cache_write_extra_cost', 'cache_write_extra_cost')):
                item[target] = round((item.get(target) or 0) + cost[field], 6)
    return {'items': list(items.values()), 'read_only': True, 'historical_amounts_unchanged': True}
