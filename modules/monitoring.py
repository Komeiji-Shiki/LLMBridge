"""
监控模块 - 用于收集和管理请求统计数据
新版本：分层日志存储系统
- 按日期（天）分文件夹
- 按小时分子文件夹
- 每个请求一个独立的JSON文件
"""

import json
import time
import threading
import gzip
from datetime import datetime, timedelta
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from typing import Dict, Optional, List
import logging
from pathlib import Path

# 导入SQLite扩展
try:
    from modules.monitoring_sqlite import SQLiteLogger
    SQLITE_AVAILABLE = True
except ImportError as e:
    logger.warning(f"SQLite扩展不可用: {e}")
    SQLITE_AVAILABLE = False
logger = logging.getLogger(__name__)

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
    MAX_RECENT_REQUESTS = 10000
    MAX_RECENT_ERRORS = 50
    STATS_UPDATE_INTERVAL = 5  # 秒

# 确保日志目录存在
MonitorConfig.LOG_DIR.mkdir(exist_ok=True)

@dataclass
class RequestInfo:
    """请求信息"""
    request_id: str
    timestamp: float
    model: str
    status: str  # 'active', 'success', 'failed'
    duration: Optional[float] = None
    error: Optional[str] = None
    messages_count: int = 0
    session_id: Optional[str] = None
    mode: Optional[str] = None
    # 新增详细信息字段
    request_messages: Optional[List[dict]] = None
    request_params: Optional[dict] = None
    response_content: Optional[str] = None
    reasoning_content: Optional[str] = None  # 新增：思维链内容
    input_tokens: int = 0
    output_tokens: int = 0

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
        
        # 初始化SQLite日志器
        self.sqlite_logger = None
        if MonitorConfig.ENABLE_SQLITE and SQLITE_AVAILABLE:
            try:
                db_path = MonitorConfig.LOG_DIR / MonitorConfig.DB_FILE
                self.sqlite_logger = SQLiteLogger(db_path)
                logger.info("✅ SQLite日志器已启用")
            except Exception as e:
                logger.error(f"初始化SQLite日志器失败: {e}")
        
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
            
            # 写入文件
            json_data = json.dumps(log_entry, ensure_ascii=False, indent=2)
            
            if MonitorConfig.USE_COMPRESSION:
                with gzip.open(file_path, 'wt', encoding='utf-8') as f:
                    f.write(json_data)
            else:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(json_data)
            
            logger.debug(f"已写入分层日志: {file_path}")
            
        except Exception as e:
            logger.error(f"写入分层日志失败: {e}", exc_info=True)
    
    def write_request_log(self, log_entry: dict):
        """写入请求日志（支持新旧两种格式+SQLite）"""
        with self._lock:
            try:
                # 🔧 核心修复：优先写入SQLite数据库（实时更新）
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
    
    def write_error_log(self, log_entry: dict):
        """写入错误日志（支持新旧两种格式）"""
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
    
    def _read_hierarchical_logs(self, log_type: str = "request", limit: int = 50,
                                days_back: int = 7) -> List[dict]:
        """
        从分层日志中读取最近的日志
        
        Args:
            log_type: 日志类型 ("request" 或 "error")
            limit: 返回的最大日志数量
            days_back: 向前搜索的天数
        
        Returns:
            日志条目列表（按时间倒序）
        """
        logs = []
        
        try:
            # 获取最近N天的日期列表
            today = datetime.now()
            dates_to_check = []
            for i in range(days_back):
                date = today - timedelta(days=i)
                date_str = date.strftime("%Y%m%d")
                dates_to_check.append(date_str)
            
            # 收集所有日志文件（按修改时间倒序）
            all_log_files = []
            for date_str in dates_to_check:
                date_dir = MonitorConfig.LOG_DIR / date_str
                if not date_dir.exists():
                    continue
                
                # 遍历该日期下的所有小时文件夹
                for hour_dir in sorted(date_dir.iterdir(), reverse=True):
                    if not hour_dir.is_dir():
                        continue
                    
                    # 获取该小时下的所有日志文件
                    pattern = "*.json.gz" if MonitorConfig.USE_COMPRESSION else "*.json"
                    for log_file in sorted(hour_dir.glob(pattern), reverse=True):
                        all_log_files.append(log_file)
                        
                        # 提前退出优化：如果已经收集了足够多的文件
                        if len(all_log_files) >= limit * 2:
                            break
                    
                    if len(all_log_files) >= limit * 2:
                        break
                
                if len(all_log_files) >= limit * 2:
                    break
            
            # 读取文件内容
            for log_file in all_log_files:
                if len(logs) >= limit:
                    break
                
                try:
                    if MonitorConfig.USE_COMPRESSION:
                        with gzip.open(log_file, 'rt', encoding='utf-8') as f:
                            log_entry = json.load(f)
                    else:
                        with open(log_file, 'r', encoding='utf-8') as f:
                            log_entry = json.load(f)
                    
                    # 过滤日志类型
                    if log_type == "request" and log_entry.get('type') == 'request_end':
                        logs.append(log_entry)
                    elif log_type == "error":
                        logs.append(log_entry)
                
                except Exception as e:
                    logger.warning(f"读取日志文件失败 {log_file}: {e}")
                    continue
            
            return logs
            
        except Exception as e:
            logger.error(f"读取分层日志失败: {e}", exc_info=True)
            return []
    
    def read_recent_logs(self, log_type: str = "requests", limit: int = 50) -> List[dict]:
        """读取最近的日志（支持新旧两种格式，优先使用新格式）"""
        # 如果启用了分层日志，从分层日志读取
        if MonitorConfig.ENABLE_HIERARCHICAL_LOGS:
            log_type_internal = "request" if log_type == "requests" else "error"
            return self._read_hierarchical_logs(log_type_internal, limit)
        
        # 否则从旧的JSONL文件读取
        log_path = self.request_log_path if log_type == "requests" else self.error_log_path
        logs = []
        
        if not log_path.exists():
            return logs
            
        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                # 从后往前读取，收集最近的 request_end 类型日志
                for line in reversed(lines):
                    if len(logs) >= limit:
                        break
                    try:
                        log_entry = json.loads(line.strip())
                        # 只返回 request_end 类型的日志（包含完整信息）
                        if log_type == "requests" and log_entry.get('type') == 'request_end':
                            logs.append(log_entry)
                        elif log_type == "errors":
                            # 错误日志不需要过滤
                            logs.append(log_entry)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.error(f"读取日志失败: {e}")
            
        return logs  # 已经是倒序的（最新的在前）

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
        self.request_details_cache = OrderedDict()  # 使用OrderedDict管理缓存
        self.MAX_DETAILS_CACHE = 10000  # 保持原有的缓存大小
        self.cache_size_limit_mb = 500  # 增加缓存大小限制为500MB，确保数据完整性
        
        # WebSocket客户端管理
        self.monitor_clients = set()
        
        # 🔧 新增：活跃请求超时配置（默认10分钟）
        self.active_request_timeout = 600  # 10分钟，超过此时间的活跃请求将被自动清理
        
        # 加载持久化的统计数据
        self._load_persisted_stats()
        
        logger.info("监控服务已初始化")
    
    def request_start(self, request_id: str, model: str, messages_count: int = 0,
                     session_id: str = None, mode: str = None,
                     messages: List[dict] = None, params: dict = None):
        """记录请求开始（增加详细信息）"""
        with self._lock:
            # 计算输入token的估算值
            estimated_input_tokens = 0
            if messages:
                for msg in messages:
                    if isinstance(msg, dict) and 'content' in msg:
                        content = msg.get('content', '')
                        if isinstance(content, str):
                            estimated_input_tokens += len(content) // 4
                        elif isinstance(content, list):
                            # 处理多模态消息
                            for part in content:
                                if isinstance(part, dict) and part.get('type') == 'text':
                                    estimated_input_tokens += len(part.get('text', '')) // 4
            
            request_info = RequestInfo(
                request_id=request_id,
                timestamp=time.time(),
                model=model,
                status='active',
                messages_count=messages_count,
                session_id=session_id,
                mode=mode,
                request_messages=messages,
                request_params=params,
                input_tokens=estimated_input_tokens  # 设置估算的输入token
            )
            self.active_requests[request_id] = request_info
            
            # 同时存储到详情缓存
            self._store_request_details(request_id, request_info)
            
            # 写入日志
            log_entry = {
                'type': 'request_start',
                'timestamp': request_info.timestamp,
                'request_id': request_id,
                'model': model,
                'messages_count': messages_count,
                'session_id': session_id,
                'mode': mode
            }
            self.log_manager.write_request_log(log_entry)
            
            logger.info(f"请求开始 [ID: {request_id[:8]}] 模型: {model}")
    
    def request_end(self, request_id: str, success: bool, error: str = None,
                    response_content: str = None, reasoning_content: str = None,
                    input_tokens: int = 0, output_tokens: int = 0, cost_info: dict = None):
        """记录请求结束（增加响应内容、思维链和成本信息）"""
        with self._lock:
            if request_id not in self.active_requests:
                logger.warning(f"未找到请求 {request_id}")
                return
                
            request_info = self.active_requests[request_id]
            request_info.status = 'success' if success else 'failed'
            request_info.duration = time.time() - request_info.timestamp
            request_info.error = error
            request_info.response_content = response_content
            request_info.reasoning_content = reasoning_content
            request_info.input_tokens = input_tokens
            request_info.output_tokens = output_tokens
            
            # 更新详情缓存
            self._store_request_details(request_id, request_info)
            
            # 更新模型统计
            model = request_info.model
            self.model_stats[model]['total'] += 1
            if success:
                self.model_stats[model]['success'] += 1
            else:
                self.model_stats[model]['failed'] += 1
                
            if request_info.duration:
                self.model_stats[model]['total_duration'] += request_info.duration
                self.model_stats[model]['count_with_duration'] += 1
            
            # 持久化统计数据
            self._persist_stats()
            
            # 添加到最近请求列表
            self.recent_requests.append(asdict(request_info))
            
            # 如果失败，添加到错误列表
            if not success:
                error_info = {
                    'timestamp': time.time(),
                    'request_id': request_id,
                    'model': model,
                    'error': error or 'Unknown error'
                }
                self.recent_errors.append(error_info)
                self.log_manager.write_error_log(error_info)
            
            # 写入请求日志（包含完整详情和成本信息）
            log_entry = {
                'type': 'request_end',
                'timestamp': time.time(),
                'request_id': request_id,
                'model': model,
                'status': request_info.status,
                'success': success,  # 🔧 关键修复：添加success布尔字段
                'duration': request_info.duration,
                'error': error,
                'mode': request_info.mode,
                'session_id': request_info.session_id,
                'messages_count': request_info.messages_count,
                'input_tokens': request_info.input_tokens,
                'output_tokens': request_info.output_tokens,
                # 包含详细信息
                'request_messages': request_info.request_messages,
                'request_params': request_info.request_params,
                'response_content': request_info.response_content,
                'reasoning_content': request_info.reasoning_content,
                # 🔧 新增：成本信息
                'cost_info': cost_info
            }
            self.log_manager.write_request_log(log_entry)
            
            # 从活动请求中移除
            del self.active_requests[request_id]
            
            logger.info(f"请求结束 [ID: {request_id[:8]}] 状态: {request_info.status} 耗时: {request_info.duration:.2f}s")
    
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
                
                # 添加到错误列表
                error_info = {
                    'timestamp': current_time,
                    'request_id': request_id,
                    'model': model,
                    'error': request_info.error
                }
                self.recent_errors.append(error_info)
                self.log_manager.write_error_log(error_info)
                
                # 写入请求日志
                log_entry = {
                    'type': 'request_end',
                    'timestamp': current_time,
                    'request_id': request_id,
                    'model': model,
                    'status': 'failed',
                    'success': False,  # 🔧 关键修复：添加success字段
                    'duration': request_info.duration,
                    'error': request_info.error,
                    'mode': request_info.mode,
                    'session_id': request_info.session_id,
                    'messages_count': request_info.messages_count
                }
                self.log_manager.write_request_log(log_entry)
                
                # 添加到最近请求列表
                self.recent_requests.append(asdict(request_info))
                
                # 从活动请求中移除
                del self.active_requests[request_id]
                
                logger.info(f"[CLEANUP] 已清理超时请求: {request_id[:8]} (超时: {request_info.duration:.1f}秒)")
            
            if stale_requests:
                # 持久化统计数据
                self._persist_stats()
                logger.warning(f"[CLEANUP] 共清理了 {len(stale_requests)} 个超时活跃请求")
            
            return len(stale_requests)
    
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
    
    async def broadcast_to_monitors(self, data: dict):
        """向所有监控客户端广播数据"""
        if not self.monitor_clients:
            return
            
        disconnected = []
        for client in self.monitor_clients:
            try:
                await client.send_json(data)
            except:
                disconnected.append(client)
        
        # 清理断开的连接
        for client in disconnected:
            self.monitor_clients.discard(client)
    
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
        """获取请求详情"""
        with self._lock:
            # 先从缓存中查找
            if request_id in self.request_details_cache:
                return self.request_details_cache[request_id]
            
            # 从活跃请求中查找
            if request_id in self.active_requests:
                return asdict(self.active_requests[request_id])
            
            # 从最近请求中查找
            for req in self.recent_requests:
                if req.get('request_id') == request_id:
                    return req
            
            # 如果内存中都没有，从日志文件中查找
            return self._find_request_in_logs(request_id)
    
    def _find_request_in_logs(self, request_id: str) -> Optional[dict]:
        """从日志文件中查找请求详情（支持分层日志和JSONL格式）"""
        try:
            # 优先从分层日志中查找（新格式）
            if MonitorConfig.ENABLE_HIERARCHICAL_LOGS:
                result = self._find_request_in_hierarchical_logs(request_id)
                if result:
                    return result
            
            # 从SQLite数据库查找
            if self.log_manager.sqlite_logger:
                try:
                    result = self.log_manager.sqlite_logger.get_request_details(request_id)
                    if result:
                        return result
                except Exception as e:
                    logger.warning(f"从SQLite查找请求详情失败: {e}")
            
            # 回退到旧的JSONL文件查找
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
        """从分层日志中查找请求详情"""
        try:
            req_id_short = request_id[:8] if request_id else "unknown"
            
            # 获取最近7天的日期列表
            today = datetime.now()
            for i in range(7):
                date = today - timedelta(days=i)
                date_str = date.strftime("%Y%m%d")
                date_dir = MonitorConfig.LOG_DIR / date_str
                
                if not date_dir.exists():
                    continue
                
                # 遍历该日期下的所有小时文件夹
                for hour_dir in date_dir.iterdir():
                    if not hour_dir.is_dir():
                        continue
                    
                    # 查找包含该request_id的文件
                    pattern = f"*_{req_id_short}.json"
                    if MonitorConfig.USE_COMPRESSION:
                        pattern = f"*_{req_id_short}.json.gz"
                    
                    for log_file in hour_dir.glob(pattern):
                        try:
                            if MonitorConfig.USE_COMPRESSION:
                                with gzip.open(log_file, 'rt', encoding='utf-8') as f:
                                    log_entry = json.load(f)
                            else:
                                with open(log_file, 'r', encoding='utf-8') as f:
                                    log_entry = json.load(f)
                            
                            # 验证request_id完全匹配
                            if log_entry.get('request_id') == request_id:
                                logger.debug(f"从分层日志找到请求详情: {log_file}")
                                return log_entry
                                
                        except Exception as e:
                            logger.warning(f"读取日志文件失败 {log_file}: {e}")
                            continue
            
            return None
            
        except Exception as e:
            logger.error(f"从分层日志查找请求详情失败: {e}", exc_info=True)
            return None
    
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
                json.dump(stats_data, f, ensure_ascii=False, indent=2)
                
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

# 创建全局监控服务实例
monitoring_service = MonitoringService()