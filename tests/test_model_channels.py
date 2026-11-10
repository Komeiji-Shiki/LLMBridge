"""渠道（多个模型共用的上游地址和 Key）测试

覆盖 core/model_channels.py 的纯函数、config_loader 加载时的展开与热更新，
以及 routes/admin_channels.py 的读-改-写：
- 展开：渠道字段覆盖模型自带的、单个与多个 Key 的写法、渠道不存在时不影响加载
- 整理：连接配置相同的模型归成一组，整理前后展开结果等价
- 删除与脱离：模型各自留一份地址和 Key
"""
import asyncio
import json
import os
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from core import config_loader
from core.model_channels import (
    CHANNELS_FILE, DEFAULT_FIELDS, adopt_suggestions, apply_channel_refs, assign_channel, channel_usage,
    connection_fields, expand_model_map, normalize_channel, remove_channel, settle_channel_defaults, suggest_channels,
)
from routes import admin_channels, admin_routes

CHANNELS = {
    'ch-one': {'name': '共用上游', 'api_base_url': 'https://shared.example/v1', 'api_keys': ['key-a', 'key-b'],
               'api_key_strategy': 'sticky', 'api_key_cooldown_seconds': 3600},
    'ch-solo': {'name': '单 Key', 'api_base_url': 'https://solo.example/v1', 'api_keys': ['only-key'],
                'api_key_auth_mode': 'x-api-key'},
}


def direct(url='https://shared.example/v1', keys=('key-a', 'key-b'), **extra):
    endpoint = {'api_type': 'direct_api', 'api_base_url': url, **extra}
    if len(keys) > 1:
        endpoint['api_keys'] = list(keys)
    elif keys:
        endpoint['api_key'] = keys[0]
    return endpoint


# ==================== 展开 ====================

class TestExpand:
    def test_channel_fields_replace_the_models_own(self):
        raw = {'m': {'api_type': 'direct_api', 'channel': 'ch-one', 'model_id': 'up', 'api_base_url': 'https://stale.example',
                     'api_key': 'stale'}}
        expanded = expand_model_map(raw, CHANNELS)['m']
        assert expanded == {'api_type': 'direct_api', 'channel': 'ch-one', 'model_id': 'up',
                            'api_base_url': 'https://shared.example/v1', 'api_keys': ['key-a', 'key-b'],
                            'api_key_strategy': 'sticky', 'api_key_cooldown_seconds': 3600}
        assert 'api_base_url' in raw['m'] and raw['m']['api_key'] == 'stale'

    def test_single_key_uses_the_string_field(self):
        expanded = expand_model_map({'m': {'api_type': 'anthropic_native', 'channel': 'ch-solo'}}, CHANNELS)['m']
        assert expanded['api_key'] == 'only-key' and 'api_keys' not in expanded
        assert expanded['api_key_auth_mode'] == 'x-api-key'

    def test_models_without_a_channel_are_returned_as_is(self):
        raw = {'plain': direct(), 'legacy': {'session_id': 's', 'mode': 'direct_chat'}}
        expanded = expand_model_map(raw, CHANNELS)
        assert expanded['plain'] is raw['plain'] and expanded['legacy'] is raw['legacy']

    def test_every_endpoint_of_a_list_config_is_expanded(self):
        raw = {'m': [{'api_type': 'direct_api', 'channel': 'ch-solo'}, direct('https://other.example', ('k',))]}
        first, second = expand_model_map(raw, CHANNELS)['m']
        assert first['api_base_url'] == 'https://solo.example/v1'
        assert second is raw['m'][1]

    def test_missing_channel_keeps_the_endpoint_loadable(self, caplog):
        raw = {'m': {'api_type': 'direct_api', 'channel': 'ch-gone', 'api_base_url': 'https://own.example'}}
        with caplog.at_level('WARNING'):
            assert expand_model_map(raw, CHANNELS)['m'] is raw['m']
        assert 'ch-gone' in caplog.text


# ==================== 渠道内容与引用 ====================

