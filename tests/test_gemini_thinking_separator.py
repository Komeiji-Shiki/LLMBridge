"""Gemini 原生链路 thinking_separator 流式切分测试（纯函数，无网络依赖）。"""
import unittest

from routes._direct_api_gemini import ThinkingSeparatorStreamSplitter


def feed(splitter, chunks):
    """按 chunk 顺序喂给 splitter，返回 (reasoning, content, skipped_count)。"""
    reasoning, content, skipped = [], [], 0
    for text in chunks:
        delta = {"content": text}
        if splitter.process(delta):
            skipped += 1
            continue
        reasoning.append(delta.get("reasoning_content") or "")
        content.append(delta.get("content") or "")
    return "".join(reasoning), "".join(content), skipped


class ThinkingSeparatorStreamSplitterTests(unittest.TestCase):
    def test_split_within_single_chunk(self):
        splitter = ThinkingSeparatorStreamSplitter("</analysis_channel>")
        reasoning, content, skipped = feed(
            splitter, ["思考内容</analysis_channel>正文"])

        self.assertEqual(reasoning, "思考内容")
        self.assertEqual(content, "正文")
        self.assertEqual(skipped, 0)

    def test_separator_across_chunks_keeps_tail_buffered(self):
        splitter = ThinkingSeparatorStreamSplitter("</analysis_channel>")
        reasoning, content, skipped = feed(
            splitter,
            ["思考", "内容</analy", "sis_channel>", "正文", "继续"])

        self.assertEqual(reasoning, "思考内容")
        self.assertEqual(content, "正文继续")
        self.assertEqual(skipped, 2)

    def test_separator_at_start_yields_empty_reasoning(self):
        splitter = ThinkingSeparatorStreamSplitter("</analysis_channel>")
        reasoning, content, skipped = feed(
            splitter, ["</analysis_channel>", "正文"])

        self.assertEqual(reasoning, "")
        self.assertEqual(content, "正文")
        self.assertEqual(skipped, 1)

    def test_tail_held_back_when_chunk_is_all_separator_prefix(self):
        splitter = ThinkingSeparatorStreamSplitter("</analysis_channel>")
        reasoning, content, skipped = feed(splitter, ["</ana"])

        self.assertEqual(reasoning, "")
        self.assertEqual(content, "")
        self.assertEqual(skipped, 1)

    def test_multi_bypass_after_separator(self):
        splitter = ThinkingSeparatorStreamSplitter("</analysis_channel>")
        reasoning, content, _ = feed(
            splitter,
            ["A</analysis_channel>B", "C", "</analysis_channel>D"])

        self.assertEqual(reasoning, "A")
        self.assertEqual(content, "BC</analysis_channel>D")

    def test_native_reasoning_disables_splitter(self):
        splitter = ThinkingSeparatorStreamSplitter("</analysis_channel>")
        delta = {"reasoning_content": "原生思考", "content": "带分隔符正文"}
        self.assertFalse(splitter.process(delta))
        self.assertEqual(delta["content"], "带分隔符正文")
        self.assertFalse(splitter.enabled)

    def test_no_separator_keeps_content_after_split_starts(self):
        splitter = ThinkingSeparatorStreamSplitter(None)
        delta = {"content": "普通正文"}
        self.assertFalse(splitter.process(delta))
        self.assertEqual(delta["content"], "普通正文")

    def test_flush_returns_held_tail_when_separator_never_appears(self):
        splitter = ThinkingSeparatorStreamSplitter("</analysis_channel>")
        delta = {"content": "普通正文"}
        splitter.process(delta)
        # 尾部属于分隔符前缀，被扣住
        self.assertNotIn("content", delta)
        self.assertEqual(splitter.flush(), "普通正文")
        # flush 后不再重复输出
        self.assertEqual(splitter.flush(), "")

    def test_flush_is_noop_after_separator_found(self):
        splitter = ThinkingSeparatorStreamSplitter("</analysis_channel>")
        feed(splitter, ["思考</analysis_channel>正文"])
        self.assertEqual(splitter.flush(), "")


if __name__ == "__main__":
    unittest.main()
