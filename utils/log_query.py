"""监控日志的分页和筛选条件，供 SQLite 与文件回退共用。"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta


@dataclass
class LogQuery:
    limit: int = 50
    offset: int = 0
    model: str | None = None
    status: str | None = None
    search: str | None = None
    start_date: str | None = None
    end_date: str | None = None

    def __post_init__(self):
        if not 1 <= self.limit <= 1000 or self.offset < 0:
            raise ValueError('每页条数必须为 1–1000，偏移量不能小于 0')
        if self.status not in (None, '', 'success', 'failed'):
            raise ValueError('状态必须为 success 或 failed')
        self.start = self._timestamp(self.start_date)
        self.end = self._timestamp(self.end_date, end=True)
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError('开始日期不能晚于结束日期')

    @staticmethod
    def _timestamp(value, end=False):
        if not value:
            return None
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
            # 按服务器本地自然日筛选，结束日期包含当天，兼容夏令时。
            if end:
                parsed += timedelta(days=1)
            return datetime.combine(parsed, time.min).timestamp()
        except (ValueError, TypeError, OverflowError, OSError) as error:
            raise ValueError('日期必须为有效的 YYYY-MM-DD') from error

    def sql(self):
        clauses, params = [], []
        if self.model:
            clauses.append('model = ?')
            params.append(self.model)
        if self.status:
            clauses.append('success = ?')
            params.append(int(self.status == 'success'))
        for bound, operator in ((self.start, '>='), (self.end, '<')):
            if bound is not None:
                clauses.append(f'timestamp {operator} ?')
                params.append(bound)
        if self.search:
            escaped = self.search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            fields = ('request_id', 'model', 'error', 'caller_id', 'caller_name', 'conversation_id')
            clauses.append('(' + ' OR '.join(f"{field} LIKE ? ESCAPE '\\'" for field in fields) + ')')
            params.extend([f'%{escaped}%'] * len(fields))
        return (' WHERE ' + ' AND '.join(clauses) if clauses else ''), params

    def matches(self, entry):
        if self.model and entry.get('model') != self.model:
            return False
        if self.status and bool(entry.get('success', entry.get('status') == 'success')) != (self.status == 'success'):
            return False
        timestamp = entry.get('timestamp') or 0
        if self.start is not None and timestamp < self.start:
            return False
        if self.end is not None and timestamp >= self.end:
            return False
        if self.search:
            fields = ('request_id', 'model', 'error', 'caller_id', 'caller_name', 'conversation_id')
            return any(self.search.lower() in str(entry.get(key) or '').lower() for key in fields)
        return True
