"""
Direct API - Gemini 原生 API 处理
"""
import logging
import uuid
from typing import Optional

from fastapi import HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

from core import stream_stop
from core.constants import resolve_first_chunk_timeout, resolve_heartbeat_interval
from utils.monitor_params import build_monitor_request_params
from utils.upstream_fields import configured_body_params, monitored_body_params
from utils.usage_tokens import (
    extract_cached_tokens,
    extract_prompt_tokens,
    get_completion_tokens_mode,
    total_output_tokens,
)
from converters.gemini_generate import convert_gemini_to_openai_response
from converters.gemini_interactions import convert_interactions_to_openai_response
from converters.message_shape import inline_mid_system, rewrite_trailing_prefill
from core.conversion_report import ConversionRejected, note_loss, raise_if_strict
from services.model_capabilities import (
    budget_to_effort,
    drop_unsupported_sampling,
    limits_for,
)
from services.thinking_split_cache import (
    remember_split_original,
    restore_history_messages,
)
from ._direct_api_gemini_stream import (
    GenerateContentStreamSession,
    InteractionsStreamSession,
    gemini_cost,
    upstream_signatures,
)
from ._direct_api_utils import (
    build_response_message,
    estimate_message_tokens_non_blocking,
    estimate_text_tokens_non_blocking,
    peek_stream,
    map_upstream_error_to_status_code,
    record_request_failure,
)

logger = logging.getLogger(__name__)


def _generation_extras(openai_req: dict, sampling: dict, accepts_sampling: bool) -> dict:
    """Chat 字段里 generationConfig 也有对应项的部分。"""
    extras = {}
    if sampling.get("top_k") is not None:
        extras["topK"] = sampling["top_k"]
    if openai_req.get("seed") is not None:
        extras["seed"] = openai_req["seed"]
    # 很多客户端默认带 0，部分模型一见到 penalty 字段就 400，只转发真正想生效的值。
    for field, name in (("presence_penalty", "presencePenalty"), ("frequency_penalty", "frequencyPenalty")):
        if openai_req.get(field) and accepts_sampling:
            extras[name] = openai_req[field]
        elif openai_req.get(field):
            note_loss(field, "此模型不再接受采样类参数，未发送到上游")
    modalities = openai_req.get("modalities")
    if isinstance(modalities, list) and modalities:
        extras["responseModalities"] = [str(item).upper() for item in modalities]
    if (openai_req.get("n") or 1) > 1:
        note_loss("n", "Gemini 3 起不支持多候选，只返回一个")
    if openai_req.get("logprobs"):
        note_loss("logprobs", "Gemini 链路不返回 logprobs")
    return extras


