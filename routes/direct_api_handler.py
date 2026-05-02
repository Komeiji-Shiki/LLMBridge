"""
Direct API处理模块 - 路由入口
将请求分发到 Gemini Native / 透传 处理模块。

拆分后结构：
- _direct_api_utils.py      工具函数（重试/错误/SSE/Token/注入）
- _direct_api_gemini.py     Gemini 原生 API 处理
- _direct_api_passthrough.py 透传模式处理
- direct_api_handler.py     本文件：主路由 + 向后兼容重导出
"""
import asyncio
import logging
import uuid

from fastapi import HTTPException
from fastapi.responses import Response

from ._direct_api_utils import (
    get_round_robin_api_key,
    normalize_auto_retry_config,
    should_retry_response,
    should_retry_http_exception,
    inject_system_prompt,
)
from ._direct_api_gemini import handle_gemini_native_direct
from ._direct_api_passthrough import handle_passthrough_direct

logger = logging.getLogger(__name__)

# 重导出，维持向后兼容
__all__ = [
    "handle_direct_api_request",
    "handle_gemini_native_direct",
    "handle_passthrough_direct",
]


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
    process_image_data_func,
    full_messages: list = None
):
    """处理Direct API请求（Gemini Native或OpenAI兼容API）

    根据 api_type 分发到对应的处理模块。
    """
    api_type = endpoint_config.get("api_type")
    logger.info(f"[DIRECT_API] 检测到Direct API模式: {model_name} (类型: {api_type})")

    # 系统提示词注入
    system_injection_config = endpoint_config.get("system_prompt_injection")
    convert_system_to_user = endpoint_config.get("convert_system_to_user", False)
    if system_injection_config and system_injection_config.get("enabled", False):
        original_messages = openai_req.get("messages", [])
        injected_messages = inject_system_prompt(
            original_messages, system_injection_config, convert_system_to_user)
        openai_req["messages"] = injected_messages
        logger.info(
            f"[DIRECT_API] 系统提示词注入已启用 "
            f"(位置: {system_injection_config.get('position', 'before_system')}, "
            f"兼容System转User: {convert_system_to_user})")

    # 获取配置
    api_base_url = endpoint_config.get("api_base_url")
    raw_api_key = endpoint_config.get("api_keys") or endpoint_config.get("api_key")
    api_key = await get_round_robin_api_key(model_name, raw_api_key)
    target_model_id = endpoint_config.get("model_id", model_name)
    display_name = endpoint_config.get("display_name", model_name)
    passthrough_mode = endpoint_config.get("passthrough", False)
    use_native_format = endpoint_config.get("use_native_format", False)
    thinking_separator = endpoint_config.get("thinking_separator")
    pricing_config = endpoint_config.get("pricing", {})
    max_temperature = endpoint_config.get("max_temperature")

    # 图片预处理
    await _preprocess_images(
        openai_req, CONFIG, endpoint_config, PROCESSED_IMAGE_CACHE,
        process_image_data_func)

    # 应用温度限制
    if max_temperature is not None and "temperature" in openai_req:
        original_temp = openai_req["temperature"]
        if original_temp > max_temperature:
            openai_req["temperature"] = max_temperature
            logger.info(f"[TEMP_LIMIT] 模型 '{model_name}' 温度限制: {original_temp} -> {max_temperature}")

    # 应用最大输出Token限制
    max_tokens_limit = endpoint_config.get("max_tokens")
    if max_tokens_limit is not None and "max_tokens" in openai_req:
        original_max_tokens = openai_req["max_tokens"]
        if original_max_tokens > max_tokens_limit:
            openai_req["max_tokens"] = max_tokens_limit
            logger.info(f"[MAX_TOKENS_LIMIT] 模型 '{model_name}' 最大输出Token限制: {original_max_tokens} -> {max_tokens_limit}")

    # 验证必需配置
    if not api_key:
        raw_config = endpoint_config.get("api_keys") or endpoint_config.get("api_key")
        if isinstance(raw_config, list):
            detail = f"模型 '{model_name}' 的Direct API配置中所有 api_keys 均为空。"
        else:
            detail = f"模型 '{model_name}' 的Direct API配置缺少 api_key。"
        raise HTTPException(status_code=500, detail=detail)

    if api_type == "direct_api" and not api_base_url:
        raise HTTPException(
            status_code=500,
            detail=f"模型 '{model_name}' 的Direct API配置缺少 api_base_url。")

    _log_config_info(api_type, api_base_url, target_model_id, display_name,
                     passthrough_mode, pricing_config, endpoint_config)

    # 自动重试配置
    retry_config = normalize_auto_retry_config(endpoint_config)
    retry_enabled = retry_config.get("enabled", False) and retry_config.get("max_retries", 0) > 0
    max_attempts = retry_config.get("max_retries", 0) + 1 if retry_enabled else 1
    retry_delay_seconds = retry_config.get("retry_delay_seconds", 0.0) if retry_enabled else 0.0

    if retry_enabled:
        _log_retry_info(model_name, retry_config)

    # 构建单次执行函数
    async def _execute_single_attempt():
        if api_type == "gemini_native" or use_native_format:
            return await handle_gemini_native_direct(
                openai_req=openai_req, model_name=model_name,
                target_model_id=target_model_id, display_name=display_name,
                api_key=api_key, api_base_url=api_base_url,
                endpoint_config=endpoint_config, pricing_config=pricing_config,
                monitoring_service=monitoring_service,
                direct_api_service=direct_api_service,
                estimate_message_tokens_func=estimate_message_tokens_func,
                estimate_tokens_func=estimate_tokens_func,
                full_messages=full_messages, CONFIG=CONFIG)

        if passthrough_mode:
            return await handle_passthrough_direct(
                openai_req=openai_req, model_name=model_name,
                target_model_id=target_model_id, display_name=display_name,
                api_base_url=api_base_url, api_key=api_key,
                endpoint_config=endpoint_config, pricing_config=pricing_config,
                thinking_separator=thinking_separator,
                monitoring_service=monitoring_service,
                direct_api_service=direct_api_service,
                estimate_message_tokens_func=estimate_message_tokens_func,
                estimate_tokens_func=estimate_tokens_func,
                full_messages=full_messages, CONFIG=CONFIG)

        logger.info(f"[DIRECT_API] 使用转换模式（暂未实现完整转换逻辑）")
        raise HTTPException(
            status_code=501,
            detail="Direct API转换模式尚未完全实现。请使用 passthrough: true 启用透传模式。")

    # 执行带重试的调用
    logger.info(f"[AUTO_RETRY] 模型 '{model_name}' 最大尝试次数: {max_attempts}")

    for attempt in range(1, max_attempts + 1):
        try:
            response = await _execute_single_attempt()

            if retry_enabled and attempt < max_attempts and isinstance(response, Response):
                should_retry, retry_reason = should_retry_response(response, retry_config)
                if should_retry:
                    logger.warning(
                        f"[AUTO_RETRY] 模型 '{model_name}' 第 {attempt}/{max_attempts} 次尝试失败"
                        f"（响应可重试）: {retry_reason}，{retry_delay_seconds}秒后重试...")
                    if retry_delay_seconds > 0:
                        await asyncio.sleep(retry_delay_seconds)
                    continue

            return response

        except HTTPException as http_exc:
            if retry_enabled and attempt < max_attempts:
                should_retry, retry_reason = should_retry_http_exception(http_exc, retry_config)
                if should_retry:
                    logger.warning(
                        f"[AUTO_RETRY] 模型 '{model_name}' 第 {attempt}/{max_attempts} 次尝试失败"
                        f"（HTTPException {http_exc.status_code}）: {retry_reason}，{retry_delay_seconds}秒后重试...")
                    if retry_delay_seconds > 0:
                        await asyncio.sleep(retry_delay_seconds)
                    continue
            raise

        except Exception as exc:
            if retry_enabled and attempt < max_attempts and retry_config.get("retry_on_other_errors", False):
                logger.warning(
                    f"[AUTO_RETRY] 模型 '{model_name}' 第 {attempt}/{max_attempts} 次尝试失败"
                    f"（异常）: {exc}，{retry_delay_seconds}秒后重试...")
                if retry_delay_seconds > 0:
                    await asyncio.sleep(retry_delay_seconds)
                continue
            raise


