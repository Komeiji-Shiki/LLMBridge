"""Direct API - 上游 OpenAI Responses 原生格式处理。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from converters.responses_bridge import (
    ResponsesBridgeError,
    build_chat_streaming_response_from_responses,
    convert_chat_request_to_responses,
    convert_responses_response_to_chat,
)
from core.constants import TimeoutDefaults
from utils.monitor_params import build_monitor_request_params
from utils.usage_tokens import MODE_MERGE, get_completion_tokens_mode, total_output_tokens
from ._direct_api_utils import (
    append_tool_call_delta,
    build_response_message,
    detect_first_chunk_error,
    estimate_message_tokens_non_blocking,
    estimate_text_tokens_non_blocking,
    finalize_tool_calls,
    is_error_json,
    map_upstream_error_to_status_code,
    normalize_to_openai_error,
)

logger = logging.getLogger(__name__)


def _extract_chat_response(chat_response: Dict[str, Any]) -> tuple:
    choices = chat_response.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content", "") if isinstance(message.get("content", ""), str) else ""
    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else None
    usage = chat_response.get("usage") if isinstance(chat_response.get("usage"), dict) else {}
    input_tokens = int(usage.get("prompt_tokens", 0) or 0)
    # 统计与计费按真实总输出（正文+思考），不受下游 completion_tokens 口径影响
    output_tokens = total_output_tokens(usage)
    prompt_details = usage.get("prompt_tokens_details")
    cached_tokens = int(prompt_details.get("cached_tokens", 0) or 0) if isinstance(prompt_details, dict) else 0
    return content, reasoning, tool_calls, input_tokens, output_tokens, cached_tokens


def _iter_chat_payloads(chunk: bytes):
    text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            yield data


def _is_chat_stream_done(chunk: bytes) -> bool:
    """严格识别 Chat SSE 终止行，避免正文中的字面量 ``[DONE]`` 误触发。"""
    text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
    return any(
        line.startswith("data:") and line[5:].strip() == "[DONE]"
        for line in text.splitlines()
    )


async def _complete_monitoring(
    *,
    monitoring_service,
    direct_api_service,
    request_id: str,
    success: bool,
    openai_req: Dict[str, Any],
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
    if input_tokens <= 0:
        try:
            input_tokens = await estimate_message_tokens_non_blocking(
                estimate_message_tokens_func,
                openai_req.get("messages", []),
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


async def _handle_non_stream(
    *,
    upstream_request: Dict[str, Any],
    openai_req: Dict[str, Any],
    request_id: str,
    display_name: str,
    api_base_url: str,
    api_key: str,
    endpoint_path: str,
    pricing_config: Dict[str, Any],
    monitoring_service,
    direct_api_service,
    estimate_message_tokens_func,
    estimate_tokens_func,
    full_messages,
    completion_tokens_mode: str = MODE_MERGE,
):
    response_buffer = bytearray()
    async for chunk in direct_api_service.call_api_passthrough(
        base_url=api_base_url,
        api_key=api_key,
        request_body=upstream_request,
        endpoint_path=endpoint_path,
    ):
        response_buffer.extend(chunk)

    try:
        responses_json = json.loads(bytes(response_buffer).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=502, detail="上游 Responses API 返回了无法解析的 JSON。") from exc

    if is_error_json(responses_json):
        normalized = normalize_to_openai_error(responses_json)
        status_code = map_upstream_error_to_status_code(normalized, default_status_code=502)
        error_obj = normalized.get("error", {})
        error_message = error_obj.get("message", "上游 Responses 请求失败") if isinstance(error_obj, dict) else str(error_obj)
        await _complete_monitoring(
            monitoring_service=monitoring_service,
            direct_api_service=direct_api_service,
            request_id=request_id,
            success=False,
            openai_req=openai_req,
            display_name=display_name,
            pricing_config=pricing_config,
            estimate_message_tokens_func=estimate_message_tokens_func,
            estimate_tokens_func=estimate_tokens_func,
            full_messages=full_messages,
            error=error_message,
            upstream_usage=responses_json.get("usage") if isinstance(responses_json.get("usage"), dict) else None,
        )
        return JSONResponse(status_code=status_code, content=normalized)

    try:
        chat_response = convert_responses_response_to_chat(
            responses_json, display_name, completion_tokens_mode)
    except ResponsesBridgeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    content, reasoning, tool_calls, input_tokens, output_tokens, cached_tokens = _extract_chat_response(chat_response)
    await _complete_monitoring(
        monitoring_service=monitoring_service,
        direct_api_service=direct_api_service,
        request_id=request_id,
        success=True,
        openai_req=openai_req,
        display_name=display_name,
        pricing_config=pricing_config,
        estimate_message_tokens_func=estimate_message_tokens_func,
        estimate_tokens_func=estimate_tokens_func,
        full_messages=full_messages,
        content=content,
        reasoning=reasoning,
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        upstream_usage=responses_json.get("usage") if isinstance(responses_json.get("usage"), dict) else None,
    )
    return JSONResponse(content=chat_response)


async def _handle_stream(
    *,
    upstream_request: Dict[str, Any],
    openai_req: Dict[str, Any],
    request_id: str,
    display_name: str,
    api_base_url: str,
    api_key: str,
    endpoint_path: str,
    endpoint_config: Dict[str, Any],
    pricing_config: Dict[str, Any],
    monitoring_service,
    direct_api_service,
    estimate_message_tokens_func,
    estimate_tokens_func,
    full_messages,
    CONFIG: Optional[Dict[str, Any]],
):
    upstream_iterator = direct_api_service.call_api_passthrough(
        base_url=api_base_url,
        api_key=api_key,
        request_body=upstream_request,
        endpoint_path=endpoint_path,
    )
    first_chunk_timeout = (CONFIG or {}).get(
        "first_chunk_timeout_seconds",
        TimeoutDefaults.FIRST_CHUNK_TIMEOUT,
    )
    try:
        first_chunk = await asyncio.wait_for(anext(upstream_iterator), timeout=first_chunk_timeout)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="上游 Responses API 首块响应超时。") from exc
    except StopAsyncIteration as exc:
        raise HTTPException(status_code=502, detail="上游 Responses API 返回空响应。") from exc

    is_error, error_json = detect_first_chunk_error(first_chunk)
    if is_error:
        status_code = map_upstream_error_to_status_code(error_json, default_status_code=502)
        error_obj = error_json.get("error", {})
        error_message = error_obj.get("message", "上游 Responses 请求失败") if isinstance(error_obj, dict) else str(error_obj)
        raise HTTPException(status_code=status_code, detail=error_message)

    async def prefixed_source():
        yield first_chunk
        async for chunk in upstream_iterator:
            yield chunk

    converted_response = build_chat_streaming_response_from_responses(
        prefixed_source(),
        display_name,
        get_completion_tokens_mode(endpoint_config),
    )

    async def monitored_generator():
        content_parts = []
        reasoning_parts = []
        tool_accumulator = {}
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        upstream_usage = None
        error_message = None
        monitoring_completed = False
        # 上游流是否已完整处理：收到 finish_reason / [DONE] / 错误事件，或迭代自然结束。
        # 成功与否只看上游流完整性，与客户端是否提前断开无关（客户端在收到
        # [DONE] 后关闭连接是标准行为，若因此误报失败则所有成功流式请求都会
        # 记录为 failed 且 error 为空）。
        stream_complete = False

        async def complete_monitoring_once() -> None:
            """在终止块交给下游前完成落盘，避免外层提前结束消费造成误报。"""
            nonlocal monitoring_completed, error_message
            if monitoring_completed:
                return
            # 先占用完成权：_complete_monitoring 内部先同步 request_end、再异步广播，
            # 若广播阶段任务被取消，finally 不得重复 request_end 生成第二条记录。
            monitoring_completed = True
            success = stream_complete and error_message is None
            if not success and error_message is None:
                error_message = "Client disconnected"
            await _complete_monitoring(
                monitoring_service=monitoring_service,
                direct_api_service=direct_api_service,
                request_id=request_id,
                success=success,
                openai_req=openai_req,
                display_name=display_name,
                pricing_config=pricing_config,
                estimate_message_tokens_func=estimate_message_tokens_func,
                estimate_tokens_func=estimate_tokens_func,
                full_messages=full_messages,
                content="".join(content_parts),
                reasoning="".join(reasoning_parts),
                tool_calls=finalize_tool_calls(tool_accumulator),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                error=error_message,
                upstream_usage=upstream_usage,
            )

        try:
            async for chunk in converted_response.body_iterator:
                chunk_bytes = chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
                for payload in _iter_chat_payloads(chunk_bytes):
                    if payload.get("error") is not None:
                        error_obj = payload["error"]
                        error_message = error_obj.get("message") if isinstance(error_obj, dict) else str(error_obj)
                        stream_complete = True
                    usage = payload.get("usage")
                    if isinstance(usage, dict):
                        input_tokens = int(usage.get("prompt_tokens", 0) or 0)
                        # 计费按真实总输出（正文+思考），下游按模式下总量还是正文
                        output_tokens = total_output_tokens(usage)
                        details = usage.get("prompt_tokens_details")
                        cached_tokens = int(details.get("cached_tokens", 0) or 0) if isinstance(details, dict) else 0
                        upstream_usage = usage
                    choices = payload.get("choices") or []
                    if choices and isinstance(choices[0], dict):
                        delta = choices[0].get("delta") if isinstance(choices[0].get("delta"), dict) else {}
                        if isinstance(delta.get("content"), str):
                            content_parts.append(delta["content"])
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(reasoning, str):
                            reasoning_parts.append(reasoning)
                        if choices[0].get("finish_reason"):
                            stream_complete = True
                        append_tool_call_delta(tool_accumulator, delta.get("tool_calls"))
                if _is_chat_stream_done(chunk_bytes):
                    stream_complete = True
                    # /v1/responses 外层收到该块后会立即生成 response.completed，
                    # 客户端通常随即停止读取。必须在交出终止块之前完成监控，
                    # 不能再依赖生成器下一次恢复或异步清理时机。
                    await complete_monitoring_once()
                yield chunk_bytes
            # 迭代自然结束 = 上游流已完整处理
            stream_complete = True
            await complete_monitoring_once()
        except asyncio.CancelledError:
            # 客户端取消/断开：流已完整输出时不改判失败，否则按断连处理
            if not stream_complete:
                error_message = "Client disconnected"
            raise
        except GeneratorExit:
            # 生成器被关闭（客户端提前断开等）：成功与否仍由上游流完整性决定
            if not stream_complete:
                error_message = "Client disconnected"
            raise
        except Exception as exc:
            # 转换/上游异常：记录真实错误，避免 finally 以 success=False + error=None 误报
            if error_message is None:
                error_message = str(exc) or "Stream processing failed"
            logger.error("[RESPONSES_NATIVE] 流式处理异常: %s", exc, exc_info=True)
            raise
        finally:
            try:
                await upstream_iterator.aclose()
            except Exception:
                pass
            if not monitoring_completed:
                await complete_monitoring_once()

    return StreamingResponse(
        monitored_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked",
        },
    )


async def handle_responses_native_direct(
    *,
    openai_req: Dict[str, Any],
    model_name: str,
    target_model_id: str,
    display_name: str,
    api_base_url: str,
    api_key: str,
    endpoint_config: Dict[str, Any],
    pricing_config: Dict[str, Any],
    monitoring_service,
    direct_api_service,
    estimate_message_tokens_func,
    estimate_tokens_func,
    full_messages=None,
    CONFIG: Optional[Dict[str, Any]] = None,
):
    """内部 Chat 请求 → 上游 Responses → 内部 Chat 响应。"""
    request_id = str(uuid.uuid4())
    completion_tokens_mode = get_completion_tokens_mode(endpoint_config)
    endpoint_path = endpoint_config.get("endpoint_path") or "/responses"
    endpoint_path = endpoint_path if endpoint_path.startswith("/") else "/" + endpoint_path
    try:
        upstream_request = convert_chat_request_to_responses(
            openai_req,
            target_model_id=target_model_id,
            endpoint_config=endpoint_config,
        )
    except ResponsesBridgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 上游 Responses 同样拒绝递归 schema，端点配置 sanitize_recursive_schemas=true
    # （默认）时在转换后统一清洗（管理面板可按模型开关）。
    if endpoint_config.get("sanitize_recursive_schemas", True):
        try:
            from utils.schema_sanitizer import (
                force_all_strict_false_responses,
                sanitize_responses_request,
            )
            washed = sanitize_responses_request(upstream_request)
            forced = force_all_strict_false_responses(upstream_request)
            if washed or forced:
                logger.info(
                    "[RESPONSES_NATIVE] 已处理递归 JSON Schema（chat->responses 转换结果，清洗=%s，strict 降级=%s）",
                    washed, forced,
                )
        except Exception as exc:
            logger.warning("[RESPONSES_NATIVE] 递归 schema 清洗失败，原样透传: %s", exc)

    monitoring_service.request_start(
        request_id=request_id,
        model=display_name,
        messages_count=len(openai_req.get("messages", [])),
        session_id=None,
        mode="responses_native",
        messages=openai_req.get("messages", []),
        params=build_monitor_request_params(
            openai_req,
            extra={"upstream_model": target_model_id, "endpoint_path": endpoint_path},
        ),
    )
    await monitoring_service.broadcast_to_monitors({
        "type": "request_start",
        "request_id": request_id,
        "model": display_name,
        "timestamp": time.time(),
    })

    try:
        if openai_req.get("stream", False):
            return await _handle_stream(
                upstream_request=upstream_request,
                openai_req=openai_req,
                request_id=request_id,
                display_name=display_name,
                api_base_url=api_base_url,
                api_key=api_key,
                endpoint_path=endpoint_path,
                endpoint_config=endpoint_config,
                pricing_config=pricing_config,
                monitoring_service=monitoring_service,
                direct_api_service=direct_api_service,
                estimate_message_tokens_func=estimate_message_tokens_func,
                estimate_tokens_func=estimate_tokens_func,
                full_messages=full_messages,
                CONFIG=CONFIG,
            )
        return await _handle_non_stream(
            upstream_request=upstream_request,
            openai_req=openai_req,
            request_id=request_id,
            display_name=display_name,
            api_base_url=api_base_url,
            api_key=api_key,
            endpoint_path=endpoint_path,
            pricing_config=pricing_config,
            monitoring_service=monitoring_service,
            direct_api_service=direct_api_service,
            estimate_message_tokens_func=estimate_message_tokens_func,
            estimate_tokens_func=estimate_tokens_func,
            full_messages=full_messages,
            completion_tokens_mode=completion_tokens_mode,
        )
    except HTTPException as exc:
        await _complete_monitoring(
            monitoring_service=monitoring_service,
            direct_api_service=direct_api_service,
            request_id=request_id,
            success=False,
            openai_req=openai_req,
            display_name=display_name,
            pricing_config=pricing_config,
            estimate_message_tokens_func=estimate_message_tokens_func,
            estimate_tokens_func=estimate_tokens_func,
            full_messages=full_messages,
            error=str(exc.detail),
        )
        raise
    except Exception as exc:
        await _complete_monitoring(
            monitoring_service=monitoring_service,
            direct_api_service=direct_api_service,
            request_id=request_id,
            success=False,
            openai_req=openai_req,
            display_name=display_name,
            pricing_config=pricing_config,
            estimate_message_tokens_func=estimate_message_tokens_func,
            estimate_tokens_func=estimate_tokens_func,
            full_messages=full_messages,
            error=str(exc),
        )
        raise


__all__ = ["handle_responses_native_direct"]
