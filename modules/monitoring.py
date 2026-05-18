"""
监控模块 - 用于收集和管理请求统计数据
新版本：分层日志存储系统
- 按日期（天）分文件夹
- 按小时分子文件夹
- 每个请求一个独立的JSON文件
"""

import asyncio
import json
import time
import threading
import gzip
import queue
import concurrent.futures
from datetime import datetime, timedelta
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from typing import Dict, Optional, List
import logging
from pathlib import Path

logger = logging.getLogger(__name__)
# 导入SQLite扩展
try:
    from modules.monitoring_sqlite import SQLiteLogger
    SQLITE_AVAILABLE = True
except ImportError as e:
    logger.warning(f"SQLite扩展不可用: {e}")
    SQLITE_AVAILABLE = False
# 配置
class MonitorConfig:
    """监控配置"""
    LOG_DIR = Path("logs")
    
    # SQLite数据库配置
    DB_FILE = "requests.db"
    ENABLE_SQLITE = True  # 是否启用SQLite数据库（高性能查询）
    # 旧版本的JSONL文件（保留用于向后兼容）
    REQUEST_LOG_FILE = "requests.jsonl"
    ERROR_LOG_FILE = "errors.jsonl"
    STATS_FILE = "stats.json"
    
    # 新版本：分层日志配置
    ENABLE_HIERARCHICAL_LOGS = True  # 是否启用新的分层日志系统
    ENABLE_LEGACY_LOGS = False  # 🔧 禁用JSONL日志（已使用SQLite和分层JSON）
    USE_COMPRESSION = False  # 是否使用gzip压缩（.json.gz）
    
    # 日志保留策略
    MAX_LOG_DAYS = 30  # 保留最近N天的日志
    MAX_LOGS_PER_HOUR = 10000  # 每小时最多保留的日志文件数
    
    # 其他配置
    MAX_LOG_SIZE = 400 * 1024 * 1024  # 单文件最大大小（仅用于旧JSONL）
    MAX_LOG_FILES = 10  # 旧JSONL文件的轮转数量
    MAX_RECENT_REQUESTS = 2000   # 降低以减少内存占用（每秒1个请求也够存半小时）
    MAX_RECENT_ERRORS = 50
    STATS_UPDATE_INTERVAL = 5  # 秒
    MONITOR_SEND_TIMEOUT_SECONDS = 0.2  # 监控WS发送超时，避免拖慢主请求链路

# 确保日志目录存在
MonitorConfig.LOG_DIR.mkdir(exist_ok=True)

@dataclass
class RequestInfo:
    """
    请求信息 (内存轻量版)
    大负载字段在内存中仅保留截断版本，全量数据仅存在于日志中
    """
    request_id: str
    timestamp: float
    model: str
    status: str  # 'active', 'success', 'failed'
    duration: Optional[float] = None
    error: Optional[str] = None
    messages_count: int = 0
    session_id: Optional[str] = None
    mode: Optional[str] = None
    # 内存中仅保留极小部分的预览，防止长上下文撑爆内存
    request_messages_preview: Optional[str] = None
    request_params: Optional[dict] = None
    response_preview: Optional[str] = None
    reasoning_preview: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

@dataclass
class Stats:
    """统计数据"""
    total_requests: int = 0
    success_requests: int = 0  # 修复：统一使用success_requests
    failed_requests: int = 0
    active_requests: int = 0
    avg_duration: float = 0.0
    total_messages: int = 0
    uptime: float = 0.0

