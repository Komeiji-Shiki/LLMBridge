"""转换预览走真实链路，但绝不发送，也不把密钥放进结果。"""
import asyncio
import json

import pytest

from services.conversion_preview import _CaptureSession, _NullMonitor, preview_conversion

CHAT = {'model': 'alias', 'temperature': 0.5, 'messages': [
    {'role': 'system', 'content': '开头'}, {'role': 'user', 'content': 'q'},
    {'role': 'system', 'content': '提醒'}, {'role': 'user', 'content': 'r'}]}
ANTHROPIC = {'model': 'alias', 'max_tokens': 10, 'system': '开头', 'messages': [{'role': 'user', 'content': 'q'}]}


def _config(api_type, **extra):
    return {'api_type': api_type, 'api_base_url': 'https://u.test/v1', 'api_key': 'SECRET-KEY', 'model_id': 'real', **extra}


def _preview(protocol, body, config):
    return asyncio.run(preview_conversion(protocol, body, 'alias', config))


@pytest.fixture
def no_network(monkeypatch):
    """真实会话一旦被调用就失败，确保预览走的是截获。"""
    import aiohttp

    def forbidden(*_args, **_kwargs):
        raise AssertionError('预览不能发起网络请求')
    monkeypatch.setattr(aiohttp.ClientSession, '_request', forbidden)


def test_chat_client_to_gemini_shows_real_body_and_losses(no_network):
    result = _preview('chat', CHAT, _config('gemini_native', model_id='gemini-3.8-flash'))
    assert result['sent'] is False and result['upstream_protocol'] == 'gemini'
    assert result['url'].endswith('/models/gemini-3.8-flash:generateContent')
    assert result['request']['systemInstruction']['parts'] == [{'text': '开头'}]
    assert '提醒' in json.dumps(result['request']['contents'], ensure_ascii=False)
    assert 'temperature' in [note['field'] for note in result['losses']]


def test_anthropic_client_to_chat_upstream(no_network):
    result = _preview('anthropic', ANTHROPIC, _config('direct_api', passthrough=True))
    assert result['upstream_protocol'] == 'chat'
    assert result['request']['model'] == 'real'
    assert result['request']['messages'][0] == {'role': 'system', 'content': '开头'}


def test_chat_client_to_anthropic_upstream(no_network):
    result = _preview('chat', CHAT, _config('anthropic_native', model_id='claude-opus-4-7'))
    assert result['upstream_protocol'] == 'anthropic' and 'temperature' not in result['request']
    assert result['request']['system'][0]['text'] == '开头'


def test_chat_client_to_interactions_upstream(no_network):
    result = _preview('chat', CHAT, _config('gemini_native', upstream_protocol='interactions', model_id='gemini-3.8-flash'))
    assert result['upstream_protocol'] == 'interactions' and result['url'].endswith('/interactions')
    assert result['request']['store'] is False and 'temperature' not in result['request'].get('generation_config', {})


def test_same_protocol_is_reported_as_passthrough(no_network):
    result = _preview('anthropic', {**ANTHROPIC, 'temperature': 0.5}, _config('anthropic_native', model_id='claude-opus-4-7'))
    assert result['mode'] == 'passthrough' and result['request']['model'] == 'claude-opus-4-7'
    assert 'temperature' not in result['request']


def test_handler_rejection_is_reported_instead_of_an_empty_preview(no_network):
    result = _preview('anthropic', ANTHROPIC, _config('direct_api'))
    assert result['request'] is None and 'passthrough' in result['rejected']


def test_secrets_never_reach_the_result(no_network):
    for protocol, body, config in (('chat', CHAT, _config('gemini_native')), ('anthropic', ANTHROPIC, _config('direct_api', passthrough=True))):
        assert 'SECRET-KEY' not in json.dumps(_preview(protocol, body, config), ensure_ascii=False)


def test_capture_session_drops_headers_and_query():
    session = _CaptureSession()
    session.post('https://u.test/a?key=SECRET', data=b'{}', headers={'Authorization': 'Bearer SECRET'})
    assert session.sent == [('https://u.test/a', b'{}')]


def test_null_monitor_accepts_any_call():
    monitor = _NullMonitor()
    assert monitor.request_start(request_id='x') is None
    assert asyncio.run(monitor.broadcast_to_monitors({})) is None
