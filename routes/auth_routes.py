"""
Web 登录与访问验证路由
/login、/auth/verify、/auth/check
（配合 core.middleware.WebAccessKeyMiddleware 与 core.web_session 使用）

安全设计：
- 验证成功后由服务端签发随机 session token，通过 HttpOnly cookie 下发，
  浏览器 JS 无法读取，cookie 中不再出现明文密钥。
- 失败限流与 middleware 层共享（core.web_session），反代部署时可通过
  config.jsonc 的 trusted_proxies 配置解析真实客户端 IP。
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from core import web_session
from core.config_loader import CONFIG

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
async def login_page():
    """返回登录页面（HTML 已外置到 login.html，next 参数由页面 JS 读取并校验）"""
    try:
        with open('login.html', 'r', encoding='utf-8') as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>登录页面未找到</h1><p>请确保 login.html 文件在服务器根目录。</p>",
            status_code=404
        )


@router.post("/auth/verify")
async def verify_web_key(request: Request):
    """验证Web访问密钥；成功后签发 HttpOnly 会话 cookie"""
    client_ip = web_session.client_ip_from_request(request)
    if web_session.is_rate_limited(client_ip):
        logger.warning(f"[AUTH] /auth/verify 触发限流: {client_ip}")
        return JSONResponse(
            status_code=429,
            content={"success": False, "message": "尝试次数过多，请稍后再试"}
        )

    try:
        data = await request.json()
    except Exception:
        # 🔧 修复：不再把内部异常细节（str(e)）返回给未认证客户端
        logger.warning(f"[AUTH] /auth/verify 请求体解析失败: {client_ip}")
        web_session.record_failure(client_ip)
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "请求格式错误"}
        )

    submitted_key = str(data.get("key") or "") if isinstance(data, dict) else ""
    web_key = CONFIG.get("web_access_key", "")

    if not web_key:
        return {"success": True, "message": "未配置密钥，无需验证"}

    if web_session.keys_equal(submitted_key, web_key):
        # 验证通过：清掉此前的失败计数，避免手误几次后即便输对也仍被限流拦
        web_session.clear_failures(client_ip)
        token = web_session.create_session()
        response = JSONResponse(content={"success": True, "message": "验证成功"})
        # 🔧 安全修复：根据 x-forwarded-proto 自适应设置 secure 标志
        # 反代部署（nginx/caddy）通过 HTTPS 访问时，反代用 HTTP 连接后端，
        # 但客户端实际使用 HTTPS——若不加 secure，cookie 在 HTTP 链路上明文泄露。
        forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
        is_https = forwarded_proto == "https" or request.url.scheme == "https"
        response.set_cookie(
            key=web_session.SESSION_COOKIE_NAME,
            value=token,
            max_age=web_session.SESSION_TTL_SECONDS,
            path="/",
            httponly=True,
            samesite="lax",
            secure=is_https,
        )
        return response

    web_session.record_failure(client_ip)
    return {"success": False, "message": "密钥错误"}


@router.get("/auth/check")
async def check_web_auth(request: Request):
    """检查当前是否已通过Web验证"""
    web_key = CONFIG.get("web_access_key", "")

    if not web_key:
        return {"authenticated": True, "reason": "no_key_configured"}

    session_token = request.cookies.get(web_session.SESSION_COOKIE_NAME, "")
    if web_session.validate_session(session_token):
        return {"authenticated": True, "reason": "valid_session"}
    return {"authenticated": False, "reason": "invalid_or_missing_session"}
