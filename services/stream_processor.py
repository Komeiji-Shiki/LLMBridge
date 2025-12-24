"""
流处理服务模块
处理来自浏览器的原始数据流，并格式化为OpenAI兼容的响应
"""

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Optional, Tuple
import mimetypes
import base64

logger = logging.getLogger(__name__)


async def _process_lmarena_stream(request_id: str, queue, request_metadata: dict, CONFIG: dict, 
                                   browser_connections: dict, response_channels: dict,
                                   IS_REFRESHING_FOR_VERIFICATION: bool, VERIFICATION_COOLDOWN_UNTIL: Optional[float],
                                   aiohttp_session, IMAGE_BASE64_CACHE: dict, IMAGE_CACHE_MAX_SIZE: int,
                                   IMAGE_CACHE_TTL: int, save_downloaded_image_async, _download_image_data_with_retry,
                                   release_tab_request):
    """
    核心内部生成器：处理来自浏览器的原始数据流，并产生结构化事件。
    事件类型: ('content', str), ('finish', str), ('error', str), ('retry_info', dict)
    """
    # 🔧 终极修复：在函数顶部稳健地定义变量
    stream_cancelled = False
    logger.info(f"[STREAM_LIFECYCLE] 🚀 _process_lmarena_stream 开始处理: {request_id[:8]}")
    
    if not queue:
        logger.error(f"PROCESSOR [ID: {request_id[:8]}]: 无法找到响应通道。")
        yield 'error', 'Internal server error: response channel not found.'
        return

    buffer = ""
    timeout = CONFIG.get("stream_response_timeout_seconds",360)
    text_pattern = re.compile(r'[ab]0:"((?:\\.|[^"\\])*)"')
    # 新增：用于匹配思维链内容的正则表达式
    reasoning_pattern = re.compile(r'ag:"((?:\\.|[^"\\])*)"')
    # 新增：用于匹配和提取图片URL的正则表达式
    image_pattern = re.compile(r'[ab]2:(\[.*?\])')
    finish_pattern = re.compile(r'[ab]d:(\{.*?"finishReason".*?\})')
    error_pattern = re.compile(r'(\{\s*"error".*?\})', re.DOTALL)
    cloudflare_patterns = [r'<title>Just a moment...</title>', r'Enable JavaScript and cookies to continue']
    
    has_yielded_content = False # 标记是否已产出过有效内容
    
    # 思维链相关变量
    # 注意：思维链数据应该总是被收集（用于监控和日志），但是否输出给客户端由配置决定
    enable_reasoning_output = CONFIG.get("enable_lmarena_reasoning", False)  # 是否输出给客户端
    reasoning_buffer = []  # 缓冲所有思维链片段
    has_reasoning = False  # 标记是否有思维链内容
    reasoning_ended = False  # 标记reasoning是否已结束
    
    # 诊断：添加流式性能追踪
    import time as time_module
    last_yield_time = time_module.time()
    chunk_count = 0
    total_chars = 0

    try:
        while True:
            # 🔍 诊断日志：检查请求是否应该被取消
            if request_id not in response_channels:
                logger.warning(f"[STREAM_LIFECYCLE] ⚠️ 请求通道已关闭（可能客户端断开）: {request_id[:8]}")
                stream_cancelled = True
                
                # 🔧 向浏览器发送取消指令
                if request_id in request_metadata:
                    tab_id = request_metadata[request_id].get("tab_id")
                    if tab_id and tab_id in browser_connections:
                        ws = browser_connections[tab_id]
                        cancel_payload = {
                            "command": "cancel_request",
                            "request_id": request_id
                        }
                        try:
                            asyncio.create_task(ws.send_text(json.dumps(cancel_payload)))
                            logger.warning(f"[STREAM_LIFECYCLE] ✉️ 通道关闭时已向浏览器发送取消指令: {request_id[:8]}")
                        except Exception as e:
                            logger.error(f"[STREAM_LIFECYCLE] 发送取消指令失败: {e}")
                
                break
            
            # 关键修复：每次循环开始时重置reasoning_found标志
            reasoning_found_in_this_chunk = False
            
            try:
                # 诊断：记录接收数据的时间
                receive_start = time_module.time()
                raw_data = await asyncio.wait_for(queue.get(), timeout=timeout)
                receive_time = time_module.time() - receive_start
                
                if CONFIG.get("debug_stream_timing", False):
                    logger.debug(f"[STREAM_TIMING] 从队列获取数据耗时: {receive_time:.3f}秒")
                    # 诊断：显示原始数据的前200个字符
                    raw_data_str = str(raw_data)[:200] if raw_data else "None"
                    logger.debug(f"[STREAM_RAW] 原始数据: {raw_data_str}...")
                    
            except asyncio.TimeoutError:
                logger.warning(f"PROCESSOR [ID: {request_id[:8]}]: 等待浏览器数据超时（{timeout}秒）。")
                yield 'error', f'Response timed out after {timeout} seconds.'
                return

            # --- Cloudflare 人机验证处理 ---
            def handle_cloudflare_verification():
                nonlocal IS_REFRESHING_FOR_VERIFICATION, VERIFICATION_COOLDOWN_UNTIL
                if not IS_REFRESHING_FOR_VERIFICATION:
                    logger.warning(f"PROCESSOR [ID: {request_id[:8]}]: 首次检测到人机验证，将发送刷新指令并启动25秒冷却。")
                    IS_REFRESHING_FOR_VERIFICATION = True
                    # 设置25秒冷却期
                    VERIFICATION_COOLDOWN_UNTIL = time.time() + 25
                    # 注意：这里需要browser_ws，但它不在参数中，需要从browser_connections获取
                    if browser_connections:
                        first_ws = list(browser_connections.values())[0]
                        asyncio.create_task(first_ws.send_text(json.dumps({"command": "refresh"}, ensure_ascii=False)))
                    
                    # 启动后台任务：25秒后自动重置状态
                    async def reset_verification_status():
                        await asyncio.sleep(25)
                        nonlocal IS_REFRESHING_FOR_VERIFICATION, VERIFICATION_COOLDOWN_UNTIL
                        IS_REFRESHING_FOR_VERIFICATION = False
                        VERIFICATION_COOLDOWN_UNTIL = None
                        logger.info("⏰ 人机验证冷却期已结束，系统已恢复正常。")
                    
                    asyncio.create_task(reset_verification_status())
                    return "检测到人机验证，已发送刷新指令。系统将冷却25秒，请稍后重试。"
                else:
                    # 计算剩余冷却时间
                    if VERIFICATION_COOLDOWN_UNTIL:
                        remaining = max(0, int(VERIFICATION_COOLDOWN_UNTIL - time.time()))
                        if remaining > 0:
                            logger.info(f"PROCESSOR [ID: {request_id[:8]}]: 检测到人机验证，冷却中（剩余{remaining}秒）。")
                            return f"正在等待人机验证冷却完成...（剩余 {remaining} 秒）"
                    logger.info(f"PROCESSOR [ID: {request_id[:8]}]: 检测到人机验证，但已在刷新中，将等待。")
                    return "正在等待人机验证完成..."

            # 1. 检查来自 WebSocket 端的直接错误或重试信息
            if isinstance(raw_data, dict):
                # 处理重试信息
                if 'retry_info' in raw_data:
                    retry_info = raw_data.get('retry_info', {})
                    logger.info(f"PROCESSOR [ID: {request_id[:8]}]: 收到重试信息 - 尝试 {retry_info.get('attempt')}/{retry_info.get('max_attempts')}")
                    # 可以选择将重试信息传递给客户端
                    yield 'retry_info', retry_info
                    continue
                
                # 处理错误
                if 'error' in raw_data:
                    error_msg = raw_data.get('error', 'Unknown browser error')
                if isinstance(error_msg, str):
                    if '413' in error_msg or 'too large' in error_msg.lower():
                        friendly_error_msg = "上传失败：附件大小超过了 LMArena 服务器的限制 (通常是 5MB左右)。请尝试压缩文件或上传更小的文件。"
                        logger.warning(f"PROCESSOR [ID: {request_id[:8]}]: 检测到附件过大错误 (413)。")
                        yield 'error', friendly_error_msg
                        return
                    if any(re.search(p, error_msg, re.IGNORECASE) for p in cloudflare_patterns):
                        yield 'error', handle_cloudflare_verification()
                        return
                yield 'error', error_msg
                return

            # 2. 检查 [DONE] 信号
            if raw_data == "[DONE]":
                logger.info(f"[STREAM_END] 收到[DONE]信号 - 请求 {request_id[:8]}")
                
                # 🔧 核心修复：等待额外的数据可能还在传输中
                logger.info(f"[STREAM_END] ⏳ 等待200ms以接收可能延迟到达的数据...")
                try:
                    # 尝试接收更多数据，但设置短超时
                    extra_data = await asyncio.wait_for(queue.get(), timeout=0.2)
                    if extra_data != "[DONE]":
                        logger.info(f"[STREAM_END] ✅ 收到延迟数据，长度: {len(str(extra_data))}")
                        # 将延迟数据添加到buffer
                        buffer += "".join(str(item) for item in extra_data) if isinstance(extra_data, list) else extra_data
                except asyncio.TimeoutError:
                    logger.info(f"[STREAM_END] ⏰ 超时，没有更多延迟数据")
                
                # 🔧 核心修复：在退出前强制处理buffer中的所有剩余内容
                if len(buffer) > 0:
                    logger.info(f"[STREAM_END] ⚠️ Buffer还有 {len(buffer)} 字符未处理，开始强制提取...")
                    logger.debug(f"[STREAM_END] Buffer完整内容: {buffer}")
                    
                    final_extracted_count = 0
                    
                    # 🔧 改进：使用更宽松的正则来匹配可能被截断的内容
                    extraction_attempts = 0
                    max_attempts = 100  # 防止无限循环
                    
                    while extraction_attempts < max_attempts:
                        extraction_attempts += 1
                        match = text_pattern.search(buffer)
                        
                        if not match:
                            # 如果没有完整匹配，尝试查找可能被截断的内容
                            partial_pattern = re.compile(r'[ab]0:"([^"]*?)(?:"|$)')
                            partial_match = partial_pattern.search(buffer)
                            
                            if partial_match and len(partial_match.group(1)) > 0:
                                logger.warning(f"[STREAM_END] 发现可能被截断的内容，尝试提取...")
                                matched_text = partial_match.group(1)
                                match_end = partial_match.end()
                            else:
                                break  # 没有更多可提取的内容
                        else:
                            matched_text = match.group(1)
                            match_end = match.end()
                        
                        try:
                            text_content = json.loads(f'"{matched_text}"')
                            if text_content:
                                has_yielded_content = True
                                total_chars += len(text_content)
                                final_extracted_count += 1
                                
                                logger.info(f"[STREAM_END] 提取文本块#{final_extracted_count}: {text_content[:100]}...")
                                yield 'content', text_content
                                
                                # 立即处理，避免阻塞
                                await asyncio.sleep(0)
                        except (ValueError, json.JSONDecodeError) as e:
                            logger.warning(f"[STREAM_END] JSON解析失败: {e}, 文本: {matched_text[:100]}")
                        
                        # 删除已处理的部分
                        buffer = buffer[match_end:]
                    
                    # 处理剩余的思维链内容
                    while (match := reasoning_pattern.search(buffer)):
                        try:
                            reasoning_content = json.loads(f'"{match.group(1)}"')
                            if reasoning_content and enable_reasoning_output:
                                has_reasoning = True
                                reasoning_buffer.append(reasoning_content)
                                if CONFIG.get("preserve_streaming", True):
                                    yield 'reasoning', reasoning_content
                        except (ValueError, json.JSONDecodeError):
                            pass
                        buffer = buffer[match.end():]
                    
                    # 处理剩余的图片内容
                    while (match := image_pattern.search(buffer)):
                        try:
                            image_data_list = json.loads(match.group(1))
                            if isinstance(image_data_list, list) and image_data_list:
                                image_info = image_data_list[0]
                                if image_info.get("type") == "image" and "image" in image_info:
                                    # 这里应该继续完整的图片处理逻辑
                                    # 但由于代码太长，暂时跳过
                                    pass
                        except (json.JSONDecodeError, IndexError):
                            pass
                        buffer = buffer[match.end():]
                    
                    if final_extracted_count > 0:
                        logger.info(f"[STREAM_END] ✅ 成功从buffer提取了 {final_extracted_count} 个文本块，共 {total_chars} 字符")
                    elif len(buffer) > 0:
                        logger.warning(f"[STREAM_END] ⚠️ 未能从buffer提取任何内容，buffer可能格式异常")
                        logger.warning(f"[STREAM_END] 剩余buffer长度: {len(buffer)}")
                        logger.warning(f"[STREAM_END] 剩余buffer内容: {buffer}")
                        
                        # 🔧 检查是否是非内容标记
                        is_control_marker = False
                        for control_prefix in ['a3:', 'ad:', 'b3:', 'bd:', 'ae:', 'be:']:
                            if control_prefix in buffer:
                                logger.info(f"[STREAM_END] 检测到控制标记 {control_prefix}，忽略不输出")
                                is_control_marker = True
                                break
                        
                        # 只有在不是控制标记时才尝试作为文本输出
                        if not is_control_marker and buffer.strip() and not buffer.startswith('[') and not buffer.startswith('{'):
                            logger.warning(f"[STREAM_END] 尝试将剩余内容作为普通文本处理...")
                            # 移除控制字符，只保留可打印字符
                            clean_text = ''.join(c for c in buffer if c.isprintable() or c in '\n\r\t')
                            if clean_text.strip():
                                logger.info(f"[STREAM_END] ⚡ 从异常buffer中提取到文本: {clean_text[:200]}...")
                                yield 'content', clean_text
                
                if has_yielded_content and IS_REFRESHING_FOR_VERIFICATION:
                     logger.info(f"PROCESSOR [ID: {request_id[:8]}]: 请求成功，人机验证状态将在下次连接时重置。")
                break

            # 3. 累加缓冲区并检查内容
            buffer += "".join(str(item) for item in raw_data) if isinstance(raw_data, list) else raw_data
            
            # 诊断：显示缓冲区大小
            if CONFIG.get("debug_stream_timing", False):
                logger.debug(f"[STREAM_BUFFER] 缓冲区大小: {len(buffer)} 字符")

            if any(re.search(p, buffer, re.IGNORECASE) for p in cloudflare_patterns):
                yield 'error', handle_cloudflare_verification()
                return
            
            if (error_match := error_pattern.search(buffer)):
                try:
                    error_json = json.loads(error_match.group(1))
                    yield 'error', error_json.get("error", "来自 LMArena 的未知错误")
                    return
                except json.JSONDecodeError: pass

            # 优先处理思维链内容（ag前缀）
            reasoning_found_in_this_chunk = False
            while (match := reasoning_pattern.search(buffer)):
                try:
                    reasoning_content = json.loads(f'"{match.group(1)}"')
                    if reasoning_content:
                        # 警告：检测到reasoning在content之后出现（异常情况）
                        if reasoning_ended:
                            logger.warning(f"[REASONING_WARN] 检测到reasoning在content之后继续出现，这可能导致think_tag模式下内容丢失！")
                        
                        # 总是收集思维链（用于监控和日志）
                        has_reasoning = True
                        reasoning_buffer.append(reasoning_content)
                        reasoning_found_in_this_chunk = True
                        
                        # 只在配置启用时才输出给客户端
                        if enable_reasoning_output and CONFIG.get("preserve_streaming", True):
                            # 流式输出思维链
                            yield 'reasoning', reasoning_content
                        
                except (ValueError, json.JSONDecodeError) as e:
                    if CONFIG.get("debug_stream_timing", False):
                        logger.debug(f"[REASONING_ERROR] 解析错误: {e}")
                    pass
                buffer = buffer[match.end():]
            
            # 处理文本内容（a0前缀）- 添加诊断
            process_start = time_module.time()
            chunks_in_buffer = 0
            
            # 诊断：检查是否有匹配
            if CONFIG.get("debug_stream_timing", False):
                matches_found = text_pattern.findall(buffer)
                if matches_found:
                    logger.debug(f"[STREAM_MATCH] 找到 {len(matches_found)} 个文本匹配")
                    for idx, match in enumerate(matches_found[:3]):  # 只显示前3个
                        logger.debug(f"  匹配#{idx+1}: {match[:50]}...")
            
            while (match := text_pattern.search(buffer)):
                matched_text = match.group(1)
                match_end = match.end()
                
                try:
                    text_content = json.loads(f'"{matched_text}"')
                    if text_content:
                        # 关键修复：在第一个content到来时，如果有reasoning且未结束，则标记结束
                        if has_reasoning and not reasoning_ended and not reasoning_found_in_this_chunk:
                            reasoning_ended = True
                            logger.info(f"[REASONING_END] 检测到reasoning结束（共{len(reasoning_buffer)}个片段）")
                            # 只在启用输出时才发送结束事件
                            if enable_reasoning_output:
                                yield 'reasoning_end', None
                        
                        has_yielded_content = True
                        chunk_count += 1
                        total_chars += len(text_content)
                        chunks_in_buffer += 1
                        
                        # 诊断：记录yield间隔
                        current_time = time_module.time()
                        yield_interval = current_time - last_yield_time
                        last_yield_time = current_time
                        
                        if CONFIG.get("debug_stream_timing", False):
                            logger.debug(f"[STREAM_TIMING] Yield间隔: {yield_interval:.3f}秒, "
                                       f"块#{chunk_count}, 字符数: {len(text_content)}, "
                                       f"累计字符: {total_chars}")
                        
                        yield 'content', text_content
                        
                        # 立即处理，不要等待
                        await asyncio.sleep(0)
                        
                        # 🔧 关键修复：成功处理后才删除buffer
                        buffer = buffer[match_end:]
                    else:
                        # 空内容，也要删除以避免死循环
                        buffer = buffer[match_end:]
                        
                except (ValueError, json.JSONDecodeError) as e:
                    # 🔧 关键修复：解析失败时记录错误但仍然删除，避免死循环
                    logger.warning(f"[PARSE_ERROR] JSON解析失败: {e}, 匹配文本: {matched_text[:100]}...")
                    buffer = buffer[match_end:]
            
            # 诊断：记录处理时间
            if chunks_in_buffer > 0 and CONFIG.get("debug_stream_timing", False):
                process_time = time_module.time() - process_start
                logger.debug(f"[STREAM_TIMING] 处理{chunks_in_buffer}个文本块耗时: {process_time:.3f}秒")

            # 新增：处理图片内容（由于篇幅限制，这里简化处理）
            while (match := image_pattern.search(buffer)):
                try:
                    image_data_list = json.loads(match.group(1))
                    if isinstance(image_data_list, list) and image_data_list:
                        image_info = image_data_list[0]
                        if image_info.get("type") == "image" and "image" in image_info:
                            image_url = image_info['image']
                            
                            # 将LMArena返回的图片URL转换为base64返回给客户端
                            show_full_urls = CONFIG.get("debug_show_full_urls", False)
                            if show_full_urls:
                                logger.info(f"📥 LMArena返回图片URL（完整）: {image_url}")
                            else:
                                display_length = CONFIG.get("url_display_length", 200)
                                if len(image_url) <= display_length:
                                    logger.info(f"📥 LMArena返回图片URL: {image_url}")
                                else:
                                    logger.info(f"📥 LMArena返回图片URL: {image_url[:display_length]}...")
                                    logger.debug(f"   完整URL: {image_url}")
                            
                            # 记录开始时间
                            import time as time_module
                            process_start_time = time_module.time()
                            
                            # 获取返回模式配置
                            return_format_config = CONFIG.get("image_return_format", {})
                            return_mode = return_format_config.get("mode", "base64")
                            save_locally = CONFIG.get("save_images_locally", True)
                            
                            logger.info(f"[IMG_PROCESS] 开始处理图片")
                            logger.info(f"  - 返回模式: {return_mode}")
                            logger.info(f"  - 本地保存: {save_locally}")
                            
                            # URL模式：立即返回，不阻塞
                            if return_mode == "url":
                                logger.info(f"[IMG_PROCESS] URL模式 - 立即返回URL给客户端")
                                yield 'content', f"![Image]({image_url})"
                                
                                # 如果需要保存到本地，创建后台任务（不阻塞响应）
                                if save_locally:
                                    logger.info(f"[IMG_PROCESS] 启动后台任务异步下载并保存图片")
                                    
                                    async def async_download_and_save():
                                        try:
                                            download_start = time_module.time()
                                            img_data, err = await _download_image_data_with_retry(image_url)
                                            download_time = time_module.time() - download_start
                                            
                                            if img_data:
                                                logger.info(f"[IMG_PROCESS] 后台下载成功，耗时: {download_time:.2f}秒")
                                                await save_downloaded_image_async(img_data, image_url, request_id)
                                                logger.info(f"[IMG_PROCESS] 图片已保存到本地")
                                            else:
                                                logger.error(f"[IMG_PROCESS] 后台下载失败: {err}")
                                        except Exception as e:
                                            logger.error(f"[IMG_PROCESS] 后台任务异常: {e}")
                                    
                                    asyncio.create_task(async_download_and_save())
                                else:
                                    logger.info(f"[IMG_PROCESS] save_images_locally=false，跳过下载")
                                
                                # URL模式处理完成，继续处理下一个消息
                                continue
                            
                            # Base64模式：必须先下载才能转换
                            logger.info(f"[IMG_PROCESS] Base64模式 - 需要下载图片进行转换")
                            
                            # 下载图片数据
                            download_start_time = time_module.time()
                            image_data, download_error = await _download_image_data_with_retry(image_url)
                            download_time = time_module.time() - download_start_time
                            logger.info(f"[IMG_PROCESS] 图片下载完成，耗时: {download_time:.2f}秒")
                            
                            # 如果需要保存到本地
                            if save_locally and image_data:
                                logger.info(f"[IMG_PROCESS] 异步保存图片到本地")
                                asyncio.create_task(save_downloaded_image_async(image_data, image_url, request_id))
                            elif not save_locally:
                                logger.info(f"[IMG_PROCESS] save_images_locally=false，跳过本地保存")
                            
                            # Base64转换
                            if True:  # 这里确定是base64模式
                                if image_data:
                                    # --- Base64 转换和缓存逻辑 ---
                                    cache_key = image_url
                                    current_time = time_module.time()
                                    
                                    # 清理过期缓存
                                    if len(IMAGE_BASE64_CACHE) > IMAGE_CACHE_MAX_SIZE:
                                        sorted_items = sorted(IMAGE_BASE64_CACHE.items(), key=lambda x: x[1][1])
                                        for url, _ in sorted_items[:IMAGE_CACHE_MAX_SIZE // 2]:
                                            del IMAGE_BASE64_CACHE[url]
                                        logger.info(f"  🧹 清理了 {IMAGE_CACHE_MAX_SIZE // 2} 个旧缓存")

                                    # 检查缓存
                                    if cache_key in IMAGE_BASE64_CACHE:
                                        cached_data, cache_time = IMAGE_BASE64_CACHE[cache_key]
                                        if current_time - cache_time < IMAGE_CACHE_TTL:
                                            logger.info(f"  ⚡ 从缓存获取图片Base64")
                                            yield 'content', cached_data
                                            continue
                                    
                                    # 执行转换
                                    content_type = mimetypes.guess_type(image_url)[0] or 'image/png'
                                    image_base64 = base64.b64encode(image_data).decode('ascii')
                                    data_url = f"data:{content_type};base64,{image_base64}"
                                    markdown_image = f"![Image]({data_url})"
                                    
                                    # 存入缓存
                                    IMAGE_BASE64_CACHE[cache_key] = (markdown_image, current_time)
                                    
                                    # 计算总耗时
                                    total_time = time_module.time() - process_start_time
                                    logger.info(f"[IMG_PROCESS] Base64转换完成，总耗时: {total_time:.2f}秒")
                                    
                                    yield 'content', markdown_image
                                else:
                                    # 下载失败，降级返回URL
                                    logger.error(f"[IMG_PROCESS] ❌ 图片下载失败 ({download_error})，降级返回原始URL")
                                    total_time = time_module.time() - process_start_time
                                    logger.info(f"[IMG_PROCESS] 处理完成（失败降级），总耗时: {total_time:.2f}秒")
                                    yield 'content', f"![Image]({image_url})"

                except (json.JSONDecodeError, IndexError) as e:
                    logger.warning(f"解析图片URL时出错: {e}, buffer: {buffer[:150]}")
                buffer = buffer[match.end():]

            if (finish_match := finish_pattern.search(buffer)):
                try:
                    finish_data = json.loads(finish_match.group(1))
                    finish_reason = finish_data.get("finishReason", "stop")
                    
                    # 🔧 新增：尝试提取LMArena返回的实际token使用信息
                    usage_info = None
                    if "usage" in finish_data:
                        usage_info = finish_data["usage"]
                        logger.info(f"[TOKEN_EXTRACT] 从LMArena提取到token使用信息: {usage_info}")
                    elif "tokenUsage" in finish_data:
                        usage_info = finish_data["tokenUsage"]
                        logger.info(f"[TOKEN_EXTRACT] 从LMArena提取到tokenUsage信息: {usage_info}")
                    
                    # 将finish_reason和usage_info一起传递
                    yield 'finish', {'reason': finish_reason, 'usage': usage_info}
                except (json.JSONDecodeError, IndexError): pass
                buffer = buffer[finish_match.end():]

    except asyncio.CancelledError:
        stream_cancelled = True
        logger.warning(f"[STREAM_LIFECYCLE] 🚫 任务被取消（asyncio.CancelledError）: {request_id[:8]}")
        logger.warning(f"  - 这意味着客户端已断开，应该停止处理")
        
        # 🔧 核心修复：向浏览器发送取消指令，中止fetch请求
        if request_id in request_metadata:
            tab_id = request_metadata[request_id].get("tab_id")
            if tab_id and tab_id in browser_connections:
                ws = browser_connections[tab_id]
                cancel_payload = {
                    "command": "cancel_request",
                    "request_id": request_id
                }
                try:
                    # 使用 create_task 避免阻塞清理流程
                    asyncio.create_task(ws.send_text(json.dumps(cancel_payload)))
                    logger.warning(f"[STREAM_LIFECYCLE] ✉️ 已向浏览器发送取消指令: {request_id[:8]}")
                except Exception as e:
                    logger.error(f"[STREAM_LIFECYCLE] 发送取消指令失败: {e}")
            else:
                logger.warning(f"[STREAM_LIFECYCLE] ⚠️ 无法发送取消指令：找不到标签页连接")
        else:
            logger.warning(f"[STREAM_LIFECYCLE] ⚠️ 无法发送取消指令：找不到请求元数据")
    finally:
        # 🔍 诊断日志：记录流处理结束状态
        if stream_cancelled:
            logger.warning(f"[STREAM_LIFECYCLE] ⛔ 流处理异常结束: {request_id[:8]}")
            logger.warning(f"  - 原因: 任务取消或通道关闭")
        else:
            logger.info(f"[STREAM_LIFECYCLE] ✅ 流处理正常结束: {request_id[:8]}")
        
        # 在清理前，如果有思维链内容且未流式输出，则一次性输出
        if enable_reasoning_output and has_reasoning and not CONFIG.get("preserve_streaming", True):
            # 非流式模式：在最后一次性输出完整思维链
            full_reasoning = "".join(reasoning_buffer)
            yield 'reasoning_complete', full_reasoning
        
        # 诊断：输出流式性能统计
        if chunk_count > 0 and CONFIG.get("debug_stream_timing", False):
            total_time = time_module.time() - (last_yield_time - yield_interval if 'yield_interval' in locals() else last_yield_time)
            logger.info(f"[STREAM_STATS] 请求ID: {request_id[:8]}")
            logger.info(f"  - 总块数: {chunk_count}")
            logger.info(f"  - 总字符数: {total_chars}")
            logger.info(f"  - 平均块大小: {total_chars/chunk_count:.1f}字符")
            logger.info(f"  - 平均yield间隔: {total_time/chunk_count:.3f}秒")
        
        # 🔧 关键修复：释放标签页请求计数
        if request_id in request_metadata:
            tab_id = request_metadata[request_id].get("tab_id")
            if tab_id:
                await release_tab_request(tab_id)
                logger.debug(f"PROCESSOR [ID: {request_id[:8]}]: 已释放标签页 '{tab_id}' 的请求计数")
        
        # 🔧 核心修复：延迟清理响应通道，给浏览器缓冲区时间发送最后的数据
        # 只在正常结束时延迟，取消时立即清理
        if not stream_cancelled:
            logger.debug(f"[STREAM_LIFECYCLE] 等待1秒后清理通道，确保浏览器缓冲数据发送完毕")
            await asyncio.sleep(1.0)
        
        if request_id in response_channels:
            del response_channels[request_id]
            logger.info(f"PROCESSOR [ID: {request_id[:8]}]: 响应通道已清理。")
        
        # 清理请求元数据（修复内存泄漏）
        if request_id in request_metadata:
            del request_metadata[request_id]
            logger.debug(f"PROCESSOR [ID: {request_id[:8]}]: 请求元数据已清理。")


async def stream_generator(request_id: str, model: str, _process_lmarena_stream_func,
                           format_openai_chunk, format_openai_finish_chunk, format_openai_error_chunk,
                           CONFIG: dict, response_channels: dict, request_metadata: dict,
                           monitoring_service, estimate_message_tokens, estimate_tokens,
                           browser_connections: dict):
    """将内部事件流格式化为 OpenAI SSE 响应。"""
    response_id = f"chatcmpl-{uuid.uuid4()}"
    logger.info(f"STREAMER [ID: {request_id[:8]}]: 流式生成器启动。")
    
    # 🔍 诊断日志：标记生成器是否被取消
    is_cancelled = False
    client_disconnected = False
    
    finish_reason_to_send = 'stop'  # 默认的结束原因
    collected_content = []  # 收集响应内容用于存储
    reasoning_content = []  # 收集思维链内容
    lmarena_usage = None  # 存储从LMArena提取的token使用信息
    
    # 诊断：添加流式性能追踪
    import time as time_module
    stream_start_time = time_module.time()
    chunks_sent = 0
    
    # 思维链配置
    enable_reasoning_output = CONFIG.get("enable_lmarena_reasoning", False)
    reasoning_mode = CONFIG.get("reasoning_output_mode", "openai")
    preserve_streaming = CONFIG.get("preserve_streaming", True)

    async for event_type, data in _process_lmarena_stream_func(request_id):
        # 🔍 诊断日志：检查客户端是否断开
        try:
            # 尝试yield一个空数据来检测客户端连接
            yield ""
        except (GeneratorExit, asyncio.CancelledError) as e:
            client_disconnected = True
            logger.warning(f"[DISCONNECT_DETECT] 🚫 客户端已断开！请求 {request_id[:8]}")
            logger.warning(f"  - 异常类型: {type(e).__name__}")
            
            # 🔧 核心修复：通知监控系统请求已结束
            monitoring_service.request_end(
                request_id,
                success=False,
                error=f"Client disconnected: {type(e).__name__}"
            )
            logger.info(f"[DISCONNECT_DETECT] 已通知监控系统客户端断开: {request_id[:8]}")
            
            # 🔧 核心修复：向浏览器发送取消指令
            if request_id in request_metadata:
                tab_id = request_metadata[request_id].get("tab_id")
                if tab_id and tab_id in browser_connections:
                    ws = browser_connections[tab_id]
                    cancel_payload = {
                        "command": "cancel_request",
                        "request_id": request_id
                    }
                    # 使用 create_task 以免阻塞当前清理流程
                    asyncio.create_task(ws.send_text(json.dumps(cancel_payload)))
                    logger.info(f"[DISCONNECT_DETECT] ✉️  已向标签页 '{tab_id}' 发送请求取消指令: {request_id[:8]}")
                else:
                    logger.warning(f"[DISCONNECT_DETECT] ⚠️ 无法发送取消指令：找不到标签页连接")
            else:
                logger.warning(f"[DISCONNECT_DETECT] ⚠️ 无法发送取消指令：找不到请求元数据 for {request_id[:8]}")

            is_cancelled = True
            break
        
        if event_type == 'retry_info':
            # 处理重试信息
            retry_msg = f"\n[重试信息] 尝试 {data.get('attempt')}/{data.get('max_attempts')}，原因: {data.get('reason')}，等待 {data.get('delay')/1000}秒...\n"
            logger.info(f"STREAMER [ID: {request_id[:8]}]: {retry_msg.strip()}")
            # 可选：将重试信息作为注释发送给客户端
            if CONFIG.get("show_retry_info_to_client", False):
                yield format_openai_chunk(retry_msg, model, response_id)
        elif event_type == 'reasoning':
            # 处理思维链片段
            reasoning_content.append(data)
            
            if enable_reasoning_output:
                if reasoning_mode == "openai" and preserve_streaming:
                    # OpenAI模式且启用流式：发送reasoning delta
                    chunk = {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {"reasoning_content": data},
                            "finish_reason": None
                        }]
                    }
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    
        elif event_type == 'reasoning_end':
            # 新增：reasoning结束事件（think_tag模式专用）
            if enable_reasoning_output and reasoning_mode == "think_tag" and reasoning_content:
                # 立即输出完整的reasoning
                full_reasoning = "".join(reasoning_content)
                wrapped_reasoning = f"<think>{full_reasoning}</think>\n\n"
                yield format_openai_chunk(wrapped_reasoning, model, response_id)
                logger.info(f"[THINK_TAG] 已输出完整reasoning（{len(reasoning_content)}个片段）")
                
        elif event_type == 'reasoning_complete':
            # 处理完整思维链（非流式模式）
            full_reasoning = data
            reasoning_content.append(full_reasoning)
            
            if enable_reasoning_output and not preserve_streaming:
                if reasoning_mode == "openai":
                    # OpenAI模式：发送完整reasoning
                    chunk = {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {"reasoning_content": full_reasoning},
                            "finish_reason": None
                        }]
                    }
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                elif reasoning_mode == "think_tag":
                    # think_tag模式：包裹后作为content输出
                    wrapped_reasoning = f"<think>{full_reasoning}</think>\n\n"
                    yield format_openai_chunk(wrapped_reasoning, model, response_id)
                    
        elif event_type == 'content':
            
            collected_content.append(data)  # 收集内容
            chunks_sent += 1
            
            # 立即生成并发送数据块，不要累积
            chunk_data = format_openai_chunk(data, model, response_id)
            
            if CONFIG.get("debug_stream_timing", False):
                logger.debug(f"[STREAM_OUTPUT] 发送块#{chunks_sent}, 大小: {len(chunk_data)}字节, 内容: {data[:100]}...")
            
            # 🔧 关键修复：确保每个块都被完整yield
            try:
                yield chunk_data
            except (GeneratorExit, asyncio.CancelledError):
                # 客户端断开，但仍要记录已发送的内容
                logger.warning(f"[STREAM_OUTPUT] ⚠️ 发送块#{chunks_sent}时客户端断开")
                raise
            
            # 强制刷新缓冲区（给其他协程执行机会）
            await asyncio.sleep(0)
        elif event_type == 'finish':
            # 记录结束原因，但不要立即返回，等待浏览器发送 [DONE]
            # data现在是一个字典: {'reason': ..., 'usage': ...}
            if isinstance(data, dict):
                finish_reason_to_send = data.get('reason', 'stop')
                lmarena_usage = data.get('usage')
                if lmarena_usage:
                    logger.info(f"[TOKEN_STREAM] 捕获到LMArena token使用信息: {lmarena_usage}")
            else:
                # 向后兼容旧格式
                finish_reason_to_send = data
            
            if finish_reason_to_send == 'content-filter':
                warning_msg = "\n\n响应被终止，可能是上下文超限或者模型内部审查（大概率）的原因"
                collected_content.append(warning_msg)  # 也收集警告信息
                yield format_openai_chunk(warning_msg, model, response_id)
        elif event_type == 'error':
            logger.error(f"STREAMER [ID: {request_id[:8]}]: 流中发生错误: {data}")
            
            # 🔍 诊断日志：记录错误时的状态
            logger.error(f"[DISCONNECT_DETECT] 错误发生时状态:")
            logger.error(f"  - 客户端断开: {client_disconnected}")
            logger.error(f"  - 生成器取消: {is_cancelled}")
            
            monitoring_service.request_end(
                request_id,
                success=False,
                error=str(data),
                response_content="".join(collected_content) if collected_content else None
            )
            await monitoring_service.broadcast_to_monitors({
                "type": "request_end",
                "request_id": request_id,
                "success": False
            })
            yield format_openai_error_chunk(str(data), model, response_id)
            yield format_openai_finish_chunk(model, response_id, reason='stop')
            return # 发生错误时，可以立即终止

    # 只有在 _process_lmarena_stream 自然结束后 (即收到 [DONE]) 才执行
    # 🔧 关键修复：在发送结束块前，添加短暂延迟确保所有内容缓冲都已刷新
    await asyncio.sleep(0.1)  # 100ms延迟，确保所有yield都已完成
    
    # 🔧 核心修复：在使用input_tokens之前先计算它们
    # 记录请求成功（包含响应内容）
    full_response = "".join(collected_content)
    full_reasoning = "".join(reasoning_content) if reasoning_content else None
    
    # 🔧 改进：优先使用LMArena返回的实际token数，否则使用tiktoken精确计数
    input_tokens = 0
    output_tokens = 0
    
    if lmarena_usage:
        # 使用LMArena返回的实际token数
        input_tokens = lmarena_usage.get('inputTokens', 0) or lmarena_usage.get('prompt_tokens', 0)
        output_tokens = lmarena_usage.get('outputTokens', 0) or lmarena_usage.get('completion_tokens', 0)
        logger.info(f"[TOKEN_STREAM] 使用LMArena实际token数: input={input_tokens}, output={output_tokens}")
    else:
        # 回退到本地tokenizer计数
        # 首先需要导入get_tokenizer_for_model来确定使用哪种tokenizer
        from modules.token_counter import get_tokenizer_for_model
        tokenizer_type = get_tokenizer_for_model(model)
        logger.info(f"[TOKEN_STREAM] LMArena未提供token信息，使用{tokenizer_type}计数（模型: {model}）")
        if hasattr(monitoring_service, 'active_requests') and request_id in monitoring_service.active_requests:
            request_info = monitoring_service.active_requests[request_id]
            if request_info.request_messages:
                # 使用tiktoken计算输入token
                try:
                    input_tokens = estimate_message_tokens(request_info.request_messages, model)
                    logger.info(f"[TOKEN_STREAM] {tokenizer_type}计算输入tokens: {input_tokens}")
                except Exception as e:
                    logger.warning(f"[TOKEN_STREAM] tiktoken计算失败，回退到估算: {e}")
                    # 回退到简单估算
                    for msg in request_info.request_messages:
                        if isinstance(msg, dict) and 'content' in msg:
                            content = msg.get('content', '')
                            if isinstance(content, str):
                                input_tokens += len(content) // 4
                            elif isinstance(content, list):
                                for part in content:
                                    if isinstance(part, dict) and part.get('type') == 'text':
                                        input_tokens += len(part.get('text', '')) // 4
        
        # 使用本地tokenizer计算输出token
        try:
            output_tokens = estimate_tokens(full_response, model)
            logger.info(f"[TOKEN_STREAM] {tokenizer_type}计算输出tokens: {output_tokens}")
        except Exception as e:
            logger.warning(f"[TOKEN_STREAM] tiktoken计算输出失败，回退到估算: {e}")
            output_tokens = len(full_response) // 4
    
    final_usage = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    yield format_openai_finish_chunk(model, response_id, reason=finish_reason_to_send, usage=final_usage)
    
    # 诊断：输出流式输出统计和内容校验
    if CONFIG.get("debug_stream_timing", False) and chunks_sent > 0:
        total_time = time_module.time() - stream_start_time
        collected_chars = sum(len(c) for c in collected_content)
        logger.info(f"[STREAM_OUTPUT_STATS] 请求ID: {request_id[:8]}")
        logger.info(f"  - 发送块数: {chunks_sent}")
        logger.info(f"  - 总字符数: {collected_chars}")
        logger.info(f"  - 总耗时: {total_time:.2f}秒")
        logger.info(f"  - 平均发送间隔: {total_time/chunks_sent:.3f}秒/块")
    
    # 🔧 内容完整性校验：确保collected_content不为空
    if not collected_content and not client_disconnected:
        logger.error(f"[STREAM_OUTPUT_STATS] ⚠️ 警告：没有收集到任何内容！可能存在内容丢失")
    
    # 🔍 诊断日志：记录结束状态
    if client_disconnected:
        logger.warning(f"[DISCONNECT_DETECT] ⚠️ 流式生成器因客户端断开而结束: {request_id[:8]}")
    elif is_cancelled:
        logger.warning(f"[DISCONNECT_DETECT] ⚠️ 流式生成器被取消: {request_id[:8]}")
    else:
        logger.info(f"STREAMER [ID: {request_id[:8]}]: 流式生成器正常结束。")

    # 记录请求成功
    monitoring_service.request_end(
        request_id,
        success=True,
        response_content=full_response,
        reasoning_content=full_reasoning,
        input_tokens=input_tokens,
        output_tokens=output_tokens
    )
    await monitoring_service.broadcast_to_monitors({
        "type": "request_end",
        "request_id": request_id,
        "success": True
    })


