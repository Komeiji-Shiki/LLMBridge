"""
Direct API - 透传模式处理
支持 OpenAI/Anthropic 兼容 API 的流式与非流式透传
"""
import asyncio
import copy
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, Response

from core.constants import TimeoutDefaults
from services.logprobs_collector import create_logprobs_collector
from utils.json_unescape import normalize_response_tool_args
from utils.monitor_params import build_monitor_request_params
from utils.usage_tokens import (
    MODE_MERGE,
    apply_usage_tokens,
    get_completion_tokens_mode,
    resolve_usage_tokens,
)
from ._direct_api_reasoning_cache import lookup_reasoning_details, store_reasoning_details
from ._direct_api_stream_session import PassthroughStreamSession
from ._direct_api_utils import (
    build_response_message,
    detect_first_chunk_error,
    estimate_message_tokens_non_blocking,
    estimate_text_tokens_non_blocking,
    extract_tool_calls_from_message,
    is_error_json,
    map_upstream_error_to_status_code,
    normalize_to_openai_error,
)

logger = logging.getLogger(__name__)

# 超过该大小的响应体才丢线程池解析/序列化（小 body 的线程切换开销比解析本身还大）
_JSON_OFFLOAD_THRESHOLD_BYTES = 64 * 1024


async def handle_passthrough_direct(
    openai_req: dict,
    model_name: str,
    target_model_id: str,
    display_name: str,
    api_base_url: Optional[str],
    api_key: Optional[str],
    endpoint_config: dict,
    pricing_config: dict,
    thinking_separator: Optional[str],
    monitoring_service,
    direct_api_service,
    estimate_message_tokens_func,
    estimate_tokens_func,
    full_messages: Optional[list] = None,
    CONFIG: Optional[dict] = None
):
    """处理透传模式的Direct API请求"""
    logger.info(f"[DIRECT_API_PASSTHROUGH] 启用透传模式")

    request_id = str(uuid.uuid4())
    endpoint_path = endpoint_config.get("endpoint_path", "/chat/completions")
    if not isinstance(endpoint_path, str) or not endpoint_path.strip():
        endpoint_path = "/chat/completions"
    endpoint_path = endpoint_path.strip()
    if not endpoint_path.startswith("/"):
        endpoint_path = "/" + endpoint_path

    monitor_extra_params: dict = {"upstream_model": target_model_id, "endpoint_path": endpoint_path}
    # 自定义参数/附加主体参数以独立字段记录到监控日志，避免平铺覆盖监控字段
    custom_params = endpoint_config.get("custom_params")
    if isinstance(custom_params, dict) and custom_params:
        monitor_extra_params["custom_params"] = custom_params
    extra_body_params = endpoint_config.get("extra_body_params")
    if isinstance(extra_body_params, dict) and extra_body_params:
        monitor_extra_params["extra_body_params"] = extra_body_params
    # 记录思考相关配置到监控日志（便于排查注入是否生效）
    for key in ("enable_thinking", "reasoning_effort", "thinking_budget", "thinking_effort",
                "oai_thinking_type", "oai_thinking_effort", "force_stream", "verbosity"):
        val = endpoint_config.get(key)
        if val is not None and val != "":
            monitor_extra_params[key] = val

    monitoring_service.request_start(
        request_id=request_id,
        model=display_name,
        messages_count=len(openai_req.get("messages", [])),
        session_id=None,
        mode="direct_api_passthrough",
        messages=openai_req.get("messages", []),
        params=build_monitor_request_params(openai_req, extra=monitor_extra_params)
    )

    await monitoring_service.broadcast_to_monitors({
        "type": "request_start",
        "request_id": request_id,
        "model": display_name,
        "timestamp": time.time()
    })

    try:
        # --- 准备透传请求体 ---
        passthrough_request = await asyncio.to_thread(_prepare_passthrough_request,
            openai_req, model_name, target_model_id, endpoint_config)

        try:
            logprobs_collector = create_logprobs_collector(
                passthrough_request=passthrough_request,
                openai_req=openai_req,
                model_name=model_name,
                target_model_id=target_model_id,
                display_name=display_name,
                api_base_url=api_base_url,
                endpoint_config=endpoint_config,
                request_id=request_id,
                full_messages=full_messages,
                endpoint_path=endpoint_path,
            )
        except Exception as collector_err:
            logger.warning("[LOGPROBS_COLLECT] 初始化采集器失败，已跳过采集: %s", collector_err)
            logprobs_collector = None

        is_stream = openai_req.get("stream", False)
        logger.info(f"[DIRECT_API_PASSTHROUGH] 使用上游端点: {endpoint_path}")

        if is_stream:
            return await _handle_passthrough_stream(
                passthrough_request, openai_req, request_id, model_name,
                display_name, api_base_url, api_key, endpoint_config,
                pricing_config, thinking_separator, endpoint_path,
                monitoring_service, direct_api_service,
                estimate_message_tokens_func, estimate_tokens_func,
                full_messages, CONFIG, logprobs_collector=logprobs_collector
            )
        else:
            return await _handle_passthrough_non_stream(
                passthrough_request, openai_req, request_id, display_name,
                api_base_url, api_key, endpoint_config, pricing_config,
                thinking_separator, endpoint_path,
                monitoring_service, direct_api_service,
                estimate_message_tokens_func, estimate_tokens_func,
                full_messages, logprobs_collector=logprobs_collector
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DIRECT_API_PASSTHROUGH] 请求处理失败: {e}", exc_info=True)
        monitoring_service.request_end(
            request_id=request_id, success=False, error=str(e),
            full_messages=full_messages)
        await monitoring_service.broadcast_to_monitors({
            "type": "request_end", "request_id": request_id, "success": False})
        raise HTTPException(status_code=500, detail=f"Direct API透传失败: {str(e)}")