class TestNormalizeAndRefs:
    def test_normalize_trims_dedupes_and_drops_defaults(self):
        channel = normalize_channel({'name': ' 上游 ', 'api_base_url': ' https://a.example ', 'api_keys': ['k1', ' k1 ', '', 'k2'],
                                     'api_key_auth_mode': 'auto', 'api_key_strategy': 'round_robin',
                                     'api_key_cooldown_seconds': 99})
        assert channel == {'name': '上游', 'api_base_url': 'https://a.example', 'api_keys': ['k1', 'k2']}

    @pytest.mark.parametrize('data', [None, {'name': ''}, {'name': 'x'}, {'name': 'x', 'api_keys': 'k'},
                                      {'name': 'x', 'api_base_url': 'u', 'api_key_strategy': 'random'}])
    def test_normalize_rejects_bad_content(self, data):
        with pytest.raises(ValueError):
            normalize_channel(data)

    def test_saving_a_model_with_a_channel_drops_its_own_connection(self):
        cleaned = apply_channel_refs({**direct(), 'channel': 'ch-one', 'model_id': 'up'}, CHANNELS)
        assert cleaned == {'api_type': 'direct_api', 'channel': 'ch-one', 'model_id': 'up'}
        assert apply_channel_refs({**direct(), 'channel': ''}, CHANNELS) == direct()
        with pytest.raises(ValueError):
            apply_channel_refs({'api_type': 'direct_api', 'channel': 'ch-gone'}, CHANNELS)

    def test_assign_then_detach_restores_a_working_endpoint(self):
        raw = {'a': direct('https://old.example', ('old',)), 'legacy': {'session_id': 's'}}
        attached, changed = assign_channel(raw, ['a', 'legacy', 'absent'], 'ch-one', CHANNELS)
        assert changed == ['a'] and attached['a'] == {'api_type': 'direct_api', 'channel': 'ch-one'}
        assert attached['legacy'] is raw['legacy']
        assert channel_usage(attached) == {'ch-one': ['a']}
        detached, changed = assign_channel(attached, ['a'], None, CHANNELS)
        assert changed == ['a'] and detached['a'] == {'api_type': 'direct_api', **connection_fields(CHANNELS['ch-one'])}

    def test_removing_a_channel_only_detaches_its_own_users(self):
        raw = {'a': {'api_type': 'direct_api', 'channel': 'ch-one'},
               'mixed': [{'api_type': 'direct_api', 'channel': 'ch-one'}, {'api_type': 'direct_api', 'channel': 'ch-solo'}]}
        new_map, new_channels, detached = remove_channel(raw, CHANNELS, 'ch-one')
        assert detached == ['a', 'mixed'] and list(new_channels) == ['ch-solo']
        assert new_map['a']['api_keys'] == ['key-a', 'key-b'] and 'channel' not in new_map['a']
        assert new_map['mixed'][1] == {'api_type': 'direct_api', 'channel': 'ch-solo'}


# ==================== 整理现有模型 ====================