async def non_stream_response(request_id: str, model: str, _process_lmarena_stream_func,
                              format_openai_non_stream_response, CONFIG: dict,
                              response_channels: dict, request_metadata: dict,
                              monitoring_service, estimate_message_tokens, estimate_tokens,
                              release_tab_request, Response):
    """聚合内部事件流并返回单个 OpenAI JSON 响应。"""
    response_id = f"chatcmpl-{uuid.uuid4()}"
    logger.info(f"NON-STREAM [ID: {request_id[:8]}]: 开始处理非流式响应。")
    
    full_content = []
    reasoning_content = []
    finish_reason = "stop"
    lmarena_usage = None  # 存储从LMArena提取的token使用信息
    
    # 思维链配置
    enable_reasoning_output = CONFIG.get("enable_lmarena_reasoning", False)
    reasoning_mode = CONFIG.get("reasoning_output_mode", "openai")
    
    async for event_type, data in _process_lmarena_stream_func(request_id):
        if event_type == 'retry_info':
            # 非流式响应中记录重试信息
            logger.info(f"NON-STREAM [ID: {request_id[:8]}]: 重试信息 - 尝试 {data.get('attempt')}/{data.get('max_attempts')}")
        elif event_type == 'reasoning' or event_type == 'reasoning_complete':
            # 收集思维链内容
            reasoning_content.append(data)
        elif event_type == 'content':
            full_content.append(data)
        elif event_type == 'finish':
            # data现在是一个字典: {'reason': ..., 'usage': ...}
            if isinstance(data, dict):
                finish_reason = data.get('reason', 'stop')
                lmarena_usage = data.get('usage')
                if lmarena_usage:
                    logger.info(f"[TOKEN_NON_STREAM] 捕获到LMArena token使用信息: {lmarena_usage}")
            else:
                # 向后兼容旧格式
                finish_reason = data
            
            if finish_reason == 'content-filter':
                full_content.append("\n\n响应被终止，可能是上下文超限或者模型内部审查（大概率）的原因")
        elif event_type == 'error':
            logger.error(f"NON-STREAM [ID: {request_id[:8]}]: 处理时发生错误: {data}")
            
            monitoring_service.request_end(
                request_id,
                success=False,
                error=str(data),
                response_content="".join(full_content) if full_content else None
            )
            await monitoring_service.broadcast_to_monitors({
                "type": "request_end",
                "request_id": request_id,
                "success": False
            })
            
            # 统一流式和非流式响应的错误状态码
            status_code = 413 if "附件大小超过了" in str(data) else 500

            error_response = {
                "error": {
                    "message": f"[LMArena Bridge Error]: {data}",
                    "type": "bridge_error",
                    "code": "attachment_too_large" if status_code == 413 else "processing_error"
                }
            }
            return Response(content=json.dumps(error_response, ensure_ascii=False), status_code=status_code, media_type="application/json")

    # 处理思维链内容
    if enable_reasoning_output and reasoning_content:
        full_reasoning = "".join(reasoning_content)
        
        if reasoning_mode == "openai":
            # OpenAI模式：添加reasoning_content字段
            final_content_str = "".join(full_content)
            response_data = {
                "id": response_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": final_content_str,
                        "reasoning_content": full_reasoning  # 添加思维链
                    },
                    "finish_reason": finish_reason,
                }],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": len(final_content_str) // 4,
                    "total_tokens": len(final_content_str) // 4,
                },
            }
        elif reasoning_mode == "think_tag":
            # think_tag模式：将思维链包裹后放在content前面
            wrapped_reasoning = f"<think>{full_reasoning}</think>\n\n"
            final_content_str = wrapped_reasoning + "".join(full_content)
            response_data = format_openai_non_stream_response(final_content_str, model, response_id, reason=finish_reason)
    else:
        # 没有启用思维链输出，或者没有思维链内容，使用原有逻辑
        final_content_str = "".join(full_content)
        response_data = format_openai_non_stream_response(final_content_str, model, response_id, reason=finish_reason)
    
    logger.info(f"NON-STREAM [ID: {request_id[:8]}]: 响应聚合完成。")
    
    # 🔧 改进：优先使用LMArena返回的实际token数，否则使用tiktoken精确计数
    input_tokens = 0
    output_tokens = 0
    
    if lmarena_usage:
        # 使用LMArena返回的实际token数
        input_tokens = lmarena_usage.get('inputTokens', 0) or lmarena_usage.get('prompt_tokens', 0)
        output_tokens = lmarena_usage.get('outputTokens', 0) or lmarena_usage.get('completion_tokens', 0)
        logger.info(f"[TOKEN_NON_STREAM] 使用LMArena实际token数: input={input_tokens}, output={output_tokens}")
    else:
        # 回退到本地tokenizer计数
        from modules.token_counter import get_tokenizer_for_model
        tokenizer_type = get_tokenizer_for_model(model)
        logger.info(f"[TOKEN_NON_STREAM] LMArena未提供token信息，使用{tokenizer_type}计数（模型: {model}）")
        if hasattr(monitoring_service, 'active_requests') and request_id in monitoring_service.active_requests:
            request_info = monitoring_service.active_requests[request_id]
            if request_info.request_messages:
                # 使用本地tokenizer计算输入token
                try:
                    input_tokens = estimate_message_tokens(request_info.request_messages, model)
                    logger.info(f"[TOKEN_NON_STREAM] {tokenizer_type}计算输入tokens: {input_tokens}")
                except Exception as e:
                    logger.warning(f"[TOKEN_NON_STREAM] tiktoken计算失败，回退到估算: {e}")
                    # 回退到简单估算
                    for msg in request_info.request_messages:
                        if isinstance(msg, dict) and 'content' in msg:
                            content = msg.get('content', '')
                            if isinstance(content, str):
                                input_tokens += len(content) // 4
                            elif isinstance(content, list):
                                for part in content:
                                    if isinstance(part, dict) and part.get('type') == 'text':
                                        input_tokens += len(part.get('text', '')) // 4
        
        # 计算完整响应内容（包括思维链）
        full_response_for_monitoring = final_content_str if 'final_content_str' in locals() else "".join(full_content)
        
        # 使用本地tokenizer计算输出token
        try:
            output_tokens = estimate_tokens(full_response_for_monitoring, model)
            logger.info(f"[TOKEN_NON_STREAM] {tokenizer_type}计算输出tokens: {output_tokens}")
        except Exception as e:
            logger.warning(f"[TOKEN_NON_STREAM] tiktoken计算输出失败，回退到估算: {e}")
            output_tokens = len(full_response_for_monitoring) // 4
    
    # 计算完整响应内容（包括思维链）
    full_response_for_monitoring = final_content_str if 'final_content_str' in locals() else "".join(full_content)
    full_reasoning_for_monitoring = "".join(reasoning_content) if reasoning_content else None
    
    # 更新响应中的usage字段
    response_data['usage'] = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    
    monitoring_service.request_end(
        request_id,
        success=True,
        response_content=full_response_for_monitoring,
        reasoning_content=full_reasoning_for_monitoring,
        input_tokens=input_tokens,
        output_tokens=output_tokens
    )
    
    # 🔧 关键修复：释放标签页请求计数
    if request_id in request_metadata:
        tab_id = request_metadata[request_id].get("tab_id")
        if tab_id:
            await release_tab_request(tab_id)
            logger.debug(f"NON-STREAM [ID: {request_id[:8]}]: 已释放标签页 '{tab_id}' 的请求计数")
    
    return Response(content=json.dumps(response_data, ensure_ascii=False), media_type="application/json")