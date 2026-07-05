"""
流处理服务模块
处理来自浏览器的原始数据流，并格式化为OpenAI兼容的响应

重构说明：
- 使用 stream_formatters 模块进行响应格式化
- 使用 stream_parsers 模块进行数据解析
- 使用 image_handler 模块进行图片处理
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Optional, Dict, Any

# 导入拆分出的模块
from services.stream_formatters import (
    StreamChunkBuilder,
    generate_response_id,
    format_openai_non_stream_response,
)
from services.stream_parsers import (
    StreamPatternMatcher,
    StreamBuffer,
    extract_partial_content,
    is_control_marker,
)
from services.image_handler import (
    ImageProcessor,
    CloudflareHandler,
)
from utils.task_registry import spawn

logger = logging.getLogger(__name__)

# 创建全局的模式匹配器实例（避免重复编译正则）
_pattern_matcher = StreamPatternMatcher()


async def _process_lmarena_stream(request_id: str, queue, request_metadata: dict, CONFIG: dict,
                                   browser_connections: dict, response_channels: dict,
                                   IMAGE_BASE64_CACHE: dict, IMAGE_CACHE_MAX_SIZE: int,
                                   IMAGE_CACHE_TTL: int):
    """
    核心内部生成器：处理来自浏览器的原始数据流，并产生结构化事件。
    事件类型: ('content', str), ('finish', str), ('error', str), ('retry_info', dict)

    人机验证状态统一读写 AppState.server（通过 CloudflareHandler），
    图片下载/保存与标签页释放函数直接从对应模块导入。
    """
    from services.image_service import (
        save_downloaded_image_async,
        download_image_data_with_retry,
    )
    from core.load_balancer import release_tab
    from core.app_state import get_app_state

    _state = get_app_state()

    stream_cancelled = False
    logger.info(f"[STREAM_LIFECYCLE] 🚀 _process_lmarena_stream 开始处理: {request_id[:8]}")
    
    if not queue:
        logger.error(f"PROCESSOR [ID: {request_id[:8]}]: 无法找到响应通道。")
        yield 'error', 'Internal server error: response channel not found.'
        return

    timeout = CONFIG.get("stream_response_timeout_seconds", 360)
    has_yielded_content = False
    enable_reasoning_output = CONFIG.get("enable_lmarena_reasoning", False)
    reasoning_buffer = []
    has_reasoning = False
    reasoning_ended = False
    
    # 使用 StreamBuffer 管理缓冲区
    stream_buffer = StreamBuffer()
    stream_start_time = time.time()
    
    # 初始化图片处理器
    # 🔧 包装下载/保存函数：ImageProcessor 只传核心参数，
    # 其余依赖（session/信号量/去重集合/配置）从 AppState 获取
    async def _download_image_for_processor(url: str):
        return await download_image_data_with_retry(
            url,
            _state.server.aiohttp_session,
            _state.server.DOWNLOAD_SEMAPHORE,
            _state.server.MAX_CONCURRENT_DOWNLOADS,
            CONFIG,
        )

    async def _save_image_for_processor(image_data: bytes, url: str, rid: str):
        await save_downloaded_image_async(
            image_data,
            url,
            rid,
            _state.image.downloaded_urls_set,
            CONFIG,
        )

    image_processor = ImageProcessor(
        config=CONFIG,
        image_cache=IMAGE_BASE64_CACHE,
        cache_max_size=IMAGE_CACHE_MAX_SIZE,
        cache_ttl=IMAGE_CACHE_TTL,
        download_func=_download_image_for_processor,
        save_func=_save_image_for_processor,
    )
    
    # 初始化 Cloudflare 处理器（验证状态自动读写 AppState.server）
    cloudflare_handler = CloudflareHandler(browser_connections=browser_connections)

    try:
        while True:
            # 检查请求通道是否已关闭
            if request_id not in response_channels:
                logger.warning(f"[STREAM_LIFECYCLE] ⚠️ 请求通道已关闭: {request_id[:8]}")
                stream_cancelled = True
                await _send_cancel_to_browser(request_id, request_metadata, browser_connections)
                break
            
            try:
                raw_data = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"PROCESSOR [ID: {request_id[:8]}]: 等待浏览器数据超时（{timeout}秒）。")
                yield 'error', f'Response timed out after {timeout} seconds.'
                return

            # 处理 WebSocket 端的直接消息
            if isinstance(raw_data, dict):
                if 'retry_info' in raw_data:
                    yield 'retry_info', raw_data.get('retry_info', {})
                    continue
                if 'error' in raw_data:
                    error_result = await _handle_dict_error(raw_data, request_id, cloudflare_handler)
                    if error_result:
                        yield error_result
                        return

            # 检查 [DONE] 信号
            if raw_data == "[DONE]":
                logger.info(f"[STREAM_END] 收到[DONE]信号 - 请求 {request_id[:8]}")
                
                # 🔧 修复：循环等待并收集所有延迟数据（不只是一个）
                delayed_chunks_count = 0
                while True:
                    try:
                        extra_data = await asyncio.wait_for(queue.get(), timeout=0.3)  # 增加到 0.3 秒
                        if extra_data == "[DONE]":
                            # 再次收到 [DONE]，继续等待可能的延迟数据
                            continue
                        stream_buffer.append(extra_data)
                        delayed_chunks_count += 1
                        logger.debug(f"[STREAM_END] 收到延迟数据块 #{delayed_chunks_count}，长度: {len(str(extra_data))}")
                    except asyncio.TimeoutError:
                        # 超时，没有更多数据
                        break
                
                if delayed_chunks_count > 0:
                    logger.info(f"[STREAM_END] 共收集了 {delayed_chunks_count} 个延迟数据块")
                
                # 处理剩余内容
                async for event in _flush_remaining_buffer(stream_buffer, enable_reasoning_output, reasoning_buffer, CONFIG):
                    has_yielded_content = True
                    yield event
                
                if has_yielded_content and cloudflare_handler.is_refreshing:
                    logger.info(f"PROCESSOR [ID: {request_id[:8]}]: 请求成功，人机验证状态将在下次连接时重置。")
                break

            # 累加缓冲区
            stream_buffer.append(raw_data)
            
            # 检查 Cloudflare
            if _pattern_matcher.check_cloudflare(stream_buffer.get_remaining()):
                error_msg = await cloudflare_handler.handle_verification(request_id)
                yield 'error', error_msg
                return
            
            # 检查错误
            error = _pattern_matcher.check_error(stream_buffer.get_remaining())
            if error:
                yield 'error', error
                return

            # 解析缓冲区内容
            parsed = stream_buffer.parse()
            
            # 处理思维链
            for reasoning_content in parsed.reasoning_chunks:
                if reasoning_ended:
                    logger.warning(f"[REASONING_WARN] 检测到reasoning在content之后继续出现！")
                has_reasoning = True
                reasoning_buffer.append(reasoning_content)
                if enable_reasoning_output and CONFIG.get("preserve_streaming", True):
                    yield 'reasoning', reasoning_content
            
            # 处理文本内容
            for text_content in parsed.content_chunks:
                if has_reasoning and not reasoning_ended and not parsed.reasoning_chunks:
                    reasoning_ended = True
                    logger.info(f"[REASONING_END] 检测到reasoning结束（共{len(reasoning_buffer)}个片段）")
                    if enable_reasoning_output:
                        yield 'reasoning_end', None
                
                has_yielded_content = True
                yield 'content', text_content
                await asyncio.sleep(0)
            
            # 处理图片
            for image_url in parsed.image_urls:
                image_result, should_continue = await image_processor.process_image_url(image_url, request_id)
                yield 'content', image_result
                if should_continue:
                    continue
            
            # 处理结束信息
            if parsed.finish_reason:
                yield 'finish', {'reason': parsed.finish_reason, 'usage': parsed.usage_info}

    except asyncio.CancelledError:
        stream_cancelled = True
        logger.warning(f"[STREAM_LIFECYCLE] 🚫 任务被取消: {request_id[:8]}")
        await _send_cancel_to_browser(request_id, request_metadata, browser_connections)
    finally:
        await _cleanup_stream(
            request_id, stream_cancelled, stream_buffer, stream_start_time,
            enable_reasoning_output, has_reasoning, reasoning_buffer, CONFIG,
            response_channels, request_metadata, release_tab,
            monitoring_service=None  # stream_generator 层负责 request_end
        )


async def _send_cancel_to_browser(request_id: str, request_metadata: dict, browser_connections: dict):
    """向浏览器发送取消指令"""
    if request_id in request_metadata:
        tab_id = request_metadata[request_id].get("tab_id")
        if tab_id and tab_id in browser_connections:
            ws = browser_connections[tab_id]
            try:
                spawn(ws.send_text(json.dumps({
                    "command": "cancel_request",
                    "request_id": request_id
                })), name="cancel-request")
                logger.warning(f"[STREAM_LIFECYCLE] ✉️ 已向浏览器发送取消指令: {request_id[:8]}")
            except Exception as e:
                logger.error(f"[STREAM_LIFECYCLE] 发送取消指令失败: {e}")


async def _handle_dict_error(raw_data: dict, request_id: str, cloudflare_handler: CloudflareHandler):
    """处理字典类型的错误"""
    error_msg = raw_data.get('error', 'Unknown browser error')
    if isinstance(error_msg, str):
        if '413' in error_msg or 'too large' in error_msg.lower():
            return 'error', "上传失败：附件大小超过了 LMArena 服务器的限制 (通常是 5MB左右)。请尝试压缩文件或上传更小的文件。"
        if any(p in error_msg for p in CloudflareHandler.PATTERNS):
            return 'error', await cloudflare_handler.handle_verification(request_id)
    return 'error', error_msg


async def _flush_remaining_buffer(stream_buffer: StreamBuffer, enable_reasoning_output: bool, 
                                   reasoning_buffer: list, CONFIG: dict):
    """刷新缓冲区中的剩余内容"""
    remaining = stream_buffer.get_remaining()
    if len(remaining) == 0:
        return
    
    logger.info(f"[STREAM_END] ⚠️ Buffer还有 {len(remaining)} 字符未处理")
    
    # 尝试提取剩余内容
    parsed = stream_buffer.parse()
    
    for text in parsed.content_chunks:
        yield 'content', text
        await asyncio.sleep(0)
    
    for reasoning in parsed.reasoning_chunks:
        if enable_reasoning_output:
            reasoning_buffer.append(reasoning)
            if CONFIG.get("preserve_streaming", True):
                yield 'reasoning', reasoning
    
    # 检查是否有控制标记
    final_remaining = stream_buffer.get_remaining()
    if final_remaining:
        if is_control_marker(final_remaining):
            logger.info(f"[STREAM_END] 检测到控制标记，忽略不输出")
        elif final_remaining.strip() and not final_remaining.startswith('[') and not final_remaining.startswith('{'):
            clean_text = ''.join(c for c in final_remaining if c.isprintable() or c in '\n\r\t')
            if clean_text.strip():
                logger.info(f"[STREAM_END] ⚡ 从异常buffer中提取到文本: {clean_text[:200]}...")
                yield 'content', clean_text


async def _cleanup_stream(request_id: str, stream_cancelled: bool, stream_buffer: StreamBuffer,
                          stream_start_time: float, enable_reasoning_output: bool, has_reasoning: bool,
                          reasoning_buffer: list, CONFIG: dict, response_channels: dict,
                          request_metadata: dict, release_tab_request, monitoring_service=None):
    """清理流处理资源，并确保孤儿请求被正确标记为失败"""
    if stream_cancelled:
        logger.warning(f"[STREAM_LIFECYCLE] ⛔ 流处理异常结束: {request_id[:8]}")
    else:
        logger.info(f"[STREAM_LIFECYCLE] ✅ 流处理正常结束: {request_id[:8]}")
    
    # 输出统计
    stats = stream_buffer.get_stats()
    if stats["chunk_count"] > 0 and CONFIG.get("debug_stream_timing", False):
        total_time = time.time() - stream_start_time
        logger.info(f"[STREAM_STATS] 请求ID: {request_id[:8]}")
        logger.info(f"  - 总块数: {stats['chunk_count']}")
        logger.info(f"  - 总字符数: {stats['total_chars']}")
        logger.info(f"  - 平均yield间隔: {total_time/stats['chunk_count']:.3f}秒")
    
    # 释放标签页
    if request_id in request_metadata:
        tab_id = request_metadata[request_id].get("tab_id")
        if tab_id:
            await release_tab_request(tab_id)
    
    # 延迟清理
    if not stream_cancelled:
        await asyncio.sleep(0.05)
    
    # 🔧 根因2修复：如果 monitoring_service 不为 None，检查请求是否还在 active_requests 中
    # 如果是，说明没有任何其他路径调用了 request_end，需要在这里兜底
    if monitoring_service is not None:
        if hasattr(monitoring_service, 'active_requests') and request_id in monitoring_service.active_requests:
            elapsed = time.time() - stream_start_time
            error_msg = f"Stream cleanup: request orphaned after {elapsed:.1f}s (cancelled={stream_cancelled})"
            logger.warning(f"[STREAM_LIFECYCLE] 🔧 兜底清理孤儿请求: {request_id[:8]} - {error_msg}")
            monitoring_service.request_end(
                request_id, success=False, error=error_msg
            )
    
    # 清理通道
    # 🔧 使用 pop() 避免 TOCTOU 竞态（KeyError）
    response_channels.pop(request_id, None)
    request_metadata.pop(request_id, None)


async def stream_generator(request_id: str, model: str, _process_lmarena_stream_func,
                           format_openai_chunk_func, format_openai_finish_chunk_func, format_openai_error_chunk_func,
                           CONFIG: dict, response_channels: dict, request_metadata: dict,
                           monitoring_service, estimate_message_tokens, estimate_tokens,
                           browser_connections: dict, full_messages: Optional[list] = None):
    """将内部事件流格式化为 OpenAI SSE 响应。"""
    response_id = generate_response_id()
    chunk_builder = StreamChunkBuilder(model, response_id)
    logger.info(f"STREAMER [ID: {request_id[:8]}]: 流式生成器启动。")
    
    is_cancelled = False
    client_disconnected = False
    finish_reason_to_send = 'stop'
    collected_content = []
    reasoning_content = []
    lmarena_usage = None
    request_end_called = False  # 🔧 根因5修复：跟踪 request_end 是否已调用
    
    stream_start_time = time.time()
    chunks_sent = 0
    
    enable_reasoning_output = CONFIG.get("enable_lmarena_reasoning", False)
    reasoning_mode = CONFIG.get("reasoning_output_mode", "openai")
    preserve_streaming = CONFIG.get("preserve_streaming", True)

    try:
        async for event_type, data in _process_lmarena_stream_func(request_id):
            # 检测客户端断开（只捕获 CancelledError，不捕获 GeneratorExit）
            # ⚠️ Python async generator 规定：捕获 GeneratorExit 后不能做 async 操作
            # 否则会抛出 RuntimeError('async generator ignored GeneratorExit')
            try:
                yield ""
            except asyncio.CancelledError as e:
                client_disconnected = True
                # CancelledError 可以安全地做 async 操作
                await _handle_client_disconnect(
                    request_id, e, collected_content, reasoning_content, model,
                    monitoring_service, estimate_message_tokens, estimate_tokens,
                    request_metadata, browser_connections
                )
                is_cancelled = True
                request_end_called = True
                break
            
            # 处理各种事件类型
            if event_type == 'retry_info':
                if CONFIG.get("show_retry_info_to_client", False):
                    yield chunk_builder.content(f"\n[重试信息] 尝试 {data.get('attempt')}/{data.get('max_attempts')}\n")
            
            elif event_type == 'reasoning':
                reasoning_content.append(data)
                if enable_reasoning_output and reasoning_mode == "openai" and preserve_streaming:
                    yield chunk_builder.reasoning(data)
            
            elif event_type == 'reasoning_end':
                if enable_reasoning_output and reasoning_mode == "think_tag" and reasoning_content:
                    full_reasoning = "".join(reasoning_content)
                    yield chunk_builder.content(f"<think>{full_reasoning}</think>\n\n")
            
            elif event_type == 'reasoning_complete':
                reasoning_content.append(data)
                if enable_reasoning_output and not preserve_streaming:
                    if reasoning_mode == "openai":
                        yield chunk_builder.reasoning(data)
                    elif reasoning_mode == "think_tag":
                        yield chunk_builder.content(f"<think>{data}</think>\n\n")
            
            elif event_type == 'content':
                collected_content.append(data)
                chunks_sent += 1
                yield chunk_builder.content(data)
                await asyncio.sleep(0)
            
            elif event_type == 'finish':
                if isinstance(data, dict):
                    finish_reason_to_send = data.get('reason', 'stop')
                    lmarena_usage = data.get('usage')
                else:
                    finish_reason_to_send = data
                
                if finish_reason_to_send == 'content-filter':
                    warning = "\n\n响应被终止，可能是上下文超限或者模型内部审查（大概率）的原因"
                    collected_content.append(warning)
                    yield chunk_builder.content(warning)
            
            elif event_type == 'error':
                await _handle_stream_error(
                    request_id, data, collected_content, reasoning_content, model,
                    monitoring_service, estimate_message_tokens, estimate_tokens, chunk_builder
                )
                request_end_called = True
                yield chunk_builder.error(str(data))
                yield chunk_builder.finish(reason='stop')
                return

        # 发送结束块
        await asyncio.sleep(0.1)
        
        full_response = "".join(collected_content)
        full_reasoning = "".join(reasoning_content) if reasoning_content else None
        
        # 计算token
        input_tokens, output_tokens = await _calculate_tokens(
            lmarena_usage, model, full_response, request_id,
            monitoring_service, estimate_message_tokens, estimate_tokens
        )
        
        final_usage = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        
        # 发送 OpenAI 规范要求的最后一个包含 usage 的数据包
        usage_final_chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [],
            "usage": final_usage
        }
        yield f"data: {json.dumps(usage_final_chunk, ensure_ascii=False)}\n\n"
        
        yield chunk_builder.finish(reason=finish_reason_to_send)
        
        # 记录请求成功
        monitoring_service.request_end(
            request_id, success=True, response_content=full_response,
            reasoning_content=full_reasoning, input_tokens=input_tokens, output_tokens=output_tokens,
            full_messages=full_messages
        )
        request_end_called = True
        await monitoring_service.broadcast_to_monitors({"type": "request_end", "request_id": request_id, "success": True})
    
    except asyncio.CancelledError:
        # CancelledError: 可以安全地做同步清理
        if not request_end_called:
            logger.warning(f"[STREAM_LIFECYCLE] 🔧 stream_generator CancelledError: {request_id[:8]}")
            partial_response = "".join(collected_content) if collected_content else None
            partial_reasoning = "".join(reasoning_content) if reasoning_content else None
            monitoring_service.request_end(
                request_id, success=False,
                error="Stream generator cancelled",
                response_content=partial_response,
                reasoning_content=partial_reasoning
            )
            request_end_called = True
    except Exception as e:
        # 捕获任何意外异常（不包括 GeneratorExit）
        if not request_end_called:
            logger.error(f"[STREAM_LIFECYCLE] 🔧 stream_generator 意外异常: {request_id[:8]} - {e}", exc_info=True)
            monitoring_service.request_end(
                request_id, success=False,
                error=f"Stream generator unexpected error: {e}"
            )
            request_end_called = True
    finally:
        # 🔧 最终兜底（纯同步操作，对 GeneratorExit 安全）
        # GeneratorExit 不会被上面的 except 捕获，会直接到这里
        if not request_end_called:
            elapsed = time.time() - stream_start_time
            logger.warning(f"[STREAM_LIFECYCLE] 🔧 stream_generator finally 兜底: {request_id[:8]} (elapsed={elapsed:.1f}s)")
            try:
                monitoring_service.request_end(
                    request_id, success=False,
                    error=f"Stream generator exited (GeneratorExit/unknown, elapsed={elapsed:.1f}s)"
                )
            except Exception as cleanup_err:
                logger.error(f"[STREAM_LIFECYCLE] 兜底 request_end 失败: {cleanup_err}")


async def _handle_client_disconnect(request_id, exception, collected_content, reasoning_content, model,
                                     monitoring_service, estimate_message_tokens, estimate_tokens,
                                     request_metadata, browser_connections):
    """处理客户端断开连接"""
    logger.warning(f"[DISCONNECT_DETECT] 🚫 客户端已断开: {request_id[:8]}")
    
    partial_response = "".join(collected_content)
    partial_reasoning = "".join(reasoning_content) if reasoning_content else None
    
    input_tokens, output_tokens = 0, 0
    if hasattr(monitoring_service, 'active_requests') and request_id in monitoring_service.active_requests:
        request_info = monitoring_service.active_requests[request_id]
        if request_info.request_messages:
            try:
                input_tokens = estimate_message_tokens(request_info.request_messages, model)
            except:
                pass
    
    if partial_response:
        try:
            output_tokens = estimate_tokens(partial_response, model)
        except:
            output_tokens = len(partial_response) // 4
    
    monitoring_service.request_end(
        request_id, success=False, error=f"Client disconnected: {type(exception).__name__}",
        response_content=partial_response, reasoning_content=partial_reasoning,
        input_tokens=input_tokens, output_tokens=output_tokens
    )
    
    # 发送取消指令
    await _send_cancel_to_browser(request_id, request_metadata, browser_connections)


async def _handle_stream_error(request_id, error_data, collected_content, reasoning_content, model,
                                monitoring_service, estimate_message_tokens, estimate_tokens, chunk_builder):
    """处理流式错误"""
    logger.error(f"STREAMER [ID: {request_id[:8]}]: 流中发生错误: {error_data}")
    
    error_response = "".join(collected_content) if collected_content else None
    error_reasoning = "".join(reasoning_content) if reasoning_content else None
    
    input_tokens, output_tokens = 0, 0
    if hasattr(monitoring_service, 'active_requests') and request_id in monitoring_service.active_requests:
        request_info = monitoring_service.active_requests[request_id]
        if request_info.request_messages:
            try:
                input_tokens = estimate_message_tokens(request_info.request_messages, model)
            except:
                pass
    
    if error_response:
        try:
            output_tokens = estimate_tokens(error_response, model)
        except:
            output_tokens = len(error_response) // 4
    
    monitoring_service.request_end(
        request_id, success=False, error=str(error_data),
        response_content=error_response, reasoning_content=error_reasoning,
        input_tokens=input_tokens, output_tokens=output_tokens
    )
    await monitoring_service.broadcast_to_monitors({"type": "request_end", "request_id": request_id, "success": False})


async def _calculate_tokens(lmarena_usage, model, full_response, request_id,
                             monitoring_service, estimate_message_tokens, estimate_tokens):
    """
    计算token数量（异步版本，不阻塞事件循环）
    
    使用 asyncio.to_thread 将同步的token计算放到线程池执行，
    避免阻塞其他请求的处理。
    """
    from modules.token_counter import (
        get_tokenizer_for_model,
        estimate_tokens_async,
        estimate_message_tokens_async
    )
    
    if lmarena_usage:
        input_tokens = lmarena_usage.get('inputTokens', 0) or lmarena_usage.get('prompt_tokens', 0)
        output_tokens = lmarena_usage.get('outputTokens', 0) or lmarena_usage.get('completion_tokens', 0)
        logger.info(f"[TOKEN] 使用LMArena实际token数: input={input_tokens}, output={output_tokens}")
        return input_tokens, output_tokens
    
    tokenizer_type = get_tokenizer_for_model(model)
    logger.info(f"[TOKEN] 使用{tokenizer_type}计数（模型: {model}）- 异步执行")
    
    input_tokens = 0
    request_messages = None
    
    if hasattr(monitoring_service, 'active_requests') and request_id in monitoring_service.active_requests:
        request_info = monitoring_service.active_requests[request_id]
        if request_info.request_messages:
            request_messages = request_info.request_messages
    
    async def _zero():
        return 0
    
    try:
        # 🔧 关键修复：并行异步计算输入和输出tokens，不阻塞事件循环
        if request_messages:
            input_task = estimate_message_tokens_async(request_messages, model)
        else:
            input_task = _zero()
        
        output_task = estimate_tokens_async(full_response, model)
        
        # 并行等待两个计算完成
        results = await asyncio.gather(input_task, output_task, return_exceptions=True)
        
        # 处理结果
        if isinstance(results[0], Exception):
            logger.warning(f"[TOKEN] 输入token计算失败: {results[0]}")
            input_tokens = sum(
                len(msg.get('content', '')) // 4
                for msg in (request_messages or [])
                if isinstance(msg, dict) and isinstance(msg.get('content'), str)
            )
        else:
            input_tokens = results[0]
        
        if isinstance(results[1], Exception):
            logger.warning(f"[TOKEN] 输出token计算失败: {results[1]}")
            output_tokens = len(full_response) // 4
        else:
            output_tokens = results[1]
            
    except Exception as e:
        logger.warning(f"[TOKEN] token计算异常，使用估算: {e}")
        input_tokens = sum(
            len(msg.get('content', '')) // 4
            for msg in (request_messages or [])
            if isinstance(msg, dict) and isinstance(msg.get('content'), str)
        )
        output_tokens = len(full_response) // 4
    
    return input_tokens, output_tokens


async def non_stream_response(request_id: str, model: str, _process_lmarena_stream_func,
                              format_openai_non_stream_response_func, CONFIG: dict,
                              response_channels: dict, request_metadata: dict,
                              monitoring_service, estimate_message_tokens, estimate_tokens,
                              release_tab_request, Response, full_messages: Optional[list] = None):
    """聚合内部事件流并返回单个 OpenAI JSON 响应。"""
    response_id = f"chatcmpl-{uuid.uuid4()}"
    logger.info(f"NON-STREAM [ID: {request_id[:8]}]: 开始处理非流式响应。")
    
    full_content = []
    reasoning_content = []
    finish_reason = "stop"
    lmarena_usage = None
    
    enable_reasoning_output = CONFIG.get("enable_lmarena_reasoning", False)
    reasoning_mode = CONFIG.get("reasoning_output_mode", "openai")
    
    async for event_type, data in _process_lmarena_stream_func(request_id):
        if event_type == 'retry_info':
            continue
        elif event_type in ('reasoning', 'reasoning_complete'):
            reasoning_content.append(data)
        elif event_type == 'content':
            full_content.append(data)
        elif event_type == 'finish':
            if isinstance(data, dict):
                finish_reason = data.get('reason', 'stop')
                lmarena_usage = data.get('usage')
            else:
                finish_reason = data
            if finish_reason == 'content-filter':
                full_content.append("\n\n响应被终止，可能是上下文超限或者模型内部审查（大概率）的原因")
        elif event_type == 'error':
            return await _handle_non_stream_error(
                request_id, data, full_content, reasoning_content, model,
                monitoring_service, estimate_message_tokens, estimate_tokens, Response
            )

    # 构建响应
    if enable_reasoning_output and reasoning_content:
        full_reasoning = "".join(reasoning_content)
        if reasoning_mode == "openai":
            response_data = format_openai_non_stream_response(
                "".join(full_content), model, response_id,
                reason=finish_reason, reasoning_content=full_reasoning
            )
        else:  # think_tag
            wrapped = f"<think>{full_reasoning}</think>\n\n" + "".join(full_content)
            response_data = format_openai_non_stream_response_func(wrapped, model, response_id, reason=finish_reason)
    else:
        response_data = format_openai_non_stream_response_func("".join(full_content), model, response_id, reason=finish_reason)
    
    # 计算token
    input_tokens, output_tokens = await _calculate_tokens(
        lmarena_usage, model, "".join(full_content), request_id,
        monitoring_service, estimate_message_tokens, estimate_tokens
    )
    
    response_data['usage'] = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    
    monitoring_service.request_end(
        request_id, success=True, response_content="".join(full_content),
        reasoning_content="".join(reasoning_content) if reasoning_content else None,
        input_tokens=input_tokens, output_tokens=output_tokens,
        full_messages=full_messages
    )
    
    # 释放标签页
    if request_id in request_metadata:
        tab_id = request_metadata[request_id].get("tab_id")
        if tab_id:
            await release_tab_request(tab_id)
    
    return Response(content=json.dumps(response_data, ensure_ascii=False), media_type="application/json")


async def _handle_non_stream_error(request_id, error_data, full_content, reasoning_content, model,
                                    monitoring_service, estimate_message_tokens, estimate_tokens, Response):
    """处理非流式错误"""
    logger.error(f"NON-STREAM [ID: {request_id[:8]}]: 处理时发生错误: {error_data}")
    
    error_content = "".join(full_content) if full_content else None
    input_tokens, output_tokens = 0, 0
    
    if hasattr(monitoring_service, 'active_requests') and request_id in monitoring_service.active_requests:
        request_info = monitoring_service.active_requests[request_id]
        if request_info.request_messages:
            try:
                input_tokens = estimate_message_tokens(request_info.request_messages, model)
            except:
                pass
    
    if error_content:
        try:
            output_tokens = estimate_tokens(error_content, model)
        except:
            output_tokens = len(error_content) // 4
    
    monitoring_service.request_end(
        request_id, success=False, error=str(error_data),
        response_content=error_content,
        reasoning_content="".join(reasoning_content) if reasoning_content else None,
        input_tokens=input_tokens, output_tokens=output_tokens
    )
    await monitoring_service.broadcast_to_monitors({"type": "request_end", "request_id": request_id, "success": False})
    
    status_code = 413 if "附件大小超过了" in str(error_data) else 500
    error_response = {
        "error": {
            "message": f"[LMArena Bridge Error]: {error_data}",
            "type": "bridge_error",
            "code": "attachment_too_large" if status_code == 413 else "processing_error"
        }
    }
    return Response(content=json.dumps(error_response, ensure_ascii=False), status_code=status_code, media_type="application/json")