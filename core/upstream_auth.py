"""按模型配置选择上游 API Key 的鉴权请求头。"""
from core.request_context import current_request


def apply_api_key_auth(headers: dict, api_key: str, mode: str | None = None) -> None:
    """自动模式保留协议原有行为，显式选择时只发送一种鉴权头。"""
    if mode is None:
        context = current_request.get()
        mode = context.endpoint.get('api_key_auth_mode') if context else None
    if mode not in ('bearer', 'api-key', 'x-api-key', 'x-goog-api-key'):
        return

    for name in tuple(headers):
        if name.lower() in ('authorization', 'api-key', 'x-api-key', 'x-goog-api-key'):
            del headers[name]
    if api_key:
        name = 'Authorization' if mode == 'bearer' else mode
        headers[name] = f'Bearer {api_key}' if mode == 'bearer' else api_key
