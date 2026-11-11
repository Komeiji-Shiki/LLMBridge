"""
渠道：多个模型共用的上游地址和 Key

很多模型接的是同一个上游，地址和 Key 完全一样，换一把 Key 要逐个模型改。
这部分抽成渠道存在 model_channels.json，模型配置里只留一个 channel 引用；
加载时再展开成完整配置，请求链路读到的 MODEL_ENDPOINT_MAP 和以前一样，
不需要知道渠道的存在。

渠道还可以给一部分模型设置统一的默认值（defaults）：模型自己写了的用自己的，
没写的跟渠道走，所以模型配置里只剩和渠道不一样的那几项。

本模块只有纯函数，不做文件 IO 与加锁；
文件读写/锁/配置重载由调用方（config_loader / admin_channels）负责。
"""
import hashlib
import json
import logging
import secrets
from typing import Callable, List, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

CHANNELS_FILE = 'model_channels.json'

# 接了渠道的模型，这些字段一律以渠道为准
CHANNEL_FIELDS = ('api_base_url', 'api_key', 'api_keys', 'api_key_auth_mode',
                  'api_key_strategy', 'api_key_cooldown_seconds')
# 只有这些协议的模型才有上游地址和 Key，LMArena 旧配置不参与
_DIRECT_TYPES = ('direct_api', 'responses_native', 'anthropic_native', 'gemini_native')

# 渠道可以统一给默认值的模型设置：模型自己写了用自己的，没写就跟渠道走，写成 null 表示明确不设置
DEFAULT_FIELDS = ('passthrough', 'convert_system_to_user', 'enable_prefix', 'enable_partial', 'force_stream',
                  'first_chunk_timeout_seconds', 'cached_tokens_mode', 'auto_retry', 'image_compression',
                  'strip_claude_agent_sdk_prefix')
_BOOLEAN_DEFAULTS = ('passthrough', 'convert_system_to_user', 'enable_prefix', 'enable_partial', 'force_stream',
                     'strip_claude_agent_sdk_prefix')
# 这些项不写时请求链路实际采用的取值，用来判断模型的值和渠道默认值是不是一回事
_BUILTIN_VALUES = {'passthrough': False, 'convert_system_to_user': False, 'enable_prefix': False, 'enable_partial': False,
                   'cached_tokens_mode': 'reverse', 'strip_claude_agent_sdk_prefix': False}


def endpoint_keys(endpoint: dict) -> List[str]:
    """一份配置里的 Key 列表：api_keys 数组非空时用它，否则用单个 api_key。"""
    keys = endpoint.get('api_keys')
    if isinstance(keys, list) and keys:
        return [key for key in keys if isinstance(key, str) and key.strip()]
    key = endpoint.get('api_key')
    return [key] if isinstance(key, str) and key.strip() else []


def connection_fields(endpoint: dict) -> dict:
    """取出一份配置里属于“连接”的部分。

    写法和模型配置保持一致：单个 Key 用 api_key，多个用 api_keys；
    等于默认值的鉴权方式和轮询策略不写，冷却时长只在粘性轮询下保留。
    """
    fields = {}
    if endpoint.get('api_base_url'):
        fields['api_base_url'] = endpoint['api_base_url']
    keys = endpoint_keys(endpoint)
    if len(keys) > 1:
        fields['api_keys'] = keys
    elif keys:
        fields['api_key'] = keys[0]
    if endpoint.get('api_key_auth_mode') not in (None, '', 'auto'):
        fields['api_key_auth_mode'] = endpoint['api_key_auth_mode']
    if endpoint.get('api_key_strategy') == 'sticky':
        fields['api_key_strategy'] = 'sticky'
        cooldown = endpoint.get('api_key_cooldown_seconds')
        if isinstance(cooldown, (int, float)) and not isinstance(cooldown, bool) and cooldown > 0:
            fields['api_key_cooldown_seconds'] = int(cooldown)
    return fields


def _normalize_defaults(data) -> dict:
    """校验渠道的默认设置，只留真正给了值的项。"""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError('defaults 必须是 JSON 对象')
    defaults = {}
    for field, value in data.items():
        if field not in DEFAULT_FIELDS:
            raise ValueError(f'渠道不能统一设置 {field}')
        if value is None:
            continue
        if field in _BOOLEAN_DEFAULTS and not isinstance(value, bool):
            raise ValueError(f'{field} 必须是 true 或 false')
        if field == 'first_chunk_timeout_seconds' and (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0):
            raise ValueError('first_chunk_timeout_seconds 必须是正数')
        if field == 'cached_tokens_mode' and value not in ('off', 'forward', 'reverse'):
            raise ValueError(f'未知的缓存 Token 统计模式: {value}')
        if field in ('auto_retry', 'image_compression') and not isinstance(value, dict):
            raise ValueError(f'{field} 必须是 JSON 对象')
        defaults[field] = value
    return defaults