class TestSuggestAndAdopt:
    MODELS = {
        'a': direct(model_id='a-up'),
        'b': direct(keys=('key-b', 'key-a'), api_key_auth_mode='auto'),
        'c': direct('https://c.example/v1', ('c-key',)),
        'd': direct('https://c.example/v1', ('c-key',)),
        'other-key': direct('https://c.example/v1', ('another',)),
        'other-key-2': direct('https://c.example/v1', ('another',)),
        'alone': direct('https://alone.example', ('x',)),
        'no-connection': {'api_type': 'gemini_native'},
        'legacy': {'session_id': 's', 'api_base_url': 'https://shared.example/v1', 'api_key': 'key-a'},
        'multi': [direct(), direct()],
        'attached': {'api_type': 'direct_api', 'channel': 'ch-solo'},
        'matches-existing': direct('https://solo.example/v1', ('only-key',), api_key_auth_mode='x-api-key'),
    }

    def test_groups_models_whose_connection_is_identical(self):
        suggestions = suggest_channels(self.MODELS, {'ch-solo': CHANNELS['ch-solo']})
        by_models = {tuple(item['models']): item for item in suggestions}
        assert set(by_models) == {('a', 'b'), ('c', 'd'), ('other-key', 'other-key-2'), ('matches-existing',)}
        assert by_models[('matches-existing',)]['channel_id'] == 'ch-solo'
        assert by_models[('a', 'b')]['channel'] == {'name': 'shared.example', 'api_base_url': 'https://shared.example/v1',
                                                    'api_keys': ['key-a', 'key-b']}
        # 同一个地址不同的 Key 是两个渠道，名字不能撞
        assert sorted(by_models[key]['channel']['name'] for key in (('c', 'd'), ('other-key', 'other-key-2'))) == [
            'c.example', 'c.example #2']

    def test_adopting_keeps_every_model_equivalent(self):
        channels = {'ch-solo': CHANNELS['ch-solo']}
        new_map, new_channels, suggestions = adopt_suggestions(self.MODELS, channels)
        assert len(new_channels) == 4 and all(item['channel_id'] in new_channels for item in suggestions)
        assert new_map['a'] == {'api_type': 'direct_api', 'model_id': 'a-up', 'channel': suggestions[0]['channel_id']}
        before, after = expand_model_map(self.MODELS, channels), expand_model_map(new_map, new_channels)
        for name in self.MODELS:
            if name == 'b':
                # 同一组里 Key 的先后以第一个模型为准
                assert sorted(after[name]['api_keys']) == sorted(before[name]['api_keys'])
                continue
            old, new = before[name], after[name]
            if isinstance(old, dict) and old.get('api_type'):
                assert connection_fields(new) == connection_fields(old), name
                expected = {k: v for k, v in {**old, **connection_fields(old)}.items() if k != 'channel'}
                assert {k: v for k, v in new.items() if k != 'channel'} == expected, name
            else:
                assert new == old, name
        # 再整理一次没有新东西
        assert adopt_suggestions(new_map, new_channels)[2] == []


# ==================== 加载与热更新 ====================

def write_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)


@pytest.fixture
def workdir(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_loader, 'MODEL_ENDPOINT_MAP', {})
    monkeypatch.setattr(config_loader, 'CONFIG_FILE_MTIMES', {'model_endpoint_map.json': 0.0})
    return tmp_path


def test_loader_expands_channels_and_follows_channel_edits(workdir):
    write_json('model_endpoint_map.json', {'m': {'api_type': 'direct_api', 'channel': 'ch-solo'}, 'plain': direct()})
    config_loader.load_model_endpoint_map()
    assert config_loader.MODEL_ENDPOINT_MAP == {'m': {'api_type': 'direct_api', 'channel': 'ch-solo'}, 'plain': direct()}

    write_json(CHANNELS_FILE, {'ch-solo': CHANNELS['ch-solo']})
    config_loader.load_model_endpoint_map()
    assert config_loader.MODEL_ENDPOINT_MAP['m']['api_key'] == 'only-key'

    # 只改渠道文件（换 Key）也要生效，模型文件没动
    write_json(CHANNELS_FILE, {'ch-solo': {**CHANNELS['ch-solo'], 'api_keys': ['rotated']}})
    os.utime(CHANNELS_FILE, (1, 1))
    config_loader.load_model_endpoint_map()
    assert config_loader.MODEL_ENDPOINT_MAP['m']['api_key'] == 'rotated'

    # 渠道文件写坏时保留上一次的结果
    with open(CHANNELS_FILE, 'w', encoding='utf-8') as f:
        f.write('{broken')
    os.utime(CHANNELS_FILE, (2, 2))
    config_loader.load_model_endpoint_map()
    assert config_loader.MODEL_ENDPOINT_MAP['m']['api_key'] == 'rotated'


# ==================== 管理接口 ====================

class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


def run_admin(monkeypatch, operation, payload=None):
    async def call():
        monkeypatch.setattr(admin_routes, '_MODEL_ENDPOINT_MAP_LOCK', asyncio.Lock())
        monkeypatch.setattr(admin_routes, '_invalidate_admin_stats_cache', AsyncMock())
        monkeypatch.setattr(admin_channels, 'load_model_endpoint_map', lambda *args: None)
        function = getattr(admin_channels, operation)
        return await (function(FakeRequest(payload)) if payload is not None else function())
    return asyncio.run(call())


