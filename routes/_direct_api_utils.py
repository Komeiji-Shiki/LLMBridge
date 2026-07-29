"""
Direct API 工具函数
- 重试判断 / 错误映射 / SSE行提取 / Token计算 / 提示词注入
"""
import asyncio
import json
import logging
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


async def get_round_robin_api_key(model_name: str, api_key_config) -> str:
    """获取轮询后的 API key"""
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
            retry_on_429 = 429 in normalized_codes
            retry_on_503 = 503 in normalized_codes

    retry_on_other_errors = bool(raw_config.get("retry_on_other_errors", raw_config.get("retry_on_errors", False)))

    return {
        "enabled": bool(raw_config.get("enabled", False)),
        "max_retries": max(0, _to_int(raw_config.get("max_retries", 2), 2)),
        "retry_delay_seconds": max(0.0, _to_float(raw_config.get("retry_delay_seconds", 2), 2.0)),
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

    if retry_config.get("retry_on_429", True) and 429 in status_codes:
        return True, "429 Too Many Requests"

    if retry_config.get("retry_on_503", True) and 503 in status_codes:
        return True, "503 Service Unavailable"

    if retry_config.get("retry_on_other_errors", False):
        other_codes = sorted(code for code in status_codes if code not in (429, 503))
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
    if 'error' in obj and obj.get('error') is not None:
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

def inject_system_prompt(messages: list, injection_config: dict, convert_system_to_user: bool = False) -> list:
    """系统提示词注入功能"""
    if not injection_config or not injection_config.get("enabled", False):
        return messages

    content = injection_config.get("content", "").strip()
    if not content:
        return messages

    position = injection_config.get("position", "before_system")
    messages = messages.copy()

    if convert_system_to_user:
        logger.warning(f"[SYSTEM_INJECT] 检测到同时启用了 System转User，将使用 user 角色注入伪装内容")
        messages.insert(0, {"role": "user", "content": content})
        messages.insert(1, {"role": "assistant", "content": "Understood. I'll follow these instructions."})
        logger.info(f"[SYSTEM_INJECT] 兼容模式：以 user+assistant 对话形式注入 {len(content)} 字符")
        return messages

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

    return messages