def normalize_channel(data) -> dict:
    """校验并整理一个渠道的内容，格式不对时抛 ValueError。

    渠道里的 Key 一律存成 api_keys 数组（哪怕只有一个），展开到模型时再按模型的写法转换。
    """
    if not isinstance(data, dict):
        raise ValueError('渠道必须是 JSON 对象')
    name = data.get('name')
    if not isinstance(name, str) or not name.strip():
        raise ValueError('渠道名称不能为空')
    url = data.get('api_base_url') or ''
    if not isinstance(url, str):
        raise ValueError('api_base_url 必须是字符串')
    keys = data.get('api_keys') or []
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise ValueError('api_keys 必须是字符串列表')
    keys = list(dict.fromkeys(key.strip() for key in keys if key.strip()))
    if not url.strip() and not keys:
        raise ValueError('地址和 Key 至少填一项')
    strategy = data.get('api_key_strategy') or 'round_robin'
    if strategy not in ('round_robin', 'sticky'):
        raise ValueError(f'未知的轮询策略: {strategy}')
    auth_mode = data.get('api_key_auth_mode') or 'auto'
    if not isinstance(auth_mode, str):
        raise ValueError('api_key_auth_mode 必须是字符串')

    defaults = _normalize_defaults(data.get('defaults'))

    channel = {'name': name.strip(), 'api_base_url': url.strip(), 'api_keys': keys}
    if auth_mode != 'auto':
        channel['api_key_auth_mode'] = auth_mode
    if strategy == 'sticky':
        channel['api_key_strategy'] = 'sticky'
        cooldown = data.get('api_key_cooldown_seconds')
        if isinstance(cooldown, (int, float)) and not isinstance(cooldown, bool) and cooldown > 0:
            channel['api_key_cooldown_seconds'] = int(cooldown)
    if defaults:
        channel['defaults'] = defaults
    return channel


def _is_direct(endpoint) -> bool:
    return isinstance(endpoint, dict) and endpoint.get('api_type') in _DIRECT_TYPES


def _strip_connection(endpoint: dict) -> dict:
    return {key: value for key, value in endpoint.items() if key not in CHANNEL_FIELDS}


def _map_endpoints(model_map: dict, model_names, change: Callable[[dict], dict]):
    """对指定模型的每个直连端点应用 change，返回 (新 map, 实际变更的模型名列表)。

    dict 与 list（多端点）两种配置形态都处理；不存在的模型名静默忽略。
    """
    new_map = dict(model_map)
    changed: List[str] = []
    for name in model_names:
        config = new_map.get(name)
        if config is None:
            continue
        items = config if isinstance(config, list) else [config]
        new_items = [change(item) if _is_direct(item) else item for item in items]
        if new_items != items:
            new_map[name] = new_items if isinstance(config, list) else new_items[0]
            changed.append(name)
    return new_map, changed


# ==================== 加载时展开 ====================

def expand_model_map(model_map: dict, channels: dict) -> dict:
    """把引用了渠道的端点展开成完整配置。

    没有引用渠道的端点原样返回（同一个对象，不复制）。
    引用的渠道不存在时保留端点自己的内容并记一条警告，不让整张表加载失败。
    """
    missing = set()

    def expand(endpoint):
        if not isinstance(endpoint, dict) or not endpoint.get('channel'):
            return endpoint
        channel = channels.get(endpoint['channel'])
        if not isinstance(channel, dict):
            missing.add(endpoint['channel'])
            return endpoint
        # 渠道的默认设置打底，模型自己的盖在上面；写成 null 的项是明确不设置
        expanded = {**(channel.get('defaults') or {}), **_strip_connection(endpoint)}
        expanded = {key: value for key, value in expanded.items() if value is not None or key not in DEFAULT_FIELDS}
        expanded.update(connection_fields(channel))
        return expanded

    expanded_map = {
        name: [expand(item) for item in config] if isinstance(config, list) else expand(config)
        for name, config in model_map.items()
    }
    if missing:
        logger.warning(f"模型引用了不存在的渠道: {', '.join(sorted(missing))}，这些模型按自身配置加载")
    return expanded_map


# ==================== 引用关系 ====================

