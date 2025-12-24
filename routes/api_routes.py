"""
核心API路由
包含 /v1/models、/v1/chat/completions、/v1beta Gemini端点等核心API
"""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from typing import Optional, Tuple
from fastapi import Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, Response

logger = logging.getLogger(__name__)


async def get_models(MODEL_ENDPOINT_MAP: dict, MODEL_NAME_TO_ID_MAP: dict):
    """提供兼容 OpenAI 的模型列表 - 返回 model_endpoint_map.json 中配置的模型。"""
    # 优先返回 MODEL_ENDPOINT_MAP 中的模型（已配置会话的模型）
    if MODEL_ENDPOINT_MAP:
        return {
            "object": "list",
            "data": [
                {
                    "id": model_name,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "LMArenaBridge"
                }
                for model_name in MODEL_ENDPOINT_MAP.keys()
            ],
        }
    # 如果 MODEL_ENDPOINT_MAP 为空，则返回 models.json 中的模型作为备用
    elif MODEL_NAME_TO_ID_MAP:
        return {
            "object": "list",
            "data": [
                {
                    "id": model_name,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "LMArenaBridge"
                }
                for model_name in MODEL_NAME_TO_ID_MAP.keys()
            ],
        }
    else:
        return JSONResponse(
            status_code=404,
            content={"error": "模型列表为空。请配置 'model_endpoint_map.json' 或 'models.json'。"}
        )


async def get_gemini_models(MODEL_ENDPOINT_MAP: dict):
    """提供Gemini v1beta格式的模型列表"""
    # 只返回配置了gemini_native类型的模型
    gemini_models = []
    
    if MODEL_ENDPOINT_MAP:
        for model_name, config in MODEL_ENDPOINT_MAP.items():
            # 处理单个配置和配置列表
            configs_to_check = [config] if isinstance(config, dict) else config if isinstance(config, list) else []
            
            for cfg in configs_to_check:
                if isinstance(cfg, dict) and cfg.get("api_type") == "gemini_native":
                    # 使用model_id字段作为模型名称
                    model_id = cfg.get("model_id", model_name)
                    display_name = cfg.get("display_name", model_id)
                    
                    gemini_models.append({
                        "name": f"models/{model_id}",
                        "displayName": display_name,
                        "description": f"Gemini model: {display_name}",
                        "supportedGenerationMethods": [
                            "generateContent",
                            "streamGenerateContent"
                        ]
                    })
                    break  # 只添加一次
    
    logger.info(f"[GEMINI_V1BETA] 返回 {len(gemini_models)} 个Gemini原生模型")
    
    return {
        "models": gemini_models
    }


async def gemini_native_api(
    model_name: str,
    request: Request,
    MODEL_ENDPOINT_MAP: dict,
    monitoring_service,
    direct_api_service,
    last_activity_time_setter
):
    """
    处理Gemini原生API格式的请求
    支持 generateContent 和 streamGenerateContent
    """
    last_activity_time_setter(datetime.now())
    
    # 解码URL编码的模型名称
    from urllib.parse import unquote
    model_name = unquote(model_name)
    
    logger.info(f"[GEMINI_V1BETA] 收到请求: 模型={model_name}")
    
    # 检查是否为流式请求
    is_stream = request.url.path.endswith(":streamGenerateContent")
    query_params = dict(request.query_params)
    if query_params.get("alt") == "sse":
        is_stream = True
    
    try:
        # 解析Gemini原生格式的请求体
        gemini_req = await request.json()
        
        # 查找模型配置
        endpoint_config = MODEL_ENDPOINT_MAP.get(model_name)
        
        if not endpoint_config:
            raise HTTPException(
                status_code=404,
                detail=f"模型 '{model_name}' 未在配置中找到"
            )
        
        # 处理多端点情况
        if isinstance(endpoint_config, list) and endpoint_config:
            endpoint_config = endpoint_config[0]
        
        # 验证是否为gemini_native类型
        api_type = endpoint_config.get("api_type")
        if api_type != "gemini_native":
            raise HTTPException(
                status_code=400,
                detail=f"模型 '{model_name}' 不是Gemini原生API类型"
            )
        
        # 获取配置
        api_base_url = endpoint_config.get("api_base_url")
        api_key = endpoint_config.get("api_key")
        target_model_id = endpoint_config.get("model_id", model_name)
        display_name = endpoint_config.get("display_name", model_name)
        pricing_config = endpoint_config.get("pricing", {})
        
        if not api_key:
            raise HTTPException(
                status_code=500,
                detail=f"模型 '{model_name}' 缺少API密钥配置"
            )
        
        logger.info(f"[GEMINI_V1BETA] 转发到目标模型: {target_model_id}")
        logger.info(f"[GEMINI_V1BETA] 流式模式: {is_stream}")
        
        # 生成请求ID
        request_id = str(uuid.uuid4())
        
        # 记录请求开始
        monitoring_service.request_start(
            request_id=request_id,
            model=display_name,
            messages_count=len(gemini_req.get("contents", [])),
            session_id=None,
            mode="gemini_v1beta",
            messages=[],
            params={
                "streaming": is_stream
            }
        )
        
        # 广播请求开始
        await monitoring_service.broadcast_to_monitors({
            "type": "request_start",
            "request_id": request_id,
            "model": display_name,
            "timestamp": time.time()
        })
        
        # 直接转发到目标API
        try:
            # 构建目标URL
            if api_base_url:
                base_url = api_base_url.rstrip('/')
            else:
                base_url = "https://generativelanguage.googleapis.com/v1beta"
            
            method = "streamGenerateContent" if is_stream else "generateContent"
            target_url = f"{base_url}/models/{target_model_id}:{method}"
            
            # 添加API key到查询参数
            if "?" in target_url:
                target_url += f"&key={api_key}"
            else:
                target_url += f"?key={api_key}"
            
            # 如果是SSE流式请求，添加alt=sse参数
            if is_stream and query_params.get("alt") == "sse":
                target_url += "&alt=sse"
            
            logger.info(f"[GEMINI_V1BETA] 目标URL: {target_url.replace(api_key, '***')}")
            
            # 转发请求
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=600, connect=30, sock_read=300)
            
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    target_url,
                    json=gemini_req,
                    headers={
                        "Content-Type": "application/json"
                    }
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(f"[GEMINI_V1BETA] 上游API错误: {resp.status} - {error_text}")
                        monitoring_service.request_end(request_id=request_id, success=False, error=error_text)
                        return JSONResponse(
                            status_code=resp.status,
                            content={"error": error_text}
                        )
                    
                    if is_stream:
                        # 流式响应
                        accumulated_chunks = []
                        accumulated_text = ""
                        input_tokens = 0
                        output_tokens = 0
                        request_success = False
                        error_msg = None
                        
                        try:
                            # 读取并累积所有块
                            async for chunk in resp.content.iter_any():
                                if not chunk:
                                    continue
                                
                                accumulated_chunks.append(chunk)
                                
                                # 尝试解析token统计
                                try:
                                    chunk_str = chunk.decode('utf-8', errors='ignore')
                                    
                                    # 处理SSE格式
                                    if query_params.get("alt") == "sse":
                                        for line in chunk_str.split('\n'):
                                            line = line.strip()
                                            if line.startswith('data: '):
                                                data_str = line[6:]
                                                if data_str and data_str != '[DONE]':
                                                    try:
                                                        chunk_data = json.loads(data_str)
                                                        if 'candidates' in chunk_data:
                                                            for candidate in chunk_data['candidates']:
                                                                if 'content' in candidate and 'parts' in candidate['content']:
                                                                    for part in candidate['content']['parts']:
                                                                        if 'text' in part:
                                                                            accumulated_text += part['text']
                                                        
                                                        if 'usageMetadata' in chunk_data:
                                                            usage_meta = chunk_data['usageMetadata']
                                                            input_tokens = usage_meta.get('promptTokenCount', input_tokens)
                                                            output_tokens = usage_meta.get('candidatesTokenCount', output_tokens)
                                                    except json.JSONDecodeError:
                                                        pass
                                    else:
                                        # JSON流格式
                                        try:
                                            chunk_data = json.loads(chunk_str)
                                            if 'candidates' in chunk_data:
                                                for candidate in chunk_data['candidates']:
                                                    if 'content' in candidate and 'parts' in candidate['content']:
                                                        for part in candidate['content']['parts']:
                                                            if 'text' in part:
                                                                accumulated_text += part['text']
                                            
                                            if 'usageMetadata' in chunk_data:
                                                usage_meta = chunk_data['usageMetadata']
                                                input_tokens = usage_meta.get('promptTokenCount', input_tokens)
                                                output_tokens = usage_meta.get('candidatesTokenCount', output_tokens)
                                        except json.JSONDecodeError:
                                            pass
                                except Exception as parse_err:
                                    logger.debug(f"[GEMINI_V1BETA] 解析块失败: {parse_err}")
                            
                            request_success = True
                            
                        except Exception as e:
                            logger.error(f"[GEMINI_V1BETA] 流式处理错误: {e}", exc_info=True)
                            error_msg = str(e)
                        finally:
                            # 如果API没有返回usage，使用tokenizer计算
                            if output_tokens == 0 and accumulated_text:
                                try:
                                    from modules.token_counter import estimate_tokens
                                    output_tokens = estimate_tokens(accumulated_text, model=display_name)
                                    logger.info(f"[GEMINI_V1BETA] 使用tokenizer计算输出: {output_tokens} tokens")
                                except Exception as token_err:
                                    logger.warning(f"[GEMINI_V1BETA] Token计算失败: {token_err}")
                            
                            # 记录请求完成
                            monitoring_service.request_end(
                                request_id=request_id,
                                success=request_success,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                error=error_msg
                            )
                            
                            logger.info(f"[GEMINI_V1BETA] 流式请求完成: {request_id[:8]}")
                            logger.info(f"  - 输入tokens: {input_tokens}, 输出tokens: {output_tokens}")
                        
                        # 创建生成器返回累积的块
                        async def replay_chunks():
                            for chunk in accumulated_chunks:
                                yield chunk
                        
                        return StreamingResponse(
                            replay_chunks(),
                            media_type="text/event-stream" if query_params.get("alt") == "sse" else "application/json",
                            headers={
                                'Cache-Control': 'no-cache',
                                'Connection': 'keep-alive',
                                'X-Accel-Buffering': 'no'
                            }
                        )
                    else:
                        # 非流式响应
                        response_data = await resp.json()
                        
                        # 提取token统计
                        usage_metadata = response_data.get('usageMetadata', {})
                        input_tokens = usage_metadata.get('promptTokenCount', 0)
                        output_tokens = usage_metadata.get('candidatesTokenCount', 0)
                        
                        # 记录请求完成
                        monitoring_service.request_end(
                            request_id=request_id,
                            success=True,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens
                        )
                        
                        return JSONResponse(content=response_data)
                    
        except Exception as e:
            logger.error(f"[GEMINI_V1BETA] 请求处理失败: {e}", exc_info=True)
            monitoring_service.request_end(request_id=request_id, success=False, error=str(e))
            raise HTTPException(status_code=500, detail=str(e))
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[GEMINI_V1BETA] 请求解析失败: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


