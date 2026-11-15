"""访客 Key 用量额度、过期时间与周期重置。"""
import asyncio
import json
from datetime import datetime
from threading import Lock, RLock
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from core import api_key_manager as module
from core import usage_quota
from core.request_context import RequestContext, current_request


def quota(**fields):
    return usage_quota.normalize_quota(fields)


@pytest.fixture
def manager(monkeypatch, tmp_path):
    manager = object.__new__(module.APIKeyManager)
    manager._lock, manager._save_lock = Lock(), RLock()
    manager._keys, manager._secret_index, manager._dirty = {}, {}, False
    manager._rate_limiter = module.RateLimiter()
    monkeypatch.setattr(module, 'API_KEYS_FILE', str(tmp_path / 'keys.json'))
    return manager


def clock(monkeypatch, *args):
    monkeypatch.setattr(module.time, 'time', lambda: datetime(*args).timestamp())


def test_cost_quota_pauses_key_until_usage_is_reset(manager):
    key = manager.create_key('guest', quota=quota(cost_limit=1))
    assert manager.validate_request(key['secret'], 'demo').valid
    manager.record_usage(key['id'], 1200, {'total_cost': 0.6, 'currency': 'USD'})
    manager.record_usage(key['id'], 800, {'total_cost': 0.4, 'currency': 'USD'})

    check = manager.validate_request(key['secret'], 'demo')
    assert (check.valid, check.reason) == (False, 'quota')
    assert '累计额度已用完' in check.error and '费用 $1 / $1' in check.error and '联系管理员' in check.error
    info = manager.get_key_info(key['id'])
    assert info['usage']['exhausted'] == ['cost']
    assert info['usage']['used'] == {'cost': 1.0, 'tokens': 2000, 'requests': 1}

    assert manager.reset_usage(key['id'])['usage']['used'] == {'cost': 0.0, 'tokens': 0, 'requests': 0}
    assert manager.validate_request(key['secret'], 'demo').valid
    saved = json.loads(open(module.API_KEYS_FILE, encoding='utf-8').read())[key['id']]
    assert saved['usage']['cost'] == {}


def test_handshake_skips_quota_and_admin_usage_is_ignored(manager):
    key = manager.create_key('guest', quota=quota(request_limit=1))
    assert manager.validate_request(key['secret'], 'demo').valid
    assert manager.validate_request(key['secret'], 'demo', consume=False).valid
    assert manager.validate_request(key['secret'], 'demo').reason == 'quota'
    manager.record_usage('admin', 10, {'total_cost': 5})
    assert manager.get_key_info(key['id'])['usage']['used']['tokens'] == 0


def test_daily_quota_recovers_in_next_period(manager, monkeypatch):
    clock(monkeypatch, 2026, 11, 15, 23, 50)
    key = manager.create_key('guest', quota=quota(period='day', token_limit=1000))
    manager.record_usage(key['id'], 1500, None)
    check = manager.validate_request(key['secret'])
    assert check.reason == 'quota'
    assert '今日额度已用完' in check.error and 'Token 1,500 / 1,000' in check.error
    assert '将于 2026-11-16 00:00 恢复' in check.error

    clock(monkeypatch, 2026, 11, 16, 0, 1)
    assert manager.validate_request(key['secret']).valid
    usage = manager.get_key_info(key['id'])['usage']
    assert usage['used']['tokens'] == 0 and usage['used']['requests'] == 1
    assert usage['period_start'] == datetime(2026, 11, 16).timestamp()


@pytest.mark.parametrize('period,now,start,end', [
    ('week', (2026, 11, 15, 12), (2026, 11, 9), (2026, 11, 16)),
    ('month', (2026, 12, 31, 23), (2026, 12, 1), (2027, 1, 1)),
])
def test_period_bounds_follow_local_calendar(period, now, start, end):
    assert usage_quota.period_bounds(period, datetime(*now).timestamp()) == (
        datetime(*start).timestamp(), datetime(*end).timestamp())


def test_cost_from_both_currencies_counts_toward_limit(manager, monkeypatch):
    from core import db_stats
    monkeypatch.setattr(db_stats, 'get_exchange_rates', lambda: (7.0, 1 / 7.0))
    key = manager.create_key('guest', quota=quota(cost_limit=10, cost_currency='CNY'))
    manager.record_usage(key['id'], 1, {'total_cost': 1, 'currency': 'USD'})
    assert manager.validate_request(key['secret']).valid
    manager.record_usage(key['id'], 1, {'total_cost': 3, 'currency': 'CNY'})
    check = manager.validate_request(key['secret'])
    assert check.reason == 'quota' and '费用 ¥10 / ¥10' in check.error


def test_expired_key_is_rejected_everywhere(manager, monkeypatch):
    clock(monkeypatch, 2026, 11, 15, 12)
    key = manager.create_key('guest', expires_at=datetime(2026, 11, 20).timestamp())
    assert manager.validate_request(key['secret']).valid
    clock(monkeypatch, 2026, 11, 20, 0, 0)
    check = manager.validate_request(key['secret'], consume=False)
    assert (check.valid, check.reason) == (False, 'expired') and '2026-11-20 00:00' in check.error
    assert manager.get_allowed_models(key['secret']) is None
    assert manager.get_key_info(key['id'])['expired'] is True


