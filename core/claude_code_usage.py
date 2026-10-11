"""只读导入 Claude Code 的本地 JSONL，按调用合并流式记录和复制历史。"""

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from core.local_usage_index import LocalUsageIndex


def discover_homes():
    paths = [Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude').expanduser()]
    paths.extend(Path(value).expanduser() for value in
                 os.environ.get('CLAUDE_CODE_USAGE_HOMES', '').split(os.pathsep) if value)
    return list(dict.fromkeys(path.resolve() for path in paths))


def _counts(usage):
    keys = ('input_tokens', 'cache_read_input_tokens', 'output_tokens', 'cache_creation_input_tokens')
    cache = usage.get('cache_creation') or {}
    details = usage.get('output_tokens_details') or {}
    if not isinstance(cache, dict) or not isinstance(details, dict):
        return None
    values = [usage.get(key, 0) for key in keys]
    values += [details.get('thinking_tokens', 0), cache.get('ephemeral_1h_input_tokens', 0),
               cache.get('ephemeral_5m_input_tokens', 0)]
    if any(type(value) is not int or value < 0 for value in values):
        return None
    # 部分记录只包含 TTL 明细，后续重复记录可能才补齐写入总量。
    values[3] = max(values[3], values[5] + values[6])
    return values[:6]


def parse_session(path):
    events = {}
    with path.open('rb') as stream:
        for line in stream:
            if not line.endswith(b'\n'):
                break
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict) or row.get('type') != 'assistant':
                continue
            message = row.get('message')
            if not isinstance(message, dict):
                continue
            model = message.get('model')
            if not isinstance(model, str) or not model or model.startswith('<'):
                continue
            usage = message.get('usage')
            if not isinstance(usage, dict):
                continue
            try:
                timestamp = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00')).timestamp()
            except (KeyError, ValueError, TypeError, AttributeError, OverflowError, OSError):
                continue
            session = str(row.get('sessionId') or path.stem)
            message_id = message.get('id') or row.get('uuid')
            identity = [message_id, row.get('requestId')] if message_id else [session, row['timestamp'], model]
            event_id = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
            provider = str(message.get('providerId') or message.get('provider_id') or message.get('provider')
                           or row.get('providerId') or row.get('provider') or '')
            # 顶层 usage 已累计主模型 iterations；advisor 则是额外调用，需独立导入。
            candidates = [(event_id, model, usage)]
            iterations = usage.get('iterations')
            if isinstance(iterations, list):
                candidates.extend((event_id + ':advisor:' + str(index), item['model'], item)
                                  for index, item in enumerate(iterations)
                                  if isinstance(item, dict) and item.get('type') == 'advisor_message'
                                  and isinstance(item.get('model'), str) and item['model']
                                  and not item['model'].startswith('<'))
            for identity, model, raw in candidates:
                counts = _counts(raw)
                if counts is None:
                    continue
                split = 'input_tokens' in raw and any(key in raw for key in
                         ('cache_read_input_tokens', 'cache_creation_input_tokens', 'cache_creation'))
                if identity not in events:
                    events[identity] = [timestamp, model, session, provider, counts, split]
                    continue
                existing = events[identity]
                old = existing[4]
                # 完整缓存拆分中的普通输入优先于未拆分的整个提示词快照。
                if split and not existing[5]:
                    ordinary = counts[0]
                elif existing[5] and not split:
                    ordinary = old[0]
                else:
                    ordinary = max(old[0], counts[0])
                existing[4] = [ordinary, *(max(a, b) for a, b in zip(old[1:], counts[1:]))]
                existing[5] = existing[5] or split
                if provider:
                    existing[3] = provider
    for event_id, (timestamp, model, session, provider, counts, _) in events.items():
        ordinary, cached, output, writes, reasoning, writes_1h = counts
        total_input = ordinary + cached + writes
        if total_input + output == 0:
            continue
        yield (event_id, timestamp, datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d'),
               model, session, provider, total_input, cached, output, min(reasoning, output),
               total_input + output, writes, min(writes_1h, writes))


class ClaudeCodeUsageIndex(LocalUsageIndex):
    label = 'Claude Code'
    source = 'claude_code'
    folders = ('projects', 'transcripts')
    token_fields = (*LocalUsageIndex.token_fields, 'cache_write_1h_tokens')

    def __init__(self, db_path=Path('logs/claude_code_usage.db'), homes=None):
        super().__init__(db_path, homes)

    @staticmethod
    def discover_homes():
        return discover_homes()

    @staticmethod
    def parse_session(path):
        return parse_session(path)

    def summarize(self, conn, start, end, exclude_providers):
        from core import claude_code_pricing
        from core.codex_usage_summary import summarize
        return summarize(conn, start, end, exclude_providers,
                         pricing=claude_code_pricing, token_fields=self.token_fields)

    def create_event_view(self, conn):
        # 跨文件也合并各项最完整计数，避免排序在前的流式副本覆盖完整记录。
        conn.execute('''
            CREATE VIEW IF NOT EXISTS unique_events AS
            WITH merged AS (
                SELECT event_id, MIN(timestamp) AS timestamp, MIN(date) AS date, model,
                    MIN(session) AS session, MAX(provider) AS provider,
                    CASE WHEN MAX(cached_tokens + cache_write_tokens) > 0
                         THEN MAX(CASE WHEN cached_tokens + cache_write_tokens > 0
                                  THEN input_tokens - cached_tokens - cache_write_tokens END)
                         ELSE MAX(input_tokens) END AS ordinary_tokens,
                    MAX(cached_tokens) AS cached_tokens, MAX(output_tokens) AS output_tokens,
                    MAX(reasoning_tokens) AS reasoning_tokens, MAX(cache_write_tokens) AS cache_write_tokens,
                    MIN(MAX(cache_write_1h_tokens), MAX(cache_write_tokens)) AS cache_write_1h_tokens
                FROM events GROUP BY event_id
            )
            SELECT *, ordinary_tokens + cached_tokens + cache_write_tokens AS input_tokens,
                      ordinary_tokens + cached_tokens + cache_write_tokens + output_tokens AS total_tokens
            FROM merged
        ''')


claude_code_usage_index = ClaudeCodeUsageIndex()
