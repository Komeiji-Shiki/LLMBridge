"""
Routes package for LMArena Bridge
包含所有API路由端点

模块结构（每个模块提供自己的 APIRouter，由主入口 include_router 统一挂载）：
- api_routes.py: 核心API入口（/v1/chat/completions、/v1/messages、Gemini 原生端点）
- models_api.py: 模型列表API（/v1/models、/v1beta/models，含 API Key 鉴权）
- gemini_v1beta_api.py: Gemini v1beta 原生API处理逻辑
- direct_api_handler.py: Direct API 处理逻辑
- lmarena_handler.py: LMArena 浏览器模式处理（仍在使用，由 chat_completions 按模型类型分发）
- websocket_routes.py: 油猴脚本 WebSocket 端点（/ws）
- internal_routes.py: 内部通信端点（ID 捕获、/update 历史别名）
- monitor_routes.py: 监控面板端点（/monitor、/api/monitor/*）
- admin_routes.py: 管理面板端点（/admin、/api/admin/*）
- auth_routes.py: Web 登录与访问密钥验证（/login、/auth/*）
- apikey_routes.py: API Key 管理（/api/admin/api_keys*）
"""

# 核心API处理函数（保持向后兼容的函数级导出）
from .api_routes import (
    get_models,
    get_gemini_models,
    gemini_native_api,
    chat_completions
)

# 处理器模块
from .direct_api_handler import (
    handle_direct_api_request,
    handle_gemini_native_direct,
    handle_passthrough_direct
)

from .lmarena_handler import handle_lmarena_request

# 路由模块（各自携带 APIRouter）
from . import api_routes
from . import responses_api
from . import models_api
from . import websocket_routes
from . import internal_routes
from . import monitor_routes
from . import admin_routes
from . import auth_routes
from . import apikey_routes

# 导出所有
__all__ = [
    # 核心API
    'get_models',
    'get_gemini_models',
    'gemini_native_api',
    'chat_completions',
    # Direct API处理
    'handle_direct_api_request',
    'handle_gemini_native_direct',
    'handle_passthrough_direct',
    # LMArena处理
    'handle_lmarena_request',
    # 路由模块
    'api_routes',
    'models_api',
    'websocket_routes',
    'internal_routes',
    'monitor_routes',
    'admin_routes',
    'auth_routes',
    'apikey_routes',
]
