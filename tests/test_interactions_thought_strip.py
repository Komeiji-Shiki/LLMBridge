"""Interactions 总结小模型原生思维链剥离的单元测试。"""
import asyncio
import json
import unittest

from converters.gemini_interactions import (
    InteractionsStreamConverter,
    InteractionsToGeminiGCConverter,
    convert_interactions_to_gemini_gc,
    convert_interactions_to_openai_response,
    convert_oai_messages_to_interactions,
    signature_cache,
)
from converters.interactions_thought_strip import (
    InteractionsNativeStreamStripper,
    ThoughtSummaryStreamStripper,
    is_summarizer_leaked_thought,
    strip_interactions_response_thoughts,
    strip_summarizer_leaked_thoughts,
    strip_thought_step,
)


def setUpModule():
    from core.request_context import RequestContext, current_request
    global _test_context_token
    _test_context_token = current_request.set(RequestContext(authenticated=True))


def tearDownModule():
    from core.request_context import current_request
    current_request.reset(_test_context_token)


class ThoughtStripPureTests(unittest.TestCase):
    def test_normal_summary_unchanged(self):
        normal = "**Acquiring and Applying Supplies**\n\nI am prioritizing buying tape first.\n\n"
        self.assertFalse(is_summarizer_leaked_thought(normal))
        self.assertEqual(strip_summarizer_leaked_thoughts(normal), normal)

    def test_non_summarizer_thinking_process_preserved(self):
        raw = (
            "Thinking Process:\n\n"
            "1. Understand the user's math question: solve x^2 - 5x + 6 = 0.\n"
            "2. Factorize as (x - 2)(x - 3) = 0.\n"
            "3. Therefore x = 2 or x = 3."
        )
        self.assertFalse(is_summarizer_leaked_thought(raw))
        self.assertEqual(strip_summarizer_leaked_thoughts(raw), raw)

    def test_variant_1_final_polish_plus_glued_output(self):
        leaked = (
            "Thinking Process:\n\n"
            "1.  **Analyze the Request:**\n"
            "    *   **Goal:** Summarize the provided thoughts in the first person (\"I\").\n"
            "    *   **Length:** Maximum 50 words for the summary paragraph.\n"
            "    *   **Format:** Header 3-7 words, starts with a verb, wrapped in double asterisks (`**Header**`).\n\n"
            "6.  **Final Polish:**\n"
            "    **Deconstructing initial riddle parameters**\n\n"
            "    I am carefully evaluating the first trick question.**Deconstructing initial riddle parameters**\n\n"
            "I am carefully evaluating the first trick question.\n\n"
        )
        self.assertTrue(is_summarizer_leaked_thought(leaked))
        self.assertEqual(
            strip_summarizer_leaked_thoughts(leaked),
            "**Deconstructing initial riddle parameters**\n\nI am carefully evaluating the first trick question.\n\n",
        )

    def test_variant_2_only_final_polish_indented(self):
        leaked = (
            "Thinking Process:\n\n"
            "1.  **Analyze the Request:**\n"
            "    *   **Task:** Summarize the thoughts in the first person (\"I\").\n"
            "    *   **Header:** 3-7 words, starts with a verb.\n\n"
            "6.  **Final Polish:**\n"
            "    **Verify physics constraints and coordinates**\n\n"
            "    Confirming the single-play freeze matches screenshot 6 perfectly, I am inspecting bone mappings.\n\n"
        )
        self.assertEqual(
            strip_summarizer_leaked_thoughts(leaked),
            "**Verify physics constraints and coordinates**\n\n"
            "Confirming the single-play freeze matches screenshot 6 perfectly, I am inspecting bone mappings.\n\n",
        )

    def test_variant_3_quadruple_asterisk_glued_after_step_title(self):
        leaked = (
            "Thinking Process:\n\n"
            "1.  **Analyze the Request:**\n"
            "    *   **Goal:** Summarize the provided thoughts.\n"
            "    *   **Constraints:** Maximum 50 words, never repeat a header.\n\n"
            "6.  **Output Generation:****Refining my voice and tone**\n\n"
            "I am absorbing the new writing guidelines and dropping filler.\n\n"
        )
        self.assertEqual(
            strip_summarizer_leaked_thoughts(leaked),
            "**Refining my voice and tone**\n\n"
            "I am absorbing the new writing guidelines and dropping filler.\n\n",
        )

    def test_variant_4_duplicate_trailing_or_double_header(self):
        leaked = (
            "Thinking Process:\n\n"
            "1.  **Analyze the Request:**\n"
            "    *   **Task:** Summarize the provided thoughts.\n"
            "    *   **Length:** Max 50 words.\n\n"
            "**Organizing final technical output**\n\n"
            "**Assembling final structured response**\n\n"
            "I am compiling all verified details into the final structured breakdown.\n\n"
            "**Assembling final structured response**\n\n"
        )
        self.assertEqual(
            strip_summarizer_leaked_thoughts(leaked),
            "**Assembling final structured response**\n\n"
            "I am compiling all verified details into the final structured breakdown.\n\n",
        )

    def test_variant_5_truncated_without_summary_returns_empty(self):
        truncated = (
            "Thinking Process:\n\n"
            "1.  **Analyze the Request:**\n"
            "    *   **Task:** Summarize the provided thoughts.\n"
            "    *   **Input Thoughts:** \"Analyzing 10 questions...\""
        )
        self.assertEqual(strip_summarizer_leaked_thoughts(truncated), "")

        no_output = (
            "Thinking Process:\n\n"
            "1.  **Analyze the Request:**\n"
            "    *   **Goal:** Summarize the thoughts.\n"
            "    *   **Constraints:** 3-7 words, starts with a verb.\n\n"
            "6.  **Output Generation.**\n\n"
        )
        self.assertEqual(strip_summarizer_leaked_thoughts(no_output), "")

    def test_quoted_thinking_process_inside_input_not_mis_split(self):
        leaked = (
            "Thinking Process:\n\n"
            "1.  **Analyze the Request:**\n"
            "    *   **Goal:** Summarize the provided thoughts.\n"
            "    *   **Input Thoughts:**\n"
            "        *   \"Key realization: Stripping `Thinking Process:` before sending to client.\"\n"
            "    *   **Constraints:** Max 50 words.\n\n"
            "6.  **Final Polish:**\n"
            "    **Designing the thought stripper**\n\n"
            "    I am anchoring split points strictly to line-start headers.\n\n"
        )
        self.assertEqual(
            strip_summarizer_leaked_thoughts(leaked),
            "**Designing the thought stripper**\n\n"
            "I am anchoring split points strictly to line-start headers.\n\n",
        )

    def test_multiple_sections_and_multiline_list_summary(self):
        sec1 = (
            "Thinking Process:\n\n"
            "1. **Analyze the Request:** Summarize the provided thoughts. Max 50 words.\n\n"
            "**Checking initial parameters**\n\n"
            "I am checking the initial state.\n\n"
        )
        sec2 = (
            "Thinking Process:\n\n"
            "1. **Analyze the Request:** Summarize the text. Start with a **bolded header**.\n\n"
            "**Strategic Execution Plan**\n\n"
            "I have locked in the plan:\n"
            "1. **First file**: update scene one.\n"
            "2. **Second file**: update scene two.\n\n"
            "Ready to execute.\n\n"
        )
        expected = (
            "**Checking initial parameters**\n\nI am checking the initial state.\n\n"
            "**Strategic Execution Plan**\n\n"
            "I have locked in the plan:\n"
            "1. **First file**: update scene one.\n"
            "2. **Second file**: update scene two.\n"
            "Ready to execute.\n\n"
        )
        self.assertEqual(strip_summarizer_leaked_thoughts(sec1 + sec2), expected)


