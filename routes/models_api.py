"""
模型列表API路由
提供 /v1/models 和 /v1beta/models 端点（含 API Key 鉴权与扫描机器人拦截）
"""
import logging
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.api_key_manager import api_key_manager
from core.config_loader import CONFIG, MODEL_ENDPOINT_MAP, MODEL_NAME_TO_ID_MAP

logger = logging.getLogger(__name__)
router = APIRouter(tags=["models"])

# 已知扫描/监控机器人 User-Agent 黑名单（不区分大小写）
_BLOCKED_USER_AGENTS = (
    "lmspeedbot",
    "go-http-client",
)

_INVALID_KEY_RESPONSE_CONTENT = {
    "error": {
        "message": "Incorrect API key provided. You can find your API key at https://platform.openai.com/account/api-keys.",
        "type": "invalid_request_error",
        "param": None,
        "code": "invalid_api_key"
    }
}


def _is_blocked_user_agent(request: Request) -> bool:
    """检查请求是否来自已知扫描机器人"""
    ua = request.headers.get("user-agent", "").lower()
    return any(blocked in ua for blocked in _BLOCKED_USER_AGENTS)


def _matches_global_key(provided_key: str, global_api_key: str) -> bool:
    """常数时间比较全局管理员 Key。

    🔧 旧版这两个端点用裸 `==`，逐字节短路比较会把"前缀猜对了几位"
    通过响应时间泄漏出去；_validate_request_api_key 早已改用
    compare_digest，这里补齐一致性。
    """
    if not global_api_key or not provided_key:
        return False
    return secrets.compare_digest(str(provided_key), str(global_api_key))


def _invalid_key_response() -> JSONResponse:
    """构造 401 无效 Key 响应"""
    return JSONResponse(
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
        content=_INVALID_KEY_RESPONSE_CONTENT
    )


def _extract_provided_key(request: Request, allow_x_api_key: bool = True) -> str | None:
    """从 Authorization / x-api-key 头中提取客户端提交的 API Key"""
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.split(" ", 1)[1]

    # Anthropic/Claude 客户端常用 x-api-key；与 Bearer 等效
    if allow_x_api_key:
        x_api_key = request.headers.get("x-api-key")
        if x_api_key:
            return x_api_key.strip()
    return None


def _is_archived(config) -> bool:
    """检查模型配置是否标记为已归档"""
    if isinstance(config, dict):
        return config.get("archived", False)
    if isinstance(config, list) and config:
        # 列表配置取第一个元素判断
        first = config[0] if isinstance(config[0], dict) else {}
        return first.get("archived", False)
    return False


async def get_models(MODEL_ENDPOINT_MAP: dict, MODEL_NAME_TO_ID_MAP: dict, allowed_models: list = None):
    """
    提供兼容 OpenAI 的模型列表 - 返回 model_endpoint_map.json 中配置的模型。
    已归档（archived: true）的模型不会出现在列表中，但统计数据仍保留。

    Args:
        MODEL_ENDPOINT_MAP: 模型端点映射字典
        MODEL_NAME_TO_ID_MAP: 模型名称到ID的映射字典
        allowed_models: 允许的模型白名单（None 或空列表表示不过滤）

    Returns:
        OpenAI 格式的模型列表响应
    """
    def _is_allowed(model_name: str) -> bool:
        """检查模型是否在白名单中（空白名单 = 允许所有）"""
        if not allowed_models:
            return True
        return model_name in allowed_models

    # 优先返回 MODEL_ENDPOINT_MAP 中的模型（已配置会话的模型）
    if MODEL_ENDPOINT_MAP:
        return {
            "object": "list",
            "data": [
                {
                    "id": model_name,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "LMArenaBridge"
                }
                for model_name, config in MODEL_ENDPOINT_MAP.items()
                if not _is_archived(config) and _is_allowed(model_name)
            ],
        }
    # 如果 MODEL_ENDPOINT_MAP 为空，则返回 models.json 中的模型作为备用
    elif MODEL_NAME_TO_ID_MAP:
        return {
            "object": "list",
            "data": [
                {
                    "id": model_name,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "LMArenaBridge"
                }
                for model_name in MODEL_NAME_TO_ID_MAP.keys()
                if _is_allowed(model_name)
            ],
        }
    else:
        return JSONResponse(
            status_code=404,
            content={"error": "模型列表为空。请配置 'model_endpoint_map.json' 或 'models.json'。"}
        )


