"""Additive request metadata migration with an explicit, tested field contract."""
import json

COLUMNS = {
    'caller_id': "TEXT DEFAULT 'unattributed'", 'caller_name': "TEXT DEFAULT '历史未归属'",
    'cache_write_tokens': 'INTEGER DEFAULT 0',
    'cache_write_1h_tokens': 'INTEGER DEFAULT 0',
    'cache_write_cost': 'REAL DEFAULT 0',
    'cache_write_extra_cost': 'REAL DEFAULT 0',
    'cache_mode': 'TEXT',
    'conversation_id': 'TEXT', 'gateway_request_id': 'TEXT', 'timings': 'TEXT', 'pricing_snapshot': 'TEXT',
}


def migrate_metadata(connection):
    existing = {row[1] for row in connection.execute('PRAGMA table_info(requests)')}
    for name, definition in COLUMNS.items():
        if name not in existing:
            connection.execute(f'ALTER TABLE requests ADD COLUMN {name} {definition}')
    connection.execute('CREATE INDEX IF NOT EXISTS idx_caller_timestamp ON requests(caller_id, timestamp)')


def write_metadata(connection, request_id, record):
    from utils.api_pricing import cache_write_usage
    writes, one_hour = cache_write_usage(record.get('upstream_usage'))
    usage_counts = {'cache_write_tokens': writes, 'cache_write_1h_tokens': one_hour}
    values = []
    for name in COLUMNS:
        value = record.get(name)
        if name.startswith('cache_write'):
            value = (record.get('cost_info') or {}).get(name, value if value is not None else usage_counts.get(name, 0)) or 0
        elif name == 'cache_mode':
            value = (record.get('cost_info') or {}).get(name, value)
        if name in ('timings', 'pricing_snapshot'):
            value = json.dumps(value, ensure_ascii=False)
        elif name == 'caller_id':
            value = value or 'unattributed'
        elif name == 'caller_name':
            value = value or '历史未归属'
        values.append(value)
    connection.execute('UPDATE requests SET ' + ','.join(name + '=?' for name in COLUMNS) + ' WHERE request_id=?', values + [request_id])


def read_metadata(row):
    result = {}
    for name in COLUMNS:
        value = row[name] if name in row.keys() else None
        if name in ('timings', 'pricing_snapshot'):
            try:
                value = json.loads(value) if value else None
            except (TypeError, ValueError):
                value = None
        result[name] = (value or 0) if name.startswith('cache_write') else value
    return result
