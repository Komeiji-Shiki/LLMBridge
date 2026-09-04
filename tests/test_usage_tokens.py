"""输出 token 口径（思考 token 是否并入 completion_tokens）回归测试。

真实线上样本的特征是上游把思考单列：
    total_tokens == prompt_tokens + completion_tokens + reasoning_tokens
此时下游只读 completion_tokens 会拿到明显偏小的输出量。测试同时覆盖
OpenAI 官方形态（completion_tokens 已含思考，相加会造成重复计费），
以及 merge / separate 两种模式在流式与非流式链路上的落地结果。
"""
import copy
import unittest

from converters.anthropic_openai import convert_openai_usage_to_anthropic
from converters.responses_openai import _usage_to_responses
from routes._direct_api_passthrough import _extract_tokens_from_response
from routes._direct_api_stream_session import PassthroughStreamSession
from utils.usage_tokens import (
    MODE_MERGE,
    MODE_SEPARATE,
    apply_usage_tokens,
    completion_excludes_reasoning,
    compose_chat_usage,
    extract_reasoning_tokens,
    get_completion_tokens_mode,
    resolve_usage_tokens,
    total_output_tokens,
)

# 上游把思考单列（completion 只算正文）
SPLIT_SAMPLE = {
    "prompt_tokens": 4680,
    "completion_tokens": 1163,
    "total_tokens": 9385,
    "completion_tokens_details": {"reasoning_tokens": 3542},
}
# OpenAI 官方形态（completion 已含思考）
MERGED_SAMPLE = {
    "prompt_tokens": 1000,
    "completion_tokens": 500,
    "total_tokens": 1500,
    "completion_tokens_details": {"reasoning_tokens": 200},
}


class ReasoningExtractionTests(unittest.TestCase):
    def test_reads_nested_completion_details(self):
        """核心缺陷：思考量在 completion_tokens_details 里，旧版只读顶层取不到。"""
        self.assertEqual(extract_reasoning_tokens(SPLIT_SAMPLE), 3542)

    def test_reads_top_level_and_gemini_aliases(self):
        self.assertEqual(extract_reasoning_tokens({"reasoning_tokens": 12}), 12)
        self.assertEqual(extract_reasoning_tokens({"thoughtsTokenCount": 30}), 30)
        self.assertEqual(
            extract_reasoning_tokens({"output_tokens_details": {"reasoning_tokens": 7}}), 7)

    def test_survives_dirty_values(self):
        for dirty in (None, {}, "x", {"completion_tokens_details": "bad"},
                      {"completion_tokens_details": {"reasoning_tokens": -5}},
                      {"reasoning_tokens": True}, {"reasoning_tokens": "40"}):
            self.assertGreaterEqual(extract_reasoning_tokens(dirty), 0)
        self.assertEqual(extract_reasoning_tokens({"reasoning_tokens": "40"}), 40)
        self.assertEqual(extract_reasoning_tokens({"reasoning_tokens": -5}), 0)
        self.assertEqual(extract_reasoning_tokens(None), 0)


class ModeResolutionTests(unittest.TestCase):
    def test_default_is_merge(self):
        self.assertEqual(get_completion_tokens_mode({}), MODE_MERGE)
        self.assertEqual(get_completion_tokens_mode(None), MODE_MERGE)

    def test_explicit_separate(self):
        self.assertEqual(get_completion_tokens_mode({"completion_tokens_mode": "separate"}),
                         MODE_SEPARATE)

    def test_illegal_value_falls_back(self):
        self.assertEqual(get_completion_tokens_mode({"completion_tokens_mode": "banana"}),
                         MODE_MERGE)


class ExclusionDetectionTests(unittest.TestCase):
    def test_split_by_total_arithmetic(self):
        self.assertTrue(completion_excludes_reasoning(4680, 1163, 3542, 9385))

    def test_merged_by_total_arithmetic(self):
        self.assertFalse(completion_excludes_reasoning(1000, 500, 200, 1500))

    def test_no_total_falls_back_to_length_heuristic(self):
        self.assertTrue(completion_excludes_reasoning(0, 1163, 3542, 0))
        self.assertFalse(completion_excludes_reasoning(0, 5000, 300, 0))

    def test_zero_reasoning_is_noop(self):
        self.assertFalse(completion_excludes_reasoning(100, 50, 0, 150))