# ============================================================
# 辅助函数
# ============================================================

async def _preprocess_images(openai_req, CONFIG, endpoint_config,
                              PROCESSED_IMAGE_CACHE, process_image_data_func):
    """图片预处理：压缩/base64优化"""
    model_image_config = endpoint_config.get("image_compression")
    global_optimization_enabled = CONFIG.get("image_optimization", {}).get("enabled", False)
    model_optimization_enabled = model_image_config.get("enabled", False) if model_image_config else False
    optimization_enabled = global_optimization_enabled or model_optimization_enabled

    if not optimization_enabled:
        return

    logger.info(f"[DIRECT_API] 开始图片预处理...")
    if model_image_config:
        logger.info(f"[DIRECT_API] 使用模型级别图片配置: {model_image_config}")

    import re
    request_id_for_img = str(uuid.uuid4())[:8]
    messages_to_process = openai_req.get("messages", [])
    image_processed_count = 0

    for msg_index, message in enumerate(messages_to_process):
        role = message.get("role", "unknown")
        content = message.get("content")

        if isinstance(content, str):
            markdown_image_pattern = r'!\[([^\]]*)\]\((data:[^)]+)\)'
            markdown_matches = re.findall(markdown_image_pattern, content)

            for match_index, (alt_text, base64_url) in enumerate(markdown_matches):
                processed_data, proc_error = await process_image_data_func(
                    base64_data=base64_url,
                    filename=f"direct_{role}_{msg_index}_{match_index}_{uuid.uuid4()}.png",
                    request_id=request_id_for_img,
                    CONFIG=CONFIG,
                    PROCESSED_IMAGE_CACHE=PROCESSED_IMAGE_CACHE,
                    model_image_config=model_image_config)

                if proc_error:
                    logger.warning(f"[DIRECT_API] 图片处理警告: {proc_error}")

                old_markdown = f"![{alt_text}]({base64_url})"
                new_markdown = f"![{alt_text}]({processed_data})"
                content = content.replace(old_markdown, new_markdown)
                message["content"] = content
                image_processed_count += 1

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
                            model_image_config=model_image_config)

                        if proc_error:
                            logger.warning(f"[DIRECT_API] 图片处理警告: {proc_error}")

                        part["image_url"]["url"] = processed_data
                        image_processed_count += 1

    if image_processed_count > 0:
        logger.info(f"[DIRECT_API] 图片预处理完成: 处理了 {image_processed_count} 张图片")


