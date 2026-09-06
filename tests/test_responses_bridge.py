import asyncio
import json
import unittest

from fastapi import HTTPException
from fastapi.responses import Response

from converters.responses_bridge import (
    build_chat_streaming_response_from_responses,
    convert_chat_request_to_responses,
    convert_responses_response_to_chat,
)
from converters.anthropic_openai import convert_openai_non_stream_response_to_anthropic
from converters.responses_openai import build_responses_streaming_response
from routes._direct_api_responses import handle_responses_native_direct


class ChatToResponsesRequestTests(unittest.TestCase):
    def test_converts_messages_tools_images_and_config(self):
        chat_request = {
            "model": "public-model",
            "messages": [
                {"role": "system", "content": "Be concise."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is this?"},
                        {"type": "image_url", "image_url": {"url": "https://example.test/a.png", "detail": "low"}},
                    ],
                },
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"q":"a"}'},
                    }],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            ],
            "max_completion_tokens": 128,
            "reasoning_effort": "high",
            "verbosity": "low",
            "tools": [{
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Look up data.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
            "tool_choice": {"type": "function", "function": {"name": "lookup"}},
        }
        endpoint_config = {
            "responses_store": True,
            "responses_reasoning_summary": "auto",
        }

        converted = convert_chat_request_to_responses(
            chat_request,
            target_model_id="upstream-model",
            endpoint_config=endpoint_config,
        )

        self.assertEqual(converted["model"], "upstream-model")
        self.assertEqual(converted["instructions"], "Be concise.")
        self.assertTrue(converted["store"])
        self.assertEqual(converted["max_output_tokens"], 128)
        self.assertEqual(converted["reasoning"], {"effort": "high", "summary": "auto"})
        self.assertEqual(converted["text"], {"verbosity": "low"})
        self.assertEqual(converted["input"][0]["content"][1], {
            "type": "input_image",
            "image_url": "https://example.test/a.png",
            "detail": "low",
        })
        self.assertEqual(converted["input"][1]["type"], "function_call")
        self.assertEqual(converted["input"][1]["call_id"], "call_1")
        self.assertEqual(converted["input"][2], {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "result",
        })
        self.assertEqual(converted["tools"][0]["name"], "lookup")
        self.assertEqual(converted["tool_choice"], {"type": "function", "name": "lookup"})


class ResponsesToChatResponseTests(unittest.TestCase):
    def test_converts_text_reasoning_tools_and_usage(self):
        converted = convert_responses_response_to_chat(
            {
                "id": "resp_123",
                "object": "response",
                "created_at": 100,
                "status": "completed",
                "model": "upstream-model",
                "output": [
                    {
                        "id": "rs_1",
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "Reasoning summary."}],
                    },
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Calling a tool.", "annotations": []}],
                    },
                    {
                        "id": "fc_1",
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "lookup",
                        "arguments": '{"q":"a"}',
                    },
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "total_tokens": 30,
                    "input_tokens_details": {"cached_tokens": 3},
                    "output_tokens_details": {"reasoning_tokens": 7},
                },
            },
            "public-model",
        )

        self.assertEqual(converted["id"], "chatcmpl-123")
        self.assertEqual(converted["model"], "upstream-model")
        choice = converted["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(choice["message"]["content"], "Calling a tool.")
        self.assertEqual(choice["message"]["reasoning_content"], "Reasoning summary.")
        self.assertEqual(choice["message"]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(converted["usage"]["prompt_tokens_details"]["cached_tokens"], 3)
        self.assertEqual(converted["usage"]["completion_tokens_details"]["reasoning_tokens"], 7)


class ResponsesToChatStreamingTests(unittest.TestCase):
    @staticmethod
    async def _read_stream(response):
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8"))
        return b"".join(chunks)

    @staticmethod
    def _payloads(raw):
        payloads = []
        for line in raw.decode("utf-8").splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            payloads.append(json.loads(data))
        return payloads

    def test_converts_text_reasoning_and_usage_events(self):
        upstream = (
            b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_1","model":"upstream-model"}}\n\n'
            b'event: response.reasoning_summary_text.delta\ndata: {"type":"response.reasoning_summary_text.delta","delta":"think","item_id":"rs_1","output_index":0,"summary_index":0}\n\n'
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hello","item_id":"msg_1","output_index":1,"content_index":0}\n\n'
            b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_1","model":"upstream-model","status":"completed","usage":{"input_tokens":2,"output_tokens":3,"total_tokens":5}}}\n\n'
            b'data: [DONE]\n\n'
        )
        response = build_chat_streaming_response_from_responses(
            Response(content=upstream, media_type="text/event-stream"),
            "public-model",
        )

        raw = asyncio.run(self._read_stream(response))
        payloads = self._payloads(raw)
        ids = {payload["id"] for payload in payloads if "id" in payload}
        self.assertEqual(len(ids), 1)
        self.assertEqual(payloads[0]["choices"][0]["delta"], {"role": "assistant"})
        self.assertTrue(any(
            payload.get("choices", [{}])[0].get("delta", {}).get("reasoning_content") == "think"
            for payload in payloads if payload.get("choices")
        ))
        self.assertTrue(any(
            payload.get("choices", [{}])[0].get("delta", {}).get("content") == "hello"
            for payload in payloads if payload.get("choices")
        ))
        self.assertTrue(any(
            payload.get("choices", [{}])[0].get("finish_reason") == "stop"
            for payload in payloads if payload.get("choices")
        ))
        usage = next(payload["usage"] for payload in payloads if payload.get("usage"))
        self.assertEqual(usage, {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5})

    def test_stream_errors_preserve_top_level_and_nested_details(self):
        cases = [
            ({"type": "error", "message": "Codex backend stream ended before a terminal response.",
              "code": "stream_ended", "param": None},
             {"message": "Codex backend stream ended before a terminal response.",
              "type": "api_error", "code": "stream_ended", "param": None}),
            ({"type": "error", "message": "Codex backend stream idle timeout after 30m0s"},
             {"message": "Codex backend stream idle timeout after 30m0s", "type": "api_error"}),
            ({"type": "error", "message": "ignored", "error": {"message": "nested", "code": "nested_code"}},
             {"message": "nested", "code": "nested_code"}),
            ({"type": "response.failed", "response": {"error": {"message": "failed", "code": "server_error"}}},
             {"message": "failed", "code": "server_error"}),
            ({"type": "error"}, {"message": "上游 Responses 请求失败", "type": "api_error"}),
        ]
        for event, expected in cases:
            with self.subTest(event=event):
                upstream = ("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode()
                response = build_chat_streaming_response_from_responses(
                    Response(content=upstream, media_type="text/event-stream"), "public-model")
                raw = asyncio.run(self._read_stream(response))
                errors = [payload["error"] for payload in self._payloads(raw) if "error" in payload]
                self.assertEqual(errors, [expected])
                self.assertTrue(raw.rstrip().endswith(b"data: [DONE]"))
                self.assertFalse(any(
                    choice.get("finish_reason") == "stop"
                    for payload in self._payloads(raw) for choice in payload.get("choices", [])))

    def test_converts_function_call_events(self):
        upstream = (
            b'event: response.output_item.added\ndata: {"type":"response.output_item.added","output_index":0,"item":{"id":"fc_1","type":"function_call","call_id":"call_1","name":"lookup","arguments":"","status":"in_progress"}}\n\n'
            b'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","item_id":"fc_1","output_index":0,"delta":"{\\"q\\":"}\n\n'
            b'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","item_id":"fc_1","output_index":0,"delta":"\\"a\\"}"}\n\n'
            b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_1","status":"completed","usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}\n\n'
            b'data: [DONE]\n\n'
        )
        response = build_chat_streaming_response_from_responses(
            Response(content=upstream, media_type="text/event-stream"),
            "public-model",
        )

        payloads = self._payloads(asyncio.run(self._read_stream(response)))
        tool_deltas = [
            payload["choices"][0]["delta"]["tool_calls"][0]
            for payload in payloads
            if payload.get("choices")
            and payload["choices"][0].get("delta", {}).get("tool_calls")
        ]
        self.assertEqual({delta["index"] for delta in tool_deltas}, {0})
        self.assertEqual(tool_deltas[0]["id"], "call_1")
        self.assertEqual(tool_deltas[0]["function"]["name"], "lookup")
        arguments = "".join(
            delta.get("function", {}).get("arguments", "")
            for delta in tool_deltas
        )
        self.assertEqual(arguments, '{"q":"a"}')
        self.assertTrue(any(
            payload.get("choices", [{}])[0].get("finish_reason") == "tool_calls"
            for payload in payloads if payload.get("choices")
        ))


    def test_stream_falls_back_to_complete_response_json(self):
        upstream = {
            "id": "resp_full",
            "object": "response",
            "status": "completed",
            "model": "upstream-model",
            "output": [{
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "full json", "annotations": []}],
            }],
            "usage": {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4},
        }
        response = build_chat_streaming_response_from_responses(
            Response(content=json.dumps(upstream) + "\n\n", media_type="application/json"),
            "public-model",
        )

        payloads = self._payloads(asyncio.run(self._read_stream(response)))
        self.assertTrue(any(
            payload.get("choices", [{}])[0].get("delta", {}).get("content") == "full json"
            for payload in payloads if payload.get("choices")
        ))
        self.assertTrue(any(
            payload.get("choices", [{}])[0].get("finish_reason") == "stop"
            for payload in payloads if payload.get("choices")
        ))
        self.assertEqual(next(payload["usage"] for payload in payloads if payload.get("usage")), {
            "prompt_tokens": 2,
            "completion_tokens": 2,
            "total_tokens": 4,
        })


