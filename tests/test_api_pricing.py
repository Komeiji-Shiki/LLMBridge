"""缓存写入从上游 usage 到持久化统计的计费回归。"""
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from utils.api_pricing import cache_write_usage, calculate_api_cost


@pytest.mark.parametrize('usage,expected', [
    ({'input_tokens_details': {'cache_write_tokens': 40}}, (40, 0)),
    ({'prompt_tokens_details': {'cache_creation_input_tokens': '40'}}, (40, 0)),
    ({'cache_write_input_tokens': 40}, (40, 0)),
    ({'cache_creation_input_tokens': 40,
      'cache_creation': {'ephemeral_5m_input_tokens': 30, 'ephemeral_1h_input_tokens': 10}}, (40, 10)),
    ({'cache_creation': {'ephemeral_5m_input_tokens': 30, 'ephemeral_1h_input_tokens': 10}}, (40, 10)),
    ({'prompt_tokens_details': None}, (0, 0)),
])
def test_provider_write_counts_do_not_double_count_totals_and_details(usage, expected):
    assert cache_write_usage(usage) == expected


def test_mixed_cache_lifetimes_replace_input_price():
    price = {'input': 3, 'output': 15, 'cached_input': .3, 'cache_write': 3.75, 'cache_write_1h': 6}
    cost = calculate_api_cost(1000000, 100000, price, 400000, 300000, 100000)
    assert cost['input_cost'] == .9
    assert cost['cached_cost'] == .12
    assert cost['cache_write_cost'] == 1.35
    assert cost['cache_write_extra_cost'] == .45
    assert cost['total_cost'] == 3.87


def test_cache_writes_survive_chat_responses_and_anthropic_conversions():
    from converters.responses_openai import _usage_to_responses
    from converters.responses_bridge import _usage_to_chat
    from converters.anthropic_openai import convert_openai_usage_to_anthropic, extract_anthropic_usage_tokens
    from utils.api_pricing import chat_cache_details
    native = {'input_tokens': 100, 'cache_read_input_tokens': 200, 'cache_creation_input_tokens': 50,
              'cache_creation': {'ephemeral_5m_input_tokens': 30, 'ephemeral_1h_input_tokens': 20}, 'output_tokens': 10}
    inputs, outputs, cached = extract_anthropic_usage_tokens(native)
    chat = {'prompt_tokens': inputs, 'completion_tokens': outputs, 'total_tokens': inputs + outputs,
            **chat_cache_details(cached, native)}
    back = _usage_to_chat(_usage_to_responses(chat))
    assert back == chat
    assert convert_openai_usage_to_anthropic(back) == native


def test_launch_price_update_preserves_endpoints_and_rejects_unknown_versions():
    from scripts.update_launch_prices import update_prices
    catalog = {'claude-opus-4-1': {'pricing': {'input': 15}, 'sources': []},
               'gpt-5': {'pricing': {'input': 1.25}, 'sources': []}}
    configs = {'// 注释': '', 'old': [{'model_id': 'claude-opus-4-1-20250805', 'api_key': 'fixture-key',
                                    'archived': True}], 'future': {'model_id': 'gpt-5.9'},
               'local': {'model_id': 'claude-opus-4-1-exl3'}}
    result, report = update_prices(configs, catalog)
    assert result['old'][0] == {**configs['old'][0], 'pricing': {'input': 15}}
    assert result['future'] == configs['future']
    assert result['local'] == configs['local']
    assert 'pricing' not in configs['old'][0]
    assert 'fixture-key' not in json.dumps(report)


def test_missing_write_rate_keeps_legacy_input_billing_and_zero_is_valid():
    price = {'input': 2, 'output': 3, 'cached_input': .2}
    cost = calculate_api_cost(1000000, 0, price, 200000, 300000)
    assert cost['total_cost'] == 1.64
    assert cost['cache_write_cost'] == cost['cache_write_extra_cost'] == 0
    free = calculate_api_cost(100, 100, {'input': 0, 'output': 0, 'cache_write': 0}, cache_write_tokens=100)
    assert free['total_cost'] == 0


def test_explicit_cache_and_tier_selection():
    price = {'input': 2, 'output': 8, 'cached_input': .4, 'cached_input_explicit': .2, 'cache_write': 2.5,
             'input_tiers': [{'min_input_tokens': 256001, 'input': 6, 'output': 24,
                              'cached_input': 1.2, 'cached_input_explicit': .6, 'cache_write': 7.5}]}
    assert calculate_api_cost(256000, 0, price, 256000)['cached_cost'] == .1024
    cost = calculate_api_cost(300000, 0, price, 200000, 100000, explicit_cache=True)
    assert cost['cached_cost'] == .12
    assert cost['cache_write_cost'] == .75
    assert cost['total_cost'] == .87