# ============================================================
# 请求体准备
# ============================================================

def _prepare_passthrough_request(openai_req, model_name, target_model_id, endpoint_config):
    """准备透传请求体：模型ID映射、空文本过滤、自定义参数、模式开关等"""
    passthrough_request = openai_req.copy()
    passthrough_request["model"] = target_model_id

    # 🔧 写时复制：后续会直接修改消息 dict（空文本过滤/system转user/
    # prefix/partial/reasoning 占位）。openai_req.copy() 是浅拷贝，
    # 消息 dict 与监控记录的 full_messages 共享引用，不复制会污染
    # 监控日志中的原始请求消息。逐条 dict() 浅复制字段引用，开销极小。
    if isinstance(passthrough_request.get("messages"), list):
        passthrough_request["messages"] = [
            dict(m) if isinstance(m, dict) else m
            for m in passthrough_request["messages"]
        ]

    # 🔍 诊断：打印实际发送的图片 URL 格式
    if "messages" in passthrough_request:
        for msg in passthrough_request["messages"]:
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        url_preview = url[:120] if url else "(empty)"
                        is_base64 = url.startswith("data:") if url else False
                        logger.info(
                            f"[IMG_DIAG] 透传图片URL: is_base64={is_base64}, "
                            f"len={len(url)}, preview={url_preview}...")

    # 过滤空文本块（Claude 不接受）
    if "messages" in passthrough_request:
        for msg in passthrough_request["messages"]:
            content = msg.get("content")
            if isinstance(content, list):
                filtered_content = [
                    item for item in content
                    if not (isinstance(item, dict)
                            and item.get("type") == "text"
                            and not item.get("text", "").strip())
                ]
                if not filtered_content:
                    filtered_content = [{"type": "text", "text": " "}]
                msg["content"] = filtered_content

    # 合并自定义参数（deepcopy：后续可能在合并进来的嵌套对象上原地
    # 写入字段，如 setdefault("reasoning")，直接引用会污染内存配置）
    custom_params = endpoint_config.get("custom_params", {})
    if custom_params and isinstance(custom_params, dict):
        passthrough_request.update(copy.deepcopy(custom_params))
        logger.info(f"[DIRECT_API_CUSTOM] 已添加自定义参数:")
        for key, value in custom_params.items():
            logger.info(f"  - {key}: {value}")

    # 合并附加主体参数（在自定义参数之后，同名键优先）
    extra_body_params = endpoint_config.get("extra_body_params", {})
    if extra_body_params and isinstance(extra_body_params, dict):
        passthrough_request.update(copy.deepcopy(extra_body_params))
        logger.info(f"[DIRECT_API_CUSTOM] 已添加附加主体参数:")
        for key, value in extra_body_params.items():
            logger.info(f"  - {key}: {value}")

    # System转User模式
    convert_system_to_user = endpoint_config.get("convert_system_to_user", False)
    if convert_system_to_user and "messages" in passthrough_request:
        converted_count = 0
        for msg in passthrough_request["messages"]:
            if isinstance(msg, dict) and msg.get("role") == "system":
                msg["role"] = "user"
                converted_count += 1
        if converted_count > 0:
            logger.info(f"[DIRECT_API_CONVERT] 已将 {converted_count} 条 system 消息转换为 user 消息")

    # DeepSeek Prefix 模式
    if endpoint_config.get("enable_prefix", False) and "messages" in passthrough_request:
        messages = passthrough_request["messages"]
        if messages and len(messages) > 0:
            last_message = messages[-1]
            if isinstance(last_message, dict) and last_message.get("role") == "assistant":
                last_message["prefix"] = True
                logger.info(f"[DIRECT_API_PREFIX] 已启用 prefix 模式")

    # Kimi K2 Partial 模式
    if endpoint_config.get("enable_partial", False) and "messages" in passthrough_request:
        messages = passthrough_request["messages"]
        if messages and len(messages) > 0:
            last_message = messages[-1]
            if isinstance(last_message, dict) and last_message.get("role") == "assistant":
                last_message["partial"] = True
                logger.info(f"[DIRECT_API_PARTIAL] 已启用 partial 模式（Kimi K2预填充）")

    # DeepSeek V4 reasoning_content 兼容
    is_deepseek = "deepseek" in (model_name or "").lower() or "deepseek" in (target_model_id or "").lower()
    if is_deepseek and "messages" in passthrough_request:
        for msg in passthrough_request["messages"]:
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                rc = msg.get("reasoning_content")
                if rc is None or (isinstance(rc, str) and not rc.strip()):
                    msg["reasoning_content"] = " "

    # 判断是否为 OpenRouter 端点（用于走 OpenRouter 专属参数格式）
    api_base_url = (endpoint_config.get("api_base_url") or "").lower()
    is_openrouter = "openrouter.ai" in api_base_url

    if is_openrouter:
        # OpenRouter：thinking 参数走 reasoning 对象
        is_qwen = "qwen" in (target_model_id or "").lower()
        enable_thinking = endpoint_config.get("enable_thinking")
        if enable_thinking is True:
            passthrough_request.setdefault("reasoning", {})
            reasoning_effort = endpoint_config.get("reasoning_effort")
            if reasoning_effort:
                # 强度等级控制（OpenRouter 支持 effort: low/medium/high）
                passthrough_request["reasoning"]["effort"] = reasoning_effort
            else:
                # Token 预算控制（默认，向后兼容）
                thinking_budget = endpoint_config.get("thinking_budget", 20000)
                passthrough_request["reasoning"]["max_tokens"] = thinking_budget
            passthrough_request["reasoning"]["exclude"] = False
            # Qwen 模型需要额外的原生参数：enable_thinking 启用思考，preserve_thinking 保留历史思考链
            # OpenRouter 会将这些参数透传给底层 Qwen API
            if is_qwen:
                passthrough_request["enable_thinking"] = True
                passthrough_request["preserve_thinking"] = True
            logger.info(
                f"[DIRECT_API_THINKING] OpenRouter reasoning 模式已启用 "
                f"({'effort=' + reasoning_effort if reasoning_effort else 'max_tokens=' + str(endpoint_config.get('thinking_budget', 20000))}, "
                f"qwen_native={'yes' if is_qwen else 'no'})")
        elif enable_thinking is False:
            passthrough_request.setdefault("reasoning", {})
            passthrough_request["reasoning"]["exclude"] = True
            # Qwen 模型需要原生 enable_thinking=false 才能真正关闭思考
            if is_qwen:
                passthrough_request["enable_thinking"] = False
            logger.info(f"[DIRECT_API_THINKING] OpenRouter reasoning 已显式关闭")
        # enable_thinking 为 None/缺失时不发送任何 reasoning 字段

        # OpenRouter：assistant 消息思考链回传
        # - 消息自带 reasoning_details（含签名）时原样透传；
        # - 否则用思考文本查询当前隔离会话缓存（响应侧记录），命中则恢复原始 reasoning_details。
        #   Anthropic 系模型的 thinking 块带加密签名，必须原样回传 reasoning_details
        #   才能通过校验；纯文本 reasoning 会被 OpenRouter 重建为无签名 thinking 块，
        # 优先保留客户端字段；仅补充当前调用方、会话和上游下已保存的签名。
        if "messages" in passthrough_request:
            new_messages = []
            for msg in passthrough_request["messages"]:
                if not isinstance(msg, dict) or msg.get("role") != "assistant" or "reasoning_details" in msg:
                    new_messages.append(msg)
                    continue
                reasoning = msg.get("reasoning_content") or msg.get("reasoning")
                details = lookup_reasoning_details(reasoning) if isinstance(reasoning, str) and reasoning else None
                new_messages.append({**msg, "reasoning_details": details} if details else msg)
            passthrough_request["messages"] = new_messages
    else:
        # 非 OpenRouter：enable_thinking 和 reasoning_effort 独立注入
        # 仅处理布尔值，字符串值（adaptive/strip）由 Anthropic 原生模式处理
        enable_thinking = endpoint_config.get("enable_thinking")
        if enable_thinking is True or enable_thinking is False:
            passthrough_request["enable_thinking"] = enable_thinking
            logger.info(f"[DIRECT_API_THINKING] enable_thinking={enable_thinking}（已注入请求体顶层）")

        # 思考强度等级（OpenAI 风格 reasoning_effort），独立于 enable_thinking
        reasoning_effort = endpoint_config.get("reasoning_effort")
        if reasoning_effort:
            passthrough_request["reasoning_effort"] = reasoning_effort
            logger.info(f"[DIRECT_API_THINKING] reasoning_effort={reasoning_effort}（已注入请求体顶层）")

        # OAI 兼容 Anthropic thinking 参数：thinking.type + output_config.effort
        # 适用于通过 OpenAI 兼容端点调用 Claude 模型的场景（如 LiteLLM / 官方 Anthropic OAI 端点）
        oai_thinking_type = endpoint_config.get("oai_thinking_type")
        if oai_thinking_type and oai_thinking_type in ("enabled", "adaptive", "disabled"):
            passthrough_request.setdefault("thinking", {})
            passthrough_request["thinking"]["type"] = oai_thinking_type
            logger.info(f"[DIRECT_API_THINKING] thinking.type={oai_thinking_type}（OAI兼容Anthropic格式）")

        oai_thinking_effort = endpoint_config.get("oai_thinking_effort")
        if oai_thinking_effort:
            passthrough_request["output_config"] = {"effort": oai_thinking_effort}
            logger.info(f"[DIRECT_API_THINKING] output_config.effort={oai_thinking_effort}（OAI兼容Anthropic格式）")

    # verbosity 注入（GPT-5 系列 Chat Completions 顶层参数：low/medium/high）
    # 独立于 enable_thinking；客户端已显式传入时不覆盖
    verbosity = endpoint_config.get("verbosity")
    if verbosity and "verbosity" not in passthrough_request:
        passthrough_request["verbosity"] = verbosity
        logger.info(f"[DIRECT_API_VERBOSITY] verbosity={verbosity}（已注入请求体顶层）")

    # 工具 Schema、strict 与 required 保真转发。

    return passthrough_request


