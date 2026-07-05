"""监控请求参数快照工具。"""

from __future__ import annotations

import json
from typing import Any, Iterable


_DEFAULT_EXCLUDED_KEYS = {"messages", "contents", "model"}
MONITOR_PARAM_EXCLUDED_KEYS = set(_DEFAULT_EXCLUDED_KEYS)
_SENSITIVE_EXACT_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "password",
    "passwd",
    "secret",
    "cookie",
}
_SENSITIVE_SUFFIXES = ("_api_key", "_secret", "_password", "_token")


def _is_sensitive_key(key: Any) -> bool:
    key_text = str(key).lower()
    if key_text in _SENSITIVE_EXACT_KEYS:
        return True
    if key_text.endswith(_SENSITIVE_SUFFIXES):
        return True
    return key_text in {"access_token", "refresh_token", "id_token"}


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if _is_sensitive_key(key):
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = _sanitize_value(item)
        return sanitized

    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]

    return value


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def build_monitor_request_params(
    request_body: dict | None,
    *,
    extra: dict | None = None,
    exclude_keys: Iterable[str] | None = None,
) -> dict:
    """
    构建监控面板用的请求参数快照。

    默认排除 messages / contents / model：消息正文已经通过 request_messages 单独记录，
    model 已在监控记录顶层记录；其余请求体字段（tools、tool_choice、thinkingConfig、
    response_format、stream_options、自定义参数等）都会保留。
    """
    excluded = set(MONITOR_PARAM_EXCLUDED_KEYS)
    if exclude_keys:
        excluded.update(exclude_keys)

    params = {}
    if isinstance(request_body, dict):
        for key, value in request_body.items():
            if key in excluded:
                continue
            params[key] = _json_safe(_sanitize_value(value))

    if "stream" in params and "streaming" not in params:
        params["streaming"] = bool(params.get("stream"))

    if extra:
        for key, value in extra.items():
            params[key] = _json_safe(_sanitize_value(value))

    return params
