import asyncio
import copy
import gzip
import json
from threading import Lock, RLock
from unittest.mock import AsyncMock, MagicMock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.request_context import RequestContext, current_request


def test_metadata_sqlite_all_read_paths_and_history(tmp_path):
    from modules.monitoring_sqlite import SQLiteLogger
    from core.usage_analysis import usage_by_caller, estimate_current_prices
    logger = SQLiteLogger(tmp_path / 'requests.db')
    record = {'type': 'request_end', 'request_id': 'request', 'timestamp': 1000, 'model': 'demo', 'success': True,
              'input_tokens': 10, 'output_tokens': 20, 'cached_tokens': 2,
              'cost_info': {'total_cost': 3.5, 'currency': 'CNY'},
              'caller_id': 'stable-key', 'caller_name': '用户', 'conversation_id': 'conversation',
              'gateway_request_id': 'wire-id', 'timings': {'total_ms': 123}, 'pricing_snapshot': {'input': 7}}
    logger.write_request(record)
    from core.request_metadata import COLUMNS
    for fetched in [logger.get_request_details('request'), logger.get_recent_requests(1)[0], logger.query_requests(search='stable-key')['items'][0]]:
        assert {key: fetched[key] for key in COLUMNS} == {key: record[key] for key in COLUMNS}
    assert usage_by_caller(logger.db_path)['items'][0]['caller_id'] == 'stable-key'
    result = estimate_current_prices(logger.db_path, {'demo': {'pricing': {'input': 1, 'output': 2, 'unit': 1}}})
    assert result['items'][0]['historical_cost'] == 3.5
    assert result['items'][0]['current_estimate'] == 50
    assert logger.get_request_details('request')['total_cost'] == 3.5
    logger.close()


def test_key_save_failure_rolls_back_creation_but_preserves_revocation(tmp_path, monkeypatch):
    from core import api_key_manager as module
    manager = object.__new__(module.APIKeyManager)
    manager._lock, manager._save_lock = Lock(), RLock()
    manager._keys, manager._secret_index, manager._dirty = {}, {}, False
    manager._rate_limiter = module.RateLimiter()
    monkeypatch.setattr(module, 'API_KEYS_FILE', str(tmp_path / 'keys.json'))
    key = manager.create_key('original')
    monkeypatch.setattr(module.os, 'replace', MagicMock(side_effect=OSError('disk unavailable')))
    with pytest.raises(module.KeyPersistenceError): manager.create_key('new')
    assert len(manager._keys) == 1
    with pytest.raises(module.KeyPersistenceError): manager.update_key(key['id'], {'name': 'changed', 'enabled': False})
    assert manager.get_key_info(key['id'])['name'] == 'original'
    assert not manager.get_key_info(key['id'])['enabled']
    with pytest.raises(module.KeyPersistenceError): manager.reload()
    assert not manager.validate_request(key['secret'])[0]
    with pytest.raises(module.KeyPersistenceError): manager.delete_key(key['id'])
    assert not manager.validate_request(key['secret'])[0]


def test_key_reload_read_failure_is_reported(tmp_path, monkeypatch):
    from core import api_key_manager as module
    manager = object.__new__(module.APIKeyManager)
    manager._lock, manager._save_lock = Lock(), RLock()
    manager._keys, manager._secret_index, manager._dirty = {'key': {'secret': 'active'}}, {'active': 'key'}, False
    path = tmp_path / 'keys.json'; path.write_text('invalid')
    monkeypatch.setattr(module, 'API_KEYS_FILE', str(path))
    with pytest.raises(module.KeyPersistenceError): manager.reload()
    assert manager._secret_index == {'active': 'key'}