class _FakeMonitoring:
    def __init__(self):
        self.starts = []
        self.ends = []
        self.broadcasts = []

    def request_start(self, **kwargs):
        self.starts.append(kwargs)

    def request_end(self, **kwargs):
        self.ends.append(kwargs)

    async def broadcast_to_monitors(self, payload):
        self.broadcasts.append(payload)


class _FakeDirectApiService:
    def __init__(self, chunks):
        self.chunks = chunks
        self.request_body = None
        self.endpoint_path = None

    async def call_api_passthrough(self, *, request_body, endpoint_path, **kwargs):
        self.request_body = request_body
        self.endpoint_path = endpoint_path
        for chunk in self.chunks:
            yield chunk

    @staticmethod
    def calculate_cost(**kwargs):
        return {}


class ResponsesNativeHandlerTests(unittest.TestCase):
    @staticmethod
    async def _read_stream(response):
        return b"".join([
            chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
            async for chunk in response.body_iterator
        ])

    def test_handler_converts_non_stream_and_can_feed_anthropic_converter(self):
        upstream_payload = {
            "id": "resp_handler",
            "object": "response",
            "status": "completed",
            "model": "upstream-model",
            "output": [{
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hello", "annotations": []}],
            }],
            "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
        }
        service = _FakeDirectApiService([
            json.dumps(upstream_payload).encode("utf-8")
        ])
        monitoring = _FakeMonitoring()

        response = asyncio.run(handle_responses_native_direct(
            openai_req={
                "model": "public-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
            model_name="public-model",
            target_model_id="upstream-model",
            display_name="Public Model",
            api_base_url="https://example.test/v1",
            api_key="key",
            endpoint_config={"api_type": "responses_native"},
            pricing_config={},
            monitoring_service=monitoring,
            direct_api_service=service,
            estimate_message_tokens_func=lambda *args, **kwargs: 1,
            estimate_tokens_func=lambda *args, **kwargs: 1,
            full_messages=[{"role": "user", "content": "hi"}],
            CONFIG={},
        ))

        chat_data = json.loads(response.body)
        self.assertEqual(service.endpoint_path, "/responses")
        self.assertEqual(service.request_body["input"][0]["content"][0]["text"], "hi")
        self.assertEqual(chat_data["choices"][0]["message"]["content"], "hello")
        self.assertTrue(monitoring.ends[0]["success"])

        anthropic_response = asyncio.run(convert_openai_non_stream_response_to_anthropic(
            Response(content=response.body, media_type="application/json"),
            request_model="public-model",
        ))
        anthropic_data = json.loads(anthropic_response.body)
        self.assertEqual(anthropic_data["type"], "message")
        self.assertEqual(anthropic_data["content"][0], {"type": "text", "text": "hello"})

    def test_handler_converts_stream(self):
        upstream = (
            b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_1","model":"upstream-model"}}\n\n'
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hello"}\n\n'
            b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_1","status":"completed","usage":{"input_tokens":2,"output_tokens":1,"total_tokens":3}}}\n\n'
            b'data: [DONE]\n\n'
        )
        service = _FakeDirectApiService([upstream])
        monitoring = _FakeMonitoring()

        response = asyncio.run(handle_responses_native_direct(
            openai_req={
                "model": "public-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            model_name="public-model",
            target_model_id="upstream-model",
            display_name="Public Model",
            api_base_url="https://example.test/v1",
            api_key="key",
            endpoint_config={"api_type": "responses_native"},
            pricing_config={},
            monitoring_service=monitoring,
            direct_api_service=service,
            estimate_message_tokens_func=lambda *args, **kwargs: 1,
            estimate_tokens_func=lambda *args, **kwargs: 1,
            full_messages=[{"role": "user", "content": "hi"}],
            CONFIG={},
        ))

        raw = asyncio.run(self._read_stream(response))
        self.assertIn(b'"content": "hello"', raw)
        self.assertIn(b'data: [DONE]', raw)
        self.assertTrue(monitoring.ends[0]["success"])


    def test_handler_stream_records_success_when_client_disconnects_after_done(self):
        """回归：客户端收到 [DONE] 后立即断开（生成器被 aclose 抛 GeneratorExit），
        上游已完整返回，不得把成功请求误报为 failed（error 为 null 的误报根源）。"""
        upstream = (
            b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_1","model":"upstream-model"}}\n\n'
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hello"}\n\n'
            b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_1","status":"completed","usage":{"input_tokens":2,"output_tokens":1,"total_tokens":3}}}\n\n'
            b'data: [DONE]\n\n'
        )
        service = _FakeDirectApiService([upstream])
        monitoring = _FakeMonitoring()

        async def scenario():
            response = await handle_responses_native_direct(
                openai_req={
                    "model": "public-model",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
                model_name="public-model",
                target_model_id="upstream-model",
                display_name="Public Model",
                api_base_url="https://example.test/v1",
                api_key="key",
                endpoint_config={"api_type": "responses_native"},
                pricing_config={},
                monitoring_service=monitoring,
                direct_api_service=service,
                estimate_message_tokens_func=lambda *args, **kwargs: 1,
                estimate_tokens_func=lambda *args, **kwargs: 1,
                full_messages=[{"role": "user", "content": "hi"}],
                CONFIG={},
            )
            # 模拟客户端：读到 [DONE] 后立即断开 → 生成器被 aclose（GeneratorExit）
            raw = b""
            async for chunk in response.body_iterator:
                raw += chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
                if b"[DONE]" in raw:
                    break
            await response.body_iterator.aclose()
            return raw

        raw = asyncio.run(scenario())
        self.assertIn(b'"content": "hello"', raw)
        self.assertIn(b"data: [DONE]", raw)
        self.assertEqual(len(monitoring.ends), 1)
        self.assertTrue(monitoring.ends[0]["success"])
        self.assertIsNone(monitoring.ends[0]["error"])
        self.assertEqual(monitoring.ends[0]["output_tokens"], 1)

    def test_full_responses_chain_records_success_before_client_stops_after_completed(self):
        """回归真实链路：Responses→Chat→Responses 双层转换。

        外层一产出 response.completed，客户端就可能停止读取。此时内层监控必须
        已经落盘成功，不能依赖外层生成器继续迭代或之后的关闭时机。
        """
        upstream = (
            b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_1","model":"upstream-model"}}\n\n'
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hello"}\n\n'
            b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_1","status":"completed","usage":{"input_tokens":2,"output_tokens":1,"total_tokens":3}}}\n\n'
            b'data: [DONE]\n\n'
        )
        service = _FakeDirectApiService([upstream])
        monitoring = _FakeMonitoring()

        async def scenario():
            chat_response = await handle_responses_native_direct(
                openai_req={
                    "model": "public-model",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
                model_name="public-model",
                target_model_id="upstream-model",
                display_name="Public Model",
                api_base_url="https://example.test/v1",
                api_key="key",
                endpoint_config={"api_type": "responses_native"},
                pricing_config={},
                monitoring_service=monitoring,
                direct_api_service=service,
                estimate_message_tokens_func=lambda *args, **kwargs: 1,
                estimate_tokens_func=lambda *args, **kwargs: 1,
                full_messages=[{"role": "user", "content": "hi"}],
                CONFIG={},
            )
            responses_response = build_responses_streaming_response(
                chat_response,
                request={"model": "public-model", "input": "hi", "stream": True},
                model="public-model",
            )

            raw = b""
            async for chunk in responses_response.body_iterator:
                raw += chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
                if b"event: response.completed" in raw:
                    self.assertEqual(len(monitoring.ends), 1)
                    self.assertTrue(monitoring.ends[0]["success"])
                    self.assertIsNone(monitoring.ends[0]["error"])
                    break
            await responses_response.body_iterator.aclose()
            return raw

        raw = asyncio.run(scenario())
        self.assertIn(b"event: response.completed", raw)
        self.assertEqual(len(monitoring.ends), 1)
        self.assertTrue(monitoring.ends[0]["success"])
        self.assertEqual(monitoring.ends[0]["response_content"], "hello")
        self.assertEqual(monitoring.ends[0]["input_tokens"], 2)
        self.assertEqual(monitoring.ends[0]["output_tokens"], 1)

    def test_handler_stream_records_failure_when_client_disconnects_mid_stream(self):
        """客户端在内容生成中途断开（未收到完整流）→ 记录失败并给出断连原因。"""
        upstream = (
            b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_1","model":"upstream-model"}}\n\n'
            b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hello"}\n\n'
            b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_1","status":"completed","usage":{"input_tokens":2,"output_tokens":1,"total_tokens":3}}}\n\n'
            b'data: [DONE]\n\n'
        )
        service = _FakeDirectApiService([upstream])
        monitoring = _FakeMonitoring()

        async def scenario():
            response = await handle_responses_native_direct(
                openai_req={
                    "model": "public-model",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
                model_name="public-model",
                target_model_id="upstream-model",
                display_name="Public Model",
                api_base_url="https://example.test/v1",
                api_key="key",
                endpoint_config={"api_type": "responses_native"},
                pricing_config={},
                monitoring_service=monitoring,
                direct_api_service=service,
                estimate_message_tokens_func=lambda *args, **kwargs: 1,
                estimate_tokens_func=lambda *args, **kwargs: 1,
                full_messages=[{"role": "user", "content": "hi"}],
                CONFIG={},
            )
            # 模拟客户端：只读第一个 chunk（role 占位）就断开
            it = response.body_iterator
            await anext(it)
            await it.aclose()

        asyncio.run(scenario())
        self.assertEqual(len(monitoring.ends), 1)
        self.assertFalse(monitoring.ends[0]["success"])
        self.assertEqual(monitoring.ends[0]["error"], "Client disconnected")

    def test_handler_records_failure_before_raising(self):
        service = _FakeDirectApiService([b'not-json'])
        monitoring = _FakeMonitoring()

        with self.assertRaises(HTTPException):
            asyncio.run(handle_responses_native_direct(
                openai_req={
                    "model": "public-model",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
                model_name="public-model",
                target_model_id="upstream-model",
                display_name="Public Model",
                api_base_url="https://example.test/v1",
                api_key="key",
                endpoint_config={"api_type": "responses_native"},
                pricing_config={},
                monitoring_service=monitoring,
                direct_api_service=service,
                estimate_message_tokens_func=lambda *args, **kwargs: 1,
                estimate_tokens_func=lambda *args, **kwargs: 1,
                full_messages=[{"role": "user", "content": "hi"}],
                CONFIG={},
            ))

        self.assertEqual(len(monitoring.ends), 1)
        self.assertFalse(monitoring.ends[0]["success"])
        self.assertIn("无法解析", monitoring.ends[0]["error"])


if __name__ == "__main__":
    unittest.main()
