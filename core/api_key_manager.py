"""
API Key 管理模块
支持创建多个 API Key，每个 key 可以配置：
- 允许使用的模型列表（白名单，空列表表示允许所有模型）
- 全局 RPM（每分钟请求数）限制（0 表示不限制）
- 名称/描述
- 启用/禁用状态
"""

import json
import logging
import os
import secrets
import time
from pathlib import Path
from threading import Lock, RLock
from functools import wraps
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger(__name__)

# API Key 数据文件路径
API_KEYS_FILE = "api_keys.json"


class RateLimiter:
    """滑动窗口速率限制器"""

    def __init__(self):
        # 🔧 普通 dict 而非 defaultdict：避免为从未限流过的 key 惰性创建空条目
        self._windows: Dict[str, list] = {}
        self._lock = Lock()

    def check_and_consume(self, key_id: str, rpm_limit: int) -> Tuple[bool, int]:
        """
        检查并消费一个请求配额。

        Args:
            key_id: API Key 的 ID
            rpm_limit: 每分钟允许的最大请求数（0 = 不限制）

        Returns:
            (allowed, remaining) - 是否允许, 剩余配额
        """
        if rpm_limit <= 0:
            return True, -1  # 不限制

        now = time.time()
        window_start = now - 60.0  # 60 秒滑动窗口

        with self._lock:
            # 清理过期记录（.get 取值，不为陈旧/陌生 key 创建条目）
            timestamps = [t for t in self._windows.get(key_id, ()) if t > window_start]

            if len(timestamps) >= rpm_limit:
                self._windows[key_id] = timestamps
                return False, 0

            # 消费一个配额
            timestamps.append(now)
            self._windows[key_id] = timestamps
            return True, rpm_limit - len(timestamps)

    def get_usage(self, key_id: str) -> int:
        """获取当前窗口内的请求数"""
        now = time.time()
        window_start = now - 60.0

        with self._lock:
            timestamps = self._windows.get(key_id, [])
            return len([t for t in timestamps if t > window_start])

    def reset(self, key_id: str):
        """重置某个 key 的速率限制"""
        with self._lock:
            self._windows.pop(key_id, None)