def test_responses_native_tools_reasoning_and_citations_round_trip():
    from converters.responses_bridge import convert_chat_request_to_responses, convert_responses_response_to_chat, _ChatStreamBuilder
    output = [{'type': 'reasoning', 'encrypted_content': 'signed'}, {'type': 'web_search_call', 'id': 'search'},
              {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Answer', 'annotations': [{'type': 'url_citation', 'url': 'https://example.test'}]}]}]
    converted = convert_responses_response_to_chat({'output': output, 'id': 'resp_id'}, 'demo')
    request = {'model': 'demo', 'messages': [converted['choices'][0]['message']], 'tools': [{'type': 'web_search', 'filters': {'allowed_domains': ['example.test']}}]}
    original = copy.deepcopy(request)
    native = convert_chat_request_to_responses(request, endpoint_config={'provider': 'deepseek'})
    assert native['input'] == output and native['tools'] == request['tools']
    assert 'store' not in native and request == original
    builder = _ChatStreamBuilder('demo')
    assert b'reasoning_content' in builder.process({'type': 'response.reasoning_text.delta', 'delta': 'Thought'})[0]
    assert b'provider_event' in builder.process({'type': 'response.web_search_call.searching'})[0]


@pytest.mark.parametrize('stream', [False, True])
def test_gemini_entry_uses_native_tools_shared_executor_and_auth(tmp_path, monkeypatch, stream):
    from routes import api_routes
    from core.request_middleware import GatewayRequestMiddleware
    from core.conversation_store import ConversationStore
    monkeypatch.setattr('core.request_middleware.conversation_store', ConversationStore(tmp_path / 'cache.db'))
    monkeypatch.setattr(api_routes, 'CONFIG', {'api_key': 'client-secret'})
    monkeypatch.setattr(api_routes.api_key_manager, 'has_keys', lambda: False)
    monkeypatch.setattr(api_routes, '_check_verification_cooldown', lambda: None)
    monkeypatch.setattr(api_routes, 'MODEL_ENDPOINT_MAP', {'gemini': {'api_type': 'gemini_native', 'api_key': 'upstream-secret', 'native_tools': ['google_search']}})
    service = MagicMock()
    service.calculate_cost.return_value = {}
    captured = {}
    async def forward(**kwargs):
        captured.update(kwargs)
        value = {'candidates': [{'content': {'parts': [{'executableCode': {'language': 'PYTHON', 'code': 'print(1)'}}]}, 'finishReason': 'STOP', 'groundingMetadata': {'webSearchQueries': ['test']}}]}
        yield (('data: ' + json.dumps(value) + '\n\n') if stream else json.dumps(value)).encode()
    service.call_api_passthrough = forward
    monkeypatch.setattr(api_routes._app_state.server, 'direct_api_service', service)
    monitor = MagicMock()
    monitor.broadcast_to_monitors = AsyncMock()
    monkeypatch.setattr(api_routes, 'monitoring_service', monitor)
    app = FastAPI(); app.add_middleware(GatewayRequestMiddleware); app.include_router(api_routes.router)
    client = TestClient(app)
    endpoint = '/v1beta/models/gemini:' + ('streamGenerateContent' if stream else 'generateContent')
    assert client.post(endpoint, json={'contents': []}).status_code == 401
    result = client.post(endpoint, json={'contents': []}, headers={'x-goog-api-key': 'client-secret'})
    assert result.status_code == 200 and 'executableCode' in result.text
    assert captured['request_body']['tools'] == [{'googleSearch': {}}]
    assert captured['headers']['x-goog-api-key'] == 'upstream-secret'
    assert 'stream' not in captured['request_body']
    assert monitor.request_end.call_count == 1
    assert len(result.headers.get_list('x-bridge-request-id')) == 1


def test_gateway_tools_admin_auth_origin_and_no_secrets(monkeypatch):
    from routes.gateway_workspace import router as workspace_router
    from routes.gateway_mcp import router as mcp_router
    from core.middleware import WebAccessKeyMiddleware
    from core import config_loader
    monkeypatch.setattr(config_loader, 'CONFIG_LOADED', True)
    monkeypatch.setitem(config_loader.CONFIG, 'web_access_key', 'admin-secret')
    monkeypatch.setattr('routes.gateway_workspace.MODEL_ENDPOINT_MAP', {'demo': {'api_type': 'responses_native', 'provider': 'deepseek', 'api_key': 'never-return-this'}})
    app = FastAPI(); app.add_middleware(WebAccessKeyMiddleware); app.include_router(workspace_router); app.include_router(mcp_router)
    client = TestClient(app)
    assert client.get('/api/admin/tools').status_code == 401
    headers = {'x-web-access-key': 'admin-secret', 'Accept': 'application/json, text/event-stream'}
    init = client.post('/api/admin/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}})
    assert init.json()['result']['protocolVersion'] == '2025-06-18'
    call = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'list_gateway_models'}}
    result = client.post('/api/admin/mcp', headers=headers, json=call)
    assert result.status_code == 200 and 'never-return-this' not in result.text and 'web_search' in result.text
    assert client.post('/api/admin/mcp', headers={**headers, 'Origin': 'https://evil.test'}, json=call).status_code == 403
    assert client.post('/api/admin/playground/run', json={'model': 'demo', 'request': {'input': 'hello'}}).status_code == 401


def test_scoped_signature_and_permanent_archive(tmp_path):
    from converters.gemini_interactions import cache_thought_signatures, match_and_inject_thought_signatures
    from core.exchange_archive import save_exchange
    first, second = RequestContext(authenticated=True, owner_id='a'), RequestContext(authenticated=True, owner_id='b')
    token = current_request.set(first)
    try:
        cache_thought_signatures([('exact', 'signed')])
        assert match_and_inject_thought_signatures('exac') == []
        assert match_and_inject_thought_signatures('exact')[0]['signature'] == 'signed'
        current_request.set(second)
        assert match_and_inject_thought_signatures('exact') == []
    finally: current_request.reset(token)
    path = save_exchange(first, {'request': {'input': '完整内容'}, 'response': {'tools': [1]}}, tmp_path)
    assert json.loads(gzip.decompress(path.read_bytes()))['request']['input'] == '完整内容'


def test_policy_migration_is_backed_up_idempotent_and_cost_preserving(tmp_path):
    import sqlite3
    from scripts.migrate_gateway_policy import migrate
    (tmp_path / 'model_endpoint_map.json').write_text(json.dumps({'one': [{'api_key': 'keep', 'sanitize_recursive_schemas': True}]}))
    (tmp_path / 'config.jsonc').write_text('// keep comment\n{"deepseek_logprobs":{"enabled":false,"compress":false}}')
    (tmp_path / 'logs').mkdir()
    path = tmp_path / 'logs/requests.db'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE requests(request_id TEXT, timestamp REAL, input_cost REAL, output_cost REAL, cached_cost REAL, total_cost REAL, currency TEXT)')
        connection.execute("INSERT INTO requests VALUES('old',1,2,3,4,9,'CNY')")
    result = migrate(tmp_path, apply=True)
    assert result['historical_amounts_unchanged']
    assert result['fidelity_endpoints'] == 1
    from pathlib import Path
    backup = Path(result['backup_directory'])
    assert (backup / 'requests.db').exists()
    assert json.loads((backup / 'model_endpoint_map.json').read_text())['one'][0]['sanitize_recursive_schemas'] is True
    assert '// keep comment' in (tmp_path / 'config.jsonc').read_text()
    again = migrate(tmp_path, apply=True)
    assert again['fidelity_endpoints'] == 0 and not again['additive_columns'] and not again['config_changed']


def test_monitor_context_is_transferred_to_serial_worker(monkeypatch):
    from modules.monitoring import monitoring_service
    writes = MagicMock()
    monkeypatch.setattr(monitoring_service.log_manager, 'write_request_log', writes)
    context = RequestContext(owner_id='caller', owner_name='User', endpoint={'pricing': {'input': 7}})
    context.mark('upstream_start'); context.mark('first_byte'); context.mark('first_business')
    token = current_request.set(context)
    try:
        monitoring_service.request_start('metadata-worker', 'demo')
        monitoring_service._event_pool.submit(lambda: None).result(timeout=5)
        context.mark('finished')
        monitoring_service.request_end('metadata-worker', True, cost_info={'total_cost': 1})
        monitoring_service._event_pool.submit(lambda: None).result(timeout=5)
    finally: current_request.reset(token)
    saved = writes.call_args.args[0]
    assert saved['caller_id'] == 'caller' and saved['pricing_snapshot'] == {'input': 7}
    assert saved['timings']['first_business_ms'] is not None
