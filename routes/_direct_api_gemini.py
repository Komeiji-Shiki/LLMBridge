"""
Direct API - Gemini 原生 API 处理
"""
import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

from core.constants import resolve_first_chunk_timeout, resolve_heartbeat_interval
from utils.monitor_params import build_monitor_request_params
from utils.usage_tokens import (
    compose_chat_usage,
    extract_cached_tokens,
    extract_prompt_tokens,
    extract_reasoning_tokens,
    get_completion_tokens_mode,
    resolve_usage_tokens,
    total_output_tokens,
)
from converters.gemini_interactions import (
    InteractionsStreamConverter,
    convert_interactions_to_openai_response,
)
from converters.message_shape import inline_mid_system, rewrite_trailing_prefill
from core.conversion_report import ConversionRejected, note_loss, raise_if_strict
from services.model_capabilities import (
    budget_to_effort,
    drop_unsupported_sampling,
    limits_for,
    supported_thinking_level,
)
from services.thinking_split_cache import (
    remember_split_original,
    restore_history_messages,
)
from ._direct_api_utils import (
    append_tool_call_delta,
    build_response_message,
    cancel_and_wait,
    estimate_message_tokens_non_blocking,
    estimate_text_tokens_non_blocking,
    finalize_tool_calls,
    peek_stream,
    map_upstream_error_to_status_code,
    record_request_failure,
)

logger = logging.getLogger(__name__)


def _gemini_cost(direct_api_service, pricing_config, input_tokens, output_tokens, cached_tokens, upstream_usage):
    """续传的每一跳都按完整提示计费；展示用的 input_tokens 只算第一跳。"""
    if not pricing_config:
        return {}
    billed = upstream_usage or {}
    return direct_api_service.calculate_cost(
        input_tokens=billed.get("billedPromptTokenCount", input_tokens), output_tokens=output_tokens,
        cached_tokens=billed.get("billedCachedTokenCount", cached_tokens),
        pricing=pricing_config, tier_input_tokens=input_tokens)


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


def upstream_signatures(response: dict) -> list:
    """按顺序取出上游响应里的思考签名，供监控展示。

    generateContent 的签名挂在首个候选的内容块上，Interactions 的挂在 steps 上。
    """
    candidates = response.get("candidates") or [{}]
    content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    items = (parts if isinstance(parts, list) else []) + (response.get("steps") or [])
    return [item.get("thoughtSignature") or item.get("signature") for item in items
            if isinstance(item, dict) and (item.get("thoughtSignature") or item.get("signature"))]


