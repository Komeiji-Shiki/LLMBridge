"""将独立来源的 Token 聚合到管理面板，不改变网关请求与成本统计。"""

from copy import deepcopy


def combine_usage(bridge, codex, source):
    result = deepcopy(bridge)
    result['source'] = source
    result['gateway_rate_stats'] = deepcopy(bridge.get('rate_stats', {}))
    for row in result['model_stats']:
        row['source'] = 'bridge'
    if source == 'bridge':
        return result
    if source == 'codex':
        result['model_stats'], result['daily_stats'] = [], []
        for key in ('total_tokens', 'total_input_tokens', 'total_output_tokens', 'total_cached_tokens',
                    'input_cost', 'output_cost', 'total_cost'):
            result[key] = 0
        for key in ('cost_usd', 'cost_cny'):
            result[key] = {'input_cost': 0, 'output_cost': 0, 'total_cost': 0}
        result['cost_by_currency'] = {}
    result['codex_usage'] = codex
    result['cost_scope'] = 'bridge_only' if source == 'all' else 'unavailable'
    for key in ('input_tokens', 'output_tokens', 'cached_tokens', 'tokens'):
        result['total_' + key] += codex.get('total_tokens' if key == 'tokens' else key, 0)
    result['total_reasoning_tokens'] = codex.get('reasoning_tokens', 0)
    result['total_cache_write_tokens'] = codex.get('cache_write_tokens', 0)
    for row in codex['model_stats']:
        result['model_stats'].append({**row, 'source': 'codex', 'display_name': row['model'],
                                      'request_count': 0, 'rpm': 0, 'tpm': 0,
                                      'input_cost': None, 'output_cost': None, 'cached_cost': None,
                                      'total_cost': None, 'currency': None})
    result['model_stats'].sort(key=lambda row: row['total_tokens'], reverse=True)
    days = {row['date']: row for row in result['daily_stats']}
    for row in codex['daily_stats']:
        target = days.setdefault(row['date'], {'date': row['date'], 'input_tokens': 0, 'output_tokens': 0,
                                              'cached_tokens': 0, 'total_tokens': 0, 'cost_usd': 0, 'cost_cny': 0})
        for key in ('input_tokens', 'output_tokens', 'cached_tokens', 'total_tokens', 'reasoning_tokens', 'cache_write_tokens'):
            target[key] = target.get(key, 0) + row.get(key, 0)
    result['daily_stats'] = sorted(days.values(), key=lambda row: row['date'])
    result['models_count'] = len(result['model_stats'])
    return result
