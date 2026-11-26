"""
Direct API - Gemini 流式会话

generateContent 和 Interactions 两种上游共用一套流式骨架：首块超时、错误转发、
手动收尾、思考分隔、客户端断开判定、统计收尾和尾部 usage 块。子类只负责把上游的
一个单位（generateContent 的响应块 / Interactions 的事件）转换成 Chat 块。
"""
import asyncio
import json
import logging
import time
from typing import List, Optional

import anyio

from core import stream_stop
from converters.gemini_generate import convert_gemini_to_openai_response
from converters.gemini_interactions import InteractionsStreamConverter
from services.thinking_split_cache import remember_split_original
from utils.usage_tokens import (
    compose_chat_usage,
    extract_cached_tokens,
    extract_prompt_tokens,
    extract_reasoning_tokens,
    resolve_usage_tokens,
    total_output_tokens,
)
from ._direct_api_utils import (
    append_tool_call_delta,
    build_response_message,
    estimate_message_tokens_non_blocking,
    estimate_text_tokens_non_blocking,
    finalize_tool_calls,
    manual_finish_chunk,
)

logger = logging.getLogger(__name__)


def gemini_cost(direct_api_service, pricing_config, input_tokens, output_tokens, cached_tokens, upstream_usage):
    """续传的每一跳都按完整提示计费；展示用的 input_tokens 只算第一跳。"""
    if not pricing_config:
        return {}
    billed = upstream_usage or {}
    return direct_api_service.calculate_cost(
        input_tokens=billed.get("billedPromptTokenCount", input_tokens), output_tokens=output_tokens,
        cached_tokens=billed.get("billedCachedTokenCount", cached_tokens),
        pricing=pricing_config, tier_input_tokens=input_tokens)


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


def _sse(value: dict) -> str:
    return f"data: {json.dumps(value, ensure_ascii=False)}\n\n"


