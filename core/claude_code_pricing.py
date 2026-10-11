"""Claude Code 按公开标准 API 单价估算，缓存写入按日志中的 TTL 拆分。"""

import re

VERIFIED_AT = '2026-10-12'
SOURCE_URL = 'https://platform.claude.com/docs/en/about-claude/pricing'


def _rate(input_price, output_price, cache_factor=.1, **options):
    return {'input': input_price, 'output': output_price,
            'cached_input': round(input_price * cache_factor, 8),
            'cache_write': input_price * 1.25, 'cache_write_1h': input_price * 2, **options}


PRICES = {
    'claude-fable-5-1': _rate(10, 50, .025),
    'claude-mythos-5-1': _rate(10, 50, .025),
    'claude-fable-5': _rate(10, 50),
    'claude-mythos-5': _rate(10, 50),
    'claude-opus-5-5': _rate(4, 20, .05),
    'claude-opus-5': _rate(5, 25),
    'claude-opus-4-8': _rate(5, 25),
    'claude-opus-4-7': _rate(5, 25),
    'claude-opus-4-6': _rate(5, 25),
    'claude-opus-4-5': _rate(5, 25),
    'claude-opus-4-1': _rate(15, 75),
    'claude-opus-4': _rate(15, 75),
    'claude-sonnet-5-5': _rate(2, 10, .05),
    'claude-sonnet-5': _rate(2, 10),
    'claude-sonnet-4-6': _rate(3, 15),
    'claude-sonnet-4-5': _rate(3, 15),
    'claude-sonnet-4': _rate(3, 15),
    'claude-haiku-5-5': _rate(.1, .5, long_context='request', threshold=100000,
                            long_context_factor=5),
    'claude-haiku-4-5': _rate(1, 5),
    'claude-3-5-haiku': _rate(.8, 4),
}


def canonical_model(model):
    return re.sub(r'-(?:\d{8}|\d{4}-\d{2}-\d{2})$', '', model).replace('.', '-')


def estimate(model, usage, long_context=False):
    rate = PRICES.get(canonical_model(model))
    if rate is None:
        return None
    factor = rate.get('long_context_factor', 1) if long_context else 1
    cached = usage['cached_tokens']
    writes = usage['cache_write_tokens']
    writes_1h = min(writes, usage.get('cache_write_1h_tokens', 0))
    ordinary = max(0, usage['input_tokens'] - cached - writes)
    input_cost = ordinary * rate['input'] * factor / 1_000_000
    cached_cost = cached * rate['cached_input'] * factor / 1_000_000
    write_cost = ((writes - writes_1h) * rate['cache_write'] +
                  writes_1h * rate['cache_write_1h']) * factor / 1_000_000
    extra_cost = write_cost - writes * rate['input'] * factor / 1_000_000
    output_cost = usage['output_tokens'] * rate['output'] * factor / 1_000_000
    return {'input_cost': input_cost, 'cached_cost': cached_cost,
            'cache_write_cost': write_cost, 'cache_write_extra_cost': extra_cost,
            'output_cost': output_cost,
            'total_cost': input_cost + cached_cost + write_cost + output_cost}


def price_metadata():
    return {'basis': 'standard_api_equivalent', 'verified_at': VERIFIED_AT,
            'version': VERIFIED_AT + '-claude-standard-v1', 'currency': 'USD', 'unit': 1_000_000,
            'rates': {model: {**rate, 'verified_at': VERIFIED_AT, 'source_url': SOURCE_URL}
                      for model, rate in PRICES.items()},
            'aliases': {}, 'exclusions': ['subscription_billing', 'fast_mode', 'tools', 'regional_processing']}