def apply_channel_refs(config, channels: dict):
    """保存模型前整理渠道引用：引用的渠道必须存在，接了渠道就不再保留自己的地址和 Key，
    和渠道默认设置一样的项也不再自己存。"""
    def clean(endpoint):
        if not isinstance(endpoint, dict) or 'channel' not in endpoint:
            return endpoint
        channel_id = endpoint['channel']
        if not channel_id:
            return {key: value for key, value in endpoint.items() if key != 'channel'}
        if channel_id not in channels:
            raise ValueError(f'渠道 {channel_id} 不存在')
        return _follow_defaults(_strip_connection(endpoint), channels[channel_id].get('defaults') or {})

    return [clean(item) for item in config] if isinstance(config, list) else clean(config)


def channel_usage(model_map: dict) -> dict:
    """每个渠道被哪些模型引用：{渠道 id: [模型名]}。"""
    usage: dict = {}
    for name, config in model_map.items():
        for endpoint in config if isinstance(config, list) else [config]:
            if isinstance(endpoint, dict) and endpoint.get('channel'):
                models = usage.setdefault(endpoint['channel'], [])
                if name not in models:
                    models.append(name)
    return usage


def assign_channel(model_map: dict, model_names, channel_id: Optional[str], channels: dict):
    """让一批模型改用某个渠道，返回 (新 map, 实际变更的模型名列表)。

    channel_id 为空表示脱离渠道：把渠道当前的地址、Key 和模型跟随着的默认设置抄回模型，模型照常可用。
    """
    def change(endpoint):
        if channel_id:
            # 从别的渠道换过来的先按原渠道还原，再按新渠道的默认设置整理，模型的效果不变
            updated = _strip_connection(_detach(endpoint, channels))
            updated['channel'] = channel_id
            return _join_defaults(updated, channels[channel_id].get('defaults') or {})
        return _detach(endpoint, channels)

    return _map_endpoints(model_map, model_names, change)


def _detach(endpoint: dict, channels: dict) -> dict:
    current = channels.get(endpoint.get('channel'))
    detached = {key: value for key, value in endpoint.items() if key != 'channel'}
    if isinstance(current, dict):
        detached = _leave_defaults(_strip_connection(detached), current.get('defaults') or {})
        detached.update(connection_fields(current))
    return detached


def remove_channel(model_map: dict, channels: dict, channel_id: str):
    """删除渠道，返回 (新 map, 新渠道表, 受影响的模型名列表)。

    还在用它的端点各自留一份地址和 Key，删除不会让模型失效。
    """
    users = channel_usage(model_map).get(channel_id, [])
    new_map, changed = _map_endpoints(
        model_map, users,
        lambda endpoint: _detach(endpoint, channels) if endpoint.get('channel') == channel_id else endpoint)
    return new_map, {key: value for key, value in channels.items() if key != channel_id}, changed


def new_channel_id(channels: dict) -> str:
    while True:
        channel_id = 'ch-' + secrets.token_hex(4)
        if channel_id not in channels:
            return channel_id


# ==================== 渠道默认值 ====================

def _effective(endpoint: dict, field: str):
    """端点在这一项上的取值；没写或写成 null 都按请求链路本来的取值算。"""
    value = endpoint.get(field)
    return _BUILTIN_VALUES.get(field) if value is None else value


def _follow_defaults(endpoint: dict, defaults: dict) -> dict:
    """保存模型时整理可继承的项：和渠道默认值一样的不存（跟着渠道走）；渠道没给默认值时，“明确不设置”等于不写。"""
    cleaned = dict(endpoint)
    for field in DEFAULT_FIELDS:
        if field not in cleaned:
            continue
        if field in defaults:
            if _effective(cleaned, field) == defaults[field]:
                del cleaned[field]
        elif cleaned[field] is None:
            del cleaned[field]
    return cleaned


def _join_defaults(endpoint: dict, defaults: dict) -> dict:
    """端点接上带默认值的渠道时保持原来的效果：和默认值一样的不再自己存，不一样的留着，没写过的记成明确不设置。"""
    joined = dict(endpoint)
    for field, default in defaults.items():
        if _effective(joined, field) == default:
            joined.pop(field, None)
        elif joined.get(field) is None:
            joined[field] = None
    return joined


def _leave_defaults(endpoint: dict, defaults: dict) -> dict:
    """端点离开渠道时把跟随的默认值抄回来，明确不设置的去掉。"""
    left = {**defaults, **endpoint}
    return {key: value for key, value in left.items() if value is not None or key not in DEFAULT_FIELDS}


