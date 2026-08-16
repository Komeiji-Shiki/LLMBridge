"""
Gemini Interactions 转换器测试（纯函数，无网络依赖）

覆盖：
- OAI messages → interactions steps（角色/图片/工具/签名注入）
- 思考签名缓存 + 前缀匹配注入
- build_interactions_request_body（generation_config/tools/response_format）
- interactions Interaction → OpenAI chat.completion
- InteractionsStreamConverter 流式状态机
- generateContent ↔ interactions 双向转换（请求/响应/流式）
"""
import asyncio
import json
import unittest

from converters.gemini_interactions import (
    InteractionsStreamConverter,
    InteractionsToGeminiGCConverter,
    _thought_signature_cache,
    build_interactions_request_body,
    cache_thought_signatures,
    convert_gemini_gc_to_interactions,
    convert_interactions_to_gemini_gc,
    convert_interactions_to_openai_response,
    convert_oai_messages_to_interactions,
    match_and_inject_thought_signatures,
)
from services.direct_api_service import DirectAPIService


def setUpModule():
    _thought_signature_cache.clear()


class OaiMessagesToInteractionsTests(unittest.TestCase):
    def setUp(self):
        _thought_signature_cache.clear()

    def test_system_user_assistant_tool(self):
        messages = [
            {"role": "system", "content": "You are a cat."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "user", "content": "What time is it?"},
        ]
        steps, system = convert_oai_messages_to_interactions(messages)
        self.assertEqual(system, "You are a cat.")
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0], {"type": "user_input", "content": [{"type": "text", "text": "Hello"}]})
        self.assertEqual(steps[1], {"type": "model_output", "content": [{"type": "text", "text": "Hi there"}]})
        self.assertEqual(steps[2]["type"], "user_input")

    def test_image_data_url(self):
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "What is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]}]
        steps, _ = convert_oai_messages_to_interactions(messages)
        blocks = steps[0]["content"]
        self.assertEqual(blocks[1], {"type": "image", "data": "AAAA", "mime_type": "image/png"})

    def test_image_http_url(self):
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
        ]}]
        steps, _ = convert_oai_messages_to_interactions(messages)
        block = steps[0]["content"][0]
        self.assertEqual(block["type"], "image")
        self.assertEqual(block["uri"], "https://example.com/a.png")
        self.assertEqual(block["mime_type"], "image/png")

    def test_input_audio(self):
        messages = [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": "QUJD", "format": "wav"}},
        ]}]
        steps, _ = convert_oai_messages_to_interactions(messages)
        block = steps[0]["content"][0]
        self.assertEqual(block["type"], "audio")
        self.assertEqual(block["mime_type"], "audio/wav")

    def test_tool_calls_and_results(self):
        messages = [
            {"role": "user", "content": "Weather?"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_123", "type": "function",
                 "function": {"name": "get_weather", "arguments": "{\"city\": \"Paris\"}"}},
            ]},
            {"role": "tool", "tool_call_id": "call_123", "content": "Sunny"},
        ]
        steps, _ = convert_oai_messages_to_interactions(messages)
        self.assertEqual(steps[1]["type"], "function_call")
        self.assertEqual(steps[1]["name"], "get_weather")
        self.assertEqual(steps[1]["arguments"], {"city": "Paris"})
        self.assertEqual(steps[1]["id"], "call_123")
        self.assertEqual(steps[2]["type"], "function_result")
        self.assertEqual(steps[2]["call_id"], "call_123")
        # 函数名从 assistant tool_calls 映射（而非 id 猜测）
        self.assertEqual(steps[2]["name"], "get_weather")
        self.assertEqual(steps[2]["result"][0]["text"], "Sunny")

    def test_reasoning_signature_injection(self):
        # 先缓存签名（模拟响应侧捕获）
        cache_thought_signatures([("Let me think about this. ", "sig_abc"), ("More thinking. ", "sig_def")])
        messages = [
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "Answer",
             "reasoning_content": "Let me think about this. More thinking. "},
        ]
        steps, _ = convert_oai_messages_to_interactions(messages)
        # user_input + 2×thought + model_output = 4 步；thought 在 model_output 之前且带签名
        self.assertEqual(len(steps), 4)
        self.assertEqual(steps[0]["type"], "user_input")
        self.assertEqual(steps[1]["type"], "thought")
        self.assertEqual(steps[1]["signature"], "sig_abc")
        self.assertEqual(steps[1]["summary"][0]["text"], "Let me think about this. ")
        self.assertEqual(steps[2]["type"], "thought")
        self.assertEqual(steps[2]["signature"], "sig_def")
        self.assertEqual(steps[3]["type"], "model_output")
        self.assertEqual(steps[3]["content"][0]["text"], "Answer")

    def test_unknown_role_skipped(self):
        messages = [{"role": "developer", "content": "ignored"}]
        steps, system = convert_oai_messages_to_interactions(messages)
        self.assertEqual(steps, [])
        self.assertEqual(system, "")


