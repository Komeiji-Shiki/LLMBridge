"""
日志配置模块
- 异步日志：把控制台日志输出移到后台线程，避免主事件循环被同步日志 IO 卡住
- 访问日志过滤：抑制监控轮询和恶意扫描产生的噪音
"""
import logging
import queue
from logging.handlers import QueueHandler, QueueListener
from typing import Optional

_LOG_QUEUE_LISTENER: Optional[QueueListener] = None


def configure_async_logging():
    """将控制台日志输出移到后台线程，避免主事件循环被同步日志IO卡住"""
    global _LOG_QUEUE_LISTENER

    root_logger = logging.getLogger()
    if getattr(root_logger, "_lmbridge_async_logging_configured", False):
        return

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    existing_handlers = list(root_logger.handlers)
    log_queue: queue.SimpleQueue = queue.SimpleQueue()
    queue_handler = QueueHandler(log_queue)

    root_logger.handlers = [queue_handler]
    _LOG_QUEUE_LISTENER = QueueListener(log_queue, *existing_handlers, respect_handler_level=True)
    _LOG_QUEUE_LISTENER.start()
    root_logger._lmbridge_async_logging_configured = True


def shutdown_async_logging():
    """停止后台日志监听线程（在应用关闭时调用）"""
    global _LOG_QUEUE_LISTENER
    if _LOG_QUEUE_LISTENER:
        _LOG_QUEUE_LISTENER.stop()
        _LOG_QUEUE_LISTENER = None


class EndpointFilter(logging.Filter):
    """过滤 HTTP access 日志：抑制监控轮询和恶意扫描的噪音（兼容 uvicorn/hypercorn）"""

    # 恶意扫描路径特征（不区分大小写）
    _SCAN_PATTERNS = (
        '/.env', '/.git/', '/php', '/info.php', '/test.php',
        '/.dockerenv', '/.npmrc', '/.kube/', '/.htpasswd', '/.netrc',
        '/adminer', '/wp-', '/wordpress', '/xmlrpc', '/config.json',
        '/actuator', '/.aws/', '/.ssh/', '/.config',
        '=phpinfo()',   # /?=phpinfo()  /index.php?=phpinfo()
        '%2567%2569%2574',  # URL-encoded .git
    )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        # 抑制监控面板的频繁轮询日志
        if "GET /api/monitor/" in message or "GET /monitor " in message:
            return False
        # 抑制恶意扫描 404 噪音
        msg_lower = message.lower()
        if " 404 " in message and any(p in msg_lower for p in self._SCAN_PATTERNS):
            return False
        return True


def install_access_log_filters():
    """给 hypercorn 的 access/error logger 安装噪音过滤器"""
    logging.getLogger("hypercorn.access").addFilter(EndpointFilter())
    logging.getLogger("hypercorn.error").addFilter(EndpointFilter())
