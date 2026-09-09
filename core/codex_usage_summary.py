"""一次读取去重事件，按模型、日期和计价档位汇总。"""

from core.codex_pricing import PRICES, canonical_model, estimate, price_metadata

TOKEN_FIELDS = ('input_tokens', 'cached_tokens', 'output_tokens', 'reasoning_tokens', 'total_tokens', 'cache_write_tokens')
COST_FIELDS = ('input_cost', 'cached_cost', 'output_cost', 'total_cost')


def _empty():
    return {**dict.fromkeys(TOKEN_FIELDS, 0), **dict.fromkeys(COST_FIELDS, 0.0),
            'event_count': 0, 'unpriced_tokens': 0, 'priced_events': 0, 'long_context_events': 0}


def summarize(conn, start_ts=None, end_ts=None, exclude_providers=()):
    conn.create_function('price_model', 1, canonical_model, deterministic=True)
    session_models = [model for model, price in PRICES.items() if price.get('long_context') == 'session']
    request_models = [model for model, price in PRICES.items() if price.get('long_context') == 'request']
    # 先在完整会话中确定上下文档位，再筛日期，避免筛选改变 session 级计价。
    marks = ','.join('?' for _ in session_models)
    request_marks = ','.join('?' for _ in request_models)
    where, params = [], session_models + request_models
    for value, op in ((start_ts, '>='), (end_ts, '<')):
        if value is not None:
            where.append(f'timestamp {op} ?')
            params.append(value)
    clause = ' WHERE ' + ' AND '.join(where) if where else ''
    sums = ','.join(f'SUM({field}) AS {field}' for field in TOKEN_FIELDS)
    query = f'''
        WITH context_events AS (
            SELECT *, CASE
                WHEN price_model(model) IN ({marks}) THEN
                    MAX(input_tokens) OVER (PARTITION BY session, model) > 272000
                WHEN price_model(model) IN ({request_marks}) THEN input_tokens > 272000
                ELSE 0 END AS long_context
            FROM unique_events
        )
        SELECT model, date, session, provider, long_context, {sums}, COUNT(*) AS event_count
        FROM context_events {clause}
        GROUP BY model, date, session, provider, long_context
    '''
    models, days, sessions, model_sessions = {}, {}, set(), {}
    totals = _empty()
    excluded = {'event_count': 0, 'total_tokens': 0, 'providers': list(exclude_providers)}
    unpriced_models = set()
    for raw in conn.execute(query, params):
        row = dict(raw)
        if row['provider'] in exclude_providers:
            for key in ('event_count', 'total_tokens'):
                excluded[key] += row[key]
            continue
        model, day = row['model'], row['date']
        sessions.add(row['session'])
        model_sessions.setdefault(model, set()).add(row['session'])
        costs = estimate(model, row, row['long_context'])
        if costs is None:
            unpriced_models.add(model)
        targets = [totals, models.setdefault(model, {'model': model, **_empty()}),
                   days.setdefault(day, {'date': day, **_empty()})]
        for target in targets:
            for field in TOKEN_FIELDS + ('event_count',):
                target[field] += row[field]
            if costs is None:
                target['unpriced_tokens'] += row['total_tokens']
            else:
                target['priced_events'] += row['event_count']
                target['long_context_events'] += row['event_count'] if row['long_context'] else 0
                for field in COST_FIELDS:
                    target[field] += costs[field]
    for model, row in models.items():
        row['session_count'] = len(model_sessions[model])
        row['price_model'] = canonical_model(model)
        row['cost_kind'] = 'estimated' if row['priced_events'] else 'unpriced'
        if not row['priced_events']:
            for field in COST_FIELDS:
                row[field] = None
    return {**totals, 'session_count': len(sessions), 'excluded_usage': excluded,
            'model_stats': sorted(models.values(), key=lambda row: row['total_tokens'], reverse=True),
            'daily_stats': sorted(days.values(), key=lambda row: row['date']),
            'pricing': {**price_metadata(), 'unpriced_models': sorted(unpriced_models),
                        'complete': not unpriced_models}}
