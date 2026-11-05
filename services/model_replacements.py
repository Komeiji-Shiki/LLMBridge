"""上游明确宣布模型已弃用并点名替代型号时，自动改用替代型号。

只在请求已经失败、上游亲口给出型号时才切换，不猜测。同一线路的后续请求直接用新型号，
进程重启后由下一次失败重新学习；配置文件不会被改写，请在后台更新 model_id。
端点配置 auto_model_replacement=false 可以关闭。
"""
import logging
import re
from typing import Any, Dict, Iterable, Optional, Tuple

from core.request_context import current_request
from services.error_translation import MODEL_RETIRED, classify_error

logger = logging.getLogger(__name__)

_REPLACEMENT_HINT = re.compile(r'\buse\s+([\w./:-]+)\s+instead\b', re.IGNORECASE)
_replacements: Dict[Tuple[str, str], str] = {}


def replacement_from_error(values: Iterable[Any]) -> Optional[str]:
    """从上游错误里取替代型号；不是弃用错误或没有点名时返回 None。"""
    for value in values:
        error = value.get('error') if isinstance(value, dict) else None
        if not isinstance(error, dict) or classify_error(error) != MODEL_RETIRED:
            continue
        for metadata in (value.get('metadata'), error.get('metadata')):
            if isinstance(metadata, dict) and isinstance(metadata.get('replacement'), str) and metadata['replacement']:
                return metadata['replacement']
        hint = _REPLACEMENT_HINT.search(str(error.get('message') or ''))
        if hint:
            return hint.group(1).rstrip('.')
    return None


def _enabled(config: dict) -> bool:
    return config.get('auto_model_replacement', True) is not False


def _swap(arguments: dict, old: str, new: str) -> None:
    if arguments.get('model') == old:
        arguments['model'] = new
    body = arguments.get('request_body')
    if isinstance(body, dict) and body.get('model') == old:
        arguments['request_body'] = {**body, 'model': new}
    # 原生 Gemini 把型号写在路径里
    path = arguments.get('endpoint_path')
    if isinstance(path, str) and f'/models/{old}:' in path:
        arguments['endpoint_path'] = path.replace(f'/models/{old}:', f'/models/{new}:')


def _current_model(arguments: dict, config: dict) -> Optional[str]:
    body = arguments.get('request_body')
    return arguments.get('model') or (body.get('model') if isinstance(body, dict) else None) or config.get('model_id')


def apply_known_replacement(arguments: dict, config: dict) -> None:
    """请求发出前调用：这条线路上已知弃用的型号直接换掉。"""
    if not _enabled(config):
        return
    old = _current_model(arguments, config)
    new = _replacements.get((config.get('api_base_url') or '', old))
    if new:
        _swap(arguments, old, new)
        _record(old, new)


def learn_replacement(values: Iterable[Any], arguments: dict, config: dict) -> bool:
    """请求失败后调用：学到新的替代型号时返回 True，调用方应立刻重试一次。"""
    if not _enabled(config):
        return False
    new = replacement_from_error(values)
    old = _current_model(arguments, config)
    if not new or not old or new == old:
        return False
    logger.warning('[MODEL_REPLACED] 上游宣布 %s 已弃用，本线路改用 %s；请在后台更新 model_id', old, new)
    _replacements[(config.get('api_base_url') or '', old)] = new
    _swap(arguments, old, new)
    _record(old, new)
    return True


def _record(old: str, new: str) -> None:
    context = current_request.get()
    if context is not None:
        context.outcome['model_replaced'] = {'from': old, 'to': new}
