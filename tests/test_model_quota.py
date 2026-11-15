"""模型与渠道额度：所有调用方合计，用完后不再请求上游。"""
import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from core import config_loader, model_quota as module, usage_quota
from core.request_context import RequestContext, current_request


def quota(**fields):
    return usage_quota.normalize_quota(fields)


@pytest.fixture
def manager(monkeypatch, tmp_path):
    monkeypatch.setattr(module, 'USAGE_FILE', str(tmp_path / 'model_usage.json'))
    monkeypatch.setattr(config_loader, 'CHANNEL_QUOTAS', {})
    return module.ModelQuotaManager()


def enforce(manager, model, endpoint, context=None):
    token = current_request.set(context) if context is not None else None
    try:
        manager.enforce(model, endpoint, context)
    finally:
        if token is not None:
            current_request.reset(token)


def status_of(call):
    with pytest.raises(HTTPException) as error:
        call()
    return error.value.status_code, error.value.detail


def test_model_quota_blocks_every_caller_once_used_up(manager, monkeypatch):
    monkeypatch.setattr(module.time, 'time', lambda: datetime(2026, 11, 15, 12).timestamp())
    endpoint = {'quota': quota(period='day', cost_limit=1)}
    enforce(manager, 'opus', endpoint)
    manager.record('opus', endpoint, 5000, {'total_cost': 1.2, 'currency': 'USD'})
    status, detail = status_of(lambda: enforce(manager, 'opus', endpoint, RequestContext(is_admin=True)))
    assert status == 402
    assert detail == '模型 opus 今日额度已用完（费用 $1.2 / $1），将于 2026-11-16 00:00 恢复。'
    enforce(manager, 'sonnet', {})  # 其他模型不受影响


def test_channel_quota_is_shared_by_its_models(manager, monkeypatch):
    monkeypatch.setitem(config_loader.CHANNEL_QUOTAS, 'ch-1', {'owner': 'ch-1', 'name': '主力中转', 'quota': quota(token_limit=1000)})
    first, second = {'channel': 'ch-1'}, {'channel': 'ch-1', 'quota': quota(request_limit=100)}
    manager.record('a', first, 600, None)
    manager.record('b', second, 500, None)
    for model, endpoint in (('a', first), ('b', second)):
        status, detail = status_of(lambda: enforce(manager, model, endpoint))
        assert status == 402 and detail.startswith('渠道 主力中转 累计额度已用完（Token 1,100 / 1,000）')
    enforce(manager, 'c', {'channel': 'ch-2'})
    assert manager.overview()['channels']['ch-1']['used']['tokens'] == 1100
    assert manager._usage['model:b']['tokens'] == 500 and 'model:a' not in manager._usage


def test_channels_sharing_a_quota_draw_from_one_pool(manager, monkeypatch):
    from core.model_channels import resolve_channel_quotas
    connection = {'api_base_url': 'https://example.test', 'api_keys': ['k']}
    monkeypatch.setattr(config_loader, 'CHANNEL_QUOTAS', resolve_channel_quotas({
        'eu': {'name': 'Vertex eu', **connection, 'quota': quota(cost_limit=1)},
        'us': {'name': 'Vertex us', **connection, 'quota_from': 'eu'},
        'lost': {'name': '断了', **connection, 'quota_from': 'gone'}}))
    assert set(config_loader.CHANNEL_QUOTAS) == {'eu', 'us'}  # 共用的渠道不存在时按不限额加载
    manager.record('a', {'channel': 'eu'}, 10, {'total_cost': 0.6})
    manager.record('b', {'channel': 'us'}, 10, {'total_cost': 0.5})
    status, detail = status_of(lambda: enforce(manager, 'b', {'channel': 'us'}))
    assert status == 402 and detail.startswith('渠道 Vertex eu 累计额度已用完（费用 $1.1 / $1）')
    assert list(manager.overview()['channels']) == ['eu'] and set(manager._usage) == {'channel:eu'}
    enforce(manager, 'c', {'channel': 'lost'})


def test_quota_stops_at_expiry_even_with_room_left(manager, monkeypatch):
    expires_at = datetime(2027, 1, 29).timestamp()
    endpoint = {'quota': quota(cost_limit=10, expires_at=expires_at)}
    monkeypatch.setattr(module.time, 'time', lambda: expires_at - 1)
    enforce(manager, 'm', endpoint)
    monkeypatch.setattr(module.time, 'time', lambda: expires_at)
    status, detail = status_of(lambda: enforce(manager, 'm', endpoint))
    assert status == 402 and detail == '模型 m 的额度已于 2027-01-29 00:00 到期，请联系管理员调整额度。'
    assert manager._usage['model:m']['requests'] == 1  # 到期挡下的请求不计次数