class StreamStripperTests(unittest.TestCase):
    def test_passthrough_zero_delay_for_normal_summary(self):
        s = ThoughtSummaryStreamStripper()
        out1 = s.start_thought("**Analyzing")
        out2 = s.feed_thought(" the bug**\n\nI am checking.")
        out3 = s.finish_thought()
        self.assertEqual(out1, "**Analyzing")
        self.assertEqual(out2, " the bug**\n\nI am checking.")
        self.assertEqual(out3, "")

    def test_fragmented_thinking_process_prefix_buffered_until_stop(self):
        s = ThoughtSummaryStreamStripper()
        chunks = [
            "Think", "ing ", "Process:\n\n1. **Analyze the Request:**\n",
            "    * Summarize the provided thoughts.\n    * Max 50 words.\n\n",
            "6. **Final Polish:**\n    **Fixing stream buffer**\n\n    I am fixing the buffer.**Fixing stream buffer**\n\nI am fixing the buffer.\n\n",
        ]
        emitted = []
        s.start_thought()
        for ch in chunks:
            out = s.feed_thought(ch)
            if out:
                emitted.append(out)
        # 在 step.stop 之前不漏出任何草稿
        self.assertEqual(emitted, [])
        flushed = s.finish_thought()
        self.assertEqual(flushed, "**Fixing stream buffer**\n\nI am fixing the buffer.\n\n")

    def test_multiple_tp_inside_single_step_flushes_early(self):
        s = ThoughtSummaryStreamStripper()
        s.start_thought()
        out1 = s.feed_thought(
            "Thinking Process:\n\n1. **Analyze the Request:** Summarize the provided thoughts.\n\n"
            "**First clean header**\n\nFirst clean body.\n\n"
        )
        self.assertEqual(out1, "")
        out2 = s.feed_thought(
            "Thinking Process:\n\n1. **Analyze the Request:** Summarize the provided thoughts.\n\n"
            "**Second clean header**\n\nSecond clean body.\n\n"
        )
        self.assertEqual(out2, "**First clean header**\n\nFirst clean body.\n\n")
        out3 = s.finish_thought()
        self.assertEqual(out3, "**Second clean header**\n\nSecond clean body.\n\n")

    def test_non_summarizer_long_thinking_process_falls_back_to_passthrough(self):
        s = ThoughtSummaryStreamStripper()
        s.start_thought()
        body = "Thinking Process:\n\n" + ("Step detail without summarizer prompt. " * 60)
        out1 = s.feed_thought(body[:50])
        self.assertEqual(out1, "")
        out2 = s.feed_thought(body[50:])
        out3 = s.finish_thought()
        self.assertEqual(out1 + out2 + out3, body)


