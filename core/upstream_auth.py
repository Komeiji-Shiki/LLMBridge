"""按模型配置选择上游 API Key 的鉴权请求头。"""
from core.google_oauth import google_oauth_headers
from core.request_context import current_request

GOOGLE_OAUTH = 'google-oauth'
_HEADER_MODES = ('bearer', 'api-key', 'x-api-key', 'x-goog-api-key')
_AUTH_HEADERS = ('authorization', 'api-key', 'x-api-key', 'x-goog-api-key')


def _resolve_mode(mode: str | None) -> str | None:
    if mode is None:
        context = current_request.get()
        mode = context.endpoint.get('api_key_auth_mode') if context else None
    return mode


def _drop_auth_headers(headers: dict) -> None:
    for name in tuple(headers):
        if name.lower() in _AUTH_HEADERS:
            del headers[name]


def apply_api_key_auth(headers: dict, api_key: str, mode: str | None = None) -> None:
    """自动模式保留协议原有行为，显式选择时只发送一种鉴权头。"""
    mode = _resolve_mode(mode)
    if mode not in _HEADER_MODES:
        return
    _drop_auth_headers(headers)
    if api_key:
        name = 'Authorization' if mode == 'bearer' else mode
        headers[name] = f'Bearer {api_key}' if mode == 'bearer' else api_key


async def apply_upstream_auth(headers: dict, api_key: str, mode: str | None = None) -> None:
    """同 apply_api_key_auth；google-oauth 模式下 api_key 是 Google 凭据，先换成访问令牌。"""
    mode = _resolve_mode(mode)
    if mode != GOOGLE_OAUTH:
        apply_api_key_auth(headers, api_key, mode)
        return
    _drop_auth_headers(headers)
    if api_key:
        headers.update(await google_oauth_headers(api_key))
