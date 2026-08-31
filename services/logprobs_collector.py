"""DeepSeek 官方 API logprobs 蒸馏数据采集。

设计目标是在 OpenAI-compatible 透传链路里做三件事：
1. 发给 DeepSeek 官方前补齐 `logprobs=true` 与 `top_logprobs`；
2. 原样把响应继续转发给客户端，同时在代理层捕获 reasoning/content 两路 token 分布；
3. 按天落成 raw + normalized 两层 JSONL，方便后续 tokenizer 对齐与稀疏 KD。

注意：这里保存的是上游返回的 sparse top-logprobs，不是完整 vocab logits。
"""
import asyncio
import copy
import fnmatch
import gzip
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from core.config_loader import CONFIG

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_TOP_LOGPROBS = 20
DEFAULT_OUTPUT_DIR = "data/deepseek-logprobs"
DEFAULT_API_HOSTS = ("api.deepseek.com",)
_FILE_APPEND_LOCK = threading.Lock()

DEFAULT_COLLECTION_CONFIG = {
    "enabled": True,
    "output_dir": DEFAULT_OUTPUT_DIR,
    "api_base_hosts": list(DEFAULT_API_HOSTS),
    "models": [],
    "top_logprobs": 20,
    "force_logprobs": True,
    "force_top_logprobs": True,
    "strip_logprobs_from_client": True,
    "record_raw_stream": True,
    "write_normalized": True,
    "redact_image_data": True,
    "require_logprobs_to_write": True,
    "compress": True,
    "candidate_finish_reasons": ["stop"],
    "tool_call_finish_reasons": ["tool_calls"],
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    return bool(value)


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _as_string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    text = str(value)
    return [text] if text.strip() else []


def get_collection_config() -> dict:
    """读取 `deepseek_logprobs` 配置并补齐默认值。"""
    raw = CONFIG.get("deepseek_logprobs", {})
    if not isinstance(raw, dict):
        raw = {}

    cfg = dict(DEFAULT_COLLECTION_CONFIG)
    cfg.update(raw)

    candidate_reasons = _as_string_list(cfg.get("candidate_finish_reasons")) or ["stop"]
    tool_reasons = _as_string_list(cfg.get("tool_call_finish_reasons")) or ["tool_calls"]
    hosts = _as_string_list(cfg.get("api_base_hosts")) or list(DEFAULT_API_HOSTS)

    return {
        "enabled": _as_bool(cfg.get("enabled"), True),
        "output_dir": str(cfg.get("output_dir") or DEFAULT_OUTPUT_DIR),
        "api_base_hosts": [host.lower().strip() for host in hosts if host.strip()],
        "models": _as_string_list(cfg.get("models")),
        "top_logprobs": _as_int(cfg.get("top_logprobs"), 20, 1, MAX_TOP_LOGPROBS),
        "force_logprobs": _as_bool(cfg.get("force_logprobs"), True),
        "force_top_logprobs": _as_bool(cfg.get("force_top_logprobs"), True),
        "strip_logprobs_from_client": _as_bool(cfg.get("strip_logprobs_from_client"), True),
        "record_raw_stream": _as_bool(cfg.get("record_raw_stream"), True),
        "write_normalized": _as_bool(cfg.get("write_normalized"), True),
        "redact_image_data": _as_bool(cfg.get("redact_image_data"), True),
        "require_logprobs_to_write": _as_bool(cfg.get("require_logprobs_to_write"), True),
        "compress": _as_bool(cfg.get("compress"), True),
        "candidate_finish_reasons": [reason.lower() for reason in candidate_reasons],
        "tool_call_finish_reasons": [reason.lower() for reason in tool_reasons],
    }


def _host_matches(host: str, allowed_hosts: Iterable[str]) -> bool:
    host = (host or "").lower().strip(".")
    if not host:
        return False
    for allowed in allowed_hosts:
        cleaned = str(allowed).lower().strip(".")
        if not cleaned:
            continue
        if host == cleaned or host.endswith("." + cleaned):
            return True
    return False


def is_deepseek_official_api_base(api_base_url: Optional[str], cfg: Optional[dict] = None) -> bool:
    cfg = cfg or get_collection_config()
    try:
        host = urlparse(api_base_url or "").hostname or ""
    except ValueError:
        host = ""
    return _host_matches(host, cfg.get("api_base_hosts") or [])


def _model_matches(models: Iterable[str], *names: Any) -> bool:
    candidates = [str(name).lower() for name in names if name]
    if not candidates:
        return False
    patterns = [str(pattern).lower() for pattern in models if pattern]
    if patterns:
        return any(fnmatch.fnmatch(candidate, pattern) for candidate in candidates for pattern in patterns)
    return any("deepseek" in candidate for candidate in candidates)


def should_collect(cfg: dict, api_base_url: Optional[str], target_model_id: str,
                   model_name: str, endpoint_config: Optional[dict]) -> bool:
    if not cfg.get("enabled"):
        return False

    endpoint_config = endpoint_config if isinstance(endpoint_config, dict) else {}
    override = endpoint_config.get("collect_logprobs")
    if override is False:
        return False

    official = is_deepseek_official_api_base(api_base_url, cfg)
    model_ok = _model_matches(
        cfg.get("models") or [],
        target_model_id,
        model_name,
        endpoint_config.get("display_name"),
        endpoint_config.get("model_id"),
    )
    if override is True:
        return official or model_ok
    return official and model_ok


def _is_chat_completion_endpoint(endpoint_path: Optional[str]) -> bool:
    path = (endpoint_path or "").lower().strip()
    return path.endswith("/chat/completions") or path.endswith("/completions")


def inject_logprobs_into_request(request_body: dict, cfg: dict) -> bool:
    """给上游 Chat Completions 请求补齐 logprobs，返回是否修改了请求体。"""
    if not isinstance(request_body, dict) or not cfg.get("force_logprobs", True):
        return False

    changed = False
    if request_body.get("logprobs") is not True:
        request_body["logprobs"] = True
        changed = True

    if cfg.get("force_top_logprobs", True):
        desired = _as_int(cfg.get("top_logprobs"), 20, 1, MAX_TOP_LOGPROBS)
        current = request_body.get("top_logprobs")
        try:
            current_int = int(current)
        except (TypeError, ValueError):
            current_int = None
        if current_int != desired:
            request_body["top_logprobs"] = desired
            changed = True

    return changed


def _redact_base64_data_url(url: str) -> str:
    if not isinstance(url, str) or not url.startswith("data:") or "," not in url:
        return url
    meta, data = url.split(",", 1)
    return f"{meta},<redacted base64 len={len(data)}>"


class LogprobsCollector:
    """单次 DeepSeek 请求的 logprobs 旁路采集器。

    capture_stream_event / capture_non_stream_response 都必须在代理层改写响应
    字段之前调用；如果 strip_from_client=True，它们会在保存原始 logprobs 后
    从发给客户端的响应体里删除 `choices[].logprobs`。
    """

    def __init__(self, *, request_id: str, model_name: str, target_model_id: str,
                 display_name: str, cfg: dict, strip_from_client: bool,
                 endpoint_config: Optional[dict] = None, full_messages: Optional[list] = None):
        self.request_id = request_id
        self.model_name = model_name
        self.target_model_id = target_model_id
        self.display_name = display_name
        self.cfg = cfg
        self.strip_from_client = bool(strip_from_client)
        self.endpoint_config = endpoint_config if isinstance(endpoint_config, dict) else {}
        self.full_messages = full_messages

        self.capture_enabled = True
        self.request_snapshot: Dict[str, Any] = {}
        self.client_requested_logprobs: Any = None
        self.client_requested_top_logprobs: Any = None

        self.started_at = _iso_now()
        self.finished_at: Optional[str] = None
        self.completed = False
        self.finish_error: Optional[str] = None

        self.upstream_id: Optional[str] = None
        self.upstream_created: Optional[int] = None
        self.response_model: Optional[str] = None
        self.system_fingerprint: Optional[str] = None
        self.usage: Optional[dict] = None
        self.finish_reason: Optional[str] = None
        self.last_error: Any = None

        self.content_parts: List[str] = []
        self.reasoning_parts: List[str] = []
        self.reasoning_logprobs: List[Any] = []
        self.content_logprobs: List[Any] = []
        self.tool_calls_by_index: Dict[int, dict] = {}
        self.has_tool_calls = False

        self.raw_stream_events: List[dict] = []
        self.raw_event_count = 0
        self.logprob_event_count = 0

        self._finish_lock: Optional[asyncio.Lock] = None
        self._finished = False
        self._finishing = False

    @property
    def logprob_token_counts(self) -> Tuple[int, int]:
        return len(self.reasoning_logprobs), len(self.content_logprobs)

    def record_upstream_request(self, request_body: dict, original_request: Optional[dict]) -> None:
        self.client_requested_logprobs = original_request.get("logprobs") if isinstance(original_request, dict) else None
        self.client_requested_top_logprobs = original_request.get("top_logprobs") if isinstance(original_request, dict) else None
        self.request_snapshot = self._build_request_snapshot(request_body)

    def capture_stream_event(self, event_json: Dict[str, Any]) -> bool:
        """处理一个 SSE 事件。返回是否修改了 event_json（用于让代理层重新序列化）。"""
        if not self.capture_enabled:
            return False
        return self._capture_response_event(event_json, strip=self.strip_from_client)

    def capture_non_stream_response(self, response_json: Dict[str, Any]) -> None:
        if not self.capture_enabled:
            return
        self._capture_response_event(response_json, strip=False)

    def strip_non_stream_response(self, response_json: Dict[str, Any]) -> bool:
        if not self.capture_enabled or not self.strip_from_client:
            return False
        return self._strip_choice_logprobs(response_json)

    async def finish(self, completed: bool = False, error: Optional[str] = None) -> None:
        if not self.capture_enabled:
            return
        if self._finish_lock is None:
            self._finish_lock = asyncio.Lock()
        async with self._finish_lock:
            if self._finished or self._finishing:
                return
            self._finishing = True
            self.completed = bool(completed)
            if error and not self.finish_error:
                self.finish_error = str(error)
            self.finished_at = _iso_now()

        try:
            raw_record = self._build_raw_record()
            if raw_record:
                await self._append_record("raw", raw_record)
                self.raw_stream_events = []

            normalized_record = self._build_normalized_record()
            if normalized_record and self._should_write_normalized():
                await self._append_record("normalized", normalized_record)
        except Exception as e:
            logger.warning("[LOGPROBS_COLLECT] 采集数据写入失败 request_id=%s: %s", self.request_id[:8], e, exc_info=True)
        finally:
            self._finished = True

    def monitor_snapshot(self) -> dict:
        return {
            "enabled": True,
            "output_dir": self.cfg.get("output_dir"),
            "top_logprobs": self.cfg.get("top_logprobs"),
            "strip_logprobs_from_client": self.strip_from_client,
            "record_raw_stream": bool(self.cfg.get("record_raw_stream")),
        }

    def _build_request_snapshot(self, request_body: dict) -> dict:
        params = {
            key: copy.deepcopy(value)
            for key, value in request_body.items()
            if key not in ("messages", "logprobs", "top_logprobs")
        }
        return {
            "model": request_body.get("model"),
            "messages": self._sanitize_messages(request_body.get("messages")),
            "logprobs": request_body.get("logprobs"),
            "top_logprobs": request_body.get("top_logprobs"),
            "params": params,
        }

    def _sanitize_messages(self, messages: Any) -> Any:
        return self._sanitize_value(messages)

    def _sanitize_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            if value.get("type") == "image_url" and isinstance(value.get("image_url"), dict):
                cloned = dict(value)
                image_url = dict(cloned["image_url"])
                url = image_url.get("url")
                if isinstance(url, str) and self.cfg.get("redact_image_data", True):
                    image_url["url"] = _redact_base64_data_url(url)
                cloned["image_url"] = image_url
                return cloned

            cloned = {}
            for key, item in value.items():
                if key == "data" and isinstance(item, str) and len(item) > 1024 and self.cfg.get("redact_image_data", True):
                    cloned[key] = f"<redacted data len={len(item)}>"
                else:
                    cloned[key] = self._sanitize_value(item)
            return cloned

        if isinstance(value, list):
            return [self._sanitize_value(item) for item in value]

        return value

    def _capture_response_event(self, event_json: Dict[str, Any], *, strip: bool) -> bool:
        if not isinstance(event_json, dict):
            return False

        raw_enabled = bool(self.cfg.get("record_raw_stream", True))
        raw_event = {"seq": self.raw_event_count} if raw_enabled else None
        raw_has_content = False

        event_id = event_json.get("id")
        if isinstance(event_id, str) and event_id:
            self.upstream_id = event_id
            if raw_enabled:
                raw_event["id"] = event_id

        created = event_json.get("created")
        if created is not None:
            self.upstream_created = created
            if raw_enabled:
                raw_event["created"] = created

        response_model = event_json.get("model")
        if isinstance(response_model, str) and response_model:
            self.response_model = response_model
            if raw_enabled:
                raw_event["model"] = response_model

        fingerprint = event_json.get("system_fingerprint")
        if isinstance(fingerprint, str) and fingerprint:
            self.system_fingerprint = fingerprint
            if raw_enabled:
                raw_event["system_fingerprint"] = fingerprint

        usage = event_json.get("usage")
        if isinstance(usage, dict) and usage:
            self._update_usage(usage)
            if raw_enabled:
                raw_event["usage"] = usage
                raw_has_content = True

        error = event_json.get("error")
        if error is not None:
            self.last_error = error
            if raw_enabled:
                raw_event["error"] = error
                raw_has_content = True

        choices = event_json.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]
            self._capture_choice_text(choice)
            finish_reason = choice.get("finish_reason")
            if finish_reason:
                self.finish_reason = finish_reason
                if raw_enabled:
                    raw_event["finish_reason"] = finish_reason
                    raw_has_content = True

            logprobs = choice.get("logprobs")
            if isinstance(logprobs, dict) and logprobs:
                self.logprob_event_count += 1
                self._extend_logprobs(logprobs, "reasoning_content", self.reasoning_logprobs)
                self._extend_logprobs(logprobs, "content", self.content_logprobs)
                if raw_enabled:
                    raw_event["logprobs"] = logprobs
                    raw_has_content = True

        if raw_enabled and raw_event is not None and raw_has_content:
            self.raw_stream_events.append(raw_event)
            self.raw_event_count += 1

        if strip:
            return self._strip_choice_logprobs(event_json)
        return False

    def _capture_choice_text(self, choice: dict) -> None:
        delta = choice.get("delta")
        message = choice.get("message")
        source = delta if isinstance(delta, dict) else message if isinstance(message, dict) else None
        if not isinstance(source, dict):
            return

        content = source.get("content")
        if isinstance(content, str) and content:
            self.content_parts.append(content)

        reasoning = source.get("reasoning_content")
        if not isinstance(reasoning, str) or not reasoning:
            reasoning = source.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            self.reasoning_parts.append(reasoning)

        tool_calls = source.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            self._capture_tool_calls(tool_calls)

    def _capture_tool_calls(self, tool_calls: list) -> None:
        self.has_tool_calls = True
        for item in tool_calls:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index", 0))
            except (TypeError, ValueError):
                index = 0
            call = self.tool_calls_by_index.setdefault(index, {"index": index})
            for key in ("id", "type"):
                value = item.get(key)
                if value:
                    call[key] = value
            function = item.get("function")
            if isinstance(function, dict):
                func = call.setdefault("function", {})
                name = function.get("name")
                if isinstance(name, str) and name:
                    func["name"] = func.get("name", "") + name
                arguments = function.get("arguments")
                if isinstance(arguments, str) and arguments:
                    func["arguments"] = func.get("arguments", "") + arguments

    def _extend_logprobs(self, logprobs: dict, key: str, bucket: list) -> None:
        items = logprobs.get(key)
        if isinstance(items, list) and items:
            bucket.extend(items)

    def _update_usage(self, usage: dict) -> None:
        if self.usage is None:
            self.usage = dict(usage)
        else:
            self.usage.update(usage)

    def _strip_choice_logprobs(self, response_json: Dict[str, Any]) -> bool:
        stripped = False
        choices = response_json.get("choices") if isinstance(response_json, dict) else None
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict) and "logprobs" in choice:
                    choice.pop("logprobs", None)
                    stripped = True
        return stripped

    def _final_tool_calls(self) -> Optional[list]:
        if not self.has_tool_calls:
            return None
        return [self.tool_calls_by_index[index] for index in sorted(self.tool_calls_by_index)]

    def _response_dict(self) -> dict:
        response = {
            "id": self.upstream_id,
            "created": self.upstream_created,
            "model": self.response_model or self.target_model_id or self.model_name,
            "content": "".join(self.content_parts),
            "reasoning_content": "".join(self.reasoning_parts),
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "system_fingerprint": self.system_fingerprint,
        }
        tool_calls = self._final_tool_calls()
        if tool_calls:
            response["tool_calls"] = tool_calls
        return response

    def _training_dict(self) -> dict:
        finish_reason = (self.finish_reason or "").lower()
        if self.finish_error or self.last_error is not None:
            category = "error"
        elif finish_reason in self.cfg.get("tool_call_finish_reasons", []) or self.has_tool_calls:
            category = "tool_call"
        elif finish_reason in self.cfg.get("candidate_finish_reasons", []):
            category = "chat"
        else:
            category = finish_reason or "unknown"

        reasoning_count, content_count = self.logprob_token_counts
        completed_ok = bool(self.completed and not self.finish_error)
        candidate = (
            completed_ok
            and category == "chat"
            and self.logprob_event_count > 0
            and bool(self.content_parts or self.reasoning_parts or self.reasoning_logprobs or self.content_logprobs)
        )
        return {
            "candidate": candidate,
            "category": category,
            "completed": completed_ok,
            "reasoning_logprob_tokens": reasoning_count,
            "content_logprob_tokens": content_count,
            "finish_reason": self.finish_reason,
        }

    def _build_raw_record(self) -> Optional[dict]:
        if not self.raw_stream_events:
            return None
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "raw",
            "request_id": self.request_id,
            "timestamp": _iso_now(),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "display_name": self.display_name,
            "model": self.target_model_id or self.model_name,
            "response_model": self.response_model,
            "client_requested_logprobs": self.client_requested_logprobs,
            "client_requested_top_logprobs": self.client_requested_top_logprobs,
            "request": self.request_snapshot,
            "stream_events": self.raw_stream_events,
            "error": self.finish_error,
        }

    def _build_normalized_record(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "normalized",
            "request_id": self.request_id,
            "timestamp": _iso_now(),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "display_name": self.display_name,
            "model": self.target_model_id or self.model_name,
            "response_model": self.response_model,
            "request": self.request_snapshot,
            "response": self._response_dict(),
            "logprobs": {
                "reasoning_content": self.reasoning_logprobs,
                "content": self.content_logprobs,
            },
            "training": self._training_dict(),
            "collection": {
                "top_logprobs": self.cfg.get("top_logprobs"),
                "strip_logprobs_from_client": self.strip_from_client,
                "logprob_events": self.logprob_event_count,
                "raw_events": self.raw_event_count,
            },
            "completed": self.completed,
            "error": self.finish_error,
        }

    def _should_write_normalized(self) -> bool:
        if not self.cfg.get("write_normalized", True):
            return False
        if self.cfg.get("require_logprobs_to_write", True) and self.logprob_event_count == 0:
            return False
        return bool(self.content_parts or self.reasoning_parts or self.reasoning_logprobs or self.content_logprobs)

    async def _append_record(self, kind: str, record: dict) -> None:
        output_dir = Path(self.cfg.get("output_dir") or DEFAULT_OUTPUT_DIR)
        suffix = "jsonl.gz" if self.cfg.get("compress") else "jsonl"
        path = output_dir / kind / f"{_utc_date()}.{suffix}"
        await asyncio.to_thread(_append_jsonl_sync, path, record)


