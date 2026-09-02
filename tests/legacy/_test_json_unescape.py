"""utils/json_unescape.py 的单元测试：

1. 基本 \\uXXXX 解码与安全保留集
2. 任意位置跨块切割的一致性（含代理对被切断）
3. 字面反斜杠（\\\\uXXXX）不被误解码
4. 孤立代理、非法转义原样保留
5. flush 残留语义
6. normalize_tool_args_json 合法/非法 JSON 行为
7. AnthropicSSEToolArgsRewriter 事件级重写（含 chunk 任意切割、
   text_delta 不受影响、stop 残留补发、事件跨 chunk）
"""
import json

from utils.json_unescape import (
    StreamingUnicodeUnescaper,
    unescape_json_unicode,
    normalize_tool_args_json,
    normalize_response_tool_args,
    AnthropicSSEToolArgsRewriter,
)


def unescape_in_chunks(text: str, size: int) -> str:
    """把 text 按 size 切块逐段 feed，返回拼接结果。"""
    u = StreamingUnicodeUnescaper()
    out = []
    for i in range(0, len(text), size):
        out.append(u.feed(text[i:i + size]))
    out.append(u.flush())
    return "".join(out)


def test_1_basic_decode():
    raw = '{"oldContent": "\\u5468\\u56f4\\u7684\\u4eba"}'
    expected = '{"oldContent": "周围的人"}'
    assert unescape_json_unicode(raw) == expected, unescape_json_unicode(raw)
    # ASCII 可打印字符转义同样解码
    assert unescape_json_unicode('"\\u0041"') == '"A"'
    print("[PASS] test_1_basic_decode")


def test_2_preserved_escapes():
    # 控制字符、引号、反斜杠、C1、U+2028/2029 必须保留转义
    for esc in ("\\u0000", "\\u001f", "\\u0022", "\\u005c", "\\u005C",
                "\\u007f", "\\u009f", "\\u2028", "\\u2029"):
        raw = f'"{esc}"'
        assert unescape_json_unicode(raw) == raw, (esc, unescape_json_unicode(raw))
    # 常规双字符转义原样保留
    raw = '"line\\nnext\\t\\"quoted\\" \\\\ end"'
    assert unescape_json_unicode(raw) == raw
    print("[PASS] test_2_preserved_escapes")


def test_3_literal_backslash_not_decoded():
    # JSON 文本 "\\u4e00" 表示字面反斜杠 + u4e00，不能解码
    raw = '{"pattern": "[^a-z0-9\\\\u4e00-\\\\u9fff]+"}'
    assert unescape_json_unicode(raw) == raw
    # 三个反斜杠：字面反斜杠 + 真转义 → 只解码后者
    raw3 = '"\\\\\\u4e00"'
    assert unescape_json_unicode(raw3) == '"\\\\一"'
    print("[PASS] test_3_literal_backslash_not_decoded")


def test_4_surrogate_pairs():
    raw = '"\\ud83d\\ude00\\u5217"'
    assert unescape_json_unicode(raw) == '"\U0001F600列"'
    # 孤立高位代理（后面不是转义）原样保留
    assert unescape_json_unicode('"\\ud83d abc"') == '"\\ud83d abc"'
    # 孤立低位代理原样保留
    assert unescape_json_unicode('"\\ude00"') == '"\\ude00"'
    # 高位代理后跟另一个高位代理：前者保留，后者继续按规则处理
    assert unescape_json_unicode('"\\ud83d\\ud83d"') == '"\\ud83d\\ud83d"'
    print("[PASS] test_4_surrogate_pairs")


def test_5_chunk_boundary_consistency():
    samples = [
        '{"old": "\\u5217\\u5b81\\u665a\\u5e74\\u8457\\u4f5c"}',
        '"\\ud83d\\ude00\\ud83d\\ude01 mixed \\u4e2d\\u6587"',
        '{"pattern": "[^\\\\u4e00-\\\\u9fff]+", "text": "\\u6d4b\\u8bd5"}',
        '"tail broken \\u52',
        '"\\\\\\\\u4e00 quad backslash"',
    ]
    for raw in samples:
        expected = unescape_json_unicode(raw)
        for size in range(1, 8):
            got = unescape_in_chunks(raw, size)
            assert got == expected, (raw, size, got, expected)
    print("[PASS] test_5_chunk_boundary_consistency")


def test_6_flush_semantics():
    u = StreamingUnicodeUnescaper()
    assert u.feed('abc\\u5') == 'abc'
    assert u.pending
    assert u.flush() == '\\u5'
    assert not u.pending
    # flush 幂等
    assert u.flush() == ''
    print("[PASS] test_6_flush_semantics")


def test_7_normalize_tool_args_json():
    # 合法 JSON：完整规范化为明文
    raw = '{"path": "\\u8bbe\\u5b9a\\u5e93/00.md"}'
    got = normalize_tool_args_json(raw)
    assert "设定库" in got and "\\u" not in got, got
    # 非法 JSON（截断）：退化为字符级解码，中文仍可读
    broken = '{"old": "\\u5217\\u5b81 broken'
    got2 = normalize_tool_args_json(broken)
    assert "列宁" in got2, got2
    # 无转义时原样返回（同一对象）
    plain = '{"a": 1}'
    assert normalize_tool_args_json(plain) is plain
    print("[PASS] test_7_normalize_tool_args_json")


