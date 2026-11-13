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
    signature_cache,
    build_interactions_request_body,
    cache_thought_signatures,
    convert_gemini_gc_to_interactions,
    convert_interactions_to_gemini_gc,
    convert_interactions_to_openai_response,
    convert_oai_messages_to_interactions,
    match_and_inject_thought_signatures,
    interaction_failure,
    remember_turn_steps,
)
from services.direct_api_service import DirectAPIService
from routes._direct_api_gemini import upstream_signatures


def setUpModule():
    from core.request_context import RequestContext, current_request
    global _test_context_token
    _test_context_token = current_request.set(RequestContext(authenticated=True))


def tearDownModule():
    from core.request_context import current_request
    current_request.reset(_test_context_token)


class OaiMessagesToInteractionsTests(unittest.TestCase):
    def setUp(self):
        signature_cache().clear()

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

    def test_developer_role_counts_as_system(self):
        steps, system = convert_oai_messages_to_interactions([{"role": "developer", "content": "规则"}])
        self.assertEqual(steps, [])
        self.assertEqual(system, "规则")

    def test_unknown_role_skipped(self):
        steps, system = convert_oai_messages_to_interactions([{"role": "narrator", "content": "ignored"}])
        self.assertEqual(steps, [])
        self.assertEqual(system, "")


class ThoughtSignatureCacheTests(unittest.TestCase):
    def setUp(self):
        signature_cache().clear()

    def test_full_match(self):
        cache_thought_signatures([("AAAA ", "sig1"), ("BBBB", "sig2")])
        steps = match_and_inject_thought_signatures("AAAA BBBB")
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["signature"], "sig1")
        self.assertEqual(steps[1]["signature"], "sig2")

    def test_truncated_text_never_receives_a_signature(self):
        cache_thought_signatures([("AAAA ", "sig1"), ("BBBB", "sig2")])
        self.assertEqual(match_and_inject_thought_signatures("AAAA"), [])
        self.assertEqual(match_and_inject_thought_signatures("AA"), [])

    def test_no_match(self):
        cache_thought_signatures([("AAAA ", "sig1")])
        self.assertEqual(match_and_inject_thought_signatures("Totally different"), [])
        self.assertEqual(match_and_inject_thought_signatures(""), [])

    def test_fragments_without_signature_not_cached(self):
        cache_thought_signatures([("AAAA ", ""), ("BBBB", "sig2")])
        # 无签名的片段整体不入缓存（valid 过滤后剩一个也不够完整，joined 非空则仍缓存）
        self.assertEqual(len(signature_cache()), 1)


