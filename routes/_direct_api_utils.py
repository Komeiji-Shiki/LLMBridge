"""
Direct API 工具函数
- 重试判断 / 错误映射 / SSE行提取 / Token计算 / 提示词注入
"""
import asyncio
import json
import logging
import os
import time
from core.endpoint_observer import observe_credential
from typing import Any, Dict, Optional

from fastapi import HTTPException
from fastapi.responses import Response

from core.config_loader import MODEL_ROUND_ROBIN_LOCK

logger = logging.getLogger(__name__)


def append_tool_call_delta(tool_call_accumulator: dict, tool_calls):
    """聚合 OpenAI 兼容流式响应中的 tool_calls 增量。"""
    if not tool_calls:
        return

    if isinstance(tool_calls, dict):
        tool_calls = [tool_calls]
    if not isinstance(tool_calls, list):
        return

    for position, tool_call in enumerate(tool_calls):
        if not isinstance(tool_call, dict):
            continue

        index = tool_call.get("index", position)
        accumulator_key = str(index)
        current = tool_call_accumulator.setdefault(accumulator_key, {"index": index})

        tool_call_id = tool_call.get("id")
        if tool_call_id:
            current["id"] = tool_call_id

        tool_call_type = tool_call.get("type")
        if tool_call_type:
            current["type"] = tool_call_type
        elif "type" not in current:
            current["type"] = "function"

        function_delta = tool_call.get("function")
        if isinstance(function_delta, dict):
            function = current.setdefault("function", {})

            function_name = function_delta.get("name")
            if function_name:
                existing_name = function.get("name", "")
                if not existing_name:
                    function["name"] = function_name
                elif existing_name != function_name and not existing_name.endswith(function_name):
                    function["name"] = existing_name + function_name

            if "arguments" in function_delta:
                arguments_part = function_delta.get("arguments")
                if arguments_part is not None:
                    function["arguments"] = f"{function.get('arguments', '')}{arguments_part}"

        for key, value in tool_call.items():
            if key in {"index", "id", "type", "function"}:
                continue
            if value is not None:
                current[key] = value


def finalize_tool_calls(tool_call_accumulator: dict):
    """将聚合后的 tool_calls 转成稳定列表，空结果返回 None。"""
    if not tool_call_accumulator:
        return None

    def _sort_key(item):
        key, value = item
        index = value.get("index", key) if isinstance(value, dict) else key
        try:
            return int(index)
        except (TypeError, ValueError):
            return 0

    result = []
    for _, tool_call in sorted(tool_call_accumulator.items(), key=_sort_key):
        if not isinstance(tool_call, dict):
            continue
        clean_call = {key: value for key, value in tool_call.items() if value is not None}
        clean_call.setdefault("type", "function")
        result.append(clean_call)

    return result or None


def extract_tool_calls_from_message(message: dict):
    """从非流式 assistant message 中提取 tool_calls，兼容 legacy function_call。"""
    if not isinstance(message, dict):
        return None

    tool_calls = message.get("tool_calls")
    if tool_calls:
        accumulator = {}
        append_tool_call_delta(accumulator, tool_calls)
        return finalize_tool_calls(accumulator) or tool_calls

    function_call = message.get("function_call")
    if isinstance(function_call, dict):
        return [{"type": "function", "function": function_call}]

    return None


def build_response_message(content, reasoning_content=None, tool_calls=None):
    """构建用于监控面板展示的 assistant 响应消息。"""
    message: Dict[str, Any] = {"role": "assistant"}

    if reasoning_content:
        message["reasoning_content"] = reasoning_content

    if tool_calls:
        message["content"] = content if content else None
        message["tool_calls"] = tool_calls
    else:
        message["content"] = content or ""

    return message


# ============================================================
# API Key 轮询
# ============================================================

# 🔧 修复：API key 轮询使用独立的索引字典。
# 旧版与 endpoint 轮询共用 MODEL_ROUND_ROBIN_INDEX[model_name]，
# 同一模型既配置多 endpoint 又配置多 key 时，两种轮询（模数不同）
# 互相覆盖同一个计数器，轮询顺序被彼此打乱。
_API_KEY_ROUND_ROBIN_INDEX: dict = {}