class LogManager:
    """日志管理器 - 支持新旧两种日志格式"""
    
    def __init__(self):
        self.request_log_path = MonitorConfig.LOG_DIR / MonitorConfig.REQUEST_LOG_FILE
        self.error_log_path = MonitorConfig.LOG_DIR / MonitorConfig.ERROR_LOG_FILE
        self._lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self._hourly_counters = {}  # {(date, hour): counter} 用于生成序号
        
        # 异步落盘队列（请求线程只入队，后台线程负责实际写盘）
        self._write_queue = queue.Queue(maxsize=2000)  # 减少内存排队
        self._writer_running = True
        
        # 初始化SQLite日志器
        self.sqlite_logger = None
        if MonitorConfig.ENABLE_SQLITE and SQLITE_AVAILABLE:
            try:
                db_path = MonitorConfig.LOG_DIR / MonitorConfig.DB_FILE
                self.sqlite_logger = SQLiteLogger(db_path)
                logger.info("✅ SQLite日志器已启用")
            except Exception as e:
                logger.error(f"初始化SQLite日志器失败: {e}")
        
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="monitor-log-writer",
            daemon=True
        )
        self._writer_thread.start()
        
    def _get_hierarchical_log_path(self, timestamp: float, request_id: str, log_type: str = "request", model_name: str = None) -> Path:
        """
        生成分层日志文件路径
        格式: logs/YYYYMMDD/HH/模型名_YYYYMMDD_HHMM_requestID[:8].json[.gz]
        
        Args:
            timestamp: Unix时间戳
            request_id: 请求ID
            log_type: 日志类型 ("request" 或 "error")
            model_name: 模型名称（可选）
        
        Returns:
            Path对象，指向日志文件路径
        """
        dt = datetime.fromtimestamp(timestamp)
        date_str = dt.strftime("%Y%m%d")  # 日期文件夹
        hour_str = dt.strftime("%H")      # 小时文件夹
        datetime_str = dt.strftime("%Y%m%d_%H%M")  # 精确到分钟的日期时间
        
        # 构建文件路径
        date_dir = MonitorConfig.LOG_DIR / date_str
        hour_dir = date_dir / hour_str
        
        # 确保目录存在
        hour_dir.mkdir(parents=True, exist_ok=True)
        
        # 文件名格式: 模型名_日期时间_请求ID前8位.json[.gz]
        req_id_short = request_id[:8] if request_id else "unknown"
        
        # 处理模型名称（去除特殊字符，避免文件名问题）
        if model_name:
            # 替换不允许的文件名字符
            safe_model_name = model_name.replace('/', '-').replace('\\', '-').replace(':', '-').replace('*', '-').replace('?', '-').replace('"', '-').replace('<', '-').replace('>', '-').replace('|', '-')
            # 限制长度
            if len(safe_model_name) > 50:
                safe_model_name = safe_model_name[:50]
        else:
            safe_model_name = "unknown"
        
        file_ext = ".json.gz" if MonitorConfig.USE_COMPRESSION else ".json"
        filename = f"{safe_model_name}_{datetime_str}_{req_id_short}{file_ext}"
        
        return hour_dir / filename
    
    def _filter_base64_from_messages(self, messages: list) -> list:
        """
        过滤消息列表中的 base64 图片数据，避免日志文件过大
        保留图片的元信息但移除实际的 base64 数据
        """
        if not messages:
            return messages
        
        import re
        import copy
        
        # base64 data URL 的正则匹配
        base64_pattern = re.compile(r'data:image/[^;]+;base64,[A-Za-z0-9+/=]{100,}')
        
        filtered_messages = []
        for msg in messages:
            if not isinstance(msg, dict):
                filtered_messages.append(msg)
                continue
            
            # 深拷贝以避免修改原始数据
            msg_copy = copy.deepcopy(msg)
            content = msg_copy.get('content')
            
            if isinstance(content, str):
                # 替换字符串中的 base64 图片
                if 'data:image' in content and 'base64' in content:
                    msg_copy['content'] = base64_pattern.sub('[BASE64_IMAGE_FILTERED]', content)
            
            elif isinstance(content, list):
                # 处理 OpenAI vision 格式的消息
                new_content = []
                for part in content:
                    if isinstance(part, dict):
                        part_copy = copy.deepcopy(part)
                        
                        if part_copy.get('type') == 'image_url':
                            image_url = part_copy.get('image_url', {})
                            url = image_url.get('url', '')
                            
                            if isinstance(url, str) and url.startswith('data:image') and 'base64' in url:
                                # 保留 MIME 类型信息，但移除 base64 数据
                                mime_match = re.match(r'(data:image/[^;]+;base64,)', url)
                                mime_type = mime_match.group(1) if mime_match else 'data:image/unknown;base64,'
                                # 计算原始数据大小（粗略估算）
                                original_size = len(url) * 3 // 4  # base64 编码后大约是原始的 4/3
                                image_url['url'] = f"[BASE64_IMAGE_FILTERED: ~{original_size // 1024}KB, {mime_type[5:-8]}]"
                                part_copy['image_url'] = image_url
                        
                        elif part_copy.get('type') == 'text':
                            text = part_copy.get('text', '')
                            if isinstance(text, str) and 'data:image' in text and 'base64' in text:
                                part_copy['text'] = base64_pattern.sub('[BASE64_IMAGE_FILTERED]', text)
                        
                        new_content.append(part_copy)
                    else:
                        new_content.append(part)
                
                msg_copy['content'] = new_content
            
            filtered_messages.append(msg_copy)
        
        return filtered_messages
    
    def _write_hierarchical_log(self, log_entry: dict, log_type: str = "request"):
        """
        写入分层日志文件
        
        Args:
            log_entry: 日志条目字典
            log_type: 日志类型 ("request" 或 "error")
        """
        try:
            timestamp = log_entry.get('timestamp', time.time())
            request_id = log_entry.get('request_id', 'unknown')
            model_name = log_entry.get('model', 'unknown')
            
            file_path = self._get_hierarchical_log_path(timestamp, request_id, log_type, model_name)
            
            # 🔧 过滤 base64 图片数据
            log_entry_filtered = log_entry.copy()
            if 'request_messages' in log_entry_filtered and log_entry_filtered['request_messages']:
                log_entry_filtered['request_messages'] = self._filter_base64_from_messages(
                    log_entry_filtered['request_messages']
                )
            
            # 写入文件
            # 紧凑序列化，减少大响应日志写盘时的 CPU/GIL 占用
            json_data = json.dumps(log_entry_filtered, ensure_ascii=False, separators=(',', ':'))
            
            if MonitorConfig.USE_COMPRESSION:
                with gzip.open(file_path, 'wt', encoding='utf-8') as f:
                    f.write(json_data)
            else:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(json_data)
            
            logger.debug(f"已写入分层日志: {file_path}")
            
        except Exception as e:
            logger.error(f"写入分层日志失败: {e}", exc_info=True)
    
    def _write_request_log_sync(self, log_entry: dict):
        """同步写入请求日志（仅供后台写线程调用）"""
        with self._lock:
            try:
                # 优先写入SQLite数据库（实时更新）
                if self.sqlite_logger:
                    try:
                        self.sqlite_logger.write_request(log_entry)
                    except Exception as e:
                        logger.error(f"写入SQLite失败: {e}")
                
                # 新格式：分层日志
                if MonitorConfig.ENABLE_HIERARCHICAL_LOGS:
                    self._write_hierarchical_log(log_entry, log_type="request")
                
                # 旧格式：JSONL（可选，用于向后兼容）
                if MonitorConfig.ENABLE_LEGACY_LOGS:
                    with open(self.request_log_path, 'a', encoding='utf-8') as f:
                        f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
            except Exception as e:
                logger.error(f"写入请求日志失败: {e}")

    def _write_error_log_sync(self, log_entry: dict):
        """同步写入错误日志（仅供后台写线程调用）"""
        with self._lock:
            try:
                # 新格式：分层日志
                if MonitorConfig.ENABLE_HIERARCHICAL_LOGS:
                    self._write_hierarchical_log(log_entry, log_type="error")
                
                # 旧格式：JSONL（可选，用于向后兼容）
                if MonitorConfig.ENABLE_LEGACY_LOGS:
                    with open(self.error_log_path, 'a', encoding='utf-8') as f:
                        f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
            except Exception as e:
                logger.error(f"写入错误日志失败: {e}")

    def _writer_loop(self):
        """后台写线程：串行消费日志队列，避免请求主链路阻塞在磁盘IO上"""
        while self._writer_running:
            try:
                item = self._write_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if item is None:
                break

            log_type, log_entry = item
            try:
                if log_type == "request":
                    self._write_request_log_sync(log_entry)
                else:
                    self._write_error_log_sync(log_entry)
            finally:
                self._write_queue.task_done()

    def write_request_log(self, log_entry: dict):
        """写入请求日志（入队，实际写盘由后台线程完成）"""
        try:
            self._write_queue.put_nowait(("request", log_entry))
        except queue.Full:
            logger.warning("请求日志队列已满，回退为同步写入")
            self._write_request_log_sync(log_entry)
    
    def write_error_log(self, log_entry: dict):
        """写入错误日志（入队，实际写盘由后台线程完成）"""
        try:
            self._write_queue.put_nowait(("error", log_entry))
        except queue.Full:
            logger.warning("错误日志队列已满，回退为同步写入")
            self._write_error_log_sync(log_entry)
    
    def read_recent_logs(self, log_type: str = "requests", limit: int = 50) -> List[dict]:
        """读取最近的日志。SQLite优先（O(log n)走索引），分层日志兜底，JSONL最后"""
        # 1️⃣ SQLite 优先 — 走 timestamp 索引，O(log n + limit)
        if log_type == "requests" and self.sqlite_logger:
            try:
                results = self.sqlite_logger.get_recent_requests(limit)
                if results:
                    return results
            except Exception as e:
                logger.warning(f"SQLite读最近日志失败，回退分层日志: {e}")
        
        # 2️⃣ 分层日志兜底 — 按小时从新到老读，凑够 limit 就停
        if MonitorConfig.ENABLE_HIERARCHICAL_LOGS:
            log_type_internal = "request" if log_type == "requests" else "error"
            return self._read_hierarchical_logs(log_type_internal, limit)
        
        # 3️⃣ JSONL 最后兜底
        log_path = self.request_log_path if log_type == "requests" else self.error_log_path
        logs = []
        if not log_path.exists():
            return logs
        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                for line in reversed(lines):
                    if len(logs) >= limit:
                        break
                    try:
                        log_entry = json.loads(line.strip())
                        if log_type == "requests" and log_entry.get('type') == 'request_end':
                            logs.append(log_entry)
                        elif log_type == "errors":
                            logs.append(log_entry)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.error(f"读取日志失败: {e}")
        return logs

    def _read_hierarchical_logs(self, log_type: str = "request", limit: int = 50,
                                days_back: int = 7) -> List[dict]:
        """从分层日志读最近的日志。按小时从新到老扫描，凑够 limit 条即停，不全量遍历。"""
        import os as _os
        logs = []
        ext = ".json.gz" if MonitorConfig.USE_COMPRESSION else ".json"
        today = datetime.now()
        
        # 从今天开始，逐个小时往过去找
        for day_offset in range(days_back):
            if len(logs) >= limit:
                break
            date = today - timedelta(days=day_offset)
            date_dir = MonitorConfig.LOG_DIR / date.strftime("%Y%m%d")
            if not date_dir.exists():
                continue
            
            # 收集该日期下的所有小时目录，按小时倒序（最新的在前）
            hour_dirs = []
            try:
                for entry in _os.scandir(date_dir):
                    if entry.is_dir():
                        hour_dirs.append(entry.name)
            except OSError:
                continue
            hour_dirs.sort(reverse=True)
            
            for hour_str in hour_dirs:
                if len(logs) >= limit:
                    break
                hour_dir = date_dir / hour_str
                
                # 收集该小时目录下的所有文件，按 mtime 倒序
                file_entries = []
                try:
                    for entry in _os.scandir(hour_dir):
                        if entry.name.endswith(ext) and entry.is_file(follow_symlinks=False):
                            try:
                                file_entries.append((entry.stat(follow_symlinks=False).st_mtime, entry.name))
                            except OSError:
                                file_entries.append((0, entry.name))
                except OSError:
                    continue
                file_entries.sort(key=lambda x: x[0], reverse=True)
                
                # 读取文件，直到凑够 limit 条
                for _, filename in file_entries:
                    if len(logs) >= limit:
                        break
                    filepath = hour_dir / filename
                    try:
                        if MonitorConfig.USE_COMPRESSION:
                            with gzip.open(filepath, 'rt', encoding='utf-8') as f:
                                log_entry = json.load(f)
                        else:
                            with open(filepath, 'r', encoding='utf-8') as f:
                                log_entry = json.load(f)
                        if log_type == "request" and log_entry.get('type') == 'request_end':
                            logs.append(log_entry)
                        elif log_type == "error":
                            logs.append(log_entry)
                    except Exception as e:
                        logger.warning(f"读取日志文件失败 {filepath}: {e}")
                        continue
        
        # 已按时间倒序（从新到老遍历），直接返回
        return logs[:limit]