class BuildRequestBodyTests(unittest.TestCase):
    def setUp(self):
        signature_cache().clear()

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
        # 现行 schema 的 generation_config 没有采样字段
        self.assertNotIn("temperature", body["generation_config"])
        self.assertEqual(body["generation_config"]["max_output_tokens"], 100)
        self.assertEqual(body["generation_config"]["thinking_level"], "low")
        self.assertEqual(body["generation_config"]["thinking_summaries"], "auto")

    def test_tools_passthrough_and_tool_choice_none(self):
        tools = [{"type": "function", "name": "f", "description": "d", "parameters": {"type": "object"}}]
        body = build_interactions_request_body("m", [{"role": "user", "content": "x"}], tools=tools)
        self.assertEqual(body["tools"], tools)
        # tool_choice=none 保留 tools，缓存前缀才不会变
        body2 = build_interactions_request_body(
            "m", [{"role": "user", "content": "x"}], tools=tools, tool_choice="none")
        self.assertEqual(body2["tools"], tools)
        self.assertEqual(body2["generation_config"]["tool_choice"], "none")
        no_tools = build_interactions_request_body(
            "m", [{"role": "user", "content": "x"}], tool_choice="none")
        self.assertNotIn("generation_config", no_tools)

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
        self.assertNotIn("response_mime_type", body)
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

    def test_thinking_budget_becomes_nearest_level(self):
        body = build_interactions_request_body(
            "m", [{"role": "user", "content": "x"}],
            thinking_config={"thinkingBudget": 20000, "includeThoughts": True})
        self.assertEqual(body["generation_config"]["thinking_level"], "high")
        dynamic = build_interactions_request_body(
            "m", [{"role": "user", "content": "x"}],
            thinking_config={"thinkingBudget": -1, "includeThoughts": True})
        self.assertNotIn("thinking_level", dynamic["generation_config"])

    def test_thinking_level_passes_through_unmodified(self):
        body = build_interactions_request_body(
            "gemini-3.8-flash", [{"role": "user", "content": "x"}],
            thinking_config={"thinkingLevel": "MINIMAL"})
        self.assertEqual(body["generation_config"]["thinking_level"], "minimal")

    def test_xhigh_passes_through_on_any_model(self):
        for model in ("gemini-4-hydrogen", "gemini-4-carbon", "gemini-3.8-flash"):
            body = build_interactions_request_body(
                model, [{"role": "user", "content": "x"}],
                thinking_config={"thinkingLevel": "XHIGH", "includeThoughts": True})
            self.assertEqual(body["generation_config"]["thinking_level"], "xhigh")

    def test_allowed_tools_use_plain_names(self):
        tools = [{"type": "function", "name": "f", "parameters": {"type": "object"}}]
        body = build_interactions_request_body(
            "m", [{"role": "user", "content": "x"}], tools=tools,
            tool_choice={"type": "allowed_tools", "allowed_tools": {
                "mode": "required", "tools": [{"type": "function", "function": {"name": "f"}}]}})
        self.assertEqual(body["generation_config"]["tool_choice"],
                         {"allowed_tools": {"mode": "any", "tools": ["f"]}})

    def test_include_thoughts_false_maps_to_none(self):
        body = build_interactions_request_body(
            "m", [{"role": "user", "content": "x"}],
            thinking_config={"includeThoughts": False})
        self.assertEqual(body["generation_config"]["thinking_summaries"], "none")
        self.assertNotIn("thinking_level", body["generation_config"])


class InteractionsToOpenaiResponseTests(unittest.TestCase):
    def setUp(self):
        signature_cache().clear()

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
        self.assertEqual(len(signature_cache()), 1)

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
        signature_cache().clear()

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
        # 调用头在 step.start 发出，参数片段按原文逐段转发
        pieces = [delta["tool_calls"][0] for delta in _deltas(chunks) if delta.get("tool_calls")]
        self.assertEqual([(tc.get("id"), tc["function"].get("name")) for tc in pieces],
                         [("fc2", "get_weather"), (None, None), (None, None)])
        self.assertEqual([tc["function"]["arguments"] for tc in pieces], ["", "{\"city\":", " \"Paris\"}"])
        self.assertEqual(_tool_calls(chunks), [{"id": "fc2", "name": "get_weather", "arguments": {"city": "Paris"}}])
        finish = [c for c in chunks if c.get("choices") and c["choices"][0].get("finish_reason")]
        self.assertEqual(finish[0]["choices"][0]["finish_reason"], "tool_calls")
        # 流式路径也应捕获思考签名
        self.assertTrue(any("Reasoning..." in key for key in signature_cache()))
        self.assertEqual(conv.signatures, ["sig_1"])

    def test_upstream_signatures_follow_block_order(self):
        generate = {"candidates": [{"content": {"parts": [
            {"text": "想", "thought": True},
            {"functionCall": {"name": "f", "args": {}}, "thoughtSignature": "call_sig"},
            {"text": "答", "thoughtSignature": "text_sig"},
        ]}}]}
        self.assertEqual(upstream_signatures(generate), ["call_sig", "text_sig"])
        interaction = {"steps": [{"type": "thought", "signature": "thought_sig"},
                                 {"type": "model_output", "content": []}]}
        self.assertEqual(upstream_signatures(interaction), ["thought_sig"])
        self.assertEqual(upstream_signatures({"candidates": [{"content": {"parts": [{"text": "无签名"}]}}]}), [])

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
        signature_cache().clear()

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
        self.assertNotIn("temperature", body["generation_config"])
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

    def test_gc_stream_keeps_call_signature_like_the_json_response(self):
        events = [
            {"event_type": "step.start", "index": 0, "step": {"type": "function_call", "id": "fc7", "name": "f"}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "arguments_delta", "arguments": "{\"x\":"}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "arguments_delta", "arguments": " 1}"}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "thought_signature", "signature": "call_sig"}},
            {"event_type": "step.stop", "index": 0},
        ]
        chunks = _feed_all(InteractionsToGeminiGCConverter(), events)
        self.assertEqual(chunks, [{"candidates": [{"content": {"role": "model", "parts": [
            {"functionCall": {"name": "f", "args": {"x": 1}, "id": "fc7"}, "thoughtSignature": "call_sig"}]}}]}])
        stored = {"steps": [{"type": "function_call", "id": "fc7", "name": "f", "arguments": {"x": 1},
                             "signature": "call_sig"}]}
        self.assertEqual(convert_interactions_to_gemini_gc(stored)["candidates"][0]["content"]["parts"],
                         chunks[0]["candidates"][0]["content"]["parts"])




