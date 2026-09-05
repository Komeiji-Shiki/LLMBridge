"""Additive request metadata migration with an explicit, tested field contract."""
import json

COLUMNS = {
    'caller_id': "TEXT DEFAULT 'unattributed'", 'caller_name': "TEXT DEFAULT '历史未归属'",
    'conversation_id': 'TEXT', 'gateway_request_id': 'TEXT', 'timings': 'TEXT', 'pricing_snapshot': 'TEXT',
}


def migrate_metadata(connection):
    existing = {row[1] for row in connection.execute('PRAGMA table_info(requests)')}
    for name, definition in COLUMNS.items():
        if name not in existing:
            connection.execute(f'ALTER TABLE requests ADD COLUMN {name} {definition}')
    connection.execute('CREATE INDEX IF NOT EXISTS idx_caller_timestamp ON requests(caller_id, timestamp)')


def write_metadata(connection, request_id, record):
    values = []
    for name in COLUMNS:
        value = record.get(name)
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
        result[name] = value
    return result
