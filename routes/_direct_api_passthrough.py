"""
Direct API - 透传模式处理
支持 OpenAI/Anthropic 兼容 API 的流式与非流式透传
"""
import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, Response

from core.constants import TimeoutDefaults
from utils.monitor_params import build_monitor_request_params
from ._direct_api_stream_session import PassthroughStreamSession, SSE_DATA_PREFIX
from ._direct_api_utils import (
    build_response_message,
    estimate_message_tokens_non_blocking,
    estimate_text_tokens_non_blocking,
    extract_tool_calls_from_message,
    is_error_json,
    map_upstream_error_to_status_code,
    normalize_to_openai_error,
)

logger = logging.getLogger(__name__)


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

    monitor_extra_params = {"upstream_model": target_model_id, "endpoint_path": endpoint_path}
    custom_params = endpoint_config.get("custom_params", {})
    if isinstance(custom_params, dict):
        monitor_extra_params.update(custom_params)
    enable_thinking = endpoint_config.get("enable_thinking")
    if enable_thinking is not None:
        monitor_extra_params["enable_thinking"] = enable_thinking

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
        passthrough_request = _prepare_passthrough_request(
            openai_req, model_name, target_model_id, endpoint_config)

        is_stream = openai_req.get("stream", False)
        logger.info(f"[DIRECT_API_PASSTHROUGH] 使用上游端点: {endpoint_path}")

        if is_stream:
            return await _handle_passthrough_stream(
                passthrough_request, openai_req, request_id, model_name,
                display_name, api_base_url, api_key, endpoint_config,
                pricing_config, thinking_separator, endpoint_path,
                monitoring_service, direct_api_service,
                estimate_message_tokens_func, estimate_tokens_func,
                full_messages, CONFIG
            )
        else:
            return await _handle_passthrough_non_stream(
                passthrough_request, openai_req, request_id, display_name,
                api_base_url, api_key, endpoint_config, pricing_config,
                thinking_separator, endpoint_path,
                monitoring_service, direct_api_service,
                estimate_message_tokens_func, estimate_tokens_func,
                full_messages
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

    # 合并自定义参数
    custom_params = endpoint_config.get("custom_params", {})
    if custom_params and isinstance(custom_params, dict):
        passthrough_request.update(custom_params)
        logger.info(f"[DIRECT_API_CUSTOM] 已添加自定义参数:")
        for key, value in custom_params.items():
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

        # OpenRouter：assistant 消息回传时，reasoning_content → reasoning
        # OpenRouter 文档要求用 message.reasoning 保留上一轮思考链，否则模型无法延续推理
        # 注意：openai_req.copy() 是浅拷贝，messages 内的 dict 是同一引用。
        # 不能直接修改原 dict（会污染监控记录的 full_messages），只能对需要改的消息创建副本。
        if "messages" in passthrough_request:
            new_messages = []
            for msg in passthrough_request["messages"]:
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    rc = msg.get("reasoning_content")
                    if rc and "reasoning" not in msg:
                        # 需要重命名：创建消息副本，带上 reasoning 字段，不带 reasoning_content
                        msg_copy = {k: v for k, v in msg.items() if k != "reasoning_content"}
                        msg_copy["reasoning"] = rc
                        new_messages.append(msg_copy)
                    else:
                        new_messages.append(msg)
                else:
                    new_messages.append(msg)
            passthrough_request["messages"] = new_messages
            # DEBUG: 临时调试日志，确认 reasoning 字段是否被正确转换
            for i, m in enumerate(new_messages):
                if isinstance(m, dict) and m.get("role") == "assistant":
                    rc_val = m.get("reasoning") or ""
                    logger.info(
                        f"[DEBUG_OR_CONVERT] msg[{i}] assistant keys={list(m.keys())} "
                        f"reasoning_len={len(rc_val) if isinstance(rc_val, str) else 'N/A'} "
                        f"reasoning[:50]={repr(rc_val[:50]) if isinstance(rc_val, str) else rc_val}")
    else:
        # 非 OpenRouter：将 enable_thinking 作为顶层字段注入请求体
        # 仅处理布尔值，字符串值（adaptive/strip）由 Anthropic 原生模式处理
        enable_thinking = endpoint_config.get("enable_thinking")
        if enable_thinking is True or enable_thinking is False:
            passthrough_request["enable_thinking"] = enable_thinking
            logger.info(f"[DIRECT_API_THINKING] enable_thinking={enable_thinking}（已注入请求体顶层）")
        # 思考强度等级（OpenAI 风格 reasoning_effort：minimal/low/medium/high 等）
        # 大部分 OAI 兼容上游识别顶层 reasoning_effort 字段
        if enable_thinking is True:
            reasoning_effort = endpoint_config.get("reasoning_effort")
            if reasoning_effort:
                passthrough_request["reasoning_effort"] = reasoning_effort
                logger.info(f"[DIRECT_API_THINKING] reasoning_effort={reasoning_effort}（已注入请求体顶层）")

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
    full_messages, CONFIG
):
    """处理流式透传请求。

    SSE 解析、thinking 分离、token 统计、监控上报全部委托给
    PassthroughStreamSession（routes/_direct_api_stream_session.py）；
    本函数只负责心跳、上游迭代与异常边界。
    """

    api_iterator = direct_api_service.call_api_passthrough(
        base_url=api_base_url, api_key=api_key,
        request_body=passthrough_request, endpoint_path=endpoint_path)

    first_chunk_timeout = TimeoutDefaults.FIRST_CHUNK_TIMEOUT
    if CONFIG:
        first_chunk_timeout = CONFIG.get("first_chunk_timeout_seconds", TimeoutDefaults.FIRST_CHUNK_TIMEOUT)

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
    )

    async def combined_stream_generator():
        try:
            # === 预读第一个块（期间向客户端发送心跳） ===
            api_task = asyncio.create_task(anext(api_iterator))
            heartbeat_interval = min(endpoint_config.get("client_disconnect_probe_interval", 30), 30)
            first_chunk_wait_start = time.time()

            while not api_task.done():
                yield f": keep-alive {int(time.time())}\n\n".encode("utf-8")
                if time.time() - first_chunk_wait_start > first_chunk_timeout:
                    api_task.cancel()
                    try:
                        await api_task
                    except asyncio.CancelledError:
                        pass
                    session.error_msg = (
                        f"上游API返回空响应或在{first_chunk_timeout}秒内未返回第一个数据块")
                    logger.error(f"[DIRECT_API_PASSTHROUGH] {session.error_msg}")
                    await _record_failed_request(
                        monitoring_service, request_id, display_name, session.error_msg,
                        openai_req, pricing_config, direct_api_service,
                        estimate_message_tokens_func, full_messages)
                    return
                try:
                    await asyncio.wait_for(asyncio.shield(api_task), timeout=heartbeat_interval)
                except asyncio.TimeoutError:
                    continue

            try:
                first_chunk_bytes = await api_task
            except StopAsyncIteration:
                session.error_msg = (
                    f"上游API返回空响应或在{first_chunk_timeout}秒内未返回第一个数据块")
                logger.error(f"[DIRECT_API_PASSTHROUGH] {session.error_msg}")
                await _record_failed_request(
                    monitoring_service, request_id, display_name, session.error_msg,
                    openai_req, pricing_config, direct_api_service,
                    estimate_message_tokens_func, full_messages)
                return

            # === 检查第一个块是否为 JSON 错误 ===
            is_err, error_json = _detect_first_chunk_error(first_chunk_bytes)
            if is_err:
                await _handle_first_chunk_error(
                    error_json, monitoring_service, request_id, display_name,
                    openai_req, pricing_config, direct_api_service,
                    estimate_message_tokens_func, full_messages)
                yield (SSE_DATA_PREFIX + json.dumps(error_json, ensure_ascii=False) + "\n\n").encode('utf-8')
                yield "data: [DONE]\n\n".encode('utf-8')
                return

            # === SSE 流处理（解析与统计委托给 session） ===
            processed_first = session.process_sse_chunk(first_chunk_bytes)

            if session.upstream_error_detected:
                yield processed_first
                return

            yield processed_first

            # 继续处理剩余流
            heartbeat_interval = endpoint_config.get("client_disconnect_probe_interval", 180)
            while True:
                try:
                    chunk_bytes = await asyncio.wait_for(anext(api_iterator), timeout=heartbeat_interval)
                except StopAsyncIteration:
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
            await session.handle_client_disconnect()
        except GeneratorExit:
            logger.warning(f"[DIRECT_API_PASSTHROUGH] 生成器被提前关闭: {request_id[:8]}")
            session.mark_client_disconnect("Client disconnected (GeneratorExit)")
        except Exception as e:
            session.error_msg = str(e)
            logger.error(f"[DIRECT_API_PASSTHROUGH] 流式处理中发生异常: {e}", exc_info=True)
        finally:
            try:
                await api_iterator.aclose()
            except Exception:
                pass

            # 收尾统计与监控上报（断连路径已上报时返回空列表，不再补发）
            tail_chunks = await session.finalize()
            try:
                for tail_chunk in tail_chunks:
                    yield tail_chunk
            except (GeneratorExit, Exception) as yield_err:
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
    full_messages
):
    """处理非流式透传请求"""
    try:
        response_bytes = b""
        async for chunk in direct_api_service.call_api_passthrough(
            base_url=api_base_url, api_key=api_key,
            request_body=passthrough_request, endpoint_path=endpoint_path
        ):
            response_bytes += chunk

        response_text = response_bytes.decode('utf-8')
        try:
            response_json = json.loads(response_text)
        except json.JSONDecodeError as e:
            if hasattr(e, 'pos') and e.pos > 1 and e.pos < len(response_text):
                try:
                    response_json = json.loads(response_text[:e.pos])
                    logger.warning(f"[DIRECT_API_PASSTHROUGH] 非流式响应包含额外数据（pos={e.pos}/{len(response_text)}），已截取修复")
                    response_bytes = json.dumps(response_json, ensure_ascii=False).encode('utf-8')
                except json.JSONDecodeError:
                    raise e
            else:
                raise

        if is_error_json(response_json):
            normalized_error = normalize_to_openai_error(response_json)
            error_details = normalized_error.get('error', {})
            status_code = map_upstream_error_to_status_code(normalized_error, default_status_code=500)
            error_message = str(error_details)
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
                cost_info=cost_info, full_messages=full_messages)
            await monitoring_service.broadcast_to_monitors({
                "type": "request_end", "request_id": request_id, "success": False})
            return JSONResponse(status_code=status_code, content=normalized_error)

        # 提取 token 统计
        input_tokens, output_tokens, reasoning_tokens, total_tokens, cached_tokens = \
            _extract_tokens_from_response(response_json, endpoint_config)

        # 提取内容并应用思考分隔
        content, reasoning_content, response_tool_calls, response_message, response_json = _extract_and_split_content(
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

        # 🔧 关键修复：_extract_and_split_content 可能修改了 response_json
        # （如添加 reasoning_content、字段转换等），必须重新编码以确保客户端收到修改后的数据
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
            response_tool_calls=response_tool_calls)

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