def test_expiry_alone_is_a_quota():
    assert quota(expires_at=1801238400) == {'period': 'total', 'cost_currency': 'USD', 'cost_limit': 0.0,
                                            'token_limit': 0, 'request_limit': 0, 'expires_at': 1801238400}
    for bad in (True, 0, -5, '2027-01-29', float('inf')):
        with pytest.raises(ValueError):
            quota(expires_at=bad)


def test_request_limit_counts_admissions_on_both_layers(manager, monkeypatch):
    monkeypatch.setitem(config_loader.CHANNEL_QUOTAS, 'ch-1', {'owner': 'ch-1', 'name': 'x', 'quota': quota(request_limit=5)})
    endpoint = {'channel': 'ch-1', 'quota': quota(request_limit=1)}
    enforce(manager, 'm', endpoint)
    assert status_of(lambda: enforce(manager, 'm', endpoint))[0] == 402
    # 被模型额度挡下的请求不占渠道的次数
    assert manager._usage['channel:ch-1']['requests'] == 1


def test_near_limit_admits_one_request_at_a_time(manager):
    endpoint = {'quota': quota(cost_limit=10)}
    manager.record('m', endpoint, 1, {'total_cost': 9.5})
    first = RequestContext()
    enforce(manager, 'm', endpoint, first)
    assert first.quota_holds == ['model:m']
    status, detail = status_of(lambda: enforce(manager, 'm', endpoint, RequestContext()))
    assert status == 429 and '同一时间只放行一个请求' in detail
    manager.release(first)
    enforce(manager, 'm', endpoint, RequestContext())


def test_nested_handlers_check_once_and_foreign_context_holds_nothing(manager):
    endpoint = {'quota': quota(request_limit=10)}
    context = RequestContext()
    enforce(manager, 'm', endpoint, context)
    enforce(manager, 'm', endpoint, context)
    assert manager._usage['model:m']['requests'] == 1 and context.quota_holds == ['model:m']
    manager.release(context)
    detached = RequestContext()
    manager.enforce('m', endpoint, detached)  # 不是当前请求的上下文：没有人负责归还，不记占用
    assert detached.quota_holds == [] and manager._inflight == {}


def test_observed_handler_never_runs_when_quota_is_used_up(manager, monkeypatch):
    from core import endpoint_observer
    monkeypatch.setattr(module, 'model_quota', manager)
    endpoint = {'api_type': 'direct_api', 'quota': quota(request_limit=1)}
    manager.enforce('m', endpoint)
    upstream = AsyncMock()

    @endpoint_observer.observe_endpoint
    async def handler(openai_req, model_name, endpoint_config):
        await upstream()

    token = current_request.set(RequestContext())
    try:
        with pytest.raises(HTTPException) as error:
            asyncio.run(handler({}, 'm', endpoint))
    finally:
        current_request.reset(token)
    assert error.value.status_code == 402 and not upstream.called