# ============================================================
# 流式透传
# ============================================================

async def _handle_passthrough_stream(
    passthrough_request, openai_req, request_id, model_name,
    display_name, api_base_url, api_key, endpoint_config,
    pricing_config, thinking_separator, endpoint_path,
    monitoring_service, direct_api_service,
    estimate_message_tokens_func, estimate_tokens_func,
    full_messages, CONFIG, logprobs_collector=None
):
    """处理流式透传请求。

    首块预读在 StreamingResponse 创建之前完成；若首块为上游错误（402/429/503等），
    直接抛出 HTTPException，让上层重试循环可以捕获并触发 sticky 冷却。
    """

    api_iterator = direct_api_service.call_api_passthrough(
        base_url=api_base_url, api_key=api_key,
        request_body=passthrough_request, endpoint_path=endpoint_path)

    first_chunk_timeout = TimeoutDefaults.FIRST_CHUNK_TIMEOUT
    if CONFIG:
        first_chunk_timeout = CONFIG.get("first_chunk_timeout_seconds", TimeoutDefaults.FIRST_CHUNK_TIMEOUT)

    # ── 预读首块（在 StreamingResponse 创建之前，错误可被上层重试循环捕获）──
    try:
        first_chunk_bytes = await asyncio.wait_for(anext(api_iterator), timeout=first_chunk_timeout)
    except asyncio.TimeoutError:
        error_msg = f"上游API在{first_chunk_timeout}秒内未返回第一个数据块"
        logger.error(f"[DIRECT_API_PASSTHROUGH] {error_msg}")
        await _record_failed_request(
            monitoring_service, request_id, display_name, error_msg,
            openai_req, pricing_config, direct_api_service,
            estimate_message_tokens_func, full_messages)
        try:
            await api_iterator.aclose()
        except Exception:
            pass
        raise HTTPException(status_code=504, detail=error_msg)
    except StopAsyncIteration:
        error_msg = "上游API返回空响应"
        logger.error(f"[DIRECT_API_PASSTHROUGH] {error_msg}")
        await _record_failed_request(
            monitoring_service, request_id, display_name, error_msg,
            openai_req, pricing_config, direct_api_service,
            estimate_message_tokens_func, full_messages)
        try:
            await api_iterator.aclose()
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=error_msg)

    # ── 首块错误检测 → 直接抛 HTTPException，触发上层重试 ──
    is_err, error_json = detect_first_chunk_error(first_chunk_bytes)
    if is_err:
        await _handle_first_chunk_error(
            error_json, monitoring_service, request_id, display_name,
            openai_req, pricing_config, direct_api_service,
            estimate_message_tokens_func, full_messages)
        try:
            await api_iterator.aclose()
        except Exception:
            pass
        status_code = map_upstream_error_to_status_code(error_json, default_status_code=500)
        error_details = error_json.get('error', {})
        error_message = error_details.get('message', '') if isinstance(error_details, dict) else str(error_details)
        raise HTTPException(status_code=status_code, detail=error_message or "上游返回错误")

    # ── 首块正常：创建 session 并处理首块 ──
    session = PassthroughStreamSession(
        request_id=request_id,
        display_name=display_name,
        openai_req=openai_req,
        endpoint_config=endpoint_config,
        pricing_config=pricing_config,
        thinking_separator=thinking_separator,
        monitoring_service=monitoring_service,
        direct_api_service=direct_api_service,
        estimate_message_tokens_func=estimate_message_tokens_func,
        estimate_tokens_func=estimate_tokens_func,
        full_messages=full_messages,
        logprobs_collector=logprobs_collector,
    )

    processed_first = session.process_sse_chunk(first_chunk_bytes)

    if session.upstream_error_detected:
        if logprobs_collector is not None:
            try:
                await logprobs_collector.finish(completed=False, error="上游返回错误响应")
            except Exception:
                pass
        try:
            await api_iterator.aclose()
        except Exception:
            pass
        raise HTTPException(status_code=502, detail="上游返回错误响应")

    # ── 流式生成器（处理首块之后的剩余流）──
    async def combined_stream_generator():
        api_task = None
        client_gone = False
        try:
            # 先 yield 已处理的首块
            yield processed_first

            # 继续处理剩余流（下限 1 秒，避免配置异常时 busy-loop）
            heartbeat_interval = max(1, endpoint_config.get("client_disconnect_probe_interval", 180) or 180)
            while True:
                if api_task is None or api_task.done():
                    api_task = asyncio.create_task(anext(api_iterator))
                try:
                    chunk_bytes = await asyncio.wait_for(asyncio.shield(api_task), timeout=heartbeat_interval)
                    api_task = None  # 成功取到值，任务已完成
                except StopAsyncIteration:
                    api_task = None
                    break
                except asyncio.TimeoutError:
                    try:
                        yield f": keep-alive {int(time.time())}\n\n".encode("utf-8")
                    except asyncio.CancelledError:
                        logger.warning(f"[DIRECT_API_PASSTHROUGH] 客户端在上游空闲期间断开: {request_id[:8]}")
                        await session.handle_client_disconnect()
                        try:
                            await api_iterator.aclose()
                        except Exception:
                            pass
                        return
                    continue

                processed_chunk = session.process_sse_chunk(chunk_bytes)

                if session.upstream_error_detected:
                    try:
                        yield processed_chunk
                    except asyncio.CancelledError:
                        logger.warning(f"[DIRECT_API_PASSTHROUGH] 客户端在错误块输出时断开: {request_id[:8]}")
                        await session.handle_client_disconnect()
                        try:
                            await api_iterator.aclose()
                        except Exception:
                            pass
                        return
                    break

                try:
                    yield processed_chunk
                except asyncio.CancelledError:
                    logger.warning(f"[DIRECT_API_PASSTHROUGH] 客户端在流式输出中断开: {request_id[:8]}")
                    await session.handle_client_disconnect()
                    try:
                        await api_iterator.aclose()
                    except Exception:
                        pass
                    return

            session.stream_completed = not session.upstream_error_detected
            session.request_success = (not session.upstream_error_detected) and (session.error_msg is None)

        except asyncio.CancelledError:
            logger.warning(f"[DIRECT_API_PASSTHROUGH] 流式任务被取消: {request_id[:8]}")
            client_gone = True
            await session.handle_client_disconnect()
        except GeneratorExit:
            logger.warning(f"[DIRECT_API_PASSTHROUGH] 生成器被提前关闭: {request_id[:8]}")
            client_gone = True
            session.mark_client_disconnect("Client disconnected (GeneratorExit)")
            raise
        except Exception as e:
            session.error_msg = str(e)
            logger.error(f"[DIRECT_API_PASSTHROUGH] 流式处理中发生异常: {e}", exc_info=True)
        finally:
            if api_task is not None and not api_task.done():
                api_task.cancel()
                try:
                    await api_task
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass
                except Exception:
                    pass
            try:
                await api_iterator.aclose()
            except Exception:
                pass

            # 收尾统计与监控上报（断连路径已上报时返回空列表，不再补发）
            tail_chunks = await session.finalize()
            # 客户端已经走了就只做统计、不再产出数据；否则补发 usage + [DONE]
            if not client_gone:
                try:
                    for tail_chunk in tail_chunks:
                        yield tail_chunk
                except Exception as yield_err:
                    logger.debug(f"[SSE_USAGE] 发送 usage/[DONE] 时客户端已断开: {yield_err}")

    return StreamingResponse(
        combined_stream_generator(),
        media_type="text/event-stream",
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
            'Transfer-Encoding': 'chunked'
        }
    )