def _feed_all(converter, events):
    chunks = []
    for event in events:
        chunks.extend(converter.feed(event))
    return chunks


def _deltas(chunks):
    return [chunk["choices"][0]["delta"] for chunk in chunks if chunk.get("choices")]


def _tool_calls(chunks):
    """按 index 拼起流式 tool_calls，参数解析成对象。"""
    calls = {}
    for delta in _deltas(chunks):
        for piece in delta.get("tool_calls") or []:
            call = calls.setdefault(piece["index"], {"id": None, "name": None, "arguments": ""})
            call["id"] = piece.get("id") or call["id"]
            call["name"] = piece.get("function", {}).get("name") or call["name"]
            call["arguments"] += piece.get("function", {}).get("arguments") or ""
    return [{**call, "arguments": json.loads(call["arguments"])} for _, call in sorted(calls.items())]


class InteractionsAuditRegressionTests(unittest.TestCase):
    """2026-11 审计发现的问题，流式和非流式都要覆盖。"""

    def setUp(self):
        from core.request_context import current_request
        context = current_request.get()
        context.artifacts.clear()
        context.outcome.clear()

    PARALLEL = [
        {"event_type": "step.start", "index": 0, "step": {"type": "function_call", "id": "a", "name": "f", "arguments": {}}},
        {"event_type": "step.delta", "index": 0, "delta": {"type": "arguments_delta", "arguments": '{"x": 1}'}},
        {"event_type": "step.stop", "index": 0},
        {"event_type": "step.start", "index": 1, "step": {"type": "function_call", "id": "b", "name": "g", "arguments": {}}},
        {"event_type": "step.delta", "index": 1, "delta": {"type": "arguments_delta", "arguments": '{"y": 2}'}},
        {"event_type": "step.stop", "index": 1},
        {"event_type": "interaction.completed", "interaction": {"status": "requires_action", "usage": {}}},
    ]

    def test_parallel_stream_calls_get_their_own_index(self):
        chunks = _feed_all(InteractionsStreamConverter("m", "r"), self.PARALLEL)
        calls = [delta["tool_calls"][0] for delta in _deltas(chunks) if delta.get("tool_calls")]
        self.assertEqual([call["index"] for call in calls], [0, 0, 1, 1])
        self.assertEqual(_tool_calls(chunks), [{"id": "a", "name": "f", "arguments": {"x": 1}},
                                               {"id": "b", "name": "g", "arguments": {"y": 2}}])

    def test_stream_call_header_precedes_arguments_and_gets_an_id(self):
        converter = InteractionsStreamConverter("m", "r")
        header = converter.feed({"event_type": "step.start", "index": 0, "step": {
            "type": "function_call", "name": "f", "arguments": {"a": 1}}})
        call = _deltas(header)[0]["tool_calls"][0]
        self.assertTrue(call["id"].startswith("call_"))
        # 起始事件里已有的参数随调用头一起发出，不会丢
        self.assertEqual(call["function"], {"name": "f", "arguments": '{"a": 1}'})
        self.assertEqual(converter.feed({"event_type": "step.stop", "index": 0}), [])
        # 历史里保存的是同一个编号，客户端回传结果时才能对上
        self.assertEqual(converter.assembler.steps[0]["id"], call["id"])

    def test_tool_calls_force_tool_finish_even_when_status_is_completed(self):
        events = self.PARALLEL[:-1] + [{"event_type": "interaction.completed",
                                        "interaction": {"status": "completed", "usage": {}}}]
        finishes = [chunk["choices"][0]["finish_reason"] for chunk in
                    _feed_all(InteractionsStreamConverter("m", "r"), events) if chunk.get("choices")]
        self.assertIn("tool_calls", finishes)

    def test_failed_status_is_an_error_not_a_stop(self):
        interaction = {"status": "failed", "errors": [{"code": "internal", "message": "炸了"}],
                       "usage": {"total_input_tokens": 9}}
        chunks = InteractionsStreamConverter("m", "r").feed(
            {"event_type": "interaction.completed", "interaction": interaction})
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["error"]["message"], "炸了")
        failure = interaction_failure(interaction)
        self.assertEqual(failure["error"]["code"], 502)
        self.assertEqual(failure["usageMetadata"]["promptTokenCount"], 9)
        self.assertEqual(interaction_failure({"status": "failed", "errors": [{"code": "safety"}]})["error"]["code"], 400)
        self.assertIsNone(interaction_failure({"status": "completed"}))

    def test_incomplete_and_continuation_report_length(self):
        for status in ("incomplete", "requires_continuation"):
            response = convert_interactions_to_openai_response({"status": status, "steps": [], "usage": {}}, "m", "r")
            self.assertEqual(response["choices"][0]["finish_reason"], "length")

    def test_transport_error_without_event_type_is_not_swallowed(self):
        chunks = InteractionsStreamConverter("m", "r").feed({"error": {"message": "断了"}})
        self.assertEqual(chunks[0]["error"]["message"], "断了")

    def _thought_turn(self):
        return [
            {"event_type": "step.start", "index": 0, "step": {"type": "thought"}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "thought_summary", "content": {"type": "text", "text": "第一段"}}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "thought_signature", "signature": "s1"}},
            {"event_type": "step.stop", "index": 0},
            {"event_type": "step.start", "index": 1, "step": {"type": "thought"}},
            {"event_type": "step.delta", "index": 1, "delta": {"type": "thought_summary", "content": {"type": "text", "text": "第二段"}}},
            {"event_type": "step.delta", "index": 1, "delta": {"type": "thought_signature", "signature": "s2"}},
            {"event_type": "step.stop", "index": 1},
            {"event_type": "step.start", "index": 2, "step": {"type": "model_output"}},
            {"event_type": "step.delta", "index": 2, "delta": {"type": "text", "text": "答"}},
            {"event_type": "step.stop", "index": 2},
            {"event_type": "interaction.completed", "interaction": {"status": "completed", "usage": {}}},
        ]

    def test_multi_segment_thoughts_replay_from_joined_reasoning(self):
        converter = InteractionsStreamConverter("m", "r")
        chunks = _feed_all(converter, self._thought_turn())
        self.assertEqual(converter.signatures, ["s1", "s2"])
        reasoning = "".join(delta.get("reasoning_content", "") for delta in _deltas(chunks))
        self.assertEqual(reasoning, "第一段第二段")
        steps, _ = convert_oai_messages_to_interactions([
            {"role": "assistant", "content": "答", "reasoning_content": reasoning}])
        self.assertEqual([step.get("signature") for step in steps if step["type"] == "thought"], ["s1", "s2"])

    def test_stream_metadata_matches_non_stream_steps(self):
        chunks = _feed_all(InteractionsStreamConverter("m", "r"), self._thought_turn())
        # 每段思考结束时另有单独的签名片段，整轮步骤在完成时下发
        steps = next(delta["provider_metadata"]["interactions_steps"] for delta in _deltas(chunks)
                     if "interactions_steps" in (delta.get("provider_metadata") or {}))
        self.assertEqual([step["type"] for step in steps], ["thought", "thought", "model_output"])
        self.assertEqual(steps[1]["summary"], [{"type": "text", "text": "第二段"}])
        self.assertEqual(steps[2]["content"], [{"type": "text", "text": "答"}])

    def _signed_call_turn(self):
        return [
            {"event_type": "step.start", "index": 0, "step": {"type": "thought", "signature": "only_sig"}},
            {"event_type": "step.stop", "index": 0},
            {"event_type": "step.start", "index": 1, "step": {"type": "function_call", "id": "c1", "name": "f", "arguments": {"a": 1}}},
            {"event_type": "step.stop", "index": 1},
            {"event_type": "interaction.completed", "interaction": {"status": "requires_action", "usage": {}}},
        ]

    def test_signature_only_thought_is_restored_by_tool_call_id(self):
        _feed_all(InteractionsStreamConverter("m", "r"), self._signed_call_turn())
        message = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": '{"a": 1}'}}]}
        steps, _ = convert_oai_messages_to_interactions([
            message, {"role": "tool", "tool_call_id": "c1", "content": "ok"}])
        self.assertEqual([step["type"] for step in steps], ["thought", "function_call", "function_result"])
        self.assertEqual(steps[0]["signature"], "only_sig")
        self.assertEqual(steps[2]["name"], "f")

    def test_edited_history_does_not_receive_a_signature(self):
        _feed_all(InteractionsStreamConverter("m", "r"), self._signed_call_turn())
        steps, _ = convert_oai_messages_to_interactions([{"role": "assistant", "content": "改过的正文", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}])
        self.assertNotIn("thought", [step["type"] for step in steps])

    def test_each_turn_is_queued_as_its_own_record(self):
        # 长对话不能有条数上限：淘汰最早的签名会改写历史开头，之后每轮都打不中上游缓存
        for index in range(80):
            remember_turn_steps([{"type": "thought", "signature": "s"},
                                 {"type": "function_call", "id": f"c{index}", "name": "f", "arguments": {}}])
        from core.request_context import current_request
        pending = current_request.get().artifacts["pending_signatures"]
        self.assertEqual([keys for keys, _ in pending], [[f"interactions-turn:c{index}"] for index in range(80)])

    def test_rewound_session_restores_signatures_saved_by_the_original_session(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from core.conversation_store import ConversationStore
        from core.request_context import RequestContext, current_request
        from converters.gemini_interactions import load_signature_history

        history = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "f", "arguments": '{"a": 1}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            {"role": "assistant", "content": "答", "reasoning_content": "想一想"},
            {"role": "user", "content": "继续"},
        ]

        def context(**fields):
            return RequestContext(authenticated=True, owner_id="admin", model="m", **fields)

        def restored(request_context):
            token = current_request.set(request_context)
            try:
                asyncio.run(load_signature_history(history))
                steps, _ = convert_oai_messages_to_interactions(history)
            finally:
                current_request.reset(token)
            return [step["signature"] for step in steps if step["type"] == "thought"]

        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "state.db")
            with patch("converters.gemini_interactions.conversation_store", store):
                original = context(session_id="cli-original")
                token = current_request.set(original)
                try:
                    _feed_all(InteractionsStreamConverter("m", "r"), self._signed_call_turn())
                    cache_thought_signatures([("想一想", "sig_text")])
                finally:
                    current_request.reset(token)
                store.remember_entries("admin", original.cache_session(), original.artifacts["pending_signatures"])

                self.assertEqual(restored(context(session_id="cli-after-rewind")), ["only_sig", "sig_text"])
                # 客户端显式要求的 X-Bridge-Session-ID 仍然隔离
                self.assertEqual(restored(context(session_id="bridge", explicit_session=True)), [])

    def test_visible_output_is_identical_for_stream_and_non_stream(self):
        annotation = {"type": "url_citation", "url": "https://a.test", "title": "A"}
        code = {"language": "python", "code": "print(1)"}
        steps = [
            {"type": "code_execution_call", "id": "x", "arguments": code},
            {"type": "code_execution_result", "call_id": "x", "result": "1"},
            {"type": "google_search_call", "id": "g", "arguments": {"queries": ["q"]}},
            {"type": "model_output", "content": [
                {"type": "text", "text": "正文", "annotations": [annotation]},
                {"type": "image", "data": "QUJD", "mime_type": "image/png"}]},
        ]
        message = convert_interactions_to_openai_response(
            {"status": "completed", "steps": steps, "usage": {}}, "m", "r")["choices"][0]["message"]
        events = []
        for index, step in enumerate(steps):
            events.append({"event_type": "step.start", "index": index, "step": {"type": step["type"], "id": step.get("id")}})
            if step["type"] == "model_output":
                events.append({"event_type": "step.delta", "index": index, "delta": {"type": "text", "text": "正文"}})
                events.append({"event_type": "step.delta", "index": index, "delta": {"type": "text_annotation_delta", "annotations": [annotation]}})
                events.append({"event_type": "step.delta", "index": index, "delta": {"type": "image", "data": "QUJD", "mime_type": "image/png"}})
            else:
                events.append({"event_type": "step.delta", "index": index,
                               "delta": {k: v for k, v in step.items() if k != "id"}})
            events.append({"event_type": "step.stop", "index": index})
        events.append({"event_type": "interaction.completed", "interaction": {"status": "completed", "usage": {}}})
        deltas = _deltas(_feed_all(InteractionsStreamConverter("m", "r"), events))
        self.assertEqual("".join(delta.get("content", "") for delta in deltas), message["content"])
        self.assertEqual([item for delta in deltas for item in delta.get("images", [])], message["images"])
        self.assertEqual([item for delta in deltas for item in delta.get("annotations", [])], message["annotations"])
        self.assertIn("```python\nprint(1)\n```", message["content"])
        self.assertEqual(message["annotations"][0]["url_citation"]["url"], "https://a.test")

    def test_usage_counts_tool_prompts_and_first_hop_only(self):
        usage = {"total_input_tokens": 300, "initial_input_tokens": 100, "total_tool_use_tokens": 20,
                 "total_output_tokens": 10, "total_thought_tokens": 5, "total_cached_tokens": 50, "total_tokens": 999}
        result = convert_interactions_to_openai_response({"status": "completed", "steps": [], "usage": usage}, "m", "r")["usage"]
        self.assertEqual(result["prompt_tokens"], 120)
        self.assertEqual(result["total_tokens"], 135)

    def test_tool_error_and_media_results(self):
        pdf = {"type": "file", "file": {"file_data": "data:application/pdf;base64,QUJD", "filename": "a.pdf"}}
        call = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}
        steps, _ = convert_oai_messages_to_interactions(
            [call, {"role": "tool", "tool_call_id": "a", "content": "[tool_result_error] 失败"}], "gemini-3.8-flash")
        self.assertTrue(steps[-1]["is_error"])
        self.assertEqual(steps[-1]["result"][0]["text"], "失败")
        modern, _ = convert_oai_messages_to_interactions(
            [call, {"role": "tool", "tool_call_id": "a", "content": [pdf]}], "gemini-3.8-flash")
        self.assertEqual(modern[-1]["result"][0]["type"], "document")
        older, _ = convert_oai_messages_to_interactions(
            [call, {"role": "tool", "tool_call_id": "a", "content": [pdf]}], "gemini-2.5-flash-lite")
        self.assertEqual(older[-1]["type"], "user_input")
        self.assertEqual(older[-1]["content"][-1]["type"], "document")

    def test_orphan_tool_result_does_not_invent_a_name(self):
        steps, _ = convert_oai_messages_to_interactions([{"role": "tool", "tool_call_id": "call_x", "content": "r"}])
        self.assertNotIn("name", steps[0])