async def chat_completions(
    request: Request,
    CONFIG: dict,
    MODEL_ENDPOINT_MAP: dict,
    MODEL_NAME_TO_ID_MAP: dict,
    MODEL_ROUND_ROBIN_INDEX: dict,
    MODEL_ROUND_ROBIN_LOCK,
    last_activity_time_setter,
    VERIFICATION_COOLDOWN_UNTIL,
    IS_REFRESHING_FOR_VERIFICATION,
    browser_ws,
    browser_connections: dict,
    browser_connections_lock,
    tab_request_counts: dict,
    response_channels: dict,
    request_metadata: dict,
    pending_requests_queue,
    monitoring_service,
    direct_api_service,
    aiohttp_session,
    IMAGE_BASE64_CACHE: dict,
    IMAGE_CACHE_MAX_SIZE: int,
    IMAGE_CACHE_TTL: int,
    save_downloaded_image_async_func,
    download_image_data_with_retry_func,
    release_tab_request_func,
    select_best_tab_for_request_func,
    convert_openai_to_lmarena_payload_func,
    process_lmarena_stream_func,
    stream_generator_func,
    non_stream_response_func,
    format_openai_chunk_func,
    format_openai_finish_chunk_func,
    format_openai_error_chunk_func,
    format_openai_non_stream_response_func,
    estimate_message_tokens_func,
    estimate_tokens_func,
    process_image_data_func
):
    """
    处理聊天补全请求。
    接收 OpenAI 格式的请求，将其转换为 LMArena 格式，
    通过 WebSocket 发送给油猴脚本，然后流式返回结果。
    """
    last_activity_time_setter(datetime.now())
    logger.info(f"API请求已收到，活动时间已更新")

    # 检查人机验证冷却状态
    if VERIFICATION_COOLDOWN_UNTIL is not None:
        remaining = VERIFICATION_COOLDOWN_UNTIL - time.time()
        if remaining > 0:
            adjusted_remaining = max(0, int(remaining - 3))
            logger.warning(f"⏰ 请求被拒绝：人机验证冷却中（剩余 {int(remaining)} 秒）")
            raise HTTPException(
                status_code=503,
                detail=f"正在等待人机验证冷却完成...（剩余 {adjusted_remaining} 秒）"
            )

    try:
        openai_req = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="无效的 JSON 请求体")

    model_name = openai_req.get("model")
    
    # 优先从 MODEL_ENDPOINT_MAP 获取模型类型
    model_type = "text"
    endpoint_mapping = MODEL_ENDPOINT_MAP.get(model_name)
    if endpoint_mapping:
        if isinstance(endpoint_mapping, dict) and "type" in endpoint_mapping:
            model_type = endpoint_mapping.get("type", "text")
        elif isinstance(endpoint_mapping, list) and endpoint_mapping:
            first_mapping = endpoint_mapping[0] if isinstance(endpoint_mapping[0], dict) else {}
            if "type" in first_mapping:
                model_type = first_mapping.get("type", "text")
    
    # 回退到 models.json
    model_info = MODEL_NAME_TO_ID_MAP.get(model_name, {})
    if not (endpoint_mapping and (isinstance(endpoint_mapping, dict) and "type" in endpoint_mapping or
            isinstance(endpoint_mapping, list) and endpoint_mapping and "type" in endpoint_mapping[0])):
        model_type = model_info.get("type", "text")

    # 检测Direct API模式
    endpoint_config = MODEL_ENDPOINT_MAP.get(model_name) if model_name else None
    
    # 处理多端点情况
    if isinstance(endpoint_config, list) and endpoint_config:
        with MODEL_ROUND_ROBIN_LOCK:
            if model_name not in MODEL_ROUND_ROBIN_INDEX:
                MODEL_ROUND_ROBIN_INDEX[model_name] = 0
            current_index = MODEL_ROUND_ROBIN_INDEX[model_name]
            endpoint_config = endpoint_config[current_index]
            MODEL_ROUND_ROBIN_INDEX[model_name] = (current_index + 1) % len(MODEL_ENDPOINT_MAP[model_name])
            logger.info(f"[DIRECT_API] 多端点轮询: 模型'{model_name}' 选择端点#{current_index + 1}")
    
    # 如果是Direct API模式，跳过浏览器连接检查
    is_direct_api_mode = isinstance(endpoint_config, dict) and endpoint_config.get("api_type") in ["direct_api", "gemini_native"]
    
    # API Key 验证
    api_key = CONFIG.get("api_key")
    if api_key and not is_direct_api_mode:
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            raise HTTPException(
                status_code=401,
                detail="未提供 API Key。请在 Authorization 头部中以 'Bearer YOUR_KEY' 格式提供。"
            )
        
        provided_key = auth_header.split(' ')[1]
        if provided_key != api_key:
            raise HTTPException(
                status_code=401,
                detail="提供的 API Key 不正确。"
            )

    # 连接检查与自动重试逻辑（Direct API模式跳过）
    if not browser_ws and not is_direct_api_mode:
        if CONFIG.get("enable_auto_retry", False):
            logger.warning("油猴脚本未连接，但自动重试已启用。请求将被暂存。")
            
            future = asyncio.get_event_loop().create_future()
            
            await pending_requests_queue.put({
                "future": future,
                "request_data": openai_req
            })
            
            logger.info(f"一个新请求已被放入暂存队列。当前队列大小: {pending_requests_queue.qsize()}")

            try:
                timeout = CONFIG.get("retry_timeout_seconds", 120)
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"一个暂存的请求等待了 {timeout} 秒后超时。")
                raise HTTPException(
                    status_code=503,
                    detail=f"浏览器与服务器连接断开，并在 {timeout} 秒内未能恢复。请求失败。"
                )
        else:
            raise HTTPException(
                status_code=503,
                detail="油猴脚本客户端未连接。请确保 LMArena 页面已打开并激活脚本。"
            )

    if IS_REFRESHING_FOR_VERIFICATION and not browser_ws:
        raise HTTPException(
            status_code=503,
            detail="正在等待浏览器刷新以完成人机验证，请在几秒钟后重试。"
        )

    # Direct API模式处理
    if is_direct_api_mode:
        return await handle_direct_api_request(
            openai_req=openai_req,
            model_name=model_name,
            endpoint_config=endpoint_config,
            CONFIG=CONFIG,
            PROCESSED_IMAGE_CACHE=IMAGE_BASE64_CACHE,
            monitoring_service=monitoring_service,
            direct_api_service=direct_api_service,
            estimate_message_tokens_func=estimate_message_tokens_func,
            estimate_tokens_func=estimate_tokens_func,
            process_image_data_func=process_image_data_func
        )
    
    # LMArena模式处理
    return await handle_lmarena_request(
        openai_req=openai_req,
        model_name=model_name,
        model_type=model_type,
        CONFIG=CONFIG,
        MODEL_ENDPOINT_MAP=MODEL_ENDPOINT_MAP,
        MODEL_ROUND_ROBIN_INDEX=MODEL_ROUND_ROBIN_INDEX,
        MODEL_ROUND_ROBIN_LOCK=MODEL_ROUND_ROBIN_LOCK,
        browser_connections=browser_connections,
        browser_connections_lock=browser_connections_lock,
        tab_request_counts=tab_request_counts,
        response_channels=response_channels,
        request_metadata=request_metadata,
        monitoring_service=monitoring_service,
        aiohttp_session=aiohttp_session,
        IMAGE_BASE64_CACHE=IMAGE_BASE64_CACHE,
        IMAGE_CACHE_MAX_SIZE=IMAGE_CACHE_MAX_SIZE,
        IMAGE_CACHE_TTL=IMAGE_CACHE_TTL,
        save_downloaded_image_async_func=save_downloaded_image_async_func,
        download_image_data_with_retry_func=download_image_data_with_retry_func,
        release_tab_request_func=release_tab_request_func,
        select_best_tab_for_request_func=select_best_tab_for_request_func,
        convert_openai_to_lmarena_payload_func=convert_openai_to_lmarena_payload_func,
        process_lmarena_stream_func=process_lmarena_stream_func,
        stream_generator_func=stream_generator_func,
        non_stream_response_func=non_stream_response_func,
        format_openai_chunk_func=format_openai_chunk_func,
        format_openai_finish_chunk_func=format_openai_finish_chunk_func,
        format_openai_error_chunk_func=format_openai_error_chunk_func,
        format_openai_non_stream_response_func=format_openai_non_stream_response_func,
        estimate_message_tokens_func=estimate_message_tokens_func,
        estimate_tokens_func=estimate_tokens_func,
        process_image_data_func=process_image_data_func,
        IS_REFRESHING_FOR_VERIFICATION=IS_REFRESHING_FOR_VERIFICATION,
        VERIFICATION_COOLDOWN_UNTIL=VERIFICATION_COOLDOWN_UNTIL
    )


