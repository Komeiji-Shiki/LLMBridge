"""
Direct API - Gemini 原生 API 处理
"""
import asyncio
import json
import logging
import time
import uuid

from fastapi import HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

from core.constants import TimeoutDefaults
from ._direct_api_utils import (
    estimate_message_tokens_non_blocking,
    estimate_text_tokens_non_blocking,
    map_upstream_error_to_status_code,
)

logger = logging.getLogger(__name__)


async def handle_gemini_native_direct(
    openai_req: dict,
    model_name: str,
    target_model_id: str,
    display_name: str,
    api_key: str,
    api_base_url: str,
    endpoint_config: dict,
    pricing_config: dict,
    monitoring_service,
    direct_api_service,
    estimate_message_tokens_func,
    estimate_tokens_func,
    full_messages: list = None,
    CONFIG: dict = None
):
    """处理Gemini原生API的Direct请求"""
    logger.info(f"[GEMINI_NATIVE] 使用Gemini原生API格式")

    request_id = str(uuid.uuid4())

    monitoring_service.request_start(
        request_id=request_id,
        model=display_name,
        messages_count=len(openai_req.get("messages", [])),
        session_id=None,
        mode="gemini_native",
        messages=openai_req.get("messages", []),
        params={
            "temperature": openai_req.get("temperature"),
            "top_p": openai_req.get("top_p"),
            "max_tokens": openai_req.get("max_tokens"),
            "streaming": openai_req.get("stream", False)
        }
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

        extra_kwargs = {}
        custom_params = endpoint_config.get("custom_params", {})
        if custom_params and isinstance(custom_params, dict):
            extra_kwargs.update(custom_params)
            logger.info(f"[GEMINI_NATIVE] 已添加自定义参数:")
            for key, value in custom_params.items():
                logger.info(f"  - {key}: {value}")

        thinking_config = None
        enable_thinking = endpoint_config.get("enable_thinking", True)
        if enable_thinking:
            thinking_budget = endpoint_config.get("thinking_budget", 20000)
            thinking_config = {
                "thinkingBudget": thinking_budget,
                "includeThoughts": True
            }
            logger.info(f"[GEMINI_NATIVE] 已启用思维链模式 (budget={thinking_budget})")

        gemini_generator = direct_api_service.call_gemini_native_api(
            api_key=api_key,
            model=target_model_id,
            messages=openai_req.get("messages", []),
            stream=is_stream,
            temperature=openai_req.get("temperature"),
            top_p=openai_req.get("top_p"),
            max_tokens=openai_req.get("max_tokens"),
            base_url=api_base_url,
            thinking_config=thinking_config,
            tools=openai_req.get("tools"),
            tool_choice=openai_req.get("tool_choice"),
            **extra_kwargs
        )

        first_chunk_timeout = TimeoutDefaults.FIRST_CHUNK_TIMEOUT
        if CONFIG:
            first_chunk_timeout = CONFIG.get("first_chunk_timeout_seconds", TimeoutDefaults.FIRST_CHUNK_TIMEOUT)

        if is_stream:
            async def gemini_stream_generator():
                content_parts = []
                reasoning_parts = []
                input_tokens = 0
                output_tokens = 0
                total_tokens = 0
                request_success = False
                stream_completed = False
                error_msg = None

                try:
                    api_task = asyncio.create_task(anext(gemini_generator))
                    heartbeat_interval = min(endpoint_config.get("client_disconnect_probe_interval", 30), 30)
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
                        return

                    # 处理预读的第一个块
                    for gemini_chunk in [first_gemini_chunk]:
                        openai_chunk = direct_api_service.convert_gemini_response_to_openai(
                            gemini_chunk, display_name, request_id, is_stream_chunk=True)

                        delta = openai_chunk.get("choices", [{}])[0].get("delta", {})
                        delta_content = delta.get("content", "")
                        delta_reasoning = delta.get("reasoning_content", "")

                        if delta_content:
                            content_parts.append(delta_content)
                        if delta_reasoning:
                            reasoning_parts.append(delta_reasoning)

                        if "usage" in openai_chunk and openai_chunk["usage"]:
                            usage = openai_chunk["usage"]
                            input_tokens = usage.get("prompt_tokens", 0)
                            output_tokens = usage.get("completion_tokens", 0)
                            total_tokens = usage.get("total_tokens", 0)

                        yield f"data: {json.dumps(openai_chunk, ensure_ascii=False)}\n\n"

                    # 继续处理剩余流
                    async for gemini_chunk in gemini_generator:
                        if "error" in gemini_chunk:
                            error_msg = str(gemini_chunk.get("error"))
                            openai_error = {"error": gemini_chunk["error"]}
                            yield f"data: {json.dumps(openai_error, ensure_ascii=False)}\n\n"
                            break

                        openai_chunk = direct_api_service.convert_gemini_response_to_openai(
                            gemini_chunk, display_name, request_id, is_stream_chunk=True)

                        delta = openai_chunk.get("choices", [{}])[0].get("delta", {})
                        delta_content = delta.get("content", "")
                        delta_reasoning = delta.get("reasoning_content", "")

                        if delta_content:
                            content_parts.append(delta_content)
                        if delta_reasoning:
                            reasoning_parts.append(delta_reasoning)

                        if "usage" in openai_chunk and openai_chunk["usage"]:
                            usage = openai_chunk["usage"]
                            input_tokens = usage.get("prompt_tokens", 0)
                            output_tokens = usage.get("completion_tokens", 0)
                            total_tokens = usage.get("total_tokens", 0)

                        yield f"data: {json.dumps(openai_chunk, ensure_ascii=False)}\n\n"

                    stream_completed = (error_msg is None)
                    request_success = (error_msg is None)

                except GeneratorExit:
                    if stream_completed or (content_parts and not error_msg):
                        request_success = True
                        logger.info(f"[GEMINI_NATIVE] 生成器被关闭，但流已完成或有有效输出，标记为成功")
                    else:
                        logger.warning(f"[GEMINI_NATIVE] 生成器被提前关闭")
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"[GEMINI_NATIVE] 流式处理失败: {e}", exc_info=True)
                finally:
                    if input_tokens == 0 or output_tokens == 0:
                        try:
                            if input_tokens == 0:
                                input_tokens = await estimate_message_tokens_non_blocking(
                                    estimate_message_tokens_func,
                                    openai_req.get('messages', []), "gemma")
                            if output_tokens == 0:
                                accumulated_content = ''.join(content_parts)
                                accumulated_reasoning = ''.join(reasoning_parts)
                                total_output_text = accumulated_reasoning + accumulated_content
                                if total_output_text:
                                    output_tokens = await estimate_text_tokens_non_blocking(
                                        estimate_tokens_func, total_output_text, "gemma")
                        except Exception as token_error:
                            logger.error(f"[GEMINI_NATIVE] Token计算失败: {token_error}")

                    cost_info = direct_api_service.calculate_cost(
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        cached_tokens=0,
                        pricing=pricing_config) if pricing_config else {}

                    monitoring_service.request_end(
                        request_id=request_id, success=request_success,
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        cached_tokens=0, error=error_msg,
                        response_content=''.join(content_parts),
                        reasoning_content=''.join(reasoning_parts),
                        cost_info=cost_info, full_messages=full_messages)

                    await monitoring_service.broadcast_to_monitors({
                        "type": "request_end", "request_id": request_id,
                        "success": request_success})

                    logger.info(f"[GEMINI_NATIVE] 流式请求完成: {request_id[:8]}")
                    logger.info(f"  - 输入tokens: {input_tokens}, 输出tokens: {output_tokens}")
                    if cost_info.get("total_cost"):
                        logger.info(f"  - 总成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")

                    try:
                        final_total_tokens = total_tokens if total_tokens > 0 else (input_tokens + output_tokens)
                        usage_final_chunk = {
                            "id": f"chatcmpl-{request_id}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": display_name,
                            "choices": [],
                            "usage": {
                                "prompt_tokens": input_tokens,
                                "completion_tokens": output_tokens,
                                "total_tokens": final_total_tokens
                            }
                        }
                        yield f"data: {json.dumps(usage_final_chunk, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                    except (GeneratorExit, Exception) as yield_err:
                        logger.debug(f"[GEMINI_NATIVE] 发送 usage/[DONE] 时客户端已断开: {yield_err}")

            return StreamingResponse(
                gemini_stream_generator(),
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

            openai_response = direct_api_service.convert_gemini_response_to_openai(
                gemini_response, display_name, request_id, is_stream_chunk=False)

            response_content = ""
            reasoning_content = ""
            if "choices" in openai_response and len(openai_response["choices"]) > 0:
                message = openai_response["choices"][0].get("message", {})
                response_content = message.get("content", "")
                reasoning_content = message.get("reasoning_content", "")

            usage = openai_response.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)

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
                cost_info=cost_info, full_messages=full_messages)

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