def read_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def test_admin_channel_lifecycle(workdir, monkeypatch):
    write_json('model_endpoint_map.json', {'a': direct(), 'b': direct(), 'alone': direct('https://alone.example', ('x',))})

    preview = run_admin(monkeypatch, 'get_channel_suggestions')['suggestions']
    assert preview == [{'channel_id': None, 'name': 'shared.example', 'api_base_url': 'https://shared.example/v1',
                        'key_count': 2, 'models': ['a', 'b']}]
    assert not os.path.exists(CHANNELS_FILE)

    adopted = run_admin(monkeypatch, 'adopt_channel_suggestions')
    channel_id = adopted['suggestions'][0]['channel_id']
    assert read_json('model_endpoint_map.json')['a'] == {'api_type': 'direct_api', 'channel': channel_id}
    assert read_json(os.path.join(adopted['backup'], 'model_endpoint_map.json'))['a'] == direct()

    listing = run_admin(monkeypatch, 'list_channels')
    assert listing['usage'] == {channel_id: ['a', 'b']} and listing['channels'][channel_id]['api_keys'] == ['key-a', 'key-b']

    # 换 Key：只改渠道
    saved = run_admin(monkeypatch, 'save_channel', {'id': channel_id, 'channel': {'name': '主力', 'api_base_url': 'https://shared.example/v1',
                                                                                  'api_keys': ['rotated']}})
    assert saved['id'] == channel_id and read_json(CHANNELS_FILE)[channel_id]['api_keys'] == ['rotated']
    with pytest.raises(HTTPException) as duplicate:
        run_admin(monkeypatch, 'save_channel', {'channel': {'name': '主力', 'api_base_url': 'https://x.example'}})
    assert duplicate.value.status_code == 409
    with pytest.raises(HTTPException) as invalid:
        run_admin(monkeypatch, 'save_channel', {'channel': {'name': '空的'}})
    assert invalid.value.status_code == 400

    moved = run_admin(monkeypatch, 'set_models_channel', {'model_names': ['alone'], 'channel': channel_id})
    assert moved['changed'] == ['alone'] and read_json('model_endpoint_map.json')['alone'] == {'api_type': 'direct_api', 'channel': channel_id}
    freed = run_admin(monkeypatch, 'set_models_channel', {'model_names': ['alone'], 'channel': None})
    assert freed['changed'] == ['alone']
    assert read_json('model_endpoint_map.json')['alone'] == {'api_type': 'direct_api', 'api_base_url': 'https://shared.example/v1',
                                                             'api_key': 'rotated'}

    deleted = run_admin(monkeypatch, 'delete_channel', {'id': channel_id})
    assert deleted['detached'] == ['a', 'b'] and read_json(CHANNELS_FILE) == {}
    assert read_json('model_endpoint_map.json')['b'] == {'api_type': 'direct_api', 'api_base_url': 'https://shared.example/v1',
                                                         'api_key': 'rotated'}


def test_saving_a_model_validates_and_normalizes_its_channel(workdir, monkeypatch):
    write_json('model_endpoint_map.json', {})
    write_json(CHANNELS_FILE, {'ch-solo': CHANNELS['ch-solo']})

    def save(config):
        async def call():
            monkeypatch.setattr(admin_routes, '_MODEL_ENDPOINT_MAP_LOCK', asyncio.Lock())
            monkeypatch.setattr(admin_routes, '_invalidate_admin_stats_cache', AsyncMock())
            return await admin_routes.update_model_config(FakeRequest({'model_name': 'm', 'config': config}), lambda: None)
        return asyncio.run(call())

    save({'api_type': 'direct_api', 'channel': 'ch-solo', 'api_base_url': 'https://stale.example', 'api_key': 'stale'})
    stored = read_json('model_endpoint_map.json')['m']
    assert stored['channel'] == 'ch-solo' and 'api_base_url' not in stored and 'api_key' not in stored
    with pytest.raises(HTTPException) as missing:
        save({'api_type': 'direct_api', 'channel': 'ch-gone'})
    assert missing.value.status_code == 400

    listing = asyncio.run(admin_routes.get_models_config({}, {}, lambda: None))
    assert listing['model_endpoint_map']['m']['channel'] == 'ch-solo' and 'api_key' not in listing['model_endpoint_map']['m']
    assert listing['channels'] == {'ch-solo': CHANNELS['ch-solo']}


