"""Vertex 的 Google OAuth：凭据换令牌、缓存和刷新，以及各入口实际发给上游的鉴权头。"""
import asyncio
import datetime
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import _helpers
from google.oauth2 import credentials as user_credentials, service_account
import pytest

from core import google_oauth
from core.google_oauth import GoogleOAuthError, google_oauth_headers
from core.upstream_auth import apply_upstream_auth
from protocol_harness import MODEL, Turn, build_gateway, exchange, interactions_reply, read_chat


def _service_account():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return {'type': 'service_account', 'project_id': 'demo-project', 'private_key_id': 'k1', 'private_key': pem,
            'client_email': 'bridge@demo-project.iam.gserviceaccount.com', 'client_id': '1',
            'token_uri': 'https://oauth2.googleapis.com/token'}


@pytest.fixture
def refreshes(monkeypatch):
    """不连 Google：刷新时发一个新令牌，一小时后过期。"""
    calls = []

    def refresh(self, request):
        calls.append(type(self).__module__)
        self.token = f'token-{len(calls)}'
        self.expiry = _helpers.utcnow() + datetime.timedelta(hours=1)

    monkeypatch.setattr(google_oauth, '_credentials', {})
    monkeypatch.setattr(google_oauth, '_locks', {})
    monkeypatch.setattr(service_account.Credentials, 'refresh', refresh)
    monkeypatch.setattr(user_credentials.Credentials, 'refresh', refresh)
    return calls


@pytest.fixture
def credential_file(tmp_path):
    path = tmp_path / 'vertex-sa.json'
    path.write_text(json.dumps(_service_account()), encoding='utf-8')
    return str(path)


def test_file_and_inline_json_credentials_get_a_bearer_token(refreshes, credential_file):
    inline = json.dumps(_service_account())
    for credential in (credential_file, inline):
        headers = asyncio.run(google_oauth_headers(credential))
        assert headers == {'authorization': f'Bearer token-{len(refreshes)}'}
    assert len(refreshes) == 2


def test_adc_user_credentials_carry_the_quota_project(refreshes, tmp_path):
    path = tmp_path / 'application_default_credentials.json'
    path.write_text(json.dumps({'type': 'authorized_user', 'client_id': 'c', 'client_secret': 's',
                                'refresh_token': 'r', 'quota_project_id': 'billing-project'}), encoding='utf-8')
    assert asyncio.run(google_oauth_headers(str(path))) == {
        'authorization': 'Bearer token-1', 'x-goog-user-project': 'billing-project'}


def test_token_is_cached_refreshed_once_under_concurrency_and_renewed_near_expiry(refreshes, credential_file):
    async def burst():
        return await asyncio.gather(*(google_oauth_headers(credential_file) for _ in range(5)))

    assert {headers['authorization'] for headers in asyncio.run(burst())} == {'Bearer token-1'}
    assert len(refreshes) == 1
    # 离过期不到刷新阈值时换新令牌
    google_oauth._credentials[credential_file].expiry = _helpers.utcnow() + datetime.timedelta(seconds=30)
    assert asyncio.run(google_oauth_headers(credential_file)) == {'authorization': 'Bearer token-2'}


def test_oauth_mode_replaces_every_other_auth_header(refreshes, credential_file):
    headers = {'Authorization': f'Bearer {credential_file}', 'x-goog-api-key': credential_file, 'Content-Type': 'x'}
    asyncio.run(apply_upstream_auth(headers, credential_file, 'google-oauth'))
    assert headers == {'Content-Type': 'x', 'authorization': 'Bearer token-1'}
    plain = {}
    asyncio.run(apply_upstream_auth(plain, 'AIza-key', 'x-goog-api-key'))
    assert plain == {'x-goog-api-key': 'AIza-key'}


def test_unreadable_credential_is_a_configuration_error(refreshes, tmp_path):
    with pytest.raises(GoogleOAuthError, match='凭据无法读取'):
        asyncio.run(google_oauth_headers(str(tmp_path / 'missing.json')))
    with pytest.raises(GoogleOAuthError):
        asyncio.run(google_oauth_headers('{"type": "service_account"}'))
    with pytest.raises(GoogleOAuthError, match='不支持'):
        asyncio.run(google_oauth_headers('{"type": "external_account"}'))


@pytest.fixture
def gateway(monkeypatch):
    app, _ = build_gateway(monkeypatch)
    return app, monkeypatch


def _oauth(credential):
    return {'api_key': credential, 'api_key_auth_mode': 'google-oauth'}


@pytest.mark.parametrize('stream', (False, True))
def test_converted_and_native_interactions_send_the_token_not_the_credential(gateway, refreshes, credential_file, stream):
    app, monkeypatch = gateway
    reply = interactions_reply(Turn(text='答'), stream)
    chat = {'model': MODEL, 'stream': stream, 'messages': [{'role': 'user', 'content': 'q'}]}
    native = {'model': MODEL, 'stream': stream, 'input': 'q'}
    for path, body in (('/v1/chat/completions', chat), ('/v1beta/interactions', native)):
        response, fake = exchange(app, monkeypatch, 'interactions', [reply], path, body, _oauth(credential_file))
        assert response.status_code == 200, response.text
        headers = {name.lower(): value for name, value in fake.requests[0]['headers'].items()}
        assert headers['authorization'] == 'Bearer token-1'
        assert 'x-goog-api-key' not in headers
        assert credential_file not in json.dumps(fake.requests[0])
    assert read_chat(exchange(app, monkeypatch, 'interactions', [reply], '/v1/chat/completions', chat,
                              _oauth(credential_file))[0], stream).text == '答'


@pytest.mark.parametrize('path', ('/v1/chat/completions', '/v1beta/interactions'))
def test_bad_credential_reaches_the_client_as_502_without_calling_upstream(gateway, refreshes, tmp_path, path):
    """网关自己的上游凭据坏了不是客户端的错，按 502 返回，客户端不会误以为自己的 Key 失效。"""
    app, monkeypatch = gateway
    body = {'model': MODEL, 'messages': [{'role': 'user', 'content': 'q'}], 'input': 'q'}
    response, fake = exchange(app, monkeypatch, 'interactions', [interactions_reply(Turn(text='答'), False)], path, body,
                              _oauth(str(tmp_path / 'missing.json')))
    assert response.status_code == 502
    assert '凭据无法读取' in response.text
    assert fake.requests == []