# ============================================================
# 非流式透传
# ============================================================

async def _handle_passthrough_non_stream(
    passthrough_request, openai_req, request_id, display_name,
    api_base_url, api_key, endpoint_config, pricing_config,
    thinking_separator, endpoint_path,
    monitoring_service, direct_api_service,
    estimate_message_tokens_func, estimate_tokens_func,
    full_messages, logprobs_collector=None
):
    """处理非流式透传请求"""
    try:
        response_buf = bytearray()
        async for chunk in direct_api_service.call_api_passthrough(
            base_url=api_base_url, api_key=api_key,
            request_body=passthrough_request, endpoint_path=endpoint_path
        ):
            response_buf.extend(chunk)

        response_bytes = bytes(response_buf)
        # 🔧 大响应的 JSON 解析移入线程池，避免卡住事件循环上其他并发流
        if len(response_bytes) > _JSON_OFFLOAD_THRESHOLD_BYTES:
            response_json = await asyncio.to_thread(json.loads, response_bytes)
        else:
            response_json = json.loads(response_bytes.decode('utf-8'))

        if is_error_json(response_json):
            normalized_error = normalize_to_openai_error(response_json)
            error_details = normalized_error.get('error', {})
            status_code = map_upstream_error_to_status_code(normalized_error, default_status_code=500)
            error_message = error_details.get('message', '') if isinstance(error_details, dict) else str(error_details)
            logger.error(f"[DIRECT_API_PASSTHROUGH] 非流式请求失败: {status_code} - {error_message}")

            partial_input_tokens = 0
            try:
                partial_input_tokens = estimate_message_tokens_func(
                    openai_req.get('messages', []), model=display_name)
            except Exception as token_err:
                logger.warning(f"[DIRECT_API_PASSTHROUGH] 非流式错误时输入token计算失败: {token_err}")

            cost_info = direct_api_service.calculate_cost(
                input_tokens=partial_input_tokens, output_tokens=0,
                pricing=pricing_config) if pricing_config else {}

            if cost_info.get("total_cost"):
                logger.info(f"[DIRECT_API_PASSTHROUGH] 非流式失败请求成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")

            monitoring_service.request_end(
                request_id=request_id, success=False, error=error_message,
                input_tokens=partial_input_tokens, output_tokens=0,
                cost_info=cost_info, full_messages=full_messages,
                upstream_usage=response_json.get("usage") if isinstance(response_json.get("usage"), dict) else None,
                system_fingerprint=response_json.get("system_fingerprint"))
            await monitoring_service.broadcast_to_monitors({
                "type": "request_end", "request_id": request_id, "success": False})
            return JSONResponse(status_code=status_code, content=normalized_error)

        if logprobs_collector is not None:
            try:
                logprobs_collector.capture_non_stream_response(response_json)
                logprobs_collector.strip_non_stream_response(response_json)
                await logprobs_collector.finish(completed=True)
            except Exception as collector_err:
                logger.warning("[LOGPROBS_COLLECT] 非流式采集失败，已继续转发响应 request_id=%s: %s", request_id[:8], collector_err)

        # 原样保留上游返回的原生 usage（在任何本地统计加工之前）
        # 必须深拷贝：下方会按 completion_tokens_mode 就地改写 response_json["usage"]，
        # 只持引用的话监控里记录的"上游原生值"会被一起改掉
        native_usage = response_json.get("usage")
        upstream_usage = copy.deepcopy(native_usage) if isinstance(native_usage, dict) else None
        # 原样保留上游返回的 system_fingerprint（DeepSeek 等 OpenAI 兼容 API 顶层字段）
        system_fingerprint = response_json.get("system_fingerprint")

        # 提取 token 统计
        input_tokens, output_tokens, reasoning_tokens, total_tokens, cached_tokens = \
            _extract_tokens_from_response(response_json, endpoint_config)

        # 把 tool_calls arguments 中的 \uXXXX 转义规范化为明文
        # （下方会重新编码 response_bytes，客户端拿到的即为规范化结果）
        normalize_response_tool_args(response_json)

        # 提取内容并应用思考分隔
        content, reasoning_content, response_tool_calls, response_message, response_json = await asyncio.to_thread(_extract_and_split_content,
            response_json, thinking_separator, direct_api_service)

        local_stats = endpoint_config.get("token_stats_mode") == "local"
        if local_stats or not response_json.get("usage") or (input_tokens == 0 and output_tokens == 0):
            if local_stats:
                logger.info(f"[TOKEN_STATS_LOCAL] local统计模式：使用本地tokenizer计算")
            else:
                logger.warning(f"[DIRECT_API] API未返回usage或tokens为0，使用tokenizer计算")
            output_text = (reasoning_content or "") + (content or "")
            try:
                input_tokens = await estimate_message_tokens_non_blocking(
                    estimate_message_tokens_func,
                    openai_req.get('messages', []), display_name)
                output_tokens = await estimate_text_tokens_non_blocking(
                    estimate_tokens_func, output_text, display_name)
                logger.info(f"[DIRECT_API] Tokenizer计算结果: 输入={input_tokens}, 输出={output_tokens}")
            except Exception as token_error:
                logger.warning(f"[DIRECT_API] Tokenizer计算失败: {token_error}，使用简单估算")
                input_tokens = sum(len(str(m.get('content', ''))) for m in openai_req.get('messages', [])) // 4
                output_tokens = len(output_text) // 4

        # 按模型配置归一下发给客户端的 usage 口径：
        # merge=completion_tokens 含思考（总输出），separate=只含正文；
        # 两种模式都在 completion_tokens_details.reasoning_tokens 保留思考量。
        # 统计与计费仍使用上方解析出的真实总输出，切换该配置不影响成本。
        if isinstance(response_json.get("usage"), dict):
            usage_tokens = apply_usage_tokens(
                response_json["usage"], get_completion_tokens_mode(endpoint_config))
            if usage_tokens.changed:
                logger.info(
                    f"[COMPLETION_TOKENS_MODE] 思考={usage_tokens.reasoning_tokens} "
                    f"→ completion_tokens={usage_tokens.reported_completion_tokens}")

        # 🔧 关键修复：_extract_and_split_content 可能修改了 response_json
        # （如添加 reasoning_content、字段转换等），必须重新编码以确保客户端收到修改后的数据
        # （大响应的序列化同样移入线程池；用解析前的字节数估计序列化后量级）
        if len(response_bytes) > _JSON_OFFLOAD_THRESHOLD_BYTES:
            response_bytes = (await asyncio.to_thread(
                json.dumps, response_json, ensure_ascii=False)).encode('utf-8')
        else:
            response_bytes = json.dumps(response_json, ensure_ascii=False).encode('utf-8')

        cost_info = direct_api_service.calculate_cost(
            input_tokens=input_tokens, output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            pricing=pricing_config) if pricing_config else {}

        monitoring_service.request_end(
            request_id=request_id, success=True,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            response_content=content,
            reasoning_content=reasoning_content,
            cost_info=cost_info, full_messages=full_messages,
            response_message=response_message,
            response_tool_calls=response_tool_calls,
            upstream_usage=upstream_usage,
            system_fingerprint=system_fingerprint)

        await monitoring_service.broadcast_to_monitors({
            "type": "request_end", "request_id": request_id, "success": True})

        logger.info(f"[DIRECT_API_PASSTHROUGH] 非流式请求完成: {request_id[:8]}")
        logger.info(f"  - 输入tokens: {input_tokens}")
        logger.info(f"  - 输出tokens: {output_tokens}")
        if reasoning_content:
            logger.info(f"  - 思考内容: {len(reasoning_content)} 字符")
        if cost_info.get("total_cost"):
            logger.info(f"  - 总成本: {cost_info['total_cost']} {cost_info['currency']}")

        return Response(content=response_bytes, media_type="application/json")

    except Exception as e:
        logger.error(f"[DIRECT_API_PASSTHROUGH] 非流式处理失败: {e}", exc_info=True)
        monitoring_service.request_end(
            request_id=request_id, success=False, error=str(e),
            full_messages=full_messages)
        await monitoring_service.broadcast_to_monitors({
            "type": "request_end", "request_id": request_id, "success": False})
        raise


