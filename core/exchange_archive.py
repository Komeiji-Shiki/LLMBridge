"""Permanent, losslessly compressed wire archives; independent of idle caches."""
from datetime import datetime
import gzip
import json
import os
from pathlib import Path


def save_exchange(context, record, root='logs/exchanges'):
    now = datetime.now()
    directory = Path(root) / now.strftime('%Y%m%d') / now.strftime('%H')
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (context.request_id + '.json.gz')
    temporary = target.with_suffix('.tmp')
    metadata = {'gateway_request_id': context.request_id, 'caller_id': context.owner_id,
                'model': context.model, 'conversation_id': context.session_id, **record}
    try:
        with gzip.open(temporary, 'wt', encoding='utf-8', compresslevel=6) as handle:
            json.dump(metadata, handle, ensure_ascii=False, separators=(',', ':'))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
