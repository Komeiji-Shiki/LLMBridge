"""
Direct API 透传模式：流式会话状态机

🔧 重构说明：
原先 routes/_direct_api_passthrough.py 中的 _handle_passthrough_stream 是一个
550 行的函数，内部嵌套 520 行的 combined_stream_generator 闭包，再嵌套 217 行的
process_sse_chunk 闭包，约 30 个闭包变量通过 nonlocal 共享，完全无法单元测试。

现在拆分为 PassthroughStreamSession 类：
- 所有累积状态成为实例字段
- SSE 块处理、thinking 分离、usage 提取、断连标记、收尾统计各自是独立方法
- 外层生成器只负责心跳、迭代与异常边界，解析与统计全部委托给本类

行为对齐说明（与旧闭包版的差异仅两处，均为无害修复）：
1. delta 显式初始化为 {}：旧版中纯 usage chunk（choices 为空）会因 delta
   未定义触发 NameError，依赖 except 兜底走异常控制流，统计结果一致但丑陋。
2. 删除了声明但从未使用的 decode_buffer 变量。
"""
import asyncio
import codecs
import copy
import json
import logging
import time
from typing import Optional

from ._direct_api_reasoning_cache import (
    merge_reasoning_detail_chunks,
    store_reasoning_details,
)
from ._direct_api_utils import (
    append_tool_call_delta,
    build_response_message,
    extract_complete_sse_lines,
    estimate_message_tokens_non_blocking,
    estimate_text_tokens_non_blocking,
    extract_tool_calls_from_message,
    finalize_tool_calls,
    _enrich_error_message,
)
from utils.json_unescape import StreamingUnicodeUnescaper, normalize_tool_args_json

logger = logging.getLogger(__name__)

# SSE 行前缀常量（拼接书写以避免编辑工具链对 SSE 字面量的特殊处理）
SSE_DATA_PREFIX = "da" "ta: "
SSE_DONE_BYTES = (SSE_DATA_PREFIX + "[DONE]\n\n").encode("utf-8")


