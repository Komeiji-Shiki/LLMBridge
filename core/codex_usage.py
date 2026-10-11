"""只读扫描 Codex 会话日志，持久化可重建的用量索引。"""

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from core.local_usage_index import LocalUsageIndex


TOKEN_FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens',
                'reasoning_output_tokens', 'total_tokens', 'cache_write_input_tokens')


def _usage(raw):
    if not isinstance(raw, dict):
        return None
    values = [raw.get(key, 0) for key in TOKEN_FIELDS]
    if any(type(value) is not int or value < 0 for value in values):
        return None
    if 'total_tokens' not in raw:
        values[4] = values[0] + values[2]
    return values


def parse_session(path):
    """逐行处理累计差值；返回的字段不包含提示词、回答或凭据。"""
    model, session, provider = 'unknown', path.stem, ''
    previous = [0] * len(TOKEN_FIELDS)
    seen = set()
    with path.open('rb') as stream:
        for line in stream:
            # 活跃日志尾部可能尚未写完，下次文件变化后重新读取。
            if not line.endswith(b'\n'):
                break
            if not any(marker in line for marker in (b'session_meta', b'turn_context', b'token_count')):
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict) or not isinstance(row.get('payload'), dict):
                continue
            payload = row['payload']
            if row.get('type') == 'session_meta':
                session = str(payload.get('id') or session)
                provider = str(payload.get('model_provider') or provider)
            elif row.get('type') == 'turn_context':
                model = str(payload.get('model') or model)
            elif row.get('type') == 'event_msg' and payload.get('type') == 'token_count':
                info = payload.get('info')
                if not isinstance(info, dict):
                    continue
                cumulative = _usage(info.get('total_token_usage'))
                last = _usage(info.get('last_token_usage'))
                try:
                    timestamp = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00')).timestamp()
                except (KeyError, ValueError, TypeError, AttributeError, OverflowError, OSError):
                    continue
                # 标识不含路径和会话 ID，因此归档、复制、分叉继承的同一事件只计一次。
                event_id = hashlib.sha256(json.dumps(
                    [row['timestamp'], model, cumulative, last], separators=(',', ':')
                ).encode()).hexdigest()
                if event_id in seen:
                    continue
                seen.add(event_id)
                if cumulative is not None:
                    if cumulative[4] < previous[4]:
                        # 压缩上下文后累计值可能下降；有本次用量时才能可靠继续计数。
                        delta = last or [0] * len(TOKEN_FIELDS)
                    else:
                        delta = [max(0, current - old) for current, old in zip(cumulative, previous)]
                    previous = cumulative
                elif last is not None:
                    delta = last
                    previous = [old + value for old, value in zip(previous, last)]
                else:
                    continue
                if not any(delta):
                    continue
                yield (event_id, timestamp, datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d'),
                       model, session, provider, *delta)


def discover_homes():
    """支持主目录、额外目录以及 JetBrains 的 Codex 缓存目录。"""
    paths = [Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')]
    paths.extend(Path(value).expanduser() for value in os.environ.get('CODEX_USAGE_HOMES', '').split(os.pathsep) if value)
    bases = [Path.home() / 'Library' / 'Caches' / 'JetBrains']
    bases.extend(Path(os.environ[key]) / 'JetBrains' for key in ('LOCALAPPDATA', 'APPDATA') if os.environ.get(key))
    for base in bases:
        if base.is_dir():
            paths.extend(base.glob('*/aia/codex'))
    return list(dict.fromkeys(path.resolve() for path in paths))


class CodexUsageIndex(LocalUsageIndex):
    label = 'Codex'
    source = 'codex'
    folders = ('sessions', 'archived_sessions')

    def __init__(self, db_path=Path('logs/codex_usage.db'), homes=None):
        super().__init__(db_path, homes)

    @staticmethod
    def discover_homes():
        return discover_homes()

    @staticmethod
    def parse_session(path):
        return parse_session(path)

    @staticmethod
    def summarize(conn, start, end, exclude_providers):
        from core.codex_usage_summary import summarize
        return summarize(conn, start, end, exclude_providers)


codex_usage_index = CodexUsageIndex()