@observe_credential
async def get_round_robin_api_key(model_name: str, api_key_config) -> str:
    """获取轮询后的 API key（逐 key 轮询）"""
    if isinstance(api_key_config, str):
        return api_key_config

    if isinstance(api_key_config, list) and len(api_key_config) > 0:
        valid_keys = [k for k in api_key_config if k and k.strip()]
        if not valid_keys:
            return ""

        async with MODEL_ROUND_ROBIN_LOCK:
            current_index = _API_KEY_ROUND_ROBIN_INDEX.get(model_name, 0)
            selected_key = valid_keys[current_index % len(valid_keys)]
            _API_KEY_ROUND_ROBIN_INDEX[model_name] = (current_index + 1) % len(valid_keys)

        logger.debug(f"[API_ROUND_ROBIN] 模型 '{model_name}' 使用 key 索引 {current_index}/{len(valid_keys)}")
        return selected_key

    return ""


# ============================================================
# Sticky API Key 轮询（粘性轮询 + 冷却期）
# ============================================================

_STICKY_STATE_FILE = "api_key_sticky_state.json"
_STICKY_KEY_STATE: dict = {}          # { model_name: { "current": key_value, "cooldowns": { key_value: cooldown_until_ts } } }
_STICKY_STATE_LOCK = asyncio.Lock()
_STICKY_STATE_DIRTY = False
_STICKY_SAVE_PENDING = False          # 防止并发保存任务堆积