class MergeModeTests(unittest.TestCase):
    def test_split_upstream_becomes_total_output(self):
        usage = copy.deepcopy(SPLIT_SAMPLE)
        tokens = apply_usage_tokens(usage, MODE_MERGE)
        self.assertEqual(tokens.output_tokens, 4705)
        self.assertEqual(usage["completion_tokens"], 4705)
        self.assertEqual(usage["total_tokens"], 9385)
        self.assertEqual(usage["completion_tokens_details"]["reasoning_tokens"], 3542)

    def test_merged_upstream_is_not_double_counted(self):
        """标准 OpenAI 响应已含思考，相加会重复计费，必须保持原值。"""
        usage = copy.deepcopy(MERGED_SAMPLE)
        tokens = apply_usage_tokens(usage, MODE_MERGE)
        self.assertEqual(usage["completion_tokens"], 500)
        self.assertEqual(usage["total_tokens"], 1500)
        self.assertFalse(tokens.changed)

    def test_operation_is_idempotent(self):
        usage = copy.deepcopy(SPLIT_SAMPLE)
        apply_usage_tokens(usage, MODE_MERGE)
        once = copy.deepcopy(usage)
        apply_usage_tokens(usage, MODE_MERGE)
        self.assertEqual(usage, once)

    def test_model_without_reasoning_is_untouched(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
        before = copy.deepcopy(usage)
        tokens = apply_usage_tokens(usage, MODE_MERGE)
        self.assertEqual(usage, before)
        self.assertFalse(tokens.changed)

    def test_upstream_total_missing_is_recomposed(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 30,
                 "completion_tokens_details": {"reasoning_tokens": 70}}
        tokens = apply_usage_tokens(usage, MODE_MERGE)
        self.assertEqual(usage["completion_tokens"], 100)
        self.assertEqual(usage["total_tokens"], 200)


class SeparateModeTests(unittest.TestCase):
    def test_split_upstream_keeps_body_tokens(self):
        usage = copy.deepcopy(SPLIT_SAMPLE)
        apply_usage_tokens(usage, MODE_SEPARATE)
        self.assertEqual(usage["completion_tokens"], 1163)
        self.assertEqual(usage["total_tokens"], 9385)
        self.assertEqual(usage["completion_tokens_details"]["reasoning_tokens"], 3542)

    def test_merged_upstream_is_split_out(self):
        usage = copy.deepcopy(MERGED_SAMPLE)
        apply_usage_tokens(usage, MODE_SEPARATE)
        self.assertEqual(usage["completion_tokens"], 300)
        self.assertEqual(usage["total_tokens"], 1500)
        self.assertEqual(usage["completion_tokens_details"]["reasoning_tokens"], 200)

    def test_responses_naming_keeps_matching_details_key(self):
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                 "output_tokens_details": {"reasoning_tokens": 5}}
        apply_usage_tokens(usage, MODE_SEPARATE)
        self.assertEqual(usage["output_tokens"], 0)
        self.assertEqual(usage["output_tokens_details"]["reasoning_tokens"], 5)
        self.assertNotIn("completion_tokens_details", usage)


class BillingInvariantTests(unittest.TestCase):
    def test_total_output_is_mode_independent(self):
        """计费与监控始终按真实总输出，切换配置不得改变成本。"""
        merged = copy.deepcopy(SPLIT_SAMPLE)
        apply_usage_tokens(merged, MODE_MERGE)
        separated = copy.deepcopy(SPLIT_SAMPLE)
        apply_usage_tokens(separated, MODE_SEPARATE)
        self.assertEqual(total_output_tokens(merged), 4705)
        self.assertEqual(total_output_tokens(separated), 4705)


class ComposeUsageTests(unittest.TestCase):
    def test_merge_and_separate(self):
        merged = compose_chat_usage(4680, 4705, 3542, 120, MODE_MERGE)
        separated = compose_chat_usage(4680, 4705, 3542, 120, MODE_SEPARATE)
        self.assertEqual(merged["completion_tokens"], 4705)
        self.assertEqual(separated["completion_tokens"], 1163)
        for usage in (merged, separated):
            self.assertEqual(usage["total_tokens"], 9385)
            self.assertEqual(usage["completion_tokens_details"]["reasoning_tokens"], 3542)
            self.assertEqual(usage["prompt_tokens_details"]["cached_tokens"], 120)

    def test_plain_model_emits_only_three_fields(self):
        """无思考无缓存时不额外塞 details，避免破坏严格校验的客户端。"""
        self.assertEqual(
            compose_chat_usage(2, 3, 0, 0, MODE_MERGE),
            {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5})


class NonStreamPassthroughTests(unittest.TestCase):
    def test_stats_include_reasoning_in_output(self):
        response = {"usage": copy.deepcopy(SPLIT_SAMPLE)}
        input_tokens, output_tokens, reasoning, total, _cached = \
            _extract_tokens_from_response(response, {})
        self.assertEqual(input_tokens, 4680)
        self.assertEqual(output_tokens, 4705)
        self.assertEqual(reasoning, 3542)
        self.assertEqual(total, 9385)

    def test_stats_match_upstream_total(self):
        """解析出的分项之和必须与上游 total 对齐，否则成本会算漏。"""
        response = {"usage": copy.deepcopy(SPLIT_SAMPLE)}
        input_tokens, output_tokens, _reasoning, total, _cached = \
            _extract_tokens_from_response(response, {})
        self.assertEqual(input_tokens + output_tokens, total)