class GeminiStreamSession:
    """一次 Gemini 流式请求：累积下发的内容和用量，流结束时统一记账并补尾部块。"""

    log_tag = "GEMINI_NATIVE"
    api_name = "Gemini Native API"

    def __init__(self, *, request_id: str, display_name: str, openai_req: dict, full_messages: Optional[list],
                 pricing_config: dict, completion_tokens_mode: str, thinking_separator: Optional[str],
                 monitoring_service, direct_api_service, estimate_message_tokens_func, estimate_tokens_func,
                 chunks, upstream, stop: asyncio.Event, first_chunk_timeout: float):
        self.request_id = request_id
        self.display_name = display_name
        self.openai_req = openai_req
        self.full_messages = full_messages
        self.pricing_config = pricing_config
        self.completion_tokens_mode = completion_tokens_mode
        self.monitoring_service = monitoring_service
        self.direct_api_service = direct_api_service
        self.estimate_message_tokens_func = estimate_message_tokens_func
        self.estimate_tokens_func = estimate_tokens_func
        self.chunks = chunks
        self.upstream = upstream
        self.stop = stop
        self.first_chunk_timeout = first_chunk_timeout

        self.splitter = ThinkingSeparatorStreamSplitter(thinking_separator)
        self.content_parts: List[str] = []
        self.reasoning_parts: List[str] = []
        self.tool_call_accumulator: dict = {}
        self.input_tokens = 0
        self.output_tokens = 0  # 含思考
        self.total_tokens = 0
        self.reasoning_tokens = 0
        self.cached_tokens = 0  # 含在 input_tokens 里
        self.upstream_usage: Optional[dict] = None  # 上游原样的用量，供日志记录
        self.request_success = False
        self.stream_completed = False
        self.finish_seen = False  # 结束原因已转发：此后客户端主动关流不算失败
        self.first_seen = False
        self.error_msg: Optional[str] = None
        self.internal_error: Optional[str] = None  # 网关自身处理异常，要在流尾告知客户端
        # 客户端已消失时 finally 不能再 yield（见 stream 的 GeneratorExit 处理）
        self.client_gone = False
        # 首块超时/空响应/预读错误时已经发了 error 事件，不再补 usage/[DONE] 尾部块
        self.tail_suppressed = False

    # ---- 子类实现 ----

    @property
    def chunk_id(self) -> str:
        """补发的思考尾巴使用与正文块相同的 id。"""
        return self.request_id

    @property
    def signatures(self) -> list:
        raise NotImplementedError

    def convert(self, item) -> List[dict]:
        """上游的一个单位 → Chat 块；首个块带 error 时表示上游报错。"""
        raise NotImplementedError

    def upstream_finished(self) -> bool:
        raise NotImplementedError

    def manual_finish(self) -> List[str]:
        """手动收尾时补发的 SSE 文本。"""
        raise NotImplementedError

    def interrupted_error(self) -> Optional[str]:
        """上游没发结束信号就关流时的失败原因。"""
        return None

    def finalize(self) -> None:
        pass

    # ---- 累积 ----

    def account(self, usage: dict) -> None:
        self.input_tokens = extract_prompt_tokens(usage)
        # 计费/监控按真实总输出（正文+思考），不受下游展示口径影响
        self.output_tokens = total_output_tokens(usage)
        self.reasoning_tokens = extract_reasoning_tokens(usage)
        self.cached_tokens = extract_cached_tokens(usage)
        self.total_tokens = resolve_usage_tokens(usage).total_tokens

    def account_failure(self, metadata) -> None:
        """续传中途失败时，已经完成的各跳用量仍然计入统计。"""
        if isinstance(metadata, dict) and metadata:
            self.input_tokens = int(metadata.get("promptTokenCount") or 0)
            self.reasoning_tokens = int(metadata.get("thoughtsTokenCount") or 0)
            self.output_tokens = int(metadata.get("candidatesTokenCount") or 0) + self.reasoning_tokens
            self.cached_tokens = int(metadata.get("cachedContentTokenCount") or 0)
            self.total_tokens = int(metadata.get("totalTokenCount") or (self.input_tokens + self.output_tokens))
            self.upstream_usage = metadata

    def consume(self, openai_chunk: dict) -> Optional[str]:
        """累积一个 Chat 块，返回要转发的 SSE 文本（被思考分隔吞掉时为 None）。"""
        choices = openai_chunk.get("choices") or []
        self.finish_seen = self.finish_seen or bool(choices and choices[0].get("finish_reason"))
        delta = choices[0].get("delta", {}) if choices else {}
        suppress_chunk = self.splitter.process(delta)
        if delta.get("content"):
            self.content_parts.append(delta["content"])
        if delta.get("reasoning_content"):
            self.reasoning_parts.append(delta["reasoning_content"])
        if delta.get("tool_calls"):
            append_tool_call_delta(self.tool_call_accumulator, delta["tool_calls"])
        if openai_chunk.get("usage"):
            self.account(openai_chunk["usage"])
        return None if suppress_chunk else _sse(openai_chunk)

    def _empty_response_message(self) -> str:
        return f"{self.api_name}返回空响应或在{self.first_chunk_timeout}秒内未返回第一个数据块"

    # ---- 流 ----

    async def stream(self):
        try:
            first_chunk_wait_start = time.time()

            async for item in self.chunks:
                if item is None:
                    if not self.first_seen and time.time() - first_chunk_wait_start > self.first_chunk_timeout:
                        self.error_msg = self._empty_response_message()
                        logger.error(f"[{self.log_tag}] {self.error_msg}")
                        # 向客户端发送错误事件，避免客户端看到空 SSE 流以为成功
                        yield _sse({'error': {'message': self.error_msg, 'type': 'timeout'}})
                        self.tail_suppressed = True
                        return
                    # 空闲时只用来检查首块超时，心跳由 GatewayRequestMiddleware 统一补
                    continue

                openai_chunks = self.convert(item)
                if openai_chunks and "error" in openai_chunks[0]:
                    error = openai_chunks[0]
                    self.error_msg = str(error.get("error"))
                    self.account_failure(error.get("usageMetadata"))
                    yield _sse({'error': error['error']})
                    if not self.first_seen:
                        logger.error(f"[{self.log_tag}] 流式预读检测到错误: {item}")
                        yield "data: [DONE]\n\n"
                        self.tail_suppressed = True
                        return
                    break

                self.first_seen = True
                for openai_chunk in openai_chunks:
                    sse_text = self.consume(openai_chunk)
                    if sse_text:
                        yield sse_text

            if self.stop.is_set() and self.error_msg is None and not self.upstream_finished():
                # 手动收尾：上游迟迟不给结束信号，替它补上，已发出的工具调用照常交给客户端
                self.first_seen = True
                for sse_text in self.manual_finish():
                    yield sse_text

            if not self.first_seen:
                self.error_msg = self._empty_response_message()
                logger.error(f"[{self.log_tag}] {self.error_msg}")
                yield _sse({'error': {'message': self.error_msg, 'type': 'empty_response'}})
                self.tail_suppressed = True
                return

            if self.error_msg is None:
                interrupted = self.interrupted_error()
                if interrupted:
                    self.error_msg = interrupted
                    logger.error(f"[{self.log_tag}] {interrupted}")
                    yield _sse({'error': {'message': interrupted, 'type': 'upstream_error', 'code': 502}})

            if self.error_msg is None:
                tail_text = self.splitter.flush()
                if tail_text:
                    self.reasoning_parts.append(tail_text)
                    yield _sse({
                        "id": self.chunk_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": self.display_name,
                        "choices": [{
                            "index": 0,
                            "delta": {"reasoning_content": tail_text},
                            "finish_reason": None
                        }],
                        "usage": None
                    })

            self.stream_completed = (self.error_msg is None)
            self.request_success = (self.error_msg is None)

        except (GeneratorExit, asyncio.CancelledError):
            # 必须重新抛出：GeneratorExit 被吞会报 "async generator ignored GeneratorExit"，
            # CancelledError 被吞会让请求取消失效
            self.client_gone = True
            # 安全拦截也算结束原因，转换层收到错误后关流时不能因此记成成功
            if (self.stream_completed or self.finish_seen) and not self.error_msg:
                self.request_success = True
                logger.info(f"[{self.log_tag}] 客户端关闭连接，但结束原因已转发，标记为成功")
            elif not self.error_msg:
                self.error_msg = stream_stop.CLIENT_DISCONNECTED
                logger.warning(f"[{self.log_tag}] 客户端在流结束前断开: {self.request_id[:8]}")
            raise
        except Exception as e:
            self.error_msg = self.internal_error = str(e)
            logger.error(f"[{self.log_tag}] 流式处理失败: {e}", exc_info=True)
        finally:
            # 中途断开也保存已收到的步骤，签名不随失败的那次请求一起丢。
            self.finalize()
            # 客户端断开时任务处于取消态，统计和关闭上游都要屏蔽取消才能做完；作用域内不能 yield
            with anyio.CancelScope(shield=True):
                await self._record_end()
                # 客户端走了就不能再续传，及时释放上游连接
                await self.chunks.aclose()
                await self.upstream.aclose()

            if not self.client_gone and not self.tail_suppressed:
                try:
                    for sse_text in self._tail():
                        yield sse_text
                except Exception as yield_err:
                    logger.debug(f"[{self.log_tag}] 发送 usage/[DONE] 时客户端已断开: {yield_err}")

    async def _record_end(self) -> None:
        if self.input_tokens == 0 or self.output_tokens == 0:
            try:
                if self.input_tokens == 0:
                    self.input_tokens = await estimate_message_tokens_non_blocking(
                        self.estimate_message_tokens_func, self.openai_req.get('messages', []), self.display_name)
                if self.output_tokens == 0:
                    total_output_text = ''.join(self.reasoning_parts) + ''.join(self.content_parts)
                    if total_output_text:
                        self.output_tokens = await estimate_text_tokens_non_blocking(
                            self.estimate_tokens_func, total_output_text, self.display_name)
            except Exception as token_error:
                logger.error(f"[{self.log_tag}] Token计算失败: {token_error}")

        cost_info = gemini_cost(self.direct_api_service, self.pricing_config, self.input_tokens,
                                self.output_tokens, self.cached_tokens, self.upstream_usage)
        final_content = ''.join(self.content_parts)
        final_reasoning = ''.join(self.reasoning_parts)
        final_tool_calls = finalize_tool_calls(self.tool_call_accumulator)

        if (self.request_success and self.splitter.done
                and self.splitter.original_content and final_content):
            remember_split_original(final_content, self.splitter.original_content)

        self.monitoring_service.request_end(
            request_id=self.request_id, success=self.request_success,
            input_tokens=self.input_tokens, output_tokens=self.output_tokens,
            cached_tokens=self.cached_tokens, error=self.error_msg,
            response_content=final_content,
            reasoning_content=final_reasoning,
            cost_info=cost_info, full_messages=self.full_messages,
            response_message=build_response_message(final_content, final_reasoning, final_tool_calls, self.signatures),
            response_tool_calls=final_tool_calls,
            upstream_usage=self.upstream_usage)

        logger.info(f"[{self.log_tag}] 流式请求完成: {self.request_id[:8]}")
        logger.info(f"  - 输入tokens: {self.input_tokens}, 输出tokens: {self.output_tokens}")
        if cost_info.get("total_cost"):
            logger.info(f"  - 总成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")

    def _tail(self) -> List[str]:
        tail = []
        if self.internal_error:
            tail.append(_sse({'error': {'message': self.internal_error, 'type': 'internal_error'}}))
        total_tokens = self.total_tokens if self.total_tokens > 0 else (self.input_tokens + self.output_tokens)
        tail.append(_sse({
            "id": f"chatcmpl-{self.request_id}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self.display_name,
            "choices": [],
            "usage": compose_chat_usage(
                prompt_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                reasoning_tokens=self.reasoning_tokens,
                cached_tokens=self.cached_tokens,
                completion_mode=self.completion_tokens_mode,
                total_tokens=total_tokens,
            )
        }))
        tail.append("data: [DONE]\n\n")
        return tail