# ==================== 渠道默认值 ====================

DEFAULTS = {'passthrough': True, 'enable_prefix': True, 'auto_retry': {'enabled': True, 'max_retries': 3}}
WITH_DEFAULTS = {'ch-d': {'name': '带默认值', 'api_base_url': 'https://d.example/v1', 'api_keys': ['k'], 'defaults': DEFAULTS},
                 'ch-solo': CHANNELS['ch-solo']}
BUILTIN = {'passthrough': False, 'convert_system_to_user': False, 'enable_prefix': False, 'enable_partial': False,
           'cached_tokens_mode': 'reverse'}


def settings(endpoint):
    """端点在可继承的各项上实际生效的取值"""
    return {field: endpoint.get(field, BUILTIN.get(field)) for field in DEFAULT_FIELDS}


class TestChannelDefaults:
    def test_normalize_keeps_valid_defaults_and_rejects_the_rest(self):
        channel = normalize_channel({'name': 'x', 'api_base_url': 'u',
                                     'defaults': {'passthrough': False, 'force_stream': None, 'cached_tokens_mode': 'forward'}})
        assert channel['defaults'] == {'passthrough': False, 'cached_tokens_mode': 'forward'}
        assert 'defaults' not in normalize_channel({'name': 'x', 'api_base_url': 'u', 'defaults': {}})
        for bad in ({'model_id': 'x'}, {'passthrough': 'yes'}, {'first_chunk_timeout_seconds': 0}, {'cached_tokens_mode': 'sideways'},
                    {'auto_retry': True}, ['passthrough']):
            with pytest.raises(ValueError):
                normalize_channel({'name': 'x', 'api_base_url': 'u', 'defaults': bad})

    def test_expand_puts_defaults_under_the_models_own_values(self):
        raw = {'follows': {'api_type': 'direct_api', 'channel': 'ch-d'},
               'differs': {'api_type': 'direct_api', 'channel': 'ch-d', 'passthrough': False, 'auto_retry': None}}
        expanded = expand_model_map(raw, WITH_DEFAULTS)
        assert expanded['follows']['passthrough'] is True and expanded['follows']['auto_retry'] == DEFAULTS['auto_retry']
        assert expanded['differs']['passthrough'] is False and expanded['differs']['enable_prefix'] is True
        assert 'auto_retry' not in expanded['differs']

    def test_saving_a_model_only_keeps_what_differs_from_the_channel(self):
        saved = apply_channel_refs({'api_type': 'direct_api', 'channel': 'ch-d', 'passthrough': True, 'enable_prefix': False,
                                    'auto_retry': None, 'force_stream': None, 'enable_partial': False}, WITH_DEFAULTS)
        assert saved == {'api_type': 'direct_api', 'channel': 'ch-d', 'enable_prefix': False, 'auto_retry': None, 'enable_partial': False}

    def test_joining_and_leaving_keep_each_models_behaviour(self):
        raw = {'same': direct(passthrough=True, enable_prefix=True), 'other': direct(passthrough=False), 'bare': direct()}
        joined, changed = assign_channel(raw, list(raw), 'ch-d', WITH_DEFAULTS)
        assert changed == list(raw)
        assert joined['same'] == {'api_type': 'direct_api', 'channel': 'ch-d', 'auto_retry': None}
        expanded = expand_model_map(joined, WITH_DEFAULTS)
        assert {name: settings(expanded[name]) for name in raw} == {name: settings(raw[name]) for name in raw}
        # 换到没有默认值的渠道、再脱离，效果都不变，也不留下 null
        moved, _ = assign_channel(joined, list(raw), 'ch-solo', WITH_DEFAULTS)
        assert moved['bare'] == {'api_type': 'direct_api', 'channel': 'ch-solo'}
        left, _ = assign_channel(joined, list(raw), None, WITH_DEFAULTS)
        assert {name: settings(left[name]) for name in raw} == {name: settings(raw[name]) for name in raw}
        assert all(None not in config.values() and 'channel' not in config for config in left.values())
        removed, _, _ = remove_channel(joined, WITH_DEFAULTS, 'ch-d')
        assert removed == left

    def test_changing_defaults_reports_models_that_differ(self):
        raw = {'follows': {'api_type': 'direct_api', 'channel': 'ch-d'},
               'explicit': {'api_type': 'direct_api', 'channel': 'ch-d', 'passthrough': False, 'convert_system_to_user': True},
               'legacy': {'api_type': 'direct_api', 'channel': 'ch-d', 'convert_system_to_user': False},
               'elsewhere': {'api_type': 'direct_api', 'channel': 'ch-solo', 'passthrough': False}}
        # 透传从开改成关，system 转 user 第一次统一，前缀续写不再统一，自动重试没动
        new = {'passthrough': False, 'convert_system_to_user': True, 'auto_retry': DEFAULTS['auto_retry']}
        base = {'api_type': 'direct_api', 'channel': 'ch-d', 'enable_prefix': True}

        kept, changed, conflicts = settle_channel_defaults(raw, 'ch-d', DEFAULTS, new)
        assert conflicts == {'convert_system_to_user': ['follows', 'legacy']} and changed == ['follows', 'explicit', 'legacy']
        assert kept['follows'] == {**base, 'convert_system_to_user': None}
        assert kept['explicit'] == base
        assert kept['legacy'] == {**base, 'convert_system_to_user': False}
        assert kept['elsewhere'] is raw['elsewhere']

        followed, _, conflicts = settle_channel_defaults(raw, 'ch-d', DEFAULTS, new, override=True)
        assert conflicts == {'convert_system_to_user': ['follows', 'legacy']}
        assert followed['follows'] == followed['explicit'] == followed['legacy'] == base

        # 默认值没变时，模型自己不同的取值不算冲突，也不动
        same, changed, conflicts = settle_channel_defaults({'own': {'api_type': 'direct_api', 'channel': 'ch-d', 'passthrough': False}},
                                                           'ch-d', DEFAULTS, DEFAULTS, override=True)
        assert (changed, conflicts) == ([], {}) and same['own']['passthrough'] is False