def test_8_normalize_response_tool_args():
    resp = {"choices": [{"message": {"tool_calls": [
        {"function": {"arguments": '{"cmd": "\\u67e5\\u8be2"}'}},
        {"function": {"arguments": '{"x": 1}'}},
    ]}}]}
    assert normalize_response_tool_args(resp) is True
    args0 = resp["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    assert "查询" in args0, args0
    # 第二次调用无修改
    assert normalize_response_tool_args(resp) is False
    print("[PASS] test_8_normalize_response_tool_args")


# ---------------------------------------------------------------------------
# AnthropicSSEToolArgsRewriter
# ---------------------------------------------------------------------------

def sse_event(event_name: str, obj: dict) -> bytes:
    return (f"event: {event_name}\ndata: "
            + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


def build_tool_stream(partials, index=0, with_text_block=True) -> bytes:
    """构造含一个 tool_use 块（可选前置 text 块）的 Anthropic SSE 字节流。"""
    out = [sse_event("message_start", {"type": "message_start", "message": {"id": "m1"}})]
    if with_text_block:
        out.append(sse_event("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}}))
        out.append(sse_event("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "文本中的 \\u4e2d 不该被动"}}))
        out.append(sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}))
    out.append(sse_event("content_block_start", {
        "type": "content_block_start", "index": index,
        "content_block": {"type": "tool_use", "id": "tu_1", "name": "apply_diff"}}))
    for p in partials:
        out.append(sse_event("content_block_delta", {
            "type": "content_block_delta", "index": index,
            "delta": {"type": "input_json_delta", "partial_json": p}}))
    out.append(sse_event("content_block_stop", {"type": "content_block_stop", "index": index}))
    out.append(sse_event("message_stop", {"type": "message_stop"}))
    return b"".join(out)


def collect_partials(raw: bytes, index) -> str:
    """从重写后的 SSE 字节流中拼接指定 index 的 partial_json。"""
    parts = []
    for line in raw.decode("utf-8").split("\n"):
        if not line.startswith("data: "):
            continue
        obj = json.loads(line[6:])
        if (obj.get("type") == "content_block_delta"
                and obj.get("index") == index
                and obj.get("delta", {}).get("type") == "input_json_delta"):
            parts.append(obj["delta"]["partial_json"])
    return "".join(parts)


def rewrite_in_chunks(stream: bytes, size: int) -> bytes:
    rw = AnthropicSSEToolArgsRewriter()
    out = bytearray()
    for i in range(0, len(stream), size):
        out += rw.feed(stream[i:i + size])
    out += rw.flush()
    return bytes(out)


def test_9_sse_rewriter_basic():
    args = '{"path": "a.md", "oldContent": "\\u5468\\u56f4\\u7684\\u4eba\\u7fa4"}'
    # 把参数按 5 字符切成多个 input_json_delta（模拟 Anthropic 增量）
    partials = [args[i:i + 5] for i in range(0, len(args), 5)]
    stream = build_tool_stream(partials, index=1)
    for chunk_size in (1, 7, 64, len(stream)):
        rewritten = rewrite_in_chunks(stream, chunk_size)
        combined = collect_partials(rewritten, 1)
        assert combined == '{"path": "a.md", "oldContent": "周围的人群"}', (chunk_size, combined)
        # 语义等价：解析结果与原始参数一致
        assert json.loads(combined) == json.loads(args)
        # text 块不受影响：字面 \u4e2d（wire 上为双反斜杠）原样保留
        assert b'\\\\u4e2d' in rewritten
    print("[PASS] test_9_sse_rewriter_basic")


def test_10_sse_rewriter_escape_split_across_events():
    # 转义序列被切在两个 input_json_delta 事件之间
    partials = ['{"old": "\\u52', '17\\u5b81"}']
    stream = build_tool_stream(partials, index=0, with_text_block=False)
    rewritten = rewrite_in_chunks(stream, 16)
    combined = collect_partials(rewritten, 0)
    assert combined == '{"old": "列宁"}', combined
    print("[PASS] test_10_sse_rewriter_escape_split_across_events")


def test_11_sse_rewriter_tail_refill_on_stop():
    # 流在转义中间截断后直接 stop：残留应作为补发 delta 保证字节完整
    partials = ['{"old": "\\u52']
    stream = build_tool_stream(partials, index=0, with_text_block=False)
    rewritten = rewrite_in_chunks(stream, len(stream))
    combined = collect_partials(rewritten, 0)
    assert combined == '{"old": "\\u52', combined
    # stop 事件仍存在且在补发 delta 之后
    text = rewritten.decode("utf-8")
    assert text.rindex('"content_block_stop"') > text.rindex('input_json_delta')
    print("[PASS] test_11_sse_rewriter_tail_refill_on_stop")


def test_12_sse_rewriter_passthrough_unrelated():
    # 与 content block 无关的事件字节原样保留（包括 ping 注释和错误事件）
    raw = (b": ping\n\n"
           + sse_event("message_delta", {"type": "message_delta", "usage": {"output_tokens": 5}})
           + b"data: [DONE]\n\n")
    rw = AnthropicSSEToolArgsRewriter()
    got = rw.feed(raw) + rw.flush()
    assert got == raw
    print("[PASS] test_12_sse_rewriter_passthrough_unrelated")


if __name__ == "__main__":
    test_1_basic_decode()
    test_2_preserved_escapes()
    test_3_literal_backslash_not_decoded()
    test_4_surrogate_pairs()
    test_5_chunk_boundary_consistency()
    test_6_flush_semantics()
    test_7_normalize_tool_args_json()
    test_8_normalize_response_tool_args()
    test_9_sse_rewriter_basic()
    test_10_sse_rewriter_escape_split_across_events()
    test_11_sse_rewriter_tail_refill_on_stop()
    test_12_sse_rewriter_passthrough_unrelated()
    print("\n全部测试通过 ✓")
