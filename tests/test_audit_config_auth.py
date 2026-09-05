"""Configuration and authentication regressions without live writes/upstream calls."""
import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from core import config_loader
from routes import admin_routes, api_routes, models_api


def request_for(body=None, headers=None, host='127.0.0.1', query=b''):
    async def receive():
        return {'type': 'http.request', 'body': json.dumps(body).encode(), 'more_body': False}
    return Request({'type': 'http', 'method': 'POST', 'path': '/',
                    'headers': [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
                    'client': (host, 1234), 'query_string': query}, receive)


@pytest.mark.parametrize('headers', [{}, {'Authorization': 'Bearer arbitrary'}, {'x-api-key': 'arbitrary'}])
def test_unconfigured_api_cannot_be_opened_with_arbitrary_key(monkeypatch, headers):
    monkeypatch.setattr(api_routes, 'CONFIG', {})
    monkeypatch.setattr(api_routes.api_key_manager, 'has_keys', lambda: False)
    with pytest.raises(HTTPException) as error:
        api_routes._validate_request_api_key(request_for(headers=headers, host='192.0.2.2'), 'demo')
    assert error.value.status_code == 401
    api_routes._validate_request_api_key(request_for(headers=headers), 'demo')


@pytest.mark.parametrize('body', [None, [], 'text', 10])
def test_non_object_request_is_client_error(body):
    with pytest.raises(HTTPException) as error:
        asyncio.run(api_routes._read_request_json_non_blocking(request_for(body)))
    assert error.value.status_code == 400


@pytest.mark.parametrize('headers,query', [({'x-goog-api-key': 'guest'}, b''), ({}, b'key=guest'),
                                         ({'Authorization': 'bearer guest'}, b'')])
def test_gemini_listing_honors_sdk_auth_and_guest_permissions(monkeypatch, headers, query):
    monkeypatch.setattr(models_api, 'CONFIG', {'api_key': 'admin'})
    monkeypatch.setattr(models_api, 'MODEL_ENDPOINT_MAP', {
        'allowed': {'api_type': 'gemini_native'}, 'private': {'api_type': 'gemini_native'}})
    monkeypatch.setattr(models_api.api_key_manager, 'has_keys', lambda: True)
    monkeypatch.setattr(models_api.api_key_manager, 'get_allowed_models', lambda key: ['allowed'] if key == 'guest' else None)
    response = asyncio.run(models_api.get_gemini_models_endpoint(request_for(headers=headers, query=query)))
    assert [item['name'] for item in response['models']] == ['models/allowed']


@pytest.mark.parametrize('value', [[], ['bad'], None, 12])
def test_invalid_config_root_preserves_previous_config(value):
    target = {'api_key': 'keep'}
    with pytest.raises(ValueError):
        config_loader._replace_dict_no_gap(target, value)
    assert target == {'api_key': 'keep'}


def test_missing_model_map_preserves_running_models(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_loader, 'MODEL_ENDPOINT_MAP', {'demo': {'api_type': 'direct_api'}})
    config_loader.load_model_endpoint_map()
    assert 'demo' in config_loader.MODEL_ENDPOINT_MAP


@pytest.mark.parametrize('operation,payload', [
    ('delete_model_config', {'model_name': 'a'}), ('reorder_models', {'order': ['b', 'a']})])
def test_model_writes_share_transaction_lock(monkeypatch, operation, payload):
    async def run():
        lock = asyncio.Lock()
        monkeypatch.setattr(admin_routes, '_MODEL_ENDPOINT_MAP_LOCK', lock)
        read = AsyncMock(return_value={'a': {}, 'b': {}})
        write = AsyncMock()
        monkeypatch.setattr(admin_routes, 'read_json_file', read)
        monkeypatch.setattr(admin_routes, 'write_json_file', write)
        monkeypatch.setattr(admin_routes, '_invalidate_admin_stats_cache', AsyncMock())
        await lock.acquire()
        task = asyncio.create_task(getattr(admin_routes, operation)(request_for(payload), lambda: None))
        await asyncio.sleep(0)
        try:
            assert not read.called, 'read-modify-write must wait for other config mutations'
        finally:
            lock.release()
            await task
        assert write.await_count == 1
        if operation == 'delete_model_config':
            assert write.call_args.args[1] == {'b': {}}
        else:
            assert list(write.call_args.args[1]) == ['b', 'a']
    asyncio.run(run())


@pytest.mark.parametrize('content', ['[]', 'null', '10'])
def test_admin_rejects_non_object_config_before_writing(monkeypatch, content):
    write = AsyncMock()
    monkeypatch.setattr(admin_routes, 'write_text_file', write)
    with pytest.raises(HTTPException) as error:
        asyncio.run(admin_routes.update_config(request_for({'content': content}), json.loads, Mock()))
    assert error.value.status_code == 400
    write.assert_not_called()


def test_delayed_key_snapshot_cannot_restore_revoked_key(monkeypatch):
    from threading import Lock
    from unittest.mock import mock_open
    from core import api_key_manager as module
    manager = object.__new__(module.APIKeyManager)
    manager._lock = Lock()
    manager._save_lock = Lock()
    manager._keys = {'keep': {'secret': 'current'}}
    manager._dirty = True
    handle = mock_open()
    monkeypatch.setattr('builtins.open', handle)
    monkeypatch.setattr(module.os, 'replace', Mock())
    manager._write_to_disk('{"revoked": {"secret": "old"}}', 1)
    assert json.loads(handle().write.call_args.args[0]) == manager._keys


@pytest.mark.parametrize('payload', [[], {'name': 1}, {'name': 'demo', 'enabled': 'false'},
                                    {'name': 'demo', 'allowed_models': [{}]}])
def test_invalid_key_configuration_is_rejected(payload):
    from routes.apikey_routes import _validate_key_payload
    with pytest.raises(HTTPException) as error:
        _validate_key_payload(payload)
    assert error.value.status_code == 400


def test_invalid_key_file_does_not_disable_existing_auth(monkeypatch):
    from threading import Lock, RLock
    from unittest.mock import mock_open
    from core import api_key_manager as module
    manager = object.__new__(module.APIKeyManager)
    manager._lock = Lock()
    manager._save_lock = RLock()
    manager._keys = {'keep': {'secret': 'current'}}
    manager._secret_index = {'current': 'keep'}
    monkeypatch.setattr(module.os.path, 'exists', lambda _: True)
    monkeypatch.setattr('builtins.open', mock_open(read_data='[]'))
    manager._load()
    assert manager._keys == {'keep': {'secret': 'current'}}
    assert manager._secret_index == {'current': 'keep'}


@pytest.mark.parametrize('body', [{'model': []}, {'messages': 'bad'}, {'messages': [1]}, {'stream': 'false'}])
def test_invalid_inference_field_types_are_client_errors(body):
    with pytest.raises(HTTPException) as error:
        asyncio.run(api_routes._read_request_json_non_blocking(request_for(body)))
    assert error.value.status_code == 400


def test_unknown_model_is_not_queued_for_retired_browser_bridge(monkeypatch):
    monkeypatch.setattr(api_routes, 'MODEL_ENDPOINT_MAP', {})
    monkeypatch.setattr(api_routes, 'MODEL_NAME_TO_ID_MAP', {})
    with pytest.raises(HTTPException) as error:
        asyncio.run(api_routes._dispatch_chat_completions_core({'model': 'missing', 'messages': []}))
    assert error.value.status_code == 404


@pytest.mark.parametrize('operation,payload', [
    ('update_config', {'config': {'server_port': 5200}}),
    ('update_all_tokenizer_mappings', {'tokenizer_config': {'demo': 'tiktoken'}}),
    ('update_archive_config', {'enabled': False, 'days': 30})])
def test_jsonc_mutations_share_transaction_lock(monkeypatch, operation, payload):
    async def run():
        lock = asyncio.Lock()
        monkeypatch.setattr(admin_routes, '_CONFIG_FILE_LOCK', lock)
        reader = AsyncMock(return_value='{}')
        monkeypatch.setattr(admin_routes, 'read_text_file', reader)
        monkeypatch.setattr(admin_routes, 'write_text_file', AsyncMock())
        monkeypatch.setattr(admin_routes, 'load_config', Mock())
        monkeypatch.setattr(admin_routes, 'CONFIG', {})
        args = [request_for(payload)]
        if operation != 'update_archive_config':
            args.extend([json.loads, Mock()])
        await lock.acquire()
        task = asyncio.create_task(getattr(admin_routes, operation)(*args))
        await asyncio.sleep(0)
        try:
            assert not reader.called
        finally:
            lock.release()
            await task
    asyncio.run(run())
