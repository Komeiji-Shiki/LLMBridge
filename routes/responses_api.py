"""OpenAI Responses API 兼容路由。"""
from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from converters.responses_bridge import convert_responses_response_to_chat
from converters.responses_openai import (
    ResponsesRequestError,
    build_responses_error_response,
    build_responses_streaming_response,
    collect_chat_stream_response,
    convert_chat_response_to_responses,
    convert_responses_to_chat_request,
    read_response_body_bytes,
)
from core.constants import TimeoutDefaults
from core.errors import BadRequestError
from core.model_archive import is_model_archived
from modules.monitoring import monitoring_service
from utils.monitor_params import build_monitor_request_params

from . import api_routes as _chat_api
from ._direct_api_utils import (
    build_response_message,
    detect_first_chunk_error,
    estimate_message_tokens_non_blocking,
    estimate_text_tokens_non_blocking,
    get_round_robin_api_key,
    is_error_json,
    map_upstream_error_to_status_code,
    normalize_to_openai_error,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["responses-api"])


def _is_stream_response(response: Response) -> bool:
    return isinstance(response, StreamingResponse) or getattr(response, "media_type", "") == "text/event-stream"


def _parse_json_body(body: bytes) -> Dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BadRequestError("上游返回了无法解析的 JSON 响应。", code="invalid_upstream_response").to_http_exception() from exc
    if not isinstance(value, dict):
        raise BadRequestError("上游返回的响应不是 JSON 对象。", code="invalid_upstream_response").to_http_exception()
    return value


async def _read_error_response(response: Response) -> JSONResponse:
    body = await read_response_body_bytes(response)
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {"message": body.decode("utf-8", errors="replace")}
    if isinstance(payload, dict) and payload.get("error") is not None:
        return JSONResponse(status_code=response.status_code, content=payload)
    return build_responses_error_response(response.status_code, payload)


def _rewrite_sse_model(chunk: bytes, client_model: Any, upstream_model: Any) -> bytes:
    """把 SSE 事件中的 response.model 从上游模型名替换回客户端请求的模型名。

    call_api_passthrough 按 SSE 事件边界切分，chunk 内通常是完整事件，可以安全
    解析替换；个别解析失败的残留块原样透传，绝不吞字节。
    """
    if client_model == upstream_model or b'"model"' not in chunk:
        return chunk
    text = chunk.decode("utf-8", errors="replace")
    out_lines = []
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if stripped.startswith("data:"):
            payload = stripped[5:].strip()
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                changed = False
                if obj.get("model") == upstream_model:
                    obj["model"] = client_model
                    changed = True
                # Responses SSE 的 model 位于 data.response.model 嵌套对象里
                # （response.created / response.completed 等事件）
                resp_obj = obj.get("response")
                if isinstance(resp_obj, dict) and resp_obj.get("model") == upstream_model:
                    resp_obj["model"] = client_model
                    changed = True
                if changed:
                    line = "data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + line[len(stripped):]
        out_lines.append(line)
    return "".join(out_lines).encode("utf-8")


# ============================================================
# 监控辅助：responses_native 原生透传的请求日志统计
# ============================================================

_SSE_BLOCK_SPLIT_RE = re.compile(r"\r?\n\r?\n")