def _thinking_config(openai_req: dict, endpoint_config: dict, target_model_id: str, limits) -> Optional[dict]:
    """enable_thinking 为 None/缺失且客户端没要求思考时不发送 thinking_config。"""
    client_reasoning_effort = openai_req.get("reasoning_effort")
    enable_thinking = endpoint_config.get("enable_thinking")
    if enable_thinking is None and client_reasoning_effort:
        enable_thinking = True
    if enable_thinking is False:
        logger.info(f"[GEMINI_NATIVE] 已显式关闭思维链模式")
        return {"includeThoughts": False}
    if enable_thinking is not True:
        return None

    reasoning_effort = client_reasoning_effort or endpoint_config.get("reasoning_effort")
    thinking_config = {"includeThoughts": True}
    if reasoning_effort:
        # 按用户选择的控制方式传参，不根据模型名称把强度换算成预算。
        # Gemini 的档位原样透传，只有其他协议的 none/max 需要换成最接近的档。
        level_map = {"none": "MINIMAL", "max": "HIGH"}
        effort = str(reasoning_effort).lower()
        thinking_config["thinkingLevel"] = level_map.get(effort, effort.upper())
        if effort == "none":
            # Gemini 没有“关闭”这一档：取最低档，并且不要思考摘要。
            thinking_config["includeThoughts"] = False
        logger.info(
            f"[GEMINI_NATIVE] reasoning_effort={reasoning_effort} "
            f"映射为 thinkingLevel={thinking_config['thinkingLevel']}"
        )
    elif endpoint_config.get("thinking_level"):
        thinking_config["thinkingLevel"] = str(endpoint_config["thinking_level"]).upper()
    elif endpoint_config.get("thinking_budget") is not None:
        budget = endpoint_config["thinking_budget"]
        if limits.thinking_budget:
            thinking_config["thinkingBudget"] = budget
        else:
            # 3.6 起用等级取代预算，配置还没改时取最接近的一档，不让请求失败。
            level = {"none": "minimal"}.get(budget_to_effort(budget), budget_to_effort(budget))
            thinking_config["thinkingLevel"] = level.upper()
            note_loss("thinking_budget", f"{target_model_id} 不接受思考预算，已换成 {level} 等级")
    logger.info(
        f"[GEMINI_NATIVE] 已启用思维链模式 "
        f"(control={thinking_config.get('thinkingLevel', thinking_config.get('thinkingBudget'))})"
    )
    return thinking_config


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
    CONFIG: Optional[dict] = None,
    thinking_separator: Optional[str] = None,
):
    """处理Gemini原生API的Direct请求"""
    logger.info(f"[GEMINI_NATIVE] 使用Gemini原生API格式")

    request_id = str(uuid.uuid4())

    # 思考 token 是否并入下发给下游的 completion_tokens（模型级配置）
    completion_tokens_mode = get_completion_tokens_mode(endpoint_config)
    extra_body = configured_body_params(endpoint_config, "GEMINI_NATIVE")
    limits = limits_for(target_model_id)
    thinking_config = _thinking_config(openai_req, endpoint_config, target_model_id, limits)

    monitor_extra_params: dict = {"upstream_model": target_model_id, **monitored_body_params(endpoint_config)}
    if thinking_config:
        monitor_extra_params["thinking_config"] = thinking_config
    # 记录思考与续传相关配置到监控日志
    for key in ("enable_thinking", "reasoning_effort", "thinking_budget", "force_stream", "gemini_max_continuation_hops"):
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

    failure_context = dict(
        display_name=display_name, messages=openai_req.get("messages", []), pricing_config=pricing_config,
        estimate_message_tokens_func=estimate_message_tokens_func, full_messages=full_messages, log_tag="GEMINI_NATIVE")

    try:
        is_stream = openai_req.get("stream", False)
        logger.info(f"[GEMINI_NATIVE] 模型映射: '{model_name}' -> '{target_model_id}'")

        # 客户端剥离思考后回传时，按正文还原上游原本生成的完整内容
        messages_for_upstream = openai_req.get("messages", [])
        if thinking_separator and isinstance(messages_for_upstream, list):
            messages_for_upstream = restore_history_messages(messages_for_upstream)

        # 文件引用只在发送前展开，监控和本地 Token 估算继续使用短引用。
        from services.audio_input import resolve_audio_references
        messages_for_upstream = await resolve_audio_references(
            messages_for_upstream, direct_api_service.session, api_key, api_base_url, endpoint_config)

        # Gemini 没有中段系统角色：留在原位转成 user 文本，systemInstruction 才能保持稳定。
        messages_for_upstream = rewrite_trailing_prefill(
            inline_mid_system(messages_for_upstream, endpoint_config.get("mid_system_mode")), limits.prefill)
        sampling = drop_unsupported_sampling(
            {key: openai_req[key] for key in ("temperature", "top_p", "top_k") if openai_req.get(key) is not None},
            target_model_id)
        generation_extras = _generation_extras(openai_req, sampling, limits.sampling)
        try:
            raise_if_strict(endpoint_config)
        except ConversionRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        stop_value = openai_req.get("stop")
        upstream_kwargs = dict(
            api_key=api_key,
            model=target_model_id,
            messages=messages_for_upstream,
            stream=is_stream,
            temperature=sampling.get("temperature"),
            top_p=sampling.get("top_p"),
            max_tokens=openai_req.get("max_tokens") or openai_req.get("max_completion_tokens"),
            base_url=api_base_url,
            thinking_config=thinking_config,
            tools=openai_req.get("tools"),
            tool_choice=openai_req.get("tool_choice"),
            response_format=openai_req.get("response_format"),
            stop_sequences=[stop_value] if isinstance(stop_value, str) else (
                stop_value if isinstance(stop_value, list) else None),
            extra_body=extra_body or None,
        )
        # 🔧 上游协议选择：generate_content（默认，旧行为）或 interactions（新 API）
        upstream_protocol = endpoint_config.get("upstream_protocol", "generate_content")
        if upstream_protocol == "interactions":
            logger.info(f"[GEMINI_NATIVE] 使用 Interactions 上游协议")
            gemini_generator = direct_api_service.call_gemini_interactions_api(**upstream_kwargs)
        else:
            gemini_generator = direct_api_service.call_gemini_native_api(
                **upstream_kwargs, generation_extras=generation_extras)

        async def upstream_error_response(payload):
            """上游在输出之前就报错：用真实状态码回给客户端，限流和超长的自动恢复才能触发。"""
            # 生成被上游终止（安全拦截等）时响应里仍有已计费的 usageMetadata
            usage_meta = payload.get("usageMetadata")
            usage_meta = usage_meta if isinstance(usage_meta, dict) else {}
            partial_input_tokens = int(usage_meta.get("promptTokenCount") or 0)
            partial_output_tokens = (int(usage_meta.get("candidatesTokenCount") or 0)
                                     + int(usage_meta.get("thoughtsTokenCount") or 0))
            await record_request_failure(
                monitoring_service, direct_api_service, request_id, str(payload.get("error")),
                **failure_context, input_tokens=partial_input_tokens, output_tokens=partial_output_tokens,
                cost_info=gemini_cost(direct_api_service, pricing_config, partial_input_tokens,
                                      partial_output_tokens, int(usage_meta.get("cachedContentTokenCount") or 0),
                                      usage_meta) if partial_input_tokens else None,
                upstream_usage=usage_meta or None)
            return JSONResponse(
                status_code=map_upstream_error_to_status_code(payload, default_status_code=500), content=payload)

        if is_stream:
            stop = stream_stop.register(request_id)
            first_event, chunks = await peek_stream(
                gemini_generator, resolve_heartbeat_interval(endpoint_config), stop=stop)
            if isinstance(first_event, dict) and "error" in first_event:
                await gemini_generator.aclose()
                return await upstream_error_response(first_event)

            session_class = (InteractionsStreamSession if upstream_protocol == "interactions"
                             else GenerateContentStreamSession)
            session = session_class(
                request_id=request_id, display_name=display_name, openai_req=openai_req,
                full_messages=full_messages, pricing_config=pricing_config,
                completion_tokens_mode=completion_tokens_mode, thinking_separator=thinking_separator,
                monitoring_service=monitoring_service, direct_api_service=direct_api_service,
                estimate_message_tokens_func=estimate_message_tokens_func,
                estimate_tokens_func=estimate_tokens_func,
                chunks=chunks, upstream=gemini_generator, stop=stop,
                first_chunk_timeout=resolve_first_chunk_timeout(CONFIG, endpoint_config))
            return StreamingResponse(
                session.stream(),
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
                return await upstream_error_response(gemini_response)

            if upstream_protocol == "interactions":
                openai_response = convert_interactions_to_openai_response(
                    gemini_response, display_name, request_id, completion_tokens_mode)
            else:
                openai_response = convert_gemini_to_openai_response(
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

                # 应用思考内容分隔符（仅在无原生 reasoning 且用户配置了分隔符时生效）
                if (thinking_separator and isinstance(response_content, str)
                        and response_content and not reasoning_content):
                    original_content = response_content
                    reasoning_content, response_content = direct_api_service.split_thinking_content(
                        response_content, thinking_separator)
                    if reasoning_content:
                        message["content"] = response_content
                        message["reasoning_content"] = reasoning_content
                        # 原始 parts 仍是未切分的整段文本，保留会让下游
                        # Anthropic 转换忽略上面的思考/正文划分
                        metadata = message.get("provider_metadata")
                        if isinstance(metadata, dict):
                            metadata.pop("gemini_parts", None)
                        if response_content:
                            remember_split_original(response_content, original_content)
                        logger.info(
                            f"[THINKING_SPLIT] 非流式响应已应用思考分隔，"
                            f"reasoning={len(reasoning_content)}字符, "
                            f"content={len(response_content)}字符")

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
            cached_tokens = extract_cached_tokens(usage)

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

            cost_info = gemini_cost(
                direct_api_service, pricing_config, input_tokens, output_tokens, cached_tokens, upstream_usage)

            monitoring_service.request_end(
                request_id=request_id, success=True,
                input_tokens=input_tokens, output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                response_content=response_content,
                reasoning_content=reasoning_content,
                cost_info=cost_info, full_messages=full_messages,
                response_message=build_response_message(response_content, reasoning_content, response_tool_calls,
                                                        upstream_signatures(gemini_response)),
                response_tool_calls=response_tool_calls,
                upstream_usage=upstream_usage)

            logger.info(f"[GEMINI_NATIVE] 非流式请求完成: {request_id[:8]}")
            logger.info(f"  - 输入tokens: {input_tokens}, 输出tokens: {output_tokens}")
            if cost_info.get("total_cost"):
                logger.info(f"  - 总成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")

            return JSONResponse(content=openai_response)

    except HTTPException as http_exc:
        logger.error(f"[GEMINI_NATIVE] 请求处理失败(HTTPException): {http_exc}", exc_info=True)
        await record_request_failure(
            monitoring_service, direct_api_service, request_id,
            str(http_exc.detail) if http_exc.detail is not None else str(http_exc), **failure_context)
        raise

    except Exception as e:
        logger.error(f"[GEMINI_NATIVE] 请求处理失败: {e}", exc_info=True)
        await record_request_failure(monitoring_service, direct_api_service, request_id, str(e), **failure_context)
        raise HTTPException(status_code=500, detail=f"Gemini Native API调用失败: {str(e)}")
