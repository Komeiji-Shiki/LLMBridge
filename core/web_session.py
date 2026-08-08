"""
Web 管理界面会话与防爆破模块

- 服务端签发随机 session token，浏览器通过 HttpOnly cookie 持有，
  杜绝明文密钥进 cookie（XSS 能偷到的最多是可过期的会话，而非密钥本体）。
- 失败计数限流由 /auth/verify 与 WebAccessKeyMiddleware 共用，
  绕过登录页直接对 /api/admin/* 爆破 x-web-access-key 头同样会被计数拦截。
- 反向代理感知：配置 trusted_proxies 后按 X-Forwarded-For 解析真实客户端 IP，
  避免所有请求被记到反代 IP 上互相误伤。
- Session 持久化到 web_session_state.json，服务器重启后登录状态保留。
"""
import hmac
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Dict

from core.config_loader import CONFIG

# ==================== 会话管理 ====================

SESSION_COOKIE_NAME = "web_session"
SESSION_TTL_SECONDS = 86400  # 24 小时，与旧版 cookie max-age 一致
_MAX_SESSIONS = 1000

_SESSION_STATE_FILE = "web_session_state.json"
_sessions: Dict[str, float] = {}  # {token: 过期时间戳}
_session_lock = threading.Lock()
_session_dirty = False


def _load_sessions():
    """从文件恢复会话状态（启动时调用）"""
    global _sessions
    if not os.path.exists(_SESSION_STATE_FILE):
        return
    try:
        with open(_SESSION_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.loads(f.read())
        if isinstance(data, dict):
            now = time.time()
            # 清理已过期的 session
            _sessions = {t: exp for t, exp in data.items() if isinstance(exp, (int, float)) and exp > now}
            if _sessions:
                import logging
                logging.getLogger(__name__).info(f"[WEB_SESSION] 已恢复 {len(_sessions)} 个有效会话")
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"[WEB_SESSION] 加载会话状态失败: {e}")


def _save_sessions():
    """持久化会话状态到文件"""
    global _session_dirty
    try:
        tmp_path = _SESSION_STATE_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(_sessions, f, ensure_ascii=False)
        os.replace(tmp_path, _SESSION_STATE_FILE)
        _session_dirty = False
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"[WEB_SESSION] 保存会话状态失败: {e}")


def _save_sessions_async():
    """异步后台保存（不阻塞请求线程）"""
    if not _session_dirty:
        return
    threading.Thread(target=_save_sessions, daemon=True).start()


def create_session() -> str:
    """签发一个新的会话 token（持久化到文件，重启后仍有效）"""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _session_lock:
        # 顺带清理过期会话；仍超上限时淘汰最早过期的
        expired = [t for t, exp in _sessions.items() if exp <= now]
        for t in expired:
            del _sessions[t]
        while len(_sessions) >= _MAX_SESSIONS:
            oldest = min(_sessions, key=_sessions.__getitem__)
            del _sessions[oldest]
        _sessions[token] = now + SESSION_TTL_SECONDS
    global _session_dirty
    _session_dirty = True
    _save_sessions_async()
    return token


def validate_session(token: str) -> bool:
    """校验会话 token 是否有效（随机 128+ bit token，字典查找即安全）"""
    if not token:
        return False
    with _session_lock:
        exp = _sessions.get(token)
        if exp is None:
            return False
        if exp <= time.time():
            del _sessions[token]
            global _session_dirty
            _session_dirty = True
            _save_sessions_async()
            return False
        return True


def keys_equal(submitted: str, expected: str) -> bool:
    """恒定时间比较，避免时序侧信道探测密钥。"""
    return hmac.compare_digest(
        (submitted or "").encode("utf-8"),
        (expected or "").encode("utf-8"),
    )


# ==================== 失败限流（防暴力破解） ====================
# 每个 IP 在滑动窗口内最多允许 N 次失败尝试；成功验证不计入

_WINDOW_SECONDS = 60
_MAX_FAILURES = 10
_failures: "defaultdict[str, deque]" = defaultdict(deque)
_failure_lock = threading.Lock()


def is_rate_limited(client_ip: str) -> bool:
    """检查该 IP 是否已超过失败次数限制（顺带清理过期记录）。

    🔧 用 .get 而非 defaultdict 取值：纯查询不应创建空 deque，
    否则扫描器刷不同 IP（或伪造 XFF）时字典无限膨胀。
    """
    now = time.time()
    with _failure_lock:
        failures = _failures.get(client_ip)
        if failures is None:
            return False
        while failures and now - failures[0] > _WINDOW_SECONDS:
            failures.popleft()
        if not failures:
            del _failures[client_ip]
            return False
        return len(failures) >= _MAX_FAILURES


def clear_failures(client_ip: str) -> None:
    """验证成功后清除该 IP 的失败记录。

    否则用户手误输错几次、随后输对并拿到会话，这些失败记录仍会在窗口内
    残留：同一 IP 再走 x-web-access-key 头访问时会被直接 429，明明已经
    证明过身份却被自己刚才的手误挡住。
    """
    with _failure_lock:
        _failures.pop(client_ip, None)


def record_failure(client_ip: str) -> None:
    """记录一次验证失败。"""
    with _failure_lock:
        _failures[client_ip].append(time.time())
        # 防止字典无限膨胀：条目过多时清理已过期的 IP
        if len(_failures) > 1000:
            now = time.time()
            stale = [ip for ip, dq in _failures.items()
                     if not dq or now - dq[-1] > _WINDOW_SECONDS]
            for ip in stale:
                del _failures[ip]


# ==================== 客户端 IP 解析（反向代理感知） ====================

def _trusted_proxies() -> set:
    proxies = CONFIG.get("trusted_proxies", []) if CONFIG else []
    return {str(p).strip() for p in proxies if p}


def resolve_client_ip(direct_ip: str, forwarded_for: str) -> str:
    """结合 X-Forwarded-For 解析真实客户端 IP。

    仅当直连 IP 在 config.jsonc 的 trusted_proxies 列表中时才信任 XFF，
    取从右往左第一个不在可信列表中的地址（右侧条目由最近一跳代理附加，
    左侧条目可被客户端伪造）。未配置可信代理时始终使用直连 IP。
    """
    trusted = _trusted_proxies()
    if not trusted or direct_ip not in trusted:
        return direct_ip
    hops = [h.strip() for h in (forwarded_for or "").split(",") if h.strip()]
    for hop in reversed(hops):
        if hop not in trusted:
            return hop
    return direct_ip


def client_ip_from_scope(scope) -> str:
    """从 ASGI scope 中解析客户端 IP（反代感知）。"""
    client = scope.get("client")
    direct_ip = client[0] if client else "unknown"
    forwarded_for = ""
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-for":
            forwarded_for = value.decode("latin-1")
            break
    return resolve_client_ip(direct_ip, forwarded_for)


def client_ip_from_request(request) -> str:
    """从 FastAPI/Starlette Request 中解析客户端 IP（反代感知）。"""
    direct_ip = request.client.host if request.client else "unknown"
    return resolve_client_ip(direct_ip, request.headers.get("x-forwarded-for", ""))


# 模块加载时从文件恢复会话状态
_load_sessions()