def settle_channel_defaults(model_map: dict, channel_id: str, old_defaults: dict, new_defaults: dict, override: bool = False):
    """渠道默认值改动后整理用它的模型，返回 (新 map, 变更的模型名列表, 冲突)。

    原来跟着渠道走的继续跟。模型自己有取值的：和新默认值一样就改成跟随；不一样、而这一项的默认值这次确实改了，算冲突，
    override 为真时也改成跟随，否则保留各自的效果。某一项不再统一时，把原来的默认值抄给跟随它的模型。
    冲突 = {字段: [模型名]}。
    """
    conflicts: dict = {}

    def settle(name: str, endpoint):
        if not isinstance(endpoint, dict) or endpoint.get('channel') != channel_id:
            return endpoint
        updated = dict(endpoint)
        for field in DEFAULT_FIELDS:
            had = field in old_defaults
            following = had and field not in updated
            if field in new_defaults:
                if following:
                    continue
                if _effective(updated, field) == new_defaults[field]:
                    updated.pop(field, None)
                elif not had or old_defaults[field] != new_defaults[field]:
                    names = conflicts.setdefault(field, [])
                    if name not in names:
                        names.append(name)
                    if override:
                        updated.pop(field, None)
                    elif field not in updated:
                        updated[field] = None
            elif following:
                updated[field] = old_defaults[field]
            elif had and updated.get(field, 0) is None:
                del updated[field]
        return updated

    new_map = dict(model_map)
    changed: List[str] = []
    for name in channel_usage(model_map).get(channel_id, []):
        config = model_map[name]
        settled = [settle(name, item) for item in config] if isinstance(config, list) else settle(name, config)
        if settled != config:
            new_map[name] = settled
            changed.append(name)
    return new_map, changed, conflicts


# ==================== 把现有模型整理成渠道 ====================

def connection_signature(endpoint: dict) -> Optional[str]:
    """地址、Key（不论顺序）、鉴权方式和轮询策略都相同的配置得到同一个签名。

    既没有地址也没有 Key 的配置没有可共用的东西，返回 None。
    """
    fields = connection_fields(endpoint)
    keys = sorted(endpoint_keys(endpoint))
    if not fields.get('api_base_url') and not keys:
        return None
    fields.pop('api_key', None)
    fields['api_keys'] = keys
    canonical = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]


def suggest_channels(model_map: dict, channels: dict, min_models: int = 2) -> list:
    """找出还没接渠道、但连接配置完全相同的模型，按组给出建议。

    某组的连接配置和已有渠道相同，就建议并入那个渠道（哪怕只有一个模型）；
    否则至少 min_models 个模型共用才值得新建。多端点模型各端点的连接可能不同，不参与。
    返回按模型数从多到少排列的列表，每项：
    {'channel_id': 已有渠道 id 或 None, 'channel': 渠道内容, 'models': [模型名]}
    """
    groups: dict = {}
    for name, config in model_map.items():
        if isinstance(config, list) and len(config) != 1:
            continue
        endpoint = config[0] if isinstance(config, list) else config
        if not _is_direct(endpoint) or endpoint.get('channel'):
            continue
        signature = connection_signature(endpoint)
        if signature:
            groups.setdefault(signature, {'endpoint': endpoint, 'models': []})['models'].append(name)

    existing = {}
    for channel_id, channel in channels.items():
        if isinstance(channel, dict):
            existing.setdefault(connection_signature(channel), channel_id)
    used_names = {channel.get('name') for channel in channels.values() if isinstance(channel, dict)}

    suggestions = []
    for signature, group in sorted(groups.items(), key=lambda item: -len(item[1]['models'])):
        channel_id = existing.get(signature)
        if channel_id:
            suggestions.append({'channel_id': channel_id, 'channel': channels[channel_id], 'models': group['models']})
            continue
        if len(group['models']) < min_models:
            continue
        endpoint = group['endpoint']
        base = urlsplit(endpoint.get('api_base_url') or '').netloc or endpoint.get('api_base_url') or '默认地址'
        name, index = base, 1
        while name in used_names:
            index += 1
            name = f'{base} #{index}'
        used_names.add(name)
        channel = normalize_channel({**endpoint, 'name': name, 'api_keys': endpoint_keys(endpoint)})
        suggestions.append({'channel_id': None, 'channel': channel, 'models': group['models']})
    return suggestions


def adopt_suggestions(model_map: dict, channels: dict, min_models: int = 2):
    """按 suggest_channels 的结果建渠道并让对应模型引用，返回 (新 map, 新渠道表, 建议列表)。

    建议列表里新建的渠道会补上分配到的 channel_id。展开后的配置和整理前等价。
    """
    new_channels = dict(channels)
    new_map = model_map
    suggestions = suggest_channels(model_map, channels, min_models)
    for suggestion in suggestions:
        if not suggestion['channel_id']:
            suggestion['channel_id'] = new_channel_id(new_channels)
            new_channels[suggestion['channel_id']] = suggestion['channel']
        new_map, _ = assign_channel(new_map, suggestion['models'], suggestion['channel_id'], new_channels)
    return new_map, new_channels, suggestions