class ThoughtSignatureCacheTests(unittest.TestCase):
    def setUp(self):
        _thought_signature_cache.clear()

    def test_full_match(self):
        cache_thought_signatures([("AAAA ", "sig1"), ("BBBB", "sig2")])
        steps = match_and_inject_thought_signatures("AAAA BBBB")
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["signature"], "sig1")
        self.assertEqual(steps[1]["signature"], "sig2")

    def test_prefix_match_truncated(self):
        cache_thought_signatures([("AAAA ", "sig1"), ("BBBB", "sig2")])
        # 客户端裁剪了思考尾部 → 只注入能匹配的前缀部分
        steps = match_and_inject_thought_signatures("AAAA")
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["signature"], "sig1")
        # 客户端文本是片段自身的前缀
        steps2 = match_and_inject_thought_signatures("AA")
        self.assertEqual(len(steps2), 1)
        self.assertEqual(steps2[0]["signature"], "sig1")
        self.assertEqual(steps2[0]["summary"][0]["text"], "AA")

    def test_no_match(self):
        cache_thought_signatures([("AAAA ", "sig1")])
        self.assertEqual(match_and_inject_thought_signatures("Totally different"), [])
        self.assertEqual(match_and_inject_thought_signatures(""), [])

    def test_fragments_without_signature_not_cached(self):
        cache_thought_signatures([("AAAA ", ""), ("BBBB", "sig2")])
        # 无签名的片段整体不入缓存（valid 过滤后剩一个也不够完整，joined 非空则仍缓存）
        self.assertEqual(len(_thought_signature_cache), 1)