class StreamSessionUsageTests(unittest.TestCase):
    @staticmethod
    def _session(endpoint_config):
        return PassthroughStreamSession(
            request_id="test-req", display_name="test-model", openai_req={"messages": []},
            endpoint_config=endpoint_config, pricing_config={},
            thinking_separator=None, monitoring_service=None, direct_api_service=None,
            estimate_message_tokens_func=None, estimate_tokens_func=None, full_messages=[],
        )

    def test_merge_mode_rewrites_chunk_and_stats(self):
        session = self._session({})
        chunk = {"usage": copy.deepcopy(SPLIT_SAMPLE)}
        self.assertTrue(session._process_usage(chunk))
        self.assertEqual(session.output_tokens, 4705)
        self.assertEqual(session.reasoning_tokens, 3542)
        self.assertEqual(session.total_tokens, 9385)
        self.assertEqual(chunk["usage"]["completion_tokens"], 4705)

    def test_separate_mode_keeps_chunk_body_tokens(self):
        session = self._session({"completion_tokens_mode": "separate"})
        chunk = {"usage": copy.deepcopy(SPLIT_SAMPLE)}
        session._process_usage(chunk)
        # 统计侧仍是真实总输出，只有下发给客户端的数字分开
        self.assertEqual(session.output_tokens, 4705)
        self.assertEqual(chunk["usage"]["completion_tokens"], 1163)
        self.assertEqual(chunk["usage"]["completion_tokens_details"]["reasoning_tokens"], 3542)

    def test_native_usage_record_stays_unmodified(self):
        """监控记录的必须是上游原值，不能被本地归一化连带改写。"""
        session = self._session({})
        original = copy.deepcopy(SPLIT_SAMPLE)
        session._process_usage({"usage": original})
        self.assertEqual(session.upstream_usage, SPLIT_SAMPLE)

    def test_cached_forward_still_wins_over_raw_prompt(self):
        session = self._session({"cached_tokens_mode": "forward"})
        chunk = {"usage": {"prompt_tokens": 100, "completion_tokens": 50,
                           "prompt_tokens_details": {"cached_tokens": 30},
                           "cost": 0.0123}}
        session._process_usage(chunk)
        self.assertEqual(session.upstream_usage["prompt_tokens"], 100)
        self.assertEqual(session.upstream_usage["cost"], 0.0123)
        self.assertEqual(chunk["usage"]["prompt_tokens"], 130)
        self.assertEqual(session.input_tokens, 130)

    def test_batched_usage_does_not_wipe_reasoning(self):
        """上游分批下发 usage：后到的块只补 total，不得清掉已捕获的思考量。"""
        session = self._session({})
        session._process_usage({"usage": copy.deepcopy(SPLIT_SAMPLE)})
        session._process_usage({"usage": {"prompt_tokens": 4680,
                                          "completion_tokens": 4705,
                                          "total_tokens": 9385}})
        self.assertEqual(session.reasoning_tokens, 3542)
        self.assertEqual(session.output_tokens, 4705)
        self.assertEqual(session.total_tokens, 9385)

    def test_model_without_reasoning_is_not_rewritten(self):
        session = self._session({})
        chunk = {"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}
        self.assertFalse(session._process_usage(chunk))
        self.assertEqual(chunk["usage"], {"prompt_tokens": 10, "completion_tokens": 20,
                                         "total_tokens": 30})


class CrossProtocolTests(unittest.TestCase):
    def test_anthropic_output_tokens_includes_reasoning(self):
        usage = compose_chat_usage(4680, 4705, 3542, 0, MODE_SEPARATE)
        anthropic = convert_openai_usage_to_anthropic(usage)
        self.assertEqual(anthropic["output_tokens"], 4705)

    def test_responses_output_tokens_includes_reasoning(self):
        usage = compose_chat_usage(4680, 4705, 3542, 0, MODE_SEPARATE)
        responses_usage = _usage_to_responses(usage)
        self.assertEqual(responses_usage["output_tokens"], 4705)
        self.assertEqual(responses_usage["total_tokens"], 9385)
        self.assertEqual(
            responses_usage["output_tokens_details"]["reasoning_tokens"], 3542)

    def test_resolve_without_mutation(self):
        usage = copy.deepcopy(SPLIT_SAMPLE)
        tokens = resolve_usage_tokens(usage, MODE_MERGE)
        self.assertEqual(tokens.output_tokens, 4705)
        self.assertEqual(usage, SPLIT_SAMPLE)


if __name__ == "__main__":
    unittest.main()
