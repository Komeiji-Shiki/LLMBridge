"""按已核对的标准 API 单价估算 Codex Token 价值，不代表订阅扣费。"""

import re

VERIFIED_AT = '2026-09-27'
PRICE_VERSION = '2026-09-27-standard-v2'
SOURCE_ROOT = 'https://developers.openai.com/api/docs/models/'

# 单位为 USD / 百万 Token；上下文范围遵循对应模型页的 request/session 说明。
PRICES = {
    'gpt-6-astra': {'input': 10, 'cached_input': 1, 'output': 50, 'cache_write': 12.5, 'long_context': 'request'},
    'gpt-6-sol': {'input': 2, 'cached_input': .2, 'output': 10, 'cache_write': 2.5,
                  'long_context': 'request', 'verified_at': '2026-09-27'},
    'gpt-5.6-sol': {'input': 4, 'cached_input': .4, 'output': 20, 'cache_write': 5, 'long_context': 'request'},
    'gpt-5.6-terra': {'input': 2, 'cached_input': .2, 'output': 12, 'cache_write': 2.5, 'long_context': 'request'},
    'gpt-5.6-luna': {'input': .2, 'cached_input': .02, 'output': 1.2, 'cache_write': .25, 'long_context': 'request'},
    'gpt-5.5': {'input': 5, 'cached_input': .5, 'output': 30, 'long_context': 'session'},
    'gpt-5.4': {'input': 2.5, 'cached_input': .25, 'output': 15, 'long_context': 'session'},
    'gpt-5.4-mini': {'input': .75, 'cached_input': .075, 'output': 4.5},
    'gpt-5.3-codex': {'input': 1.75, 'cached_input': .175, 'output': 14},
    'gpt-5.2-codex': {'input': 1.75, 'cached_input': .175, 'output': 14},
}

# auto-review 按用户指定的 Sol 单价估算，保留原始模型名称用于统计。
MODEL_ALIASES = {'gpt-5.6': 'gpt-5.6-sol', 'codex-auto-review': 'gpt-6-sol'}


def canonical_model(model):
    # 仅应用明确的计价映射；Spark 和其他未知模型仍保留未定价状态。
    model = re.sub(r'-\d{4}-\d{2}-\d{2}$', '', model)
    return MODEL_ALIASES.get(model, model)


def estimate(model, usage, long_context=False):
    model = canonical_model(model)
    rate = PRICES.get(model)
    if rate is None:
        return None
    input_factor = 2 if long_context else 1
    output_factor = 1.5 if long_context else 1
    cached = min(usage['cached_tokens'], usage['input_tokens'])
    writes = min(usage.get('cache_write_tokens', 0), usage['input_tokens'] - cached)
    # 没有单独 cache-write 价的模型，写入仍归入普通输入，不能重复计费。
    billable_writes = writes if 'cache_write' in rate else 0
    input_cost = (usage['input_tokens'] - cached - billable_writes) * rate['input'] * input_factor / 1_000_000
    write_cost = billable_writes * rate.get('cache_write', 0) * input_factor / 1_000_000
    extra_cost = write_cost - billable_writes * rate['input'] * input_factor / 1_000_000
    cached_cost = cached * rate['cached_input'] * input_factor / 1_000_000
    output_cost = usage['output_tokens'] * rate['output'] * output_factor / 1_000_000
    return {'input_cost': input_cost, 'cached_cost': cached_cost, 'output_cost': output_cost,
            'cache_write_cost': write_cost, 'cache_write_extra_cost': extra_cost,
            'total_cost': input_cost + cached_cost + write_cost + output_cost}


def price_metadata():
    return {'basis': 'standard_api_equivalent', 'verified_at': VERIFIED_AT, 'version': PRICE_VERSION,
            'currency': 'USD', 'unit': 1_000_000,
            # 其余模型继续沿用原快照，核对日期按模型保留。
            'rates': {model: {**rate, 'verified_at': rate.get('verified_at', '2026-09-09'),
                              'source_url': SOURCE_ROOT + model} for model, rate in PRICES.items()},
            'aliases': dict(MODEL_ALIASES),
            'exclusions': ['subscription_billing', 'fast_mode', 'tools', 'regional_processing']}