def _append_jsonl_sync(path: Path, record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _FILE_APPEND_LOCK:
        if path.suffix == ".gz":
            with gzip.open(path, "at", encoding="utf-8") as f:
                f.write(line)
        else:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)


def create_logprobs_collector(*, passthrough_request: dict, openai_req: dict,
                              model_name: str, target_model_id: str, display_name: str,
                              api_base_url: Optional[str], endpoint_config: dict,
                              request_id: str, full_messages: Optional[list] = None,
                              endpoint_path: Optional[str] = None) -> Optional[LogprobsCollector]:
    """按配置创建 collector，并在命中时强制上游返回 logprobs。"""
    if not isinstance(passthrough_request, dict):
        return None
    if endpoint_path and not _is_chat_completion_endpoint(endpoint_path):
        return None

    cfg = get_collection_config()
    if not should_collect(cfg, api_base_url, target_model_id, model_name, endpoint_config):
        return None

    original_logprobs = openai_req.get("logprobs") if isinstance(openai_req, dict) else None
    if not cfg.get("force_logprobs", True) and not original_logprobs:
        return None

    inject_logprobs_into_request(passthrough_request, cfg)
    strip_from_client = bool(cfg.get("strip_logprobs_from_client") and not original_logprobs)
    collector = LogprobsCollector(
        request_id=request_id,
        model_name=model_name,
        target_model_id=target_model_id,
        display_name=display_name,
        cfg=cfg,
        strip_from_client=strip_from_client,
        endpoint_config=endpoint_config,
        full_messages=full_messages,
    )
    collector.record_upstream_request(passthrough_request, openai_req)
    logger.debug("[LOGPROBS_COLLECT] 已启用 DeepSeek logprobs 采集 request_id=%s", request_id[:8])
    return collector
