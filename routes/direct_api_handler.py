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
from typing import Any, Optional

from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse

from ._direct_api_utils import (
    get_api_key,
    get_round_robin_api_key,
    mark_sticky_key_cooldown,
    is_quota_exceeded,
    normalize_auto_retry_config,
    should_retry_response,
    should_retry_http_exception,
    inject_system_prompt,
    ensure_reasoning_noop_tool,
)
from ._direct_api_gemini import handle_gemini_native_direct
from ._direct_api_passthrough import handle_passthrough_direct
from ._direct_api_responses import handle_responses_native_direct
from ._direct_api_anthropic import handle_anthropic_native_from_openai

logger = logging.getLogger(__name__)

# 重导出，维持向后兼容
__all__ = [
    "handle_direct_api_request",
    "handle_gemini_native_direct",
    "handle_passthrough_direct",
    "handle_responses_native_direct",
]


async def handle_direct_api_request(
    openai_req: dict,
    model_name: Optional[str],
    endpoint_config: dict,
    CONFIG: dict,
    PROCESSED_IMAGE_CACHE: Any,
    monitoring_service,
    direct_api_service,
    estimate_message_tokens_func,
    estimate_tokens_func,
    process_image_data_func,
    full_messages: Optional[list] = None
):
    """处理Direct API请求（Gemini Native或OpenAI兼容API）

    根据 api_type 分发到对应的处理模块。
    """
    # 规范化 model_name，避免 None 沿调用链传播
    model_name = model_name or openai_req.get("model") or "unknown"
    api_type = endpoint_config.get("api_type")
    logger.info(f"[DIRECT_API] 检测到Direct API模式: {model_name} (类型: {api_type})")

    # 系统提示词注入（anthropic_native 由 handle_anthropic_native_from_openai 内部用 Anthropic 格式处理，此处跳过）
    system_injection_config = endpoint_config.get("system_prompt_injection")
    convert_system_to_user = endpoint_config.get("convert_system_to_user", False)
    if api_type != "anthropic_native" and system_injection_config and system_injection_config.get("enabled", False):
        original_messages = openai_req.get("messages", [])
        injected_messages = inject_system_prompt(
            original_messages, system_injection_config, convert_system_to_user)
        openai_req["messages"] = injected_messages
        # 🔧 full_messages 同步为注入后的消息：日志 request_messages 才能
        # 记录到伪造对话历史（此前记的是调用方传入的注入前原始消息）
        full_messages = injected_messages
        logger.info(
            f"[DIRECT_API] 系统提示词注入已启用 "
            f"(位置: {system_injection_config.get('position', 'before_system')}, "
            f"兼容System转User: {convert_system_to_user})")

        # 思考模型伪造思维链：确保请求带 tools 以触发历史 reasoning_content 拼接
        # （仅 OpenAI 兼容格式，DeepSeek 等；下游已带工具则保持原样）
        if api_type == "direct_api":
            ensure_reasoning_noop_tool(openai_req, system_injection_config)

    # 预填充注入（anthropic_native 跳过，Anthropic 格式 prefilling 方式不同）
    prefill_content = endpoint_config.get("prefill_content")
    if api_type != "anthropic_native" and prefill_content and isinstance(prefill_content, str) and prefill_content.strip():
        messages = openai_req.get("messages", [])
        messages.append({"role": "assistant", "content": prefill_content})
        openai_req["messages"] = messages
        logger.info(
            f"[DIRECT_API] 预填充已启用: 在消息末尾追加 assistant 消息 "
            f"({len(prefill_content)} 字符)")

    # 获取配置
    api_base_url = endpoint_config.get("api_base_url")
    raw_api_key = endpoint_config.get("api_keys") or endpoint_config.get("api_key")
    # 🔧 支持 api_key_strategy 选择轮询策略：round_robin（默认）或 sticky
    api_key_strategy = endpoint_config.get("api_key_strategy", "round_robin")
    api_key_cooldown = int(endpoint_config.get("api_key_cooldown_seconds", 0) or 0)
    if api_key_cooldown <= 0:
        api_key_cooldown = 172800  # 默认 48 小时
    api_key = await get_api_key(model_name, raw_api_key, strategy=api_key_strategy, cooldown_seconds=api_key_cooldown)
    target_model_id = endpoint_config.get("model_id", model_name)
    display_name = endpoint_config.get("display_name", model_name)
    passthrough_mode = endpoint_config.get("passthrough", False)
    use_native_format = endpoint_config.get("use_native_format", False)
    thinking_separator = endpoint_config.get("thinking_separator")
    pricing_config = endpoint_config.get("pricing", {})
    max_temperature = endpoint_config.get("max_temperature")

    # 强制流式/非流式：不就地改写 openai_req["stream"]。
    # 🔧 旧版直接 openai_req["stream"] = bool(force_stream)，该 dict 在调用方
    # （api_routes.py）被多处引用（原始 stream 判断、/v1/messages 分支等），
    # 一次写入会让后续所有引用点读到错误值。
    force_stream = endpoint_config.get("force_stream")
    original_stream = openai_req.get("stream", False)
    _stream_overridden = force_stream is not None and (original_stream != bool(force_stream))
    if _stream_overridden:
        logger.info(f"[DIRECT_API] force_stream={force_stream} 覆盖客户端 stream={original_stream}")

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

    # 应用最大输出Token限制（新版 OpenAI SDK 可能只发 max_completion_tokens，两者都检查）
    max_tokens_limit = endpoint_config.get("max_tokens")
    if max_tokens_limit is not None:
        if "max_tokens" in openai_req:
            original_max_tokens = openai_req["max_tokens"]
            if original_max_tokens > max_tokens_limit:
                openai_req["max_tokens"] = max_tokens_limit
                logger.info(f"[MAX_TOKENS_LIMIT] 模型 '{model_name}' 最大输出Token限制: {original_max_tokens} -> {max_tokens_limit}")
        if "max_completion_tokens" in openai_req:
            original_max_tokens = openai_req["max_completion_tokens"]
            if original_max_tokens > max_tokens_limit:
                openai_req["max_completion_tokens"] = max_tokens_limit
                logger.info(f"[MAX_TOKENS_LIMIT] 模型 '{model_name}' max_completion_tokens限制: {original_max_tokens} -> {max_tokens_limit}")

    # 验证必需配置
    # 本地部署模型（localhost/127.0.0.1/局域网）不需要 API key
    api_key_required = not _is_local_api_base(api_base_url)
    if api_key_required and not api_key:
        raw_config = endpoint_config.get("api_keys") or endpoint_config.get("api_key")
        if isinstance(raw_config, list):
            detail = f"模型 '{model_name}' 的Direct API配置中所有 api_keys 均为空。"
        else:
            detail = f"模型 '{model_name}' 的Direct API配置缺少 api_key。"
        raise HTTPException(status_code=500, detail=detail)

    if api_type in ("direct_api", "responses_native") and not api_base_url:
        raise HTTPException(
            status_code=500,
            detail=f"模型 '{model_name}' 的Direct API配置缺少 api_base_url。")

    _log_config_info(api_type, api_base_url, target_model_id, display_name,
                     passthrough_mode, pricing_config, endpoint_config)

    # 自动重试配置
    retry_config = normalize_auto_retry_config(endpoint_config)
    retry_enabled = retry_config.get("enabled", False) and retry_config.get("max_retries", 0) > 0
    auto_max_attempts = retry_config.get("max_retries", 0) + 1 if retry_enabled else 1
    retry_delay_seconds = retry_config.get("retry_delay_seconds", 0.0) if retry_enabled else 0.0

    # sticky 策略：确保至少能尝试所有 key（不依赖 auto_retry）
    if api_key_strategy == "sticky":
        if isinstance(raw_api_key, list):
            key_count = len([k for k in raw_api_key if k and k.strip()])
        elif isinstance(raw_api_key, str) and raw_api_key.strip():
            key_count = 1
        else:
            key_count = 1
        sticky_max_attempts = max(key_count, 1) + 1  # +1 为降级兜底
        max_attempts = max(auto_max_attempts, sticky_max_attempts)
        # sticky 默认至少 1 秒延迟
        if retry_delay_seconds <= 0:
            retry_delay_seconds = 1.0
    else:
        max_attempts = auto_max_attempts

    if retry_enabled or api_key_strategy == "sticky":
        _log_retry_info(model_name, retry_config, api_key_strategy, max_attempts)

    # 🔧 构建上游请求体的副本，force_stream 改写只影响副本而非调用方 openai_req。
    # 这样 api_routes.py 等上层调用者继续读原值做 client stream 判断，
    # 而三条下游链路读到的是已覆盖的 stream 值。
    req_for_upstream = openai_req
    if _stream_overridden:
        req_for_upstream = {**openai_req, "stream": bool(force_stream)}

    # 构建单次执行函数（🔧 api_key 作为参数传入：重试时可换用轮询到的下一个 key）
    async def _execute_single_attempt(attempt_api_key):
        if api_type == "gemini_native" or use_native_format:
            return await handle_gemini_native_direct(
                openai_req=req_for_upstream, model_name=model_name,
                target_model_id=target_model_id, display_name=display_name,
                api_key=attempt_api_key, api_base_url=api_base_url,
                endpoint_config=endpoint_config, pricing_config=pricing_config,
                monitoring_service=monitoring_service,
                direct_api_service=direct_api_service,
                estimate_message_tokens_func=estimate_message_tokens_func,
                estimate_tokens_func=estimate_tokens_func,
                full_messages=full_messages, CONFIG=CONFIG)

        if api_type == "responses_native":
            return await handle_responses_native_direct(
                openai_req=req_for_upstream,
                model_name=model_name,
                target_model_id=target_model_id,
                display_name=display_name,
                api_base_url=api_base_url,
                api_key=attempt_api_key,
                endpoint_config=endpoint_config,
                pricing_config=pricing_config,
                monitoring_service=monitoring_service,
                direct_api_service=direct_api_service,
                estimate_message_tokens_func=estimate_message_tokens_func,
                estimate_tokens_func=estimate_tokens_func,
                full_messages=full_messages,
                CONFIG=CONFIG,
            )

        if api_type == "anthropic_native":
            return await handle_anthropic_native_from_openai(
                openai_req=req_for_upstream, model_name=model_name,
                endpoint_config=endpoint_config,
                api_key=attempt_api_key,
                CONFIG=CONFIG,
                monitoring_service=monitoring_service,
                direct_api_service=direct_api_service,
                estimate_message_tokens_func=estimate_message_tokens_func,
                estimate_tokens_func=estimate_tokens_func,
                full_messages=full_messages,
                thinking_separator=thinking_separator)

        if passthrough_mode:
            return await handle_passthrough_direct(
                openai_req=req_for_upstream, model_name=model_name,
                target_model_id=target_model_id, display_name=display_name,
                api_base_url=api_base_url, api_key=attempt_api_key,
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
    if api_key_strategy == "sticky":
        logger.info(f"[STICKY_KEY] 模型 '{model_name}' 使用粘性轮询（冷却: {api_key_cooldown // 3600}h）")

    # ── sticky 公共辅助：quota exceeded → 冷却 ──
    async def _sticky_cooldown_if_quota(status_code: int, body_text: str = ""):
        if api_key_strategy == "sticky" and is_quota_exceeded(status_code, body_text):
            await mark_sticky_key_cooldown(model_name, raw_api_key, api_key, api_key_cooldown)
            return True
        return False

    for attempt in range(1, max_attempts + 1):
        try:
            if attempt > 1:
                if api_key_strategy == "sticky":
                    # sticky 策略：由 mark_sticky_key_cooldown 在上次失败时清空 current，
                    # 此处 get_sticky_api_key 会自动选下一个不在冷却期的 key
                    api_key = await get_api_key(model_name, raw_api_key, strategy="sticky", cooldown_seconds=api_key_cooldown) or api_key
                else:
                    # round_robin 策略：每次重试轮询到下一个 key
                    api_key = await get_round_robin_api_key(model_name, raw_api_key) or api_key
            response = await _execute_single_attempt(api_key)

            # ── 重试判断（auto_retry + sticky quota）──
            if attempt < max_attempts and isinstance(response, Response):
                should_retry = False
                retry_reason = ""

                # auto_retry 判断
                if retry_enabled:
                    should_retry, retry_reason = should_retry_response(response, retry_config)

                # sticky 策略：quota exceeded → 总是冷却 + 重试（不依赖 auto_retry）
                if not should_retry:
                    resp_status = getattr(response, "status_code", 0)
                    try:
                        resp_body = response.body if hasattr(response, "body") else b""
                        resp_body_text = resp_body.decode("utf-8", errors="ignore") if resp_body else ""
                    except Exception:
                        resp_body_text = ""
                    if await _sticky_cooldown_if_quota(resp_status, resp_body_text):
                        should_retry = True
                        retry_reason = f"Sticky quota exceeded (HTTP {resp_status}，换key重试)"

                if should_retry:
                    logger.warning(
                        f"[AUTO_RETRY] 模型 '{model_name}' 第 {attempt}/{max_attempts} 次尝试失败"
                        f"（响应可重试）: {retry_reason}，{retry_delay_seconds}秒后重试...")
                    if retry_delay_seconds > 0:
                        await asyncio.sleep(retry_delay_seconds)
                    continue

            # 强制非流式时客户端期望 SSE → 将非流式 JSON 转为流式 chunk 再包装
            # 🔧 错误响应不应被改写——上游 4xx/5xx 的 JSONResponse 若被包成 SSE 流，
            # 客户端读到的是 HTTP 200 text/event-stream 包裹的错误，语义完全错。
            if (_stream_overridden and original_stream
                    and isinstance(response, Response)
                    and type(response) is not StreamingResponse):
                resp_status = getattr(response, "status_code", 200)
                if resp_status >= 400:
                    return response
                try:
                    resp_body = response.body
                except Exception:
                    resp_body = b""
                    logger.warning(f"[DIRECT_API] 强制非流式：无法读取 response.body")
                if resp_body:
                    sse_data = _convert_non_stream_to_sse(resp_body)
                    logger.info(f"[DIRECT_API] 强制非流式：已将 JSON ({len(resp_body)} bytes) 转为流式 SSE")
                else:
                    logger.warning(f"[DIRECT_API] 强制非流式：response.body 为空，仅发送 [DONE]")
                    sse_data = "data: [DONE]\n\n"
                response = Response(content=sse_data, media_type="text/event-stream", status_code=resp_status)

            return response

        except HTTPException as http_exc:
            if attempt < max_attempts:
                should_retry = False
                retry_reason = ""

                # auto_retry 判断
                if retry_enabled:
                    should_retry, retry_reason = should_retry_http_exception(http_exc, retry_config)

                # sticky 策略：quota exceeded → 总是冷却 + 重试（不依赖 auto_retry）
                if not should_retry:
                    if await _sticky_cooldown_if_quota(http_exc.status_code):
                        should_retry = True
                        retry_reason = f"Sticky quota exceeded (HTTP {http_exc.status_code}，换key重试)"

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

    # 🔧 Direct API 透传模式下强制禁用图床上传
    # process_image_data 会读取 file_bed_enabled，如果为 true 会把
    # base64 图片上传到图床并替换为 HTTP URL，但上游 API 只接受 base64。
    # 使用浅拷贝覆盖该字段，避免修改全局 CONFIG 引发并发竞态与配置污染。
    img_config = {**CONFIG, "file_bed_enabled": False}

    logger.info(f"[DIRECT_API] 开始图片预处理...")
    if model_image_config:
        logger.info(f"[DIRECT_API] 使用模型级别图片配置: {model_image_config}")

    import re
    request_id_for_img = str(uuid.uuid4())[:8]
    messages_to_process = openai_req.get("messages", [])
    image_processed_count = 0

    for msg_index, message in enumerate(messages_to_process):
        if not isinstance(message, dict):
            # 🔧 类型防护：messages 元素可能是非 dict（脏数据），跳过交给上游报错
            continue
        role = message.get("role", "unknown")
        content = message.get("content")

        if isinstance(content, str):
            markdown_image_pattern = r'!\[([^\]]*)\]\((data:[^)]+)\)'
            # 🔧 性能：先异步处理所有图片建立 base64→结果映射，再用 re.sub 回调
            # 一次性替换。旧版每张图 content.replace 全量复制字符串，
            # 多张大 base64 图时退化为 O(n·m)；重复图片也不再重复处理
            replacements = {}
            for match_index, match in enumerate(re.finditer(markdown_image_pattern, content)):
                base64_url = match.group(2)
                if base64_url in replacements:
                    continue
                processed_data, proc_error = await process_image_data_func(
                    base64_data=base64_url,
                    filename=f"direct_{role}_{msg_index}_{match_index}_{uuid.uuid4()}.png",
                    request_id=request_id_for_img,
                    CONFIG=img_config,
                    PROCESSED_IMAGE_CACHE=PROCESSED_IMAGE_CACHE,
                    model_image_config=model_image_config)

                if proc_error:
                    logger.warning(f"[DIRECT_API] 图片处理警告: {proc_error}")

                replacements[base64_url] = processed_data
                image_processed_count += 1

            if replacements:
                content = re.sub(
                    markdown_image_pattern,
                    lambda m: f"![{m.group(1)}]({replacements.get(m.group(2), m.group(2))})",
                    content)
                message["content"] = content

        elif isinstance(content, list):
            for part_index, part in enumerate(content):
                # 🔧 类型防护：part 可能是非 dict，image_url 也可能是字符串
                if not isinstance(part, dict) or part.get("type") != "image_url":
                    continue
                img = part.get("image_url")
                url_content = img.get("url") if isinstance(img, dict) else (img if isinstance(img, str) else None)

                if url_content and url_content.startswith("data:"):
                    processed_data, proc_error = await process_image_data_func(
                        base64_data=url_content,
                        filename=f"direct_{role}_{msg_index}_{part_index}_{uuid.uuid4()}.png",
                        request_id=request_id_for_img,
                        CONFIG=img_config,
                        PROCESSED_IMAGE_CACHE=PROCESSED_IMAGE_CACHE,
                        model_image_config=model_image_config)

                    if proc_error:
                        logger.warning(f"[DIRECT_API] 图片处理警告: {proc_error}")

                    # 按 image_url 的实际类型分别写回（dict 写 .url，str 写整个字段）
                    if isinstance(img, dict):
                        img["url"] = processed_data
                    else:
                        part["image_url"] = {"url": processed_data}
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


def _log_retry_info(model_name, retry_config, api_key_strategy="round_robin", max_attempts=1):
    """输出重试配置信息到日志"""
    retry_targets = []
    if retry_config.get("retry_on_402", True):
        retry_targets.append("402")
    if retry_config.get("retry_on_429", True):
        retry_targets.append("429")
    if retry_config.get("retry_on_503", True):
        retry_targets.append("503")
    if retry_config.get("retry_on_other_errors", False):
        retry_targets.append("other_errors")
    if api_key_strategy == "sticky":
        retry_targets.append("sticky_quota")
    logger.info(
        f"[AUTO_RETRY] 模型 '{model_name}' 启用自动重试: "
        f"strategy={api_key_strategy}, max_attempts={max_attempts}, "
        f"delay={retry_config.get('retry_delay_seconds', 0)}s, "
        f"targets={','.join(retry_targets) if retry_targets else 'none'}")


def _is_local_api_base(url: Optional[str]) -> bool:
    """判断 api_base_url 是否指向本地/局域网地址（无需 API key）。

    🔧 修复：旧版用子串匹配（'localhost' in url 等），
    https://localhost.evil.com 或路径中含私网 IP 的公网 URL 会被误判为
    本地地址而跳过 API key 必填校验。现在用 urlparse 提取 hostname
    后精确判断（ipaddress 覆盖 loopback/私网/链路本地/0.0.0.0）。
    """
    if not url:
        return False
    import ipaddress
    from urllib.parse import urlparse
    candidate = url if "://" in url else f"http://{url}"
    try:
        host = (urlparse(candidate).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # 非 IP 的域名一律视为非本地
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified



def _convert_non_stream_to_sse(resp_body: bytes) -> str:
    """将上游非流式 JSON 响应转为 OpenAI 流式 SSE chunk 格式。

    客户端发 stream=true 但 force_stream=false 时，上游返回的是完整
    chat.completion 对象（message 字段），而客户端按 delta 解析。
    此处拆成 delta chunk + finish chunk + usage chunk + [DONE]。
    """
    import json as _json
    import time as _time

    try:
        data = _json.loads(resp_body.decode("utf-8"))
    except Exception:
        return f"data: {resp_body.decode('utf-8', errors='replace')}\n\ndata: [DONE]\n\n"

    if not isinstance(data, dict):
        return f"data: {resp_body.decode('utf-8', errors='replace')}\n\ndata: [DONE]\n\n"

    # 如果已经是流式格式（chunk），直接透传
    if data.get("object") == "chat.completion.chunk":
        return f"data: {resp_body.decode('utf-8')}\n\ndata: [DONE]\n\n"

    rid = data.get("id", "")
    model = data.get("model", "")
    created = data.get("created", int(_time.time()))
    usage = data.get("usage")
    choices = data.get("choices", [])

    chunks: list[str] = []

    if choices and len(choices) > 0:
        choice = choices[0]
        message = choice.get("message", {})
        finish_reason = choice.get("finish_reason", "stop")

        # 构建 delta（内容块）
        delta: dict = {}
        content = message.get("content")
        if content:
            delta["content"] = content
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if reasoning:
            delta["reasoning_content"] = reasoning
        tool_calls = message.get("tool_calls")
        if tool_calls:
            delta["tool_calls"] = tool_calls

        delta_chunk = {
            "id": rid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}]
        }
        chunks.append(f"data: {_json.dumps(delta_chunk, ensure_ascii=False)}\n\n")

        # finish chunk
        finish_chunk = {
            "id": rid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]
        }
        chunks.append(f"data: {_json.dumps(finish_chunk, ensure_ascii=False)}\n\n")

    # usage chunk
    if usage:
        usage_chunk = {
            "id": rid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": usage
        }
        chunks.append(f"data: {_json.dumps(usage_chunk, ensure_ascii=False)}\n\n")

    chunks.append("data: [DONE]\n\n")
    return "".join(chunks)