class GenerateContentStreamSession(GeminiStreamSession):
    """generateContent 上游：每个响应块转换成一个 Chat 块。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tool_call_indices: dict = {}
        self.seen_sources: set = set()
        self._signatures: list = []

    @property
    def signatures(self) -> list:
        return self._signatures

    def convert(self, item) -> List[dict]:
        meta = item.get("usageMetadata")
        if isinstance(meta, dict) and meta:
            self.upstream_usage = meta
        self._signatures.extend(upstream_signatures(item))
        return [convert_gemini_to_openai_response(
            item, self.display_name, self.request_id, is_stream_chunk=True,
            completion_tokens_mode=self.completion_tokens_mode, tool_call_indices=self.tool_call_indices,
            seen_sources=self.seen_sources)]

    def upstream_finished(self) -> bool:
        return self.finish_seen

    def manual_finish(self) -> List[str]:
        self.finish_seen = True
        return [manual_finish_chunk(self.request_id, self.display_name, bool(self.tool_call_accumulator))]


class InteractionsStreamSession(GeminiStreamSession):
    """Interactions 上游：事件流由 InteractionsStreamConverter 跨事件维护 step 状态。"""

    log_tag = "GEMINI_INTERACTIONS"
    api_name = "Gemini Interactions API"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.converter = InteractionsStreamConverter(self.display_name, self.request_id, self.completion_tokens_mode)

    @property
    def chunk_id(self) -> str:
        return f"chatcmpl-{self.request_id}"

    @property
    def signatures(self) -> list:
        return self.converter.signatures

    def convert(self, item) -> List[dict]:
        return self.converter.feed(item)

    def account(self, usage: dict) -> None:
        super().account(usage)
        self.upstream_usage = self.converter.usage

    def upstream_finished(self) -> bool:
        return self.converter.completed

    def manual_finish(self) -> List[str]:
        return [sse_text for sse_text in map(self.consume, self.converter.finish_manually()) if sse_text]

    def interrupted_error(self) -> Optional[str]:
        if not self.converter.completed:
            return "Gemini Interactions API 的响应流在结束事件之前中断"
        return None

    def finalize(self) -> None:
        self.converter.finalize()
