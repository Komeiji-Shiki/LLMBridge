"""
ASGI 中间件集合
- SelectiveGZipMiddleware: 只对管理页面/静态资源启用 GZip，流式 API 零开销透传
- CachedStaticFiles: 带 Cache-Control 头的静态文件服务
- WebAccessKeyMiddleware: Web 管理界面访问验证（session cookie / 访问密钥头）
"""
import logging
from urllib.parse import unquote

from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware as _OriginalCORSMiddleware
from starlette.middleware.gzip import GZipMiddleware as _OriginalGZipMiddleware
from starlette.responses import RedirectResponse

from core import web_session
from core import config_loader
from core.config_loader import CONFIG

logger = logging.getLogger(__name__)


class SelectiveGZipMiddleware:
    """只对非流式路径启用 GZip 压缩的中间件。

    🔧 性能关键：原版 GZipMiddleware 会缓冲 SSE chunk 直到达到 minimum_size，
    导致流式响应每次攒 2-3 个 chunk 才 flush，造成"CPU不高但真卡流"。

    对 /v1/、/ws/ 等 API/WebSocket 路径直接透传，
    只对 /admin、/monitor、/js/、/css/ 等静态/管理页面启用 GZip。
    """

    # 跳过 GZip 的路径前缀（流式 API、WebSocket）
    # 🔧 修复：补上 /v1beta/（Gemini 原生 streamGenerateContent 的 SSE 端点），
    # 否则流式响应会被 GZipMiddleware 缓冲压缩，重现“CPU 不高但真卡流”
    SKIP_PREFIXES = ("/v1/", "/v1beta/", "/ws/", "/ws", "/internal/", "/auth/")

    def __init__(self, app, minimum_size: int = 500):
        self.app = app
        self.gzip_app = _OriginalGZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # 对 API/WebSocket 路径跳过 GZip，避免缓冲 SSE chunk
        if any(path.startswith(p) for p in self.SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return

        # 对管理页面/静态资源启用 GZip 压缩
        await self.gzip_app(scope, receive, send)


class SelectiveCORSMiddleware:
    """只对公开 API 路径启用 CORS 放行的中间件。

    🔧 安全修复：旧版对全站（含 /api/admin、/api/monitor）通配
    allow_origins=["*"]。未配置 web_access_key 的本机部署下，任意网页的 JS
    都能跨域读取管理接口的响应（模型配置、请求日志、统计中的对话预览）。

    现在只有公开 API 路径参与 CORS 放行：
    - /v1/、/v1beta/：OpenAI / Anthropic / Gemini 兼容端点（跨域客户端需要）
    - /internal/：油猴脚本从 lmarena.ai 页面上下文 fetch 的上报端点
    管理/监控路径不下发任何跨域许可头，浏览器同源策略会拦截跨域读取。
    """

    CORS_PREFIXES = ("/v1/", "/v1beta/", "/internal/")

    def __init__(self, app, **cors_options):
        self.app = app
        self.cors_app = _OriginalCORSMiddleware(app, **cors_options)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and any(
                scope.get("path", "").startswith(p) for p in self.CORS_PREFIXES):
            await self.cors_app(scope, receive, send)
            return
        await self.app(scope, receive, send)


class CachedStaticFiles(StaticFiles):
    """带 Cache-Control 头的静态文件服务（避免每次刷新都重传大文件）"""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            original_send = send

            async def send_with_cache(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    path = scope.get("path", "")
                    # 🔧 区分 vendor 与业务 JS 缓存策略：
                    # vendor 第三方库（/js/vendor/）文件名带版本 hash，可长缓存 + immutable；
                    # 业务 JS 频繁更新，使用 no-cache 避免浏览器缓存旧版本导致故障。
                    if "/js/vendor/" in path or "vendor" in path:
                        headers.append((b"cache-control", b"public, max-age=31536000, immutable"))
                    elif path.endswith(".js") or path.endswith(".mjs"):
                        headers.append((b"cache-control", b"no-cache"))
                    else:
                        # CSS / 图片 / 字体等静态资源：1 小时缓存
                        headers.append((b"cache-control", b"public, max-age=3600"))
                    message = {**message, "headers": headers}
                await original_send(message)

            await super().__call__(scope, receive, send_with_cache)
        else:
            await super().__call__(scope, receive, send)


class WebAccessKeyMiddleware:
    """Web界面访问验证中间件

    验证方式（按优先级）：
    1. HttpOnly 会话 cookie（web_session）：浏览器登录后由服务端签发，
       cookie 中不再存放明文密钥，XSS 无法窃取密钥本体。
    2. x-web-access-key 请求头：供程序化访问；验证失败计入与 /auth/verify
       共享的爆破限流，绕过登录页直接爆破该头同样会被 429 拦截。

    🔧 性能关键：使用纯 ASGI 实现而非 BaseHTTPMiddleware。
    BaseHTTPMiddleware 会在内部创建协程桥接队列，把 StreamingResponse 的每个 chunk
    都经过 put→事件循环调度→get 的流程，导致 SSE 流式响应严重卡顿（CPU 不高但延迟大）。
    纯 ASGI 中间件对不需要验证的路径实现零开销透传（直接 await self.app(scope, receive, send)）。
    """

    # 登录页 /login 与 /auth/* 本就不在保护列表内，不需要再单列排除项
    # （旧版的 EXCLUDED_PATHS 检查排在"不在 PROTECTED_PATHS 就直接放行"
    #  之后，条件恒为假，是纯死代码）
    # 🔧 安全修复：补上 /api/request 与 /api/logs。这两个前缀由 monitor_routes
    # 注册（/api/request/{id} 返回单条请求全量详情、/api/logs/download 直接
    # FileResponse 整个 requests.jsonl，内含完整对话明文），却不匹配原有任何
    # 保护前缀——未登录即可拖走全部提示词与回复。
    PROTECTED_PATHS = ("/admin", "/monitor", "/token_calculator", "/api/admin", "/api/monitor", "/api/request", "/api/logs", "/ws/monitor", "/internal")

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _path_matches(path: str, prefixes) -> bool:
        """按路径段精确匹配：/admin 匹配 /admin 与 /admin/x，不匹配 /administrator。

        🔧 旧版裸 startswith 会把 /administrator 之类误纳入匹配范围
        （保护面偏大无实害，但语义不精确），现在收紧为路径段边界匹配。
        """
        for p in prefixes:
            base = p.rstrip("/")
            if path == base or path.startswith(base + "/"):
                return True
        return False

    async def __call__(self, scope, receive, send):
        # 非 HTTP/WebSocket 请求直接透传（如 lifespan）
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")

        # 快速路径：绝大多数请求（如 /v1/chat/completions）不在保护列表中，零开销透传
        if not self._path_matches(path, self.PROTECTED_PATHS):
            await self.app(scope, receive, send)
            return

        # 需要验证：先确认配置可用，再检查密钥
        # 🔧 鉴权 fail-closed：首次启动配置解析失败时 CONFIG_LOADED 仍为 False，
        # 此时绝不能因「web_access_key 为空」而放行——那会等于把管理面板裸奔。
        # 注意必须用 config_loader.CONFIG_LOADED 按属性读取，不能 from import 快照，
        # 否则布尔值在重新赋值后不会同步（本仓库已踩过同类坑，见 api_server_new.py）。
        if not config_loader.CONFIG_LOADED:
            logger.error("[AUTH] 配置未成功加载，对受保护路径拒绝服务（fail-closed）")
            await self._reject_unavailable(scope, receive, send)
            return

        web_key = CONFIG.get("web_access_key", "")
        if not web_key:
            await self.app(scope, receive, send)
            return

        session_token, header_key = self._extract_credentials(scope)

        # 1. 会话 cookie 校验（浏览器正常路径）
        if session_token and web_session.validate_session(session_token):
            await self.app(scope, receive, send)
            return

        # 2. x-web-access-key 头校验（程序化访问路径），失败计入爆破限流
        if header_key:
            client_ip = web_session.client_ip_from_scope(scope)
            if web_session.is_rate_limited(client_ip):
                logger.warning(f"[AUTH] x-web-access-key 触发限流: {client_ip}")
                await self._reject(scope, receive, send, path, rate_limited=True)
                return
            if web_session.keys_equal(header_key, web_key):
                await self.app(scope, receive, send)
                return
            web_session.record_failure(client_ip)

        # ---- 验证失败 ----
        await self._reject(scope, receive, send, path)

    async def _reject_unavailable(self, scope, receive, send):
        """配置不可用时的拒绝响应（HTTP 503 或 WebSocket close）。"""
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1013, "reason": "服务配置不可用"})
            return
        response = JSONResponse(
            status_code=503,
            content={"error": "Service Unavailable", "message": "服务器配置未能正确加载，已拒绝服务（fail-closed）"}
        )
        await response(scope, receive, send)

    async def _reject(self, scope, receive, send, path: str, rate_limited: bool = False):
        """发送验证失败响应（HTTP 401/429/303 或 WebSocket close）"""
        if scope["type"] == "websocket":
            # WebSocket 握手阶段直接关闭；ASGI 服务器会将未 accept 的 close 映射为握手失败
            await send({
                "type": "websocket.close",
                "code": 1008,
                "reason": "需要Web访问验证"
            })
            return

        if rate_limited:
            response = JSONResponse(
                status_code=429,
                content={"error": "Too Many Requests", "message": "尝试次数过多，请稍后再试"}
            )
        elif path in ("/admin", "/monitor", "/token_calculator"):
            response = RedirectResponse(url=f"/login?next={path}", status_code=303)
        else:
            response = JSONResponse(
                status_code=401,
                content={"error": "Unauthorized", "message": "需要Web访问验证"}
            )
        await response(scope, receive, send)

    @staticmethod
    def _extract_credentials(scope):
        """从 ASGI scope 的 headers 中提取 (session_token, x-web-access-key)"""
        cookie_value = ""
        x_key_value = ""

        for name, value in scope.get("headers", []):
            if name == b"cookie":
                cookie_value = value.decode("latin-1")
            elif name == b"x-web-access-key":
                x_key_value = value.decode("latin-1")

        session_token = ""
        if cookie_value:
            prefix = f"{web_session.SESSION_COOKIE_NAME}="
            for part in cookie_value.split(";"):
                part = part.strip()
                if part.startswith(prefix):
                    session_token = unquote(part[len(prefix):])
                    break

        return session_token, (unquote(x_key_value) if x_key_value else "")