# ============================================================
# 辅助函数
# ============================================================

async def _record_failed_request(monitoring_service, request_id, display_name,
                                  error_msg, openai_req, pricing_config,
                                  direct_api_service, estimate_message_tokens_func,
                                  full_messages):
    """记录失败请求的token和成本"""
    partial_input_tokens = 0
    try:
        partial_input_tokens = await estimate_message_tokens_non_blocking(
            estimate_message_tokens_func,
            openai_req.get('messages', []), display_name)
    except Exception as token_err:
        logger.warning(f"[DIRECT_API_PASSTHROUGH] 输入token计算失败: {token_err}")

    cost_info = direct_api_service.calculate_cost(
        input_tokens=partial_input_tokens, output_tokens=0,
        pricing=pricing_config) if pricing_config else {}
    if cost_info.get("total_cost"):
        logger.info(f"[DIRECT_API_PASSTHROUGH] 失败请求成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")

    monitoring_service.request_end(
        request_id=request_id, success=False, error=error_msg,
        input_tokens=partial_input_tokens, output_tokens=0,
        cost_info=cost_info, full_messages=full_messages)
    await monitoring_service.broadcast_to_monitors(
        {"type": "request_end", "request_id": request_id, "success": False})


async def _handle_first_chunk_error(error_json, monitoring_service, request_id,
                                     display_name, openai_req, pricing_config,
                                     direct_api_service, estimate_message_tokens_func,
                                     full_messages):
    """处理第一个块中的错误"""
    error_details = error_json.get('error', {})
    error_message = error_details.get('message', '') if isinstance(error_details, dict) else str(error_details)
    logger.error(f"[DIRECT_API_PASSTHROUGH] 请求失败，上游返回错误: {error_json}")
    await _record_failed_request(
        monitoring_service, request_id, display_name, error_message,
        openai_req, pricing_config, direct_api_service,
        estimate_message_tokens_func, full_messages)