class GenerateContentAuditRegressionTests(unittest.TestCase):
    def setUp(self):
        from core.request_context import current_request
        current_request.get().outcome.clear()

    def test_function_declarations_read_json_schema_and_builtin_tools_survive(self):
        schema = {"type": "object", "properties": {"x": {"type": "number"}}}
        body = convert_gemini_gc_to_interactions({
            "contents": "hi",
            "tools": [{"functionDeclarations": [{"name": "f", "parametersJsonSchema": schema}]},
                      {"googleSearch": {}}, {"urlContext": {}}, {"codeExecution": {}},
                      {"fileSearch": {"fileSearchStoreNames": ["s"], "topK": 3}}],
        }, "gemini-3.8-flash")
        self.assertEqual(body["tools"][0]["parameters"], schema)
        self.assertEqual([tool["type"] for tool in body["tools"][1:]],
                         ["google_search", "url_context", "code_execution", "file_search"])
        self.assertEqual(body["tools"][4]["file_search_store_names"], ["s"])
        self.assertEqual(body["tools"][4]["top_k"], 3)

    def test_generation_config_and_safety_mapping(self):
        schema = {"type": "object"}
        body = convert_gemini_gc_to_interactions({
            "contents": "hi",
            "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema, "seed": 7,
                                 "temperature": 0.2, "responseModalities": ["TEXT", "IMAGE"]},
            "safetySettings": [{"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"}],
        }, "m")
        self.assertNotIn("response_mime_type", body)
        self.assertEqual(body["response_format"], [
            {"type": "text", "mime_type": "application/json", "schema": schema}, {"type": "image"}])
        self.assertEqual(body["generation_config"], {"seed": 7})
        self.assertEqual(body["safety_settings"], [{"type": "harassment", "threshold": "off"}])
        from core.conversion_report import conversion_notes
        self.assertIn("generationConfig.temperature", [note["field"] for note in conversion_notes()])

    def test_thinking_config_xhigh_survives_generate_content_bridge(self):
        body = convert_gemini_gc_to_interactions({
            "contents": "hi",
            "generationConfig": {"thinkingConfig": {"thinkingLevel": "XHIGH", "includeThoughts": True}},
        }, "gemini-4-carbon")
        self.assertEqual(body["generation_config"]["thinking_level"], "xhigh")
        self.assertEqual(body["generation_config"]["thinking_summaries"], "auto")

    def test_mode_none_keeps_tools(self):
        body = convert_gemini_gc_to_interactions({
            "contents": "hi",
            "tools": [{"functionDeclarations": [{"name": "f"}]}],
            "toolConfig": {"functionCallingConfig": {"mode": "NONE"}},
        }, "m")
        self.assertEqual(len(body["tools"]), 1)
        self.assertEqual(body["generation_config"]["tool_choice"], "none")

    def test_calls_without_ids_get_stable_ids_and_results_match(self):
        request = {"contents": [
            {"role": "model", "parts": [{"functionCall": {"name": "f", "args": {}}},
                                        {"functionCall": {"name": "f", "args": {"n": 2}}}]},
            {"role": "user", "parts": [{"functionResponse": {"name": "f", "response": {"result": "1"}}},
                                       {"functionResponse": {"name": "f", "response": {"error": "坏了"}}}]},
        ]}
        first = convert_gemini_gc_to_interactions(request, "m")["input"]
        self.assertEqual(first, convert_gemini_gc_to_interactions(request, "m")["input"])
        self.assertEqual([first[2]["call_id"], first[3]["call_id"]], [first[0]["id"], first[1]["id"]])
        self.assertNotEqual(first[0]["id"], first[1]["id"])
        self.assertTrue(first[3]["is_error"])

    def test_response_maps_code_grounding_usage_and_signature_position(self):
        response = convert_interactions_to_gemini_gc({"status": "completed", "steps": [
            {"type": "google_search_call", "id": "g", "arguments": {"queries": ["q"]}},
            {"type": "google_search_result", "call_id": "g", "result": [], "search_suggestions": "<div/>"},
            {"type": "code_execution_call", "id": "x", "arguments": {"language": "python", "code": "1"}},
            {"type": "code_execution_result", "call_id": "x", "result": "1", "is_error": True},
            {"type": "model_output", "content": [{"type": "text", "text": "答", "annotations": [
                {"type": "url_citation", "url": "https://a.test", "title": "A"}]}]},
            {"type": "function_call", "id": "c", "name": "f", "arguments": {}, "signature": "s"},
        ], "usage": {"total_input_tokens": 5, "total_tool_use_tokens": 2, "total_output_tokens": 1, "total_tokens": 8}})
        candidate = response["candidates"][0]
        parts = candidate["content"]["parts"]
        self.assertEqual(parts[0]["executableCode"]["language"], "PYTHON")
        self.assertEqual(parts[1]["codeExecutionResult"]["outcome"], "OUTCOME_FAILED")
        self.assertEqual(parts[3]["thoughtSignature"], "s")
        self.assertNotIn("thoughtSignature", parts[3]["functionCall"])
        self.assertEqual(candidate["groundingMetadata"]["webSearchQueries"], ["q"])
        self.assertEqual(candidate["groundingMetadata"]["groundingChunks"][0]["web"]["uri"], "https://a.test")
        self.assertEqual(response["usageMetadata"]["promptTokenCount"], 5)
        self.assertEqual(response["usageMetadata"]["toolUsePromptTokenCount"], 2)

    def test_stream_thoughts_arrive_incrementally_and_failure_stays_quiet(self):
        converter = InteractionsToGeminiGCConverter()
        chunks = _feed_all(converter, [
            {"event_type": "step.start", "index": 0, "step": {"type": "thought"}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "thought_summary", "content": {"type": "text", "text": "想"}}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "thought_signature", "signature": "s"}},
            {"event_type": "step.stop", "index": 0},
        ])
        self.assertEqual(chunks[0]["candidates"][0]["content"]["parts"], [{"text": "想", "thought": True}])
        self.assertEqual(chunks[1]["candidates"][0]["content"]["parts"], [{"thought": True, "thoughtSignature": "s"}])
        self.assertEqual(converter.feed({"event_type": "interaction.completed",
                                         "interaction": {"status": "failed", "usage": {}}}), [])

    def test_native_accounting_reads_tool_tokens_and_step_text(self):
        from services.native_exchange import native_usage, text_from_native
        inputs, outputs, _, _ = native_usage({"usage": {"total_input_tokens": 10, "total_tool_use_tokens": 4,
                                                        "total_output_tokens": 3, "total_thought_tokens": 2}})
        self.assertEqual((inputs, outputs), (14, 5))
        self.assertEqual(text_from_native({"event_type": "step.delta", "delta": {"type": "text", "text": "片"}}), "片")
        self.assertEqual(text_from_native({"steps": [{"type": "model_output", "content": [{"type": "text", "text": "整"}]}]}), "整")


class _FakeInteractionsResponse:
    status = 200
    headers = {}

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
