"""合并 Token、网关历史金额和 Codex 估算；历史记录保持不变。"""

from copy import deepcopy

COST_FIELDS = ('input_cost', 'cached_cost', 'output_cost', 'total_cost')


def combine_usage(bridge, codex, source):
    result = deepcopy(bridge)
    result['source'] = source
    result['gateway_rate_stats'] = deepcopy(bridge.get('rate_stats', {}))
    result['cost_scope'] = 'bridge'
    for row in result['model_stats']:
        row['source'] = 'bridge'
        row['cost_kind'] = 'recorded'
    if source == 'bridge':
        return result
    if source == 'codex':
        result['model_stats'], result['daily_stats'] = [], []
        for key in ('total_tokens', 'total_input_tokens', 'total_output_tokens', 'total_cached_tokens',
                    *COST_FIELDS):
            result[key] = 0
        for key in ('cost_usd', 'cost_cny'):
            result[key] = dict.fromkeys(COST_FIELDS, 0)
        result['cost_by_currency'] = {}
    result['codex_usage'] = codex
    result['cost_scope'] = 'combined_estimate' if source == 'all' else 'codex_estimate'
    if source == 'codex' and not codex.get('priced_events'):
        result['cost_scope'] = 'unavailable'
    if source == 'codex' and not codex.get('priced_events'):
        result['cost_scope'] = 'unavailable'
    result['pricing'] = codex.get('pricing', {})
    result['unpriced_tokens'] = codex.get('unpriced_tokens', 0)
    usd_to_cny = bridge.get('exchange_rate', {}).get('USD_TO_CNY', 7.2)
    estimates = {key: codex.get(key, 0) or 0 for key in COST_FIELDS}
    result['cost_breakdown'] = {'codex_estimated_usd': estimates,
                              'bridge_recorded_usd': deepcopy(bridge.get('cost_usd', {})) if source == 'all' else {}}
    for key in COST_FIELDS:
        result.setdefault('cost_usd', {}).setdefault(key, result.get(key, 0))
        result.setdefault('cost_cny', {}).setdefault(key, result.get(key, 0) * usd_to_cny)
        result['cost_usd'][key] = round(result['cost_usd'][key] + estimates[key], 6)
        result['cost_cny'][key] = round(result['cost_cny'][key] + estimates[key] * usd_to_cny, 6)
        result[key] = result.get(key, 0) + estimates[key]
        bucket = result.setdefault('cost_by_currency', {}).setdefault('USD', {})
        bucket[key] = bucket.get(key, 0) + estimates[key]
    for key in ('input_tokens', 'output_tokens', 'cached_tokens', 'tokens'):
        result['total_' + key] += codex.get('total_tokens' if key == 'tokens' else key, 0)
    result['total_reasoning_tokens'] = codex.get('reasoning_tokens', 0)
    result['total_cache_write_tokens'] = codex.get('cache_write_tokens', 0)
    for row in codex['model_stats']:
        result['model_stats'].append({**row, 'source': 'codex', 'display_name': row['model'],
                                      'request_count': 0, 'rpm': 0, 'tpm': 0,
                                      'currency': 'USD' if row.get('cost_kind') == 'estimated' else None})
    result['model_stats'].sort(key=lambda row: row['total_tokens'], reverse=True)
    days = {row['date']: row for row in result['daily_stats']}
    for row in codex['daily_stats']:
        target = days.setdefault(row['date'], {'date': row['date'], 'input_tokens': 0, 'output_tokens': 0,
                                              'cached_tokens': 0, 'total_tokens': 0, 'cost_usd': 0, 'cost_cny': 0})
        for key in ('input_tokens', 'output_tokens', 'cached_tokens', 'total_tokens', 'reasoning_tokens', 'cache_write_tokens'):
            target[key] = target.get(key, 0) + row.get(key, 0)
        target['estimated_cost_usd'] = row.get('total_cost', 0)
        target['cost_usd'] += row.get('total_cost', 0)
        target['cost_cny'] += row.get('total_cost', 0) * usd_to_cny
        target['unpriced_tokens'] = row.get('unpriced_tokens', 0)
    result['daily_stats'] = sorted(days.values(), key=lambda row: row['date'])
    result['models_count'] = len(result['model_stats'])
    return result