class PassthroughStreamSession:
    """流式透传会话：旁路解析 SSE、分离 thinking、统计 token、上报监控。"""

    def __init__(
        self, *,
        request_id: str,
        display_name: str,
        openai_req: dict,
        endpoint_config: dict,
        pricing_config: dict,
        thinking_separator,
        monitoring_service,
        direct_api_service,
        estimate_message_tokens_func,
        estimate_tokens_func,
        full_messages,
        logprobs_collector=None,
    ):
        self.request_id = request_id
        self.display_name = display_name
        self.openai_req = openai_req
        self.endpoint_config = endpoint_config
        self.pricing_config = pricing_config
        self.thinking_separator = thinking_separator
        self.monitoring_service = monitoring_service
        self.direct_api_service = direct_api_service
        self.estimate_message_tokens_func = estimate_message_tokens_func
        self.estimate_tokens_func = estimate_tokens_func
        self.full_messages = full_messages

        # ---- 流累积状态 ----
        self.request_success = False
        self.stream_completed = False
        self.error_msg = None
        self.upstream_error_detected = False
        self.upstream_done_received = False
        self.content_parts = []
        self.reasoning_parts = []
        # OpenRouter reasoning_details 增量分片（Anthropic thinking 签名在其中）
        self.reasoning_detail_chunks = []
        self.tool_call_accumulator = {}
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.reasoning_tokens = 0
        self.cached_tokens = 0
        self.upstream_usage = None  # 上游返回的原生 usage 对象（原样保留，供日志记录）
        self.system_fingerprint = None  # 上游返回的 system_fingerprint（OpenAI 兼容顶层字段）
        self.separator_found = False
        self.repetition_detected = False  # 预留：回放检测
        self.request_end_called = False
        self.client_disconnected = False

        # ---- DeepSeek logprobs 蒸馏采集 ----
        self.logprobs_collector = logprobs_collector
        self._logprobs_finish_tasks = set()

        # ---- thinking separator 切分状态 ----
        self._accumulated_for_split = ""
        self._output_position = 0
        self._split_done = False

        # ---- SSE 解码状态 ----
        self._utf8_decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self._pending_line = ""

        # ---- tool args unicode 转义流式解码状态（tool index → 解码器）----
        self._tool_args_unescapers = {}

    # ================= 断连处理 =================

    def mark_client_disconnect(self, reason: str) -> None:
        """同步标记客户端断连并落盘监控记录（幂等）。"""
        if self.request_end_called:
            return

        self.client_disconnected = True
        self.request_success = False
        self.error_msg = reason

        self._flush_tool_args_tails()
        partial_content = ''.join(self.content_parts)
        partial_reasoning = ''.join(self.reasoning_parts)
        local_input_tokens = self.input_tokens or 0
        partial_tool_calls = finalize_tool_calls(self.tool_call_accumulator)
        local_output_tokens = self.output_tokens or (
            len(partial_reasoning + partial_content) // 4
            if (partial_reasoning or partial_content or partial_tool_calls) else 0)

        self.monitoring_service.request_end(
            request_id=self.request_id, success=False,
            input_tokens=local_input_tokens, output_tokens=local_output_tokens,
            cached_tokens=self.cached_tokens, error=self.error_msg,
            response_content=partial_content, reasoning_content=partial_reasoning,
            full_messages=self.full_messages,
            response_message=build_response_message(partial_content, partial_reasoning, partial_tool_calls),
            response_tool_calls=partial_tool_calls,
            upstream_usage=self.upstream_usage,
            system_fingerprint=self.system_fingerprint)
        self.request_end_called = True
        self._schedule_logprobs_finish(completed=False, error=reason)

    async def handle_client_disconnect(self) -> None:
        """异步断连处理：标记 + 广播。"""
        if self.request_end_called:
            return
        self.mark_client_disconnect("Client disconnected")
        await self.monitoring_service.broadcast_to_monitors({
            "type": "request_end", "request_id": self.request_id, "success": False})

    # ================= SSE 块处理 =================

    def process_sse_chunk(self, chunk_bytes: bytes) -> bytes:
        """处理一个 SSE 字节块：旁路解析并按需改写，返回应转发给客户端的字节。"""
        try:
            chunk_str = self._utf8_decoder.decode(chunk_bytes, final=False)
        except Exception as e:
            logger.warning(f"[UTF8_DECODE] 解码失败: {e}")
            chunk_str = chunk_bytes.decode('utf-8', errors='replace')

        if not chunk_str:
            return b''

        lines, self._pending_line, needs_reassembly = extract_complete_sse_lines(
            chunk_str, self._pending_line)
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
                    self.upstream_done_received = True
                    lines_skipped = True
                    logger.debug(f"[SSE_FILTER] 过滤掉上游 [DONE]")
                    continue
                events, remainder = self._extract_json_objects(data_content)
                if not events:
                    # 整行无法解析：原样透传，不做任何“修复”（下游自行处理）
                    logger.debug(
                        "[JSON_PARSE_FAIL] 无法解析 data 行, 长度=%d, 前100字符: %s",
                        len(data_content), data_content[:100])
                    result_lines.append(line)
                    continue

                glued = len(events) > 1 or bool(remainder)
                if glued:
                    modified = True
                    logger.warning(
                        "[SSE_GLUED_EVENTS] 检测到粘连的 SSE 事件行: %d 个对象, 残余 %d 字符",
                        len(events), len(remainder))

                forwarded_any = False
                for chunk_json in events:
                    event_modified, skip_event = self._process_event_json(chunk_json)
                    if skip_event:
                        lines_skipped = True
                        continue
                    if event_modified or glued:
                        modified = True
                        if forwarded_any:
                            # SSE 规范：独立事件之间必须用空行分隔，
                            # 否则相邻 data 行会被客户端拼成同一个事件
                            result_lines.append('')
                        result_lines.append(SSE_DATA_PREFIX + json.dumps(chunk_json, ensure_ascii=False))
                    else:
                        result_lines.append(line)
                    forwarded_any = True

                if remainder:
                    # 行内残余无法恢复（行已完整，不存在跨 chunk 补全的可能），
                    # 不再静默丢弃：完整记录供排查
                    logger.warning(
                        "[SSE_TAIL_REMAINDER] 丢弃无法解析的行内残余 %d 字符, 前200字符: %s",
                        len(remainder), remainder[:200])
                continue

            result_lines.append(line)

        if modified or lines_skipped or needs_reassembly:
            # 🔧 每行补回换行符重组。不能用 '\n'.join：跨 chunk 拼接场景下
            # 事件终止空行的换行数会因 split/join 的边界语义少一个，
            # 相邻事件会被下游客户端拼成同一个事件
            return ''.join(line + '\n' for line in result_lines).encode('utf-8')
        else:
            return chunk_bytes

    # ---- SSE 事件 JSON 提取与处理 ----

    @staticmethod
    def _extract_json_objects(data_content: str) -> tuple:
        """用标准 JSON 解析器从一行 data 内容中依次提取所有 JSON 对象。

        返回 (objects, remainder)：
        - objects: 依次解析出的 JSON 值列表（正常情况恰好一个）
        - remainder: 尾部无法解析的残余字符串（空串表示全部解析成功）

        raw_decode 是真正的 JSON 解析器，能正确跳过字符串值内部的
        花括号等内容，不会像子串启发式那样误切；粘连的多个事件全部保留。
        """
        decoder = json.JSONDecoder()
        objects = []
        idx = 0
        length = len(data_content)
        while idx < length:
            while idx < length and data_content[idx].isspace():
                idx += 1
            if idx >= length:
                break
            try:
                obj, end = decoder.raw_decode(data_content, idx)
            except (json.JSONDecodeError, ValueError):
                return objects, data_content[idx:]
            objects.append(obj)
            idx = end
        return objects, ""

    def _process_event_json(self, chunk_json) -> tuple:
        """处理单个 SSE 事件 JSON（错误检测/choices/usage/reasoning 归一化）。

        返回 (event_modified, skip_event)。
        """
        event_modified = False
        try:
            # 提取上游 system_fingerprint（DeepSeek 等 OpenAI 兼容 API 的顶层字段，
            # 流式 chunk 每个事件都携带，取最后一个非空值即可）
            if isinstance(chunk_json, dict):
                fp = chunk_json.get("system_fingerprint")
                if isinstance(fp, str) and fp:
                    self.system_fingerprint = fp

            if self.logprobs_collector is not None:
                try:
                    if self.logprobs_collector.capture_stream_event(chunk_json):
                        event_modified = True
                except Exception as lp_err:
                    logger.debug("[LOGPROBS_COLLECT] 旁路采集异常 request_id=%s: %s", self.request_id[:8], lp_err)

            error_modified = self._check_error_event(chunk_json)
            if error_modified:
                event_modified = True

            delta = {}

            if 'choices' in chunk_json and len(chunk_json['choices']) > 0:
                delta_modified, skip_event, delta = self._process_choices(chunk_json)
                if delta_modified:
                    event_modified = True
                if skip_event:
                    return event_modified, True

            if self._process_usage(chunk_json):
                event_modified = True

            # reasoning 字段名归一化（reasoning → reasoning_content）
            if 'reasoning' in delta and 'reasoning_content' not in delta:
                delta['reasoning_content'] = delta.pop('reasoning')
                chunk_json['choices'][0]['delta'] = delta
                event_modified = True
                logger.debug("[REASONING_FIELD_CONVERT] 将 reasoning 转换为 reasoning_content")
        except Exception as process_err:
            logger.debug("[PROCESS_SSE] 处理事件时出错: %s", process_err)
        return event_modified, False

    def _schedule_logprobs_finish(self, completed: bool = False, error: Optional[str] = None) -> None:
        """同步断连路径里异步落盘 logprobs 采集记录。"""
        if not self.logprobs_collector:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.logprobs_collector.finish(completed=completed, error=error))
        self._logprobs_finish_tasks.add(task)
        task.add_done_callback(self._on_logprobs_finish_done)

    def _on_logprobs_finish_done(self, task: asyncio.Task) -> None:
        self._logprobs_finish_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.debug("[LOGPROBS_COLLECT] 异步收尾失败 request_id=%s: %s", self.request_id[:8], exc)


    # ---- 错误事件检测 ----

    def _check_error_event(self, chunk_json) -> bool:
        """检测上游错误事件并归一化格式，返回是否修改了 chunk_json。"""
        if not (isinstance(chunk_json, dict) and 'error' in chunk_json and chunk_json['error'] is not None):
            return False

        self.upstream_error_detected = True
        error_val = chunk_json['error']

        # 提取友好的错误消息（合并 metadata 中的详细信息）
        if isinstance(error_val, dict):
            self.error_msg = _enrich_error_message(error_val)
        else:
            self.error_msg = str(error_val)

        logger.error(f"[DIRECT_API_PASSTHROUGH] 流式上游返回错误事件: {chunk_json}")

        # 归一化：如果 error 是字符串而非对象，包装成 OpenAI 兼容格式
        if not isinstance(error_val, dict):
            chunk_json['error'] = {
                "message": self.error_msg,
                "type": "upstream_error",
                "code": "unknown"
            }
            return True

        # error 是 dict：把丰富后的 message 写回（保留其他字段如 code、metadata）
        enriched_error = dict(error_val)
        enriched_error['message'] = self.error_msg
        chunk_json['error'] = enriched_error
        return True

    # ---- choices/delta 处理（含 thinking 分离） ----

    def _process_choices(self, chunk_json) -> tuple:
        """处理 choices[0]：内容累积与 thinking_separator 流式切分。

        返回 (line_modified, skip_line, delta)。
        """
        line_modified = False
        delta = chunk_json['choices'][0].get('delta', {})
        raw_content = delta.get('content', '')
        raw_reasoning = delta.get('reasoning_content', '') or delta.get('reasoning', '')
        raw_tool_calls = delta.get('tool_calls')
        tool_calls_from_delta = bool(raw_tool_calls)

        message = chunk_json['choices'][0].get('message', {}) if isinstance(chunk_json['choices'][0], dict) else {}
        if isinstance(message, dict):
            raw_content = raw_content or message.get('content', '')
            raw_reasoning = raw_reasoning or message.get('reasoning_content', '') or message.get('reasoning', '')
            raw_tool_calls = raw_tool_calls or extract_tool_calls_from_message(message)

        if raw_tool_calls:
            # 提前把参数中的 \uXXXX 转义解码为明文再转发下游（跨 chunk 安全）
            if tool_calls_from_delta:
                if self._decode_tool_args_inplace(raw_tool_calls, streaming=True):
                    line_modified = True
            else:
                # message 整体形式：raw_tool_calls 是重建的副本，两份都规范化
                # （副本进监控累积器，message 原件转发给下游）
                self._decode_tool_args_inplace(raw_tool_calls, streaming=False)
                if isinstance(message, dict) and isinstance(message.get('tool_calls'), list):
                    if self._decode_tool_args_inplace(message['tool_calls'], streaming=False):
                        line_modified = True
            append_tool_call_delta(self.tool_call_accumulator, raw_tool_calls)

        # 收集 OpenRouter reasoning_details 增量分片（含 thinking 签名，供下一轮回传恢复）
        raw_details = delta.get('reasoning_details')
        if not raw_details and isinstance(message, dict):
            raw_details = message.get('reasoning_details')
        if isinstance(raw_details, list) and raw_details:
            self.reasoning_detail_chunks.extend(raw_details)

        # thinking_separator 流式切分
        if self.thinking_separator and not self._split_done and raw_content:
            self._accumulated_for_split += raw_content

            if self.thinking_separator in self._accumulated_for_split:
                self.separator_found = True
                self._split_done = True

                parts = self._accumulated_for_split.split(self.thinking_separator, 1)
                full_reasoning = parts[0]
                content_part = parts[1] if len(parts) > 1 else ""

                remaining_reasoning = full_reasoning[self._output_position:]
                raw_reasoning = remaining_reasoning
                raw_content = content_part

                new_delta = {}
                if remaining_reasoning:
                    new_delta['reasoning_content'] = remaining_reasoning
                if content_part:
                    new_delta['content'] = content_part

                if new_delta:
                    chunk_json['choices'][0]['delta'] = new_delta
                    delta = new_delta
                    line_modified = True
                    logger.info(f"[THINKING_SPLIT_STREAM] 检测到分隔符'{self.thinking_separator}'")
                    logger.info(f"  - 思考总长: {len(full_reasoning)} 字符")
                    logger.info(f"  - 本次输出reasoning: {len(remaining_reasoning)} 字符")
                    logger.info(f"  - 正文部分: {len(content_part)} 字符")
                else:
                    return line_modified, True, delta
            else:
                sep_len = len(self.thinking_separator)
                safe_position = max(self._output_position, len(self._accumulated_for_split) - sep_len)
                safe_content = self._accumulated_for_split[self._output_position:safe_position]

                if safe_content:
                    raw_reasoning = safe_content
                    raw_content = ""
                    delta['reasoning_content'] = safe_content
                    delta.pop('content', None)
                    self._output_position = safe_position
                    line_modified = True
                else:
                    return line_modified, True, delta

        if raw_content:
            self.content_parts.append(raw_content)
        if raw_reasoning:
            self.reasoning_parts.append(raw_reasoning)

        return line_modified, False, delta

    def _decode_tool_args_inplace(self, tool_calls, streaming: bool) -> bool:
        """把 tool_calls 参数中的 \\uXXXX 转义提前解码为明文（原地修改）。

        streaming=True（delta 增量形式）时按 index 维护跨 chunk 解码状态；
        False（message 整体形式）时对完整 arguments 一次性规范化。
        返回是否有修改（触发 SSE 行重编码转发给下游）。
        """
        modified = False
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function")
            if not isinstance(func, dict):
                continue
            args = func.get("arguments")
            if not isinstance(args, str) or not args:
                continue
            if streaming:
                idx = tc.get("index", 0)
                unescaper = self._tool_args_unescapers.get(idx)
                if unescaper is None:
                    unescaper = self._tool_args_unescapers[idx] = StreamingUnicodeUnescaper()
                if "\\" not in args and not unescaper.pending:
                    continue
                decoded = unescaper.feed(args)
            else:
                decoded = normalize_tool_args_json(args)
            if decoded != args:
                func["arguments"] = decoded
                modified = True
        return modified

    def _flush_tool_args_tails(self) -> list:
        """冲刷各解码器的残留尾部，并入累积器保证监控记录字节完整。

        残留只在流于转义序列中间截断时出现；flush 幂等，可安全多次调用。
        返回应补发给下游客户端的 SSE delta 字节块列表（可能为空）。
        """
        filler_chunks = []
        for idx, unescaper in self._tool_args_unescapers.items():
            tail = unescaper.flush()
            if tail:
                append_tool_call_delta(
                    self.tool_call_accumulator,
                    [{"index": idx, "function": {"arguments": tail}}])
                # 构造 SSE delta 块补发给客户端
                filler_chunk = {
                    "id": f"chatcmpl-{self.request_id}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.display_name,
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "tool_calls": [{
                                "index": idx,
                                "function": {"arguments": tail}
                            }]
                        }
                    }]
                }
                filler_chunks.append(
                    (SSE_DATA_PREFIX + json.dumps(filler_chunk, ensure_ascii=False) + "\n\n").encode('utf-8'))
        return filler_chunks

    # ---- usage 处理 ----

    def _process_usage(self, chunk_json) -> bool:
        """从 chunk 中提取 usage 统计，返回是否修改了 chunk_json。"""
        if not ('usage' in chunk_json and chunk_json['usage'] is not None):
            return False

        line_modified = False
        usage = chunk_json['usage']

        # 原样保留上游返回的原生 usage（必须在本地修正之前深拷贝；部分上游分批下发时增量合并）
        if isinstance(usage, dict) and usage:
            try:
                native_usage = copy.deepcopy(usage)
                if isinstance(self.upstream_usage, dict):
                    self.upstream_usage.update(native_usage)
                else:
                    self.upstream_usage = native_usage
            except Exception:
                pass

        base_prompt = usage.get('prompt_tokens', 0) or usage.get('input_tokens', 0)
        base_output = usage.get('completion_tokens', 0) or usage.get('output_tokens', 0)

        prompt_details = usage.get('prompt_tokens_details', {})
        cached = prompt_details.get('cached_tokens', 0) if isinstance(prompt_details, dict) else 0
        if cached > 0:
            self.cached_tokens = cached

        cached_mode = self.endpoint_config.get('cached_tokens_mode', 'reverse')
        if cached > 0 and cached_mode == 'forward':
            usage['prompt_tokens'] = base_prompt + cached
            line_modified = True
            logger.info(f"[CACHED_TOKENS] 正向模式修正: prompt {base_prompt} + cached {cached} = {usage['prompt_tokens']}")

        if cached_mode == 'forward':
            if base_prompt > 0:
                self.input_tokens = base_prompt + cached
        else:
            if base_prompt > 0:
                self.input_tokens = base_prompt
        if base_output > 0:
            self.output_tokens = base_output
        self.total_tokens = usage.get('total_tokens', 0)
        self.reasoning_tokens = usage.get('reasoning_tokens', 0)

        return line_modified

    # ================= 收尾统计 =================

    async def finalize(self) -> list:
        """流结束后的统计与监控上报，返回应追加发送的 SSE 尾部字节块列表。

        （usage chunk + [DONE]；若已在断连路径记录过则返回空列表）
        """
        if self.logprobs_collector is not None:
            try:
                await self.logprobs_collector.finish(
                    completed=self.request_success,
                    error=self.error_msg or ("client disconnected" if self.client_disconnected else None),
                )
            except Exception as lp_err:
                logger.debug("[LOGPROBS_COLLECT] 收尾失败 request_id=%s: %s", self.request_id[:8], lp_err)

        if self.request_end_called:
            return []

        # 应用思考内容分隔符（整体切分兜底：流式过程中没分离到时）
        accumulated_content = ''.join(self.content_parts)
        accumulated_reasoning = ''.join(self.reasoning_parts)
        final_reasoning = accumulated_reasoning
        final_content = accumulated_content

        if self.thinking_separator and accumulated_content and not accumulated_reasoning:
            reasoning_part, main_part = self.direct_api_service.split_thinking_content(
                accumulated_content, self.thinking_separator)
            if reasoning_part:
                final_reasoning = reasoning_part
                final_content = main_part
                logger.info(f"[THINKING_SPLIT] 检测到思考内容分隔符，分离出 {len(reasoning_part)} 字符的思考内容")

        # 缓存 OpenRouter reasoning_details（含 Anthropic thinking 签名），供下一轮回传恢复
        if self.reasoning_detail_chunks:
            try:
                merged_details = merge_reasoning_detail_chunks(self.reasoning_detail_chunks)
                store_reasoning_details(final_reasoning, merged_details)
                logger.info(
                    f"[DIRECT_API_REASONING] 已缓存 reasoning_details: "
                    f"{len(merged_details)} 块, 思考文本 {len(final_reasoning)} 字符")
            except Exception as cache_err:
                logger.warning(f"[DIRECT_API_REASONING] reasoning_details 缓存失败: {cache_err}")

        # Token 计算
        local_stats = self.endpoint_config.get("token_stats_mode") == "local"
        if self.input_tokens == 0 or local_stats:
            if local_stats:
                logger.info(f"[TOKEN_STATS_LOCAL] local统计模式：使用本地tokenizer计算输入")
            else:
                logger.warning(f"[DIRECT_API_PASSTHROUGH] API未返回input_tokens，使用tokenizer计算")
            try:
                self.input_tokens = await estimate_message_tokens_non_blocking(
                    self.estimate_message_tokens_func,
                    self.openai_req.get('messages', []), self.display_name)
            except Exception as token_error:
                logger.error(f"[DIRECT_API_PASSTHROUGH] Input token计算失败: {token_error}")
                self.input_tokens = sum(
                    len(str(m.get('content', ''))) for m in self.openai_req.get('messages', [])) // 4

        total_output_text = (final_reasoning or "") + (final_content or "")
        content_char_count = len(total_output_text) if total_output_text else 0

        # 🔧 高并发优化：默认信任上游返回的 usage，移除旧版"上游值偏小"的
        # 字符数启发式重算（代码/高压缩内容极易误触发，高并发下每个请求都
        # 多跑一次本地 tokenizer，会灌满默认线程池并加剧 GIL 争抢）。
        # 仅以下三种情况才调用本地 tokenizer：
        # - local 统计模式（endpoint 显式配置忽略上游值）
        # - 上游未返回有效 output_tokens
        # - 回放检测生效（内容已被本地截断，上游值失真）
        should_recalculate = (
            local_stats
            or self.output_tokens <= 1
            or self.repetition_detected
        )

        if should_recalculate and total_output_text:
            try:
                calculated_output_tokens = await estimate_text_tokens_non_blocking(
                    self.estimate_tokens_func, total_output_text, self.display_name)
                if local_stats or calculated_output_tokens >= 10:
                    reason = "local统计模式" if local_stats else (
                        "回放检测" if self.repetition_detected else "上游未返回")
                    logger.info(f"[DIRECT_API_PASSTHROUGH] Token修正({reason}): 上游={self.output_tokens}, 计算={calculated_output_tokens}, 内容={content_char_count}字符")
                    self.output_tokens = calculated_output_tokens
            except Exception as token_error:
                logger.error(f"[DIRECT_API_PASSTHROUGH] Output token计算失败: {token_error}")
                fallback_tokens = len(total_output_text) // 4
                if fallback_tokens >= 10:
                    self.output_tokens = fallback_tokens

        self.total_tokens = self.input_tokens + self.output_tokens

        cost_info = self.direct_api_service.calculate_cost(
            input_tokens=self.input_tokens, output_tokens=self.output_tokens,
            cached_tokens=self.cached_tokens,
            pricing=self.pricing_config) if self.pricing_config else {}
        filler_chunks = self._flush_tool_args_tails()
        final_tool_calls = finalize_tool_calls(self.tool_call_accumulator)

        self.monitoring_service.request_end(
            request_id=self.request_id, success=self.request_success,
            input_tokens=self.input_tokens, output_tokens=self.output_tokens,
            cached_tokens=self.cached_tokens, error=self.error_msg,
            response_content=final_content,
            reasoning_content=final_reasoning,
            cost_info=cost_info, full_messages=self.full_messages,
            response_message=build_response_message(final_content, final_reasoning, final_tool_calls),
            response_tool_calls=final_tool_calls,
            upstream_usage=self.upstream_usage,
            system_fingerprint=self.system_fingerprint)
        self.request_end_called = True
        await self.monitoring_service.broadcast_to_monitors({
            "type": "request_end", "request_id": self.request_id,
            "success": self.request_success})

        logger.info(f"[DIRECT_API_PASSTHROUGH] 流式请求完成: {self.request_id[:8]}, 成功: {self.request_success}")
        if final_reasoning:
            logger.info(f"  - 思考内容: {len(final_reasoning)} 字符")
        if self.input_tokens > 0 or self.output_tokens > 0:
            usage_parts = [f"输入={self.input_tokens}"]
            if self.cached_tokens > 0:
                usage_parts.append(f"(缓存={self.cached_tokens})")
            usage_parts.append(f"输出={self.output_tokens}")
            if self.reasoning_tokens > 0:
                usage_parts.append(f"思考={self.reasoning_tokens}")
            usage_parts.append(f"总计={self.total_tokens}")
            logger.info(f"[DIRECT_API_PASSTHROUGH] Token统计: {', '.join(usage_parts)}")
        if cost_info.get("total_cost"):
            logger.info(f"[DIRECT_API_PASSTHROUGH] 总成本: {cost_info['total_cost']:.6f} {cost_info.get('currency', 'USD')}")

        # 组装最后的 usage chunk 和 [DONE]
        tail_chunks = []
        # 补发 unicode 转义解码尾部残留 delta（此前只补进累积器，客户端缺少这几个字符）
        if filler_chunks:
            tail_chunks.extend(filler_chunks)
            logger.debug(f"[SSE_TAIL_FILLER] 补发 {len(filler_chunks)} 个 tool_args 尾部 delta")
        if self.input_tokens > 0 or self.output_tokens > 0:
            final_total_tokens = self.total_tokens if self.total_tokens > 0 else (self.input_tokens + self.output_tokens)
            usage_final_chunk = {
                "id": f"chatcmpl-{self.request_id}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": self.display_name,
                "choices": [],
                "usage": {
                    "prompt_tokens": self.input_tokens,
                    "completion_tokens": self.output_tokens,
                    "total_tokens": final_total_tokens,
                    **({"prompt_tokens_details": {"cached_tokens": self.cached_tokens}}
                       if self.cached_tokens else {}),
                    **({"completion_tokens_details": {"reasoning_tokens": self.reasoning_tokens}}
                       if self.reasoning_tokens else {})
                }
            }
            tail_chunks.append(
                (SSE_DATA_PREFIX + json.dumps(usage_final_chunk, ensure_ascii=False) + "\n\n").encode('utf-8'))
            logger.debug(f"[SSE_USAGE] 已发送 usage chunk: input={self.input_tokens}, output={self.output_tokens}, total={final_total_tokens}")

        tail_chunks.append(SSE_DONE_BYTES)
        return tail_chunks