@pytest.mark.parametrize('stream', [False, True])
def test_native_responses_usage_reaches_real_calculator(stream):
    from services.direct_api_service import DirectAPIService
    from services.native_exchange import forward_native_exchange

    async def run():
        service = DirectAPIService(MagicMock())
        monitor = MagicMock()
        monitor.broadcast_to_monitors = AsyncMock()
        response = {'id': 'r', 'output': [], 'usage': {'input_tokens': 100000,
                    'output_tokens': 1000, 'input_tokens_details': {'cached_tokens': 60000, 'cache_write_tokens': 20000}}}
        async def upstream(**kwargs):
            payload = {'type': 'response.completed', 'response': response} if stream else response
            raw = json.dumps(payload)
            yield (('data: ' + raw + '\n\n') if stream else raw).encode()
        service.call_api_passthrough = upstream
        config = {'provider': 'openai', 'api_type': 'responses_native', 'api_key': 'test',
                  'api_base_url': 'https://example.test', 'pricing': {
                      'input': 10, 'output': 50, 'cached_input': 1, 'cache_write': 12.5}}
        result = await forward_native_exchange({'input': 'hello'}, config, 'alias', service, monitor, stream=stream)
        if stream:
            _ = [part async for part in result.body_iterator]
        ended = monitor.request_end.call_args.kwargs
        assert ended['cost_info']['cache_write_tokens'] == 20000
        assert ended['cost_info']['cache_write_extra_cost'] == .05
        assert ended['cost_info']['total_cost'] == .56
    asyncio.run(run())


def test_sqlite_details_statistics_csv_and_reestimate_preserve_write_costs(tmp_path):
    from modules.monitoring_sqlite import SQLiteLogger
    from core.statistics_queries import query_token_stats
    from core.usage_analysis import estimate_current_prices
    from routes.admin_usage import usage_csv
    import sqlite3

    path = tmp_path / 'requests.db'
    writer = SQLiteLogger(path)
    price = {'input': 10, 'output': 50, 'cached_input': 1, 'cache_write': 12.5,
             'input_tiers': [{'min_input_tokens': 272001, 'input': 20, 'output': 75,
                              'cached_input': 2, 'cache_write': 25}]}
    # 合计跨过阈值，但每次请求都在短上下文档，重新估价不得混淆。
    for i in range(3):
        cost = calculate_api_cost(100000, 1000, price, 60000, 20000)
        writer.write_request({'type': 'request_end', 'request_id': str(i), 'model': 'demo',
                              'timestamp': time.time(), 'success': True, 'input_tokens': 100000,
                              'output_tokens': 1000, 'cached_tokens': 60000, 'cost_info': cost})
    details = writer.get_request_details('0')
    assert details['cache_write_cost'] == .25
    assert writer.query_requests()['items'][0]['cache_write_tokens'] == 20000
    with sqlite3.connect(path) as connection:
        stats = query_token_stats(connection, None, None, {'demo': {'pricing': price}}, None, 7.2)
    assert stats['total_cache_write_tokens'] == 60000
    assert stats['cost_usd']['cache_write_extra_cost'] == .15
    assert stats['cost_usd']['total_cost'] == 1.68
    estimate = estimate_current_prices(path, {'demo': {'pricing': price}})['items'][0]
    assert estimate['current_estimate'] == 1.68
    assert estimate['historical_cost'] == pytest.approx(1.68)
    assert estimate['current_cache_write_cost'] == .75
    csv = usage_csv(stats).body.decode('utf-8-sig')
    assert '缓存写入额外成本' in csv
    assert '60000' in csv
    # 重新估算始终只读。
    assert writer.get_request_details('0')['total_cost'] == .56
    writer.close()


def test_unpriced_usage_is_counted_and_legacy_estimate_can_recover_writes(tmp_path):
    from modules.monitoring_sqlite import SQLiteLogger
    from core.usage_analysis import estimate_current_prices
    import sqlite3
    path = tmp_path / 'requests.db'
    writer = SQLiteLogger(path)
    writer.write_request({'type': 'request_end', 'request_id': 'unpriced', 'model': 'demo',
                          'timestamp': time.time(), 'success': True, 'input_tokens': 1000,
                          'upstream_usage': {'input_tokens_details': {'cache_write_tokens': 1000}}})
    assert writer.get_request_details('unpriced')['cache_write_tokens'] == 1000
    # 模拟迁移前记录：原始 usage 已存，但新增列还没有历史数据。
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE requests SET cache_write_tokens=0')
    estimate = estimate_current_prices(path, {'demo': {'pricing': {'input': 10, 'cache_write': 12.5}}})
    assert estimate['items'][0]['current_estimate'] == .0125
    assert writer.get_request_details('unpriced')['total_cost'] == 0
    writer.close()
