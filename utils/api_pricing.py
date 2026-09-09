"""共享 API 计费：输入总量包含缓存读取与写入，写入单价表示完整单价。"""

from utils.usage_tokens import as_int


def uses_explicit_cache(body):
    """读取实际发往上游的缓存标记，不能用是否命中推测缓存模式。"""
    if not isinstance(body, dict):
        return False
    if body.get('cache_control'):
        return True
    blocks = list(body.get('tools') or [])
    system = body.get('system')
    if isinstance(system, list):
        blocks.extend(system)
    for message in body.get('messages') or []:
        content = message.get('content') if isinstance(message, dict) else None
        if isinstance(content, list):
            blocks.extend(content)
    return any(isinstance(block, dict) and block.get('cache_control') for block in blocks)


def cache_write_usage(usage):
    """返回（全部写入、一小时写入）；累计值和 TTL 明细不重复相加。"""
    if not isinstance(usage, dict):
        return 0, 0
    creation = usage.get('cache_creation') or {}
    one_hour = as_int(creation.get('ephemeral_1h_input_tokens'))
    short = as_int(creation.get('ephemeral_5m_input_tokens'))
    total = as_int(usage.get('cache_creation_input_tokens'))
    total = max(total, short + one_hour, as_int(usage.get('cache_write_tokens')),
                as_int(usage.get('cache_write_input_tokens')))
    for name in ('input_tokens_details', 'prompt_tokens_details'):
        details = usage.get(name) or {}
        total = max(total, as_int(details.get('cache_write_tokens')),
                    as_int(details.get('cache_write_input_tokens')),
                    as_int(details.get('cache_creation_input_tokens')))
        one_hour = max(one_hour, as_int(details.get('cache_write_1h_tokens')))
    total = max(total, one_hour)
    return total, one_hour


def chat_cache_details(cached_tokens, usage):
    """转换为 Chat usage 的缓存明细，同时保留一小时写入量。"""
    writes, one_hour = cache_write_usage(usage)
    if not cached_tokens and not writes:
        return {}
    details = {'cached_tokens': as_int(cached_tokens)}
    if writes:
        details['cache_write_tokens'] = writes
    if one_hour:
        details['cache_write_1h_tokens'] = one_hour
    return {'prompt_tokens_details': details}


def calculate_api_cost(input_tokens, output_tokens, pricing, cached_tokens=0,
                       cache_write_tokens=0, cache_write_1h_tokens=0, upstream_usage=None,
                       explicit_cache=False):
    """缓存写入按完整单价替换普通输入价，额外费用仅作为明细，不再次加总。"""
    inputs, outputs = as_int(input_tokens), as_int(output_tokens)
    # 阶梯以单次请求总输入量选择，命中缓存的输入也参与判断。
    base = pricing
    for tier in sorted(base.get('input_tiers', []), key=lambda item: item['min_input_tokens']):
        if inputs >= tier['min_input_tokens']:
            pricing = {**base, **tier}
    cached = min(as_int(cached_tokens), inputs)
    if upstream_usage is not None:
        cache_write_tokens, cache_write_1h_tokens = cache_write_usage(upstream_usage)
    writes = min(as_int(cache_write_tokens), inputs - cached)
    one_hour = min(as_int(cache_write_1h_tokens), writes)
    unit = float(pricing.get('unit', 1000000))
    input_price = float(pricing.get('input', 0))
    output_price = float(pricing.get('output', 0))
    cached_price = pricing.get('cached_input')
    if explicit_cache:
        cached_price = pricing.get('cached_input_explicit', cached_price)
    write_price = pricing.get('cache_write')
    hour_price = pricing.get('cache_write_1h', write_price)
    # 未配置写入价时保留旧行为：这些 token 仍按普通输入计费。
    short_billable = writes - one_hour if write_price is not None else 0
    hour_billable = one_hour if hour_price is not None else 0
    read_billable = cached if cached_price is not None else 0
    ordinary = inputs - read_billable - short_billable - hour_billable
    input_cost = ordinary * input_price / unit
    cached_cost = read_billable * float(cached_price or 0) / unit
    write_cost = (short_billable * float(write_price or 0)
                  + hour_billable * float(hour_price or 0)) / unit
    extra_cost = write_cost - (short_billable + hour_billable) * input_price / unit
    output_cost = outputs * output_price / unit
    return {
        'input_tokens': inputs, 'output_tokens': outputs, 'cached_tokens': cached,
        'cache_write_tokens': writes, 'cache_write_1h_tokens': one_hour,
        'cache_mode': 'explicit' if explicit_cache else 'implicit',
        'total_tokens': inputs + outputs,
        'input_cost': round(input_cost, 6), 'cached_cost': round(cached_cost, 6),
        'cache_write_cost': round(write_cost, 6),
        'cache_write_extra_cost': round(extra_cost, 6),
        'output_cost': round(output_cost, 6),
        'total_cost': round(input_cost + cached_cost + write_cost + output_cost, 6),
        'currency': pricing.get('currency', 'USD'),
        'pricing': {
            'input_price_per_unit': input_price, 'output_price_per_unit': output_price,
            'cached_input_price_per_unit': cached_price,
            'cache_write_price_per_unit': write_price,
            'cache_write_1h_price_per_unit': hour_price, 'unit': unit,
        },
    }
