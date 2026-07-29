"""
LMArena模式处理模块（已弃用，保留兼容）

⚠️ LMArena 功能已经不再维护，无需考虑本模块及相关链路
（websocket_routes / load_balancer / message_converter / stream_processor 的
LMArena 部分）的代码维护。目前项目重点是中转 API（Direct API）。

处理通过浏览器WebSocket转发的LMArena请求。
新配置请优先使用 Direct API；此模块仅继续服务旧的 LMArena 兼容配置。

重构说明：
- 依赖通过模块 import 与 AppState 单例获取，不再使用长参数链注入
"""
import asyncio
import json
import logging
import time
import uuid
import re
from datetime import datetime
from typing import Optional
from fastapi import HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, Response

from core.config_loader import (
    CONFIG,
    MODEL_ENDPOINT_MAP,
    MODEL_ROUND_ROBIN_INDEX,
    MODEL_ROUND_ROBIN_LOCK,
)
from core.app_state import get_app_state
from core.load_balancer import select_best_tab, release_tab
from modules.monitoring import monitoring_service
from modules.token_counter import estimate_message_tokens, estimate_tokens
from services.message_converter import convert_openai_to_lmarena_payload
from services.stream_processor import _process_lmarena_stream, stream_generator, non_stream_response
from services.image_service import process_image_data
from utils.api_helpers import (
    format_openai_chunk,
    format_openai_finish_chunk,
    format_openai_error_chunk,
    format_openai_non_stream_response,
)
from utils.monitor_params import build_monitor_request_params

logger = logging.getLogger(__name__)

_app_state = get_app_state()