class BuildRequestBodyTests(unittest.TestCase):
    def setUp(self):
        _thought_signature_cache.clear()

    def test_basic_body(self):
        body = build_interactions_request_body(
            model="gemini-3-flash-preview",
            messages=[{"role": "user", "content": "Hi"}],
            stream=True,
            temperature=0.7,
            max_tokens=100,
            thinking_config={"thinkingLevel": "LOW", "includeThoughts": True},
        )
        self.assertEqual(body["model"], "gemini-3-flash-preview")
        self.assertIs(body["store"], False)
        self.assertIs(body["stream"], True)
        self.assertEqual(body["input"][0]["type"], "user_input")
        self.assertEqual(body["generation_config"]["temperature"], 0.7)
        self.assertEqual(body["generation_config"]["max_output_tokens"], 100)
        self.assertEqual(body["generation_config"]["thinking_level"], "low")
        self.assertEqual(body["generation_config"]["thinking_summaries"], "auto")

    def test_tools_passthrough_and_tool_choice_none(self):
        tools = [{"type": "function", "name": "f", "description": "d", "parameters": {"type": "object"}}]
        body = build_interactions_request_body("m", [{"role": "user", "content": "x"}], tools=tools)
        self.assertEqual(body["tools"], tools)
        body2 = build_interactions_request_body(
            "m", [{"role": "user", "content": "x"}], tools=tools, tool_choice="none")
        self.assertNotIn("tools", body2)

    def test_nested_chat_tools_are_flattened(self):
        tools = [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }]
        body = build_interactions_request_body("m", [{"role": "user", "content": "x"}], tools=tools)
        self.assertEqual(body["tools"], [{
            "type": "function",
            "name": "get_weather",
            "description": "Get weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        }])

    def test_tool_choice_maps_to_generation_config(self):
        tools = [{"type": "function", "name": "f", "parameters": {"type": "object"}}]
        body = build_interactions_request_body(
            "m", [{"role": "user", "content": "x"}],
            tools=tools, tool_choice="required")
        self.assertEqual(body["generation_config"]["tool_choice"], {
            "allowed_tools": {"mode": "any"}
        })
        named = build_interactions_request_body(
            "m", [{"role": "user", "content": "x"}],
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "f"}},
        )
        self.assertEqual(named["generation_config"]["tool_choice"]["allowed_tools"]["tools"], ["f"])

    def test_response_format_json_object(self):
        body = build_interactions_request_body(
            "m", [{"role": "user", "content": "x"}], response_format={"type": "json_object"})
        self.assertEqual(body["response_mime_type"], "application/json")
        self.assertEqual(body["response_format"], {
            "type": "text",
            "mime_type": "application/json",
        })

    def test_response_schema_and_stop_sequences(self):
        body = build_interactions_request_body(
            "m",
            [{"role": "user", "content": "x"}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": {"type": "object"}},
            },
            stop_sequences=["END"],
        )
        self.assertEqual(body["response_format"]["schema"], {"type": "object"})
        self.assertEqual(body["generation_config"]["stop_sequences"], ["END"])

    def test_thinking_budget_ignored(self):
        body = build_interactions_request_body(
            "m", [{"role": "user", "content": "x"}],
            thinking_config={"thinkingBudget": 20000, "includeThoughts": True})
        self.assertNotIn("thinking_level", body.get("generation_config", {}))

    def test_include_thoughts_false_maps_to_none(self):
        body = build_interactions_request_body(
            "m", [{"role": "user", "content": "x"}],
            thinking_config={"includeThoughts": False})
        self.assertEqual(body["generation_config"]["thinking_summaries"], "none")
        self.assertNotIn("thinking_level", body["generation_config"])


class InteractionsToOpenaiResponseTests(unittest.TestCase):
    def setUp(self):
        _thought_signature_cache.clear()

    def test_full_response(self):
        interaction = {
            "id": "v1_xxx", "status": "completed", "model": "gemini-3-flash-preview",
            "steps": [
                {"type": "thought", "summary": {"type": "text", "text": "Hmm, thinking"}, "signature": "sig_x"},
                {"type": "model_output", "content": [{"type": "text", "text": "Hello!"}]},
            ],
            "usage": {"total_input_tokens": 10, "total_output_tokens": 20,
                      "total_thought_tokens": 5, "total_tokens": 35},
        }
        resp = convert_interactions_to_openai_response(interaction, "gemini-3-flash-preview", "req123")
        self.assertEqual(resp["object"], "chat.completion")
        self.assertEqual(resp["choices"][0]["message"]["content"], "Hello!")
        self.assertEqual(resp["choices"][0]["message"]["reasoning_content"], "Hmm, thinking")
        self.assertEqual(resp["choices"][0]["finish_reason"], "stop")
        self.assertEqual(resp["usage"]["prompt_tokens"], 10)
        # 思考 token 计入输出（#44 修复方向）
        self.assertEqual(resp["usage"]["completion_tokens"], 25)
        # 非流式响应也应捕获签名（供后续轮次注入）
        self.assertEqual(len(_thought_signature_cache), 1)

    def test_tool_calls_and_requires_action(self):
        interaction = {
            "id": "v1_yyy", "status": "requires_action",
            "steps": [
                {"type": "function_call", "id": "fc1", "name": "get_weather",
                 "arguments": {"city": "Paris"}},
            ],
            "usage": {},
        }
        resp = convert_interactions_to_openai_response(interaction, "m", "r2")
        msg = resp["choices"][0]["message"]
        self.assertEqual(resp["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "get_weather")
        args = json.loads(msg["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(args["city"], "Paris")


class InteractionsStreamConverterTests(unittest.TestCase):
    def setUp(self):
        _thought_signature_cache.clear()

    def test_text_stream(self):
        conv = InteractionsStreamConverter("m", "req")
        events = [
            {"event_type": "interaction.created", "interaction": {"id": "i1"}},
            {"event_type": "step.start", "index": 0, "step": {"type": "model_output"}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "text", "text": "Hel"}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "text", "text": "lo"}},
            {"event_type": "step.stop", "index": 0},
            {"event_type": "interaction.completed", "interaction": {
                "id": "i1", "status": "completed",
                "usage": {"total_input_tokens": 3, "total_output_tokens": 5,
                          "total_thought_tokens": 0, "total_tokens": 8}}},
        ]
        chunks = []
        for ev in events:
            chunks.extend(conv.feed(ev))
        texts = [c["choices"][0]["delta"].get("content", "") for c in chunks
                 if c.get("choices") and c["choices"][0]["delta"].get("content")]
        self.assertEqual("".join(texts), "Hello")
        finish = [c for c in chunks if c.get("choices") and c["choices"][0].get("finish_reason")]
        self.assertEqual(finish[0]["choices"][0]["finish_reason"], "stop")
        usage_chunks = [c for c in chunks if c.get("usage")]
        self.assertEqual(usage_chunks[0]["usage"]["completion_tokens"], 5)

    def test_thought_and_function_call_stream(self):
        conv = InteractionsStreamConverter("m", "req")
        events = [
            {"event_type": "step.start", "index": 0, "step": {"type": "thought"}},
            {"event_type": "step.delta", "index": 0,
             "delta": {"type": "thought_summary", "content": {"type": "text", "text": "Reasoning..."}}},
            {"event_type": "step.delta", "index": 0,
             "delta": {"type": "thought_signature", "signature": "sig_1"}},
            {"event_type": "step.stop", "index": 0},
            {"event_type": "step.start", "index": 1,
             "step": {"type": "function_call", "id": "fc2", "name": "get_weather", "arguments": {}}},
            {"event_type": "step.delta", "index": 1,
             "delta": {"type": "arguments_delta", "partial_arguments": "{\"city\":"}},
            {"event_type": "step.delta", "index": 1,
             "delta": {"type": "arguments_delta", "arguments": " \"Paris\"}"}},
            {"event_type": "step.stop", "index": 1},
            {"event_type": "interaction.completed",
             "interaction": {"id": "i1", "status": "requires_action", "usage": {}}},
        ]
        chunks = []
        for ev in events:
            chunks.extend(conv.feed(ev))
        reasoning = [c["choices"][0]["delta"].get("reasoning_content", "") for c in chunks
                     if c.get("choices") and c["choices"][0]["delta"].get("reasoning_content")]
        self.assertEqual("".join(reasoning), "Reasoning...")
        tool_chunks = [c for c in chunks if c.get("choices") and c["choices"][0]["delta"].get("tool_calls")]
        self.assertEqual(len(tool_chunks), 1)
        tc = tool_chunks[0]["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(tc["function"]["name"], "get_weather")
        args = json.loads(tc["function"]["arguments"])
        self.assertEqual(args["city"], "Paris")
        finish = [c for c in chunks if c.get("choices") and c["choices"][0].get("finish_reason")]
        self.assertEqual(finish[0]["choices"][0]["finish_reason"], "tool_calls")
        # 流式路径也应捕获思考签名
        self.assertTrue(any("Reasoning..." in key for key in _thought_signature_cache))

    def test_error_event(self):
        conv = InteractionsStreamConverter("m", "req")
        chunks = conv.feed({"event_type": "error", "error": {"message": "boom", "code": "internal"}})
        self.assertEqual(chunks[0]["error"]["message"], "boom")

    def test_unknown_event_ignored(self):
        conv = InteractionsStreamConverter("m", "req")
        chunks = conv.feed({"event_type": "interaction.status_update", "status": "in_progress"})
        self.assertEqual(chunks, [])


class GeminiGcInteractionsTests(unittest.TestCase):
    def setUp(self):
        _thought_signature_cache.clear()

    def test_gc_to_interactions_request(self):
        gc = {
            "contents": [
                {"role": "user", "parts": [{"text": "Hi"}]},
                {"role": "model", "parts": [{"text": "Hello"}]},
                {"role": "user", "parts": [
                    {"text": "Weather?"},
                    {"functionCall": {"name": "get_weather", "args": {"city": "Paris"}, "id": "fc1"}},
                ]},
                {"role": "user", "parts": [
                    {"functionResponse": {"name": "get_weather", "response": {"content": "Sunny"}, "id": "fc1"}},
                ]},
            ],
            "systemInstruction": {"role": "system", "parts": [{"text": "Be nice"}]},
            "generationConfig": {"temperature": 0.5, "maxOutputTokens": 100},
            "tools": [{"functionDeclarations": [{"name": "get_weather", "description": "d", "parameters": {}}]}],
            "toolConfig": {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["get_weather"]}},
        }
        body = convert_gemini_gc_to_interactions(gc, "gemini-3-flash-preview")
        self.assertEqual(body["model"], "gemini-3-flash-preview")
        self.assertIs(body["store"], False)
        self.assertEqual(body["system_instruction"], "Be nice")
        self.assertEqual(body["generation_config"]["temperature"], 0.5)
        self.assertEqual(body["generation_config"]["max_output_tokens"], 100)
        self.assertEqual(body["tools"][0]["type"], "function")
        self.assertEqual(body["tools"][0]["name"], "get_weather")
        self.assertEqual(body["generation_config"]["tool_choice"]["allowed_tools"]["tools"], ["get_weather"])
        types = [s["type"] for s in body["input"]]
        self.assertEqual(types, ["user_input", "model_output", "user_input", "function_call", "function_result"])
        # function_call step 拆出后，user 消息的文本块保留
        self.assertEqual(body["input"][2]["content"][0]["text"], "Weather?")
        self.assertEqual(body["input"][4]["result"][0]["text"], "Sunny")

    def test_gc_to_interactions_preserves_thought_signature_and_camel_case_media(self):
        gc = {
            "contents": [{
                "role": "model",
                "parts": [
                    {"text": "thinking", "thought": True, "thoughtSignature": "sig_native"},
                    {"text": "answer"},
                ],
            }, {
                "role": "user",
                "parts": [{"inlineData": {"mimeType": "image/png", "data": "AAAA"}}],
            }],
            "generationConfig": {"thinkingConfig": {"includeThoughts": True}},
        }
        body = convert_gemini_gc_to_interactions(gc, "gemini-3-flash-preview")
        self.assertEqual([step["type"] for step in body["input"]], [
            "thought", "model_output", "user_input"
        ])
        self.assertEqual(body["input"][0]["signature"], "sig_native")
        self.assertEqual(body["input"][0]["summary"][0]["text"], "thinking")
        self.assertEqual(body["input"][2]["content"][0]["type"], "image")
        self.assertEqual(body["generation_config"]["thinking_summaries"], "auto")

        response = convert_interactions_to_gemini_gc({
            "status": "completed",
            "steps": [{"type": "thought", "signature": "sig_native",
                        "summary": [{"type": "text", "text": "thinking"}]}],
            "usage": {},
        })
        thought_part = response["candidates"][0]["content"]["parts"][0]
        self.assertTrue(thought_part["thought"])
        self.assertEqual(thought_part["thoughtSignature"], "sig_native")

    def test_interactions_to_gc_response(self):
        interaction = {
            "id": "v1_x", "status": "completed",
            "steps": [
                {"type": "model_output", "content": [{"type": "text", "text": "Hi there"}]},
                {"type": "function_call", "id": "fc9", "name": "f", "arguments": {"a": 1}},
            ],
            "usage": {"total_input_tokens": 5, "total_output_tokens": 7,
                      "total_thought_tokens": 3, "total_tokens": 15},
        }
        resp = convert_interactions_to_gemini_gc(interaction)
        parts = resp["candidates"][0]["content"]["parts"]
        self.assertEqual(parts[0]["text"], "Hi there")
        self.assertEqual(parts[1]["functionCall"]["name"], "f")
        self.assertEqual(resp["candidates"][0]["finishReason"], "STOP")
        self.assertEqual(resp["usageMetadata"]["promptTokenCount"], 5)
        self.assertEqual(resp["usageMetadata"]["candidatesTokenCount"], 7)
        self.assertEqual(resp["usageMetadata"]["thoughtsTokenCount"], 3)
        # OpenAI 兼容 usage：思考 token 计入 completion
        self.assertEqual(resp["usage"]["completion_tokens"], 10)

    def test_interactions_to_gc_stream(self):
        conv = InteractionsToGeminiGCConverter()
        events = [
            {"event_type": "step.start", "index": 0, "step": {"type": "model_output"}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "text", "text": "Yo"}},
            {"event_type": "step.stop", "index": 0},
            {"event_type": "interaction.completed", "interaction": {
                "status": "completed",
                "usage": {"total_input_tokens": 1, "total_output_tokens": 2,
                          "total_thought_tokens": 0, "total_tokens": 3}}},
        ]
        chunks = []
        for ev in events:
            chunks.extend(conv.feed(ev))
        texts = [c["candidates"][0]["content"]["parts"][0]["text"] for c in chunks
                 if c.get("candidates") and c["candidates"][0].get("content")]
        self.assertEqual("".join(texts), "Yo")
        finish = [c for c in chunks if c.get("candidates") and c["candidates"][0].get("finishReason")]
        self.assertEqual(finish[0]["candidates"][0]["finishReason"], "STOP")
        usage_chunks = [c for c in chunks if "usageMetadata" in c]
        self.assertEqual(usage_chunks[0]["usageMetadata"]["candidatesTokenCount"], 2)

    def test_gc_stream_function_call(self):
        conv = InteractionsToGeminiGCConverter()
        events = [
            {"event_type": "step.start", "index": 0,
             "step": {"type": "function_call", "id": "fc7", "name": "f", "arguments": {}}},
            {"event_type": "step.delta", "index": 0,
             "delta": {"type": "arguments_delta", "arguments": "{\"x\": 1}"}},
            {"event_type": "step.stop", "index": 0},
        ]
        chunks = []
        for ev in events:
            chunks.extend(conv.feed(ev))
        self.assertEqual(len(chunks), 1)
        fc = chunks[0]["candidates"][0]["content"]["parts"][0]["functionCall"]
        self.assertEqual(fc["name"], "f")
        self.assertEqual(fc["args"], {"x": 1})
        self.assertEqual(fc["id"], "fc7")




class _FakeInteractionsResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return {"status": "completed", "steps": [], "usage": {}}


class _FakeInteractionsSession:
    def __init__(self):
        self.endpoint = None
        self.kwargs = None

    def post(self, endpoint, **kwargs):
        self.endpoint = endpoint
        self.kwargs = kwargs
        return _FakeInteractionsResponse()


class InteractionsHttpTests(unittest.TestCase):
    def test_header_auth_and_base_url_normalization(self):
        session = _FakeInteractionsSession()
        service = DirectAPIService.__new__(DirectAPIService)
        service.session = session

        async def collect():
            return [item async for item in service.call_gemini_interactions_api(
                api_key="AIza/test?key",
                model="gemini-3-flash-preview",
                messages=[{"role": "user", "content": "Hi"}],
                base_url="https://proxy.example/v1beta",
            )]

        result = asyncio.run(collect())
        self.assertEqual(result[0]["status"], "completed")
        self.assertEqual(session.endpoint, "https://proxy.example/v1beta/interactions")
        self.assertNotIn("key=", session.endpoint)
        self.assertEqual(session.kwargs["headers"]["x-goog-api-key"], "AIza/test?key")
        request_body = json.loads(session.kwargs["data"])
        self.assertEqual(request_body["model"], "gemini-3-flash-preview")


if __name__ == "__main__":
    unittest.main()
