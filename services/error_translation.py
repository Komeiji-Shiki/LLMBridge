"""把各上游的错误翻译成客户端协议认识的语义。

客户端的自动恢复靠错误类型和措辞判断：Anthropic 客户端看 "prompt is too long"，
OpenAI 客户端看 context_length_exceeded。上游原话保留在消息里，方便排查。
"""
import re
from typing import Any, Dict, Optional

CONTEXT_OVERFLOW = 'context_overflow'
RATE_LIMIT = 'rate_limit'
OVERLOADED = 'overloaded'
AUTHENTICATION = 'authentication'
PERMISSION = 'permission'
NOT_FOUND = 'not_found'
MODEL_RETIRED = 'model_retired'
INVALID_REQUEST = 'invalid_request'
TIMEOUT = 'timeout'

_ANTHROPIC_TYPES = {
    CONTEXT_OVERFLOW: 'invalid_request_error', RATE_LIMIT: 'rate_limit_error', OVERLOADED: 'overloaded_error',
    AUTHENTICATION: 'authentication_error', PERMISSION: 'permission_error', NOT_FOUND: 'not_found_error',
    MODEL_RETIRED: 'not_found_error', INVALID_REQUEST: 'invalid_request_error', TIMEOUT: 'timeout_error',
}
_OPENAI_FIELDS = {
    CONTEXT_OVERFLOW: ('invalid_request_error', 'context_length_exceeded'),
    RATE_LIMIT: ('rate_limit_error', 'rate_limit_exceeded'),
    OVERLOADED: ('server_error', 'server_is_overloaded'),
    AUTHENTICATION: ('authentication_error', 'invalid_api_key'),
    PERMISSION: ('permission_error', 'permission_denied'),
    NOT_FOUND: ('not_found_error', 'model_not_found'),
    MODEL_RETIRED: ('not_found_error', 'model_not_found'),
    INVALID_REQUEST: ('invalid_request_error', 'invalid_request'),
    TIMEOUT: ('timeout_error', 'timeout'),
}
_OVERFLOW_PATTERN = re.compile(
    r'prompt is too long|context[_ ]length|maximum context|context window|too many (input )?tokens'
    r'|input token count.*exceeds|exceeds the maximum number of tokens|input is too long|reduce the length',
    re.IGNORECASE)
_RETIRED_PATTERN = re.compile(
    r'model.?deprecated|model.{0,60}(has been )?(deprecated|retired|discontinued|sunset|no longer (available|supported))'
    r'|(deprecated|retired|discontinued).{0,30}model', re.IGNORECASE)
_TOKEN_COUNTS = re.compile(r'(\d[\d,]{2,})\D{1,60}?(\d[\d,]{2,})')
_STATUS_KINDS = {401: AUTHENTICATION, 403: PERMISSION, 404: NOT_FOUND, 408: TIMEOUT, 429: RATE_LIMIT,
                 503: OVERLOADED, 504: TIMEOUT, 529: OVERLOADED}
_GOOGLE_STATUS_KINDS = {'RESOURCE_EXHAUSTED': RATE_LIMIT, 'UNAVAILABLE': OVERLOADED, 'UNAUTHENTICATED': AUTHENTICATION,
                        'PERMISSION_DENIED': PERMISSION, 'NOT_FOUND': NOT_FOUND, 'DEADLINE_EXCEEDED': TIMEOUT,
                        'INVALID_ARGUMENT': INVALID_REQUEST, 'FAILED_PRECONDITION': INVALID_REQUEST}
_TYPE_KINDS = {'rate_limit_error': RATE_LIMIT, 'rate_limit_exceeded': RATE_LIMIT, 'overloaded_error': OVERLOADED,
               'authentication_error': AUTHENTICATION, 'permission_error': PERMISSION, 'not_found_error': NOT_FOUND,
               'invalid_request_error': INVALID_REQUEST, 'timeout_error': TIMEOUT, 'insufficient_quota': RATE_LIMIT,
               'model_not_found': NOT_FOUND, 'context_length_exceeded': CONTEXT_OVERFLOW}


def _status_code(error: Dict[str, Any], status: Optional[int]) -> Optional[int]:
    for value in (status, error.get('code'), error.get('status')):
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def classify_error(error: Any, status: Optional[int] = None) -> Optional[str]:
    """按措辞、类型和状态码归类；措辞优先，中转经常把超长报成 500。"""
    if not isinstance(error, dict):
        error = {'message': str(error or '')}
    message = str(error.get('message') or '')
    code = str(error.get('code') or '')
    kind = str(error.get('type') or '')
    if code == 'context_length_exceeded' or _OVERFLOW_PATTERN.search(message):
        return CONTEXT_OVERFLOW
    if 'modeldeprecated' in kind.lower() or _RETIRED_PATTERN.search(message):
        return MODEL_RETIRED
    for value in (code, kind):
        if value in _TYPE_KINDS:
            return _TYPE_KINDS[value]
    google_status = str(error.get('status') or '')
    if google_status in _GOOGLE_STATUS_KINDS:
        return _GOOGLE_STATUS_KINDS[google_status]
    numeric = _status_code(error, status)
    if numeric in _STATUS_KINDS:
        return _STATUS_KINDS[numeric]
    return INVALID_REQUEST if numeric == 400 else None


def _anthropic_overflow_message(message: str) -> str:
    if message.lower().startswith('prompt is too long'):
        return message
    counts = _TOKEN_COUNTS.search(message)
    if counts:
        first, second = (int(value.replace(',', '')) for value in counts.groups())
        return f'prompt is too long: {max(first, second)} tokens > {min(first, second)} maximum. {message}'
    return f'prompt is too long. {message}'


def translate_anthropic_error(error: Dict[str, Any], status: Optional[int] = None) -> Dict[str, Any]:
    """返回 Anthropic 客户端能识别的 type 和 message，其余诊断字段不动。"""
    kind = classify_error(error, status)
    if kind is None:
        return error
    translated = {**error, 'type': _ANTHROPIC_TYPES[kind]}
    if kind == CONTEXT_OVERFLOW:
        translated['message'] = _anthropic_overflow_message(str(error.get('message') or ''))
    return translated


def translate_openai_error(error: Dict[str, Any], status: Optional[int] = None) -> Dict[str, Any]:
    """补全 OpenAI 风格的 type 和 code；上游给了明确 code 时不覆盖。"""
    kind = classify_error(error, status)
    if kind is None:
        return error
    error_type, code = _OPENAI_FIELDS[kind]
    if kind == CONTEXT_OVERFLOW or not isinstance(error.get('code'), str):
        return {**error, 'type': error_type, 'code': code}
    return {**error, 'type': error.get('type') or error_type}