async def get_gemini_models(MODEL_ENDPOINT_MAP: dict):
    """
    提供Gemini v1beta格式的模型列表

    Args:
        MODEL_ENDPOINT_MAP: 模型端点映射字典

    Returns:
        Gemini v1beta 格式的模型列表响应
    """
    # 只返回配置了gemini_native类型的模型
    gemini_models = []

    if MODEL_ENDPOINT_MAP:
        for model_name, config in MODEL_ENDPOINT_MAP.items():
            # 处理单个配置和配置列表
            configs_to_check = [config] if isinstance(config, dict) else config if isinstance(config, list) else []

            for cfg in configs_to_check:
                if isinstance(cfg, dict) and cfg.get("api_type") == "gemini_native":
                    # 使用配置键 model_name 作为模型名称（避免 model_id ≠ 配置键时调用 404）
                    model_id = cfg.get("model_id", model_name)
                    display_name = cfg.get("display_name", model_id)

                    gemini_models.append({
                        "name": f"models/{model_name}",
                        "displayName": display_name,
                        "description": f"Gemini model: {display_name}",
                        "supportedGenerationMethods": [
                            "generateContent",
                            "streamGenerateContent"
                        ]
                    })
                    break  # 只添加一次

    logger.info(f"[GEMINI_V1BETA] 返回 {len(gemini_models)} 个Gemini原生模型")

    return {
        "models": gemini_models
    }


# ============================================================================
# 端点注册（鉴权 + 业务函数调用）
# ============================================================================

@router.get("/v1/models")
async def get_models_endpoint(request: Request):
    """提供兼容 OpenAI 的模型列表（根据 API Key 权限过滤）"""
    # 直接拒绝已知扫描机器人，返回空列表，不产生 401 日志噪音
    if _is_blocked_user_agent(request):
        return {"object": "list", "data": []}

    provided_key = _extract_provided_key(request)

    global_api_key = CONFIG.get("api_key")
    has_guest_keys = api_key_manager.has_keys()

    # 只要配置了任何一种认证，就必须提供有效 key 才能获取模型列表，防止模型名泄露
    if global_api_key or has_guest_keys:
        if not provided_key:
            user_agent = request.headers.get("user-agent", "unknown")
            logger.warning(f"[401-/v1/models] 未提供API Key | 来源: {request.client.host if request.client else '?'} | User-Agent: {user_agent}")
            return _invalid_key_response()

        # 管理员 key 通过，返回所有模型
        if _matches_global_key(provided_key, global_api_key):
            allowed_models = None
        elif has_guest_keys:
            allowed_models = api_key_manager.get_allowed_models(provided_key)
            if allowed_models is None:
                return _invalid_key_response()
            if len(allowed_models) == 0:
                allowed_models = None
        else:
            # 提供了 key 但不匹配，且没有访客 key 系统
            return _invalid_key_response()
    else:
        allowed_models = None

    return await get_models(MODEL_ENDPOINT_MAP, MODEL_NAME_TO_ID_MAP, allowed_models)


@router.get("/v1beta/models")
async def get_gemini_models_endpoint(request: Request):
    """提供Gemini v1beta格式的模型列表"""
    # 直接拒绝已知扫描机器人，返回空列表，不产生 401 日志噪音
    if _is_blocked_user_agent(request):
        return {"models": []}

    provided_key = _extract_provided_key(request, allow_x_api_key=False)

    global_api_key = CONFIG.get("api_key")
    has_guest_keys = api_key_manager.has_keys()

    # 只要配置了任何一种认证，就必须提供有效 key 才能获取模型列表，防止模型名泄露
    if global_api_key or has_guest_keys:
        if not provided_key:
            return _invalid_key_response()

        is_valid = False
        if _matches_global_key(provided_key, global_api_key):
            is_valid = True
        elif has_guest_keys:
            if api_key_manager.get_allowed_models(provided_key) is not None:
                is_valid = True

        if not is_valid:
            return _invalid_key_response()

    return await get_gemini_models(MODEL_ENDPOINT_MAP)