class MonitoringService:
    """监控服务"""
    
    def __init__(self):
        self.startup_time = time.time()
        self.log_manager = LogManager()
        self.active_requests: Dict[str, RequestInfo] = {}
        self.recent_requests = deque(maxlen=MonitorConfig.MAX_RECENT_REQUESTS)
        self.recent_errors = deque(maxlen=MonitorConfig.MAX_RECENT_ERRORS)
        self.model_stats = defaultdict(lambda: {
            'total': 0, 'success': 0, 'failed': 0,
            'total_duration': 0, 'count_with_duration': 0
        })
        self._lock = threading.Lock()
        
        # 新增：存储完整的请求详情（用于详情查看）
        # 使用OrderedDict实现更好的内存管理
        from collections import OrderedDict
        self.request_details_cache = OrderedDict()  # 使用OrderedDict管理缓存（FIFO）
        self.MAX_DETAILS_CACHE = 50     # 严格限制，每条含完整请求数据，防止内存泄漏
        self.cache_size_limit_mb = 10   # 降到10MB（sys.getsizeof严重低估，实际内存需x10+）
        
        # WebSocket客户端管理
        self.monitor_clients = set()
        
        # 活跃请求超时配置（与流响应超时保持一致）
        from core.constants import TimeoutDefaults
        self.active_request_timeout = TimeoutDefaults.STREAM_RESPONSE_TIMEOUT  # 与流超时同步，当前2000秒
        
        # 统计持久化节流，避免每个请求结束都同步写 stats.json
        self._last_persist_time = 0.0
        self._persist_interval_seconds = MonitorConfig.STATS_UPDATE_INTERVAL
        
        # 🔧 性能修复：线程池用于offload request_start/request_end的阻塞操作
        # 避免 threading.Lock + asdict + logger.info 等阻塞 asyncio 事件循环
        self._monitor_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="monitor-ops"
        )
        
        # 加载持久化的统计数据
        self._load_persisted_stats()
        
        logger.info("监控服务已初始化")
        logger.info(f"  专用线程池: {self._monitor_pool._max_workers} workers (隔离于默认asyncio线程池)")
    
    def request_start(self, request_id: str, model: str, messages_count: int = 0,
                     session_id: str = None, mode: str = None,
                     messages: List[dict] = None, params: dict = None):
        """记录请求开始（非阻塞版：提交到线程池，不阻塞事件循环）"""
        self._monitor_pool.submit(
            self._request_start_sync,
            request_id, model, messages_count, session_id, mode, messages, params
        )
    
    def _request_start_sync(self, request_id: str, model: str, messages_count: int = 0,
                     session_id: str = None, mode: str = None,
                     messages: List[dict] = None, params: dict = None):
        """记录请求开始（实际执行，在线程池中运行）"""
        # 热路径只保留轻量预览，不再在 request_start 阶段遍历整份 messages 做伪 token 估算
        estimated_input_tokens = 0
        msg_preview = ""
        if messages:
            try:
                first_msg = messages[0].get('content', '') if messages else ""
                last_msg = messages[-1].get('content', '') if len(messages) > 1 else ""
                msg_preview = f"First: {str(first_msg)[:100]}... Last: {str(last_msg)[:100]}"
            except Exception:
                msg_preview = "[Messages List]"
        
        request_info = RequestInfo(
            request_id=request_id,
            timestamp=time.time(),
            model=model,
            status='active',
            messages_count=messages_count,
            session_id=session_id,
            mode=mode,
            request_messages_preview=msg_preview,
            request_params=params,
            input_tokens=estimated_input_tokens
        )
        
        with self._lock:
            self.active_requests[request_id] = request_info
            self._store_request_details(request_id, request_info)
        
        # 🔧 锁外执行日志写入（write_request_log 内部通过队列异步落盘）
        log_entry = {
            'type': 'request_start',
            'timestamp': request_info.timestamp,
            'request_id': request_id,
            'model': model,
            'messages_count': messages_count,
            'session_id': session_id,
            'mode': mode,
            # request_end 已经会记录 full_messages，这里只保留预览，避免新请求触发大块 JSON 序列化
            'request_messages_preview': msg_preview,
            'request_params': params
        }
        self.log_manager.write_request_log(log_entry)
        
        logger.info(f"请求开始 [ID: {request_id[:8]}] 模型: {model}")
    
    def request_end(self, request_id: str, success: bool, error: str = None,
                    response_content: str = None, reasoning_content: str = None,
                    input_tokens: int = 0, output_tokens: int = 0, cached_tokens: int = 0,
                    cost_info: dict = None, full_messages: List[dict] = None,
                    response_message: dict = None, response_tool_calls: List[dict] = None):
        """记录请求结束（非阻塞版：提交到线程池，不阻塞事件循环）"""
        self._monitor_pool.submit(
            self._request_end_sync,
            request_id, success, error, response_content, reasoning_content,
            input_tokens, output_tokens, cached_tokens, cost_info, full_messages,
            response_message, response_tool_calls
        )
    
    def _request_end_sync(self, request_id: str, success: bool, error: str = None,
                    response_content: str = None, reasoning_content: str = None,
                    input_tokens: int = 0, output_tokens: int = 0, cached_tokens: int = 0,
                    cost_info: dict = None, full_messages: List[dict] = None,
                    response_message: dict = None, response_tool_calls: List[dict] = None):
        """记录请求结束（实际执行，在线程池中运行）"""
        # 🔧 缩锁优化：锁内只做数据更新，asdict/日志构建移到锁外
        current_time = time.time()
        
        with self._lock:
            if request_id not in self.active_requests:
                logger.warning(f"未找到请求 {request_id}")
                return
                
            request_info = self.active_requests[request_id]
            request_info.status = 'success' if success else 'failed'
            request_info.duration = current_time - request_info.timestamp
            request_info.error = error
            
            request_info.response_preview = (response_content[:500] + "...") if response_content and len(response_content) > 500 else response_content
            request_info.reasoning_preview = (reasoning_content[:500] + "...") if reasoning_content and len(reasoning_content) > 500 else reasoning_content
            request_info.input_tokens = input_tokens
            request_info.output_tokens = output_tokens
            request_info.cached_tokens = cached_tokens

            model = request_info.model
            self.model_stats[model]['total'] += 1
            if success:
                self.model_stats[model]['success'] += 1
            else:
                self.model_stats[model]['failed'] += 1
            if request_info.duration:
                self.model_stats[model]['total_duration'] += request_info.duration
                self.model_stats[model]['count_with_duration'] += 1
            
            # 🔧 从活跃请求中移除 + 更新详情缓存（必须在锁内）
            del self.active_requests[request_id]
            self._store_request_details(request_id, request_info)
            
            # 锁内收集数据（asdict 放锁外太重）
            request_dict_for_recent = asdict(request_info)
            
            _mode = request_info.mode
            _session_id = request_info.session_id
            _messages_count = request_info.messages_count
            _request_params = request_info.request_params
            _original_timestamp = request_info.timestamp
            _duration = request_info.duration
            
            self.recent_requests.append(request_dict_for_recent)
            _error_info = None
            if not success:
                _error_info = {
                    'timestamp': time.time(),
                    'request_id': request_id,
                    'model': model,
                    'error': error or 'Unknown error'
                }
                self.recent_errors.append(_error_info)

            should_persist = (current_time - self._last_persist_time) >= self._persist_interval_seconds
            logger.info(f"请求结束 [ID: {request_id[:8]}] 状态: {'success' if success else 'failed'} 耗时: {_duration:.2f}s")
        
        # ═══════════════════════════════════════════
        # 🔧 锁外操作：日志构建 + IO写入（不持锁，不阻塞其他线程）
        # ═══════════════════════════════════════════
        
        # （_store_request_details 已移回锁内，因为需要修改共享的 request_details_cache）
        
        log_entry = {
            'type': 'request_end',
            'timestamp': _original_timestamp,
            'end_timestamp': time.time(),
            'request_id': request_id,
            'model': model,
            'status': request_info.status,
            'success': success,
            'duration': _duration,
            'error': error,
            'mode': _mode,
            'session_id': _session_id,
            'messages_count': _messages_count,
            'input_tokens': request_info.input_tokens,
            'output_tokens': request_info.output_tokens,
            'cached_tokens': cached_tokens,
            'request_messages': full_messages,
            'request_params': _request_params,
            'response_content': response_content,
            'response_message': response_message,
            'response_tool_calls': response_tool_calls,
            'reasoning_content': reasoning_content,
            'cost_info': cost_info
        }
        
        if _error_info:
            self.log_manager.write_error_log(_error_info)
        if log_entry:
            self.log_manager.write_request_log(log_entry)
        if should_persist:
            self._persist_stats_if_needed()
    
    def get_stats(self) -> Stats:
        """获取统计数据"""
        with self._lock:
            stats = Stats()
            stats.uptime = time.time() - self.startup_time
            stats.active_requests = len(self.active_requests)
            
            # 获取所有时间的统计（从持久化数据）
            # 优先使用持久化的总数，这样即使重启服务器也能保持准确
            total_all_time = sum(s['total'] for s in self.model_stats.values())
            success_all_time = sum(s['success'] for s in self.model_stats.values())
            failed_all_time = sum(s['failed'] for s in self.model_stats.values())
            
            # 使用所有时间的总数
            stats.total_requests = total_all_time
            stats.success_requests = success_all_time  # 修复：统一使用success_requests
            stats.failed_requests = failed_all_time
            
            # 计算总消息数（从最近的请求中累加）
            stats.total_messages = sum(req.get('messages_count', 0) for req in self.recent_requests)
            
            # 计算平均响应时间（使用最近100个请求）
            recent_durations = []
            for req in list(self.recent_requests)[-100:]:  # 最近100个请求
                if req.get('duration'):
                    recent_durations.append(req['duration'])
            
            if recent_durations:
                stats.avg_duration = sum(recent_durations) / len(recent_durations)
                
            return stats
    
    def get_model_stats(self) -> List[dict]:
        """获取模型统计"""
        with self._lock:
            model_stats_list = []
            for model, stats in self.model_stats.items():
                avg_duration = 0
                if stats['count_with_duration'] > 0:
                    avg_duration = stats['total_duration'] / stats['count_with_duration']
                    
                success_rate = 0
                if stats['total'] > 0:
                    success_rate = (stats['success'] / stats['total']) * 100
                    
                model_stats_list.append({
                    'model': model,
                    'total_requests': stats['total'],
                    'success_requests': stats['success'],  # 修复：统一使用success_requests
                    'failed_requests': stats['failed'],
                    'avg_duration': avg_duration,
                    'success_rate': success_rate
                })
            
            # 按总请求数排序
            model_stats_list.sort(key=lambda x: x['total_requests'], reverse=True)
            return model_stats_list
    
    def get_active_requests(self) -> List[dict]:
        """获取活动请求列表"""
        with self._lock:
            return [asdict(req) for req in self.active_requests.values()]
    
    def cleanup_stale_requests(self) -> int:
        """
        🔧 核心修复：清理超时的活跃请求
        
        Returns:
            清理的请求数量
        """
        # 🔧 性能修复：收集锁内数据，锁外执行 IO
        pending_error_logs = []
        pending_request_logs = []
        cleaned_count = 0
        
        with self._lock:
            current_time = time.time()
            stale_requests = []
            
            # 查找超时的请求
            for request_id, request_info in self.active_requests.items():
                request_age = current_time - request_info.timestamp
                if request_age > self.active_request_timeout:
                    stale_requests.append(request_id)
                    logger.warning(f"[CLEANUP] 发现超时活跃请求: {request_id[:8]} (存活: {request_age:.1f}秒)")
            
            # 清理超时的请求
            for request_id in stale_requests:
                request_info = self.active_requests[request_id]
                
                # 标记为失败并记录
                request_info.status = 'failed'
                request_info.duration = current_time - request_info.timestamp
                request_info.error = f"Request timeout after {request_info.duration:.1f} seconds"
                
                # 更新统计
                model = request_info.model
                self.model_stats[model]['total'] += 1
                self.model_stats[model]['failed'] += 1
                
                # 准备错误日志（锁外写入）
                error_info = {
                    'timestamp': current_time,
                    'request_id': request_id,
                    'model': model,
                    'error': request_info.error
                }
                self.recent_errors.append(error_info)
                pending_error_logs.append(error_info)
                
                # 准备请求日志（锁外写入）
                log_entry = {
                    'type': 'request_end',
                    'timestamp': request_info.timestamp,
                    'end_timestamp': current_time,
                    'request_id': request_id,
                    'model': model,
                    'status': 'failed',
                    'success': False,
                    'duration': request_info.duration,
                    'error': request_info.error,
                    'mode': request_info.mode,
                    'session_id': request_info.session_id,
                    'messages_count': request_info.messages_count
                }
                pending_request_logs.append(log_entry)
                
                # 添加到最近请求列表
                self.recent_requests.append(asdict(request_info))
                
                # 从活动请求中移除
                del self.active_requests[request_id]
                
                logger.info(f"[CLEANUP] 已清理超时请求: {request_id[:8]} (超时: {request_info.duration:.1f}秒)")
            
            cleaned_count = len(stale_requests)
        
        # 🔧 锁外执行 IO 操作
        for error_info in pending_error_logs:
            self.log_manager.write_error_log(error_info)
        for log_entry in pending_request_logs:
            self.log_manager.write_request_log(log_entry)
        
        if cleaned_count > 0:
            self._persist_stats_if_needed(force=True)
            logger.warning(f"[CLEANUP] 共清理了 {cleaned_count} 个超时活跃请求")
        
        # 🔧 根因4修复：返回清理的请求ID列表，让调用方也能清理关联资源
        return cleaned_count, stale_requests
    
    def get_recent_requests(self, limit: int = 50) -> List[dict]:
        """获取最近的请求"""
        with self._lock:
            requests = list(self.recent_requests)
            return requests[-limit:][::-1]  # 最新的在前
    
    def get_recent_errors(self, limit: int = 30) -> List[dict]:
        """获取最近的错误"""
        with self._lock:
            errors = list(self.recent_errors)
            return errors[-limit:][::-1]  # 最新的在前
    
    def get_summary(self) -> dict:
        """获取监控摘要"""
        stats = self.get_stats()
        model_stats = self.get_model_stats()
        
        return {
            'stats': asdict(stats),
            'model_stats': model_stats,
            'active_requests_list': self.get_active_requests(),
            'recent_errors_count': len(self.recent_errors)
        }
    
    async def _send_to_monitor_client(self, client, data: dict):
        """向单个监控客户端发送消息；超时或失败时自动剔除"""
        try:
            await asyncio.wait_for(
                client.send_json(data),
                timeout=MonitorConfig.MONITOR_SEND_TIMEOUT_SECONDS
            )
        except Exception:
            self.monitor_clients.discard(client)

    async def broadcast_to_monitors(self, data: dict):
        """向所有监控客户端广播数据（非阻塞主请求链路）"""
        if not self.monitor_clients:
            return

        # 监控广播不应阻塞正常请求；后台发送，慢客户端超时后自动剔除
        for client in list(self.monitor_clients):
            asyncio.create_task(self._send_to_monitor_client(client, data))
    
    def add_monitor_client(self, websocket):
        """添加监控客户端"""
        self.monitor_clients.add(websocket)
        logger.debug(f"监控客户端已连接，当前客户端数: {len(self.monitor_clients)}")
    
    def remove_monitor_client(self, websocket):
        """移除监控客户端"""
        self.monitor_clients.discard(websocket)
        logger.debug(f"监控客户端已断开，当前客户端数: {len(self.monitor_clients)}")
    
    def _store_request_details(self, request_id: str, request_info: RequestInfo):
        """存储请求详情到缓存（保持数据完整性）"""
        import sys
        
        # 创建要存储的数据 - 保持完整性，不截断
        request_data = asdict(request_info)
        
        # 检查缓存大小（粗略估算）
        cache_size_bytes = sys.getsizeof(self.request_details_cache)
        cache_size_mb = cache_size_bytes / (1024 * 1024)
        
        # 如果缓存过大（超过500MB），删除最老的10%项目
        if cache_size_mb > self.cache_size_limit_mb and len(self.request_details_cache) > 0:
            # 删除最老的10%项目
            items_to_remove = max(1, len(self.request_details_cache) // 10)
            for _ in range(items_to_remove):
                self.request_details_cache.popitem(last=False)
            cache_size_bytes = sys.getsizeof(self.request_details_cache)
            cache_size_mb = cache_size_bytes / (1024 * 1024)
            logger.info(f"[CACHE] 缓存超过限制，已清理 {items_to_remove} 个旧项，当前大小: ~{cache_size_mb:.2f}MB")
        
        # 限制缓存项数
        if len(self.request_details_cache) >= self.MAX_DETAILS_CACHE:
            # 删除最老的缓存项（FIFO）
            self.request_details_cache.popitem(last=False)
        
        # 存储新项 - 保持数据完整
        self.request_details_cache[request_id] = request_data
        
        # 定期记录缓存状态（每500个请求）
        if len(self.request_details_cache) % 500 == 0:
            logger.debug(f"[CACHE] 详情缓存状态 - 项数: {len(self.request_details_cache)}, 大小: ~{cache_size_mb:.2f}MB")
    
    def get_request_details(self, request_id: str) -> Optional[dict]:
        """获取请求详情 (优先从日志获取全量数据)"""
        # 🔧 重构：SQLite 优先（有索引），分层日志精确定位（用 timestamp），避免目录遍历
        
        # 1. 从日志文件中查找（SQLite优先 → 分层日志精确定位 → JSONL兜底）
        log_detail = self._find_request_in_logs(request_id)
        if log_detail:
            return log_detail

        with self._lock:
            # 2. 没入库的（还在活跃），从内存找
            if request_id in self.active_requests:
                return asdict(self.active_requests[request_id])
            
            # 3. 内存缓存兜底
            if request_id in self.request_details_cache:
                return self.request_details_cache[request_id]
            
            for req in self.recent_requests:
                if req.get('request_id') == request_id:
                    return req
        
        return None
    
    def _find_request_in_logs(self, request_id: str) -> Optional[dict]:
        """从日志文件中查找请求详情（支持分层日志和JSONL格式）"""
        try:
            sqlite_timestamp = None
            
            # 1️⃣ SQLite 优先（索引查询，O(log n)，且能拿到 timestamp）
            if self.log_manager.sqlite_logger:
                try:
                    result = self.log_manager.sqlite_logger.get_request_details(request_id)
                    if result:
                        sqlite_timestamp = result.get('timestamp')
                        # SQLite 返回的数据不含大字段（request_messages/response_content 等）
                        # 如果启用了分层日志且有 timestamp，精确定位文件获取全量数据
                        if MonitorConfig.ENABLE_HIERARCHICAL_LOGS and sqlite_timestamp:
                            full_result = self._find_request_in_hierarchical_logs_fast(
                                request_id, sqlite_timestamp)
                            if full_result:
                                return full_result
                        # 否则直接返回 SQLite 数据（无大字段但够用）
                        return result
                except Exception as e:
                    logger.warning(f"从SQLite查找请求详情失败: {e}")
            
            # 2️⃣ 分层日志兜底（无 timestamp 时慢速遍历，从最近开始，找到即停）
            if MonitorConfig.ENABLE_HIERARCHICAL_LOGS:
                result = self._find_request_in_hierarchical_logs(request_id)
                if result:
                    return result
            
            # 3️⃣ JSONL 文件最后兜底
            if self.log_manager.request_log_path.exists():
                with open(self.log_manager.request_log_path, 'r', encoding='utf-8') as f:
                    # 从后往前读取，提高查找效率
                    lines = f.readlines()
                    for line in reversed(lines):
                        try:
                            log_entry = json.loads(line.strip())
                            if (log_entry.get('request_id') == request_id and
                                log_entry.get('type') == 'request_end'):
                                # 找到了完整的请求记录
                                return log_entry
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            logger.error(f"从日志文件查找请求详情失败: {e}")
        
        return None
    
    def _find_request_in_hierarchical_logs(self, request_id: str) -> Optional[dict]:
        """从分层日志中查找请求详情（无timestamp时慢速遍历，从最近开始，找到即停）"""
        try:
            req_id_short = request_id[:8] if request_id else "unknown"
            ext = ".json.gz" if MonitorConfig.USE_COMPRESSION else ".json"
            
            today = datetime.now()
            for i in range(7):
                date = today - timedelta(days=i)
                date_str = date.strftime("%Y%m%d")
                date_dir = MonitorConfig.LOG_DIR / date_str
                
                if not date_dir.exists():
                    continue
                
                # 🔧 用 scandir 代替 iterdir，减少 syscall
                import os as _os
                try:
                    for hour_entry in _os.scandir(date_dir):
                        if not hour_entry.is_dir():
                            continue
                        pattern = f"*_{req_id_short}{ext}"
                        for log_file in Path(hour_entry.path).glob(pattern):
                            try:
                                if MonitorConfig.USE_COMPRESSION:
                                    with gzip.open(log_file, 'rt', encoding='utf-8') as f:
                                        log_entry = json.load(f)
                                else:
                                    with open(log_file, 'r', encoding='utf-8') as f:
                                        log_entry = json.load(f)
                                if log_entry.get('request_id') == request_id:
                                    logger.debug(f"从分层日志找到请求详情: {log_file}")
                                    return log_entry
                            except Exception as e:
                                logger.warning(f"读取日志文件失败 {log_file}: {e}")
                                continue
                except OSError:
                    continue
            
            return None
            
        except Exception as e:
            logger.error(f"从分层日志查找请求详情失败: {e}", exc_info=True)
            return None

    def _find_request_in_hierarchical_logs_fast(self, request_id: str, timestamp: float) -> Optional[dict]:
        """用 timestamp 精确定位分层日志文件，O(1) 目录查找，只扫描单个小时目录"""
        try:
            req_id_short = request_id[:8] if request_id else "unknown"
            dt = datetime.fromtimestamp(timestamp)
            date_str = dt.strftime("%Y%m%d")
            hour_str = dt.strftime("%H")
            hour_dir = MonitorConfig.LOG_DIR / date_str / hour_str
            
            if not hour_dir.exists():
                return None
            
            ext = ".json.gz" if MonitorConfig.USE_COMPRESSION else ".json"
            pattern = f"*_{req_id_short}{ext}"
            
            for log_file in hour_dir.glob(pattern):
                try:
                    if MonitorConfig.USE_COMPRESSION:
                        with gzip.open(log_file, 'rt', encoding='utf-8') as f:
                            log_entry = json.load(f)
                    else:
                        with open(log_file, 'r', encoding='utf-8') as f:
                            log_entry = json.load(f)
                    if log_entry.get('request_id') == request_id:
                        logger.debug(f"从分层日志快速定位找到请求详情: {log_file}")
                        return log_entry
                except Exception as e:
                    logger.warning(f"读取日志文件失败 {log_file}: {e}")
                    continue
            return None
        except Exception as e:
            logger.error(f"快速查找分层日志失败: {e}")
            return None
    
    def _persist_stats_if_needed(self, force: bool = False):
        """按节流策略决定是否持久化统计数据"""
        current_time = time.time()
        if not force and (current_time - self._last_persist_time) < self._persist_interval_seconds:
            return
        self._persist_stats()
        self._last_persist_time = current_time

    def _persist_stats(self):
        """持久化统计数据到文件（包含每日统计）"""
        try:
            stats_path = MonitorConfig.LOG_DIR / MonitorConfig.STATS_FILE
            
            # 🔧 核心修复：计算每日统计
            daily_stats = {}
            for req in self.recent_requests:
                timestamp = req.get('timestamp', 0)
                if timestamp:
                    date_str = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d')
                    if date_str not in daily_stats:
                        daily_stats[date_str] = {
                            'total': 0,
                            'success': 0,
                            'failed': 0
                        }
                    
                    daily_stats[date_str]['total'] += 1
                    if req.get('status') == 'success':
                        daily_stats[date_str]['success'] += 1
                    else:
                        daily_stats[date_str]['failed'] += 1
            
            # 准备要保存的数据
            stats_data = {
                'last_update': time.time(),
                'startup_time': self.startup_time,
                'model_stats': dict(self.model_stats),
                # 保存总体统计
                'total_requests_all_time': sum(s['total'] for s in self.model_stats.values()),
                'total_success_all_time': sum(s['success'] for s in self.model_stats.values()),
                'total_failed_all_time': sum(s['failed'] for s in self.model_stats.values()),
                # 🔧 新增：保存每日统计
                'daily_stats': daily_stats
            }
            
            # 写入文件
            with open(stats_path, 'w', encoding='utf-8') as f:
                json.dump(stats_data, f, ensure_ascii=False, separators=(',', ':'))
                
        except Exception as e:
            logger.error(f"持久化统计数据失败: {e}")
    
    def _load_persisted_stats(self):
        """从文件加载持久化的统计数据"""
        try:
            stats_path = MonitorConfig.LOG_DIR / MonitorConfig.STATS_FILE
            
            if not stats_path.exists():
                logger.info("未找到持久化统计数据，将从零开始")
                return
            
            with open(stats_path, 'r', encoding='utf-8') as f:
                stats_data = json.load(f)
            
            # 恢复模型统计
            if 'model_stats' in stats_data:
                # 🔧 关键修复：确保所有模型统计包含必需字段
                loaded_stats = {}
                for model, stats in stats_data['model_stats'].items():
                    loaded_stats[model] = {
                        'total': stats.get('total', 0),
                        'success': stats.get('success', 0),
                        'failed': stats.get('failed', 0),
                        'total_duration': stats.get('total_duration', 0),
                        'count_with_duration': stats.get('count_with_duration', 0)
                    }
                
                self.model_stats = defaultdict(
                    lambda: {'total': 0, 'success': 0, 'failed': 0,
                            'total_duration': 0, 'count_with_duration': 0},
                    loaded_stats
                )
            
            # 恢复最近的请求和错误
            if 'recent_requests' in stats_data:
                for req in stats_data['recent_requests']:
                    self.recent_requests.append(req)
            
            if 'recent_errors' in stats_data:
                for err in stats_data['recent_errors']:
                    self.recent_errors.append(err)
            
            # 如果是同一次运行会话，保持原有的启动时间
            # 否则重置启动时间
            if 'startup_time' in stats_data:
                time_since_last_update = time.time() - stats_data.get('last_update', 0)
                # 如果距离上次更新超过1小时，认为是新的会话
                if time_since_last_update > 3600:
                    self.startup_time = time.time()
                else:
                    self.startup_time = stats_data['startup_time']
            
            logger.info(f"已加载持久化统计数据：{len(self.model_stats)} 个模型统计")
            
        except Exception as e:
            logger.error(f"加载持久化统计数据失败: {e}")
    
    def get_all_time_stats(self) -> dict:
        """获取所有时间的统计数据（从日志文件计算）"""
        try:
            if not self.log_manager.request_log_path.exists():
                return {
                    'total_requests': 0,
                    'total_success': 0,
                    'total_failed': 0,
                    'models': {}
                }
            
            model_counts = defaultdict(lambda: {'total': 0, 'success': 0, 'failed': 0})
            total_requests = 0
            total_success = 0
            total_failed = 0
            
            with open(self.log_manager.request_log_path, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        log_entry = json.loads(line.strip())
                        if log_entry.get('type') == 'request_end':
                            model = log_entry.get('model', 'unknown')
                            status = log_entry.get('status', 'failed')
                            
                            total_requests += 1
                            model_counts[model]['total'] += 1
                            
                            if status == 'success':
                                total_success += 1
                                model_counts[model]['success'] += 1
                            else:
                                total_failed += 1
                                model_counts[model]['failed'] += 1
                                
                    except json.JSONDecodeError:
                        continue
            
            return {
                'total_requests': total_requests,
                'total_success': total_success,
                'total_failed': total_failed,
                'models': dict(model_counts)
            }
            
        except Exception as e:
            logger.error(f"计算所有时间统计失败: {e}")
            return {
                'total_requests': 0,
                'total_success': 0,
                'total_failed': 0,
                'models': {}
            }

    # ==================== Async 包装方法 ====================
    # 🔧 A1 修复：将持有 threading.Lock 的同步方法包装为异步版本
    # 避免 async handler 直接调用同步锁方法阻塞事件循环
    # 🔧 线程池隔离：使用 _monitor_pool 而非默认 asyncio 线程池
    # 防止监控查询（admin面板加载）耗尽默认线程池，影响 API 请求的 json.dumps 等操作

    async def get_summary_async(self) -> dict:
        """异步版 get_summary，不阻塞事件循环"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._monitor_pool, self.get_summary)

    async def get_stats_async(self):
        """异步版 get_stats"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._monitor_pool, self.get_stats)

    async def get_model_stats_async(self) -> list:
        """异步版 get_model_stats"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._monitor_pool, self.get_model_stats)

    async def get_active_requests_async(self) -> list:
        """异步版 get_active_requests"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._monitor_pool, self.get_active_requests)

    async def get_recent_requests_async(self, limit: int = 50) -> list:
        """异步版 get_recent_requests"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._monitor_pool, self.get_recent_requests, limit)

    async def get_recent_errors_async(self, limit: int = 30) -> list:
        """异步版 get_recent_errors"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._monitor_pool, self.get_recent_errors, limit)


# 创建全局监控服务实例
monitoring_service = MonitoringService()