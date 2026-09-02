"""reasoning_details 缓存与流式分片合并的单元测试"""
from routes._direct_api_reasoning_cache import (
    lookup_reasoning_details,
    merge_reasoning_detail_chunks,
    store_reasoning_details,
)


def test_merge_stream_chunks():
    """流式分片：text 逐段拼接，signature 在尾部分片到达"""
    chunks = [
        {"type": "reasoning.text", "text": "让我想", "format": "anthropic-claude-v1", "index": 0},
        {"type": "reasoning.text", "text": "想这个问题", "format": "anthropic-claude-v1", "index": 0},
        {"type": "reasoning.text", "text": "", "signature": "SIG123",
         "format": "anthropic-claude-v1", "index": 0},
    ]
    merged = merge_reasoning_detail_chunks(chunks)
    assert len(merged) == 1
    assert merged[0]["text"] == "让我想想这个问题"
    assert merged[0]["signature"] == "SIG123"
    assert merged[0]["format"] == "anthropic-claude-v1"


def test_merge_multiple_blocks():
    """多个块（如 text + encrypted）按 index/type 独立合并，保持顺序"""
    chunks = [
        {"type": "reasoning.text", "text": "A", "index": 0},
        {"type": "reasoning.encrypted", "data": "ENC1", "index": 1},
        {"type": "reasoning.text", "text": "B", "signature": "S0", "index": 0},
        {"type": "reasoning.encrypted", "data": "ENC2", "index": 1},
    ]
    merged = merge_reasoning_detail_chunks(chunks)
    assert len(merged) == 2
    assert merged[0]["text"] == "AB" and merged[0]["signature"] == "S0"
    assert merged[1]["data"] == "ENC1ENC2"


def test_merge_ignores_invalid():
    """非 dict 分片被忽略，空列表返回空"""
    assert merge_reasoning_detail_chunks([]) == []
    assert merge_reasoning_detail_chunks(["oops", None]) == []


def test_store_and_lookup_with_strip():
    """存取命中，且容忍客户端保存时的首尾空白差异"""
    details = [{"type": "reasoning.text", "text": "abc", "signature": "S"}]
    store_reasoning_details("思考内容X", details)
    assert lookup_reasoning_details("思考内容X") == details
    assert lookup_reasoning_details("  思考内容X\n") == details
    assert lookup_reasoning_details("不存在的思考") is None


def test_store_rejects_empty():
    """空文本/空 details 不入缓存"""
    store_reasoning_details("", [{"type": "reasoning.text", "text": "x"}])
    store_reasoning_details("有文本", [])
    assert lookup_reasoning_details("") is None
    assert lookup_reasoning_details("有文本") is None


if __name__ == "__main__":
    test_merge_stream_chunks()
    test_merge_multiple_blocks()
    test_merge_ignores_invalid()
    test_store_and_lookup_with_strip()
    test_store_rejects_empty()
    print("OK: reasoning cache 全部测试通过")