async def handle_lmarena_request(
    openai_req: dict,
    model_name: Optional[str],
    model_type: str,
):
    """
    处理LMArena模式的请求（通过WebSocket转发给油猴脚本）

    Args:
        openai_req: OpenAI格式的请求体
        model_name: 模型名称
        model_type: 模型类型（text/image）
    """
    response_channels = _app_state.response_channels
    request_metadata = _app_state.request_metadata
    browser_connections = _app_state.browser_connections
    image_state = _app_state.image

    # 模型与会话ID映射逻辑
    session_id = None
    mode_override, battle_target_override = None, None
    selected_index_for_update = None
    max_temperature_config = None
    max_tokens_config = None

    if model_name and model_name in MODEL_ENDPOINT_MAP:
        mapping_entry = MODEL_ENDPOINT_MAP[model_name]
        selected_mapping = None

        if isinstance(mapping_entry, list) and mapping_entry:
            # 轮询策略选择映射
            async with MODEL_ROUND_ROBIN_LOCK:
                # 🔧 越界修复：先对当前列表长度取模，防止配置热重载后索引越界
                current_index = MODEL_ROUND_ROBIN_INDEX.get(model_name, 0) % len(mapping_entry)
                selected_mapping = mapping_entry[current_index]

                logger.info(f"[ROUND_ROBIN] ✅ 模型 '{model_name}' 轮询选择: "
                            f"#{current_index + 1}/{len(mapping_entry)} "
                            f"(Session ID后8位: ...{str(selected_mapping.get('session_id', 'N/A'))[-8:]})")

                selected_index_for_update = current_index
        elif isinstance(mapping_entry, dict):
            selected_mapping = mapping_entry
            logger.info(f"为模型 '{model_name}' 找到了单个端点映射（旧格式）。")

        if selected_mapping:
            session_id = selected_mapping.get("session_id")
            mode_override = selected_mapping.get("mode")
            battle_target_override = selected_mapping.get("battle_target")
            max_temperature_config = selected_mapping.get("max_temperature")
            max_tokens_config = selected_mapping.get("max_tokens")
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

    # 应用最大输出Token限制
    if max_tokens_config is not None and "max_tokens" in openai_req:
        original_max_tokens = openai_req["max_tokens"]
        if original_max_tokens > max_tokens_config:
            openai_req["max_tokens"] = max_tokens_config
            logger.info(f"[MAX_TOKENS_LIMIT] 模型 '{model_name}' 最大输出Token限制: {original_max_tokens} -> {max_tokens_config}")

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
        params=build_monitor_request_params(openai_req)
    )

    # 广播请求开始
    await monitoring_service.broadcast_to_monitors({
        "type": "request_start",
        "request_id": request_id,
        "model": model_name,
        "timestamp": time.time()
    })

    def _make_stream_processor(rid: str):
        """构建针对单个请求的内部事件流处理器"""
        return _process_lmarena_stream(
            rid,
            response_channels.get(rid),
            request_metadata,
            CONFIG,
            browser_connections,
            response_channels,
            image_state.IMAGE_BASE64_CACHE,
            image_state.IMAGE_CACHE_MAX_SIZE,
            image_state.IMAGE_CACHE_TTL,
        )

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
            logger.info(f"📋 开始统一图片处理流程，准备处理 {len(messages_to_process)} 条消息中的图片")
            if model_image_config:
                logger.info(f"📋 使用模型级别配置: convert_png_to_jpg={model_image_config.get('convert_png_to_jpg')}, target_size_kb={model_image_config.get('target_size_kb')}")

            role_image_count = {}

            for msg_index, message in enumerate(messages_to_process):
                role = message.get("role", "unknown")
                content = message.get("content")

                # 处理字符串内容中的Markdown图片
                if isinstance(content, str):
                    markdown_image_pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
                    markdown_matches = re.findall(markdown_image_pattern, content)
                    base64_matches = [(alt, url) for alt, url in markdown_matches if url.startswith('data:')]

                    if base64_matches:
                        logger.info(f"  📷 发现 {len(base64_matches)} 个Markdown格式base64图片")

                        for match_index, (alt_text, base64_url) in enumerate(base64_matches):
                            role_image_count[role] = role_image_count.get(role, 0) + 1

                            processed_data, proc_error = await process_image_data(
                                base64_data=base64_url,
                                filename=f"{role}_string_{msg_index}_{match_index}_{uuid.uuid4()}.png",
                                request_id=request_id,
                                CONFIG=CONFIG,
                                # 🔧 使用独立的输入图片处理缓存：旧版误传响应图片
                                # base64 缓存，两种语义的条目互相挤占驱逐
                                PROCESSED_IMAGE_CACHE=image_state.PROCESSED_IMAGE_CACHE,
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
                    for part_index, part in enumerate(content):
                        if part.get("type") == "image_url":
                            url_content = part.get("image_url", {}).get("url")

                            if url_content and url_content.startswith("data:"):
                                role_image_count[role] = role_image_count.get(role, 0) + 1

                                processed_data, proc_error = await process_image_data(
                                    base64_data=url_content,
                                    filename=f"{role}_list_{msg_index}_{part_index}_{uuid.uuid4()}.png",
                                    request_id=request_id,
                                    CONFIG=CONFIG,
                                    PROCESSED_IMAGE_CACHE=image_state.PROCESSED_IMAGE_CACHE,
                                    model_image_config=model_image_config  # 传递模型配置
                                )

                                if proc_error:
                                    logger.warning(f"    ⚠️ 图片处理出现警告: {proc_error}")

                                part["image_url"]["url"] = processed_data

            if role_image_count:
                logger.info(f"✅ 图片处理完成。各角色图片统计：{role_image_count}")

        # 转换请求
        logger.info(f"[SEND_DEBUG] 开始转换OpenAI请求到LMArena格式...")
        lmarena_payload = await convert_openai_to_lmarena_payload(
            openai_req,
            session_id,
            mode_override=mode_override,
            battle_target_override=battle_target_override
        )
        logger.info(f"[SEND_DEBUG] ✅ 请求转换完成")

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
        logger.info(f"[SEND_DEBUG] 调用 select_best_tab()...")
        selected_tab_id, selected_ws = await select_best_tab()
        logger.info(f"[SEND_DEBUG] ✅ 已选择标签页: {selected_tab_id}")

        request_metadata[request_id]["tab_id"] = selected_tab_id
        if not request_metadata[request_id].get("original_tab_id"):
            request_metadata[request_id]["original_tab_id"] = selected_tab_id

        logger.info(f"API CALL [ID: {request_id[:8]}]: 通过标签页 '{selected_tab_id}' 发送请求")

        try:
            message_to_browser_json = await asyncio.to_thread(
                json.dumps,
                message_to_browser,
                ensure_ascii=False,
                separators=(',', ':')
            )
            await asyncio.wait_for(
                selected_ws.send_text(message_to_browser_json),
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
                stream_generator(
                    request_id,
                    model_name or "default_model",
                    _make_stream_processor,
                    format_openai_chunk,
                    format_openai_finish_chunk,
                    format_openai_error_chunk,
                    CONFIG,
                    response_channels,
                    request_metadata,
                    monitoring_service,
                    estimate_message_tokens,
                    estimate_tokens,
                    browser_connections,
                    full_messages=openai_req.get("messages", [])  # 传递全量消息用于日志
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
            result = await non_stream_response(
                request_id,
                model_name or "default_model",
                _make_stream_processor,
                format_openai_non_stream_response,
                CONFIG,
                response_channels,
                request_metadata,
                monitoring_service,
                estimate_message_tokens,
                estimate_tokens,
                release_tab,
                Response,
                full_messages=openai_req.get("messages", [])  # 传递全量消息用于日志
            )
            await monitoring_service.broadcast_to_monitors({
                "type": "request_end",
                "request_id": request_id,
                "success": True
            })
            return result

    except (ValueError, IOError) as e:
        logger.error(f"API CALL [ID: {request_id[:8]}]: 附件预处理失败: {e}")
        await _handle_request_failure(request_id, model_name, openai_req, e)
        return JSONResponse(
            status_code=500,
            content={"error": {"message": f"[LMArena Bridge Error] 附件处理失败: {e}", "type": "attachment_error"}}
        )
    except HTTPException:
        # HTTP 异常（如 503 无可用标签页）也要清理资源后重新抛出
        await _handle_request_failure(request_id, model_name, openai_req, "HTTPException")
        raise
    except Exception as e:
        logger.error(f"API CALL [ID: {request_id[:8]}]: 处理请求时发生致命错误: {e}", exc_info=True)
        await _handle_request_failure(request_id, model_name, openai_req, e)
        return JSONResponse(
            status_code=500,
            content={"error": {"message": str(e), "type": "internal_server_error"}}
        )


async def _handle_request_failure(request_id: str, model_name: Optional[str], openai_req: dict, error) -> None:
    """请求失败时的统一清理：记录监控、释放标签页、清理通道。"""
    response_channels = _app_state.response_channels
    request_metadata = _app_state.request_metadata

    # 即使失败也计算输入tokens
    partial_input_tokens = 0
    try:
        partial_input_tokens = estimate_message_tokens(
            openai_req.get('messages', []),
            model=model_name or "unknown"
        )
    except Exception as token_err:
        logger.warning(f"[LMARENA] 失败时输入token计算失败: {token_err}")

    monitoring_service.request_end(
        request_id,
        success=False,
        error=str(error),
        input_tokens=partial_input_tokens,
        output_tokens=0,
        full_messages=openai_req.get('messages', [])  # 错误时也全量存日志
    )
    await monitoring_service.broadcast_to_monitors({
        "type": "request_end",
        "request_id": request_id,
        "success": False
    })

    if request_id in request_metadata:
        tab_id = request_metadata[request_id].get("tab_id")
        if tab_id:
            await release_tab(tab_id)

    # 🔧 使用 pop() 避免 TOCTOU 竞态
    response_channels.pop(request_id, None)
    request_metadata.pop(request_id, None)