def _extract_message_content_text(content: Any) -> str:
    """把 message item 的 content（字符串或 part 数组）转为纯文本，图片等用占位符。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    text_parts: List[str] = []
    for part in content:
        if isinstance(part, str):
            text_parts.append(part)
        elif isinstance(part, dict):
            part_type = part.get("type")
            if part_type in ("input_text", "output_text", "text"):
                text_parts.append(str(part.get("text", "")))
            elif part_type == "refusal":
                text_parts.append(str(part.get("refusal", "")))
            elif part_type in ("input_image", "image"):
                text_parts.append("[image]")
            elif part_type in ("input_file", "file"):
                text_parts.append("[file]")
            elif part_type in ("input_audio", "audio"):
                text_parts.append("[audio]")
    return "".join(text_parts)


def _summarize_tools_for_monitor(tools: Any) -> Dict[str, Any]:
    """工具定义的监控摘要：记数量、规范哈希与工具名序列，不记全文。

    上游隐式 prompt cache 把 tools 计入缓存前缀，顺序或定义差一个字节
    就会整体 miss；日志只记 tools_count 会留下排查盲区，记 sha256 + names 补上。
    顺序参与哈希（不排序），因为工具顺序变化同样破坏缓存前缀。
    """
    if not isinstance(tools, list):
        return {}
    try:
        canonical = json.dumps(tools, ensure_ascii=False, separators=(",", ":"), sort_keys=False, default=str)
    except (TypeError, ValueError):
        return {"tools_count": len(tools)}
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    names: List[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not name and isinstance(tool.get("function"), dict):
            name = tool["function"].get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return {"tools_count": len(tools), "tools_sha256": digest, "tools_names": names}


def _extract_reasoning_summary_text(summary: Any) -> str:
    """提取 reasoning item 的 summary 文本（思维链摘要）。"""
    if not isinstance(summary, list):
        return ""
    return "".join(
        str(part.get("text", ""))
        for part in summary
        if isinstance(part, dict) and part.get("type") in ("summary_text", "text")
    )


def _responses_input_to_messages(responses_request: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把 Responses 请求转成 oai 兼容的消息列表（监控日志用）。

    Responses API 把一轮 assistant 输出拆成多个 output item。这里按会话边界把
    连续的 reasoning / assistant message / function_call 重新合并成一条消息，
    再把 function_call_output 转成 tool 消息，避免监控页把同一回复拆成多个气泡。
    """
    messages: List[Dict[str, Any]] = []
    instructions = responses_request.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    input_items = responses_request.get("input")
    if isinstance(input_items, str):
        # Responses API 允许 input 简写为字符串
        messages.append({"role": "user", "content": input_items})
        return messages
    if not isinstance(input_items, list):
        return messages

    pending_assistant: Optional[Dict[str, Any]] = None

    def ensure_pending_assistant() -> Dict[str, Any]:
        nonlocal pending_assistant
        if pending_assistant is None:
            pending_assistant = {"role": "assistant", "content": ""}
        return pending_assistant

    def flush_pending_assistant() -> None:
        nonlocal pending_assistant
        if pending_assistant is not None:
            messages.append(pending_assistant)
            pending_assistant = None

    def append_reasoning_signature(message: Dict[str, Any], signature: Any) -> None:
        """单条签名保持字符串；同一回复有多条签名时全部保留。"""
        existing = message.get("reasoning_signature")
        if existing is None:
            message["reasoning_signature"] = signature
        elif isinstance(existing, list):
            existing.append(signature)
        else:
            message["reasoning_signature"] = [existing, signature]

    for item in input_items:
        if isinstance(item, str):
            flush_pending_assistant()
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            flush_pending_assistant()
            continue

        item_type = item.get("type")
        if item_type == "message":
            role = item.get("role") or "user"
            content = _extract_message_content_text(item.get("content"))
            if role == "assistant":
                if content:
                    assistant = ensure_pending_assistant()
                    assistant["content"] = str(assistant.get("content", "")) + content
            else:
                flush_pending_assistant()
                if content:
                    messages.append({"role": role, "content": content})
        elif item_type == "reasoning":
            summary = _extract_reasoning_summary_text(item.get("summary"))
            signature = item.get("encrypted_content") or item.get("id")
            if summary or signature:
                assistant = ensure_pending_assistant()
                if summary:
                    existing_summary = assistant.get("reasoning_content")
                    assistant["reasoning_content"] = (
                        f"{existing_summary}\n{summary}" if existing_summary else summary
                    )
                if signature:
                    append_reasoning_signature(assistant, signature)
        elif item_type == "function_call":
            arguments = item.get("arguments")
            if not isinstance(arguments, str):
                arguments = json.dumps(
                    arguments, ensure_ascii=False, separators=(",", ":")
                ) if arguments is not None else "{}"
            assistant = ensure_pending_assistant()
            assistant.setdefault("tool_calls", []).append({
                "id": item.get("call_id") or item.get("id") or "",
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": arguments,
                },
            })
        elif item_type == "function_call_output":
            flush_pending_assistant()
            output = item.get("output")
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or "",
                "content": str(output) if output is not None else "",
            })
        else:
            # 不认识的 item 不能作为跨项合并的桥梁，避免误并两个独立回复。
            flush_pending_assistant()

    flush_pending_assistant()
    return messages