def _log_config_info(api_type, api_base_url, target_model_id, display_name,
                     passthrough_mode, pricing_config, endpoint_config):
    """输出Direct API配置信息到日志"""
    logger.info(f"[DIRECT_API] 配置信息:")
    logger.info(f"  - API类型: {api_type}")
    logger.info(f"  - 基础URL: {api_base_url if api_base_url else '(使用默认)'}")
    logger.info(f"  - 目标模型ID: {target_model_id}")
    logger.info(f"  - 显示名称: {display_name}")
    logger.info(f"  - 透传模式: {passthrough_mode}")
    logger.info(f"  - 计费配置: {pricing_config}")

    raw_api_key_config = endpoint_config.get("api_keys") or endpoint_config.get("api_key")
    if isinstance(raw_api_key_config, list) and len(raw_api_key_config) > 1:
        key_count = len([k for k in raw_api_key_config if k and k.strip()])
        logger.info(f"  - API Key轮询: 已启用 ({key_count} 个有效key)")


def _log_retry_info(model_name, retry_config):
    """输出重试配置信息到日志"""
    retry_targets = []
    if retry_config.get("retry_on_429", True):
        retry_targets.append("429")
    if retry_config.get("retry_on_503", True):
        retry_targets.append("503")
    if retry_config.get("retry_on_other_errors", False):
        retry_targets.append("other_errors")
    logger.info(
        f"[AUTO_RETRY] 模型 '{model_name}' 启用自动重试: "
        f"max_retries={retry_config.get('max_retries', 0)}, "
        f"delay={retry_config.get('retry_delay_seconds', 0)}s, "
        f"targets={','.join(retry_targets) if retry_targets else 'none'}")
