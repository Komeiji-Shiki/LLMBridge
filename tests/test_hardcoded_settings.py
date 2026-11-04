"""配置项与模型家族判断：行为由配置决定，默认值只有一处定义。"""
import asyncio
import json
import logging
import re
import runpy
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request

from core.constants import GeminiDefaults, RetryDefaults, ServerDefaults, TimeoutDefaults, resolve_first_chunk_timeout, resolve_heartbeat_interval
from routes._direct_api_passthrough import _prepare_passthrough_request
from services.provider_capabilities import diagnose_model, model_family, provider_name, validate_tool_config
from utils.usage_tokens import rough_message_tokens, rough_token_count

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize('config,names,expected', [
    ({}, ['gl/gemini-4-argon-high'], 'gemini'),
    ({}, ['alias', 'DeepSeek-V4'], 'deepseek'),
    ({}, ['qwen3-max'], 'qwen'),
    ({}, ['gpt-6'], None),
    ({'model_family': 'gemini'}, ['my-alias'], 'gemini'),
    ({'model_family': 'other'}, ['gemini-lookalike'], None),
    ({'api_base_url': 'https://api.deepseek.com/v1'}, ['my-alias'], 'deepseek'),
    ({'api_base_url': 'https://dashscope.aliyuncs.com/v1'}, ['deepseek-r1'], 'deepseek'),
    ({'api_base_url': 'https://dashscope.aliyuncs.com/v1'}, ['plain'], 'qwen'),
    ({'provider': 'openai'}, ['plain'], None),
])
def test_model_family_priority(config, names, expected):
    assert model_family(config, *names) == expected


def test_unknown_model_family_is_rejected():
    with pytest.raises(ValueError):
        validate_tool_config({'model_family': 'typo'})
    validate_tool_config({'model_family': 'other'})


@pytest.mark.parametrize('config,expected', [
    ({'api_base_url': 'https://openrouter.ai/api/v1'}, 'openrouter'),
    ({'api_base_url': 'https://eu.openrouter.ai/api/v1'}, 'openrouter'),
    ({'api_base_url': 'https://relay.example/openrouter.ai/v1'}, 'custom'),
    ({'api_base_url': 'https://openrouter.ai/api/v1', 'provider': 'custom'}, 'custom'),
    ({'api_base_url': 'https://relay.example/v1', 'provider': 'openrouter'}, 'openrouter'),
])
def test_openrouter_is_detected_by_host_or_explicit_provider(config, expected):
    assert provider_name(config) == expected


def test_openrouter_has_a_valid_diagnosis():
    assert diagnose_model('demo', {'api_base_url': 'https://openrouter.ai/api/v1', 'api_key': 'k'})['provider'] == 'openrouter'


@pytest.mark.parametrize('url', [
    'https://www.workbuddy.cn/v2', 'https://www.workbuddy.ai/v2',
    'https://www.codebuddy.cn/v2', 'https://copilot.tencent.com/v2',
])
def test_workbuddy_is_detected_by_host(url):
    assert provider_name({'api_base_url': url}) == 'workbuddy'


@pytest.mark.parametrize('url', ['https://relay.example/www.workbuddy.cn/v2', 'https://notworkbuddy.cn/v2'])
def test_workbuddy_lookalike_hosts_stay_custom(url):
    assert provider_name({'api_base_url': url}) == 'custom'


WORKBUDDY = {'api_base_url': 'https://www.workbuddy.cn/v2'}


def test_workbuddy_replays_thinking_as_reasoning():
    original = {'role': 'assistant', 'content': 'hi', 'reasoning_content': '思考'}
    request = {'model': 'demo', 'messages': [dict(original)]}
    message = _prepare_passthrough_request(request, 'hy4', 'hy4free', WORKBUDDY)['messages'][0]
    assert message['reasoning'] == '思考'
    assert 'reasoning_content' not in message
    # 监控记录和重试线路共用原始请求，不能被改写
    assert request['messages'][0] == original