def _extract_responses_usage_tokens(usage: Any) -> tuple:
    """从 Responses usage 中提取 (input_tokens, output_tokens, cached_tokens)。"""
    if not isinstance(usage, dict):
        return 0, 0, 0
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    details = usage.get("input_tokens_details")
    cached_tokens = int(details.get("cached_tokens", 0) or 0) if isinstance(details, dict) else 0
    return input_tokens, output_tokens, cached_tokens


def _apply_responses_sse_event(event: Any, stats: Dict[str, Any]) -> None:
    """把单个 Responses SSE 事件累加进统计 dict。

    stats 键：content_parts / reasoning_parts / input_tokens / output_tokens /
    cached_tokens / upstream_usage / error_message / saw_completed。
    """
    if not isinstance(event, dict):
        return
    event_type = event.get("type") or event.get("_event_type") or ""
    if event_type == "response.output_text.delta":
        delta = event.get("delta")
        if isinstance(delta, str) and delta:
            stats["content_parts"].append(delta)
    elif event_type == "response.reasoning_summary_text.delta":
        delta = event.get("delta")
        if isinstance(delta, str) and delta:
            stats["reasoning_parts"].append(delta)
    elif event_type in ("response.completed", "response.incomplete"):
        # 上游已完整结束一轮（usage 可能缺席，但结束事实本身成立）；
        # Codex 等客户端拿到 completed 后会主动断开，此时不得再记为失败。
        stats["saw_completed"] = True
        response = event.get("response")
        if isinstance(response, dict) and isinstance(response.get("usage"), dict):
            stats["upstream_usage"] = response["usage"]
            (stats["input_tokens"], stats["output_tokens"],
             stats["cached_tokens"]) = _extract_responses_usage_tokens(response["usage"])
    elif event_type in ("response.failed", "error"):
        error = event.get("error")
        if error is None and isinstance(event.get("response"), dict):
            error = event["response"].get("error")
        if isinstance(error, dict):
            stats["error_message"] = error.get("message") or str(error)
        elif error is not None:
            stats["error_message"] = str(error)
        elif event_type == "error" and event.get("message"):
            # OpenAI 流式 error 事件：code/message 在事件顶层
            stats["error_message"] = str(event["message"])
        elif stats["error_message"] is None:
            stats["error_message"] = "上游 Responses 请求失败"


def _drain_sse_blocks(buffer: str) -> tuple:
    """从 SSE 文本缓冲区取出所有完整事件块，返回 (blocks, remaining_buffer)。"""
    blocks = []
    while True:
        match = _SSE_BLOCK_SPLIT_RE.search(buffer)
        if not match:
            break
        block, buffer = buffer[:match.start()], buffer[match.end():]
        blocks.append(block)
    return blocks, buffer


def _apply_sse_block(block: str, stats: Dict[str, Any]) -> None:
    """解析一个 SSE 事件块（data: 行），累计统计；[DONE] 标记流完成。"""
    for line in block.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            stats["stream_complete"] = True
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        _apply_responses_sse_event(event, stats)


