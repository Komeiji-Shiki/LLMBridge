import sqlite3
from core.request_metadata import migrate_metadata, write_metadata, read_metadata


def test_migration_preserves_old_amounts_and_covers_every_field():
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.execute('CREATE TABLE requests(request_id TEXT PRIMARY KEY, timestamp REAL, total_cost REAL)')
    connection.execute('INSERT INTO requests VALUES(?,?,?)', ('old', 1, 12.34))
    migrate_metadata(connection)
    migrate_metadata(connection)
    assert connection.execute('SELECT total_cost FROM requests').fetchone()[0] == 12.34
    assert connection.execute('SELECT caller_id FROM requests').fetchone()[0] == 'unattributed'
    record = {'caller_id': 'key-id', 'caller_name': '用户', 'conversation_id': 'session',
              'gateway_request_id': 'logical', 'timings': {'total_ms': 123},
              'pricing_snapshot': {'pricing': {'input': 1}, 'exchange_rate': {'USD_TO_CNY': 7.2}}}
    write_metadata(connection, 'old', record)
    row = connection.execute('SELECT * FROM requests').fetchone()
    assert read_metadata(row) == record
    assert row['total_cost'] == 12.34
    connection.close()