def test_admin_channel_defaults_ask_before_overriding_models(workdir, monkeypatch):
    models = {'a': {'api_type': 'direct_api', 'channel': 'ch-d', 'passthrough': False}, 'b': {'api_type': 'direct_api', 'channel': 'ch-d'}}
    plain = {'name': '带默认值', 'api_base_url': 'https://d.example/v1', 'api_keys': ['k']}
    write_json('model_endpoint_map.json', models)
    write_json(CHANNELS_FILE, {'ch-d': plain})

    asked = run_admin(monkeypatch, 'save_channel', {'id': 'ch-d', 'channel': {**plain, 'defaults': {'passthrough': True}}})
    assert asked == {'status': 'conflict', 'conflicts': {'passthrough': ['a', 'b']}}
    assert read_json(CHANNELS_FILE) == {'ch-d': plain} and read_json('model_endpoint_map.json') == models

    kept = run_admin(monkeypatch, 'save_channel', {'id': 'ch-d', 'channel': {**plain, 'defaults': {'passthrough': True}}, 'override': False})
    assert kept['status'] == 'success' and kept['changed'] == ['b']
    assert read_json(CHANNELS_FILE)['ch-d']['defaults'] == {'passthrough': True}
    assert read_json('model_endpoint_map.json') == {'a': models['a'], 'b': {**models['b'], 'passthrough': None}}

    both = {'passthrough': True, 'enable_prefix': True}
    followed = run_admin(monkeypatch, 'save_channel', {'id': 'ch-d', 'channel': {**plain, 'defaults': both}, 'override': True})
    assert followed['changed'] == []
    expanded = expand_model_map(read_json('model_endpoint_map.json'), read_json(CHANNELS_FILE))
    assert settings(expanded['a'])['enable_prefix'] is True and settings(expanded['a'])['passthrough'] is False
    assert 'passthrough' not in expanded['b'] and expanded['b']['enable_prefix'] is True

    # 取消统一：跟随的模型拿回原来的默认值
    run_admin(monkeypatch, 'save_channel', {'id': 'ch-d', 'channel': plain})
    assert read_json('model_endpoint_map.json') == {'a': {**models['a'], 'enable_prefix': True}, 'b': {**models['b'], 'enable_prefix': True}}
