import asyncio
import json
import unittest

from fastapi.responses import Response

from converters.responses_openai import (
    ResponsesRequestError,
    build_responses_streaming_response,
    collect_chat_stream_response,
    convert_chat_response_to_responses,
    convert_responses_to_chat_request,
)


class ResponsesRequestConversionTests(unittest.TestCase):
    def test_converts_text_image_tools_and_parameters(self):
        request = {
            "model": "demo-model",
            "instructions": "You are concise.",
            "input": [{
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Describe this."},
                    {"type": "input_image", "image_url": "https://example.test/image.png"},
                ],
            }],
            "max_output_tokens": 256,
            "reasoning": {"effort": "low"},
            "tools": [{
                "type": "function",
                "name": "lookup",
                "description": "Look something up.",
                "parameters": {"type": "object", "properties": {}},
            }],
            "tool_choice": {"type": "function", "name": "lookup"},
        }

        converted = convert_responses_to_chat_request(request)

        self.assertEqual(converted["model"], "demo-model")
        self.assertEqual(converted["messages"][0], {"role": "system", "content": "You are concise."})
        user_message = converted["messages"][1]
        self.assertEqual(user_message["role"], "user")
        self.assertEqual(user_message["content"][0], {"type": "text", "text": "Describe this."})
        self.assertEqual(
            user_message["content"][1],
            {"type": "image_url", "image_url": {"url": "https://example.test/image.png"}},
        )
        self.assertEqual(converted["max_completion_tokens"], 256)
        self.assertEqual(converted["reasoning_effort"], "low")
        self.assertEqual(converted["tools"][0]["function"]["name"], "lookup")
        self.assertEqual(converted["tool_choice"], {
            "type": "function",
            "function": {"name": "lookup"},
        })

    def test_converts_function_call_round_trip_input_items(self):
        converted = convert_responses_to_chat_request({
            "model": "demo-model",
            "input": [
                {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_1", "output": {"ok": True}},
            ],
        })

        self.assertEqual(converted["messages"][0]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(converted["messages"][1]["role"], "tool")
        self.assertEqual(converted["messages"][1]["tool_call_id"], "call_1")
        self.assertEqual(converted["messages"][1]["content"], '{"ok":true}')

    def test_rejects_stateful_features_in_stateless_phase(self):
        for field, value in (
            ("previous_response_id", "resp_old"),
            ("store", True),
            ("background", True),
            ("conversation", "conv_1"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ResponsesRequestError):
                    convert_responses_to_chat_request({
                        "model": "demo-model",
                        "input": "hello",
                        field: value,
                    })


class ResponsesResponseConversionTests(unittest.TestCase):
    def test_converts_text_reasoning_tool_calls_and_usage(self):
        response = convert_chat_response_to_responses(
            {
                "id": "chatcmpl_test",
                "object": "chat.completion",
                "model": "demo-model",
                "choices": [{
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "I need a tool.",
                        "reasoning_content": "I checked the available tools.",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                        }],
                    },
                }],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                    "prompt_tokens_details": {"cached_tokens": 2},
                    "completion_tokens_details": {"reasoning_tokens": 5},
                },
            },
            {"model": "demo-model", "input": "hello"},
        )

        self.assertTrue(response["id"].startswith("resp_"))
        self.assertEqual(response["status"], "completed")
        self.assertEqual([item["type"] for item in response["output"]], ["reasoning", "message", "function_call"])
        self.assertEqual(response["output"][1]["content"][0]["text"], "I need a tool.")
        self.assertEqual(response["output"][2]["call_id"], "call_1")
        self.assertEqual(response["usage"]["input_tokens_details"]["cached_tokens"], 2)
        self.assertEqual(response["usage"]["output_tokens_details"]["reasoning_tokens"], 5)


class ResponsesStreamingTests(unittest.TestCase):
    @staticmethod
    async def _read_stream(response):
        return b"".join([
            chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
            async for chunk in response.body_iterator
        ])

    @staticmethod
    def _parse_events(raw):
        events = []
        for block in raw.decode("utf-8").split("\n\n"):
            if not block.strip():
                continue
            event_name = next(line[7:] for line in block.splitlines() if line.startswith("event: "))
            data = next(line[6:] for line in block.splitlines() if line.startswith("data: "))
            events.append((event_name, json.loads(data)))
        return events

    def test_converts_chat_sse_to_responses_events(self):
        chat_sse = (
            b'data: {"id":"chatcmpl_1","object":"chat.completion.chunk","model":"demo-model",'
            b'"choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
            b'data: {"id":"chatcmpl_1","object":"chat.completion.chunk","model":"demo-model",'
            b'"choices":[{"index":0,"delta":{"content":" world"},"finish_reason":"stop"}]}\n\n'
            b'data: {"id":"chatcmpl_1","object":"chat.completion.chunk","model":"demo-model",'
            b'"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":2,"total_tokens":4}}\n\n'
            b'data: [DONE]\n\n'
        )
        response = build_responses_streaming_response(
            Response(content=chat_sse, media_type="text/event-stream"),
            request={"model": "demo-model", "input": "hello", "stream": True},
            model="demo-model",
        )

        events = self._parse_events(asyncio.run(self._read_stream(response)))
        event_names = [name for name, _ in events]
        self.assertEqual(event_names[0:2], ["response.created", "response.in_progress"])
        self.assertIn("response.output_text.delta", event_names)
        self.assertIn("response.output_text.done", event_names)
        self.assertIn("response.content_part.done", event_names)
        self.assertIn("response.output_item.done", event_names)
        self.assertEqual(event_names[-1], "response.completed")
        completed = events[-1][1]["response"]
        self.assertEqual(completed["output"][0]["content"][0]["text"], "hello world")
        self.assertEqual(completed["usage"]["input_tokens"], 2)

    def test_collects_stream_for_non_stream_client(self):
        chat_sse = (
            b'data: {"id":"chatcmpl_1","model":"demo-model","choices":[{"index":0,'
            b'"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
            b'data: {"id":"chatcmpl_1","model":"demo-model","choices":[{"index":0,'
            b'"delta":{},"finish_reason":"stop"}]}\n\n'
            b'data: [DONE]\n\n'
        )
        chat_response = Response(content=chat_sse, media_type="text/event-stream")
        collected = asyncio.run(collect_chat_stream_response(chat_response, "demo-model"))
        self.assertEqual(collected["choices"][0]["message"]["content"], "hello")
        self.assertEqual(collected["choices"][0]["finish_reason"], "stop")


if __name__ == "__main__":
    unittest.main()
