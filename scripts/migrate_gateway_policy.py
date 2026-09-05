"""Apply the approved fidelity/retention policy and additive request metadata.

Default is a read-only plan. --apply backs up configuration and SQLite first.
No secret values or conversation text are printed.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.jsonc_edit import parse_jsonc, set_jsonc_values, atomic_write_text, atomic_write_json
from core.request_metadata import COLUMNS, migrate_metadata


def migrate(root, apply=False):
    root = Path(root).resolve()
    backup_dir = root / '.migration_backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    model_path, config_path, database = root / 'model_endpoint_map.json', root / 'config.jsonc', root / 'logs/requests.db'
    changes, model_map, config_text, fields = {}, None, None, []
    if model_path.exists():
        model_map = json.loads(model_path.read_text(encoding='utf-8'))
        count = 0
        for raw in model_map.values():
            for endpoint in raw if isinstance(raw, list) else [raw]:
                if isinstance(endpoint, dict) and endpoint.get('sanitize_recursive_schemas') is not False:
                    endpoint['sanitize_recursive_schemas'] = False
                    count += 1
        changes['fidelity_endpoints'] = count
    if config_path.exists():
        original = config_path.read_text(encoding='utf-8')
        config = parse_jsonc(original)
        collector = {**(config.get('deepseek_logprobs') or {}), 'compress': True}
        updated = {'deepseek_logprobs': collector}
        if 'tokenizer_trusted_sources' not in config:
            updated['tokenizer_trusted_sources'] = []
        if 'request_limits' not in config:
            updated['request_limits'] = {'max_body_mb': 256}
        config_text = set_jsonc_values(original, updated)
        changes['config_changed'] = config_text != original
    if database.exists():
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
            existing = {row[1] for row in connection.execute('PRAGMA table_info(requests)')}
        fields = [name for name in COLUMNS if name not in existing]
    changes['additive_columns'] = fields
    if not apply:
        return {**changes, 'applied': False}
    if any((changes.get('fidelity_endpoints'), changes.get('config_changed'), fields)):
        backup_dir.mkdir(parents=True)
    if changes.get('fidelity_endpoints'):
        shutil.copy2(model_path, backup_dir / model_path.name)
        atomic_write_json(str(model_path), model_map)
    if changes.get('config_changed'):
        shutil.copy2(config_path, backup_dir / config_path.name)
        atomic_write_text(str(config_path), config_text)
    if fields:
        with sqlite3.connect(str(database), timeout=30) as connection:
            with sqlite3.connect(str(backup_dir / 'requests.db')) as backup:
                connection.backup(backup)
            # Lock writers for the additive transaction. Existing amounts are hashed
            # before and after within one transaction, so concurrent calls cannot skew it.
            connection.execute('BEGIN IMMEDIATE')
            def cost_digest():
                digest = hashlib.sha256()
                for row in connection.execute('SELECT request_id,input_cost,output_cost,cached_cost,total_cost,currency FROM requests ORDER BY request_id'):
                    digest.update(json.dumps(row, ensure_ascii=False).encode())
                return digest.hexdigest()
            before = cost_digest()
            migrate_metadata(connection)
            if cost_digest() != before:
                raise RuntimeError('Historical price integrity check failed')
            connection.commit()
        changes['historical_amounts_unchanged'] = True
    return {**changes, 'applied': True, 'backup_directory': str(backup_dir) if backup_dir.exists() else None}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--apply', action='store_true')
    arguments = parser.parse_args()
    print(json.dumps(migrate(arguments.root, arguments.apply), ensure_ascii=False, indent=2))
