"""记录协议转换时无法保留的内容，让损失可见，而不是静默丢弃。

记录挂在当前请求上下文的 outcome 里：响应头 X-Bridge-Warnings、监控详情都从这里读。
端点配置 strict_conversion=true 时，请求侧只要有损失就直接拒绝。
"""
import json

from core.request_context import current_request

WARNINGS_HEADER = 'x-bridge-warnings'
# 响应头过长会被部分代理拒绝，完整记录仍保留在监控里。
_MAX_HEADER_NOTES = 8
_MAX_NOTES = 50


class ConversionRejected(ValueError):
    """端点开启了严格转换，而请求里有内容无法转换。"""


def note_loss(field: str, message: str) -> None:
    context = current_request.get()
    if context is None:
        return
    notes = context.outcome.setdefault('conversion_notes', [])
    entry = {'field': field, 'applied': False, 'message': message}
    if entry not in notes and len(notes) < _MAX_NOTES:
        notes.append(entry)


def conversion_notes() -> list:
    context = current_request.get()
    return list(context.outcome.get('conversion_notes') or []) if context else []


def raise_if_strict(endpoint_config: dict) -> None:
    """请求侧转换结束后调用。响应侧已经开始输出，只能记录不能拒绝。"""
    if not isinstance(endpoint_config, dict) or not endpoint_config.get('strict_conversion'):
        return
    notes = conversion_notes()
    if notes:
        detail = '；'.join(f"{note['field']}: {note['message']}" for note in notes)
        raise ConversionRejected(f'端点开启了严格转换，以下内容无法转换到上游：{detail}')


def note_cache_diagnostics(message: dict) -> None:
    """记下 Claude 缓存诊断给出的未命中原因，排查缓存断点时不用再比对请求。"""
    context = current_request.get()
    diagnostics = message.get('diagnostics') if isinstance(message, dict) else None
    reason = diagnostics.get('cache_miss_reason') if isinstance(diagnostics, dict) else None
    if context is not None and reason:
        context.outcome['cache_miss_reason'] = reason


def warnings_header_value(existing: bytes = None) -> bytes:
    """合并路由自己写的告警和转换记录；没有内容时返回 None。"""
    merged = []
    if existing:
        try:
            parsed = json.loads(existing)
            merged = parsed if isinstance(parsed, list) else []
        except ValueError:
            merged = []
    merged.extend(note for note in conversion_notes() if note not in merged)
    if not merged:
        return None
    return json.dumps(merged[:_MAX_HEADER_NOTES], ensure_ascii=True, separators=(',', ':')).encode('ascii')