def _extract_tokens_from_response(response_json, endpoint_config):
    """从响应JSON中提取token统计。

    output_tokens 统一返回「正文 + 思考」的真实总输出，成本和监控都按这个
    口径走；下游客户端看到的 completion_tokens 是总量还是正文，由
    completion_tokens_mode 在改写 response_json["usage"] 时单独决定。
    """
    usage = response_json.get("usage", {})
    input_tokens = 0
    output_tokens = 0
    reasoning_tokens = 0
    total_tokens = 0
    cached_tokens = 0

    if usage:
        base_prompt_tokens = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)

        prompt_details = usage.get("prompt_tokens_details", {})
        cached_tokens = prompt_details.get("cached_tokens", 0) if isinstance(prompt_details, dict) else 0

        cached_mode = endpoint_config.get('cached_tokens_mode', 'reverse')
        if cached_mode == 'forward':
            input_tokens = base_prompt_tokens + cached_tokens
            if cached_tokens > 0:
                logger.info(f"[CACHED_TOKENS] 非流式正向模式: prompt {base_prompt_tokens} + cached {cached_tokens} = {input_tokens}")
        elif cached_mode == 'reverse':
            input_tokens = base_prompt_tokens
            if cached_tokens > 0:
                logger.info(f"[CACHED_TOKENS] 非流式反向模式: total {input_tokens}, cached {cached_tokens}")
        else:
            input_tokens = base_prompt_tokens

        # 思考 token 大多放在 usage.completion_tokens_details.reasoning_tokens，
        # 旧版只读顶层字段所以恒为 0，既没上报也没计入成本
        usage_tokens = resolve_usage_tokens(usage, MODE_MERGE)
        reasoning_tokens = usage_tokens.reasoning_tokens
        output_tokens = usage_tokens.output_tokens
        total_tokens = usage_tokens.total_tokens
        # 正向缓存模式下本地修正过输入量，上游 total 不含这部分，需要重新合成
        if cached_mode == 'forward' and cached_tokens > 0:
            total_tokens = max(total_tokens, input_tokens + output_tokens)

        if reasoning_tokens > 0:
            logger.info(f"[DIRECT_API] 检测到思考token: {reasoning_tokens}")
        logger.info(
            f"[DIRECT_API] 使用API返回的token统计: 输入={input_tokens}, "
            f"输出={output_tokens}(正文{usage_tokens.content_tokens}+思考{reasoning_tokens})")

    return input_tokens, output_tokens, reasoning_tokens, total_tokens, cached_tokens