def _serialize_key_mutation(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._save_lock:
            return method(self, *args, **kwargs)
    return wrapped


class APIKeyManager:
    """API Key 管理器（单例）"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._keys: Dict[str, Dict[str, Any]] = {}
        self._lock = Lock()
        # 🔧 磁盘写入专用锁：写盘已移出 _lock（validate_request 热路径与管理
        # 操作共享 _lock，锁内写盘会把所有请求的鉴权卡在磁盘 IO 上）
        self._save_lock = RLock()
        self._rate_limiter = RateLimiter()
        # 建立 secret -> key_id 的快速查找索引
        self._secret_index: Dict[str, str] = {}
        # 热路径只更新内存统计，脏标记 + 后台任务周期性落盘
        self._dirty = False
        self._load()

    @_serialize_key_mutation
    def _load(self):
        """从文件加载 API Key 配置"""
        if not os.path.exists(API_KEYS_FILE):
            logger.info(f"[APIKeyManager] '{API_KEYS_FILE}' 不存在，将使用空配置")
            return

        try:
            with open(API_KEYS_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return
                data = json.loads(content)

            if not isinstance(data, dict) or not all(isinstance(value, dict) for value in data.values()):
                raise ValueError("API Key 配置必须是以 ID 为键、对象为值的 JSON 对象")

            with self._lock:
                self._keys.clear()
                self._secret_index.clear()
                if isinstance(data, dict):
                    self._keys.update(data)
                    # 重建索引
                    for key_id, key_data in self._keys.items():
                        secret = key_data.get("secret", "")
                        if secret:
                            self._secret_index[secret] = key_id

            logger.info(f"[APIKeyManager] ✅ 已加载 {len(self._keys)} 个 API Key")
        except Exception as e:
            logger.error(f"[APIKeyManager] ❌ 加载 '{API_KEYS_FILE}' 失败: {e}")

    def _serialize_unsafe(self) -> Tuple[str, int]:
        """锁内调用：序列化当前 keys 并清除脏标记，返回 (JSON文本, key数量)。

        序列化是纯内存操作（微秒级），放在锁内保证快照一致；
        真正的磁盘写入由锁外的 _write_to_disk 完成。
        """
        self._dirty = False
        return json.dumps(self._keys, ensure_ascii=False, indent=2), len(self._keys)

    def _write_to_disk(self, payload: str, count: int):
        """锁外调用：原子写盘（临时文件 + os.replace，_save_lock 串行化）。

        🔧 修复：写盘不再持有 _lock。旧版管理操作在锁内同步写盘，
        validate_request 热路径抢同一把锁时会被磁盘 IO 卡住（事件循环
        线程直接调用鉴权，等锁即阻塞所有并发请求）。
        写盘失败时重新置脏，等待后台任务下次重试。
        """
        tmp_path = API_KEYS_FILE + ".tmp"
        try:
            with self._save_lock:
                # 管理请求在线程池并发执行。先取得写锁再取最新快照，
                # 防止较早生成的快照较晚写入，恢复已删除/撤销的 Key。
                with self._lock:
                    payload, count = self._serialize_unsafe()
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp_path, API_KEYS_FILE)
            logger.info(f"[APIKeyManager] ✅ 已保存 {count} 个 API Key")
        except Exception as e:
            logger.error(f"[APIKeyManager] ❌ 保存 '{API_KEYS_FILE}' 失败: {e}")
            with self._lock:
                self._dirty = True
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

    def reload(self):
        """重新加载配置"""
        self._load()

    @staticmethod
    def _generate_secret() -> str:
        """生成一个安全的 API Key secret"""
        return "sk-" + secrets.token_urlsafe(32)

    @_serialize_key_mutation
    def create_key(
        self,
        name: str,
        allowed_models: Optional[List[str]] = None,
        rpm_limit: int = 0,
        enabled: bool = True,
        description: str = "",
    ) -> Dict[str, Any]:
        """
        创建一个新的 API Key。

        Args:
            name: Key 的名称
            allowed_models: 允许使用的模型列表（None 或空列表 = 允许所有）
            rpm_limit: 每分钟请求数限制（0 = 不限制）
            enabled: 是否启用
            description: 描述

        Returns:
            创建的 key 信息（包含明文 secret，仅此一次展示）
        """
        secret = self._generate_secret()
        key_id = secrets.token_hex(8)

        key_data = {
            "name": name,
            "secret": secret,
            "allowed_models": allowed_models or [],
            "rpm_limit": rpm_limit,
            "enabled": enabled,
            "description": description,
            "created_at": time.time(),
            "last_used_at": None,
            "total_requests": 0,
        }

        with self._lock:
            self._keys[key_id] = key_data
            self._secret_index[secret] = key_id
            payload, count = self._serialize_unsafe()
        self._write_to_disk(payload, count)

        logger.info(f"[APIKeyManager] 🔑 创建新 API Key: name='{name}', id={key_id}")

        return {
            "id": key_id,
            "secret": secret,  # 明文 secret，仅在创建时返回
            **{k: v for k, v in key_data.items() if k != "secret"},
        }

    @_serialize_key_mutation
    def delete_key(self, key_id: str) -> bool:
        """删除一个 API Key"""
        payload = None
        count = 0
        with self._lock:
            key_data = self._keys.pop(key_id, None)
            if key_data:
                secret = key_data.get("secret", "")
                self._secret_index.pop(secret, None)
                self._rate_limiter.reset(key_id)
                payload, count = self._serialize_unsafe()
        if payload is None:
            return False
        self._write_to_disk(payload, count)
        logger.info(f"[APIKeyManager] 🗑️ 删除 API Key: id={key_id}")
        return True

    @_serialize_key_mutation
    def update_key(self, key_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        更新一个 API Key 的配置。

        可更新字段: name, allowed_models, rpm_limit, enabled, description
        """
        allowed_fields = {"name", "allowed_models", "rpm_limit", "enabled", "description"}

        with self._lock:
            if key_id not in self._keys:
                return None

            for field, value in updates.items():
                if field in allowed_fields:
                    self._keys[key_id][field] = value

            payload, count = self._serialize_unsafe()
            # 返回脱敏信息（锁内快照，写盘在锁外）
            info = self._get_key_info_unsafe(key_id)

        self._write_to_disk(payload, count)
        return info

    def get_key_info(self, key_id: str) -> Optional[Dict[str, Any]]:
        """获取 API Key 的信息（不包含 secret）"""
        with self._lock:
            return self._get_key_info_unsafe(key_id)

    def _get_key_info_unsafe(self, key_id: str) -> Optional[Dict[str, Any]]:
        """内部方法：获取 key 信息（不加锁，需在锁内调用）"""
        key_data = self._keys.get(key_id)
        if not key_data:
            return None

        secret = key_data.get("secret", "")
        masked_secret = secret[:7] + "..." + secret[-4:] if len(secret) > 11 else "***"

        return {
            "id": key_id,
            "name": key_data.get("name", ""),
            "secret_masked": masked_secret,
            "allowed_models": key_data.get("allowed_models", []),
            "rpm_limit": key_data.get("rpm_limit", 0),
            "enabled": key_data.get("enabled", True),
            "description": key_data.get("description", ""),
            "created_at": key_data.get("created_at"),
            "last_used_at": key_data.get("last_used_at"),
            "total_requests": key_data.get("total_requests", 0),
            "current_rpm": self._rate_limiter.get_usage(key_id),
        }

    def list_keys(self) -> List[Dict[str, Any]]:
        """列出所有 API Key（不包含 secret）"""
        with self._lock:
            return [
                info for key_id in self._keys
                if (info := self._get_key_info_unsafe(key_id)) is not None
            ]

    def validate_request(
        self, provided_secret: str, model_name: Optional[str] = None
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        验证 API 请求。

        Args:
            provided_secret: 请求中提供的 API Key
            model_name: 请求的模型名称（可选，用于检查模型权限）

        Returns:
            (valid, key_id, error_message)
            - valid: 验证是否通过
            - key_id: 匹配的 key ID（验证通过时）
            - error_message: 错误信息（验证失败时）
        """
        with self._lock:
            key_id = self._secret_index.get(provided_secret)
            if key_id is None:
                return False, None, "提供的 API Key 不正确。"

            key_data = self._keys.get(key_id)
            if not key_data:
                return False, None, "API Key 数据异常。"

            # 检查是否启用
            if not key_data.get("enabled", True):
                return False, key_id, "此 API Key 已被禁用。"

            # 检查模型权限
            allowed_models = key_data.get("allowed_models", [])
            if allowed_models and model_name:
                if model_name not in allowed_models:
                    return (
                        False,
                        key_id,
                        f"此 API Key 无权访问模型 '{model_name}'。允许的模型: {', '.join(allowed_models)}",
                    )

            # 🔧 在锁内读取 rpm_limit，避免与 update_key/delete_key 的锁外读竞态
            rpm_limit = key_data.get("rpm_limit", 0)

        # RPM 检查（在锁外进行，因为 rate_limiter 有自己的锁）
        allowed, remaining = self._rate_limiter.check_and_consume(key_id, rpm_limit)
        if not allowed:
            return (
                False,
                key_id,
                f"请求频率超限。此 Key 的 RPM 限制为 {rpm_limit} 次/分钟。",
            )

        # 更新使用统计
        # 热路径不做同步文件落盘，否则每个新请求都会阻塞事件循环并拖慢其他流式请求；
        # 标记为脏，由后台任务（background_tasks.monitors.api_key_stats_saver）周期性落盘
        with self._lock:
            if key_id in self._keys:
                self._keys[key_id]["last_used_at"] = time.time()
                self._keys[key_id]["total_requests"] = (
                    self._keys[key_id].get("total_requests", 0) + 1
                )
                self._dirty = True

        return True, key_id, None

    def get_allowed_models(self, provided_secret: str) -> Optional[List[str]]:
        """
        根据 API Key 获取允许的模型列表。

        Args:
            provided_secret: API Key secret

        Returns:
            允许的模型列表（空列表表示允许所有），None 表示 key 无效
        """
        with self._lock:
            key_id = self._secret_index.get(provided_secret)
            if key_id is None:
                return None

            key_data = self._keys.get(key_id)
            if not key_data or not key_data.get("enabled", True):
                return None

            return key_data.get("allowed_models", [])

    def has_keys(self) -> bool:
        """检查是否有任何已配置的 API Key"""
        with self._lock:
            return len(self._keys) > 0

    def save_now(self):
        """立即保存（用于关闭前保存统计数据）"""
        with self._lock:
            payload, count = self._serialize_unsafe()
        self._write_to_disk(payload, count)

    def save_if_dirty(self):
        """仅在有未落盘变更时保存（供后台周期任务在线程池中调用）。

        🔧 修复：旧版只在优雅关闭时 save_now()，进程被强杀会丢失全部使用统计。
        """
        with self._lock:
            if not self._dirty:
                return
            payload, count = self._serialize_unsafe()
        self._write_to_disk(payload, count)


# 全局单例
api_key_manager = APIKeyManager()