class ThinkingSeparatorStreamSplitter:
    """把正文里夹带的思考内容按 thinking_separator 改写成 reasoning_content。

    与透传链路的 PassthroughStreamSession 同语义：分隔符出现前，把已确认
    不可能是分隔符前缀的内容先下发为 reasoning_content，尾部 sep_len 个字符
    留在缓冲里，避免分隔符被拆到两个 chunk 时误判。上游已返回原生
    reasoning_content 时直接停用，避免把正常正文误判成思考。
    """

    def __init__(self, separator: Optional[str]):
        self.separator = separator or ""
        self.enabled = bool(self.separator)
        self.done = False
        self._accumulated = ""
        self._output_position = 0
        # 切分前的完整正文，用于历史回传时还原上游原文
        self.original_content = ""

    @staticmethod
    def _drop_gemini_parts(delta: dict) -> None:
        """改写 content 后作废原始 parts。

        下游 Anthropic 转换器优先按 provider_metadata.gemini_parts 展开，
        保留它会让改写后的 reasoning_content/content 被忽略，全文仍按正文下发。
        """
        metadata = delta.get("provider_metadata")
        if isinstance(metadata, dict):
            metadata.pop("gemini_parts", None)

    def process(self, delta: dict) -> bool:
        """原地改写 delta，返回 True 表示该 chunk 应整体跳过。"""
        raw_content = delta.get("content")
        # 分隔符出现后仍继续累积，保存上游完整原文（含分隔符本身）
        if self.separator and isinstance(raw_content, str) and raw_content:
            self.original_content += raw_content

        if not self.enabled or self.done:
            return False
        if delta.get("reasoning_content"):
            # 上游自带原生思考，分隔符不再参与切分
            self.enabled = False
            return False

        if not isinstance(raw_content, str) or not raw_content:
            return False

        self._accumulated += raw_content

        if self.separator in self._accumulated:
            self.done = True
            reasoning_part, _, content_part = self._accumulated.partition(self.separator)
            remaining_reasoning = reasoning_part[self._output_position:]

            delta.pop("content", None)
            self._drop_gemini_parts(delta)
            if remaining_reasoning:
                delta["reasoning_content"] = remaining_reasoning
            if content_part:
                delta["content"] = content_part

            logger.info(f"[THINKING_SPLIT_STREAM] 检测到分隔符'{self.separator}'")
            logger.info(f"  - 思考总长: {len(reasoning_part)} 字符")
            logger.info(f"  - 本次输出reasoning: {len(remaining_reasoning)} 字符")
            logger.info(f"  - 正文部分: {len(content_part)} 字符")
            return not delta

        safe_position = max(
            self._output_position,
            len(self._accumulated) - len(self.separator))
        if safe_position <= self._output_position:
            # 整个增量都可能属于分隔符前缀，先扣住不发
            delta.pop("content", None)
            self._drop_gemini_parts(delta)
            return not delta

        safe_content = self._accumulated[self._output_position:safe_position]
        self._output_position = safe_position
        delta.pop("content", None)
        self._drop_gemini_parts(delta)
        delta["reasoning_content"] = safe_content
        return False

    def flush(self) -> str:
        """流结束时仍未出现分隔符：交还扣住的尾部，避免末尾字符丢失。"""
        if not self.enabled or self.done:
            return ""
        remaining = self._accumulated[self._output_position:]
        self._output_position = len(self._accumulated)
        if remaining:
            logger.warning(
                f"[THINKING_SPLIT_STREAM] 流结束仍未检测到分隔符"
                f"'{self.separator}'，剩余 {len(remaining)} 字符按思考内容下发")
        return remaining


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

    # 思考 token 是否并入下发给下游的 completion_tokens（模型级配置，
    # 两个内嵌生成器通过闭包读取）
    completion_tokens_mode = get_completion_tokens_mode(endpoint_config)

    extra_kwargs = {}
    custom_params = endpoint_config.get("custom_params", {})
    if custom_params and isinstance(custom_params, dict):
        extra_kwargs.update(custom_params)
        logger.info(f"[GEMINI_NATIVE] 已添加自定义参数:")
        for key, value in custom_params.items():
            logger.info(f"  - {key}: {value}")

    # 合并附加主体参数（在自定义参数之后，同名键优先）
    extra_body_params = endpoint_config.get("extra_body_params", {})
    if extra_body_params and isinstance(extra_body_params, dict):
        extra_kwargs.update(extra_body_params)
        logger.info(f"[GEMINI_NATIVE] 已添加附加主体参数:")
        for key, value in extra_body_params.items():
            logger.info(f"  - {key}: {value}")

    thinking_config = None
    limits = limits_for(target_model_id)
    client_reasoning_effort = openai_req.get("reasoning_effort")
    enable_thinking = endpoint_config.get("enable_thinking")
    if enable_thinking is None and client_reasoning_effort:
        enable_thinking = True
    if enable_thinking is True:
        reasoning_effort = client_reasoning_effort or endpoint_config.get("reasoning_effort")
        thinking_config = {"includeThoughts": True}
        if reasoning_effort:
            # 按用户选择的控制方式传参，不根据模型名称把强度换算成预算。
            level_map = {
                "none": "MINIMAL",
                "minimal": "MINIMAL",
                "low": "LOW",
                "medium": "MEDIUM",
                "high": "HIGH",
                "xhigh": "HIGH",
                "max": "HIGH",
            }
            effort = str(reasoning_effort).lower()
            level = level_map.get(effort, effort.upper())
            thinking_config["thinkingLevel"] = supported_thinking_level(target_model_id, level.lower()).upper()
            if effort == "none":
                # Gemini 没有“关闭”这一档：取最低档，并且不要思考摘要。
                thinking_config["includeThoughts"] = False
            logger.info(
                f"[GEMINI_NATIVE] reasoning_effort={reasoning_effort} "
                f"映射为 thinkingLevel={thinking_config['thinkingLevel']}"
            )
        elif endpoint_config.get("thinking_level"):
            level = str(endpoint_config["thinking_level"]).lower()
            thinking_config["thinkingLevel"] = supported_thinking_level(target_model_id, level).upper()
        elif endpoint_config.get("thinking_budget") is not None:
            budget = endpoint_config["thinking_budget"]
            if limits.thinking_budget:
                thinking_config["thinkingBudget"] = budget
            else:
                # 3.6 起用等级取代预算，配置还没改时取最接近的一档，不让请求失败。
                level = {"none": "minimal"}.get(budget_to_effort(budget), budget_to_effort(budget))
                thinking_config["thinkingLevel"] = supported_thinking_level(target_model_id, level).upper()
                note_loss("thinking_budget", f"{target_model_id} 不接受思考预算，已换成 {level} 等级")
        logger.info(
            f"[GEMINI_NATIVE] 已启用思维链模式 "
            f"(control={thinking_config.get('thinkingLevel', thinking_config.get('thinkingBudget'))})"
        )
    elif enable_thinking is False:
        thinking_config = {
            "includeThoughts": False
        }
        logger.info(f"[GEMINI_NATIVE] 已显式关闭思维链模式")
    # enable_thinking 为 None/缺失时不发送 thinking_config

    # 自定义参数/附加主体参数以独立字段记录到监控日志，避免平铺覆盖监控字段
    # （显式 dict 注解：后续会塞入 dict 类型的值，与 _direct_api_passthrough 写法对齐）
    monitor_extra_params: dict = {"upstream_model": target_model_id}
    if isinstance(custom_params, dict) and custom_params:
        monitor_extra_params["custom_params"] = custom_params
    if isinstance(extra_body_params, dict) and extra_body_params:
        monitor_extra_params["extra_body_params"] = extra_body_params
    if thinking_config:
        monitor_extra_params["thinking_config"] = thinking_config
    # 记录思考相关配置到监控日志
    for key in ("enable_thinking", "reasoning_effort", "thinking_budget", "force_stream"):
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

    await monitoring_service.broadcast_to_monitors({
        "type": "request_start",
        "request_id": request_id,
        "model": display_name,
        "timestamp": time.time()
    })

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

        # 🔧 上游协议选择：generate_content（默认，旧行为）或 interactions（新 API）
        upstream_protocol = endpoint_config.get("upstream_protocol", "generate_content")
        if upstream_protocol == "interactions":
            logger.info(f"[GEMINI_NATIVE] 使用 Interactions 上游协议")
            gemini_generator = direct_api_service.call_gemini_interactions_api(
                api_key=api_key,
                model=target_model_id,
                messages=messages_for_upstream,
                stream=is_stream,
                temperature=sampling.get("temperature"),
                top_p=sampling.get("top_p"),
                max_tokens=(
                    openai_req.get("max_tokens")
                    or openai_req.get("max_completion_tokens")
                ),
                base_url=api_base_url,
                thinking_config=thinking_config,
                tools=openai_req.get("tools"),
                tool_choice=openai_req.get("tool_choice"),
                response_format=openai_req.get("response_format"),
                stop_sequences=(
                    openai_req.get("stop")
                    if isinstance(openai_req.get("stop"), list)
                    else ([openai_req["stop"]] if isinstance(openai_req.get("stop"), str) else None)
                ),
                extra_body=extra_kwargs if extra_kwargs else None,
            )
        else:
            gemini_generator = direct_api_service.call_gemini_native_api(
                api_key=api_key,
                model=target_model_id,
                messages=messages_for_upstream,
                stream=is_stream,
                temperature=sampling.get("temperature"),
                top_p=sampling.get("top_p"),
                max_tokens=(
                    openai_req.get("max_tokens")
                    or openai_req.get("max_completion_tokens")
                ),
                base_url=api_base_url,
                thinking_config=thinking_config,
                tools=openai_req.get("tools"),
                tool_choice=openai_req.get("tool_choice"),
                response_format=openai_req.get("response_format"),
                stop_sequences=([openai_req["stop"]] if isinstance(openai_req.get("stop"), str) else openai_req.get("stop")),
                extra_body=extra_kwargs if extra_kwargs else None,
                generation_extras=generation_extras,
            )

        first_chunk_timeout = resolve_first_chunk_timeout(CONFIG, endpoint_config)

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
                cost_info=_gemini_cost(direct_api_service, pricing_config, partial_input_tokens,
                                       partial_output_tokens, int(usage_meta.get("cachedContentTokenCount") or 0),
                                       usage_meta) if partial_input_tokens else None,
                upstream_usage=usage_meta or None)
            return JSONResponse(
                status_code=map_upstream_error_to_status_code(payload, default_status_code=500), content=payload)


        if is_stream:
            async def gemini_stream_generator():
                content_parts = []
                reasoning_parts = []
                tool_call_accumulator = {}
                tool_call_indices = {}
                seen_sources = set()
                input_tokens = 0
                output_tokens = 0
                total_tokens = 0
                reasoning_tokens = 0  # 思考 token（含在 output_tokens 里）
                cached_tokens = 0  # 缓存命中（含在 input_tokens 里）
                upstream_usage = None  # Gemini 原生 usageMetadata（原样保留，供日志记录）
                signatures = []
                request_success = False
                stream_completed = False
                finish_seen = False  # 结束原因已转发：此后客户端主动关流不算失败
                error_msg = None
                internal_error = None  # 网关自身处理异常，要在流尾告知客户端
                # 客户端已消失时 finally 不能再 yield（见下方 GeneratorExit 处理）
                client_gone = False
                # 首块超时/空响应/预读错误时，已在分支中 yield 了 error 事件；
                # 设此标志阻止 finally 再补发空的 usage/[DONE] 尾部块
                tail_suppressed = False
                first_chunk_seen = False
                splitter = ThinkingSeparatorStreamSplitter(thinking_separator)

                def consume(gemini_chunk):
                    """累积一个上游块，返回要转发的 SSE 文本（被思考分隔吞掉时为 None）。"""
                    nonlocal upstream_usage, input_tokens, output_tokens, reasoning_tokens
                    nonlocal cached_tokens, total_tokens, finish_seen
                    _meta = gemini_chunk.get("usageMetadata")
                    if isinstance(_meta, dict) and _meta:
                        upstream_usage = _meta
                    signatures.extend(upstream_signatures(gemini_chunk))
                    openai_chunk = direct_api_service.convert_gemini_response_to_openai(
                        gemini_chunk, display_name, request_id, is_stream_chunk=True,
                        completion_tokens_mode=completion_tokens_mode, tool_call_indices=tool_call_indices,
                        seen_sources=seen_sources)

                    choice = openai_chunk.get("choices", [{}])[0]
                    finish_seen = finish_seen or bool(choice.get("finish_reason"))
                    delta = choice.get("delta", {})
                    suppress_chunk = splitter.process(delta)
                    delta_content = delta.get("content", "")
                    delta_reasoning = delta.get("reasoning_content", "")
                    delta_tool_calls = delta.get("tool_calls")

                    if delta_content:
                        content_parts.append(delta_content)
                    if delta_reasoning:
                        reasoning_parts.append(delta_reasoning)
                    if delta_tool_calls:
                        append_tool_call_delta(tool_call_accumulator, delta_tool_calls)

                    if "usage" in openai_chunk and openai_chunk["usage"]:
                        usage = openai_chunk["usage"]
                        input_tokens = extract_prompt_tokens(usage)
                        # 计费/监控按真实总输出（正文+思考），不受下游展示口径影响
                        output_tokens = total_output_tokens(usage)
                        reasoning_tokens = extract_reasoning_tokens(usage)
                        cached_tokens = extract_cached_tokens(usage)
                        total_tokens = resolve_usage_tokens(usage).total_tokens

                    return None if suppress_chunk else f"data: {json.dumps(openai_chunk, ensure_ascii=False)}\n\n"

                try:
                    yield f": keep-alive {int(time.time())}\n\n"
                    first_chunk_wait_start = time.time()

                    async for gemini_chunk in chunks:
                        if gemini_chunk is None:
                            if not first_chunk_seen and time.time() - first_chunk_wait_start > first_chunk_timeout:
                                error_msg = f"Gemini Native API返回空响应或在{first_chunk_timeout}秒内未返回第一个数据块"
                                logger.error(f"[GEMINI_NATIVE] {error_msg}")
                                # 向客户端发送错误事件，避免客户端看到空 SSE 流以为成功
                                yield f"data: {json.dumps({'error': {'message': error_msg, 'type': 'timeout'}}, ensure_ascii=False)}\n\n"
                                tail_suppressed = True
                                return
                            # 续传跳之间模型可能长时间静默，心跳覆盖整条流
                            yield f": keep-alive {int(time.time())}\n\n"
                            continue

                        if "error" in gemini_chunk:
                            error_msg = str(gemini_chunk.get("error"))
                            yield f"data: {json.dumps({'error': gemini_chunk['error']}, ensure_ascii=False)}\n\n"
                            if not first_chunk_seen:
                                logger.error(f"[GEMINI_NATIVE] 流式预读检测到错误: {gemini_chunk}")
                                yield "data: [DONE]\n\n"
                                tail_suppressed = True
                                return
                            break

                        first_chunk_seen = True
                        sse_text = consume(gemini_chunk)
                        if sse_text:
                            yield sse_text

                    if not first_chunk_seen:
                        error_msg = f"Gemini Native API返回空响应或在{first_chunk_timeout}秒内未返回第一个数据块"
                        logger.error(f"[GEMINI_NATIVE] {error_msg}")
                        yield f"data: {json.dumps({'error': {'message': error_msg, 'type': 'empty_response'}}, ensure_ascii=False)}\n\n"
                        tail_suppressed = True
                        return

                    if error_msg is None:
                        tail_text = splitter.flush()
                        if tail_text:
                            reasoning_parts.append(tail_text)
                            tail_chunk = {
                                "id": request_id,
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": display_name,
                                "choices": [{
                                    "index": 0,
                                    "delta": {"reasoning_content": tail_text},
                                    "finish_reason": None
                                }],
                                "usage": None
                            }
                            if not client_gone:
                                yield f"data: {json.dumps(tail_chunk, ensure_ascii=False)}\n\n"

                    stream_completed = (error_msg is None)
                    request_success = (error_msg is None)

                except (GeneratorExit, asyncio.CancelledError):
                    # 必须重新抛出：GeneratorExit 被吞会报 "async generator ignored GeneratorExit"，
                    # CancelledError 被吞会让请求取消失效
                    client_gone = True
                    # 安全拦截也算结束原因，转换层收到错误后关流时不能因此记成成功
                    if (stream_completed or finish_seen) and not error_msg:
                        request_success = True
                        logger.info(f"[GEMINI_NATIVE] 客户端关闭连接，但结束原因已转发，标记为成功")
                    elif not error_msg:
                        error_msg = "Client disconnected"
                        logger.warning(f"[GEMINI_NATIVE] 客户端在流结束前断开: {request_id[:8]}")
                    raise
                except Exception as e:
                    error_msg = internal_error = str(e)
                    logger.error(f"[GEMINI_NATIVE] 流式处理失败: {e}", exc_info=True)
                finally:
                    if input_tokens == 0 or output_tokens == 0:
                        # 🔧 修复：旧版这里硬编码模型名 "gemma"，绕过了
                        # config.jsonc 中 tokenizer_config 的按模型映射，
                        # 与同文件非流式分支（用 display_name）结果不一致
                        try:
                            if input_tokens == 0:
                                input_tokens = await asyncio.shield(estimate_message_tokens_non_blocking(
                                    estimate_message_tokens_func,
                                    openai_req.get('messages', []), display_name))
                            if output_tokens == 0:
                                accumulated_content = ''.join(content_parts)
                                accumulated_reasoning = ''.join(reasoning_parts)
                                total_output_text = accumulated_reasoning + accumulated_content
                                if total_output_text:
                                    output_tokens = await asyncio.shield(estimate_text_tokens_non_blocking(
                                        estimate_tokens_func, total_output_text, display_name))
                        except asyncio.CancelledError:
                            # shield 也会在取消态下抛出 CancelledError，此时保持 0 值
                            logger.warning(f"[GEMINI_NATIVE] Token 估算被取消，使用上游返回的 usage 值")
                        except Exception as token_error:
                            logger.error(f"[GEMINI_NATIVE] Token计算失败: {token_error}")

                    cost_info = _gemini_cost(
                        direct_api_service, pricing_config, input_tokens, output_tokens, cached_tokens, upstream_usage)
                    final_content = ''.join(content_parts)
                    final_reasoning = ''.join(reasoning_parts)
                    final_tool_calls = finalize_tool_calls(tool_call_accumulator)

                    if (request_success and splitter.done
                            and splitter.original_content and final_content):
                        remember_split_original(final_content, splitter.original_content)

                    monitoring_service.request_end(
                        request_id=request_id, success=request_success,
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        cached_tokens=cached_tokens, error=error_msg,
                        response_content=final_content,
                        reasoning_content=final_reasoning,
                        cost_info=cost_info, full_messages=full_messages,
                        response_message=build_response_message(final_content, final_reasoning, final_tool_calls, signatures),
                        response_tool_calls=final_tool_calls,
                        upstream_usage=upstream_usage)

                    await monitoring_service.broadcast_to_monitors({
                        "type": "request_end", "request_id": request_id,
                        "success": request_success})

                    logger.info(f"[GEMINI_NATIVE] 流式请求完成: {request_id[:8]}")
                    logger.info(f"  - 输入tokens: {input_tokens}, 输出tokens: {output_tokens}")
                    if cost_info.get("total_cost"):
                        logger.info(f"  - 总成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")

                    # 客户端走了就不能再续传，及时释放上游连接
                    await chunks.aclose()
                    await gemini_generator.aclose()

                    if not client_gone and not tail_suppressed:
                        try:
                            if internal_error:
                                yield f"data: {json.dumps({'error': {'message': internal_error, 'type': 'internal_error'}}, ensure_ascii=False)}\n\n"
                            final_total_tokens = total_tokens if total_tokens > 0 else (input_tokens + output_tokens)
                            usage_final_chunk = {
                                "id": f"chatcmpl-{request_id}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": display_name,
                                "choices": [],
                                "usage": compose_chat_usage(
                                    prompt_tokens=input_tokens,
                                    output_tokens=output_tokens,
                                    reasoning_tokens=reasoning_tokens,
                                    cached_tokens=cached_tokens,
                                    completion_mode=completion_tokens_mode,
                                    total_tokens=final_total_tokens,
                                )
                            }
                            yield f"data: {json.dumps(usage_final_chunk, ensure_ascii=False)}\n\n"
                            yield "data: [DONE]\n\n"
                        except Exception as yield_err:
                            logger.debug(f"[GEMINI_NATIVE] 发送 usage/[DONE] 时客户端已断开: {yield_err}")

            async def interactions_stream_generator():
                """Interactions 上游流式生成器：事件流 → OpenAI chunk 流。

                与 gemini_stream_generator 骨架一致（keep-alive/首块超时/取消/
                统计/尾部块），区别在于转换部分：interactions 是事件流（状态机
                InteractionsStreamConverter 跨事件维护 step 状态）。
                """
                content_parts = []
                reasoning_parts = []
                tool_call_accumulator = {}
                input_tokens = 0
                output_tokens = 0
                total_tokens = 0
                reasoning_tokens = 0  # 思考 token（含在 output_tokens 里）
                cached_tokens = 0  # 缓存命中（含在 input_tokens 里）
                upstream_usage = None  # interactions usage（interaction.completed 事件携带）
                request_success = False
                stream_completed = False
                error_msg = None
                client_gone = False
                tail_suppressed = False
                finish_seen = False  # 结束原因已转发：此后客户端主动关流不算失败
                converter = InteractionsStreamConverter(display_name, request_id, completion_tokens_mode)
                splitter = ThinkingSeparatorStreamSplitter(thinking_separator)

                first_event_seen = False

                def account(usage):
                    nonlocal input_tokens, output_tokens, reasoning_tokens, cached_tokens, total_tokens, upstream_usage
                    input_tokens = extract_prompt_tokens(usage)
                    # 计费/监控按真实总输出（正文+思考），不受下游展示口径影响
                    output_tokens = total_output_tokens(usage)
                    reasoning_tokens = extract_reasoning_tokens(usage)
                    cached_tokens = extract_cached_tokens(usage)
                    total_tokens = resolve_usage_tokens(usage).total_tokens
                    upstream_usage = converter.usage

                def account_failure(metadata):
                    """续传中途失败时，已经完成的各跳仍然要记账。"""
                    nonlocal input_tokens, output_tokens, cached_tokens, upstream_usage
                    if isinstance(metadata, dict) and metadata:
                        input_tokens = int(metadata.get("promptTokenCount") or 0)
                        output_tokens = (int(metadata.get("candidatesTokenCount") or 0)
                                         + int(metadata.get("thoughtsTokenCount") or 0))
                        cached_tokens = int(metadata.get("cachedContentTokenCount") or 0)
                        upstream_usage = metadata

                def consume(openai_chunk):
                    """累积一个转换后的块，返回要转发的 SSE 文本（被思考分隔吞掉时为 None）。"""
                    nonlocal finish_seen
                    choices = openai_chunk.get("choices") or []
                    finish_seen = finish_seen or bool(choices and choices[0].get("finish_reason"))
                    delta = choices[0].get("delta", {}) if choices else {}
                    suppress_chunk = splitter.process(delta)
                    delta_content = delta.get("content", "")
                    delta_reasoning = delta.get("reasoning_content", "")
                    delta_tool_calls = delta.get("tool_calls")

                    if delta_content:
                        content_parts.append(delta_content)
                    if delta_reasoning:
                        reasoning_parts.append(delta_reasoning)
                    if delta_tool_calls:
                        append_tool_call_delta(tool_call_accumulator, delta_tool_calls)
                    if openai_chunk.get("usage"):
                        account(openai_chunk["usage"])
                    return None if suppress_chunk else f"data: {json.dumps(openai_chunk, ensure_ascii=False)}\n\n"

                try:
                    yield f": keep-alive {int(time.time())}\n\n"
                    first_chunk_wait_start = time.time()

                    async for event in chunks:
                        if event is None:
                            if not first_event_seen and time.time() - first_chunk_wait_start > first_chunk_timeout:
                                error_msg = f"Gemini Interactions API返回空响应或在{first_chunk_timeout}秒内未返回第一个数据块"
                                logger.error(f"[GEMINI_INTERACTIONS] {error_msg}")
                                yield f"data: {json.dumps({'error': {'message': error_msg, 'type': 'timeout'}}, ensure_ascii=False)}\n\n"
                                tail_suppressed = True
                                return
                            # 长输出的检查点之间和续传跳之间模型可能长时间静默，心跳覆盖整条流
                            yield f": keep-alive {int(time.time())}\n\n"
                            continue

                        openai_chunks = converter.feed(event)
                        if openai_chunks and "error" in openai_chunks[0]:
                            error_msg = str(openai_chunks[0].get("error"))
                            account_failure(openai_chunks[0].get("usageMetadata"))
                            yield f"data: {json.dumps({'error': openai_chunks[0]['error']}, ensure_ascii=False)}\n\n"
                            if not first_event_seen:
                                logger.error(f"[GEMINI_INTERACTIONS] 流式预读检测到错误: {event}")
                                yield "data: [DONE]\n\n"
                                tail_suppressed = True
                                return
                            break

                        first_event_seen = True
                        for openai_chunk in openai_chunks:
                            sse_text = consume(openai_chunk)
                            if sse_text:
                                yield sse_text

                    if not first_event_seen:
                        error_msg = f"Gemini Interactions API返回空响应或在{first_chunk_timeout}秒内未返回第一个数据块"
                        logger.error(f"[GEMINI_INTERACTIONS] {error_msg}")
                        yield f"data: {json.dumps({'error': {'message': error_msg, 'type': 'empty_response'}}, ensure_ascii=False)}\n\n"
                        tail_suppressed = True
                        return

                    if error_msg is None and not converter.completed:
                        # 没收到结束事件说明上游中途断了，不能按成功收尾。
                        error_msg = "Gemini Interactions API 的响应流在结束事件之前中断"
                        logger.error(f"[GEMINI_INTERACTIONS] {error_msg}")
                        yield f"data: {json.dumps({'error': {'message': error_msg, 'type': 'upstream_error', 'code': 502}}, ensure_ascii=False)}\n\n"

                    if error_msg is None:
                        tail_text = splitter.flush()
                        if tail_text:
                            reasoning_parts.append(tail_text)
                            tail_chunk = {
                                "id": f"chatcmpl-{request_id}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": display_name,
                                "choices": [{
                                    "index": 0,
                                    "delta": {"reasoning_content": tail_text},
                                    "finish_reason": None
                                }],
                                "usage": None
                            }
                            if not client_gone:
                                yield f"data: {json.dumps(tail_chunk, ensure_ascii=False)}\n\n"

                    stream_completed = (error_msg is None)
                    request_success = (error_msg is None)

                except (GeneratorExit, asyncio.CancelledError):
                    client_gone = True
                    if (stream_completed or finish_seen) and not error_msg:
                        request_success = True
                        logger.info(f"[GEMINI_INTERACTIONS] 客户端关闭连接，但结束原因已转发，标记为成功")
                    elif not error_msg:
                        error_msg = "Client disconnected"
                        logger.warning(f"[GEMINI_INTERACTIONS] 客户端在流结束前断开: {request_id[:8]}")
                    raise
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"[GEMINI_INTERACTIONS] 流式处理失败: {e}", exc_info=True)
                finally:
                    # 中途断开也保存已收到的步骤，签名不随失败的那次请求一起丢。
                    converter.finalize()
                    if input_tokens == 0 or output_tokens == 0:
                        try:
                            if input_tokens == 0:
                                input_tokens = await asyncio.shield(estimate_message_tokens_non_blocking(
                                    estimate_message_tokens_func,
                                    openai_req.get('messages', []), display_name))
                            if output_tokens == 0:
                                accumulated_content = ''.join(content_parts)
                                accumulated_reasoning = ''.join(reasoning_parts)
                                total_output_text = accumulated_reasoning + accumulated_content
                                if total_output_text:
                                    output_tokens = await asyncio.shield(estimate_text_tokens_non_blocking(
                                        estimate_tokens_func, total_output_text, display_name))
                        except asyncio.CancelledError:
                            logger.warning(f"[GEMINI_INTERACTIONS] Token 估算被取消，使用上游返回的 usage 值")
                        except Exception as token_error:
                            logger.error(f"[GEMINI_INTERACTIONS] Token计算失败: {token_error}")

                    cost_info = _gemini_cost(
                        direct_api_service, pricing_config, input_tokens, output_tokens, cached_tokens, upstream_usage)
                    final_content = ''.join(content_parts)
                    final_reasoning = ''.join(reasoning_parts)
                    final_tool_calls = finalize_tool_calls(tool_call_accumulator)

                    if (request_success and splitter.done
                            and splitter.original_content and final_content):
                        remember_split_original(final_content, splitter.original_content)

                    monitoring_service.request_end(
                        request_id=request_id, success=request_success,
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        cached_tokens=cached_tokens, error=error_msg,
                        response_content=final_content,
                        reasoning_content=final_reasoning,
                        cost_info=cost_info, full_messages=full_messages,
                        response_message=build_response_message(final_content, final_reasoning, final_tool_calls, converter.signatures),
                        response_tool_calls=final_tool_calls,
                        upstream_usage=upstream_usage)

                    await monitoring_service.broadcast_to_monitors({
                        "type": "request_end", "request_id": request_id,
                        "success": request_success})

                    logger.info(f"[GEMINI_INTERACTIONS] 流式请求完成: {request_id[:8]}")
                    logger.info(f"  - 输入tokens: {input_tokens}, 输出tokens: {output_tokens}")
                    if cost_info.get("total_cost"):
                        logger.info(f"  - 总成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")

                    # 客户端走了就不能再续传，及时释放上游连接
                    await chunks.aclose()
                    await gemini_generator.aclose()

                    if not client_gone and not tail_suppressed:
                        try:
                            final_total_tokens = total_tokens if total_tokens > 0 else (input_tokens + output_tokens)
                            usage_final_chunk = {
                                "id": f"chatcmpl-{request_id}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": display_name,
                                "choices": [],
                                "usage": compose_chat_usage(
                                    prompt_tokens=input_tokens,
                                    output_tokens=output_tokens,
                                    reasoning_tokens=reasoning_tokens,
                                    cached_tokens=cached_tokens,
                                    completion_mode=completion_tokens_mode,
                                    total_tokens=final_total_tokens,
                                )
                            }
                            yield f"data: {json.dumps(usage_final_chunk, ensure_ascii=False)}\n\n"
                            yield "data: [DONE]\n\n"
                        except Exception as yield_err:
                            logger.debug(f"[GEMINI_INTERACTIONS] 发送 usage/[DONE] 时客户端已断开: {yield_err}")

            first_event, chunks = await peek_stream(gemini_generator, resolve_heartbeat_interval(endpoint_config))
            if isinstance(first_event, dict) and "error" in first_event:
                await gemini_generator.aclose()
                return await upstream_error_response(first_event)

            return StreamingResponse(
                interactions_stream_generator() if upstream_protocol == "interactions" else gemini_stream_generator(),
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
                openai_response = direct_api_service.convert_gemini_response_to_openai(
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

            cost_info = _gemini_cost(
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

            await monitoring_service.broadcast_to_monitors({
                "type": "request_end", "request_id": request_id, "success": True})

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
