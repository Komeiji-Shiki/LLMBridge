"""用 Google 凭据换取 OAuth 访问令牌，供 Vertex AI 的项目接口使用。

凭据填在模型配置的 API Key 里，可以是凭据文件路径，也可以是单行 JSON。支持服务账号
密钥（service_account）和 gcloud auth application-default login 生成的 ADC 文件
（authorized_user），后者设置了配额项目时会带上 x-goog-user-project。

令牌按凭据缓存，快过期时由 google-auth 刷新；同一凭据的并发请求只刷新一次。
刷新走 requests，和上游请求一样使用环境变量里的代理。
"""
import asyncio
import json

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2 import credentials as user_credentials, service_account

SCOPES = ('https://www.googleapis.com/auth/cloud-platform',)

_credentials = {}
_locks = {}


class GoogleOAuthError(Exception):
    """凭据读不了或换不到令牌，属于配置问题，不是上游故障。"""


def _load(credential: str):
    text = credential.strip()
    try:
        if text.startswith('{'):
            info = json.loads(text)
        else:
            with open(text, encoding='utf-8') as handle:
                info = json.load(handle)
        kind = info.get('type') if isinstance(info, dict) else None
        # 只认这两种类型，按类型专用的方法读取，不接受任意的外部凭据配置
        if kind == 'service_account':
            return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        if kind == 'authorized_user':
            return user_credentials.Credentials.from_authorized_user_info(info, scopes=SCOPES)
    except (ValueError, OSError, GoogleAuthError) as error:
        raise GoogleOAuthError(f'Google OAuth 凭据无法读取: {error}') from error
    raise GoogleOAuthError(f'Google OAuth 凭据类型 {kind!r} 不支持，只支持 service_account 和 authorized_user')


async def google_oauth_headers(credential: str) -> dict:
    """返回带访问令牌的鉴权头；凭据设置了配额项目时同时带上 x-goog-user-project。"""
    loaded = _credentials.get(credential)
    if loaded is None or not loaded.valid:
        async with _locks.setdefault(credential, asyncio.Lock()):
            loaded = _credentials.get(credential)
            if loaded is None:
                loaded = _credentials[credential] = await asyncio.to_thread(_load, credential)
            if not loaded.valid:
                try:
                    await asyncio.to_thread(loaded.refresh, Request())
                except GoogleAuthError as error:
                    raise GoogleOAuthError(f'Google OAuth 换取访问令牌失败: {error}') from error
    headers = {}
    loaded.apply(headers)
    return headers
