"""responses_native 原生透传分支测试。"""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.responses import JSONResponse, StreamingResponse

from routes.responses_api import (
    _handle_responses_native_passthrough,
    _responses_input_to_messages,
    _rewrite_sse_model,
    responses_endpoint,
)

REASONING_ITEM = {
    "type": "reasoning",
    "id": "rs_test_signature_1",
    "summary": [{"type": "summary_text", "text": "I reasoned about this."}],
    "encrypted_content": "ciphertext_do_not_touch_12345",
}


class ResponsesMonitoringMessageConversionTests(unittest.TestCase):
    def test_merges_reasoning_text_and_multiple_calls_from_one_assistant_turn(self):
        messages = _responses_input_to_messages({
            "input": [
                {"type": "message", "role": "user", "content": "inspect this"},
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"type": "summary_text", "text": "first thought"}],
                    "encrypted_content": "sig_1",
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "working"}],
                },
                {
                    "type": "reasoning",
                    "id": "rs_2",
                    "summary": [{"type": "summary_text", "text": "second thought"}],
                    "encrypted_content": "sig_2",
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "read_file",
                    "arguments": {"path": "a.txt"},
                },
                {
                    "type": "function_call",
                    "id": "call_2",
                    "name": "search_in_files",
                    "arguments": '{"query":"needle"}',
                },
                {"type": "function_call_output", "call_id": "call_1", "output": "A"},
                {"type": "function_call_output", "call_id": "call_2", "output": "B"},
            ],
        })

        self.assertEqual([message["role"] for message in messages], [
            "user", "assistant", "tool", "tool",
        ])
        assistant = messages[1]
        self.assertEqual(assistant["content"], "working")
        self.assertEqual(assistant["reasoning_content"], "first thought\nsecond thought")
        self.assertEqual(assistant["reasoning_signature"], ["sig_1", "sig_2"])
        self.assertEqual([call["id"] for call in assistant["tool_calls"]], ["call_1", "call_2"])
        self.assertEqual(
            assistant["tool_calls"][0]["function"]["arguments"],
            '{"path":"a.txt"}',
        )
        self.assertEqual([message["tool_call_id"] for message in messages[2:]], [
            "call_1", "call_2",
        ])

    def test_tool_outputs_and_new_user_messages_are_hard_turn_boundaries(self):
        messages = _responses_input_to_messages({
            "input": [
                {"type": "reasoning", "id": "rs_a", "summary": []},
                {"type": "function_call", "call_id": "call_a", "name": "a", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_a", "output": "done-a"},
                {"type": "reasoning", "id": "rs_b", "summary": []},
                {"type": "function_call", "call_id": "call_b", "name": "b", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_b", "output": "done-b"},
                {"type": "message", "role": "user", "content": "next turn"},
                {"type": "reasoning", "id": "rs_c", "summary": []},
                {"type": "message", "role": "assistant", "content": "final answer"},
            ],
        })

        self.assertEqual([message["role"] for message in messages], [
            "assistant", "tool", "assistant", "tool", "user", "assistant",
        ])
        self.assertEqual(messages[0]["tool_calls"][0]["id"], "call_a")
        self.assertEqual(messages[2]["tool_calls"][0]["id"], "call_b")
        self.assertEqual(messages[4]["content"], "next turn")
        self.assertEqual(messages[5]["content"], "final answer")
        self.assertEqual(messages[5]["reasoning_signature"], "rs_c")

    def test_empty_assistant_items_do_not_split_adjacent_function_calls(self):
        messages = _responses_input_to_messages({
            "input": [
                {"type": "function_call", "call_id": "call_1", "name": "a", "arguments": "{}"},
                {"type": "reasoning", "summary": []},
                {"type": "message", "role": "assistant", "content": ""},
                {"type": "function_call", "call_id": "call_2", "name": "b", "arguments": None},
            ],
        })

        self.assertEqual(len(messages), 1)
        self.assertEqual([call["id"] for call in messages[0]["tool_calls"]], [
            "call_1", "call_2",
        ])
        self.assertEqual(messages[0]["tool_calls"][1]["function"]["arguments"], "{}")


class _FakeDirectApiService:
    """记录透传参数，按预设 chunk 返回。"""

    def __init__(self, chunks):
        self.chunks = chunks
        self.captured = {}
        self.closed = False

    def call_api_passthrough(self, base_url, api_key, request_body, headers=None,
                             endpoint_path="/chat/completions"):
        self.captured = {
            "base_url": base_url,
            "api_key": api_key,
            "request_body": request_body,
            "endpoint_path": endpoint_path,
            "headers": headers,
        }

        async def gen():
            try:
                for chunk in self.chunks:
                    if isinstance(chunk, BaseException):
                        raise chunk
                    yield chunk
            finally:
                self.closed = True

        return gen()


class _FakeServer:
    def __init__(self, direct_api_service):
        self.direct_api_service = direct_api_service
        self.VERIFICATION_COOLDOWN_UNTIL = None


class _FakeAppState:
    def __init__(self, direct_api_service):
        self.server = _FakeServer(direct_api_service)

    def update_activity(self):
        pass


NATIVE_CONFIG = {
    "api_type": "responses_native",
    "model_id": "gpt-5.6-sol",
    "display_name": "gpt-5.6-sol",
    "endpoint_path": "/responses",
    "api_base_url": "http://localhost:8088/v1",
    "api_key": "sk-upstream",
}


def _upstream_non_stream_payload(model="gpt-5.6-sol"):
    return {
        "id": "resp_upstream_1",
        "object": "response",
        "created_at": 1700000000,
        "status": "completed",
        "model": model,
        "output": [
            dict(REASONING_ITEM),
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hello from upstream", "annotations": []}],
            },
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


def _upstream_stream_payloads():
    return [
        (
            b'event: response.created\n'
            b'data: {"type":"response.created","response":{"id":"resp_1","object":"response",'
            b'"created_at":1700000000,"status":"in_progress","model":"gpt-5.6-sol","output":[]}}\n\n'
        ),
        (
            b'event: response.output_item.added\n'
            b'data: {"type":"response.output_item.added","output_index":0,"item":'
            + json.dumps(REASONING_ITEM, ensure_ascii=False).encode("utf-8") + b'}\n\n'
        ),
        (
            b'event: response.completed\n'
            b'data: {"type":"response.completed","response":{"id":"resp_1","object":"response",'
            b'"created_at":1700000000,"status":"completed","model":"gpt-5.6-sol",'
            b'"output":[{"type":"message","id":"msg_1","role":"assistant",'
            b'"content":[{"type":"output_text","text":"hi","annotations":[]}]}]}}\n\n'
        ),
        b'data: [DONE]\n\n',
    ]


class RewriteSseModelTests(unittest.TestCase):
    def test_rewrites_matching_model_keeps_structure(self):
        chunk = (
            b'event: response.completed\n'
            b'data: {"type":"response.completed","response":{"id":"resp_1","model":"gpt-5.6-sol",'
            b'"output":[{"type":"reasoning","id":"rs_1","encrypted_content":"cipher"}]}}\n\n'
        )
        out = _rewrite_sse_model(chunk, "gpt-5.6-sol_sph", "gpt-5.6-sol")
        self.assertEqual(out.count(b"gpt-5.6-sol_sph"), 1)
        self.assertIn(b'"model":"gpt-5.6-sol_sph"', out)
        self.assertIn(b'"encrypted_content":"cipher"', out)
        # 事件结构与行尾保持原样
        self.assertTrue(out.endswith(b"\n\n"))
        self.assertTrue(out.startswith(b"event: response.completed\n"))

    def test_unchanged_when_model_matches_client(self):
        chunk = b'data: {"type":"response.completed","response":{"model":"gpt-5.6-sol"}}\n\n'
        self.assertEqual(_rewrite_sse_model(chunk, "gpt-5.6-sol", "gpt-5.6-sol"), chunk)

    def test_unchanged_for_done_and_non_json_lines(self):
        chunk = b'data: [DONE]\n\n'
        self.assertEqual(_rewrite_sse_model(chunk, "client", "upstream"), chunk)
        # JSON 被 TCP 切断的残留块：解析失败必须原样透传
        broken = b'event: x\ndata: {"model": "upstream"\n\n'
        out = _rewrite_sse_model(broken, "client", "upstream")
        self.assertEqual(out, broken)


class PassthroughHandlerTests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    async def _handle_async(self, service, request_body, config=NATIVE_CONFIG, consume_stream=False):
        fake_app = _FakeAppState(service)
        monitoring_mock = AsyncMock()
        # request_start/request_end 是 fire-and-forget 同步调用：AsyncMock 未 await 的
        # coroutine 不计数，改用 MagicMock 才能断言调用；broadcast 保持 AsyncMock
        monitoring_mock.request_start = MagicMock()
        monitoring_mock.request_end = MagicMock()
        self.monitoring_mock = monitoring_mock
        with (
            patch("routes.api_routes._app_state", fake_app),
            patch("routes.responses_api._chat_api._validate_request_api_key"),
            patch("routes.responses_api.get_round_robin_api_key", new=AsyncMock(return_value="sk-upstream")),
            patch("routes.responses_api.monitoring_service", monitoring_mock),
        ):
            result = await _handle_responses_native_passthrough(
                responses_request=request_body,
                model=request_body["model"],
                endpoint_config=config,
                request=MagicMock(),
            )
            # 流式生成器在 patch 上下文内消费，否则 finally 落盘会跑到真实监控服务上
            if consume_stream and isinstance(result, StreamingResponse):
                raw = await _read_stream(result)
                return result, raw
            return result

    def _handle(self, service, request_body, config=NATIVE_CONFIG):
        return self._run(self._handle_async(service, request_body, config=config))

    def test_non_stream_success_keeps_reasoning_item_and_rewrites_model(self):
        service = _FakeDirectApiService(
            [json.dumps(_upstream_non_stream_payload(), ensure_ascii=False).encode("utf-8")])

        request_body = {
            "model": "gpt-5.6-sol_sph",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                dict(REASONING_ITEM),
            ],
            "stream": False,
        }
        result = self._handle(service, request_body)

        self.assertIsInstance(result, JSONResponse)
        self.assertEqual(result.status_code, 200)

        # 上游请求：model 替换为 target，reasoning item 原样
        captured = service.captured
        self.assertEqual(captured["request_body"]["model"], "gpt-5.6-sol")
        self.assertEqual(captured["request_body"]["input"][1], REASONING_ITEM)
        self.assertEqual(captured["base_url"], "http://localhost:8088/v1")
        self.assertEqual(captured["endpoint_path"], "/responses")
        self.assertEqual(captured["api_key"], "sk-upstream")

        # 响应：model 回写，reasoning item 原样保留
        body = json.loads(result.body)
        self.assertEqual(body["model"], "gpt-5.6-sol_sph")
        self.assertEqual(body["output"][0], REASONING_ITEM)
        self.assertEqual(body["output"][1]["content"][0]["text"], "hello from upstream")

    def test_non_stream_error_maps_status_code(self):
        error_payload = {
            "error": {
                "message": "Rate limit exceeded",
                "type": "rate_limit_error",
                "code": "rate_limit",
            },
            "_http_status": 429,
        }
        service = _FakeDirectApiService([json.dumps(error_payload).encode("utf-8")])

        result = self._handle(service, {"model": "gpt-5.6-sol_sph", "input": "hi"})

        self.assertEqual(result.status_code, 429)
        body = json.loads(result.body)
        self.assertIn("Rate limit", body["error"]["message"])
        # 内部字段不泄露给客户端
        self.assertNotIn("_http_status", body)

    def test_stream_success_rewrites_model_and_keeps_reasoning(self):
        service = _FakeDirectApiService(_upstream_stream_payloads())

        request_body = {
            "model": "gpt-5.6-sol_sph",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                dict(REASONING_ITEM),
            ],
            "stream": True,
        }

        async def run_all():
            result = await self._handle_async(service, request_body)
            raw = await _read_stream(result)
            return result, raw

        result, raw = self._run(run_all())

        self.assertIsInstance(result, StreamingResponse)
        self.assertEqual(result.media_type, "text/event-stream")

        text = raw.decode("utf-8")
        # model 回写：上游 gpt-5.6-sol → 客户端 gpt-5.6-sol_sph
        self.assertIn('"model":"gpt-5.6-sol_sph"', text)
        self.assertNotIn('"model":"gpt-5.6-sol"', text)
        # reasoning item 事件原样保留（含 encrypted_content）
        self.assertIn('"type": "reasoning"', text)
        self.assertIn("ciphertext_do_not_touch_12345", text)
        self.assertTrue(text.endswith("data: [DONE]\n\n"))

    def test_stream_first_chunk_error(self):
        error_chunk = (
            b'data: {"error": {"message": "Bad signature", "type": "invalid_request_error", "code": 400}}\n\n'
            b'data: [DONE]\n\n'
        )
        service = _FakeDirectApiService([error_chunk])

        result = self._handle(service, {"model": "gpt-5.6-sol_sph", "input": "hi", "stream": True})

        self.assertIsInstance(result, JSONResponse)
        self.assertEqual(result.status_code, 400)
        body = json.loads(result.body)
        self.assertIn("Bad signature", body["error"]["message"])
        self.assertTrue(service.closed)

    def test_non_stream_client_disconnect_records_failure(self):
        service = _FakeDirectApiService([asyncio.CancelledError()])

        with self.assertRaises(asyncio.CancelledError):
            self._handle(service, {
                "model": "gpt-5.6-sol_sph",
                "input": "hi",
                "stream": False,
            })

        monitoring = self.monitoring_mock
        monitoring.request_end.assert_called_once()
        end_kwargs = monitoring.request_end.call_args.kwargs
        self.assertFalse(end_kwargs["success"])
        self.assertIn("Client disconnected", end_kwargs["error"])
        self.assertTrue(service.closed)

    def test_missing_api_base_url_returns_500(self):
        service = _FakeDirectApiService([])
        bad_config = {k: v for k, v in NATIVE_CONFIG.items() if k != "api_base_url"}

        result = self._handle(service, {"model": "gpt-5.6-sol_sph", "input": "hi"}, config=bad_config)

        self.assertEqual(result.status_code, 500)

    # ── 监控日志：透传请求必须与转换链路一样记录 request_start/request_end ──

    def test_non_stream_success_records_request_log(self):
        service = _FakeDirectApiService(
            [json.dumps(_upstream_non_stream_payload(), ensure_ascii=False).encode("utf-8")])

        result = self._handle(service, {"model": "gpt-5.6-sol_sph", "input": "hi"})

        self.assertIsInstance(result, JSONResponse)
        self.assertEqual(result.status_code, 200)

        monitoring = self.monitoring_mock
        monitoring.request_start.assert_called_once()
        start_kwargs = monitoring.request_start.call_args.kwargs
        self.assertEqual(start_kwargs["model"], "gpt-5.6-sol")
        self.assertEqual(start_kwargs["mode"], "responses_native_passthrough")
        self.assertNotIn("input", start_kwargs["params"])
        self.assertEqual(start_kwargs["params"]["upstream_model"], "gpt-5.6-sol")

        monitoring.request_end.assert_called_once()
        end_kwargs = monitoring.request_end.call_args.kwargs
        self.assertTrue(end_kwargs["success"])
        self.assertEqual(end_kwargs["input_tokens"], 10)
        self.assertEqual(end_kwargs["output_tokens"], 5)
        self.assertEqual(end_kwargs["response_content"], "hello from upstream")
        self.assertIsNone(end_kwargs["error"])
        self.assertEqual(end_kwargs["upstream_usage"]["input_tokens"], 10)
        # 请求日志包含 input 消息
        self.assertEqual(end_kwargs["full_messages"], [{"role": "user", "content": "hi"}])
        monitoring.broadcast_to_monitors.assert_called()

    def test_instructions_recorded_as_system_message(self):
        """instructions（系统提示词）应作为 oai 兼容的 system 消息记录。"""
        service = _FakeDirectApiService(
            [json.dumps(_upstream_non_stream_payload(), ensure_ascii=False).encode("utf-8")])
        request_body = {
            "model": "gpt-5.6-sol_sph",
            "instructions": "你是灰魂，一位技术精湛的软件工程师少女。",
            "input": [
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "hi"}]},
            ],
        }

        result = self._handle(service, request_body)
        self.assertEqual(result.status_code, 200)

        monitoring = self.monitoring_mock
        start_kwargs = monitoring.request_start.call_args.kwargs
        # params 不重复记录 instructions（已转入 request_messages）
        self.assertNotIn("instructions", start_kwargs["params"])
        self.assertNotIn("input", start_kwargs["params"])

        end_kwargs = monitoring.request_end.call_args.kwargs
        self.assertEqual(end_kwargs["full_messages"], [
            {"role": "system", "content": "你是灰魂，一位技术精湛的软件工程师少女。"},
            {"role": "user", "content": "hi"},
        ])

    def test_reasoning_item_keeps_summary_and_signature(self):
        """reasoning item 的思维链摘要与签名（encrypted_content）必须保留。"""
        service = _FakeDirectApiService(
            [json.dumps(_upstream_non_stream_payload(), ensure_ascii=False).encode("utf-8")])
        request_body = {
            "model": "gpt-5.6-sol_sph",
            "input": [dict(REASONING_ITEM)],
        }

        result = self._handle(service, request_body)
        self.assertEqual(result.status_code, 200)

        monitoring = self.monitoring_mock
        end_kwargs = monitoring.request_end.call_args.kwargs
        self.assertEqual(end_kwargs["full_messages"], [{
            "role": "assistant",
            "content": "",
            "reasoning_content": "I reasoned about this.",
            "reasoning_signature": "ciphertext_do_not_touch_12345",
        }])

    def test_function_call_and_output_recorded(self):
        """function_call / function_call_output 应转为 oai 的 tool_calls / tool 消息。"""
        service = _FakeDirectApiService(
            [json.dumps(_upstream_non_stream_payload(), ensure_ascii=False).encode("utf-8")])
        request_body = {
            "model": "gpt-5.6-sol_sph",
            "input": [
                {"type": "function_call", "call_id": "call_1", "name": "read_file",
                 "arguments": "{\"path\": \"a.txt\"}"},
                {"type": "function_call_output", "call_id": "call_1", "output": "file content"},
            ],
        }

        result = self._handle(service, request_body)
        self.assertEqual(result.status_code, 200)

        monitoring = self.monitoring_mock
        end_kwargs = monitoring.request_end.call_args.kwargs
        self.assertEqual(end_kwargs["full_messages"], [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{\"path\": \"a.txt\"}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "file content"},
        ])

    def test_empty_placeholder_messages_filtered(self):
        """纯占位空消息（content 为空且无附加信息）应过滤，避免日志刷屏。"""
        service = _FakeDirectApiService(
            [json.dumps(_upstream_non_stream_payload(), ensure_ascii=False).encode("utf-8")])
        request_body = {
            "model": "gpt-5.6-sol_sph",
            "input": [
                {"type": "message", "role": "user", "content": "real question"},
                {"type": "message", "role": "assistant", "content": ""},
                {"type": "message", "role": "user", "content": ""},
                "plain string item",
            ],
        }

        result = self._handle(service, request_body)
        self.assertEqual(result.status_code, 200)

        monitoring = self.monitoring_mock
        end_kwargs = monitoring.request_end.call_args.kwargs
        self.assertEqual(end_kwargs["full_messages"], [
            {"role": "user", "content": "real question"},
            {"role": "user", "content": "plain string item"},
        ])

    def test_params_excludes_bulk_fields_and_records_tools_count(self):
        """instructions/tools/include/prompt_cache_key 不进 params；tools 记摘要。"""
        service = _FakeDirectApiService(
            [json.dumps(_upstream_non_stream_payload(), ensure_ascii=False).encode("utf-8")])
        request_body = {
            "model": "gpt-5.6-sol_sph",
            "instructions": "sys",
            "input": "hi",
            "tools": [
                {"type": "function", "name": "read_file"},
                {"type": "function", "function": {"name": "write_file"}},
            ],
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": "cache-123",
            "stream": False,
        }

        result = self._handle(service, request_body)
        self.assertEqual(result.status_code, 200)

        monitoring = self.monitoring_mock
        start_kwargs = monitoring.request_start.call_args.kwargs
        params = start_kwargs["params"]
        for key in ("input", "instructions", "tools", "include", "prompt_cache_key"):
            self.assertNotIn(key, params)
        self.assertEqual(params["tools_count"], 2)
        # 工具摘要：规范哈希 + 工具名序列，用于缓存掉链时排除工具变化
        self.assertEqual(params["tools_names"], ["read_file", "write_file"])
        self.assertIn("tools_sha256", params)
        self.assertEqual(len(params["tools_sha256"]), 64)
        self.assertEqual(params["upstream_model"], "gpt-5.6-sol")

    def test_tools_summary_is_order_sensitive_and_schema_sensitive(self):
        """工具顺序或定义变化必须改变摘要哈希（缓存前缀同样会被破坏）。"""
        from routes.responses_api import _summarize_tools_for_monitor

        base = [
            {"type": "function", "name": "read_file", "description": "read"},
            {"type": "function", "name": "write_file", "description": "write"},
        ]
        same = [dict(tool) for tool in base]
        reordered = [base[1], base[0]]
        edited = [dict(base[0], description="READ"), base[1]]

        self.assertEqual(
            _summarize_tools_for_monitor(base)["tools_sha256"],
            _summarize_tools_for_monitor(same)["tools_sha256"],
        )
        self.assertNotEqual(
            _summarize_tools_for_monitor(base)["tools_sha256"],
            _summarize_tools_for_monitor(reordered)["tools_sha256"],
        )
        self.assertNotEqual(
            _summarize_tools_for_monitor(base)["tools_sha256"],
            _summarize_tools_for_monitor(edited)["tools_sha256"],
        )
        self.assertEqual(
            _summarize_tools_for_monitor(reordered)["tools_names"],
            ["write_file", "read_file"],
        )
        self.assertEqual(_summarize_tools_for_monitor(None), {})
        self.assertEqual(_summarize_tools_for_monitor("not-a-list"), {})

    def test_non_stream_error_records_failure(self):
        error_payload = {
            "error": {
                "message": "Rate limit exceeded",
                "type": "rate_limit_error",
                "code": "rate_limit",
            },
            "_http_status": 429,
        }
        service = _FakeDirectApiService([json.dumps(error_payload).encode("utf-8")])

        result = self._handle(service, {"model": "gpt-5.6-sol_sph", "input": "hi"})

        self.assertEqual(result.status_code, 429)
        monitoring = self.monitoring_mock
        monitoring.request_start.assert_called_once()
        monitoring.request_end.assert_called_once()
        end_kwargs = monitoring.request_end.call_args.kwargs
        self.assertFalse(end_kwargs["success"])
        self.assertIn("Rate limit exceeded", end_kwargs["error"])

    def test_stream_success_records_request_log(self):
        service = _FakeDirectApiService(_upstream_stream_payloads())
        request_body = {"model": "gpt-5.6-sol_sph", "input": "hi", "stream": True}

        result, raw = self._run(self._handle_async(service, request_body, consume_stream=True))
        self.assertIsInstance(result, StreamingResponse)

        monitoring = self.monitoring_mock
        monitoring.request_start.assert_called_once()
        monitoring.request_end.assert_called_once()
        end_kwargs = monitoring.request_end.call_args.kwargs
        self.assertTrue(end_kwargs["success"])
        self.assertIsNone(end_kwargs["error"])

    def test_stream_delta_and_usage_accumulated(self):
        payloads = [
            b'data: {"type":"response.output_text.delta","output_index":0,"delta":"Hel"}\n\n',
            b'data: {"type":"response.output_text.delta","output_index":0,"delta":"lo"}\n\n',
            b'data: {"type":"response.completed","response":{"status":"completed","usage":'
            b'{"input_tokens":7,"output_tokens":3,"total_tokens":10,'
            b'"input_tokens_details":{"cached_tokens":2}}}}\n\n',
            b'data: [DONE]\n\n',
        ]
        service = _FakeDirectApiService(payloads)
        request_body = {"model": "gpt-5.6-sol_sph", "input": "hi", "stream": True}

        result, raw = self._run(self._handle_async(service, request_body, consume_stream=True))
        self.assertIsInstance(result, StreamingResponse)
        self.assertTrue(raw.endswith(b"data: [DONE]\n\n"))

        monitoring = self.monitoring_mock
        monitoring.request_start.assert_called_once()
        monitoring.request_end.assert_called_once()
        end_kwargs = monitoring.request_end.call_args.kwargs
        self.assertTrue(end_kwargs["success"])
        self.assertEqual(end_kwargs["response_content"], "Hello")
        self.assertEqual(end_kwargs["input_tokens"], 7)
        self.assertEqual(end_kwargs["output_tokens"], 3)
        self.assertEqual(end_kwargs["cached_tokens"], 2)
        self.assertEqual(end_kwargs["upstream_usage"]["input_tokens"], 7)
        self.assertIsNone(end_kwargs["error"])

    def test_stream_failed_event_records_failure(self):
        payloads = [
            b'data: {"type":"response.failed","response":{"error":{"message":"upstream boom"}}}\n\n',
            b'data: [DONE]\n\n',
        ]
        service = _FakeDirectApiService(payloads)
        request_body = {"model": "gpt-5.6-sol_sph", "input": "hi", "stream": True}

        result, raw = self._run(self._handle_async(service, request_body, consume_stream=True))
        self.assertIsInstance(result, StreamingResponse)

        monitoring = self.monitoring_mock
        monitoring.request_end.assert_called_once()
        end_kwargs = monitoring.request_end.call_args.kwargs
        self.assertFalse(end_kwargs["success"])
        self.assertIn("upstream boom", end_kwargs["error"])

    def test_stream_top_level_error_event_records_failure(self):
        # 顶层 code+message 的 error 事件会被 is_error_json 判为首块错误 → 502 JSON 响应
        payloads = [
            b'event: error\n'
            b'data: {"type":"error","code":"invalid_request_error","message":"bad top-level error"}\n\n',
            b'data: [DONE]\n\n',
        ]
        service = _FakeDirectApiService(payloads)
        request_body = {"model": "gpt-5.6-sol_sph", "input": "hi", "stream": True}

        result = self._handle(service, request_body)
        self.assertIsInstance(result, JSONResponse)
        self.assertEqual(result.status_code, 502)

        monitoring = self.monitoring_mock
        monitoring.request_start.assert_called_once()
        monitoring.request_end.assert_called_once()
        end_kwargs = monitoring.request_end.call_args.kwargs
        self.assertFalse(end_kwargs["success"])
        self.assertIn("bad top-level error", end_kwargs["error"])

    def test_stream_client_disconnect_records_failure(self):
        payloads = [
            b'data: {"type":"response.output_text.delta","output_index":0,"delta":"Hi"}\n\n',
        ]
        service = _FakeDirectApiService(payloads)
        request_body = {"model": "gpt-5.6-sol_sph", "input": "hi", "stream": True}

        async def run_all():
            fake_app = _FakeAppState(service)
            monitoring_mock = AsyncMock()
            monitoring_mock.request_start = MagicMock()
            monitoring_mock.request_end = MagicMock()
            self.monitoring_mock = monitoring_mock
            with (
                patch("routes.api_routes._app_state", fake_app),
                patch("routes.responses_api._chat_api._validate_request_api_key"),
                patch("routes.responses_api.get_round_robin_api_key",
                      new=AsyncMock(return_value="sk-upstream")),
                patch("routes.responses_api.monitoring_service", monitoring_mock),
            ):
                result = await _handle_responses_native_passthrough(
                    responses_request=request_body,
                    model=request_body["model"],
                    endpoint_config=NATIVE_CONFIG,
                    request=MagicMock(),
                )
                # 客户端在流完成前断开：只消费第一块就关闭生成器
                it = result.body_iterator
                await anext(it)
                await it.aclose()
                return result

        result = self._run(run_all())
        self.assertIsInstance(result, StreamingResponse)

        monitoring = self.monitoring_mock
        monitoring.request_end.assert_called_once()
        end_kwargs = monitoring.request_end.call_args.kwargs
        self.assertFalse(end_kwargs["success"])
        self.assertIn("Client disconnected", end_kwargs["error"])


async def _read_stream(response):
    return b"".join([
        chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
        async for chunk in response.body_iterator
    ])


class EndpointRoutingTests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_native_model_goes_to_passthrough(self):
        service = _FakeDirectApiService(
            [json.dumps(_upstream_non_stream_payload(), ensure_ascii=False).encode("utf-8")])
        fake_app = _FakeAppState(service)

        request_body = {
            "model": "gpt-5.6-sol_sph",
            "input": [dict(REASONING_ITEM)],
        }
        monitoring_mock = AsyncMock()
        monitoring_mock.request_start = MagicMock()
        monitoring_mock.request_end = MagicMock()
        with (
            patch("routes.api_routes._app_state", fake_app),
            patch("routes.api_routes._check_verification_cooldown"),
            patch("routes.api_routes._read_request_json_non_blocking",
                  new=AsyncMock(return_value=request_body)),
            patch("routes.api_routes._select_endpoint_config_for_model",
                  new=AsyncMock(return_value=NATIVE_CONFIG)),
            patch("routes.api_routes._validate_request_api_key"),
            patch("routes.responses_api.get_round_robin_api_key", new=AsyncMock(return_value="sk-upstream")),
            patch("routes.responses_api.monitoring_service", monitoring_mock),
        ):
            result = self._run(responses_endpoint(MagicMock()))

        self.assertIsInstance(result, JSONResponse)
        self.assertEqual(result.status_code, 200)
        # 确认走了透传分支而不是转换链路（转换链路会因 reasoning item 抛错）
        self.assertEqual(service.captured["request_body"]["model"], "gpt-5.6-sol")
        body = json.loads(result.body)
        self.assertEqual(body["output"][0], REASONING_ITEM)

    def test_archived_native_model_returns_404_before_passthrough(self):
        service = _FakeDirectApiService([])
        fake_app = _FakeAppState(service)
        archived_config = {**NATIVE_CONFIG, "archived": True}
        request_body = {"model": "gpt-5.6-sol_sph", "input": "hi"}

        with (
            patch("routes.api_routes._app_state", fake_app),
            patch("routes.api_routes._check_verification_cooldown"),
            patch("routes.api_routes._read_request_json_non_blocking",
                  new=AsyncMock(return_value=request_body)),
            patch("routes.api_routes.MODEL_ENDPOINT_MAP", {
                "gpt-5.6-sol_sph": archived_config,
            }),
            patch("routes.api_routes._select_endpoint_config_for_model",
                  new=AsyncMock(return_value=archived_config)),
        ):
            result = self._run(responses_endpoint(MagicMock()))

        self.assertIsInstance(result, JSONResponse)
        self.assertEqual(result.status_code, 404)
        self.assertIn("不存在", json.loads(result.body)["error"]["message"])
        self.assertEqual(service.captured, {})

    def test_non_native_model_keeps_compat_chain(self):
        service = _FakeDirectApiService([])
        fake_app = _FakeAppState(service)
        chat_response = {
            "id": "chatcmpl_1",
            "object": "chat.completion",
            "model": "demo-model",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "hello"},
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

        with (
            patch("routes.api_routes._app_state", fake_app),
            patch("routes.api_routes._check_verification_cooldown"),
            patch("routes.api_routes._read_request_json_non_blocking",
                  new=AsyncMock(return_value={"model": "demo-model", "input": "hi"})),
            patch("routes.api_routes._select_endpoint_config_for_model", new=AsyncMock(return_value=None)),
            patch("routes.api_routes._validate_request_api_key"),
            patch("routes.api_routes._dispatch_chat_completions_core",
                  new=AsyncMock(return_value=JSONResponse(status_code=200, content=chat_response))),
        ):
            result = self._run(responses_endpoint(MagicMock()))

        self.assertIsInstance(result, JSONResponse)
        self.assertEqual(result.status_code, 200)
        body = json.loads(result.body)
        self.assertEqual(body["output"][0]["type"], "message")
        self.assertEqual(body["output"][0]["content"][0]["text"], "hello")
        # 透传分支未被触发
        self.assertEqual(service.captured, {})


if __name__ == "__main__":
    unittest.main()
