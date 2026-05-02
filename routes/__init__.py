"""
Routes package for LMArena Bridge
包含所有API路由端点

模块结构：
- api_routes.py: 核心API入口（chat_completions）
- models_api.py: 模型列表API（/v1/models, /v1beta/models）
- gemini_v1beta_api.py: Gemini v1beta原生API
- direct_api_handler.py: Direct API处理逻辑
- lmarena_handler.py: LMArena模式处理（已弃用，保留兼容）
- websocket_routes.py: WebSocket端点
- internal_routes.py: 内部通信端点
- monitor_routes.py: 监控面板端点
- admin_routes.py: 管理面板端点
"""

# 核心API路由
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

# 🔧 修复：添加缺失的路由模块导入
from . import websocket_routes
from . import internal_routes
from . import monitor_routes
from . import admin_routes

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
    # 🔧 修复：添加路由模块导出
    'websocket_routes',
    'internal_routes',
    'monitor_routes',
    'admin_routes',
]