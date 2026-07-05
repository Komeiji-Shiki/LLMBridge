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
import codecs
import json
import logging
import time

from ._direct_api_utils import (
    append_tool_call_delta,
    build_response_message,
    extract_complete_sse_lines,
    estimate_message_tokens_non_blocking,
    estimate_text_tokens_non_blocking,
    extract_tool_calls_from_message,
    finalize_tool_calls,
)

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
        self.tool_call_accumulator = {}
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.reasoning_tokens = 0
        self.cached_tokens = 0
        self.separator_found = False
        self.repetition_detected = False  # 预留：回放检测
        self.request_end_called = False
        self.client_disconnected = False

        # ---- thinking separator 切分状态 ----
        self._accumulated_for_split = ""
        self._output_position = 0
        self._split_done = False

        # ---- SSE 解码状态 ----
        self._utf8_decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self._pending_line = ""

    # ================= 断连处理 =================

    def mark_client_disconnect(self, reason: str) -> None:
        """同步标记客户端断连并落盘监控记录（幂等）。"""
        if self.request_end_called:
            return

        self.client_disconnected = True
        self.request_success = False
        self.error_msg = reason

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
            response_tool_calls=partial_tool_calls)
        self.request_end_called = True

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

        lines, self._pending_line, buffered_incomplete_line = extract_complete_sse_lines(
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
                line_modified = False
                try:
                    chunk_json = json.loads(data_content)
                except json.JSONDecodeError as json_err:
                    chunk_json, fixed = self._try_fix_broken_json(data_content, json_err)
                    if not fixed:
                        logger.debug(f"[JSON_PARSE_FAIL] 位置: {json_err.pos if hasattr(json_err, 'pos') else 'N/A'}, 数据前100字符: {data_content[:100]}")
                        result_lines.append(line_stripped)
                        continue
                    modified = True
                    line_modified = True

                try:
                    line_modified = self._check_error_event(chunk_json) or line_modified
                    if line_modified and self.upstream_error_detected:
                        modified = True

                    delta = {}
                    skip_line = False

                    if 'choices' in chunk_json and len(chunk_json['choices']) > 0:
                        delta_modified, skip_line, delta = self._process_choices(chunk_json)
                        if delta_modified:
                            line_modified = True
                        if skip_line:
                            lines_skipped = True
                            continue

                    if self._process_usage(chunk_json):
                        line_modified = True

                    # reasoning 字段名归一化（reasoning → reasoning_content）
                    if 'reasoning' in delta and 'reasoning_content' not in delta:
                        delta['reasoning_content'] = delta.pop('reasoning')
                        chunk_json['choices'][0]['delta'] = delta
                        line_modified = True
                        logger.debug(f"[REASONING_FIELD_CONVERT] 将 reasoning 转换为 reasoning_content")

                    if line_modified:
                        modified = True
                        result_lines.append(SSE_DATA_PREFIX + json.dumps(chunk_json, ensure_ascii=False))
                        continue

                except Exception as process_err:
                    logger.debug(f"[PROCESS_SSE] 处理行时出错: {process_err}")
                    if line_modified:
                        result_lines.append(SSE_DATA_PREFIX + json.dumps(chunk_json, ensure_ascii=False))
                        continue

            result_lines.append(line)

        if modified or lines_skipped or buffered_incomplete_line:
            return '\n'.join(result_lines).encode('utf-8')
        else:
            return chunk_bytes

    # ---- JSON 修复启发式 ----

    @staticmethod
    def _try_fix_broken_json(data_content: str, json_err) -> tuple:
        """尝试修复截断/拼接的 JSON 行，返回 (chunk_json, fixed)。"""
        # 1) 截断修复：JSON 后面跟了垃圾数据
        if hasattr(json_err, 'pos') and json_err.pos > 1 and json_err.pos < len(data_content):
            try:
                truncated_json = json.loads(data_content[:json_err.pos])
                if isinstance(truncated_json, dict) and (
                        'choices' in truncated_json or 'error' in truncated_json or 'usage' in truncated_json):
                    logger.warning(f"[JSON_TRUNCATE_FIX] 截取前 {json_err.pos}/{len(data_content)} 字符修复成功")
                    return truncated_json, True
            except json.JSONDecodeError:
                pass

        # 2) 拼接修复：两个 JSON 对象连在同一行
        concat_patterns = ['}{', '}\n{', '}\r\n{']
        for pattern in concat_patterns:
            if pattern in data_content:
                split_pos = data_content.find(pattern) + 1
                first_json_str = data_content[:split_pos]
                second_json_str = data_content[split_pos:].lstrip('\n\r')
                for json_str in [second_json_str, first_json_str]:
                    try:
                        chunk_json = json.loads(json_str)
                        if 'choices' in chunk_json or 'error' in chunk_json:
                            logger.warning(f"[JSON_CONCAT_FIX] 检测到拼接JSON，已修复。模式: '{pattern}'")
                            return chunk_json, True
                    except json.JSONDecodeError:
                        continue

        return None, False

    # ---- 错误事件检测 ----

    def _check_error_event(self, chunk_json) -> bool:
        """检测上游错误事件并归一化格式，返回是否修改了 chunk_json。"""
        if not (isinstance(chunk_json, dict) and 'error' in chunk_json and chunk_json['error'] is not None):
            return False

        self.upstream_error_detected = True
        error_val = chunk_json['error']
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
        return False

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

        message = chunk_json['choices'][0].get('message', {}) if isinstance(chunk_json['choices'][0], dict) else {}
        if isinstance(message, dict):
            raw_content = raw_content or message.get('content', '')
            raw_reasoning = raw_reasoning or message.get('reasoning_content', '') or message.get('reasoning', '')
            raw_tool_calls = raw_tool_calls or extract_tool_calls_from_message(message)

        if raw_tool_calls:
            append_tool_call_delta(self.tool_call_accumulator, raw_tool_calls)

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

    # ---- usage 处理 ----

    def _process_usage(self, chunk_json) -> bool:
        """从 chunk 中提取 usage 统计，返回是否修改了 chunk_json。"""
        if not ('usage' in chunk_json and chunk_json['usage'] is not None):
            return False

        line_modified = False
        usage = chunk_json['usage']
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
        min_expected_tokens = content_char_count // 3 if content_char_count > 0 else 0

        should_recalculate = (
            local_stats
            or self.output_tokens <= 1
            or self.repetition_detected
            or (content_char_count > 100 and self.output_tokens < min_expected_tokens * 0.5)
        )

        if should_recalculate and total_output_text:
            try:
                calculated_output_tokens = await estimate_text_tokens_non_blocking(
                    self.estimate_tokens_func, total_output_text, self.display_name)
                if local_stats or calculated_output_tokens >= 10:
                    reason = "local统计模式" if local_stats else (
                        "回放检测" if self.repetition_detected else (
                            "上游值偏小" if self.output_tokens > 1 else "上游未返回"))
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
        final_tool_calls = finalize_tool_calls(self.tool_call_accumulator)

        self.monitoring_service.request_end(
            request_id=self.request_id, success=self.request_success,
            input_tokens=self.input_tokens, output_tokens=self.output_tokens,
            cached_tokens=self.cached_tokens, error=self.error_msg,
            response_content=final_content,
            reasoning_content=final_reasoning,
            cost_info=cost_info, full_messages=self.full_messages,
            response_message=build_response_message(final_content, final_reasoning, final_tool_calls),
            response_tool_calls=final_tool_calls)
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
                    "total_tokens": final_total_tokens
                }
            }
            tail_chunks.append(
                (SSE_DATA_PREFIX + json.dumps(usage_final_chunk, ensure_ascii=False) + "\n\n").encode('utf-8'))
            logger.debug(f"[SSE_USAGE] 已发送 usage chunk: input={self.input_tokens}, output={self.output_tokens}, total={final_total_tokens}")

        tail_chunks.append(SSE_DONE_BYTES)
        return tail_chunks
