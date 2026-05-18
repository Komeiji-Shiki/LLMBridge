"""
Direct API - 透传模式处理
支持 OpenAI/Anthropic 兼容 API 的流式与非流式透传
"""
import asyncio
import codecs
import json
import logging
import time
import uuid

from fastapi import HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, Response

from core.constants import TimeoutDefaults
from utils.monitor_params import build_monitor_request_params
from ._direct_api_utils import (
    append_tool_call_delta,
    build_response_message,
    extract_complete_sse_lines,
    estimate_message_tokens_non_blocking,
    estimate_text_tokens_non_blocking,
    extract_tool_calls_from_message,
    finalize_tool_calls,
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
    api_base_url: str,
    api_key: str,
    endpoint_config: dict,
    pricing_config: dict,
    thinking_separator: str,
    monitoring_service,
    direct_api_service,
    estimate_message_tokens_func,
    estimate_tokens_func,
    full_messages: list = None,
    CONFIG: dict = None
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
    if endpoint_config.get("enable_thinking", True):
        monitor_extra_params["thinkingConfig"] = {
            "thinkingBudget": endpoint_config.get("thinking_budget", 20000),
            "includeThoughts": True
        }

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

    # Gemini Thinking 模式
    if endpoint_config.get("enable_thinking", True):
        thinking_budget = endpoint_config.get("thinking_budget", 20000)
        passthrough_request["thinkingConfig"] = {
            "thinkingBudget": thinking_budget,
            "includeThoughts": True
        }
        logger.info(f"[DIRECT_API_THINKING] 已启用思维链模式 (thinkingBudget={thinking_budget})")

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
    """处理流式透传请求"""

    api_iterator = direct_api_service.call_api_passthrough(
        base_url=api_base_url, api_key=api_key,
        request_body=passthrough_request, endpoint_path=endpoint_path)

    first_chunk_timeout = TimeoutDefaults.FIRST_CHUNK_TIMEOUT
    if CONFIG:
        first_chunk_timeout = CONFIG.get("first_chunk_timeout_seconds", TimeoutDefaults.FIRST_CHUNK_TIMEOUT)

    async def combined_stream_generator():
        request_success = False
        stream_completed = False
        error_msg = None
        upstream_error_detected = False
        upstream_done_received = False
        content_parts = []
        reasoning_parts = []
        tool_call_accumulator = {}
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        reasoning_tokens = 0
        separator_found = False
        repetition_detected = False
        cached_tokens = 0
        request_end_called = False
        client_disconnected = False

        def _mark_client_disconnect_sync(reason: str):
            nonlocal request_end_called, request_success, error_msg, client_disconnected
            if request_end_called:
                return

            client_disconnected = True
            request_success = False
            error_msg = reason

            partial_content = ''.join(content_parts)
            partial_reasoning = ''.join(reasoning_parts)
            local_input_tokens = input_tokens or 0
            partial_tool_calls = finalize_tool_calls(tool_call_accumulator)
            local_output_tokens = output_tokens or (
                len(partial_reasoning + partial_content) // 4
                if (partial_reasoning or partial_content or partial_tool_calls) else 0)

            monitoring_service.request_end(
                request_id=request_id, success=False,
                input_tokens=local_input_tokens, output_tokens=local_output_tokens,
                cached_tokens=cached_tokens, error=error_msg,
                response_content=partial_content, reasoning_content=partial_reasoning,
                full_messages=full_messages,
                response_message=build_response_message(partial_content, partial_reasoning, partial_tool_calls),
                response_tool_calls=partial_tool_calls)
            request_end_called = True

        async def _handle_client_disconnect():
            if request_end_called:
                return
            _mark_client_disconnect_sync("Client disconnected")
            await monitoring_service.broadcast_to_monitors({
                "type": "request_end", "request_id": request_id, "success": False})

        try:
            # === 预读第一个块（发送心跳） ===
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
                    error_msg = f"上游API返回空响应或在{first_chunk_timeout}秒内未返回第一个数据块"
                    logger.error(f"[DIRECT_API_PASSTHROUGH] {error_msg}")
                    await _record_failed_request(
                        monitoring_service, request_id, display_name, error_msg,
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
                error_msg = f"上游API返回空响应或在{first_chunk_timeout}秒内未返回第一个数据块"
                logger.error(f"[DIRECT_API_PASSTHROUGH] {error_msg}")
                await _record_failed_request(
                    monitoring_service, request_id, display_name, error_msg,
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
                yield f"data: {json.dumps(error_json, ensure_ascii=False)}\n\n".encode('utf-8')
                yield "data: [DONE]\n\n".encode('utf-8')
                return

            # === SSE 流处理 ===
            accumulated_for_split = ""
            output_position = 0
            sep_len = len(thinking_separator) if thinking_separator else 0
            split_done = False
            utf8_decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            decode_buffer = ""
            pending_line = ""

            def process_sse_chunk(chunk_bytes):
                nonlocal separator_found, accumulated_for_split, output_position, split_done
                nonlocal input_tokens, output_tokens, total_tokens, reasoning_tokens
                nonlocal error_msg, upstream_error_detected
                nonlocal decode_buffer
                nonlocal content_parts, reasoning_parts, tool_call_accumulator
                nonlocal cached_tokens
                nonlocal upstream_done_received
                nonlocal pending_line

                try:
                    chunk_str = utf8_decoder.decode(chunk_bytes, final=False)
                except Exception as e:
                    logger.warning(f"[UTF8_DECODE] 解码失败: {e}")
                    chunk_str = chunk_bytes.decode('utf-8', errors='replace')

                if not chunk_str:
                    return b''

                lines, pending_line, buffered_incomplete_line = extract_complete_sse_lines(
                    chunk_str, pending_line)
                result_lines = []
                modified = False
                lines_skipped = False

                for line in lines:
                    line_stripped = line.rstrip('\r')
                    if line_stripped.startswith('data:'):
                        data_content = line_stripped[5:].lstrip()
                        if data_content == '':
                            result_lines.append(line_stripped)
                            continue
                        if data_content == '[DONE]':
                            upstream_done_received = True
                            lines_skipped = True
                            logger.debug(f"[SSE_FILTER] 过滤掉上游 [DONE]")
                            continue
                        line_modified = False
                        try:
                            chunk_json = json.loads(data_content)
                        except json.JSONDecodeError as json_err:
                            fixed = False

                            if hasattr(json_err, 'pos') and json_err.pos > 1 and json_err.pos < len(data_content):
                                try:
                                    truncated_json = json.loads(data_content[:json_err.pos])
                                    if isinstance(truncated_json, dict) and (
                                            'choices' in truncated_json or 'error' in truncated_json or 'usage' in truncated_json):
                                        chunk_json = truncated_json
                                        fixed = True
                                        logger.warning(f"[JSON_TRUNCATE_FIX] 截取前 {json_err.pos}/{len(data_content)} 字符修复成功")
                                except json.JSONDecodeError:
                                    pass

                            if not fixed:
                                concat_patterns = ['}{', '}\n{', '}\r\n{']
                                for pattern in concat_patterns:
                                    if pattern in data_content:
                                        split_pos = data_content.find(pattern) + 1
                                        first_json_str = data_content[:split_pos]
                                        second_json_str = data_content[split_pos:].lstrip('\n\r')
                                        for json_str in [second_json_str, first_json_str]:
                                            try:
                                                chunk_json = json.loads(json_str)
                                                if 'choices' in chunk_json or 'error' in chunk_json:
                                                    logger.warning(f"[JSON_CONCAT_FIX] 检测到拼接JSON，已修复。模式: '{pattern}'")
                                                    fixed = True
                                                    break
                                            except json.JSONDecodeError:
                                                continue
                                        if fixed:
                                            break

                            if not fixed:
                                logger.debug(f"[JSON_PARSE_FAIL] 位置: {json_err.pos if hasattr(json_err, 'pos') else 'N/A'}, 数据前100字符: {data_content[:100]}")
                                result_lines.append(line_stripped)
                                continue

                            modified = True
                            line_modified = True

                        try:
                            if isinstance(chunk_json, dict) and 'error' in chunk_json and chunk_json['error'] is not None:
                                upstream_error_detected = True
                                error_msg = str(chunk_json.get('error'))
                                logger.error(f"[DIRECT_API_PASSTHROUGH] 流式上游返回错误事件: {chunk_json}")

                            if 'choices' in chunk_json and len(chunk_json['choices']) > 0:
                                finish_reason = chunk_json['choices'][0].get('finish_reason')

                                delta = chunk_json['choices'][0].get('delta', {})
                                raw_content = delta.get('content', '')
                                raw_reasoning = delta.get('reasoning_content', '') or delta.get('reasoning', '')
                                raw_tool_calls = delta.get('tool_calls')

                                message = chunk_json['choices'][0].get('message', {}) if isinstance(chunk_json['choices'][0], dict) else {}
                                if isinstance(message, dict):
                                    raw_content = raw_content or message.get('content', '')
                                    raw_reasoning = raw_reasoning or message.get('reasoning_content', '') or message.get('reasoning', '')
                                    raw_tool_calls = raw_tool_calls or extract_tool_calls_from_message(message)

                                if raw_tool_calls:
                                    append_tool_call_delta(tool_call_accumulator, raw_tool_calls)

                                # thinking_separator 处理
                                if thinking_separator and not split_done and raw_content:
                                    accumulated_for_split += raw_content

                                    if thinking_separator in accumulated_for_split:
                                        separator_found = True
                                        split_done = True

                                        parts = accumulated_for_split.split(thinking_separator, 1)
                                        full_reasoning = parts[0]
                                        content_part = parts[1] if len(parts) > 1 else ""

                                        remaining_reasoning = full_reasoning[output_position:]
                                        raw_reasoning = remaining_reasoning
                                        raw_content = content_part

                                        new_delta = {}
                                        if remaining_reasoning:
                                            new_delta['reasoning_content'] = remaining_reasoning
                                        if content_part:
                                            new_delta['content'] = content_part

                                        if new_delta:
                                            chunk_json['choices'][0]['delta'] = new_delta
                                            line_modified = True
                                            logger.info(f"[THINKING_SPLIT_STREAM] 检测到分隔符'{thinking_separator}'")
                                            logger.info(f"  - 思考总长: {len(full_reasoning)} 字符")
                                            logger.info(f"  - 本次输出reasoning: {len(remaining_reasoning)} 字符")
                                            logger.info(f"  - 正文部分: {len(content_part)} 字符")
                                        else:
                                            lines_skipped = True
                                            continue
                                    else:
                                        sep_len = len(thinking_separator)
                                        safe_position = max(output_position, len(accumulated_for_split) - sep_len)
                                        safe_content = accumulated_for_split[output_position:safe_position]

                                        if safe_content:
                                            raw_reasoning = safe_content
                                            raw_content = ""
                                            delta['reasoning_content'] = safe_content
                                            delta.pop('content', None)
                                            output_position = safe_position
                                            line_modified = True
                                        else:
                                            lines_skipped = True
                                            continue

                                if raw_content:
                                    content_parts.append(raw_content)
                                if raw_reasoning:
                                    reasoning_parts.append(raw_reasoning)

                            if 'usage' in chunk_json and chunk_json['usage'] is not None:
                                usage = chunk_json['usage']
                                base_prompt = usage.get('prompt_tokens', 0) or usage.get('input_tokens', 0)
                                base_output = usage.get('completion_tokens', 0) or usage.get('output_tokens', 0)

                                prompt_details = usage.get('prompt_tokens_details', {})
                                cached = prompt_details.get('cached_tokens', 0) if isinstance(prompt_details, dict) else 0
                                if cached > 0:
                                    cached_tokens = cached

                                cached_mode = endpoint_config.get('cached_tokens_mode', 'reverse')
                                if cached > 0 and cached_mode == 'forward':
                                    usage['prompt_tokens'] = base_prompt + cached
                                    line_modified = True
                                    logger.info(f"[CACHED_TOKENS] 正向模式修正: prompt {base_prompt} + cached {cached} = {usage['prompt_tokens']}")

                                if cached_mode == 'forward':
                                    if base_prompt > 0:
                                        input_tokens = base_prompt + cached
                                else:
                                    if base_prompt > 0:
                                        input_tokens = base_prompt
                                if base_output > 0:
                                    output_tokens = base_output
                                total_tokens = usage.get('total_tokens', 0)
                                reasoning_tokens = usage.get('reasoning_tokens', 0)

                            if 'reasoning' in delta and 'reasoning_content' not in delta and raw_reasoning:
                                delta['reasoning_content'] = delta.pop('reasoning')
                                chunk_json['choices'][0]['delta'] = delta
                                line_modified = True
                                logger.debug(f"[REASONING_FIELD_CONVERT] 将 reasoning 转换为 reasoning_content")

                            if line_modified:
                                modified = True
                                result_lines.append(f'data: {json.dumps(chunk_json, ensure_ascii=False)}')
                                continue

                        except Exception as process_err:
                            logger.debug(f"[PROCESS_SSE] 处理行时出错: {process_err}")
                            if line_modified:
                                result_lines.append(f'data: {json.dumps(chunk_json, ensure_ascii=False)}')
                                continue

                    result_lines.append(line)

                if modified or lines_skipped or buffered_incomplete_line:
                    return '\n'.join(result_lines).encode('utf-8')
                else:
                    return chunk_bytes

            # 处理第一个块
            processed_first = process_sse_chunk(first_chunk_bytes)

            if upstream_error_detected:
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
                        await _handle_client_disconnect()
                        try:
                            await api_iterator.aclose()
                        except Exception:
                            pass
                        return
                    continue

                processed_chunk = process_sse_chunk(chunk_bytes)

                if upstream_error_detected:
                    try:
                        yield processed_chunk
                    except asyncio.CancelledError:
                        logger.warning(f"[DIRECT_API_PASSTHROUGH] 客户端在错误块输出时断开: {request_id[:8]}")
                        await _handle_client_disconnect()
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
                    await _handle_client_disconnect()
                    try:
                        await api_iterator.aclose()
                    except Exception:
                        pass
                    return

            stream_completed = not upstream_error_detected
            request_success = (not upstream_error_detected) and (error_msg is None)

        except asyncio.CancelledError:
            logger.warning(f"[DIRECT_API_PASSTHROUGH] 流式任务被取消: {request_id[:8]}")
            await _handle_client_disconnect()
        except GeneratorExit:
            logger.warning(f"[DIRECT_API_PASSTHROUGH] 生成器被提前关闭: {request_id[:8]}")
            _mark_client_disconnect_sync("Client disconnected (GeneratorExit)")
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[DIRECT_API_PASSTHROUGH] 流式处理中发生异常: {e}", exc_info=True)
        finally:
            if request_end_called:
                try:
                    await api_iterator.aclose()
                except Exception:
                    pass
                return

            try:
                await api_iterator.aclose()
            except Exception:
                pass

            # 应用思考内容分隔符
            accumulated_content = ''.join(content_parts)
            accumulated_reasoning = ''.join(reasoning_parts)
            final_reasoning = accumulated_reasoning
            final_content = accumulated_content

            if thinking_separator and accumulated_content and not accumulated_reasoning:
                reasoning_part, main_part = direct_api_service.split_thinking_content(
                    accumulated_content, thinking_separator)
                if reasoning_part:
                    final_reasoning = reasoning_part
                    final_content = main_part
                    logger.info(f"[THINKING_SPLIT] 检测到思考内容分隔符，分离出 {len(reasoning_part)} 字符的思考内容")

            # Token 计算
            if input_tokens == 0:
                logger.warning(f"[DIRECT_API_PASSTHROUGH] API未返回input_tokens，使用tokenizer计算")
                try:
                    input_tokens = await estimate_message_tokens_non_blocking(
                        estimate_message_tokens_func,
                        openai_req.get('messages', []), display_name)
                except Exception as token_error:
                    logger.error(f"[DIRECT_API_PASSTHROUGH] Input token计算失败: {token_error}")
                    input_tokens = sum(len(str(m.get('content', ''))) for m in openai_req.get('messages', [])) // 4

            total_output_text = (final_reasoning or "") + (final_content or "")
            content_char_count = len(total_output_text) if total_output_text else 0
            min_expected_tokens = content_char_count // 3 if content_char_count > 0 else 0

            should_recalculate = (
                output_tokens <= 1
                or repetition_detected
                or (content_char_count > 100 and output_tokens < min_expected_tokens * 0.5)
            )

            if should_recalculate and total_output_text:
                try:
                    calculated_output_tokens = await estimate_text_tokens_non_blocking(
                        estimate_tokens_func, total_output_text, display_name)
                    if calculated_output_tokens >= 10:
                        reason = "回放检测" if repetition_detected else (
                            "上游值偏小" if output_tokens > 1 else "上游未返回")
                        logger.info(f"[DIRECT_API_PASSTHROUGH] Token修正({reason}): 上游={output_tokens}, 计算={calculated_output_tokens}, 内容={content_char_count}字符")
                        output_tokens = calculated_output_tokens
                except Exception as token_error:
                    logger.error(f"[DIRECT_API_PASSTHROUGH] Output token计算失败: {token_error}")
                    fallback_tokens = len(total_output_text) // 4
                    if fallback_tokens >= 10:
                        output_tokens = fallback_tokens

            total_tokens = input_tokens + output_tokens

            cost_info = direct_api_service.calculate_cost(
                input_tokens=input_tokens, output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                pricing=pricing_config) if pricing_config else {}
            final_tool_calls = finalize_tool_calls(tool_call_accumulator)

            monitoring_service.request_end(
                request_id=request_id, success=request_success,
                input_tokens=input_tokens, output_tokens=output_tokens,
                cached_tokens=cached_tokens, error=error_msg,
                response_content=final_content,
                reasoning_content=final_reasoning,
                cost_info=cost_info, full_messages=full_messages,
                response_message=build_response_message(final_content, final_reasoning, final_tool_calls),
                response_tool_calls=final_tool_calls)
            await monitoring_service.broadcast_to_monitors({
                "type": "request_end", "request_id": request_id,
                "success": request_success})

            logger.info(f"[DIRECT_API_PASSTHROUGH] 流式请求完成: {request_id[:8]}, 成功: {request_success}")
            if final_reasoning:
                logger.info(f"  - 思考内容: {len(final_reasoning)} 字符")
            if input_tokens > 0 or output_tokens > 0:
                usage_parts = [f"输入={input_tokens}"]
                if cached_tokens > 0:
                    usage_parts.append(f"(缓存={cached_tokens})")
                usage_parts.append(f"输出={output_tokens}")
                if reasoning_tokens > 0:
                    usage_parts.append(f"思考={reasoning_tokens}")
                usage_parts.append(f"总计={total_tokens}")
                logger.info(f"[DIRECT_API_PASSTHROUGH] Token统计: {', '.join(usage_parts)}")
            if cost_info.get("total_cost"):
                logger.info(f"[DIRECT_API_PASSTHROUGH] 总成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")

            # 发送最后的 usage 和 [DONE]
            try:
                if input_tokens > 0 or output_tokens > 0:
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
                    yield f"data: {json.dumps(usage_final_chunk, ensure_ascii=False)}\n\n".encode('utf-8')
                    logger.debug(f"[SSE_USAGE] 已发送 usage chunk: input={input_tokens}, output={output_tokens}, total={final_total_tokens}")

                yield "data: [DONE]\n\n".encode('utf-8')
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

        if not response_json.get("usage") or (input_tokens == 0 and output_tokens == 0):
            logger.warning(f"[DIRECT_API] API未返回usage或tokens为0，使用tokenizer计算")
            try:
                input_tokens = await estimate_message_tokens_non_blocking(
                    estimate_message_tokens_func,
                    openai_req.get('messages', []), display_name)
                content = _extract_response_content(response_json)
                output_tokens = await estimate_text_tokens_non_blocking(
                    estimate_tokens_func, content or "", display_name)
                logger.info(f"[DIRECT_API] Tokenizer计算结果: 输入={input_tokens}, 输出={output_tokens}")
            except Exception as token_error:
                logger.warning(f"[DIRECT_API] Tokenizer计算失败: {token_error}，使用简单估算")
                input_tokens = sum(len(str(m.get('content', ''))) for m in openai_req.get('messages', [])) // 4
                output_tokens = len(_extract_response_content(response_json) or "") // 4

        # 提取内容并应用思考分隔
        content, reasoning_content, response_tool_calls, response_message, response_json = _extract_and_split_content(
            response_json, thinking_separator, direct_api_service)

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