async def handle_direct_api_request(
    openai_req: dict,
    model_name: str,
    endpoint_config: dict,
    CONFIG: dict,
    PROCESSED_IMAGE_CACHE: dict,
    monitoring_service,
    direct_api_service,
    estimate_message_tokens_func,
    estimate_tokens_func,
    process_image_data_func
):
    """处理Direct API请求（Gemini Native或OpenAI兼容API）"""
    api_type = endpoint_config.get("api_type")
    logger.info(f"[DIRECT_API] 检测到Direct API模式: {model_name} (类型: {api_type})")
    
    # 获取配置
    api_base_url = endpoint_config.get("api_base_url")
    api_key = endpoint_config.get("api_key")
    target_model_id = endpoint_config.get("model_id", model_name)
    display_name = endpoint_config.get("display_name", model_name)
    passthrough_mode = endpoint_config.get("passthrough", False)
    use_native_format = endpoint_config.get("use_native_format", False)
    thinking_separator = endpoint_config.get("thinking_separator")
    pricing_config = endpoint_config.get("pricing", {})
    max_temperature = endpoint_config.get("max_temperature")
    
    # 获取模型级别图片压缩配置
    model_image_config = endpoint_config.get("image_compression")
    
    # 检查是否需要进行图片预处理
    global_optimization_enabled = CONFIG.get("image_optimization", {}).get("enabled", False)
    model_optimization_enabled = model_image_config.get("enabled", False) if model_image_config else False
    optimization_enabled = global_optimization_enabled or model_optimization_enabled
    
    if optimization_enabled:
        logger.info(f"[DIRECT_API] 开始图片预处理...")
        if model_image_config:
            logger.info(f"[DIRECT_API] 使用模型级别图片配置: {model_image_config}")
        
        request_id_for_img = str(uuid.uuid4())[:8]
        messages_to_process = openai_req.get("messages", [])
        image_processed_count = 0
        
        for msg_index, message in enumerate(messages_to_process):
            role = message.get("role", "unknown")
            content = message.get("content")
            
            # 处理字符串内容中的Markdown base64图片
            if isinstance(content, str):
                import re
                markdown_image_pattern = r'!\[([^\]]*)\]\((data:[^)]+)\)'
                markdown_matches = re.findall(markdown_image_pattern, content)
                
                for match_index, (alt_text, base64_url) in enumerate(markdown_matches):
                    processed_data, proc_error = await process_image_data_func(
                        base64_data=base64_url,
                        filename=f"direct_{role}_{msg_index}_{match_index}_{uuid.uuid4()}.png",
                        request_id=request_id_for_img,
                        CONFIG=CONFIG,
                        PROCESSED_IMAGE_CACHE=PROCESSED_IMAGE_CACHE,
                        model_image_config=model_image_config
                    )
                    
                    if proc_error:
                        logger.warning(f"[DIRECT_API] 图片处理警告: {proc_error}")
                    
                    old_markdown = f"![{alt_text}]({base64_url})"
                    new_markdown = f"![{alt_text}]({processed_data})"
                    content = content.replace(old_markdown, new_markdown)
                    message["content"] = content
                    image_processed_count += 1
            
            # 处理列表内容（OpenAI vision格式）
            elif isinstance(content, list):
                for part_index, part in enumerate(content):
                    if part.get("type") == "image_url":
                        url_content = part.get("image_url", {}).get("url")
                        
                        if url_content and url_content.startswith("data:"):
                            processed_data, proc_error = await process_image_data_func(
                                base64_data=url_content,
                                filename=f"direct_{role}_{msg_index}_{part_index}_{uuid.uuid4()}.png",
                                request_id=request_id_for_img,
                                CONFIG=CONFIG,
                                PROCESSED_IMAGE_CACHE=PROCESSED_IMAGE_CACHE,
                                model_image_config=model_image_config
                            )
                            
                            if proc_error:
                                logger.warning(f"[DIRECT_API] 图片处理警告: {proc_error}")
                            
                            part["image_url"]["url"] = processed_data
                            image_processed_count += 1
        
        if image_processed_count > 0:
            logger.info(f"[DIRECT_API] 图片预处理完成: 处理了 {image_processed_count} 张图片")
    
    # 应用温度限制
    if max_temperature is not None and "temperature" in openai_req:
        original_temp = openai_req["temperature"]
        if original_temp > max_temperature:
            openai_req["temperature"] = max_temperature
            logger.info(f"[TEMP_LIMIT] 模型 '{model_name}' 温度限制: {original_temp} -> {max_temperature}")
    
    # 验证必需配置
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail=f"模型 '{model_name}' 的Direct API配置缺少 api_key。"
        )
    
    # OpenAI兼容格式需要api_base_url
    if api_type == "direct_api" and not api_base_url:
        raise HTTPException(
            status_code=500,
            detail=f"模型 '{model_name}' 的Direct API配置缺少 api_base_url。"
        )
    
    logger.info(f"[DIRECT_API] 配置信息:")
    logger.info(f"  - API类型: {api_type}")
    logger.info(f"  - 基础URL: {api_base_url if api_base_url else '(使用默认)'}")
    logger.info(f"  - 目标模型ID: {target_model_id}")
    logger.info(f"  - 显示名称: {display_name}")
    logger.info(f"  - 透传模式: {passthrough_mode}")
    logger.info(f"  - 计费配置: {pricing_config}")
    
    # Gemini原生格式处理
    if api_type == "gemini_native" or use_native_format:
        return await handle_gemini_native_direct(
            openai_req=openai_req,
            model_name=model_name,
            target_model_id=target_model_id,
            display_name=display_name,
            api_key=api_key,
            api_base_url=api_base_url,
            endpoint_config=endpoint_config,
            pricing_config=pricing_config,
            monitoring_service=monitoring_service,
            direct_api_service=direct_api_service,
            estimate_message_tokens_func=estimate_message_tokens_func,
            estimate_tokens_func=estimate_tokens_func
        )
    
    # 透传模式
    elif passthrough_mode:
        return await handle_passthrough_direct(
            openai_req=openai_req,
            model_name=model_name,
            target_model_id=target_model_id,
            display_name=display_name,
            api_base_url=api_base_url,
            api_key=api_key,
            endpoint_config=endpoint_config,
            pricing_config=pricing_config,
            thinking_separator=thinking_separator,
            monitoring_service=monitoring_service,
            direct_api_service=direct_api_service,
            estimate_message_tokens_func=estimate_message_tokens_func,
            estimate_tokens_func=estimate_tokens_func
        )
    
    # 转换模式
    else:
        logger.info(f"[DIRECT_API] 使用转换模式（暂未实现完整转换逻辑）")
        raise HTTPException(
            status_code=501,
            detail="Direct API转换模式尚未完全实现。请使用 passthrough: true 启用透传模式。"
        )


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
    estimate_tokens_func
):
    """处理Gemini原生API的Direct请求"""
    logger.info(f"[GEMINI_NATIVE] 使用Gemini原生API格式")
    
    # 生成请求ID
    request_id = str(uuid.uuid4())
    
    # 记录请求开始
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
    
    # 广播请求开始
    await monitoring_service.broadcast_to_monitors({
        "type": "request_start",
        "request_id": request_id,
        "model": display_name,
        "timestamp": time.time()
    })
    
    try:
        is_stream = openai_req.get("stream", False)
        
        logger.info(f"[GEMINI_NATIVE] 模型映射: '{model_name}' -> '{target_model_id}'")
        
        # 准备额外参数
        extra_kwargs = {}
        custom_params = endpoint_config.get("custom_params", {})
        if custom_params and isinstance(custom_params, dict):
            extra_kwargs.update(custom_params)
            logger.info(f"[GEMINI_NATIVE] 已添加自定义参数:")
            for key, value in custom_params.items():
                logger.info(f"  - {key}: {value}")
        
        # 调用Gemini原生API
        gemini_generator = direct_api_service.call_gemini_native_api(
            api_key=api_key,
            model=target_model_id,
            messages=openai_req.get("messages", []),
            stream=is_stream,
            temperature=openai_req.get("temperature"),
            top_p=openai_req.get("top_p"),
            max_tokens=openai_req.get("max_tokens"),
            base_url=api_base_url,
            **extra_kwargs
        )
        
        if is_stream:
            # 流式响应
            async def gemini_stream_generator():
                accumulated_content = ""
                input_tokens = 0
                output_tokens = 0
                request_success = False
                error_msg = None
                
                try:
                    async for gemini_chunk in gemini_generator:
                        # 检查错误
                        if "error" in gemini_chunk:
                            error_msg = str(gemini_chunk.get("error"))
                            openai_error = {"error": gemini_chunk["error"]}
                            yield f"data: {json.dumps(openai_error, ensure_ascii=False)}\n\n"
                            break
                        
                        # 转换为OpenAI格式
                        openai_chunk = direct_api_service.convert_gemini_response_to_openai(
                            gemini_chunk, display_name, request_id, is_stream_chunk=True
                        )
                        
                        # 累积内容
                        delta_content = openai_chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if delta_content:
                            accumulated_content += delta_content
                        
                        # 提取usage信息
                        if "usage" in openai_chunk and openai_chunk["usage"]:
                            usage = openai_chunk["usage"]
                            input_tokens = usage.get("prompt_tokens", 0)
                            output_tokens = usage.get("completion_tokens", 0)
                        
                        # 发送SSE格式数据
                        yield f"data: {json.dumps(openai_chunk, ensure_ascii=False)}\n\n"
                    
                    # 发送结束标记
                    yield "data: [DONE]\n\n"
                    request_success = True
                    
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"[GEMINI_NATIVE] 流式处理失败: {e}", exc_info=True)
                finally:
                    # 如果没有usage，使用tokenizer计算
                    if input_tokens == 0 or output_tokens == 0:
                        try:
                            if input_tokens == 0:
                                input_tokens = estimate_message_tokens_func(
                                    openai_req.get('messages', []),
                                    model=display_name
                                )
                            if output_tokens == 0 and accumulated_content:
                                output_tokens = estimate_tokens_func(
                                    accumulated_content,
                                    model=display_name
                                )
                        except Exception as token_error:
                            logger.error(f"[GEMINI_NATIVE] Token计算失败: {token_error}")
                    
                    # 计算成本
                    cost_info = direct_api_service.calculate_cost(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        pricing=pricing_config
                    ) if pricing_config else {}
                    
                    # 记录请求结束
                    monitoring_service.request_end(
                        request_id=request_id,
                        success=request_success,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        error=error_msg,
                        cost_info=cost_info
                    )
                    
                    await monitoring_service.broadcast_to_monitors({
                        "type": "request_end",
                        "request_id": request_id,
                        "success": request_success
                    })
                    
                    logger.info(f"[GEMINI_NATIVE] 流式请求完成: {request_id[:8]}")
                    logger.info(f"  - 输入tokens: {input_tokens}, 输出tokens: {output_tokens}")
                    if cost_info.get("total_cost"):
                        logger.info(f"  - 总成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")
            
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
            
            # 检查错误
            if "error" in gemini_response:
                error_msg = str(gemini_response.get("error"))
                monitoring_service.request_end(request_id=request_id, success=False, error=error_msg)
                await monitoring_service.broadcast_to_monitors({
                    "type": "request_end",
                    "request_id": request_id,
                    "success": False
                })
                return JSONResponse(status_code=500, content=gemini_response)
            
            # 转换为OpenAI格式
            openai_response = direct_api_service.convert_gemini_response_to_openai(
                gemini_response, display_name, request_id, is_stream_chunk=False
            )
            
            # 提取token统计
            usage = openai_response.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
            
            # 如果没有usage，使用tokenizer计算
            if input_tokens == 0 or output_tokens == 0:
                try:
                    if input_tokens == 0:
                        input_tokens = estimate_message_tokens_func(
                            openai_req.get('messages', []),
                            model=display_name
                        )
                    if output_tokens == 0:
                        content = openai_response.get("choices", [{}])[0].get("message", {}).get("content", "")
                        output_tokens = estimate_tokens_func(content, model=display_name)
                except Exception as token_error:
                    logger.error(f"[GEMINI_NATIVE] Token计算失败: {token_error}")
            
            # 计算成本
            cost_info = direct_api_service.calculate_cost(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                pricing=pricing_config
            ) if pricing_config else {}
            
            # 记录请求结束
            monitoring_service.request_end(
                request_id=request_id,
                success=True,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_info=cost_info
            )
            
            await monitoring_service.broadcast_to_monitors({
                "type": "request_end",
                "request_id": request_id,
                "success": True
            })
            
            logger.info(f"[GEMINI_NATIVE] 非流式请求完成: {request_id[:8]}")
            logger.info(f"  - 输入tokens: {input_tokens}, 输出tokens: {output_tokens}")
            if cost_info.get("total_cost"):
                logger.info(f"  - 总成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")
            
            return JSONResponse(content=openai_response)
            
    except Exception as e:
        logger.error(f"[GEMINI_NATIVE] 请求处理失败: {e}", exc_info=True)
        monitoring_service.request_end(request_id=request_id, success=False, error=str(e))
        await monitoring_service.broadcast_to_monitors({
            "type": "request_end",
            "request_id": request_id,
            "success": False
        })
        raise HTTPException(status_code=500, detail=f"Gemini Native API调用失败: {str(e)}")


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
    estimate_tokens_func
):
    """处理透传模式的Direct API请求"""
    logger.info(f"[DIRECT_API_PASSTHROUGH] 启用透传模式")
    
    # 生成请求ID
    request_id = str(uuid.uuid4())
    
    # 记录请求开始
    monitoring_service.request_start(
        request_id=request_id,
        model=display_name,
        messages_count=len(openai_req.get("messages", [])),
        session_id=None,
        mode="direct_api_passthrough",
        messages=openai_req.get("messages", []),
        params={
            "temperature": openai_req.get("temperature"),
            "top_p": openai_req.get("top_p"),
            "max_tokens": openai_req.get("max_tokens"),
            "streaming": openai_req.get("stream", False)
        }
    )
    
    # 广播请求开始
    await monitoring_service.broadcast_to_monitors({
        "type": "request_start",
        "request_id": request_id,
        "model": display_name,
        "timestamp": time.time()
    })
    
    try:
        # 修改请求体中的model字段为目标模型ID
        passthrough_request = openai_req.copy()
        passthrough_request["model"] = target_model_id
        
        # 合并自定义参数
        custom_params = endpoint_config.get("custom_params", {})
        if custom_params and isinstance(custom_params, dict):
            passthrough_request.update(custom_params)
            logger.info(f"[DIRECT_API_CUSTOM] 已添加自定义参数:")
            for key, value in custom_params.items():
                logger.info(f"  - {key}: {value}")
        
        # DeepSeek Prefix模式支持
        enable_prefix = endpoint_config.get("enable_prefix", False)
        if enable_prefix and "messages" in passthrough_request:
            messages = passthrough_request["messages"]
            if messages and isinstance(messages, list) and len(messages) > 0:
                last_message = messages[-1]
                if isinstance(last_message, dict) and last_message.get("role") == "assistant":
                    last_message["prefix"] = True
                    logger.info(f"[DIRECT_API_PREFIX] 已为最后一条assistant消息启用prefix模式")
        
        # Gemini Thinking模式支持
        enable_thinking = endpoint_config.get("enable_thinking", True)
        if enable_thinking:
            passthrough_request["thinkingConfig"] = {
                "thinkingBudget": endpoint_config.get("thinking_budget", 20000)
            }
            logger.info(f"[DIRECT_API_THINKING] 已启用Gemini思维链模式")
            logger.info(f"  - 思维预算: {passthrough_request['thinkingConfig']['thinkingBudget']} tokens")
        
        is_stream = openai_req.get("stream", False)
        
        if is_stream:
            # 流式透传 - 预读第一个块来决定返回类型
            api_iterator = direct_api_service.call_api_passthrough(
                base_url=api_base_url,
                api_key=api_key,
                request_body=passthrough_request
            )

            # 预读第一个数据块以检查错误
            try:
                first_chunk_bytes = await asyncio.wait_for(anext(api_iterator), timeout=180)
            except (StopAsyncIteration, asyncio.TimeoutError):
                error_msg = "上游API返回空响应或在180秒内未返回第一个数据块"
                logger.error(f"[DIRECT_API_PASSTHROUGH] {error_msg}")
                monitoring_service.request_end(request_id=request_id, success=False, error=error_msg)
                await monitoring_service.broadcast_to_monitors({"type": "request_end", "request_id": request_id, "success": False})
                raise HTTPException(status_code=502, detail=error_msg)
            
            # 检查第一个块是否为JSON错误
            is_error = False
            error_json = None
            try:
                decoded_chunk = first_chunk_bytes.decode('utf-8')
                error_json = json.loads(decoded_chunk)
                if 'error' in error_json:
                    is_error = True
            except (json.JSONDecodeError, UnicodeDecodeError):
                is_error = False
                
            # 如果是错误，立即返回JSONResponse
            if is_error:
                error_details = error_json.get('error', {})
                error_type = error_details.get('type')
                
                # 根据OAI错误类型映射HTTP状态码
                status_code = 500
                if error_type == 'invalid_request_error':
                    status_code = 400
                elif error_type == 'authentication_error':
                    status_code = 401
                elif error_type == 'permission_error':
                    status_code = 403
                
                error_message = str(error_details)
                logger.error(f"[DIRECT_API_PASSTHROUGH] 请求失败，上游返回错误: {error_json}")
                monitoring_service.request_end(request_id=request_id, success=False, error=error_message, cost_info=None)
                await monitoring_service.broadcast_to_monitors({"type": "request_end", "request_id": request_id, "success": False})
                return JSONResponse(status_code=status_code, content=error_json)

            # 如果不是错误，构建新的生成器来组合第一个块和剩余流
            async def combined_stream_generator():
                """组合流，并在结束后执行清理和日志记录"""
                request_success = False
                error_msg = None
                accumulated_content = ""
                accumulated_reasoning = ""  # 收集思考内容
                accumulated_buffer = ""
                input_tokens = 0
                output_tokens = 0
                total_tokens = 0
                reasoning_tokens = 0
                separator_found = False
                
                try:
                    # 🔧 核心修复：用于跨chunk分隔符检测
                    accumulated_for_split = ""  # 累积所有内容用于分隔符检测
                    output_position = 0  # 已经输出的位置（用于避免重复输出）
                    sep_len = len(thinking_separator) if thinking_separator else 0
                    split_done = False  # 标记分隔是否已完成
                    
                    # 定义处理SSE块的函数（支持跨chunk分隔符检测）
                    def process_sse_chunk(chunk_bytes):
                        nonlocal separator_found, accumulated_for_split, output_position, split_done
                        
                        # 如果没有配置思考分隔符，直接返回原始数据
                        if not thinking_separator:
                            return chunk_bytes
                        
                        # 🔧 关键修复：如果分隔已完成，直接返回原始数据
                        if split_done:
                            return chunk_bytes
                        
                        try:
                            chunk_str = chunk_bytes.decode('utf-8')
                            lines = chunk_str.split('\n')
                            result_lines = []
                            
                            for line in lines:
                                # 在循环内也检查split_done状态
                                if split_done:
                                    result_lines.append(line)
                                    continue
                                    
                                if line.startswith('data: ') and line[6:].strip() not in ['', '[DONE]']:
                                    try:
                                        data_content = line[6:]
                                        chunk_json = json.loads(data_content)
                                        
                                        if 'choices' in chunk_json and len(chunk_json['choices']) > 0:
                                            delta = chunk_json['choices'][0].get('delta', {})
                                            content = delta.get('content', '')
                                            
                                            if content:
                                                # 累积内容
                                                accumulated_for_split += content
                                                
                                                # 检查累积的内容是否包含完整分隔符
                                                if thinking_separator in accumulated_for_split:
                                                    separator_found = True
                                                    split_done = True  # 标记分隔完成
                                                    
                                                    parts = accumulated_for_split.split(thinking_separator, 1)
                                                    full_reasoning = parts[0]
                                                    content_part = parts[1] if len(parts) > 1 else ""
                                                    
                                                    # 🔧 核心修复：只输出还没输出过的 reasoning 部分
                                                    remaining_reasoning = full_reasoning[output_position:]
                                                    
                                                    new_delta = {}
                                                    if remaining_reasoning:
                                                        new_delta['reasoning_content'] = remaining_reasoning
                                                    if content_part:
                                                        new_delta['content'] = content_part
                                                    
                                                    # 如果两者都为空，跳过
                                                    if not new_delta:
                                                        continue
                                                    
                                                    chunk_json['choices'][0]['delta'] = new_delta
                                                    modified_data = json.dumps(chunk_json, ensure_ascii=False)
                                                    result_lines.append(f'data: {modified_data}')
                                                    
                                                    logger.info(f"[THINKING_SPLIT_STREAM] ✅ 检测到分隔符'{thinking_separator}'")
                                                    logger.info(f"  - 思考总长: {len(full_reasoning)} 字符")
                                                    logger.info(f"  - 本次输出reasoning: {len(remaining_reasoning)} 字符 (已输出: {output_position})")
                                                    logger.info(f"  - 正文部分: {len(content_part)} 字符")
                                                    continue
                                                else:
                                                    # 还没找到分隔符
                                                    # 计算安全可输出的位置（末尾可能是分隔符的开始部分）
                                                    safe_position = max(output_position, len(accumulated_for_split) - sep_len)
                                                    safe_content = accumulated_for_split[output_position:safe_position]
                                                    
                                                    if safe_content:
                                                        # 输出安全的内容
                                                        delta['reasoning_content'] = safe_content
                                                        delta.pop('content', None)
                                                        output_position = safe_position  # 更新已输出位置
                                                        modified_data = json.dumps(chunk_json, ensure_ascii=False)
                                                        result_lines.append(f'data: {modified_data}')
                                                    # 如果没有安全可输出的内容，跳过这个chunk（等待更多数据）
                                                    continue
                                    except json.JSONDecodeError:
                                        pass
                                
                                result_lines.append(line)
                            
                            return '\n'.join(result_lines).encode('utf-8')
                        except Exception as e:
                            logger.warning(f"[THINKING_SPLIT_STREAM] 处理块失败: {e}")
                            return chunk_bytes
                    
                    # 处理第一个块
                    processed_first = process_sse_chunk(first_chunk_bytes)
                    yield processed_first
                    
                    # 解析第一个块以提取内容
                    try:
                        decoded_first = first_chunk_bytes.decode('utf-8')
                        for line in decoded_first.split('\n'):
                            if line.startswith('data: '):
                                data_content = line[6:].strip()
                                if data_content and data_content != '[DONE]':
                                    try:
                                        chunk_json = json.loads(data_content)
                                        if 'choices' in chunk_json and len(chunk_json['choices']) > 0:
                                            delta = chunk_json['choices'][0].get('delta', {})
                                            content = delta.get('content', '')
                                            if content:
                                                accumulated_content += content
                                    except json.JSONDecodeError:
                                        pass
                    except Exception as e:
                        logger.debug(f"[DIRECT_API_PASSTHROUGH] 解析第一个块内容失败: {e}")
                    
                    # 继续处理剩余的流
                    async for chunk_bytes in api_iterator:
                        processed_chunk = process_sse_chunk(chunk_bytes)
                        yield processed_chunk
                        
                        # 解析每个块以提取内容和usage信息
                        try:
                            decoded_chunk = chunk_bytes.decode('utf-8')
                            for line in decoded_chunk.split('\n'):
                                if line.startswith('data: '):
                                    data_content = line[6:].strip()
                                    if data_content and data_content != '[DONE]':
                                        try:
                                            chunk_json = json.loads(data_content)
                                            
                                            # 提取内容
                                            if 'choices' in chunk_json and len(chunk_json['choices']) > 0:
                                                delta = chunk_json['choices'][0].get('delta', {})
                                                content = delta.get('content', '')
                                                reasoning = delta.get('reasoning_content', '')
                                                if content:
                                                    accumulated_content += content
                                                if reasoning:
                                                    accumulated_reasoning += reasoning  # 分开收集思考内容
                                            
                                            # 检查usage信息
                                            if 'usage' in chunk_json:
                                                usage = chunk_json['usage']
                                                input_tokens = usage.get('prompt_tokens', 0)
                                                output_tokens = usage.get('completion_tokens', 0)
                                                total_tokens = usage.get('total_tokens', 0)
                                                reasoning_tokens = usage.get('reasoning_tokens', 0)
                                        except json.JSONDecodeError:
                                            pass
                        except Exception as e:
                            logger.debug(f"[DIRECT_API_PASSTHROUGH] 解析块失败: {e}")
                    
                    request_success = True
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"[DIRECT_API_PASSTHROUGH] 流式处理中发生异常: {e}", exc_info=True)
                finally:
                    # 应用思考内容分隔符（如果有配置）
                    final_reasoning = accumulated_reasoning  # 优先使用API返回的reasoning_content
                    final_content = accumulated_content
                    
                    if thinking_separator and accumulated_content and not accumulated_reasoning:
                        # 只有在没有API返回的reasoning_content时，才尝试用分隔符分割
                        reasoning_part, main_part = direct_api_service.split_thinking_content(
                            accumulated_content, thinking_separator
                        )
                        if reasoning_part:
                            final_reasoning = reasoning_part
                            final_content = main_part
                            logger.info(f"[THINKING_SPLIT] 检测到思考内容分隔符，分离出 {len(reasoning_part)} 字符的思考内容")
                    
                    # 如果API没有返回usage，使用tokenizer计算
                    if input_tokens == 0 or output_tokens == 0:
                        logger.warning(f"[DIRECT_API_PASSTHROUGH] API未返回完整usage信息，使用tokenizer计算")
                        try:
                            if input_tokens == 0:
                                input_tokens = estimate_message_tokens_func(
                                    openai_req.get('messages', []),
                                    model=display_name
                                )
                            
                            if output_tokens == 0 and accumulated_content:
                                output_tokens = estimate_tokens_func(
                                    accumulated_content,
                                    model=display_name
                                )
                            
                            total_tokens = input_tokens + output_tokens
                            logger.info(f"[DIRECT_API_PASSTHROUGH] Tokenizer计算: 输入={input_tokens}, 输出={output_tokens}")
                        except Exception as token_error:
                            logger.error(f"[DIRECT_API_PASSTHROUGH] Token计算失败: {token_error}")
                            if input_tokens == 0:
                                input_tokens = sum(len(str(m.get('content', ''))) for m in openai_req.get('messages', [])) // 4
                            if output_tokens == 0:
                                output_tokens = len(accumulated_content) // 4
                            total_tokens = input_tokens + output_tokens
                    
                    # 计算成本
                    cost_info = direct_api_service.calculate_cost(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        pricing=pricing_config
                    ) if pricing_config else {}
                    
                    # 记录请求结束（包含响应内容和思考内容）
                    monitoring_service.request_end(
                        request_id=request_id,
                        success=request_success,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        error=error_msg,
                        response_content=final_content[:2000] if final_content else None,  # 限制长度
                        reasoning_content=final_reasoning[:5000] if final_reasoning else None,  # 思考内容可以更长
                        cost_info=cost_info
                    )
                    await monitoring_service.broadcast_to_monitors({
                        "type": "request_end", "request_id": request_id, "success": request_success
                    })
                    
                    logger.info(f"[DIRECT_API_PASSTHROUGH] 流式请求完成: {request_id[:8]}, 成功: {request_success}")
                    if final_reasoning:
                        logger.info(f"  - 思考内容: {len(final_reasoning)} 字符")
                    if input_tokens > 0 or output_tokens > 0:
                        usage_parts = [f"输入={input_tokens}", f"输出={output_tokens}"]
                        if reasoning_tokens > 0:
                            usage_parts.append(f"思考={reasoning_tokens}")
                        usage_parts.append(f"总计={total_tokens}")
                        logger.info(f"[DIRECT_API_PASSTHROUGH] Token统计: {', '.join(usage_parts)}")
                    if cost_info.get("total_cost"):
                        logger.info(f"[DIRECT_API_PASSTHROUGH] 总成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")

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
        else:
            # 非流式透传
            try:
                response_bytes = b""
                async for chunk in direct_api_service.call_api_passthrough(
                    base_url=api_base_url,
                    api_key=api_key,
                    request_body=passthrough_request
                ):
                    response_bytes += chunk
                
                # 解析响应以提取统计信息
                response_json = json.loads(response_bytes.decode('utf-8'))

                # 检查响应是否为错误
                if 'error' in response_json:
                    error_details = response_json.get('error', {})
                    error_type = error_details.get('type')

                    status_code = 500
                    if error_type == 'invalid_request_error':
                        status_code = 400
                    elif error_type == 'authentication_error':
                        status_code = 401
                    elif error_type == 'permission_error':
                        status_code = 403

                    error_message = str(error_details)
                    
                    logger.error(f"[DIRECT_API_PASSTHROUGH] 非流式请求失败: {status_code} - {error_message}")
                    monitoring_service.request_end(
                        request_id=request_id,
                        success=False,
                        error=error_message,
                        input_tokens=0,
                        output_tokens=0,
                        cost_info=None
                    )
                    await monitoring_service.broadcast_to_monitors({
                        "type": "request_end",
                        "request_id": request_id,
                        "success": False
                    })
                    return JSONResponse(status_code=status_code, content=response_json)
                
                # 改进的token提取逻辑
                usage = response_json.get("usage", {})
                if usage:
                    input_tokens = usage.get("prompt_tokens", 0)
                    output_tokens = usage.get("completion_tokens", 0)
                    reasoning_tokens = usage.get("reasoning_tokens", 0)
                    total_tokens = usage.get("total_tokens", 0)
                    
                    if reasoning_tokens > 0:
                        logger.info(f"[DIRECT_API] 检测到思考token: {reasoning_tokens}")
                    
                    logger.info(f"[DIRECT_API] 使用API返回的token统计: 输入={input_tokens}, 输出={output_tokens}")
                else:
                    # 从响应中提取内容
                    content = ""
                    if "choices" in response_json and len(response_json["choices"]) > 0:
                        message = response_json["choices"][0].get("message", {})
                        content = message.get("content", "")
                    
                    # 回退：使用tokenizer计算
                    logger.warning(f"[DIRECT_API] API未返回usage，使用tokenizer计算")
                    try:
                        input_tokens = estimate_message_tokens_func(
                            openai_req.get('messages', []),
                            model=display_name
                        )
                        output_tokens = estimate_tokens_func(
                            content if content else "",
                            model=display_name
                        )
                        logger.info(f"[DIRECT_API] Tokenizer计算结果: 输入={input_tokens}, 输出={output_tokens}")
                    except Exception as token_error:
                        logger.warning(f"[DIRECT_API] Tokenizer计算失败: {token_error}，使用简单估算")
                        input_tokens = sum(len(str(m.get('content', ''))) for m in openai_req.get('messages', [])) // 4
                        output_tokens = len(content) // 4
                
                # 提取内容
                content = ""
                reasoning_content = ""
                if "choices" in response_json and len(response_json["choices"]) > 0:
                    message = response_json["choices"][0].get("message", {})
                    content = message.get("content", "")
                    reasoning_content = message.get("reasoning_content", "")
                    
                    # 应用思考内容分隔符
                    if thinking_separator and content and not reasoning_content:
                        reasoning_part, main_part = direct_api_service.split_thinking_content(
                            content, thinking_separator
                        )
                        if reasoning_part:
                            message["reasoning_content"] = reasoning_part
                            message["content"] = main_part
                            content = main_part
                            reasoning_content = reasoning_part
                            
                            logger.info(f"[THINKING_SPLIT] 非流式响应已应用思考分隔")
                
                # 计算成本
                cost_info = direct_api_service.calculate_cost(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    pricing=pricing_config
                ) if pricing_config else {}
                
                # 记录请求完成（包含响应内容和思考内容）
                monitoring_service.request_end(
                    request_id=request_id,
                    success=True,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    response_content=content[:2000] if content else None,
                    reasoning_content=reasoning_content[:5000] if reasoning_content else None,  # 添加思考内容
                    cost_info=cost_info
                )
                
                await monitoring_service.broadcast_to_monitors({
                    "type": "request_end",
                    "request_id": request_id,
                    "success": True
                })
                
                logger.info(f"[DIRECT_API_PASSTHROUGH] 非流式请求完成: {request_id[:8]}")
                logger.info(f"  - 输入tokens: {input_tokens}")
                logger.info(f"  - 输出tokens: {output_tokens}")
                if reasoning_content:
                    logger.info(f"  - 思考内容: {len(reasoning_content)} 字符")
                if cost_info.get("total_cost"):
                    logger.info(f"  - 总成本: {cost_info['total_cost']} {cost_info['currency']}")
                
                # 原样返回响应
                return Response(
                    content=response_bytes,
                    media_type="application/json"
                )
            
            except Exception as e:
                logger.error(f"[DIRECT_API_PASSTHROUGH] 非流式处理失败: {e}", exc_info=True)
                monitoring_service.request_end(request_id=request_id, success=False, error=str(e))
                await monitoring_service.broadcast_to_monitors({
                    "type": "request_end",
                    "request_id": request_id,
                    "success": False
                })
                raise
    
    except Exception as e:
        logger.error(f"[DIRECT_API_PASSTHROUGH] 请求处理失败: {e}", exc_info=True)
        monitoring_service.request_end(request_id=request_id, success=False, error=str(e))
        await monitoring_service.broadcast_to_monitors({
            "type": "request_end",
            "request_id": request_id,
            "success": False
        })
        raise HTTPException(status_code=500, detail=f"Direct API透传失败: {str(e)}")