class ProtocolIntegrationStripTests(unittest.TestCase):
    def setUp(self):
        signature_cache().clear()

    def _leaked_summary(self, hdr: str, body: str) -> str:
        return (
            "Thinking Process:\n\n"
            "1.  **Analyze the Request:**\n"
            "    *   **Goal:** Summarize the provided thoughts in the first person (\"I\").\n"
            "    *   **Length:** Maximum 50 words.\n\n"
            f"6.  **Final Polish:**\n    **{hdr}**\n\n    {body}**{hdr}**\n\n{body}\n\n"
        )

    def test_oai_non_stream_strips_reasoning_and_preserves_signature_roundtrip(self):
        raw_summary = self._leaked_summary("Solving step one", "I am solving step one cleanly.")
        interaction = {
            "status": "completed",
            "steps": [
                {"type": "thought", "signature": "sig_roundtrip_1",
                 "summary": [{"type": "text", "text": raw_summary}]},
                {"type": "model_output", "content": [{"type": "text", "text": "Done."}]},
            ],
            "usage": {"total_input_tokens": 10, "total_output_tokens": 2, "total_thought_tokens": 20},
        }
        resp = convert_interactions_to_openai_response(interaction, "gemini-4-hydrogen", "req1")
        msg = resp["choices"][0]["message"]
        self.assertEqual(msg["reasoning_content"], "**Solving step one**\n\nI am solving step one cleanly.\n\n")
        # provider_metadata 仍保留原始未剥离的 steps
        self.assertIn("Thinking Process:", msg["provider_metadata"]["interactions_steps"][0]["summary"][0]["text"])

        # 客户端丢掉 provider_metadata，只回传剥离后的 reasoning_content，仍能恢复原始带签名 thought step
        next_steps, _ = convert_oai_messages_to_interactions([
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "Done.", "reasoning_content": msg["reasoning_content"]},
            {"role": "user", "content": "Q2"},
        ])
        thought_steps = [s for s in next_steps if s.get("type") == "thought"]
        self.assertEqual(len(thought_steps), 1)
        self.assertEqual(thought_steps[0]["signature"], "sig_roundtrip_1")
        self.assertEqual(thought_steps[0]["summary"][0]["text"], raw_summary)

    def test_oai_stream_strips_leaked_and_truncated_keeps_signature_roundtrip(self):
        raw_trunc = "Thinking Process:\n\n1. **Analyze the Request:** Summarize the provided thoughts."
        raw_clean = self._leaked_summary("Verifying logic", "I am verifying the core logic.")
        conv = InteractionsStreamConverter("gemini-4-carbon", "req2")
        events = [
            {"event_type": "step.start", "index": 0, "step": {"type": "thought"}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "thought_summary", "content": {"type": "text", "text": raw_trunc}}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "thought_signature", "signature": "sig_trunc"}},
            {"event_type": "step.stop", "index": 0},
            {"event_type": "step.start", "index": 1, "step": {"type": "thought"}},
            {"event_type": "step.delta", "index": 1, "delta": {"type": "thought_summary", "content": {"type": "text", "text": raw_clean[:40]}}},
            {"event_type": "step.delta", "index": 1, "delta": {"type": "thought_summary", "content": {"type": "text", "text": raw_clean[40:]}}},
            {"event_type": "step.delta", "index": 1, "delta": {"type": "thought_signature", "signature": "sig_clean"}},
            {"event_type": "step.stop", "index": 1},
            {"event_type": "step.start", "index": 2, "step": {"type": "model_output"}},
            {"event_type": "step.delta", "index": 2, "delta": {"type": "text", "text": "Answer."}},
            {"event_type": "step.stop", "index": 2},
            {"event_type": "interaction.completed", "interaction": {"status": "completed", "usage": {}}},
        ]
        chunks = [c for ev in events for c in conv.feed(ev)]
        reasonings = [c["choices"][0]["delta"]["reasoning_content"]
                      for c in chunks if c.get("choices") and c["choices"][0]["delta"].get("reasoning_content")]
        combined_reasoning = "".join(reasonings)
        self.assertEqual(combined_reasoning, "**Verifying logic**\n\nI am verifying the core logic.\n\n")
        self.assertEqual(conv.signatures, ["sig_trunc", "sig_clean"])

        # 客户端只回传剥离后的 reasoning_content，两个签名都应完整恢复
        next_steps, _ = convert_oai_messages_to_interactions([
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Answer.", "reasoning_content": combined_reasoning},
            {"role": "user", "content": "Next"},
        ])
        thoughts = [s for s in next_steps if s.get("type") == "thought"]
        self.assertEqual([t["signature"] for t in thoughts], ["sig_trunc", "sig_clean"])

    def test_gemini_gc_non_stream_and_stream_strip_and_keep_signature(self):
        raw_trunc = "Thinking Process:\n\n1. **Analyze the Request:** Summarize the provided thoughts."
        raw_clean = self._leaked_summary("Analyzing matrix", "I am analyzing the matrix.")
        gc = convert_interactions_to_gemini_gc({
            "status": "completed",
            "steps": [
                {"type": "thought", "signature": "sig_t", "summary": [{"type": "text", "text": raw_trunc}]},
                {"type": "thought", "signature": "sig_c", "summary": [{"type": "text", "text": raw_clean}]},
                {"type": "model_output", "content": [{"type": "text", "text": "OK"}]},
            ],
            "usage": {},
        })
        parts = gc["candidates"][0]["content"]["parts"]
        self.assertEqual(parts, [
            {"thought": True, "thoughtSignature": "sig_t"},
            {"text": "**Analyzing matrix**\n\nI am analyzing the matrix.\n\n", "thought": True, "thoughtSignature": "sig_c"},
            {"text": "OK"},
        ])

        conv = InteractionsToGeminiGCConverter()
        stream_chunks = []
        for ev in [
            {"event_type": "step.start", "index": 0, "step": {"type": "thought"}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "thought_summary", "content": {"type": "text", "text": raw_trunc}}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "thought_signature", "signature": "sig_t"}},
            {"event_type": "step.stop", "index": 0},
            {"event_type": "step.start", "index": 1, "step": {"type": "thought"}},
            {"event_type": "step.delta", "index": 1, "delta": {"type": "thought_summary", "content": {"type": "text", "text": raw_clean}}},
            {"event_type": "step.delta", "index": 1, "delta": {"type": "thought_signature", "signature": "sig_c"}},
            {"event_type": "step.stop", "index": 1},
            {"event_type": "interaction.completed", "interaction": {"status": "completed", "usage": {}}},
        ]:
            stream_chunks.extend(conv.feed(ev))
        stream_parts = [p for ch in stream_chunks for p in (ch.get("candidates") or [{}])[0].get("content", {}).get("parts", [])]
        self.assertEqual(stream_parts, [
            {"thought": True, "thoughtSignature": "sig_t"},
            {"text": "**Analyzing matrix**\n\nI am analyzing the matrix.\n\n", "thought": True},
            {"thought": True, "thoughtSignature": "sig_c"},
        ])

    def test_native_interactions_non_stream_and_stream_stripper(self):
        raw_clean = self._leaked_summary("Checking state", "I am checking the state.")
        resp = strip_interactions_response_thoughts({
            "id": "int_1",
            "status": "completed",
            "steps": [
                {"type": "thought", "signature": "sig_n", "summary": [{"type": "text", "text": raw_clean}]},
                {"type": "model_output", "content": [{"type": "text", "text": "Hello"}]},
            ],
        })
        self.assertEqual(resp["steps"][0]["signature"], "sig_n")
        self.assertEqual(resp["steps"][0]["summary"], [{"type": "text", "text": "**Checking state**\n\nI am checking the state.\n\n"}])

        # 流式原生 SSE bytes
        stripper = InteractionsNativeStreamStripper()
        sse_events = [
            {"event_type": "step.start", "index": 0, "step": {"type": "thought"}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "thought_summary", "content": {"type": "text", "text": raw_clean[:30]}}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "thought_summary", "content": {"type": "text", "text": raw_clean[30:]}}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "thought_signature", "signature": "sig_n"}},
            {"event_type": "step.stop", "index": 0},
        ]
        observed_all = []
        out_bytes_all = b""
        for ev in sse_events:
            raw_chunk = f"event: {ev['event_type']}\ndata: {json.dumps(ev)}\n\n".encode("utf-8")
            obs, out_b = stripper.process_chunk(raw_chunk)
            observed_all.extend(obs)
            out_bytes_all += out_b
        out_text = out_bytes_all.decode("utf-8")
        self.assertNotIn("Analyze the Request", out_text)
        self.assertIn("**Checking state**\\n\\nI am checking the state.\\n\\n", out_text)
        self.assertIn("sig_n", out_text)

        # 非泄露正常思考应零拷贝透传原 bytes
        stripper2 = InteractionsNativeStreamStripper()
        normal_chunk = b'event: step.delta\ndata: {"event_type": "step.delta", "delta": {"type": "thought_summary", "content": {"type": "text", "text": "**Normal**\\n\\nOk."}}}\n\n'
        obs2, out2 = stripper2.process_chunk(normal_chunk)
        self.assertEqual(out2, normal_chunk)


if __name__ == "__main__":
    unittest.main()
