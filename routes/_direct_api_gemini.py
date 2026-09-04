"""
Direct API - Gemini 原生 API 处理
"""
import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

from core.constants import TimeoutDefaults
from utils.monitor_params import build_monitor_request_params
from utils.usage_tokens import (
    compose_chat_usage,
    extract_prompt_tokens,
    extract_reasoning_tokens,
    get_completion_tokens_mode,
    resolve_usage_tokens,
    total_output_tokens,
)
from converters.gemini_interactions import (
    InteractionsStreamConverter,
    convert_interactions_to_openai_response,
)
from ._direct_api_utils import (
    append_tool_call_delta,
    build_response_message,
    estimate_message_tokens_non_blocking,
    estimate_text_tokens_non_blocking,
    finalize_tool_calls,
    map_upstream_error_to_status_code,
)

logger = logging.getLogger(__name__)


async def handle_gemini_native_direct(
    openai_req: dict,
    model_name: str,
    target_model_id: str,
    display_name: str,
    api_key: Optional[str],
    api_base_url: Optional[str],
    endpoint_config: dict,
    pricing_config: dict,
    monitoring_service,
    direct_api_service,
    estimate_message_tokens_func,
    estimate_tokens_func,
    full_messages: Optional[list] = None,
    CONFIG: Optional[dict] = None
):
    """处理Gemini原生API的Direct请求"""
    logger.info(f"[GEMINI_NATIVE] 使用Gemini原生API格式")

    request_id = str(uuid.uuid4())

    # 思考 token 是否并入下发给下游的 completion_tokens（模型级配置，
    # 两个内嵌生成器通过闭包读取）
    completion_tokens_mode = get_completion_tokens_mode(endpoint_config)

    extra_kwargs = {}
    custom_params = endpoint_config.get("custom_params", {})
    if custom_params and isinstance(custom_params, dict):
        extra_kwargs.update(custom_params)
        logger.info(f"[GEMINI_NATIVE] 已添加自定义参数:")
        for key, value in custom_params.items():
            logger.info(f"  - {key}: {value}")

    # 合并附加主体参数（在自定义参数之后，同名键优先）
    extra_body_params = endpoint_config.get("extra_body_params", {})
    if extra_body_params and isinstance(extra_body_params, dict):
        extra_kwargs.update(extra_body_params)
        logger.info(f"[GEMINI_NATIVE] 已添加附加主体参数:")
        for key, value in extra_body_params.items():
            logger.info(f"  - {key}: {value}")

    thinking_config = None
    client_reasoning_effort = openai_req.get("reasoning_effort")
    enable_thinking = endpoint_config.get("enable_thinking")
    if enable_thinking is None and client_reasoning_effort:
        enable_thinking = True
    if enable_thinking is True:
        reasoning_effort = client_reasoning_effort or endpoint_config.get("reasoning_effort")
        is_gemini_3 = "gemini-3" in (target_model_id or model_name or "").lower()
        if reasoning_effort and is_gemini_3:
            # Gemini 3 generateContent 使用 thinkingLevel；Responses 的
            # reasoning.effort 在进入此处前已归一化为 reasoning_effort。
            level_map = {
                "none": "MINIMAL",
                "minimal": "MINIMAL",
                "low": "LOW",
                "medium": "MEDIUM",
                "high": "HIGH",
                "xhigh": "HIGH",
                "max": "HIGH",
            }
            thinking_config = {
                "thinkingLevel": level_map.get(str(reasoning_effort).lower(), "MEDIUM"),
                "includeThoughts": True,
            }
            logger.info(
                f"[GEMINI_NATIVE] reasoning_effort={reasoning_effort} "
                f"映射为 thinkingLevel={thinking_config['thinkingLevel']}"
            )
        elif reasoning_effort:
            # Gemini 2.5 及旧模型使用 thinkingBudget。
            effort_budget_map = {
                "minimal": 512, "low": 4096, "medium": 12288,
                "high": 24576, "xhigh": 32768, "max": 32768,
            }
            thinking_budget = effort_budget_map.get(str(reasoning_effort).lower(), 20000)
            thinking_config = {
                "thinkingBudget": thinking_budget,
                "includeThoughts": True,
            }
            logger.info(f"[GEMINI_NATIVE] reasoning_effort={reasoning_effort} 映射为 thinkingBudget={thinking_budget}")
        else:
            if is_gemini_3:
                thinking_level = str(endpoint_config.get("thinking_level", "MEDIUM")).upper()
                thinking_config = {
                    "thinkingLevel": thinking_level,
                    "includeThoughts": True,
                }
            else:
                thinking_config = {
                    "thinkingBudget": endpoint_config.get("thinking_budget", 20000),
                    "includeThoughts": True,
                }
        logger.info(
            f"[GEMINI_NATIVE] 已启用思维链模式 "
            f"(control={thinking_config.get('thinkingLevel', thinking_config.get('thinkingBudget'))})"
        )
    elif enable_thinking is False:
        thinking_config = {
            "includeThoughts": False
        }
        logger.info(f"[GEMINI_NATIVE] 已显式关闭思维链模式")
    # enable_thinking 为 None/缺失时不发送 thinking_config

    # 自定义参数/附加主体参数以独立字段记录到监控日志，避免平铺覆盖监控字段
    # （显式 dict 注解：后续会塞入 dict 类型的值，与 _direct_api_passthrough 写法对齐）
    monitor_extra_params: dict = {"upstream_model": target_model_id}
    if isinstance(custom_params, dict) and custom_params:
        monitor_extra_params["custom_params"] = custom_params
    if isinstance(extra_body_params, dict) and extra_body_params:
        monitor_extra_params["extra_body_params"] = extra_body_params
    if thinking_config:
        monitor_extra_params["thinking_config"] = thinking_config
    # 记录思考相关配置到监控日志
    for key in ("enable_thinking", "reasoning_effort", "thinking_budget", "force_stream"):
        val = endpoint_config.get(key)
        if val is not None and val != "":
            monitor_extra_params[key] = val

    monitoring_service.request_start(
        request_id=request_id,
        model=display_name,
        messages_count=len(openai_req.get("messages", [])),
        session_id=None,
        mode="gemini_native",
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
        is_stream = openai_req.get("stream", False)
        logger.info(f"[GEMINI_NATIVE] 模型映射: '{model_name}' -> '{target_model_id}'")

        # 🔧 上游协议选择：generate_content（默认，旧行为）或 interactions（新 API）
        upstream_protocol = endpoint_config.get("upstream_protocol", "generate_content")
        if upstream_protocol == "interactions":
            logger.info(f"[GEMINI_NATIVE] 使用 Interactions 上游协议")
            gemini_generator = direct_api_service.call_gemini_interactions_api(
                api_key=api_key,
                model=target_model_id,
                messages=openai_req.get("messages", []),
                stream=is_stream,
                temperature=openai_req.get("temperature"),
                top_p=openai_req.get("top_p"),
                max_tokens=(
                    openai_req.get("max_tokens")
                    or openai_req.get("max_completion_tokens")
                ),
                base_url=api_base_url,
                thinking_config=thinking_config,
                tools=openai_req.get("tools"),
                tool_choice=openai_req.get("tool_choice"),
                response_format=openai_req.get("response_format"),
                stop_sequences=(
                    openai_req.get("stop")
                    if isinstance(openai_req.get("stop"), list)
                    else ([openai_req["stop"]] if isinstance(openai_req.get("stop"), str) else None)
                ),
                extra_body=extra_kwargs if extra_kwargs else None,
            )
        else:
            gemini_generator = direct_api_service.call_gemini_native_api(
                api_key=api_key,
                model=target_model_id,
                messages=openai_req.get("messages", []),
                stream=is_stream,
                temperature=openai_req.get("temperature"),
                top_p=openai_req.get("top_p"),
                max_tokens=(
                    openai_req.get("max_tokens")
                    or openai_req.get("max_completion_tokens")
                ),
                base_url=api_base_url,
                thinking_config=thinking_config,
                tools=openai_req.get("tools"),
                tool_choice=openai_req.get("tool_choice"),
                extra_body=extra_kwargs if extra_kwargs else None,
            )

        first_chunk_timeout = TimeoutDefaults.FIRST_CHUNK_TIMEOUT
        if CONFIG:
            first_chunk_timeout = CONFIG.get("first_chunk_timeout_seconds", TimeoutDefaults.FIRST_CHUNK_TIMEOUT)

        if is_stream:
            async def gemini_stream_generator():
                content_parts = []
                reasoning_parts = []
                tool_call_accumulator = {}
                input_tokens = 0
                output_tokens = 0
                total_tokens = 0
                reasoning_tokens = 0  # 思考 token（含在 output_tokens 里）
                upstream_usage = None  # Gemini 原生 usageMetadata（原样保留，供日志记录）
                request_success = False
                stream_completed = False
                error_msg = None
                # 客户端已消失时 finally 不能再 yield（见下方 GeneratorExit 处理）
                client_gone = False
                # 首块超时/空响应/预读错误时，已在分支中 yield 了 error 事件；
                # 设此标志阻止 finally 再补发空的 usage/[DONE] 尾部块
                tail_suppressed = False

                try:
                    api_task = asyncio.create_task(anext(gemini_generator))
                    # 下限 1 秒：避免配置为 0/负数时 busy-loop 狂发 keep-alive（与透传链路对齐）
                    heartbeat_interval = max(1, min(endpoint_config.get("client_disconnect_probe_interval", 30) or 30, 30))
                    first_chunk_wait_start = time.time()

                    while not api_task.done():
                        yield f": keep-alive {int(time.time())}\n\n"
                        if time.time() - first_chunk_wait_start > first_chunk_timeout:
                            api_task.cancel()
                            try:
                                await api_task
                            except asyncio.CancelledError:
                                pass
                            error_msg = f"Gemini Native API返回空响应或在{first_chunk_timeout}秒内未返回第一个数据块"
                            logger.error(f"[GEMINI_NATIVE] {error_msg}")
                            partial_input_tokens = 0
                            try:
                                partial_input_tokens = estimate_message_tokens_func(
                                    openai_req.get('messages', []), model=display_name)
                            except Exception as token_err:
                                logger.warning(f"[GEMINI_NATIVE] 超时时输入token计算失败: {token_err}")
                            cost_info = direct_api_service.calculate_cost(
                                input_tokens=partial_input_tokens, output_tokens=0,
                                pricing=pricing_config) if pricing_config else {}
                            if cost_info.get("total_cost"):
                                logger.info(f"[GEMINI_NATIVE] 失败请求成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")
                            monitoring_service.request_end(
                                request_id=request_id, success=False, error=error_msg,
                                input_tokens=partial_input_tokens, output_tokens=0,
                                cost_info=cost_info, full_messages=full_messages)
                            await monitoring_service.broadcast_to_monitors(
                                {"type": "request_end", "request_id": request_id, "success": False})
                            # 向客户端发送错误事件，避免客户端看到空 SSE 流以为成功
                            yield f"data: {json.dumps({'error': {'message': error_msg, 'type': 'timeout'}}, ensure_ascii=False)}\n\n"
                            tail_suppressed = True
                            return
                        try:
                            await asyncio.wait_for(asyncio.shield(api_task), timeout=heartbeat_interval)
                        except asyncio.TimeoutError:
                            continue

                    try:
                        first_gemini_chunk = await api_task
                    except StopAsyncIteration:
                        error_msg = f"Gemini Native API返回空响应或在{first_chunk_timeout}秒内未返回第一个数据块"
                        logger.error(f"[GEMINI_NATIVE] {error_msg}")
                        partial_input_tokens = 0
                        try:
                            partial_input_tokens = estimate_message_tokens_func(
                                openai_req.get('messages', []), model=display_name)
                        except Exception as token_err:
                            logger.warning(f"[GEMINI_NATIVE] 空响应时输入token计算失败: {token_err}")
                        cost_info = direct_api_service.calculate_cost(
                            input_tokens=partial_input_tokens, output_tokens=0,
                            pricing=pricing_config) if pricing_config else {}
                        if cost_info.get("total_cost"):
                            logger.info(f"[GEMINI_NATIVE] 失败请求成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")
                        monitoring_service.request_end(
                            request_id=request_id, success=False, error=error_msg,
                            input_tokens=partial_input_tokens, output_tokens=0,
                            cost_info=cost_info, full_messages=full_messages)
                        await monitoring_service.broadcast_to_monitors(
                            {"type": "request_end", "request_id": request_id, "success": False})
                        yield f"data: {json.dumps({'error': {'message': error_msg, 'type': 'empty_response'}}, ensure_ascii=False)}\n\n"
                        tail_suppressed = True
                        return

                    if "error" in first_gemini_chunk:
                        error_detail = first_gemini_chunk.get("error")
                        logger.error(f"[GEMINI_NATIVE] 流式预读检测到错误: {first_gemini_chunk}")
                        partial_input_tokens = 0
                        try:
                            partial_input_tokens = estimate_message_tokens_func(
                                openai_req.get('messages', []), model=display_name)
                        except Exception as token_err:
                            logger.warning(f"[GEMINI_NATIVE] 错误时输入token计算失败: {token_err}")
                        cost_info = direct_api_service.calculate_cost(
                            input_tokens=partial_input_tokens, output_tokens=0,
                            pricing=pricing_config) if pricing_config else {}
                        if cost_info.get("total_cost"):
                            logger.info(f"[GEMINI_NATIVE] 失败请求成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")
                        monitoring_service.request_end(
                            request_id=request_id, success=False, error=str(error_detail),
                            input_tokens=partial_input_tokens, output_tokens=0,
                            cost_info=cost_info, full_messages=full_messages)
                        await monitoring_service.broadcast_to_monitors(
                            {"type": "request_end", "request_id": request_id, "success": False})
                        yield f"data: {json.dumps({'error': first_gemini_chunk['error']}, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        tail_suppressed = True
                        return

                    # 处理预读的第一个块
                    for gemini_chunk in [first_gemini_chunk]:
                        _meta = gemini_chunk.get("usageMetadata")
                        if isinstance(_meta, dict) and _meta:
                            upstream_usage = _meta
                        openai_chunk = direct_api_service.convert_gemini_response_to_openai(
                            gemini_chunk, display_name, request_id, is_stream_chunk=True,
                            completion_tokens_mode=completion_tokens_mode)

                        delta = openai_chunk.get("choices", [{}])[0].get("delta", {})
                        delta_content = delta.get("content", "")
                        delta_reasoning = delta.get("reasoning_content", "")
                        delta_tool_calls = delta.get("tool_calls")

                        if delta_content:
                            content_parts.append(delta_content)
                        if delta_reasoning:
                            reasoning_parts.append(delta_reasoning)
                        if delta_tool_calls:
                            append_tool_call_delta(tool_call_accumulator, delta_tool_calls)

                        if "usage" in openai_chunk and openai_chunk["usage"]:
                            usage = openai_chunk["usage"]
                            input_tokens = extract_prompt_tokens(usage)
                            # 计费/监控按真实总输出（正文+思考），不受下游展示口径影响
                            output_tokens = total_output_tokens(usage)
                            reasoning_tokens = extract_reasoning_tokens(usage)
                            total_tokens = resolve_usage_tokens(usage).total_tokens

                        yield f"data: {json.dumps(openai_chunk, ensure_ascii=False)}\n\n"

                    # 继续处理剩余流
                    async for gemini_chunk in gemini_generator:
                        if "error" in gemini_chunk:
                            error_msg = str(gemini_chunk.get("error"))
                            openai_error = {"error": gemini_chunk["error"]}
                            yield f"data: {json.dumps(openai_error, ensure_ascii=False)}\n\n"
                            break

                        _meta = gemini_chunk.get("usageMetadata")
                        if isinstance(_meta, dict) and _meta:
                            upstream_usage = _meta

                        openai_chunk = direct_api_service.convert_gemini_response_to_openai(
                            gemini_chunk, display_name, request_id, is_stream_chunk=True,
                            completion_tokens_mode=completion_tokens_mode)

                        delta = openai_chunk.get("choices", [{}])[0].get("delta", {})
                        delta_content = delta.get("content", "")
                        delta_reasoning = delta.get("reasoning_content", "")
                        delta_tool_calls = delta.get("tool_calls")

                        if delta_content:
                            content_parts.append(delta_content)
                        if delta_reasoning:
                            reasoning_parts.append(delta_reasoning)
                        if delta_tool_calls:
                            append_tool_call_delta(tool_call_accumulator, delta_tool_calls)

                        if "usage" in openai_chunk and openai_chunk["usage"]:
                            usage = openai_chunk["usage"]
                            input_tokens = extract_prompt_tokens(usage)
                            # 计费/监控按真实总输出（正文+思考），不受下游展示口径影响
                            output_tokens = total_output_tokens(usage)
                            reasoning_tokens = extract_reasoning_tokens(usage)
                            total_tokens = resolve_usage_tokens(usage).total_tokens

                        yield f"data: {json.dumps(openai_chunk, ensure_ascii=False)}\n\n"

                    stream_completed = (error_msg is None)
                    request_success = (error_msg is None)

                except GeneratorExit:
                    # 🔧 GeneratorExit 必须重新抛出，否则 Python 在生成器结束时
                    # 会报 "async generator ignored GeneratorExit"
                    client_gone = True
                    if stream_completed or (content_parts and not error_msg):
                        request_success = True
                        logger.info(f"[GEMINI_NATIVE] 生成器被关闭，但流已完成或有有效输出，标记为成功")
                    else:
                        logger.warning(f"[GEMINI_NATIVE] 生成器被提前关闭")
                    raise
                except asyncio.CancelledError:
                    # 🔧 客户端断开时 asyncio 向生成器注入 CancelledError（BaseException），
                    # 不会被 except Exception 捕获，必须显式处理以防 request_end 丢失
                    client_gone = True
                    if stream_completed or (content_parts and not error_msg):
                        request_success = True
                        logger.info(f"[GEMINI_NATIVE] 请求被取消，但流已完成或有有效输出，标记为成功")
                    else:
                        logger.warning(f"[GEMINI_NATIVE] 请求被取消，客户端断开")
                    raise
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"[GEMINI_NATIVE] 流式处理失败: {e}", exc_info=True)
                finally:
                    if input_tokens == 0 or output_tokens == 0:
                        # 🔧 修复：旧版这里硬编码模型名 "gemma"，绕过了
                        # config.jsonc 中 tokenizer_config 的按模型映射，
                        # 与同文件非流式分支（用 display_name）结果不一致
                        try:
                            if input_tokens == 0:
                                input_tokens = await asyncio.shield(estimate_message_tokens_non_blocking(
                                    estimate_message_tokens_func,
                                    openai_req.get('messages', []), display_name))
                            if output_tokens == 0:
                                accumulated_content = ''.join(content_parts)
                                accumulated_reasoning = ''.join(reasoning_parts)
                                total_output_text = accumulated_reasoning + accumulated_content
                                if total_output_text:
                                    output_tokens = await asyncio.shield(estimate_text_tokens_non_blocking(
                                        estimate_tokens_func, total_output_text, display_name))
                        except asyncio.CancelledError:
                            # shield 也会在取消态下抛出 CancelledError，此时保持 0 值
                            logger.warning(f"[GEMINI_NATIVE] Token 估算被取消，使用上游返回的 usage 值")
                        except Exception as token_error:
                            logger.error(f"[GEMINI_NATIVE] Token计算失败: {token_error}")

                    cost_info = direct_api_service.calculate_cost(
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        cached_tokens=0,
                        pricing=pricing_config) if pricing_config else {}
                    final_content = ''.join(content_parts)
                    final_reasoning = ''.join(reasoning_parts)
                    final_tool_calls = finalize_tool_calls(tool_call_accumulator)

                    monitoring_service.request_end(
                        request_id=request_id, success=request_success,
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        cached_tokens=0, error=error_msg,
                        response_content=final_content,
                        reasoning_content=final_reasoning,
                        cost_info=cost_info, full_messages=full_messages,
                        response_message=build_response_message(final_content, final_reasoning, final_tool_calls),
                        response_tool_calls=final_tool_calls,
                        upstream_usage=upstream_usage)

                    await monitoring_service.broadcast_to_monitors({
                        "type": "request_end", "request_id": request_id,
                        "success": request_success})

                    logger.info(f"[GEMINI_NATIVE] 流式请求完成: {request_id[:8]}")
                    logger.info(f"  - 输入tokens: {input_tokens}, 输出tokens: {output_tokens}")
                    if cost_info.get("total_cost"):
                        logger.info(f"  - 总成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")

                    if not client_gone and not tail_suppressed:
                        try:
                            final_total_tokens = total_tokens if total_tokens > 0 else (input_tokens + output_tokens)
                            usage_final_chunk = {
                                "id": f"chatcmpl-{request_id}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": display_name,
                                "choices": [],
                                "usage": compose_chat_usage(
                                    prompt_tokens=input_tokens,
                                    output_tokens=output_tokens,
                                    reasoning_tokens=reasoning_tokens,
                                    completion_mode=completion_tokens_mode,
                                    total_tokens=final_total_tokens,
                                )
                            }
                            yield f"data: {json.dumps(usage_final_chunk, ensure_ascii=False)}\n\n"
                            yield "data: [DONE]\n\n"
                        except Exception as yield_err:
                            logger.debug(f"[GEMINI_NATIVE] 发送 usage/[DONE] 时客户端已断开: {yield_err}")

            async def interactions_stream_generator():
                """Interactions 上游流式生成器：事件流 → OpenAI chunk 流。

                与 gemini_stream_generator 骨架一致（keep-alive/首块超时/取消/
                统计/尾部块），区别在于转换部分：interactions 是事件流（状态机
                InteractionsStreamConverter 跨事件维护 step 状态）。
                """
                content_parts = []
                reasoning_parts = []
                tool_call_accumulator = {}
                input_tokens = 0
                output_tokens = 0
                total_tokens = 0
                reasoning_tokens = 0  # 思考 token（含在 output_tokens 里）
                upstream_usage = None  # interactions usage（interaction.completed 事件携带）
                request_success = False
                stream_completed = False
                error_msg = None
                client_gone = False
                tail_suppressed = False
                converter = InteractionsStreamConverter(display_name, request_id, completion_tokens_mode)

                try:
                    api_task = asyncio.create_task(anext(gemini_generator))
                    heartbeat_interval = max(1, min(endpoint_config.get("client_disconnect_probe_interval", 30) or 30, 30))
                    first_chunk_wait_start = time.time()

                    while not api_task.done():
                        yield f": keep-alive {int(time.time())}\n\n"
                        if time.time() - first_chunk_wait_start > first_chunk_timeout:
                            api_task.cancel()
                            try:
                                await api_task
                            except asyncio.CancelledError:
                                pass
                            error_msg = f"Gemini Interactions API返回空响应或在{first_chunk_timeout}秒内未返回第一个数据块"
                            logger.error(f"[GEMINI_INTERACTIONS] {error_msg}")
                            partial_input_tokens = 0
                            try:
                                partial_input_tokens = estimate_message_tokens_func(
                                    openai_req.get('messages', []), model=display_name)
                            except Exception as token_err:
                                logger.warning(f"[GEMINI_INTERACTIONS] 超时时输入token计算失败: {token_err}")
                            cost_info = direct_api_service.calculate_cost(
                                input_tokens=partial_input_tokens, output_tokens=0,
                                pricing=pricing_config) if pricing_config else {}
                            if cost_info.get("total_cost"):
                                logger.info(f"[GEMINI_INTERACTIONS] 失败请求成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")
                            monitoring_service.request_end(
                                request_id=request_id, success=False, error=error_msg,
                                input_tokens=partial_input_tokens, output_tokens=0,
                                cost_info=cost_info, full_messages=full_messages)
                            await monitoring_service.broadcast_to_monitors(
                                {"type": "request_end", "request_id": request_id, "success": False})
                            yield f"data: {json.dumps({'error': {'message': error_msg, 'type': 'timeout'}}, ensure_ascii=False)}\n\n"
                            tail_suppressed = True
                            return
                        try:
                            await asyncio.wait_for(asyncio.shield(api_task), timeout=heartbeat_interval)
                        except asyncio.TimeoutError:
                            continue

                    try:
                        first_event = await api_task
                    except StopAsyncIteration:
                        error_msg = f"Gemini Interactions API返回空响应或在{first_chunk_timeout}秒内未返回第一个数据块"
                        logger.error(f"[GEMINI_INTERACTIONS] {error_msg}")
                        partial_input_tokens = 0
                        try:
                            partial_input_tokens = estimate_message_tokens_func(
                                openai_req.get('messages', []), model=display_name)
                        except Exception as token_err:
                            logger.warning(f"[GEMINI_INTERACTIONS] 空响应时输入token计算失败: {token_err}")
                        cost_info = direct_api_service.calculate_cost(
                            input_tokens=partial_input_tokens, output_tokens=0,
                            pricing=pricing_config) if pricing_config else {}
                        if cost_info.get("total_cost"):
                            logger.info(f"[GEMINI_INTERACTIONS] 失败请求成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")
                        monitoring_service.request_end(
                            request_id=request_id, success=False, error=error_msg,
                            input_tokens=partial_input_tokens, output_tokens=0,
                            cost_info=cost_info, full_messages=full_messages)
                        await monitoring_service.broadcast_to_monitors(
                            {"type": "request_end", "request_id": request_id, "success": False})
                        yield f"data: {json.dumps({'error': {'message': error_msg, 'type': 'empty_response'}}, ensure_ascii=False)}\n\n"
                        tail_suppressed = True
                        return

                    if "error" in first_event:
                        error_detail = first_event.get("error")
                        logger.error(f"[GEMINI_INTERACTIONS] 流式预读检测到错误: {first_event}")
                        partial_input_tokens = 0
                        try:
                            partial_input_tokens = estimate_message_tokens_func(
                                openai_req.get('messages', []), model=display_name)
                        except Exception as token_err:
                            logger.warning(f"[GEMINI_INTERACTIONS] 错误时输入token计算失败: {token_err}")
                        cost_info = direct_api_service.calculate_cost(
                            input_tokens=partial_input_tokens, output_tokens=0,
                            pricing=pricing_config) if pricing_config else {}
                        if cost_info.get("total_cost"):
                            logger.info(f"[GEMINI_INTERACTIONS] 失败请求成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")
                        monitoring_service.request_end(
                            request_id=request_id, success=False, error=str(error_detail),
                            input_tokens=partial_input_tokens, output_tokens=0,
                            cost_info=cost_info, full_messages=full_messages)
                        await monitoring_service.broadcast_to_monitors(
                            {"type": "request_end", "request_id": request_id, "success": False})
                        yield f"data: {json.dumps({'error': first_event['error']}, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        tail_suppressed = True
                        return

                    # 预读的第一个事件
                    for openai_chunk in converter.feed(first_event):
                        if "error" in openai_chunk:
                            error_msg = str(openai_chunk.get("error"))
                            yield f"data: {json.dumps({'error': openai_chunk['error']}, ensure_ascii=False)}\n\n"
                            tail_suppressed = True
                            return

                        choices = openai_chunk.get("choices") or []
                        delta = choices[0].get("delta", {}) if choices else {}
                        delta_content = delta.get("content", "")
                        delta_reasoning = delta.get("reasoning_content", "")
                        delta_tool_calls = delta.get("tool_calls")

                        if delta_content:
                            content_parts.append(delta_content)
                        if delta_reasoning:
                            reasoning_parts.append(delta_reasoning)
                        if delta_tool_calls:
                            append_tool_call_delta(tool_call_accumulator, delta_tool_calls)

                        if "usage" in openai_chunk and openai_chunk["usage"]:
                            usage = openai_chunk["usage"]
                            input_tokens = extract_prompt_tokens(usage)
                            # 计费/监控按真实总输出（正文+思考），不受下游展示口径影响
                            output_tokens = total_output_tokens(usage)
                            reasoning_tokens = extract_reasoning_tokens(usage)
                            total_tokens = resolve_usage_tokens(usage).total_tokens
                            upstream_usage = converter.usage

                        yield f"data: {json.dumps(openai_chunk, ensure_ascii=False)}\n\n"

                    # 继续处理剩余事件流
                    async for event in gemini_generator:
                        openai_chunks = converter.feed(event)
                        if openai_chunks and "error" in openai_chunks[0]:
                            error_msg = str(openai_chunks[0].get("error"))
                            yield f"data: {json.dumps({'error': openai_chunks[0]['error']}, ensure_ascii=False)}\n\n"
                            break

                        for openai_chunk in openai_chunks:
                            choices = openai_chunk.get("choices") or []
                            delta = choices[0].get("delta", {}) if choices else {}
                            delta_content = delta.get("content", "")
                            delta_reasoning = delta.get("reasoning_content", "")
                            delta_tool_calls = delta.get("tool_calls")

                            if delta_content:
                                content_parts.append(delta_content)
                            if delta_reasoning:
                                reasoning_parts.append(delta_reasoning)
                            if delta_tool_calls:
                                append_tool_call_delta(tool_call_accumulator, delta_tool_calls)

                            if "usage" in openai_chunk and openai_chunk["usage"]:
                                usage = openai_chunk["usage"]
                                input_tokens = extract_prompt_tokens(usage)
                                # 计费/监控按真实总输出（正文+思考），不受下游展示口径影响
                                output_tokens = total_output_tokens(usage)
                                reasoning_tokens = extract_reasoning_tokens(usage)
                                total_tokens = resolve_usage_tokens(usage).total_tokens
                                upstream_usage = converter.usage

                            yield f"data: {json.dumps(openai_chunk, ensure_ascii=False)}\n\n"

                    stream_completed = (error_msg is None)
                    request_success = (error_msg is None)

                except GeneratorExit:
                    client_gone = True
                    if stream_completed or (content_parts and not error_msg):
                        request_success = True
                        logger.info(f"[GEMINI_INTERACTIONS] 生成器被关闭，但流已完成或有有效输出，标记为成功")
                    else:
                        logger.warning(f"[GEMINI_INTERACTIONS] 生成器被提前关闭")
                    raise
                except asyncio.CancelledError:
                    client_gone = True
                    if stream_completed or (content_parts and not error_msg):
                        request_success = True
                        logger.info(f"[GEMINI_INTERACTIONS] 请求被取消，但流已完成或有有效输出，标记为成功")
                    else:
                        logger.warning(f"[GEMINI_INTERACTIONS] 请求被取消，客户端断开")
                    raise
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"[GEMINI_INTERACTIONS] 流式处理失败: {e}", exc_info=True)
                finally:
                    if input_tokens == 0 or output_tokens == 0:
                        try:
                            if input_tokens == 0:
                                input_tokens = await asyncio.shield(estimate_message_tokens_non_blocking(
                                    estimate_message_tokens_func,
                                    openai_req.get('messages', []), display_name))
                            if output_tokens == 0:
                                accumulated_content = ''.join(content_parts)
                                accumulated_reasoning = ''.join(reasoning_parts)
                                total_output_text = accumulated_reasoning + accumulated_content
                                if total_output_text:
                                    output_tokens = await asyncio.shield(estimate_text_tokens_non_blocking(
                                        estimate_tokens_func, total_output_text, display_name))
                        except asyncio.CancelledError:
                            logger.warning(f"[GEMINI_INTERACTIONS] Token 估算被取消，使用上游返回的 usage 值")
                        except Exception as token_error:
                            logger.error(f"[GEMINI_INTERACTIONS] Token计算失败: {token_error}")

                    cost_info = direct_api_service.calculate_cost(
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        cached_tokens=0,
                        pricing=pricing_config) if pricing_config else {}
                    final_content = ''.join(content_parts)
                    final_reasoning = ''.join(reasoning_parts)
                    final_tool_calls = finalize_tool_calls(tool_call_accumulator)

                    monitoring_service.request_end(
                        request_id=request_id, success=request_success,
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        cached_tokens=0, error=error_msg,
                        response_content=final_content,
                        reasoning_content=final_reasoning,
                        cost_info=cost_info, full_messages=full_messages,
                        response_message=build_response_message(final_content, final_reasoning, final_tool_calls),
                        response_tool_calls=final_tool_calls,
                        upstream_usage=upstream_usage)

                    await monitoring_service.broadcast_to_monitors({
                        "type": "request_end", "request_id": request_id,
                        "success": request_success})

                    logger.info(f"[GEMINI_INTERACTIONS] 流式请求完成: {request_id[:8]}")
                    logger.info(f"  - 输入tokens: {input_tokens}, 输出tokens: {output_tokens}")
                    if cost_info.get("total_cost"):
                        logger.info(f"  - 总成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")

                    if not client_gone and not tail_suppressed:
                        try:
                            final_total_tokens = total_tokens if total_tokens > 0 else (input_tokens + output_tokens)
                            usage_final_chunk = {
                                "id": f"chatcmpl-{request_id}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": display_name,
                                "choices": [],
                                "usage": compose_chat_usage(
                                    prompt_tokens=input_tokens,
                                    output_tokens=output_tokens,
                                    reasoning_tokens=reasoning_tokens,
                                    completion_mode=completion_tokens_mode,
                                    total_tokens=final_total_tokens,
                                )
                            }
                            yield f"data: {json.dumps(usage_final_chunk, ensure_ascii=False)}\n\n"
                            yield "data: [DONE]\n\n"
                        except Exception as yield_err:
                            logger.debug(f"[GEMINI_INTERACTIONS] 发送 usage/[DONE] 时客户端已断开: {yield_err}")

            return StreamingResponse(
                interactions_stream_generator() if upstream_protocol == "interactions" else gemini_stream_generator(),
                media_type="text/event-stream",
                headers={
                    'Cache-Control': 'no-cache',
                    'Connection': 'keep-alive',
                    'X-Accel-Buffering': 'no',
                    'Transfer-Encoding': 'chunked'
                }
            )
        else:
            # 非流式响应
            gemini_response = await anext(gemini_generator)

            if "error" in gemini_response:
                error_msg = str(gemini_response.get("error"))

                partial_input_tokens = 0
                try:
                    partial_input_tokens = estimate_message_tokens_func(
                        openai_req.get('messages', []), model=display_name)
                except Exception as token_err:
                    logger.warning(f"[GEMINI_NATIVE] 错误时输入token计算失败: {token_err}")

                cost_info = direct_api_service.calculate_cost(
                    input_tokens=partial_input_tokens, output_tokens=0,
                    pricing=pricing_config) if pricing_config else {}

                if cost_info.get("total_cost"):
                    logger.info(f"[GEMINI_NATIVE] 失败请求成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")

                monitoring_service.request_end(
                    request_id=request_id, success=False, error=error_msg,
                    input_tokens=partial_input_tokens, output_tokens=0,
                    cost_info=cost_info, full_messages=full_messages)
                await monitoring_service.broadcast_to_monitors({
                    "type": "request_end", "request_id": request_id, "success": False})

                mapped_status_code = map_upstream_error_to_status_code(gemini_response, default_status_code=500)
                return JSONResponse(status_code=mapped_status_code, content=gemini_response)

            if upstream_protocol == "interactions":
                openai_response = convert_interactions_to_openai_response(
                    gemini_response, display_name, request_id, completion_tokens_mode)
            else:
                openai_response = direct_api_service.convert_gemini_response_to_openai(
                    gemini_response, display_name, request_id, is_stream_chunk=False,
                    completion_tokens_mode=completion_tokens_mode)

            response_content = ""
            reasoning_content = ""
            response_tool_calls = None
            if "choices" in openai_response and len(openai_response["choices"]) > 0:
                message = openai_response["choices"][0].get("message", {})
                response_content = message.get("content", "")
                reasoning_content = message.get("reasoning_content", "")
                response_tool_calls = message.get("tool_calls")

            if upstream_protocol == "interactions":
                # interactions 的 usage 在 Interaction 顶层（total_input_tokens 等）
                _meta = gemini_response.get("usage")
            else:
                _meta = gemini_response.get("usageMetadata")
            upstream_usage = _meta if isinstance(_meta, dict) and _meta else None

            usage = openai_response.get("usage", {})
            input_tokens = extract_prompt_tokens(usage)
            # 计费按真实总输出（正文+思考），与下游看到的 completion_tokens 口径无关
            output_tokens = total_output_tokens(usage)

            if input_tokens == 0 or output_tokens == 0:
                try:
                    if input_tokens == 0:
                        input_tokens = await estimate_message_tokens_non_blocking(
                            estimate_message_tokens_func,
                            openai_req.get('messages', []), display_name)
                    if output_tokens == 0 and response_content:
                        output_tokens = await estimate_text_tokens_non_blocking(
                            estimate_tokens_func, response_content, display_name)
                except Exception as token_error:
                    logger.error(f"[GEMINI_NATIVE] Token计算失败: {token_error}")

            cost_info = direct_api_service.calculate_cost(
                input_tokens=input_tokens, output_tokens=output_tokens,
                cached_tokens=0,
                pricing=pricing_config) if pricing_config else {}

            monitoring_service.request_end(
                request_id=request_id, success=True,
                input_tokens=input_tokens, output_tokens=output_tokens,
                response_content=response_content,
                reasoning_content=reasoning_content,
                cost_info=cost_info, full_messages=full_messages,
                response_message=build_response_message(response_content, reasoning_content, response_tool_calls),
                response_tool_calls=response_tool_calls,
                upstream_usage=upstream_usage)

            await monitoring_service.broadcast_to_monitors({
                "type": "request_end", "request_id": request_id, "success": True})

            logger.info(f"[GEMINI_NATIVE] 非流式请求完成: {request_id[:8]}")
            logger.info(f"  - 输入tokens: {input_tokens}, 输出tokens: {output_tokens}")
            if cost_info.get("total_cost"):
                logger.info(f"  - 总成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")

            return JSONResponse(content=openai_response)

    except HTTPException as http_exc:
        logger.error(f"[GEMINI_NATIVE] 请求处理失败(HTTPException): {http_exc}", exc_info=True)

        partial_input_tokens = 0
        try:
            partial_input_tokens = estimate_message_tokens_func(
                openai_req.get('messages', []), model=display_name)
        except Exception as token_err:
            logger.warning(f"[GEMINI_NATIVE] 异常时输入token计算失败: {token_err}")

        cost_info = direct_api_service.calculate_cost(
            input_tokens=partial_input_tokens, output_tokens=0,
            pricing=pricing_config) if pricing_config else {}

        if cost_info.get("total_cost"):
            logger.info(f"[GEMINI_NATIVE] 异常请求成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")

        monitoring_service.request_end(
            request_id=request_id, success=False,
            error=str(http_exc.detail) if http_exc.detail is not None else str(http_exc),
            input_tokens=partial_input_tokens, output_tokens=0,
            cost_info=cost_info, full_messages=full_messages)
        await monitoring_service.broadcast_to_monitors({
            "type": "request_end", "request_id": request_id, "success": False})
        raise

    except Exception as e:
        logger.error(f"[GEMINI_NATIVE] 请求处理失败: {e}", exc_info=True)

        partial_input_tokens = 0
        try:
            partial_input_tokens = estimate_message_tokens_func(
                openai_req.get('messages', []), model=display_name)
        except Exception as token_err:
            logger.warning(f"[GEMINI_NATIVE] 异常时输入token计算失败: {token_err}")

        cost_info = direct_api_service.calculate_cost(
            input_tokens=partial_input_tokens, output_tokens=0,
            pricing=pricing_config) if pricing_config else {}

        if cost_info.get("total_cost"):
            logger.info(f"[GEMINI_NATIVE] 异常请求成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")

        monitoring_service.request_end(
            request_id=request_id, success=False, error=str(e),
            input_tokens=partial_input_tokens, output_tokens=0,
            cost_info=cost_info, full_messages=full_messages)
        await monitoring_service.broadcast_to_monitors({
            "type": "request_end", "request_id": request_id, "success": False})
        raise HTTPException(status_code=500, detail=f"Gemini Native API调用失败: {str(e)}")