def test_other_upstreams_keep_reasoning_content():
    request = {'model': 'demo', 'messages': [{'role': 'assistant', 'content': 'hi', 'reasoning_content': '思考'}]}
    message = _prepare_passthrough_request(request, 'hy4', 'hy4', {'api_base_url': 'https://api.example/v1'})['messages'][0]
    assert message['reasoning_content'] == '思考'
    assert 'reasoning' not in message


def test_workbuddy_keeps_deepseek_placeholder_and_client_reasoning():
    request = {'model': 'demo', 'messages': [
        {'role': 'assistant', 'content': 'a'},
        {'role': 'assistant', 'content': 'b', 'reasoning': '自带', 'reasoning_content': '另一份'},
    ]}
    first, second = _prepare_passthrough_request(request, 'deepseek-v4', 'x', WORKBUDDY)['messages']
    assert first['reasoning_content'] == ' '
    assert 'reasoning' not in first
    assert second['reasoning'] == '自带'
    assert 'reasoning_content' not in second


def assistant_call():
    return {'model': 'demo', 'messages': [{'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'one', 'type': 'function', 'function': {'name': 'f', 'arguments': '{}'}, '_thought_signature': 'sig'}]}]}


def signature_of(prepared):
    return prepared['messages'][0]['tool_calls'][0].get('extra_content', {}).get('google', {}).get('thought_signature')


def test_gemini_signature_follows_family_not_model_name():
    assert signature_of(_prepare_passthrough_request(assistant_call(), 'demo', 'gemini-4-argon', {})) == 'sig'
    assert signature_of(_prepare_passthrough_request(assistant_call(), 'demo', 'plain-alias', {})) is None
    assert signature_of(_prepare_passthrough_request(assistant_call(), 'demo', 'plain-alias', {'model_family': 'gemini'})) == 'sig'
    assert signature_of(_prepare_passthrough_request(assistant_call(), 'demo', 'gemini-4', {'model_family': 'other'})) is None


def test_deepseek_placeholder_follows_family():
    request = {'model': 'demo', 'messages': [{'role': 'assistant', 'content': 'hi'}]}
    assert _prepare_passthrough_request(request, 'deepseek-v4', 'x', {})['messages'][0]['reasoning_content'] == ' '
    assert 'reasoning_content' not in _prepare_passthrough_request(request, 'deepseek-v4', 'x', {'model_family': 'other'})['messages'][0]
    assert _prepare_passthrough_request(request, 'alias', 'x', {'model_family': 'deepseek'})['messages'][0]['reasoning_content'] == ' '


def test_openrouter_reasoning_uses_provider_and_qwen_family():
    base = {'api_base_url': 'https://openrouter.ai/api/v1', 'enable_thinking': True}
    prepared = _prepare_passthrough_request({'model': 'demo', 'messages': []}, 'demo', 'plain', {**base, 'model_family': 'qwen'})
    assert prepared['reasoning']['max_tokens'] == 20000 and prepared['preserve_thinking'] is True
    plain = _prepare_passthrough_request({'model': 'demo', 'messages': []}, 'demo', 'plain', base)
    assert 'preserve_thinking' not in plain
    # 路径里带 openrouter.ai 的中转不走 OpenRouter 专属格式
    relay = _prepare_passthrough_request({'model': 'demo', 'messages': []}, 'demo', 'plain',
                                         {'api_base_url': 'https://relay.example/openrouter.ai/v1', 'enable_thinking': True})
    assert 'reasoning' not in relay and relay['enable_thinking'] is True


def test_first_chunk_timeout_precedence():
    assert resolve_first_chunk_timeout() == TimeoutDefaults.FIRST_CHUNK_TIMEOUT == 600
    assert resolve_first_chunk_timeout({'first_chunk_timeout_seconds': 90}) == 90
    assert resolve_first_chunk_timeout({'first_chunk_timeout_seconds': 90}, {'first_chunk_timeout_seconds': 15}) == 15
    assert resolve_first_chunk_timeout({'first_chunk_timeout_seconds': 90}, {}) == 90


def test_heartbeat_honors_configuration():
    assert resolve_heartbeat_interval() == TimeoutDefaults.STREAM_HEARTBEAT_INTERVAL
    assert resolve_heartbeat_interval({'client_disconnect_probe_interval': 120}) == 120
    assert resolve_heartbeat_interval({'client_disconnect_probe_interval': 0.1}) == 1


def test_native_exchange_uses_shared_first_chunk_default():
    from services import native_exchange

    async def run():
        waited = {}

        async def fake_wait_for(awaitable, timeout):
            waited['timeout'] = timeout
            awaitable.close()
            raise asyncio.TimeoutError

        service = AsyncMock()
        service.calculate_cost = lambda *args, **kwargs: {}

        async def upstream(**kwargs):
            yield b''

        service.call_api_passthrough = upstream
        monitor = AsyncMock()
        monitor.request_start = lambda **kwargs: None
        monitor.request_end = lambda **kwargs: None
        config = {'provider': 'gemini', 'api_type': 'gemini_native', 'api_key': 'test'}
        with patch.object(native_exchange.asyncio, 'wait_for', fake_wait_for), patch.dict(native_exchange.CONFIG, clear=True):
            with pytest.raises(Exception):
                await native_exchange.forward_native_exchange({'contents': []}, config, 'alias', service, monitor, stream=True)
        return waited['timeout']

    assert asyncio.run(run()) == 600


def test_rough_tokens_distinguish_cjk_and_skip_images():
    assert rough_token_count(None) == 0
    assert rough_token_count('a' * 40) == 10
    assert rough_token_count('中' * 15) == 10
    image = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + 'A' * 100000}}
    assert rough_message_tokens([{'role': 'user', 'content': [{'type': 'text', 'text': 'a' * 40}, image]},
                                 {'role': 'user', 'content': '中' * 15}, 'not-a-message']) == 20


def test_token_counter_final_fallback_counts_cjk():
    from modules.token_counter import _counting

    with patch.object(_counting, 'get_tiktoken_encoding', return_value=None), \
            patch.object(_counting, 'get_model_multiplier', return_value=1.5), \
            patch.object(_counting, 'get_tokenizer_for_model', return_value='tiktoken'):
        assert _counting.count_text_tokens('中' * 300, 'plain-model') == 300


def request_with(user_agent, key=None):
    headers = [(b'user-agent', user_agent.encode())]
    if key:
        headers.append((b'authorization', f'Bearer {key}'.encode()))
    return Request({'type': 'http', 'method': 'GET', 'path': '/v1/models', 'headers': headers,
                    'query_string': b'', 'client': ('203.0.113.1', 1)})


def models_response(user_agent, key=None, quiet=None):
    from routes import models_api

    config = {'api_key': 'gateway-key', **({'quiet_user_agents': quiet} if quiet is not None else {})}
    with patch.dict(models_api.CONFIG, config, clear=True), \
            patch.object(models_api.api_key_manager, 'has_keys', return_value=False), \
            patch.object(models_api, 'get_models', AsyncMock(return_value={'object': 'list', 'data': [{'id': 'real'}]})):
        return asyncio.run(models_api.get_models_endpoint(request_with(user_agent, key)))


def test_go_client_with_valid_key_gets_models():
    assert models_response('Go-http-client/2.0', 'gateway-key')['data'] == [{'id': 'real'}]


def test_scanner_without_key_gets_quiet_empty_list():
    assert models_response('Go-http-client/2.0') == {'object': 'list', 'data': []}


def test_quiet_agents_are_configurable():
    assert models_response('Go-http-client/2.0', quiet=[]).status_code == 401
    assert models_response('MyScanner/1', quiet=['myscanner'])['data'] == []


def test_invalid_key_error_does_not_point_at_openai():
    response = models_response('curl/8', 'wrong')
    assert response.status_code == 401
    body = json.loads(response.body)
    assert 'openai.com' not in body['error']['message']
    assert body['error']['code'] == 'invalid_api_key'


def test_gemini_models_endpoint_quiet_only_without_key():
    from routes import models_api

    def call(agent, key=None):
        with patch.dict(models_api.CONFIG, {'api_key': 'gateway-key'}, clear=True), \
                patch.object(models_api.api_key_manager, 'has_keys', return_value=False), \
                patch.object(models_api, 'get_gemini_models', AsyncMock(return_value={'models': ['real']})):
            return asyncio.run(models_api.get_gemini_models_endpoint(request_with(agent, key)))

    assert call('Go-http-client/2.0') == {'models': []}
    assert call('Go-http-client/2.0', 'gateway-key') == {'models': ['real']}
    assert call('curl/8').status_code == 401


def test_lmarena_payload_has_no_fake_default_model_and_logs_no_key(caplog):
    from services.message_converter import _convert_openai_to_lmarena_payload_sync

    secret = 'sk-must-not-appear'
    with patch('core.config_loader.MODEL_ENDPOINT_MAP', {'demo': {'api_key': secret, 'type': 'text'}}), \
            patch('core.config_loader.MODEL_NAME_TO_ID_MAP', {'claude-3-5-sonnet-20241022': {'id': 'wrong', 'type': 'image'}}), \
            caplog.at_level(logging.INFO):
        _convert_openai_to_lmarena_payload_sync(
            {'model': 'demo', 'messages': [{'role': 'user', 'content': 'hi'}]}, 'session')
        payload = _convert_openai_to_lmarena_payload_sync({'messages': [{'role': 'user', 'content': 'hi'}]}, 'session')
    assert secret not in caplog.text
    assert 'wrong' not in json.dumps(payload, ensure_ascii=False, default=str)


@pytest.mark.parametrize('body,supported', [
    ({'api_base_url': 'https://api.deepseek.com/v1'}, True),
    ({'api_base_url': 'https://relay.example/v1', 'provider': 'deepseek'}, True),
    ({'api_base_url': 'https://not-deepseek.example/deepseek/v1'}, False),
])
def test_balance_query_only_sends_keys_to_deepseek(body, supported):
    from routes import admin_routes

    async def run():
        with patch.object(admin_routes, '_read_admin_json', AsyncMock(return_value={'api_keys': ['sk-secret'], **body})), \
                patch.object(admin_routes._app_state.server, 'aiohttp_session', None):
            return await admin_routes.query_key_balance(None)

    result = asyncio.run(run())
    # 通过判定后因连接池未初始化返回 error，不会真的发请求
    assert result['status'] == ('error' if supported else 'unsupported')


def test_startup_script_reads_active_port_only():
    module = runpy.run_path(str(ROOT / 'scripts' / 'read_config.py'))
    read_value = module['read_value']
    assert module['DEFAULTS']['server_port'] == ServerDefaults.DEFAULT_PORT
    assert read_value('server_port', '{\n  // "server_port": 1111,\n  "server_port": 2222,\n}') == '2222'
    assert read_value('server_port', '{}') == str(ServerDefaults.DEFAULT_PORT)
    assert read_value('name', '  "name": "value",') == 'value'


def test_startup_scripts_have_no_literal_port():
    scripts = [*ROOT.glob('*.cmd'), *ROOT.glob('*.CMD')]
    assert scripts
    for script in scripts:
        text = script.read_text(encoding='utf-8')
        assert '5102' not in text, script.name
        assert text.startswith('@echo off'), script.name


def test_shared_defaults_are_not_duplicated_in_source():
    patterns = {str(RetryDefaults.API_KEY_COOLDOWN_SECONDS): re.compile(r'\b172800\b'),
                GeminiDefaults.BASE_URL: re.compile(r'''["']https://generativelanguage\.googleapis\.com''')}
    assert GeminiDefaults.BASE_URL.startswith(f'https://{GeminiDefaults.HOST}/')
    for folder in ('routes', 'services', 'converters'):
        for path in (ROOT / folder).rglob('*.py'):
            text = path.read_text(encoding='utf-8')
            for name, pattern in patterns.items():
                assert not pattern.search(text), f'{path.name} 重复定义 {name}'
            assert not re.search(r'len\([^)]*\)\s*//\s*4', text), f'{path.name} 仍在自行粗估 token'