async def _complete_responses_monitoring(
    *,
    monitoring_service,
    direct_api_service,
    request_id: str,
    success: bool,
    display_name: str,
    pricing_config: Dict[str, Any],
    estimate_message_tokens_func,
    estimate_tokens_func,
    full_messages,
    content: str = "",
    reasoning: str = "",
    tool_calls=None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    error: Optional[str] = None,
    upstream_usage: Optional[Dict[str, Any]] = None,
) -> None:
    """透传请求结束的统一监控落盘：token 缺失时估算，计算费用并广播。"""
    if input_tokens <= 0:
        try:
            input_tokens = await estimate_message_tokens_non_blocking(
                estimate_message_tokens_func,
                full_messages or [],
                display_name,
            )
        except Exception as exc:
            logger.warning("[RESPONSES_NATIVE] 输入 token 估算失败: %s", exc)
    if output_tokens <= 0 and (content or reasoning):
        try:
            output_tokens = await estimate_text_tokens_non_blocking(
                estimate_tokens_func,
                (reasoning or "") + (content or ""),
                display_name,
            )
        except Exception as exc:
            logger.warning("[RESPONSES_NATIVE] 输出 token 估算失败: %s", exc)

    cost_info = direct_api_service.calculate_cost(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        pricing=pricing_config,
    ) if pricing_config else {}
    monitoring_service.request_end(
        request_id=request_id,
        success=success,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        error=error,
        response_content=content or None,
        reasoning_content=reasoning or None,
        response_message=build_response_message(content, reasoning, tool_calls),
        response_tool_calls=tool_calls,
        cost_info=cost_info,
        full_messages=full_messages,
        upstream_usage=upstream_usage,
    )
    await monitoring_service.broadcast_to_monitors({
        "type": "request_end",
        "request_id": request_id,
        "success": success,
    })