def _extract_response_content(response_json):
    """从响应JSON中提取正文内容"""
    if "choices" in response_json and len(response_json["choices"]) > 0:
        message = response_json["choices"][0].get("message", {})
        return message.get("content", "")
    return ""


def _extract_and_split_content(response_json, thinking_separator, direct_api_service):
    """提取响应内容并应用思考分隔符"""
    content = ""
    reasoning_content = ""
    tool_calls = None
    response_message = None
    if "choices" in response_json and len(response_json["choices"]) > 0:
        message = response_json["choices"][0].get("message", {})
        content = message.get("content", "")
        reasoning_content = message.get("reasoning_content", "") or message.get("reasoning", "")
        tool_calls = extract_tool_calls_from_message(message)

        # 缓存 OpenRouter reasoning_details（含 Anthropic thinking 签名），供下一轮回传恢复
        reasoning_details = message.get("reasoning_details")
        if isinstance(reasoning_details, list) and reasoning_details and reasoning_content:
            store_reasoning_details(reasoning_content, reasoning_details)

        if "reasoning" in message and "reasoning_content" not in message and reasoning_content:
            message["reasoning_content"] = message.pop("reasoning")
            response_json["choices"][0]["message"] = message
            logger.debug(f"[REASONING_FIELD_CONVERT] 非流式响应: 将 reasoning 转换为 reasoning_content")

        if thinking_separator and content and not reasoning_content:
            reasoning_part, main_part = direct_api_service.split_thinking_content(
                content, thinking_separator)
            if reasoning_part:
                message["reasoning_content"] = reasoning_part
                message["content"] = main_part
                content = main_part
                reasoning_content = reasoning_part
                logger.info(f"[THINKING_SPLIT] 非流式响应已应用思考分隔")

    response_message = build_response_message(content, reasoning_content, tool_calls)
    return content, reasoning_content, tool_calls, response_message, response_json
