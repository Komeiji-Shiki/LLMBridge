"""合并网关历史统计与本地工具用量、估算金额。"""

from copy import deepcopy

COST_FIELDS = ('input_cost', 'cached_cost', 'output_cost', 'total_cost', 'cache_write_cost', 'cache_write_extra_cost')
TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'cached_tokens', 'cache_write_tokens', 'cache_write_1h_tokens')


def combine_usage(bridge, codex, source, claude_code=None):
    result = deepcopy(bridge)
    result['source'] = source
    result['gateway_rate_stats'] = deepcopy(bridge.get('rate_stats', {}))
    result['cost_scope'] = 'bridge'
    for row in result['model_stats']:
        row['source'] = 'bridge'
        row['cost_kind'] = 'recorded'
    if source == 'bridge':
        return result
    if source != 'all':
        result['model_stats'], result['daily_stats'] = [], []
        for key in ('total_tokens', 'total_input_tokens', 'total_output_tokens', 'total_cached_tokens', 'total_cache_write_tokens', 'total_cache_write_1h_tokens',
                    *COST_FIELDS):
            result[key] = 0
        for key in ('cost_usd', 'cost_cny'):
            result[key] = dict.fromkeys(COST_FIELDS, 0)
        result['cost_by_currency'] = {}
    usages = {key: usage for key, usage in (('codex', codex), ('claude_code', claude_code))
              if usage is not None and source in ('all', key)}
    result['cost_scope'] = 'combined_estimate' if source == 'all' else source + '_estimate'
    if source != 'all' and not any(usage.get('priced_events') for usage in usages.values()):
        result['cost_scope'] = 'unavailable'
    result['pricing_by_source'] = {key: usage.get('pricing', {}) for key, usage in usages.items()}
    result['pricing'] = {'rates': {}, 'aliases': {}, 'unpriced_models': [], 'complete': True}
    result['pricing']['version'] = '+'.join(pricing['version'] for pricing in result['pricing_by_source'].values()
                                          if pricing.get('version'))
    result['unpriced_tokens'] = 0
    result['total_reasoning_tokens'] = 0
    usd_to_cny = bridge.get('exchange_rate', {}).get('USD_TO_CNY', 7.2)
    result['cost_breakdown'] = {'bridge_recorded_usd': deepcopy(bridge.get('cost_usd', {})) if source == 'all' else {}}
    days = {row['date']: row for row in result['daily_stats']}
    for name, usage in usages.items():
        result[name + '_usage'] = usage
        pricing = usage.get('pricing', {})
        combined_pricing = result['pricing']
        for field in ('rates', 'aliases'):
            combined_pricing[field].update(pricing.get(field, {}))
        combined_pricing['unpriced_models'].extend(pricing.get('unpriced_models', []))
        combined_pricing['complete'] &= pricing.get('complete', True)
        for field in ('basis', 'currency', 'unit', 'exclusions'):
            if field in pricing:
                combined_pricing[field] = pricing[field]
        combined_pricing['verified_at'] = max(combined_pricing.get('verified_at', ''), pricing.get('verified_at', ''))
        result['unpriced_tokens'] += usage.get('unpriced_tokens', 0)
        estimates = {key: usage.get(key, 0) or 0 for key in COST_FIELDS}
        result['cost_breakdown'][name + '_estimated_usd'] = estimates
        for key in COST_FIELDS:
            result.setdefault('cost_usd', {}).setdefault(key, result.get(key, 0))
            result.setdefault('cost_cny', {}).setdefault(key, result.get(key, 0) * usd_to_cny)
            result['cost_usd'][key] = round(result['cost_usd'][key] + estimates[key], 6)
            result['cost_cny'][key] = round(result['cost_cny'][key] + estimates[key] * usd_to_cny, 6)
            result[key] = result.get(key, 0) + estimates[key]
            bucket = result.setdefault('cost_by_currency', {}).setdefault('USD', {})
            bucket[key] = bucket.get(key, 0) + estimates[key]
        for field in TOKEN_FIELDS:
            result['total_' + field] = result.get('total_' + field, 0) + usage.get(field, 0)
        result['total_tokens'] += usage.get('total_tokens', 0)
        result['total_reasoning_tokens'] += usage.get('reasoning_tokens', 0)
        for row in usage.get('model_stats', []):
            result['model_stats'].append({**row, 'source': name, 'display_name': row['model'],
                                          'request_count': 0, 'rpm': 0, 'tpm': 0,
                                          'currency': 'USD' if row.get('cost_kind') == 'estimated' else None})
        for row in usage.get('daily_stats', []):
            target = days.setdefault(row['date'], {'date': row['date'], 'input_tokens': 0, 'output_tokens': 0,
                                                  'cached_tokens': 0, 'total_tokens': 0, 'cost_usd': 0, 'cost_cny': 0})
            for field in (*TOKEN_FIELDS, 'total_tokens', 'reasoning_tokens', 'unpriced_tokens'):
                target[field] = target.get(field, 0) + row.get(field, 0)
            cost = row.get('total_cost', 0) or 0
            target['estimated_cost_usd'] = target.get('estimated_cost_usd', 0) + cost
            target['cost_usd'] = target.get('cost_usd', 0) + cost
            target['cost_cny'] = target.get('cost_cny', 0) + cost * usd_to_cny
    result['model_stats'].sort(key=lambda row: row['total_tokens'], reverse=True)
    result['daily_stats'] = sorted(days.values(), key=lambda row: row['date'])
    result['models_count'] = len(result['model_stats'])
    return result