async def handle_lmarena_request(
    openai_req: dict,
    model_name: str,
    model_type: str,
    CONFIG: dict,
    MODEL_ENDPOINT_MAP: dict,
    MODEL_ROUND_ROBIN_INDEX: dict,
    MODEL_ROUND_ROBIN_LOCK,
    browser_connections: dict,
    browser_connections_lock,
    tab_request_counts: dict,
    response_channels: dict,
    request_metadata: dict,
    monitoring_service,
    aiohttp_session,
    IMAGE_BASE64_CACHE: dict,
    IMAGE_CACHE_MAX_SIZE: int,
    IMAGE_CACHE_TTL: int,
    save_downloaded_image_async_func,
    download_image_data_with_retry_func,
    release_tab_request_func,
    select_best_tab_for_request_func,
    convert_openai_to_lmarena_payload_func,
    process_lmarena_stream_func,
    stream_generator_func,
    non_stream_response_func,
    format_openai_chunk_func,
    format_openai_finish_chunk_func,
    format_openai_error_chunk_func,
    format_openai_non_stream_response_func,
    estimate_message_tokens_func,
    estimate_tokens_func,
    process_image_data_func,
    IS_REFRESHING_FOR_VERIFICATION,
    VERIFICATION_COOLDOWN_UNTIL
):
    """处理LMArena模式的请求（通过WebSocket转发给油猴脚本）"""
    
    # 模型与会话ID映射逻辑
    session_id, message_id = None, None
    mode_override, battle_target_override = None, None
    selected_index_for_update = None
    max_temperature_config = None

    if model_name and model_name in MODEL_ENDPOINT_MAP:
        mapping_entry = MODEL_ENDPOINT_MAP[model_name]
        selected_mapping = None

        if isinstance(mapping_entry, list) and mapping_entry:
            # 轮询策略选择映射
            current_time_ms = int(time.time() * 1000)
            
            with MODEL_ROUND_ROBIN_LOCK:
                if model_name not in MODEL_ROUND_ROBIN_INDEX:
                    MODEL_ROUND_ROBIN_INDEX[model_name] = 0
                    logger.info(f"[ROUND_ROBIN_FIX] 🆕 模型 '{model_name}' 首次轮询，初始化索引为 0")
                
                current_index = MODEL_ROUND_ROBIN_INDEX[model_name]
                selected_mapping = mapping_entry[current_index]
                
                logger.info(f"[ROUND_ROBIN_FIX] ✅ 模型 '{model_name}' 轮询选择:")
                logger.info(f"  ⏰ 时间戳: {current_time_ms}")
                logger.info(f"  📊 总映射数: {len(mapping_entry)}")
                logger.info(f"  👉 当前选择索引: {current_index}")
                logger.info(f"  🎯 选择的映射: #{current_index + 1}/{len(mapping_entry)}")
                logger.info(f"  🔑 Session ID后8位: ...{selected_mapping.get('session_id', 'N/A')[-8:]}")
                logger.info(f"  ✅ 索引暂不更新，将在请求成功完成后更新")
                
                selected_index_for_update = current_index
            
            logger.info(f"✅ 为模型 '{model_name}' 从ID列表中轮询选择了映射 #{current_index + 1}/{len(mapping_entry)}（线程安全）")
        elif isinstance(mapping_entry, dict):
            selected_mapping = mapping_entry
            logger.info(f"为模型 '{model_name}' 找到了单个端点映射（旧格式）。")
        
        if selected_mapping:
            session_id = selected_mapping.get("session_id")
            mode_override = selected_mapping.get("mode")
            battle_target_override = selected_mapping.get("battle_target")
            max_temperature_config = selected_mapping.get("max_temperature")
            log_msg = f"将使用 Session ID: ...{session_id[-6:] if session_id else 'N/A'}"
            if mode_override:
                log_msg += f" (模式: {mode_override}"
                if mode_override == 'battle':
                    log_msg += f", 目标: {battle_target_override or 'A'}"
                log_msg += ")"
            logger.info(log_msg)

    # 全局回退逻辑
    if not session_id:
        if CONFIG.get("use_default_ids_if_mapping_not_found", True):
            session_id = CONFIG.get("session_id")
            mode_override, battle_target_override = None, None
            logger.info(f"模型 '{model_name}' 未找到有效映射，使用全局默认 Session ID: ...{session_id[-6:] if session_id else 'N/A'}")
        else:
            logger.error(f"模型 '{model_name}' 未在 'model_endpoint_map.json' 中找到有效映射，且已禁用回退到默认ID。")
            raise HTTPException(
                status_code=400,
                detail=f"模型 '{model_name}' 没有配置独立的会话ID。"
            )

    # 应用温度限制
    if max_temperature_config is not None and "temperature" in openai_req:
        original_temp = openai_req["temperature"]
        if original_temp > max_temperature_config:
            openai_req["temperature"] = max_temperature_config
            logger.info(f"[TEMP_LIMIT] 模型 '{model_name}' 温度限制: {original_temp} -> {max_temperature_config}")
    
    # 验证最终确定的会话信息
    if not session_id or "YOUR_" in session_id:
        raise HTTPException(
            status_code=400,
            detail="最终确定的 Session ID 无效。"
        )

    request_id = str(uuid.uuid4())
    response_channels[request_id] = asyncio.Queue()
    
    # 保存请求元数据
    request_metadata[request_id] = {
        "openai_request": openai_req.copy(),
        "model_name": model_name,
        "session_id": session_id,
        "mode_override": mode_override,
        "battle_target_override": battle_target_override,
        "created_at": datetime.now().isoformat(),
        "selected_index": selected_index_for_update,
        "mapping_list_length": len(MODEL_ENDPOINT_MAP.get(model_name, [])) if isinstance(MODEL_ENDPOINT_MAP.get(model_name), list) else None,
        "transfer_allowed": True,
        "original_tab_id": None,
        "transfer_count": 0,
        "last_transfer_time": None
    }
    
    logger.info(f"API CALL [ID: {request_id[:8]}]: 已创建响应通道。")
    
    # 记录请求开始
    monitoring_service.request_start(
        request_id=request_id,
        model=model_name or "unknown",
        messages_count=len(openai_req.get("messages", [])),
        session_id=session_id[-6:] if session_id else None,
        mode=mode_override or CONFIG.get("id_updater_last_mode", "direct_chat"),
        messages=openai_req.get("messages", []),
        params={
            "temperature": openai_req.get("temperature"),
            "top_p": openai_req.get("top_p"),
            "max_tokens": openai_req.get("max_tokens"),
            "streaming": openai_req.get("stream", False)
        }
    )
    
    # 广播请求开始
    await monitoring_service.broadcast_to_monitors({
        "type": "request_start",
        "request_id": request_id,
        "model": model_name,
        "timestamp": time.time()
    })

    try:
        # 获取模型级别的图片压缩配置
        model_image_config = None
        if model_name and model_name in MODEL_ENDPOINT_MAP:
            endpoint_entry = MODEL_ENDPOINT_MAP[model_name]
            # 处理单个配置或配置列表
            if isinstance(endpoint_entry, dict):
                model_image_config = endpoint_entry.get("image_compression")
            elif isinstance(endpoint_entry, list) and endpoint_entry:
                # 使用第一个配置的图片压缩设置（通常所有端点使用相同的图片配置）
                model_image_config = endpoint_entry[0].get("image_compression") if isinstance(endpoint_entry[0], dict) else None
        
        if model_image_config:
            logger.info(f"[IMG_CONFIG] 模型 '{model_name}' 使用自定义图片压缩配置: {model_image_config}")
        
        # 图片预处理
        file_bed_enabled = CONFIG.get("file_bed_enabled", False)
        global_optimization_enabled = CONFIG.get("image_optimization", {}).get("enabled", False)
        model_optimization_enabled = model_image_config.get("enabled", False) if model_image_config else False
        optimization_enabled = global_optimization_enabled or model_optimization_enabled
        
        if file_bed_enabled or optimization_enabled:
            messages_to_process = openai_req.get("messages", [])
            logger.info(f"📋 开始统一图片处理流程")
            logger.info(f"📋 准备处理 {len(messages_to_process)} 条消息中的图片")
            if model_image_config:
                logger.info(f"📋 使用模型级别配置: convert_png_to_jpg={model_image_config.get('convert_png_to_jpg')}, target_size_kb={model_image_config.get('target_size_kb')}")
                
            role_image_count = {}

            for msg_index, message in enumerate(messages_to_process):
                role = message.get("role", "unknown")
                content = message.get("content")
                
                # 处理字符串内容中的Markdown图片
                if isinstance(content, str):
                    import re
                    markdown_image_pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
                    markdown_matches = re.findall(markdown_image_pattern, content)
                    base64_matches = [(alt, url) for alt, url in markdown_matches if url.startswith('data:')]
                    
                    if base64_matches:
                        logger.info(f"  📷 发现 {len(base64_matches)} 个Markdown格式base64图片")
                        
                        for match_index, (alt_text, base64_url) in enumerate(base64_matches):
                            role_image_count[role] = role_image_count.get(role, 0) + 1
                            
                            processed_data, proc_error = await process_image_data_func(
                                base64_data=base64_url,
                                filename=f"{role}_string_{msg_index}_{match_index}_{uuid.uuid4()}.png",
                                request_id=request_id,
                                CONFIG=CONFIG,
                                PROCESSED_IMAGE_CACHE=IMAGE_BASE64_CACHE,
                                model_image_config=model_image_config  # 传递模型配置
                            )
                            
                            if proc_error:
                                logger.warning(f"    ⚠️ 图片处理出现警告: {proc_error}")
                            
                            old_markdown = f"![{alt_text}]({base64_url})"
                            new_markdown = f"![{alt_text}]({processed_data})"
                            content = content.replace(old_markdown, new_markdown)
                            message["content"] = content
                
                # 处理列表内容
                elif isinstance(content, list):
                    image_count_in_msg = 0
                    for part_index, part in enumerate(content):
                        if part.get("type") == "image_url":
                            url_content = part.get("image_url", {}).get("url")
                            
                            if url_content and url_content.startswith("data:"):
                                image_count_in_msg += 1
                                role_image_count[role] = role_image_count.get(role, 0) + 1
                                
                                processed_data, proc_error = await process_image_data_func(
                                    base64_data=url_content,
                                    filename=f"{role}_list_{msg_index}_{part_index}_{uuid.uuid4()}.png",
                                    request_id=request_id,
                                    CONFIG=CONFIG,
                                    PROCESSED_IMAGE_CACHE=IMAGE_BASE64_CACHE,
                                    model_image_config=model_image_config  # 传递模型配置
                                )
                                
                                if proc_error:
                                    logger.warning(f"    ⚠️ 图片处理出现警告: {proc_error}")
                                
                                part["image_url"]["url"] = processed_data

            if role_image_count:
                logger.info(f"✅ 图片处理完成。各角色图片统计：{role_image_count}")

        # 转换请求
        logger.info(f"[SEND_DEBUG] 开始转换OpenAI请求到LMArena格式...")
        lmarena_payload = await convert_openai_to_lmarena_payload_func(
            openai_req,
            session_id,
            mode_override=mode_override,
            battle_target_override=battle_target_override
        )
        logger.info(f"[SEND_DEBUG] ✅ 请求转换完成")
        
        battle_target = lmarena_payload.get("battle_target")
        
        if model_type == 'image':
            lmarena_payload['is_image_request'] = True
        
        # 包装成发送给浏览器的消息
        empty_response_retry_config = CONFIG.get("empty_response_retry", {})
        message_to_browser = {
            "request_id": request_id,
            "payload": lmarena_payload,
            "retry_config": {
                "enabled": empty_response_retry_config.get("enabled", True),
                "max_retries": empty_response_retry_config.get("max_retries", 5),
                "base_delay_ms": empty_response_retry_config.get("base_delay_ms", 1000),
                "max_delay_ms": empty_response_retry_config.get("max_delay_ms", 30000),
                "show_retry_info": empty_response_retry_config.get("show_retry_info_to_client", False)
            }
        }
        
        # 选择最佳标签页并发送
        logger.info(f"[SEND_DEBUG] 调用 select_best_tab_for_request()...")
        selected_tab_id, selected_ws = await select_best_tab_for_request_func()
        logger.info(f"[SEND_DEBUG] ✅ 已选择标签页: {selected_tab_id}")
        
        request_metadata[request_id]["tab_id"] = selected_tab_id
        if not request_metadata[request_id].get("original_tab_id"):
            request_metadata[request_id]["original_tab_id"] = selected_tab_id
        
        logger.info(f"API CALL [ID: {request_id[:8]}]: 通过标签页 '{selected_tab_id}' 发送请求")
        
        try:
            await asyncio.wait_for(
                selected_ws.send_text(json.dumps(message_to_browser)),
                timeout=10.0
            )
            logger.info(f"[SEND_DEBUG] ✅ WebSocket消息已发送")
        except asyncio.TimeoutError:
            logger.error(f"[SEND_DEBUG] ❌ WebSocket发送超时（10秒）！")
            raise HTTPException(status_code=504, detail="WebSocket发送超时")

        # 根据stream参数决定返回类型
        is_stream = openai_req.get("stream", False)

        if is_stream:
            # 流式响应
            response = StreamingResponse(
                stream_generator_func(
                    request_id,
                    model_name or "default_model",
                    lambda rid: process_lmarena_stream_func(
                        rid, response_channels.get(rid), request_metadata, CONFIG,
                        browser_connections, response_channels, IS_REFRESHING_FOR_VERIFICATION,
                        VERIFICATION_COOLDOWN_UNTIL, aiohttp_session, IMAGE_BASE64_CACHE,
                        IMAGE_CACHE_MAX_SIZE, IMAGE_CACHE_TTL, save_downloaded_image_async_func,
                        download_image_data_with_retry_func, release_tab_request_func
                    ),
                    format_openai_chunk_func,
                    format_openai_finish_chunk_func,
                    format_openai_error_chunk_func,
                    CONFIG,
                    response_channels,
                    request_metadata,
                    monitoring_service,
                    estimate_message_tokens_func,
                    estimate_tokens_func,
                    browser_connections
                ),
                media_type="text/event-stream",
                headers={
                    'Cache-Control': 'no-cache',
                    'Connection': 'keep-alive',
                    'X-Accel-Buffering': 'no',
                    'Transfer-Encoding': 'chunked'
                }
            )
            response.headers['X-Content-Type-Options'] = 'nosniff'
            return response
        else:
            # 非流式响应
            result = await non_stream_response_func(
                request_id,
                model_name or "default_model",
                lambda rid: process_lmarena_stream_func(
                    rid, response_channels.get(rid), request_metadata, CONFIG,
                    browser_connections, response_channels, IS_REFRESHING_FOR_VERIFICATION,
                    VERIFICATION_COOLDOWN_UNTIL, aiohttp_session, IMAGE_BASE64_CACHE,
                    IMAGE_CACHE_MAX_SIZE, IMAGE_CACHE_TTL, save_downloaded_image_async_func,
                    download_image_data_with_retry_func, release_tab_request_func
                ),
                format_openai_non_stream_response_func,
                CONFIG,
                response_channels,
                request_metadata,
                monitoring_service,
                estimate_message_tokens_func,
                estimate_tokens_func,
                release_tab_request_func,
                Response
            )
            await monitoring_service.broadcast_to_monitors({
                "type": "request_end",
                "request_id": request_id,
                "success": True
            })
            return result
            
    except (ValueError, IOError) as e:
        logger.error(f"API CALL [ID: {request_id[:8]}]: 附件预处理失败: {e}")
        monitoring_service.request_end(request_id, success=False, error=str(e))
        await monitoring_service.broadcast_to_monitors({
            "type": "request_end",
            "request_id": request_id,
            "success": False
        })
        
        if request_id in request_metadata:
            tab_id = request_metadata[request_id].get("tab_id")
            if tab_id:
                await release_tab_request_func(tab_id)
        
        if request_id in response_channels:
            del response_channels[request_id]
        if request_id in request_metadata:
            del request_metadata[request_id]
            
        return JSONResponse(
            status_code=500,
            content={"error": {"message": f"[LMArena Bridge Error] 附件处理失败: {e}", "type": "attachment_error"}}
        )
    except Exception as e:
        monitoring_service.request_end(request_id, success=False, error=str(e))
        await monitoring_service.broadcast_to_monitors({
            "type": "request_end",
            "request_id": request_id,
            "success": False
        })
        
        if request_id in request_metadata:
            tab_id = request_metadata[request_id].get("tab_id")
            if tab_id:
                await release_tab_request_func(tab_id)
        
        if request_id in response_channels:
            del response_channels[request_id]
        if request_id in request_metadata:
            del request_metadata[request_id]
            
        logger.error(f"API CALL [ID: {request_id[:8]}]: 处理请求时发生致命错误: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": {"message": str(e), "type": "internal_server_error"}}
        )