def test_failed_save_removes_fields_added_to_legacy_key(manager, monkeypatch):
    manager._keys['old'] = {'name': 'legacy', 'secret': 'sk-old', 'rpm_limit': 0}
    manager._secret_index['sk-old'] = 'old'
    monkeypatch.setattr(module.os, 'replace', MagicMock(side_effect=OSError('disk unavailable')))
    with pytest.raises(module.KeyPersistenceError):
        manager.update_key('old', {'quota': quota(period='day', cost_limit=5), 'expires_at': 1900000000})
    assert 'quota' not in manager._keys['old'] and 'expires_at' not in manager._keys['old']

    manager.record_usage('old', 50, None)
    with pytest.raises(module.KeyPersistenceError):
        manager.reset_usage('old')
    assert manager._keys['old']['usage']['tokens'] == 50


def guest_request(secret):
    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}
    return Request({'type': 'http', 'method': 'POST', 'path': '/', 'query_string': b'', 'client': ('192.0.2.2', 1),
                    'headers': [(b'authorization', f'Bearer {secret}'.encode())]}, receive)


def test_exhausted_quota_is_payment_required(manager, monkeypatch):
    from routes import api_routes
    monkeypatch.setattr(api_routes, 'CONFIG', {'api_key': 'admin'})
    monkeypatch.setattr(api_routes, 'api_key_manager', manager)
    key = manager.create_key('guest', quota=quota(request_limit=1), allowed_models=['demo'])
    api_routes._validate_request_api_key(guest_request(key['secret']), 'demo')
    for model, status in (('demo', 402), ('other', 403)):
        with pytest.raises(HTTPException) as error:
            api_routes._validate_request_api_key(guest_request(key['secret']), model)
        assert error.value.status_code == status


def test_request_end_charges_the_calling_key(monkeypatch):
    from modules import monitoring
    record = MagicMock()
    monkeypatch.setattr(monitoring.api_key_manager, 'record_usage', record)
    monkeypatch.setattr(monitoring.monitoring_service.log_manager, 'write_request_log', MagicMock())
    token = current_request.set(RequestContext(owner_id='guest-key', owner_name='Guest', model='demo'))
    try:
        monitoring.monitoring_service.request_start('quota-charge', 'demo')
        monitoring.monitoring_service.request_end('quota-charge', True, input_tokens=30, output_tokens=12,
                                                  cost_info={'total_cost': 0.5, 'currency': 'CNY'})
        monitoring.monitoring_service._event_pool.submit(lambda: None).result(timeout=5)
    finally:
        current_request.reset(token)
    record.assert_called_once_with('guest-key', 42, {'total_cost': 0.5, 'currency': 'CNY'}, billable=True)


def test_unbillable_request_is_not_counted(manager):
    key = manager.create_key('guest')
    manager.record_usage(key['id'], 12, {'total_cost': 0.001, 'currency': 'USD'}, billable=False)
    manager.record_usage(key['id'], 300, {'total_cost': 0.02, 'currency': 'USD'})
    assert manager.get_key_info(key['id'])['usage']['used'] == {'cost': 0.02, 'tokens': 300, 'requests': 0}


@pytest.mark.parametrize('payload', [{'quota': {'period': 'year'}}, {'quota': {'cost_currency': 'EUR', 'cost_limit': 1}},
                                     {'quota': {'cost_limit': -1}}, {'quota': {'token_limit': 'many'}},
                                     {'quota': {'cost_limit': True}}, {'quota': {'bogus': 1}}, {'quota': 5},
                                     {'expires_at': 'tomorrow'}, {'expires_at': True}])
def test_invalid_quota_payload_is_rejected(payload):
    from routes.apikey_routes import _validate_key_payload
    with pytest.raises(HTTPException) as error:
        _validate_key_payload({'name': 'guest', **payload})
    assert error.value.status_code == 400


def test_create_route_passes_quota_settings(manager, monkeypatch):
    from routes import apikey_routes
    monkeypatch.setattr(apikey_routes, 'api_key_manager', manager)

    async def receive():
        body = {'name': 'guest', 'expires_at': 1900000000,
                'quota': {'period': 'month', 'cost_limit': 2.5, 'cost_currency': 'CNY', 'token_limit': 100000}}
        return {'type': 'http.request', 'body': json.dumps(body).encode(), 'more_body': False}
    request = Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': [], 'query_string': b''}, receive)
    created = asyncio.run(apikey_routes.create_api_key(request))['key']
    info = manager.get_key_info(created['id'])
    assert info['quota'] == {'period': 'month', 'cost_currency': 'CNY', 'cost_limit': 2.5, 'token_limit': 100000,
                             'request_limit': 0}
    assert info['expires_at'] == 1900000000 and info['usage']['limits'] == {'cost': 2.5, 'tokens': 100000, 'requests': 0}


@pytest.mark.parametrize('fields,expected', [
    ({}, None), ({'period': 'total', 'cost_limit': 0}, None),
    ({'period': 'month'}, {'period': 'month', 'cost_currency': 'USD', 'cost_limit': 0.0, 'token_limit': 0, 'request_limit': 0}),
    ({'token_limit': 1.9}, {'period': 'total', 'cost_currency': 'USD', 'cost_limit': 0.0, 'token_limit': 1, 'request_limit': 0}),
])
def test_quota_without_limits_is_dropped_unless_periodic(fields, expected):
    assert usage_quota.normalize_quota(fields) == expected