async def _handle_responses_native_passthrough(
    *,
    responses_request: Dict[str, Any],
    model: str,
    endpoint_config: Dict[str, Any],
    request: Request,
):
    """responses_native 配置：/v1/responses 请求原样透传到上游 Responses API。

    与转换链路的关键区别：input 中的 reasoning item（encrypted_content / id /
    summary）不做任何解析、转换或丢弃，原样转发；上游响应也不做 Chat ↔ Responses
    双向转换，保证思维链签名等密文载体完整回传。
    """
    # ── 认证（与转换链路一致；内部调度不再重复校验）──
    try:
        _chat_api._validate_request_api_key(request, model)
    except HTTPException as exc:
        return build_responses_error_response(exc.status_code, exc.detail)

    # ── 取配置 ──
    api_base_url = endpoint_config.get("api_base_url")
    if not api_base_url:
        logger.error("[RESPONSES_NATIVE] 模型 '%s' 配置缺少 api_base_url", model)
        return build_responses_error_response(500, f"模型 '{model}' 配置缺少 api_base_url。")

    endpoint_path = (endpoint_config.get("endpoint_path") or "/responses").strip()
    if not endpoint_path.startswith("/"):
        endpoint_path = "/" + endpoint_path

    # 单 key 或 api_keys 列表轮询
    raw_api_key = endpoint_config.get("api_keys") or endpoint_config.get("api_key")
    upstream_api_key = await get_round_robin_api_key(model, raw_api_key)

    # 兼容 target_model_id（新）/ model_id（现有配置）两种字段名
    target_model_id = endpoint_config.get("target_model_id") or endpoint_config.get("model_id") or model

    direct_api_service = _chat_api._app_state.server.direct_api_service
    if not direct_api_service:
        return build_responses_error_response(503, "Direct API service not initialized")

    # ── 构造上游请求：仅替换 model，其余字段（含 reasoning item）原样保留 ──
    passthrough_body = dict(responses_request)
    passthrough_body["model"] = target_model_id

    # Codex 等客户端下发的 tools / text.format 常带递归 $ref，上游会 400：
    #   Recursive JSON schemas are not currently supported。
    # 端点配置 sanitize_recursive_schemas=true（默认）时清洗：先截断真正的
    # 递归环（非递归 $ref 原样保留，不膨胀体量），再把剩余 strict=True 全降级
    # （非 strict 下递归是放行的）。管理面板模型编辑页可按模型开关。
    if endpoint_config.get("sanitize_recursive_schemas", True):
        try:
            from utils.schema_sanitizer import (
                force_all_strict_false_responses,
                sanitize_responses_request,
            )
            washed = sanitize_responses_request(passthrough_body)
            forced = force_all_strict_false_responses(passthrough_body)
            if washed or forced:
                logger.info(
                    "[RESPONSES_NATIVE] 已处理 JSON Schema（清洗改写=%s，strict 降级数=%s）: model=%s",
                    washed, forced, model,
                )
        except Exception as exc:
            logger.warning("[RESPONSES_NATIVE] 递归 schema 清洗失败，原样透传: %s", exc)

    is_stream = bool(responses_request.get("stream", False))
    logger.info(
        "[RESPONSES_NATIVE] 原生透传: model=%s → target=%s, endpoint_path=%s, stream=%s",
        model, target_model_id, endpoint_path, is_stream,
    )

    # ── 监控：透传请求与转换链路一样记录请求日志、token 与费用 ──
    request_id = str(uuid.uuid4())
    display_name = endpoint_config.get("display_name") or model
    pricing_config = endpoint_config.get("pricing", {})
    full_messages = _responses_input_to_messages(responses_request)

    # 延迟导入避免启动开销（与 api_routes 的 direct 分支一致）
    from modules.token_counter import estimate_message_tokens, estimate_tokens

    # instructions 已转为 system 消息进入 request_messages；tools 全文巨大不进 params，
    # 但 tools 参与上游隐式 prompt cache 前缀：记规范哈希 + 工具名序列 + 数量，
    # 否则缓存掉链时无法排除工具定义变化（只记数量是排查盲区）。
    # 取清洗后实际发送的 passthrough_body，而非清洗前的原始请求。
    monitor_extra = {"upstream_model": target_model_id, "endpoint_path": endpoint_path}
    monitor_extra.update(_summarize_tools_for_monitor(passthrough_body.get("tools")))

    monitoring_service.request_start(
        request_id=request_id,
        model=display_name,
        messages_count=len(full_messages),
        session_id=None,
        mode="responses_native_passthrough",
        messages=full_messages,
        params=build_monitor_request_params(
            responses_request,
            exclude_keys={"input", "instructions", "tools", "include", "prompt_cache_key"},
            extra=monitor_extra,
        ),
    )
    await monitoring_service.broadcast_to_monitors({
        "type": "request_start",
        "request_id": request_id,
        "model": display_name,
        "timestamp": time.time(),
    })

    api_iter = direct_api_service.call_api_passthrough(
        base_url=api_base_url,
        api_key=upstream_api_key,
        request_body=passthrough_body,
        endpoint_path=endpoint_path,
    )

    if is_stream:
        # ── 流式：首块错误检测 + 原样逐块转发（边转发边累计监控统计）──
        first_chunk_timeout = _chat_api.CONFIG.get(
            "first_chunk_timeout_seconds", TimeoutDefaults.FIRST_CHUNK_TIMEOUT)
        try:
            first_chunk = await asyncio.wait_for(anext(api_iter), timeout=first_chunk_timeout)
        except asyncio.TimeoutError:
            await _complete_responses_monitoring(
                monitoring_service=monitoring_service,
                direct_api_service=direct_api_service,
                request_id=request_id,
                success=False,
                display_name=display_name,
                pricing_config=pricing_config,
                estimate_message_tokens_func=estimate_message_tokens,
                estimate_tokens_func=estimate_tokens,
                full_messages=full_messages,
                error="上游 Responses API 首块响应超时。",
            )
            return build_responses_error_response(504, "上游 Responses API 首块响应超时。")
        except StopAsyncIteration:
            await _complete_responses_monitoring(
                monitoring_service=monitoring_service,
                direct_api_service=direct_api_service,
                request_id=request_id,
                success=False,
                display_name=display_name,
                pricing_config=pricing_config,
                estimate_message_tokens_func=estimate_message_tokens,
                estimate_tokens_func=estimate_tokens,
                full_messages=full_messages,
                error="上游 Responses API 返回空响应。",
            )
            return build_responses_error_response(502, "上游 Responses API 返回空响应。")

        is_error, error_json = detect_first_chunk_error(first_chunk)
        if is_error:
            status_code = map_upstream_error_to_status_code(error_json, default_status_code=502)
            logger.warning("[RESPONSES_NATIVE] 流式请求上游返回错误: HTTP %s", status_code)
            clean_error = {k: v for k, v in error_json.items() if not k.startswith("_")}
            error_obj = clean_error.get("error")
            error_message = error_obj.get("message") if isinstance(error_obj, dict) else str(clean_error)
            try:
                await api_iter.aclose()
            except Exception:
                pass
            await _complete_responses_monitoring(
                monitoring_service=monitoring_service,
                direct_api_service=direct_api_service,
                request_id=request_id,
                success=False,
                display_name=display_name,
                pricing_config=pricing_config,
                estimate_message_tokens_func=estimate_message_tokens,
                estimate_tokens_func=estimate_tokens,
                full_messages=full_messages,
                error=error_message,
            )
            return build_responses_error_response(status_code, clean_error)

        async def passthrough_source():
            stats = {
                "content_parts": [],
                "reasoning_parts": [],
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "upstream_usage": None,
                "error_message": None,
                "stream_complete": False,
                "saw_completed": False,
            }
            monitoring_done = False
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            sse_buffer = ""

            async def complete_monitoring_once() -> None:
                nonlocal monitoring_done
                if monitoring_done:
                    return
                # 先占用完成权，避免 finally 重复落盘第二条记录
                monitoring_done = True
                # 上游 response.completed 已到 = 本轮实际成功；客户端随后断开
                # （Codex 拿到完整回复就关连接是常态）不得改判失败。
                success = (stats["stream_complete"] or stats.get("saw_completed")) and stats["error_message"] is None
                if not success and stats["error_message"] is None:
                    stats["error_message"] = "Client disconnected"
                await _complete_responses_monitoring(
                    monitoring_service=monitoring_service,
                    direct_api_service=direct_api_service,
                    request_id=request_id,
                    success=success,
                    display_name=display_name,
                    pricing_config=pricing_config,
                    estimate_message_tokens_func=estimate_message_tokens,
                    estimate_tokens_func=estimate_tokens,
                    full_messages=full_messages,
                    content="".join(stats["content_parts"]),
                    reasoning="".join(stats["reasoning_parts"]),
                    input_tokens=stats["input_tokens"],
                    output_tokens=stats["output_tokens"],
                    cached_tokens=stats["cached_tokens"],
                    error=stats["error_message"],
                    upstream_usage=stats["upstream_usage"],
                )

            try:
                # 首块已预取：先统计再转发（不遗漏 response.created 等事件）
                sse_buffer += decoder.decode(first_chunk, final=False)
                blocks, sse_buffer = _drain_sse_blocks(sse_buffer)
                for block in blocks:
                    _apply_sse_block(block, stats)
                yield _rewrite_sse_model(first_chunk, model, target_model_id)

                async for chunk in api_iter:
                    sse_buffer += decoder.decode(chunk, final=False)
                    blocks, sse_buffer = _drain_sse_blocks(sse_buffer)
                    for block in blocks:
                        _apply_sse_block(block, stats)
                    yield _rewrite_sse_model(chunk, model, target_model_id)

                # 流自然结束 = 上游流已完整处理
                stats["stream_complete"] = True
                await complete_monitoring_once()
            except asyncio.CancelledError:
                # 客户端取消：上游已 completed（或流已完整输出）时不改判失败，否则按断连处理
                if not (stats["stream_complete"] or stats.get("saw_completed")):
                    stats["error_message"] = "Client disconnected"
                raise
            except GeneratorExit:
                if not (stats["stream_complete"] or stats.get("saw_completed")):
                    stats["error_message"] = "Client disconnected"
                raise
            except Exception as exc:
                if stats["error_message"] is None:
                    stats["error_message"] = str(exc) or "Stream processing failed"
                logger.error("[RESPONSES_NATIVE] 流式透传处理异常: %s", exc, exc_info=True)
                raise
            finally:
                try:
                    await api_iter.aclose()
                except Exception:
                    pass
                if not monitoring_done:
                    await complete_monitoring_once()

        return StreamingResponse(
            passthrough_source(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ── 非流式：收集完整 JSON，错误则包装，正常则 model 回写后原样返回 ──
    response_buffer = bytearray()
    try:
        async for chunk in api_iter:
            response_buffer.extend(chunk)
    except asyncio.CancelledError:
        try:
            await api_iter.aclose()
        except Exception:
            pass
        await _complete_responses_monitoring(
            monitoring_service=monitoring_service,
            direct_api_service=direct_api_service,
            request_id=request_id,
            success=False,
            display_name=display_name,
            pricing_config=pricing_config,
            estimate_message_tokens_func=estimate_message_tokens,
            estimate_tokens_func=estimate_tokens,
            full_messages=full_messages,
            error="Client disconnected before non-stream response completed",
        )
        raise
    except Exception as exc:
        logger.error("[RESPONSES_NATIVE] 非流式请求失败: %s", exc, exc_info=True)
        await _complete_responses_monitoring(
            monitoring_service=monitoring_service,
            direct_api_service=direct_api_service,
            request_id=request_id,
            success=False,
            display_name=display_name,
            pricing_config=pricing_config,
            estimate_message_tokens_func=estimate_message_tokens,
            estimate_tokens_func=estimate_tokens,
            full_messages=full_messages,
            error=f"上游 Responses API 请求失败: {exc}",
        )
        return build_responses_error_response(502, f"上游 Responses API 请求失败: {exc}")

    try:
        responses_json = json.loads(bytes(response_buffer).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        await _complete_responses_monitoring(
            monitoring_service=monitoring_service,
            direct_api_service=direct_api_service,
            request_id=request_id,
            success=False,
            display_name=display_name,
            pricing_config=pricing_config,
            estimate_message_tokens_func=estimate_message_tokens,
            estimate_tokens_func=estimate_tokens,
            full_messages=full_messages,
            error="上游 Responses API 返回了无法解析的 JSON。",
        )
        return build_responses_error_response(502, "上游 Responses API 返回了无法解析的 JSON。")

    if is_error_json(responses_json):
        normalized = normalize_to_openai_error(responses_json)
        status_code = map_upstream_error_to_status_code(normalized, default_status_code=502)
        logger.warning("[RESPONSES_NATIVE] 非流式请求上游返回错误: HTTP %s", status_code)
        clean_error = {k: v for k, v in normalized.items() if not k.startswith("_")}
        error_obj = clean_error.get("error")
        error_message = error_obj.get("message") if isinstance(error_obj, dict) else str(clean_error)
        await _complete_responses_monitoring(
            monitoring_service=monitoring_service,
            direct_api_service=direct_api_service,
            request_id=request_id,
            success=False,
            display_name=display_name,
            pricing_config=pricing_config,
            estimate_message_tokens_func=estimate_message_tokens,
            estimate_tokens_func=estimate_tokens,
            full_messages=full_messages,
            error=error_message,
        )
        return build_responses_error_response(status_code, clean_error)

    # model 回写：上游响应的 model 是 target_model_id，替换回客户端请求名
    if responses_json.get("model") == target_model_id and model != target_model_id:
        responses_json["model"] = model

    # 提取内容与 usage 记录监控（透传响应本身原样返回，转换结果仅用于统计）
    converted = convert_responses_response_to_chat(responses_json, model)
    choice = (converted.get("choices") or [{}])[0]
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content", "") if isinstance(message.get("content", ""), str) else ""
    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    tool_calls = message.get("tool_calls")
    usage = responses_json.get("usage")
    input_tokens, output_tokens, cached_tokens = _extract_responses_usage_tokens(usage)
    await _complete_responses_monitoring(
        monitoring_service=monitoring_service,
        direct_api_service=direct_api_service,
        request_id=request_id,
        success=True,
        display_name=display_name,
        pricing_config=pricing_config,
        estimate_message_tokens_func=estimate_message_tokens,
        estimate_tokens_func=estimate_tokens,
        full_messages=full_messages,
        content=content,
        reasoning=reasoning,
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        upstream_usage=usage if isinstance(usage, dict) else None,
    )
    return JSONResponse(status_code=200, content=responses_json)


@router.post("/v1/responses")
async def responses_endpoint(request: Request):
    """处理 OpenAI Responses API 请求（第一阶段无状态兼容）。"""
    app_state = _chat_api._app_state
    app_state.update_activity()
    try:
        _chat_api._check_verification_cooldown()
    except HTTPException as exc:
        return build_responses_error_response(exc.status_code, exc.detail)

    try:
        responses_request = await _chat_api._read_request_json_non_blocking(request)
    except json.JSONDecodeError as exc:
        return build_responses_error_response(400, str(exc) or "无效的 JSON 请求体。")
    except HTTPException as exc:
        return build_responses_error_response(exc.status_code, exc.detail)
    except Exception as exc:
        if "Disconnect" in type(exc).__name__:
            return JSONResponse(status_code=499, content={"error": {"message": "Client Disconnected", "type": "api_error"}})
        raise

    # ── responses_native 原生透传分支 ──
    # 与转换链路的关键区别：input 中的 reasoning item（encrypted_content / id /
    # summary）不做任何解析/转换，原样转发，保证思维链签名等密文载体完整回传。
    passthrough_model = responses_request.get("model")
    endpoint_config = None
    if isinstance(passthrough_model, str):
        raw_config = _chat_api.MODEL_ENDPOINT_MAP.get(passthrough_model)
        if raw_config is not None and is_model_archived(raw_config):
            return build_responses_error_response(
                404, f"模型 '{passthrough_model}' 不存在"
            )
        endpoint_config = await _chat_api._select_endpoint_config_for_model(passthrough_model)
    if isinstance(endpoint_config, dict) and endpoint_config.get("api_type") == "responses_native":
        return await _handle_responses_native_passthrough(
            responses_request=responses_request,
            model=passthrough_model,
            endpoint_config=endpoint_config,
            request=request,
        )

    try:
        chat_request = convert_responses_to_chat_request(responses_request)
    except ResponsesRequestError as exc:
        return build_responses_error_response(400, str(exc))

    model = chat_request["model"]
    try:
        # 认证在兼容层完成一次，内部调度使用 skip_api_auth，避免重复计 RPM。
        _chat_api._validate_request_api_key(request, model)
        upstream_response = await _chat_api._dispatch_chat_completions_core(
            chat_request,
            request=None,
            skip_api_auth=True,
        )
    except HTTPException as exc:
        logger.warning("[RESPONSES_COMPAT] 上游请求失败: HTTP %s", exc.status_code)
        return build_responses_error_response(exc.status_code, exc.detail)

    response_status = getattr(upstream_response, "status_code", 200)
    if response_status >= 400:
        return await _read_error_response(upstream_response)

    if chat_request.get("stream"):
        return build_responses_streaming_response(
            upstream_response,
            request=responses_request,
            model=model,
        )

    if _is_stream_response(upstream_response):
        chat_response = await collect_chat_stream_response(upstream_response, model=model)
    else:
        try:
            chat_response = _parse_json_body(await read_response_body_bytes(upstream_response))
        except HTTPException as exc:
            return build_responses_error_response(exc.status_code, exc.detail)

    if chat_response.get("error") is not None:
        error_status = map_upstream_error_to_status_code(
            chat_response,
            default_status_code=502,
        )
        return JSONResponse(status_code=error_status, content=chat_response)

    try:
        response_payload = convert_chat_response_to_responses(
            chat_response,
            request=responses_request,
        )
    except ResponsesRequestError as exc:
        logger.error("[RESPONSES_COMPAT] 响应转换失败: %s", exc, exc_info=True)
        return build_responses_error_response(502, str(exc))
    return JSONResponse(status_code=200, content=response_payload)


__all__ = ["router", "responses_endpoint"]
