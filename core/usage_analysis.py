"""Read-only usage views; current-price estimates never modify historical rows."""
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
import sqlite3


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
        rows = connection.execute(f'''SELECT model, currency, COUNT(*) AS requests,
            SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
            SUM(cached_tokens) AS cached_tokens, SUM(total_cost) AS historical_cost
            FROM requests {where} GROUP BY model,currency ORDER BY model''', params).fetchall()
    by_alias = {}
    for alias, raw in model_config.items():
        config = raw[0] if isinstance(raw, list) and raw else raw
        if isinstance(config, dict):
            by_alias[alias] = config
            by_alias.setdefault(config.get('display_name') or alias, config)
    items = []
    for row in rows:
        item = dict(row)
        config = by_alias.get(row['model'])
        pricing = config.get('pricing') if config else None
        if isinstance(pricing, dict) and pricing:
            unit = float(pricing.get('unit') or 1000000)
            inputs, outputs, cached = row['input_tokens'] or 0, row['output_tokens'] or 0, row['cached_tokens'] or 0
            cached_price = pricing.get('cached_input')
            estimated = ((max(0, inputs - cached) if cached_price is not None else inputs) * float(pricing.get('input') or 0)
                         + outputs * float(pricing.get('output') or 0)
                         + (cached * float(cached_price) if cached_price is not None else 0)) / unit
            item.update(current_estimate=round(estimated, 6), current_currency=pricing.get('currency', 'USD'))
        else:
            item.update(current_estimate=None, current_currency=None)
        items.append(item)
    return {'items': items, 'read_only': True, 'historical_amounts_unchanged': True}