def _load_sticky_state():
    """从文件加载 sticky 轮询状态"""
    global _STICKY_KEY_STATE
    if not os.path.exists(_STICKY_STATE_FILE):
        return
    try:
        with open(_STICKY_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.loads(f.read())
        if isinstance(data, dict):
            _STICKY_KEY_STATE = data
            logger.info(f"[STICKY_KEY] ✅ 已加载 sticky 状态 ({len(_STICKY_KEY_STATE)} 个模型)")
    except Exception as e:
        logger.error(f"[STICKY_KEY] ❌ 加载 sticky 状态失败: {e}")


def _save_sticky_state():
    """持久化 sticky 轮询状态到文件"""
    global _STICKY_STATE_DIRTY
    try:
        tmp_path = _STICKY_STATE_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(_STICKY_KEY_STATE, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, _STICKY_STATE_FILE)
        _STICKY_STATE_DIRTY = False
        logger.debug("[STICKY_KEY] 💾 sticky 状态已保存")
    except Exception as e:
        logger.error(f"[STICKY_KEY] ❌ 保存 sticky 状态失败: {e}")


def _save_sticky_if_dirty():
    """仅在状态变脏时保存"""
    if _STICKY_STATE_DIRTY:
        _save_sticky_state()


async def mark_sticky_key_cooldown(model_name: str, api_key_config, key_value: str, cooldown_seconds: int = 172800):
    """将指定的 key 标记为冷却状态。

    Args:
        model_name: 模型名称（作为命名空间）
        api_key_config: api_key 或 api_keys 配置
        key_value: 被标记冷却的 key 值
        cooldown_seconds: 冷却时长（秒），默认 172800（48小时）
    """
    if not key_value or cooldown_seconds <= 0:
        return

    valid_keys = _get_valid_keys(api_key_config)
    if key_value not in valid_keys:
        return

    cooldown_until = time.time() + cooldown_seconds

    async with _STICKY_STATE_LOCK:
        state = _STICKY_KEY_STATE.setdefault(model_name, {"current": None, "cooldowns": {}})
        state["cooldowns"][key_value] = cooldown_until
        # 如果当前粘住的 key 就是被冷却的 key，清掉 current 让它下次重新选
        if state.get("current") == key_value:
            state["current"] = None
        global _STICKY_STATE_DIRTY
        _STICKY_STATE_DIRTY = True

    logger.warning(
        f"[STICKY_KEY] 🔒 模型 '{model_name}' 的 key '{_mask_key(key_value)}' 已进入冷却期 "
        f"({cooldown_seconds // 3600}小时，至 {time.strftime('%Y-%m-%d %H:%M', time.localtime(cooldown_until))})"
    )
    # 异步保存（不阻塞主流程）
    asyncio.create_task(_async_save_sticky_state())


async def set_sticky_current_key(model_name: str, key_value: str) -> bool:
    """手动将指定 key 设为 sticky 轮询的当前 key（并清除其冷却）。

    用于管理面板「查余额后自动粘性到余额最多的 key」等操作：
    被指定的 key 即使正处于冷却期也会一并解除冷却，
    确保下一次请求真正使用它（否则 get_sticky_api_key 会因冷却而重新选 key）。

    Args:
        model_name: 模型名称（作为命名空间）
        key_value: 要设为 sticky current 的 key 值

    Returns:
        是否设置成功
    """
    if not key_value:
        return False

    async with _STICKY_STATE_LOCK:
        state = _STICKY_KEY_STATE.setdefault(model_name, {"current": None, "cooldowns": {}})
        cooldowns: dict = state.get("cooldowns", {})
        if key_value in cooldowns:
            del cooldowns[key_value]
            logger.info(f"[STICKY_KEY] ✅ 模型 '{model_name}' 的 key '{_mask_key(key_value)}' 冷却已手动解除")
        state["current"] = key_value
        global _STICKY_STATE_DIRTY
        _STICKY_STATE_DIRTY = True

    logger.info(f"[STICKY_KEY] 🎯 模型 '{model_name}' 的 sticky current 已手动设为 '{_mask_key(key_value)}'")
    # 异步保存（不阻塞主流程）
    asyncio.create_task(_async_save_sticky_state())
    return True


async def _async_save_sticky_state():
    """异步保存 sticky 状态（去重：避免并发保存任务堆积）"""
    global _STICKY_SAVE_PENDING
    if _STICKY_SAVE_PENDING:
        return
    _STICKY_SAVE_PENDING = True
    try:
        await asyncio.to_thread(_save_sticky_state)
    except Exception:
        pass
    finally:
        _STICKY_SAVE_PENDING = False


def _get_valid_keys(api_key_config) -> list:
    """从配置中提取有效的 key 列表"""
    if isinstance(api_key_config, str):
        return [api_key_config] if api_key_config.strip() else []
    if isinstance(api_key_config, list):
        return [k for k in api_key_config if k and k.strip()]
    return []


def _mask_key(key: str) -> str:
    """脱敏显示 key"""
    if len(key) <= 8:
        return "***"
    return key[:4] + "..." + key[-4:]


async def get_sticky_api_key(model_name: str, api_key_config, cooldown_seconds: int = 172800) -> str:
    """粘性轮询：优先使用上一次成功调用的 key，直到它被冷却。

    策略：
    1. 如果 current key 存在且不在冷却期 → 返回它
    2. 如果 current key 存在但冷却已过期 → 清除冷却，返回它
    3. 如果 current key 不存在或处于冷却期 → 找第一个不在冷却期的 key
    4. 如果所有 key 都在冷却期 → 返回冷却最早到期的 key（降级）

    Args:
        model_name: 模型名称
        api_key_config: api_key 或 api_keys 配置
        cooldown_seconds: 冷却时长（秒）

    Returns:
        选中的 API key 字符串
    """
    valid_keys = _get_valid_keys(api_key_config)
    if not valid_keys:
        return ""
    if len(valid_keys) == 1:
        return valid_keys[0]

    now = time.time()

    async with _STICKY_STATE_LOCK:
        state = _STICKY_KEY_STATE.setdefault(model_name, {"current": None, "cooldowns": {}})
        cooldowns: dict = state.get("cooldowns", {})

        # 清理已过期的冷却
        expired = [k for k, until in cooldowns.items() if now >= until]
        for k in expired:
            del cooldowns[k]
            logger.info(f"[STICKY_KEY] ✅ 模型 '{model_name}' 的 key '{_mask_key(k)}' 冷却期已过，恢复可用")
        if expired:
            global _STICKY_STATE_DIRTY
            _STICKY_STATE_DIRTY = True

        current_key = state.get("current")

        # 情况 1/2：current key 可用
        if current_key and current_key in valid_keys:
            if current_key not in cooldowns:
                return current_key

        # 情况 3/4：需要选择新 key
        # 优先选第一个不在冷却期的 key
        for k in valid_keys:
            if k not in cooldowns:
                state["current"] = k
                _STICKY_STATE_DIRTY = True
                logger.info(f"[STICKY_KEY] 🔀 模型 '{model_name}' 切换到 key '{_mask_key(k)}'")
                return k

        # 所有 key 都在冷却期 → 选最早到期的
        best_key = min(valid_keys, key=lambda k: cooldowns.get(k, 0))
        best_cooldown = cooldowns.get(best_key, 0)
        remaining = max(0, best_cooldown - now)
        logger.warning(
            f"[STICKY_KEY] ⚠️ 模型 '{model_name}' 所有 {len(valid_keys)} 个 key 均在冷却期，"
            f"降级使用 '{_mask_key(best_key)}'（剩余冷却 {remaining / 3600:.1f} 小时）"
        )
        state["current"] = best_key
        return best_key


@observe_credential
async def get_api_key(model_name: str, api_key_config, strategy: str = "round_robin", cooldown_seconds: int = 172800) -> str:
    """统一的 API key 获取入口，根据策略分发。

    Args:
        model_name: 模型名称
        api_key_config: api_key 或 api_keys 配置
        strategy: "round_robin"（默认）或 "sticky"
        cooldown_seconds: sticky 策略的冷却时长（秒）

    Returns:
        选中的 API key 字符串
    """
    if strategy == "sticky":
        return await get_sticky_api_key(model_name, api_key_config, cooldown_seconds)
    return await get_round_robin_api_key(model_name, api_key_config)


def is_quota_exceeded(status_code: int, response_body: Optional[str] = None) -> bool:
    """判断响应是否表示额度/quota 不足（需要触发 sticky 冷却）。

    检测条件：
    - HTTP 402 Payment Required（余额不足）
    - HTTP 429 Too Many Requests（速率限制）
    - 响应体包含 insufficient balance / quota exceeded 等关键词
    """
    # 402 Payment Required：很多 API 用这个表示余额不足/欠费
    if status_code == 402:
        return True
    if status_code == 429:
        return True

    if response_body:
        lower_body = response_body.lower()
        quota_keywords = [
            "insufficient balance", "insufficient quota",
            "out of credits", "no credits", "run out of",
            "quota exceeded", "quota limit", "quota exceeded",
            "rate limit exceeded", "too many requests", "resource exhausted",
            "billing account", "payment required",
            "balance insufficient", "not enough",
        ]
        for kw in quota_keywords:
            if kw in lower_body:
                return True

    return False


# 启动时加载持久化状态
_load_sticky_state()


# ============================================================
# 自动重试配置
# ============================================================

def normalize_auto_retry_config(endpoint_config: dict) -> dict:
    """标准化模型级自动重试配置"""
    raw_config = endpoint_config.get("auto_retry", {}) if isinstance(endpoint_config, dict) else {}
    if not isinstance(raw_config, dict):
        raw_config = {}

    def _to_int(value, default_value: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default_value

    def _to_float(value, default_value: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default_value

    retry_on_402 = bool(raw_config.get("retry_on_402", True))
    retry_on_429 = bool(raw_config.get("retry_on_429", True))
    retry_on_503 = bool(raw_config.get("retry_on_503", True))

    legacy_codes = raw_config.get("retry_on_status_codes")
    if isinstance(legacy_codes, list):
        normalized_codes = set()
        for code in legacy_codes:
            try:
                normalized_codes.add(int(code))
            except (TypeError, ValueError):
                continue
        if normalized_codes:
            retry_on_402 = 402 in normalized_codes
            retry_on_429 = 429 in normalized_codes
            retry_on_503 = 503 in normalized_codes

    retry_on_other_errors = bool(raw_config.get("retry_on_other_errors", raw_config.get("retry_on_errors", False)))

    return {
        "enabled": bool(raw_config.get("enabled", False)),
        "max_retries": max(0, _to_int(raw_config.get("max_retries", 2), 2)),
        "retry_delay_seconds": max(0.0, _to_float(raw_config.get("retry_delay_seconds", 2), 2.0)),
        "retry_on_402": retry_on_402,
        "retry_on_429": retry_on_429,
        "retry_on_503": retry_on_503,
        "retry_on_other_errors": retry_on_other_errors
    }


# ============================================================
# HTTP 状态码映射
# ============================================================

def normalize_error_status_code(raw_status) -> int:
    """将各种错误码/状态字符串统一映射为HTTP状态码"""
    if isinstance(raw_status, int):
        return raw_status if 100 <= raw_status <= 599 else 0

    if isinstance(raw_status, str):
        stripped = raw_status.strip()
        if stripped.isdigit():
            numeric = int(stripped)
            return numeric if 100 <= numeric <= 599 else 0

        status_map = {
            "RESOURCE_EXHAUSTED": 429,
            "TOO_MANY_REQUESTS": 429,
            "UNAVAILABLE": 503,
            "SERVICE_UNAVAILABLE": 503,
            "DEADLINE_EXCEEDED": 504
        }
        return status_map.get(stripped.upper(), 0)

    return 0


def extract_retry_status_candidates(status_code: int, payload: Optional[dict] = None) -> set:
    """从HTTP状态和错误载荷中提取可用于重试判断的状态码集合"""
    status_candidates = set()

    if isinstance(status_code, int) and status_code >= 400:
        status_candidates.add(status_code)

    if not isinstance(payload, dict):
        return status_candidates

    for key in ("code", "status"):
        normalized = normalize_error_status_code(payload.get(key))
        if normalized:
            status_candidates.add(normalized)

    error_obj = payload.get("error")
    error_type = None
    if isinstance(error_obj, dict):
        for key in ("code", "status"):
            normalized = normalize_error_status_code(error_obj.get(key))
            if normalized:
                status_candidates.add(normalized)
        error_type = error_obj.get("type")
    elif isinstance(error_obj, str):
        error_type = error_obj

    if isinstance(payload.get("type"), str) and not error_type:
        error_type = payload.get("type")

    if isinstance(error_type, str):
        error_type_map = {
            "invalid_request_error": 400,
            "authentication_error": 401,
            "permission_error": 403,
            "rate_limit_error": 429,
            "too_many_requests": 429,
            "service_unavailable_error": 503,
            "server_error": 503,
            "overloaded_error": 503
        }
        mapped = error_type_map.get(error_type.strip().lower())
        if mapped:
            status_candidates.add(mapped)

    if payload.get("scode") == "0x1":
        status_candidates.add(401)
    elif payload.get("scode") == "0x5":
        status_candidates.add(404)

    return status_candidates


def extract_json_from_response(response: Response) -> Optional[dict]:
    """从FastAPI响应对象中提取JSON载荷"""
    body = getattr(response, "body", None)
    if body is None:
        return None

    try:
        if isinstance(body, (bytes, bytearray)):
            body_text = bytes(body).decode("utf-8", errors="ignore")
        else:
            body_text = str(body)
        if not body_text.strip():
            return None
        parsed = json.loads(body_text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


# ============================================================
# 重试判断
# ============================================================

def should_retry_from_status_codes(status_codes: set, retry_config: dict) -> tuple:
    """根据状态码集合和重试配置判断是否应重试"""
    if not status_codes:
        return False, ""

    if retry_config.get("retry_on_402", True) and 402 in status_codes:
        return True, "402 Payment Required (余额/quota不足)"

    if retry_config.get("retry_on_429", True) and 429 in status_codes:
        return True, "429 Too Many Requests"

    if retry_config.get("retry_on_503", True) and 503 in status_codes:
        return True, "503 Service Unavailable"

    if retry_config.get("retry_on_other_errors", False):
        other_codes = sorted(code for code in status_codes if code not in (402, 429, 503))
        if other_codes:
            return True, f"HTTP {other_codes[0]}"

    return False, ""


def should_retry_response(response: Response, retry_config: dict) -> tuple:
    """根据响应对象判断是否应重试"""
    status_code = getattr(response, "status_code", 200)
    if not isinstance(status_code, int) or status_code < 400:
        return False, ""

    payload = extract_json_from_response(response)
    status_codes = extract_retry_status_candidates(status_code, payload)
    return should_retry_from_status_codes(status_codes, retry_config)


def should_retry_http_exception(http_exc: HTTPException, retry_config: dict) -> tuple:
    """根据HTTPException判断是否应重试"""
    detail_payload = http_exc.detail if isinstance(http_exc.detail, dict) else {"detail": str(http_exc.detail)}
    status_codes = extract_retry_status_candidates(http_exc.status_code, detail_payload)
    return should_retry_from_status_codes(status_codes, retry_config)


# ============================================================
# 错误格式处理
# ============================================================

def map_upstream_error_to_status_code(error_payload: dict, default_status_code: int = 500) -> int:
    """将上游错误对象映射为更准确的HTTP状态码"""
    if not isinstance(error_payload, dict):
        return default_status_code

    # 优先使用上游保留的原始 HTTP 状态码（_normalize_error_for_passthrough 注入）
    http_status = error_payload.get('_http_status')
    if isinstance(http_status, int) and 400 <= http_status <= 599:
        return http_status

    payload_status_codes = extract_retry_status_candidates(0, error_payload)
    candidate_codes = sorted(code for code in payload_status_codes if code >= 400)
    primary_code = candidate_codes[0] if candidate_codes else default_status_code

    error_obj = error_payload.get("error", {})
    error_type = None
    if isinstance(error_obj, dict):
        error_type = error_obj.get("type")
    elif isinstance(error_obj, str):
        error_type = error_obj

    type_map = {
        "invalid_request_error": 400,
        "authentication_error": 401,
        "permission_error": 403,
        "rate_limit_error": 429,
        "too_many_requests": 429,
        "service_unavailable_error": 503,
        "server_error": 503,
        "overloaded_error": 503
    }

    if isinstance(error_type, str):
        mapped = type_map.get(error_type.strip().lower())
        if mapped:
            return mapped

    return primary_code


def is_error_json(obj: dict) -> bool:
    """判断一个JSON对象是否为错误响应（支持多种格式）"""
    if not isinstance(obj, dict):
        return False
    if 'error' in obj and obj.get('error') not in (None, '', {}):
        return True
    if 'code' in obj and 'choices' not in obj:
        code_val = obj['code']
        has_msg = bool(obj.get('msg') or obj.get('message'))
        if has_msg:
            if isinstance(code_val, int) and code_val not in (0, 200):
                return True
            if isinstance(code_val, str) and code_val.strip() not in ('0', '200', 'ok', 'success'):
                return True
    return False


def _enrich_error_message(error_obj: dict) -> str:
    """从错误对象中提取最完整的人类可读消息。

    OpenRouter / 上游 provider 常把详细错误藏在 metadata.raw 中，
    message 字段只给一句笼统的 "Provider returned error"。这里把 metadata
    中的关键线索合并进去，让客户端能看到完整的错误原因。
    """
    message = error_obj.get('message', '')
    if not isinstance(message, str):
        message = str(message)

    metadata = error_obj.get('metadata')
    if isinstance(metadata, dict):
        raw = metadata.get('raw')
        if isinstance(raw, str) and raw.strip():
            # 把 raw 追加到 message 后面，用分隔符区分
            message = f"{message} — {raw.strip()}"
            return message

        # 某些上游（如 Gemini）把详细信息放在 metadata 的其他字段
        provider_name = metadata.get('provider_name')
        extra_bits = []
        if provider_name:
            extra_bits.append(f"provider: {provider_name}")
        # 收集其他非空字符串字段
        for key, val in metadata.items():
            if key in ('raw', 'provider_name', 'is_byok'):
                continue
            if isinstance(val, str) and val.strip():
                extra_bits.append(f"{key}: {val}")
        if extra_bits:
            message = f"{message} ({'; '.join(extra_bits)})"

    return message


def normalize_to_openai_error(obj: dict) -> dict:
    """将非标准错误格式转换为 OpenAI 兼容的 {"error": {...}} 格式。

    支持的输入格式：
    - {"error": {"message": "...", "type": "...", "code": ...}}  → 直接返回
    - {"error": "some string"}  → 包装为 {"error": {"message": "...", ...}}
    - {"msg": "...", "code": ...}  → 包装
    """
    if 'error' in obj and obj.get('error') is not None:
        error_val = obj['error']
        if isinstance(error_val, dict):
            # 已是标准 OpenAI 错误格式 — 但 message 可能过于笼统，补齐详细信息
            enriched = dict(error_val)
            enriched['message'] = _enrich_error_message(error_val)
            result = dict(obj)
            result['error'] = enriched
            # 保留原始错误对象以便调试
            if '_original_error' not in result:
                result['_original_error'] = error_val
            return result
        # error 是字符串或其他非对象类型 → 包装成标准格式
        return {
            "error": {
                "message": str(error_val),
                "type": "upstream_error",
                "code": "unknown"
            }
        }
    msg = obj.get('msg') or obj.get('message') or str(obj)
    code = obj.get('code', 'unknown')
    return {
        "error": {
            "message": msg,
            "type": "upstream_error",
            "code": code
        },
        "_original": obj
    }


def detect_first_chunk_error(first_chunk_bytes) -> tuple:
    """检查流式首块是否为上游错误，返回 (is_error, normalized_error_json)。

    覆盖两种形态：
    - 裸 JSON 错误体（上游直接返回 JSON 而非 SSE）
    - call_api_passthrough 对非 2xx 响应输出的 "data: {错误}" SSE 包装块

    正常 SSE 流（如 data: {...chunk...} / event: message_start）返回 (False, None)。
    """
    is_error = False
    error_json = None
    try:
        decoded_chunk = first_chunk_bytes.decode('utf-8') if isinstance(first_chunk_bytes, bytes) else str(first_chunk_bytes)
        try:
            maybe_json = json.loads(decoded_chunk)
            if is_error_json(maybe_json):
                error_json = normalize_to_openai_error(maybe_json)
                is_error = True
        except json.JSONDecodeError:
            for line in decoded_chunk.splitlines():
                line = line.strip()
                if not line.startswith('data:'):
                    continue
                data = line[5:].strip()
                if not data or data == '[DONE]':
                    continue
                try:
                    maybe_json = json.loads(data)
                    if is_error_json(maybe_json):
                        error_json = normalize_to_openai_error(maybe_json)
                        is_error = True
                        break
                except json.JSONDecodeError:
                    continue
    except UnicodeDecodeError:
        is_error = False
    return is_error, error_json


# ============================================================
# 异步 Token 计算
# ============================================================

async def estimate_message_tokens_non_blocking(estimate_message_tokens_func, messages, model: str) -> int:
    """在线程池中执行消息 token 计算"""
    return await asyncio.to_thread(estimate_message_tokens_func, messages, model=model)


async def estimate_text_tokens_non_blocking(estimate_tokens_func, text: str, model: str) -> int:
    """在线程池中执行文本 token 计算"""
    return await asyncio.to_thread(estimate_tokens_func, text, model=model)


# ============================================================
# SSE 行处理
# ============================================================

def extract_complete_sse_lines(decoded_chunk: str, pending_line: str) -> tuple:
    """合并跨 chunk 的尾部残片，只返回完整的 SSE 行。

    返回 (lines, pending_line, needs_reassembly)：
    - lines: 完整逻辑行列表（不含行尾换行符），重组发送时每行须补 '\\n'
    - pending_line: 尾部未完成的半行，缓冲到下一个 chunk
    - needs_reassembly: 本次消费了旧缓冲或产生了新缓冲。为 True 时调用方
      不得原样转发本次输入字节（输入字节与重组行已不一致），必须由 lines 重组。

    🔧 修复说明：旧版第三个返回值只反映"输出侧是否缓冲了尾部"，漏掉
    "输入侧消费了 pending"的情况——上一 chunk 被扣下的半行会永久丢失、
    当前 chunk 的前半段原样转发，SSE 流直接损坏。此前该缺陷被上游
    call_api_passthrough 按完整事件切块所掩盖，现在不再依赖该隐式契约。
    """
    had_pending = bool(pending_line)
    if pending_line:
        decoded_chunk = pending_line + decoded_chunk
        pending_line = ""

    if not decoded_chunk:
        return [], pending_line, had_pending

    lines = decoded_chunk.split('\n')
    needs_reassembly = had_pending

    if decoded_chunk.endswith('\n'):
        # split 的尾部空串是切分产物而非逻辑行，丢弃以保证"每行+\n"重组语义
        lines.pop()
    else:
        pending_line = lines.pop()
        needs_reassembly = True

    return lines, pending_line, needs_reassembly


# ============================================================
# 系统提示词注入
# ============================================================

def inject_fake_conversation(messages: list, fake_conversation: list, insert_at: Optional[int] = None) -> list:
    """伪造整轮/多轮对话历史注入

    将伪造的历史对话插入到消息列表，让上游模型误以为对话已经推进到某个阶段。
    支持 assistant 消息携带 reasoning_content（DeepSeek 历史思维链回传），
    以及 tool 角色消息携带 tool_call_id（带工具调用的多轮拼接）。

    fake_conversation: [
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "...", "reasoning_content": "..."},
        {"role": "tool", "tool_call_id": "call_xxx", "content": "..."},
    ]
    insert_at: 显式插入索引；None 时自动定位到开头的连续 system 消息之后。
    """
    if not fake_conversation:
        return messages

    msgs = messages.copy()

    if insert_at is None:
        insert_at = 0
        for i, m in enumerate(msgs):
            if isinstance(m, dict) and m.get("role") == "system":
                insert_at = i + 1
            else:
                break

    for item in reversed(fake_conversation):
        if not isinstance(item, dict):
            continue
        role = item.get("role") or "assistant"
        new_msg = {"role": role, "content": item.get("content", "")}
        reasoning = item.get("reasoning_content")
        if reasoning and role == "assistant":
            new_msg["reasoning_content"] = reasoning
        tool_call_id = item.get("tool_call_id")
        if tool_call_id and role == "tool":
            new_msg["tool_call_id"] = tool_call_id
        msgs.insert(insert_at, new_msg)

    logger.info(f"[SYSTEM_INJECT] 伪造对话注入：{len(fake_conversation)} 条消息 @ index {insert_at}")
    return msgs


def inject_system_prompt(messages: list, injection_config: dict, convert_system_to_user: bool = False) -> list:
    """系统提示词注入 + 伪造对话历史注入"""
    if not injection_config or not injection_config.get("enabled", False):
        return messages

    content = injection_config.get("content", "").strip()
    fake_conversation = injection_config.get("fake_conversation") or []
    if not content and not fake_conversation:
        return messages

    position = injection_config.get("position", "before_system")
    messages = messages.copy()

    if convert_system_to_user:
        if content:
            logger.warning(f"[SYSTEM_INJECT] 检测到同时启用了 System转User，将使用 user 角色注入伪装内容")
            messages.insert(0, {"role": "user", "content": content})
            messages.insert(1, {"role": "assistant", "content": "Understood. I'll follow these instructions."})
            logger.info(f"[SYSTEM_INJECT] 兼容模式：以 user+assistant 对话形式注入 {len(content)} 字符")
        # 伪造对话插在兼容消息之后（无 content 时则插在最前）
        return inject_fake_conversation(messages, fake_conversation, insert_at=2 if content else 0)

    if content:
        system_index = -1
        for i, msg in enumerate(messages):
            if isinstance(msg, dict) and msg.get("role") == "system":
                system_index = i
                break

        if position == "replace_system":
            messages = [msg for msg in messages if msg.get("role") != "system"]
            messages.insert(0, {"role": "system", "content": content})
            logger.info(f"[SYSTEM_INJECT] 替换模式：用注入内容替换了所有 system 消息")

        elif position == "before_system":
            if system_index >= 0:
                original_content = messages[system_index].get("content", "")
                if isinstance(original_content, str):
                    messages[system_index]["content"] = content + "\n\n" + original_content
                else:
                    messages[system_index]["content"] = [{"type": "text", "text": content}] + (
                        original_content if isinstance(original_content, list) else [{"type": "text", "text": str(original_content)}]
                    )
                logger.info(f"[SYSTEM_INJECT] 前置模式：在 system 消息前插入 {len(content)} 字符")
            else:
                messages.insert(0, {"role": "system", "content": content})
                logger.info(f"[SYSTEM_INJECT] 前置模式：创建新的 system 消息")

        elif position == "after_system":
            if system_index >= 0:
                original_content = messages[system_index].get("content", "")
                if isinstance(original_content, str):
                    messages[system_index]["content"] = original_content + "\n\n" + content
                else:
                    messages[system_index]["content"] = (
                        original_content if isinstance(original_content, list) else [{"type": "text", "text": str(original_content)}]
                    ) + [{"type": "text", "text": content}]
                logger.info(f"[SYSTEM_INJECT] 后置模式：在 system 消息后追加 {len(content)} 字符")
            else:
                messages.insert(0, {"role": "system", "content": content})
                logger.info(f"[SYSTEM_INJECT] 后置模式：创建新的 system 消息")

    return inject_fake_conversation(messages, fake_conversation)


# ============================================================
# 思考模型伪造思维链：自动注入占位工具
# ============================================================

# DeepSeek 等思考模型在「携带 tools 参数」的请求中，会把历史 assistant 消息的
# reasoning_content 拼接进上下文；无工具调用时历史思维链会被忽略。
# 为了让伪造的思维链真正生效，若伪造对话里含 reasoning_content 且下游未带工具，
# 注入一个精简、不会被主动调用的占位工具。
_REASONING_NOOP_TOOL = {
    "type": "function",
    "function": {
        "name": "noop",
        "description": "Reserved placeholder. Never call this tool.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def ensure_reasoning_noop_tool(openai_req: dict, injection_config: dict) -> dict:
    """确保思考模型请求携带 tools，以触发历史 reasoning_content 拼接。

    仅当伪造对话中存在 assistant 消息携带 reasoning_content，且请求本身没有
    tools 时，才注入一个精简的占位工具。下游已有工具则保持原样。
    """
    if not isinstance(injection_config, dict):
        return openai_req

    fake_conversation = injection_config.get("fake_conversation") or []
    has_reasoning = any(
        isinstance(m, dict)
        and m.get("role") == "assistant"
        and m.get("reasoning_content")
        for m in fake_conversation
    )
    if not has_reasoning:
        return openai_req

    tools = openai_req.get("tools")
    if tools:
        # 下游已带工具，尊重原样，避免覆盖
        return openai_req

    openai_req["tools"] = [_REASONING_NOOP_TOOL]
    logger.info("[SYSTEM_INJECT] 伪造思维链检测到，注入占位工具以触发历史 reasoning_content 拼接")
    return openai_req
