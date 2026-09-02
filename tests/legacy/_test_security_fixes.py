# -*- coding: utf-8 -*-
"""安全与解析加固回归测试
覆盖：
1. check_error 行首定位 + raw_decode（不误杀正文、支持嵌套 error 对象）
2. core.web_session 会话签发/校验/限流/反代 IP 解析
3. WebAccessKeyMiddleware 凭证提取
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

# ==================== 1. check_error 加固 ====================
from services.stream_parsers import StreamPatternMatcher

m = StreamPatternMatcher()

# 行首的真实错误 JSON 应被检出
assert m.check_error('{"error": "rate limit exceeded"}') == "rate limit exceeded"
assert m.check_error('a0:"hello"\n{"error": "boom"}') == "boom"

# 嵌套 error 对象（旧版非贪婪正则会截断解析失败而漏报）
nested = '{"error": {"code": 429, "message": "slow down", "meta": {"retry": true}}}'
result = m.check_error(nested)
assert isinstance(result, dict) and result.get("code") == 429, f"嵌套error解析失败: {result}"

# 正文里出现 {"error" 字样（非行首）不应误杀
safe = 'a0:"the response is {\\"error\\": \\"fake\\"} ok"'
assert m.check_error(safe) is None, "正文中的error字样被误杀"
# 非行首的裸 JSON 也不触发（前面有其他字符）
assert m.check_error('xx{"error": "not at line start"}') is None

# 截断的 error JSON 不崩溃、返回 None（等待更多数据）
assert m.check_error('{"error": {"code": 42') is None
print("1. check_error 行首定位/嵌套/截断: OK")

# ==================== 2. web_session ====================
from core import web_session

# 会话签发与校验
token = web_session.create_session()
assert web_session.validate_session(token) is True
assert web_session.validate_session("nonexistent-token") is False
assert web_session.validate_session("") is False

# 过期会话被拒绝并清除
import time
with web_session._session_lock:
    web_session._sessions[token] = time.time() - 1
assert web_session.validate_session(token) is False
assert token not in web_session._sessions

# 恒定时间比较
assert web_session.keys_equal("abc", "abc") is True
assert web_session.keys_equal("abc", "abd") is False
assert web_session.keys_equal("", "") is True
assert web_session.keys_equal(None, "x") is False

# 失败限流：10 次失败后触发
ip = "192.0.2.99"
web_session._failures.pop(ip, None)
assert web_session.is_rate_limited(ip) is False
for _ in range(10):
    web_session.record_failure(ip)
assert web_session.is_rate_limited(ip) is True
web_session._failures.pop(ip, None)
print("2a. 会话签发/过期/限流: OK")

# 反代 IP 解析
from core.config_loader import CONFIG

_saved = CONFIG.get("trusted_proxies")
try:
    # 未配置可信代理：始终用直连 IP（XFF 可伪造）
    CONFIG.pop("trusted_proxies", None)
    assert web_session.resolve_client_ip("1.2.3.4", "6.6.6.6") == "1.2.3.4"

    # 配置可信代理：直连 IP 是代理时取 XFF 最右非可信项
    CONFIG["trusted_proxies"] = ["10.0.0.1", "10.0.0.2"]
    assert web_session.resolve_client_ip("10.0.0.1", "6.6.6.6, 7.7.7.7, 10.0.0.2") == "7.7.7.7"
    # 直连 IP 不是可信代理：忽略 XFF
    assert web_session.resolve_client_ip("8.8.8.8", "6.6.6.6") == "8.8.8.8"
    # XFF 为空：回退直连 IP
    assert web_session.resolve_client_ip("10.0.0.1", "") == "10.0.0.1"
finally:
    if _saved is None:
        CONFIG.pop("trusted_proxies", None)
    else:
        CONFIG["trusted_proxies"] = _saved
print("2b. 反代感知 IP 解析: OK")

# ==================== 3. middleware 凭证提取 ====================
from core.middleware import WebAccessKeyMiddleware

scope = {
    "headers": [
        (b"cookie", b"foo=bar; web_session=tok123; other=x"),
        (b"x-web-access-key", b"headerkey"),
    ]
}
session_token, header_key = WebAccessKeyMiddleware._extract_credentials(scope)
assert session_token == "tok123", session_token
assert header_key == "headerkey", header_key

# 无 cookie 时
scope2 = {"headers": [(b"x-web-access-key", b"only-header")]}
st2, hk2 = WebAccessKeyMiddleware._extract_credentials(scope2)
assert st2 == "" and hk2 == "only-header"

# 都没有
st3, hk3 = WebAccessKeyMiddleware._extract_credentials({"headers": []})
assert st3 == "" and hk3 == ""
print("3. middleware 凭证提取: OK")

print("\n全部安全加固测试通过 ✅")
