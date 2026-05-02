"""
统一错误处理模块
确保整个项目使用一致的错误响应格式
"""

import logging
from typing import Optional, Dict, Any, Union
from fastapi import HTTPException
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class APIError(Exception):
    """
    统一的API错误基类
    
    使用这个类代替直接抛出 HTTPException 或返回 JSONResponse，
    确保错误格式一致。
    """
    
    def __init__(
        self,
        message: str,
        error_type: str = "api_error",
        status_code: int = 500,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        self.message = message
        self.error_type = error_type
        self.status_code = status_code
        self.code = code or error_type
        self.details = details or {}
        super().__init__(self.message)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为OpenAI兼容的错误字典"""
        error_body = {
            "message": self.message,
            "type": self.error_type,
            "code": self.code
        }
        if self.details:
            error_body["details"] = self.details
        return {"error": error_body}
    
    def to_response(self) -> JSONResponse:
        """转换为 JSONResponse"""
        return JSONResponse(
            status_code=self.status_code,
            content=self.to_dict()
        )
    
    def to_http_exception(self) -> HTTPException:
        """转换为 HTTPException"""
        return HTTPException(
            status_code=self.status_code,
            detail=self.message
        )


# 具体错误类型

class BadRequestError(APIError):
    """400 Bad Request - 请求参数错误"""
    def __init__(self, message: str, code: str = "invalid_request", details: Dict[str, Any] = None):
        super().__init__(
            message=message,
            error_type="invalid_request_error",
            status_code=400,
            code=code,
            details=details
        )


class AuthenticationError(APIError):
    """401 Unauthorized - 认证失败"""
    def __init__(self, message: str = "API key is invalid or missing", code: str = "invalid_api_key"):
        super().__init__(
            message=message,
            error_type="authentication_error",
            status_code=401,
            code=code
        )


class PermissionError(APIError):
    """403 Forbidden - 权限不足"""
    def __init__(self, message: str = "Permission denied", code: str = "permission_denied"):
        super().__init__(
            message=message,
            error_type="permission_error",
            status_code=403,
            code=code
        )


class NotFoundError(APIError):
    """404 Not Found - 资源不存在"""
    def __init__(self, message: str = "Resource not found", code: str = "not_found"):
        super().__init__(
            message=message,
            error_type="not_found_error",
            status_code=404,
            code=code
        )


class RateLimitError(APIError):
    """429 Too Many Requests - 请求过于频繁"""
    def __init__(self, message: str = "Rate limit exceeded", code: str = "rate_limit_exceeded"):
        super().__init__(
            message=message,
            error_type="rate_limit_error",
            status_code=429,
            code=code
        )


class ServiceUnavailableError(APIError):
    """503 Service Unavailable - 服务不可用"""
    def __init__(self, message: str, code: str = "service_unavailable"):
        super().__init__(
            message=message,
            error_type="service_unavailable_error",
            status_code=503,
            code=code
        )


class GatewayTimeoutError(APIError):
    """504 Gateway Timeout - 网关超时"""
    def __init__(self, message: str = "Gateway timeout", code: str = "gateway_timeout"):
        super().__init__(
            message=message,
            error_type="gateway_timeout_error",
            status_code=504,
            code=code
        )


class InternalServerError(APIError):
    """500 Internal Server Error - 内部服务器错误"""
    def __init__(self, message: str, code: str = "internal_error", details: Dict[str, Any] = None):
        super().__init__(
            message=message,
            error_type="internal_server_error",
            status_code=500,
            code=code,
            details=details
        )


class BridgeError(APIError):
    """LMArena Bridge 特定错误"""
    def __init__(self, message: str, code: str = "bridge_error", status_code: int = 500):
        super().__init__(
            message=f"[LMArena Bridge Error] {message}",
            error_type="bridge_error",
            status_code=status_code,
            code=code
        )


class AttachmentError(BridgeError):
    """附件处理错误"""
    def __init__(self, message: str):
        super().__init__(
            message=f"附件处理失败: {message}",
            code="attachment_error",
            status_code=500
        )


class AttachmentTooLargeError(BridgeError):
    """附件过大错误 (413)"""
    def __init__(self, message: str = "附件大小超过了 LMArena 服务器的限制"):
        super().__init__(
            message=message,
            code="attachment_too_large",
            status_code=413
        )


class VerificationRequiredError(ServiceUnavailableError):
    """需要人机验证"""
    def __init__(self, remaining_seconds: int = 0):
        if remaining_seconds > 0:
            message = f"正在等待人机验证冷却完成...（剩余 {remaining_seconds} 秒）"
        else:
            message = "正在等待人机验证完成..."
        super().__init__(message=message, code="verification_required")


class BrowserNotConnectedError(ServiceUnavailableError):
    """浏览器未连接"""
    def __init__(self):
        super().__init__(
            message="油猴脚本客户端未连接。请确保 LMArena 页面已打开并激活脚本。",
            code="browser_not_connected"
        )


class ModelNotFoundError(BadRequestError):
    """模型未找到"""
    def __init__(self, model_name: str):
        super().__init__(
            message=f"模型 '{model_name}' 没有配置独立的会话ID。",
            code="model_not_found"
        )


class InvalidSessionError(BadRequestError):
    """无效的会话ID"""
    def __init__(self):
        super().__init__(
            message="最终确定的 Session ID 无效。",
            code="invalid_session"
        )


# 错误处理辅助函数

def handle_api_error(error: Union[APIError, Exception]) -> JSONResponse:
    """
    统一处理API错误，返回JSONResponse
    
    Args:
        error: APIError 或其他异常
    
    Returns:
        JSONResponse
    """
    if isinstance(error, APIError):
        logger.error(f"[API_ERROR] {error.error_type}: {error.message}")
        return error.to_response()
    else:
        logger.error(f"[UNEXPECTED_ERROR] {type(error).__name__}: {str(error)}", exc_info=True)
        return InternalServerError(message=str(error)).to_response()


def format_upstream_error(
    error_response: Dict[str, Any],
    default_status: int = 500
) -> JSONResponse:
    """
    格式化上游API返回的错误
    
    Args:
        error_response: 上游API返回的错误响应
        default_status: 默认状态码
    
    Returns:
        JSONResponse
    """
    error_details = error_response.get('error', {})
    
    # 处理不同的错误格式
    if isinstance(error_details, dict):
        error_type = error_details.get('type', 'api_error')
        error_message = error_details.get('message', str(error_details))
    elif isinstance(error_details, str):
        error_type = 'api_error'
        error_message = error_details
    else:
        error_type = 'api_error'
        error_message = str(error_response)
    
    # 根据错误类型映射HTTP状态码
    status_code = default_status
    if error_type == 'invalid_request_error':
        status_code = 400
    elif error_type == 'authentication_error':
        status_code = 401
    elif error_type == 'permission_error':
        status_code = 403
    elif error_type == 'not_found_error':
        status_code = 404
    elif error_type == 'rate_limit_error':
        status_code = 429
    
    # 也检查非标准格式的错误码
    if 'code' in error_response:
        code = error_response.get('code')
        if code == 401:
            status_code = 401
        elif code == 404:
            status_code = 404
        elif code == 400:
            status_code = 400
    
    logger.error(f"[UPSTREAM_ERROR] {status_code} - {error_message}")
    
    return JSONResponse(
        status_code=status_code,
        content=error_response  # 透传原始错误响应
    )