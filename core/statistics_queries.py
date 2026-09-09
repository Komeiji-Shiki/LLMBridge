"""统计查询只扫描必要的分组；Token 和金额总计复用模型聚合结果。"""

import time

TOKENS = ('input_tokens', 'output_tokens', 'total_tokens', 'cached_tokens')
COSTS = ('input_cost', 'cached_cost', 'output_cost', 'total_cost')


def query_token_stats(conn, start_ts, end_ts, model_config, rpm_period, usd_to_cny):
    where, params = [], []
    for value, operator in ((start_ts, '>='), (end_ts, '<')):
        if value is not None:
            where.append(f'timestamp {operator} ?')
            params.append(value)
    clause = ' WHERE ' + ' AND '.join(where) if where else ''
    fields = TOKENS + COSTS
    sums = ','.join(f'COALESCE(SUM({key}),0)' for key in fields)
    grouped = conn.execute(f'''
        SELECT model, COALESCE(currency,'USD'), COUNT(*), {sums},
               SUM(CASE WHEN total_cost > 0 THEN 1 ELSE 0 END)
        FROM requests {clause} GROUP BY model, COALESCE(currency,'USD')
    ''', params)
    models, costs_by_currency = {}, {}
    totals = dict.fromkeys(TOKENS, 0)
    for model, currency, count, *amounts in grouped:
        item = models.setdefault(model, {'model': model, 'request_count': 0,
                                         **dict.fromkeys(TOKENS, 0), '_currencies': {}})
        item['request_count'] += count
        values = dict(zip(fields, amounts[:-1]))
        for key in TOKENS:
            item[key] += values[key]
            totals[key] += values[key]
        item['_currencies'][currency] = {'count': count, 'paid': amounts[-1], **values}
        cost_group = costs_by_currency.setdefault(currency, dict.fromkeys(COSTS, 0.0))
        for key in COSTS:
            cost_group[key] += values[key]

    now = time.time()
    minutes = 60.0 if rpm_period == 'hour' else 1440.0
    rates = {row[0]: {'request_count': row[1], 'total_tokens': row[2] or 0} for row in conn.execute('''
        SELECT model, COUNT(*), SUM(total_tokens) FROM requests
        WHERE timestamp >= ? AND timestamp <= ? GROUP BY model
    ''', (now - minutes * 60, now))}

    for model, item in models.items():
        config = (model_config or {}).get(model, {})
        config = config[0] if isinstance(config, list) and config else config
        config = config if isinstance(config, dict) else {}
        pricing = config.get('pricing') or {}
        currencies = item.pop('_currencies')
        usd, cny = currencies.get('USD', {}), currencies.get('CNY', {})
        currency = pricing.get('currency') if isinstance(pricing, dict) else None
        if not currency:
            if usd.get('paid', 0) != cny.get('paid', 0):
                currency = 'USD' if usd.get('paid', 0) > cny.get('paid', 0) else 'CNY'
            elif usd.get('count', 0) != cny.get('count', 0):
                currency = 'USD' if usd.get('count', 0) > cny.get('count', 0) else 'CNY'
            else:
                currency = max(currencies) or 'USD'
        item.update(display_name=config.get('display_name', model), currency=currency,
                    rpm=round(rates.get(model, {}).get('request_count', 0) / minutes, 2),
                    tpm=round(rates.get(model, {}).get('total_tokens', 0) / minutes, 2))
        for key in COSTS:
            item[key] = sum(values[key] * (usd_to_cny if unit == 'USD' and currency == 'CNY'
                                          else 1 / usd_to_cny if unit == 'CNY' and currency == 'USD' else 1)
                            for unit, values in currencies.items())

    daily = []
    token_sums = ','.join(f'COALESCE(SUM({key}),0)' for key in TOKENS)
    for day, *values in conn.execute(f'''
        SELECT date, {token_sums},
            SUM(CASE WHEN currency='CNY' THEN COALESCE(total_cost,0)/? ELSE COALESCE(total_cost,0) END)
        FROM requests {clause} GROUP BY date ORDER BY date
    ''', [usd_to_cny, *params]):
        daily.append({'date': day, **dict(zip(TOKENS, values[:-1])),
                      'cost_usd': round(values[-1] or 0, 6),
                      'cost_cny': round((values[-1] or 0) * usd_to_cny, 6)})

    cost_usd = {key: sum(values[key] / usd_to_cny if unit == 'CNY' else values[key]
                         for unit, values in costs_by_currency.items()) for key in COSTS}
    return {'model_stats': sorted(models.values(), key=lambda row: row['total_tokens'], reverse=True),
            'daily_stats': daily, 'total_input_tokens': totals['input_tokens'],
            'total_output_tokens': totals['output_tokens'], 'total_tokens': totals['total_tokens'],
            'total_cached_tokens': totals['cached_tokens'], **cost_usd, 'currency': 'USD',
            'cost_usd': {key: round(value, 6) for key, value in cost_usd.items()},
            'cost_cny': {key: round(value * usd_to_cny, 6) for key, value in cost_usd.items()},
            'cost_by_currency': costs_by_currency,
            'exchange_rate': {'USD_TO_CNY': usd_to_cny, 'CNY_TO_USD': 1 / usd_to_cny},
            'rate_stats': {'period': 'hour' if rpm_period == 'hour' else 'day', 'minutes': minutes,
                           'request_count': sum(row['request_count'] for row in rates.values()),
                           'total_tokens': sum(row['total_tokens'] for row in rates.values())},
            'models_count': len(models)}