def test_middleware_returns_the_hold_when_request_finishes(manager, monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core import request_middleware
    from core.endpoint_observer import bind_endpoint
    monkeypatch.setattr(module, 'model_quota', manager)
    monkeypatch.setattr(request_middleware, 'model_quota', manager)
    app = FastAPI()
    app.add_middleware(request_middleware.GatewayRequestMiddleware)
    seen = []

    @app.post('/v1/chat/completions')
    async def route():
        bind_endpoint(current_request.get(), 'm', {'quota': quota(cost_limit=10)})
        seen.append(dict(manager._inflight))
        return {'ok': True}

    assert TestClient(app).post('/v1/chat/completions', json={}).status_code == 200
    assert seen == [{'model:m': 1}] and manager._inflight == {}


def test_usage_survives_restart_and_follows_rename(manager):
    endpoint = {'quota': quota(token_limit=100)}
    manager.record('old', endpoint, 40, None)
    manager.rename_model('old', 'new')
    manager.save_if_dirty()
    reloaded = module.ModelQuotaManager()
    assert reloaded._usage['model:new']['tokens'] == 40 and 'model:old' not in reloaded._usage
    reloaded.forget('model:new')
    assert 'model:new' not in reloaded._usage


def test_loader_collects_channel_quotas(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_loader, 'MODEL_ENDPOINT_MAP', {})
    monkeypatch.setattr(config_loader, 'CHANNEL_QUOTAS', {'stale': {}})
    (tmp_path / 'model_endpoint_map.json').write_text(json.dumps({'m': {'api_type': 'direct_api', 'channel': 'ch'}}))
    (tmp_path / 'model_channels.json').write_text(json.dumps({
        'ch': {'name': '中转', 'api_base_url': 'https://example.test', 'api_keys': ['k'], 'quota': quota(cost_limit=3)},
        'free': {'name': '免费', 'api_base_url': 'https://example.test', 'api_keys': ['k']}}))
    config_loader.load_model_endpoint_map(True)
    assert config_loader.CHANNEL_QUOTAS == {'ch': {'owner': 'ch', 'name': '中转', 'quota': quota(cost_limit=3)}}
    assert 'quota' not in config_loader.MODEL_ENDPOINT_MAP['m']


def test_channel_normalization_validates_quota():
    from core.model_channels import normalize_channel
    base = {'name': 'c', 'api_base_url': 'https://example.test', 'api_keys': ['k']}
    assert normalize_channel({**base, 'quota': {'cost_limit': 2}})['quota'] == quota(cost_limit=2)
    assert 'quota' not in normalize_channel({**base, 'quota': {'cost_limit': 0}})
    with pytest.raises(ValueError):
        normalize_channel({**base, 'quota': {'period': 'hour'}})


def admin_request(body):
    async def receive():
        return {'type': 'http.request', 'body': json.dumps(body).encode(), 'more_body': False}
    return Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': [], 'query_string': b''}, receive)


def test_model_save_validates_quota_and_restarts_usage_when_added(manager, monkeypatch):
    from routes import admin_routes
    monkeypatch.setattr(admin_routes, 'model_quota', manager)
    stored = {'m': {'api_type': 'direct_api'}}
    monkeypatch.setattr(admin_routes, 'read_json_file', AsyncMock(side_effect=lambda path: json.loads(json.dumps(stored))))
    monkeypatch.setattr(admin_routes, 'read_channels_file', AsyncMock(return_value={}))
    monkeypatch.setattr(admin_routes, 'write_json_file', AsyncMock(side_effect=lambda path, data: stored.update(data)))
    monkeypatch.setattr(admin_routes, '_invalidate_admin_stats_cache', AsyncMock())
    manager._usage['model:m'] = {'period_start': 0, 'requests': 9, 'tokens': 9, 'cost': {}}

    def save(config):
        return asyncio.run(admin_routes.update_model_config(admin_request({'model_name': 'm', 'old_model_name': 'm',
                                                                           'config': config}), lambda: None))

    with pytest.raises(HTTPException) as error:
        save({'api_type': 'direct_api', 'quota': {'period': 'hour'}})
    assert error.value.status_code == 400
    save({'api_type': 'direct_api', 'quota': {'cost_limit': 5}})
    assert stored['m']['quota'] == quota(cost_limit=5) and 'model:m' not in manager._usage
    manager.record('m', stored['m'], 10, None)
    save({'api_type': 'direct_api', 'quota': {'cost_limit': 8}})
    assert manager._usage['model:m']['tokens'] == 10  # 只改上限不清用量
    save({'api_type': 'direct_api', 'quota': {'cost_limit': 0}})
    assert 'quota' not in stored['m'] and 'model:m' not in manager._usage


@pytest.mark.parametrize('started_output', [False, True])
def test_failed_request_is_charged_only_after_upstream_output(monkeypatch, started_output):
    from modules import monitoring
    record, key_record = MagicMock(), MagicMock()
    monkeypatch.setattr(monitoring.model_quota, 'record', record)
    monkeypatch.setattr(monitoring.api_key_manager, 'record_usage', key_record)
    monkeypatch.setattr(monitoring.monitoring_service.log_manager, 'write_request_log', MagicMock())
    endpoint = {'channel': 'ch', 'quota': quota(cost_limit=1)}
    context = RequestContext(owner_id='admin', model='m', endpoint=endpoint)
    if started_output:
        context.mark('first_business')
    token = current_request.set(context)
    try:
        monitoring.monitoring_service.request_start('model-quota-charge', 'm')
        monitoring.monitoring_service.request_end('model-quota-charge', False, error='cut', input_tokens=7,
                                                  output_tokens=3, cost_info={'total_cost': 0.1})
        monitoring.monitoring_service._event_pool.submit(lambda: None).result(timeout=5)
    finally:
        current_request.reset(token)
    record.assert_called_once_with('m', endpoint, 10, {'total_cost': 0.1}, billable=started_output)
    assert key_record.call_args.kwargs == {'billable': started_output}
