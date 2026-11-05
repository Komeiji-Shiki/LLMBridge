"""上游保存的对象（Interactions、Responses）和创建它的线路、密钥的对应关系。

查询、取消、删除和 previous_*_id 续接的请求里没有模型名，网关靠这份记录找回线路；
多密钥轮询时也必须用回原来的密钥，对象只在创建它的项目里可见。
只存端点身份和密钥指纹，不存密钥原文；按调用方隔离，别人拿到 id 也取不到。

后台任务创建时还没有用量：记下当时已经记账的部分，轮询到终态时补记差额，
并用数据库认领保证同一个对象只补记一次。
"""
import copy
from dataclasses import dataclass
import logging
from typing import Any, Optional

from core import config_loader
from core import conversation_store as store_module
from core.request_context import credential_identity, endpoint_identity

logger = logging.getLogger(__name__)

INTERACTIONS, RESPONSES = 'interactions', 'responses'
# 到了这些状态，对象的用量就不会再变
TERMINAL_STATUSES = {
    INTERACTIONS: {'completed', 'failed', 'cancelled', 'incomplete', 'requires_action', 'requires_continuation'},
    RESPONSES: {'completed', 'failed', 'cancelled', 'incomplete'},
}


@dataclass
class Bound:
    alias: str
    config: dict
    key: str
    record: dict


def stores_object(body: Any) -> bool:
    """两家都默认保存；显式 store=false 的请求不会在上游留下对象。"""
    return isinstance(body, dict) and body.get('store') is not False


def remember(owner: str, kind: str, object_id: str, alias: str, config: dict, key: str,
             billed: tuple, status: Optional[str]) -> None:
    """记下对象属于哪条线路；billed 是这次请求已经记账的 (输入, 输出, 缓存)。"""
    if not owner or not object_id:
        return
    payload = {'model': alias, 'endpoint': endpoint_identity(config), 'credential': credential_identity(key),
               'billed': dict(zip(('input', 'output', 'cached'), (int(value or 0) for value in billed)))}
    try:
        store_module.conversation_store.remember_object(
            owner, kind, object_id, payload, settled=status in TERMINAL_STATUSES[kind])
    except Exception:
        logger.exception('[STORED_OBJECTS] 对象 %s 的线路记录保存失败，后续查询将无法定位', object_id)


def _endpoints(alias: str) -> list:
    configured = config_loader.MODEL_ENDPOINT_MAP.get(alias)
    if isinstance(configured, list):
        return [item for item in configured if isinstance(item, dict)]
    return [configured] if isinstance(configured, dict) else []


def resolve(owner: Optional[str], kind: str, object_id: str) -> Optional[Bound]:
    """找回创建对象时的线路和密钥；记录不存在、线路或密钥已从配置中删除时返回 None。"""
    from routes._direct_api_utils import _get_valid_keys
    if not owner or not object_id:
        return None
    record = store_module.conversation_store.stored_object(owner, kind, object_id)
    if record is None:
        return None
    for config in _endpoints(record['model']):
        if endpoint_identity(config) != record['endpoint']:
            continue
        for key in _get_valid_keys(config.get('api_keys') or config.get('api_key')):
            if credential_identity(key) == record['credential']:
                return Bound(record['model'], copy.deepcopy(config), key, record)
    logger.warning('[STORED_OBJECTS] 对象 %s 的线路或密钥已从配置中删除', object_id)
    return None


def forget(owner: str, kind: str, object_id: str) -> None:
    store_module.conversation_store.forget_object(owner, kind, object_id)


async def settle(context, kind: str, object_id: str, bound: Bound, value: Any, service, monitor) -> None:
    """对象第一次被观察到终态时，补记创建时还没有的用量。"""
    import asyncio
    import uuid
    from services.native_exchange import native_usage, text_from_native
    if not isinstance(value, dict) or value.get('status') not in TERMINAL_STATUSES[kind]:
        return
    claimed = await asyncio.to_thread(store_module.conversation_store.claim_settlement,
                                      context.owner_id, kind, object_id)
    if not claimed:
        return
    inputs, outputs, cached, usage = native_usage(value)
    billed = bound.record.get('billed') or {}
    extra_input = max(0, inputs - billed.get('input', 0))
    extra_output = max(0, outputs - billed.get('output', 0))
    extra_cached = max(0, min(cached - billed.get('cached', 0), extra_input))
    if not (extra_input or extra_output):
        return
    request_id = uuid.uuid4().hex
    display = bound.config.get('display_name') or bound.alias
    monitor.request_start(
        request_id=request_id, model=display, mode=f'native_{kind}_background',
        messages=[{'role': 'user', 'content': f'后台任务 {object_id} 的结果'}],
        params={'protocol': kind, 'object_id': object_id, 'model_alias': bound.alias,
                'caller_id': context.owner_id, 'caller_name': context.owner_name,
                'gateway_request_id': context.request_id})
    failed = value.get('status') == 'failed'
    error = value.get('error') or value.get('errors')
    cost = service.calculate_cost(extra_input, extra_output, bound.config.get('pricing') or {}, cached_tokens=extra_cached)
    monitor.request_end(request_id=request_id, success=not failed, error=str(error or 'failed') if failed else None,
                        input_tokens=extra_input, output_tokens=extra_output, cached_tokens=extra_cached,
                        cost_info=cost, response_content=text_from_native(value), upstream_usage=usage or None)
    await monitor.broadcast_to_monitors({'type': 'request_end', 'request_id': request_id, 'success': not failed})
