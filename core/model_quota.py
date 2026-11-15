"""
模型与渠道额度

模型额度写在端点配置的 quota 字段，按模型别名统计；渠道额度写在渠道的 quota 字段，
渠道下所有模型共用一份，写了 quota_from 的渠道记在被共用的渠道名下（同一个上游 Key 接了好几个
地址时，从同一笔钱里扣）。两层额度对所有调用方（包括管理员）合计生效，任一层用完或到期，
请求在发往上游之前就以 402 结束，只能在管理面板调高上限、延后到期时间或重置用量后恢复。

费用和 Token 要等请求结束才知道，进行中的请求不会被打断。为了不让并发请求把用量冲出上限太多，
费用或 Token 用到 90% 以后，同一份额度同一时间只放行一个请求，其余返回 429 让客户端稍后重试。

用量存在 model_usage.json，只记录设了额度的对象。额度从无到有、从有到无，或者模型、渠道被删除时，
管理接口调用 forget 清掉旧用量，重新设置的额度从零开始。
"""
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from core import usage_quota
from core.errors import QuotaExceededError, RateLimitError
from core.request_context import current_request

logger = logging.getLogger(__name__)

USAGE_FILE = "model_usage.json"
# 费用或 Token 用到上限的这个比例以后，同一份额度只放行一个进行中的请求
NEAR_LIMIT = 0.9


class ModelQuotaManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._usage: Dict[str, Dict[str, Any]] = {}
        self._inflight: Dict[str, int] = {}
        self._dirty = False
        self._load()

    def _load(self):
        try:
            with open(USAGE_FILE, "r", encoding="utf-8") as f:
                content = f.read()
            data = json.loads(content) if content.strip() else {}
            if not isinstance(data, dict):
                raise ValueError("根节点必须是 JSON 对象")
            self._usage = data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            logger.error(f"[ModelQuota] 读取 '{USAGE_FILE}' 失败，模型和渠道用量从零开始: {error}")

    @staticmethod
    def subjects(model: Optional[str], endpoint: Optional[Dict[str, Any]]) -> List[Tuple[str, str, Dict[str, Any]]]:
        """一次请求涉及的额度：[(用量键, 提示里的名称, 额度配置)]。"""
        from core.config_loader import CHANNEL_QUOTAS
        endpoint = endpoint or {}
        found = []
        quota = endpoint.get("quota")
        if model and isinstance(quota, dict):
            found.append(("model:" + model, f"模型 {model}", quota))
        channel_id = endpoint.get("channel")
        entry = CHANNEL_QUOTAS.get(channel_id) if channel_id else None
        if entry:
            found.append(("channel:" + entry["owner"], f"渠道 {entry['name']}", entry["quota"]))
        return found

    def _usage_unsafe(self, key: str, quota: Dict[str, Any], now: float) -> Dict[str, Any]:
        usage = usage_quota.current_usage(self._usage.get(key), quota.get("period", "total"), now)
        if usage is not self._usage.get(key):
            self._usage[key] = usage
            self._dirty = True
        return usage

    @staticmethod
    def _near_limit(quota: Dict[str, Any]) -> bool:
        return any(quota["limits"][kind] and quota["used"][kind] >= quota["limits"][kind] * NEAR_LIMIT
                   for kind in ("cost", "tokens"))

    def enforce(self, model: Optional[str], endpoint: Optional[Dict[str, Any]], context=None) -> None:
        """请求发往上游之前检查额度：用完抛 402；接近上限且已有请求在进行时抛 429；通过后计一次请求。

        context 是当前请求的上下文时记下占用，由请求中间件或 WebSocket 处理函数在结束时 release。
        同一个请求嵌套经过多个处理函数时只检查一次。
        """
        if context is not None and context.quota_checked:
            return
        subjects = self.subjects(model, endpoint)
        if not subjects:
            return
        hold = context is not None and current_request.get() is context
        now = time.time()
        with self._lock:
            for key, label, config in subjects:
                quota = usage_quota.snapshot(config, self._usage_unsafe(key, config, now), now)
                if usage_quota.blocked(quota):
                    raise QuotaExceededError(usage_quota.exhausted_message(label, quota)).to_http_exception()
                if self._inflight.get(key) and self._near_limit(quota):
                    raise RateLimitError(f"{label} 的额度快用完了，同一时间只放行一个请求，请稍后重试。").to_http_exception()
            for key, _, _ in subjects:
                self._usage[key]["requests"] += 1
                if hold:
                    self._inflight[key] = self._inflight.get(key, 0) + 1
            self._dirty = True
        if context is not None:
            context.quota_checked = True
        if hold:
            context.quota_holds.extend(key for key, _, _ in subjects)

    def release(self, context) -> None:
        """归还请求占用的并发名额；请求结束或 WebSocket 开始下一轮时调用。"""
        holds, context.quota_holds, context.quota_checked = context.quota_holds, [], False
        if not holds:
            return
        with self._lock:
            for key in holds:
                left = self._inflight.get(key, 0) - 1
                if left > 0:
                    self._inflight[key] = left
                else:
                    self._inflight.pop(key, None)

    def record(self, model: Optional[str], endpoint: Optional[Dict[str, Any]], tokens: int,
               cost_info: Optional[Dict[str, Any]], *, billable: bool = True) -> None:
        """请求结束后把 Token 和费用计入模型和渠道额度；计入规则与访客 Key 相同（见 record_usage）。"""
        cost, currency = usage_quota.cost_of(cost_info)
        if not billable or (not tokens and not cost):
            return
        subjects = self.subjects(model, endpoint)
        if not subjects:
            return
        now = time.time()
        with self._lock:
            for key, _, config in subjects:
                usage_quota.add_usage(self._usage_unsafe(key, config, now), tokens, cost, currency)
            self._dirty = True

    def overview(self) -> Dict[str, Dict[str, Any]]:
        """管理面板用：所有设了额度的模型和渠道的本期用量，以及进行中的请求数；共用额度的渠道不单列。"""
        from core.config_loader import CHANNEL_QUOTAS, MODEL_ENDPOINT_MAP
        now = time.time()
        models, channels = {}, {}
        with self._lock:
            for name, config in list(MODEL_ENDPOINT_MAP.items()):
                endpoint = config[0] if isinstance(config, list) and config else config
                quota = endpoint.get("quota") if isinstance(endpoint, dict) else None
                if isinstance(quota, dict):
                    models[name] = self._snapshot_unsafe("model:" + name, quota, now)
            for channel_id, entry in list(CHANNEL_QUOTAS.items()):
                if entry["owner"] == channel_id:
                    channels[channel_id] = self._snapshot_unsafe("channel:" + channel_id, entry["quota"], now)
        return {"models": models, "channels": channels}

    def _snapshot_unsafe(self, key: str, config: Dict[str, Any], now: float) -> Dict[str, Any]:
        quota = usage_quota.snapshot(config, self._usage_unsafe(key, config, now), now)
        quota["in_flight"] = self._inflight.get(key, 0)
        return quota

    def forget(self, subject: str) -> None:
        """清零一份额度的用量（重置用量，或额度被设置、取消、删除时）。subject 形如 model:名称、channel:id。"""
        with self._lock:
            if self._usage.pop(subject, None) is not None:
                self._dirty = True

    def rename_model(self, old: str, new: str) -> None:
        with self._lock:
            usage = self._usage.pop("model:" + old, None)
            if usage is not None:
                self._usage["model:" + new] = usage
                self._dirty = True

    def save_if_dirty(self) -> None:
        """原子写盘；失败时重新置脏，等后台任务下次重试。"""
        with self._save_lock:
            with self._lock:
                if not self._dirty:
                    return
                payload = json.dumps(self._usage, ensure_ascii=False, indent=2)
                self._dirty = False
            tmp_path = USAGE_FILE + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp_path, USAGE_FILE)
            except OSError as error:
                logger.error(f"[ModelQuota] 保存 '{USAGE_FILE}' 失败: {error}")
                with self._lock:
                    self._dirty = True


model_quota = ModelQuotaManager()