def _detect_first_chunk_error(first_chunk_bytes) -> tuple:
    """检查第一个块是否为JSON错误"""
    is_error = False
    error_json = None
    try:
        decoded_chunk = first_chunk_bytes.decode('utf-8')
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


async def _record_failed_request(monitoring_service, request_id, display_name,
                                  error_msg, openai_req, pricing_config,
                                  direct_api_service, estimate_message_tokens_func,
                                  full_messages):
    """记录失败请求的token和成本"""
    partial_input_tokens = 0
    try:
        partial_input_tokens = estimate_message_tokens_func(
            openai_req.get('messages', []), model=display_name)
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
    error_message = str(error_details) if error_details else str(error_json)
    logger.error(f"[DIRECT_API_PASSTHROUGH] 请求失败，上游返回错误: {error_json}")
    await _record_failed_request(
        monitoring_service, request_id, display_name, error_message,
        openai_req, pricing_config, direct_api_service,
        estimate_message_tokens_func, full_messages)


def _extract_tokens_from_response(response_json, endpoint_config):
    """从响应JSON中提取token统计"""
    usage = response_json.get("usage", {})
    input_tokens = 0
    output_tokens = 0
    reasoning_tokens = 0
    total_tokens = 0
    cached_tokens = 0

    if usage:
        base_prompt_tokens = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
        base_output_tokens = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)

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

        output_tokens = base_output_tokens
        reasoning_tokens = usage.get("reasoning_tokens", 0)
        total_tokens = usage.get("total_tokens", 0)

        if reasoning_tokens > 0:
            logger.info(f"[DIRECT_API] 检测到思考token: {reasoning_tokens}")
        logger.info(f"[DIRECT_API] 使用API返回的token统计: 输入={input_tokens}, 输出={output_tokens}")

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
