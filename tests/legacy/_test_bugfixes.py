"""本轮 bug 修复的专项验证

1. extract_complete_sse_lines：pending 消费必须触发重组信号
2. process_sse_chunk：跨 chunk 断行不丢字节、事件终止空行完整
3. parse_buffer：正文/思维链交错时内容不丢失
4. _extract_complete_sse_events：search_start 偏移不影响正确性
5. StreamingUnicodeUnescaper 回归（行缓冲改动不影响转义解码）
"""
import json

from routes._direct_api_utils import extract_complete_sse_lines
from routes._direct_api_stream_session import PassthroughStreamSession
from services.stream_parsers import StreamPatternMatcher
from services.direct_api_service import _extract_complete_sse_events

DATA = "da" "ta: "
NL = chr(10)
SEP = NL * 2


# ============ 1. extract_complete_sse_lines ============

def test_sse_lines_pending_consumption():
    # 场景 A：尾部半行 → 缓冲 + 重组信号
    lines, pending, reassembly = extract_complete_sse_lines("data: {\"co", "")
    assert lines == [] and pending == "data: {\"co" and reassembly

    # 场景 B：消费 pending 拼出完整行 → 即使本次输入以换行结尾也必须要求重组
    lines, pending, reassembly = extract_complete_sse_lines(
        "ntent\":1}" + SEP, "data: {\"co")
    assert lines == ["data: {\"content\":1}", ""], lines
    assert pending == "" and reassembly, (pending, reassembly)

    # 场景 C：无缓冲参与的完整块 → 不强制重组（保持原样透传的快速路径）
    lines, pending, reassembly = extract_complete_sse_lines("data: x" + SEP, "")
    assert lines == ["data: x", ""] and pending == "" and not reassembly

    # 场景 D：逻辑行 + '\n' 重组必须与原文字节一致
    original = "a" + NL + NL + "b" + NL
    lines, pending, _ = extract_complete_sse_lines(original, "")
    assert "".join(line + NL for line in lines) == original and pending == ""
    print("1. extract_complete_sse_lines pending 语义: OK")


# ============ 2. process_sse_chunk 跨 chunk 断行 ============

def make_session(**overrides):
    kwargs = dict(
        request_id="t", display_name="m", openai_req={"messages": []},
        endpoint_config={}, pricing_config={}, thinking_separator=None,
        monitoring_service=None, direct_api_service=None,
        estimate_message_tokens_func=None, estimate_tokens_func=None,
        full_messages=[],
    )
    kwargs.update(overrides)
    return PassthroughStreamSession(**kwargs)


def test_split_chunk_no_byte_loss():
    s = make_session()
    event = {"choices": [{"delta": {"content": "hello world"}, "finish_reason": None}]}
    full_line = DATA + json.dumps(event) + SEP
    cut = len(full_line) // 2

    out1 = s.process_sse_chunk(full_line[:cut].encode("utf-8"))
    out2 = s.process_sse_chunk(full_line[cut:].encode("utf-8"))
    combined = (out1 + out2).decode("utf-8")

    # 旧版 bug：out2 直接返回原始后半段字节，前半行永久丢失
    assert combined == full_line, (combined, full_line)
    assert "".join(s.content_parts) == "hello world"
    print("2a. 跨 chunk 断行字节完整: OK")


def test_split_chunk_event_boundary():
    s = make_session()
    e1 = DATA + json.dumps({"choices": [{"delta": {"content": "A"}}]}) + SEP
    e2 = DATA + json.dumps({"choices": [{"delta": {"content": "B"}}]}) + SEP
    stream = e1 + e2
    # 切在第一个事件的终止空行中间（e1 末尾倒数第 1 个字节处）
    cut = len(e1) - 1
    out = s.process_sse_chunk(stream[:cut].encode("utf-8"))
    out += s.process_sse_chunk(stream[cut:].encode("utf-8"))
    text = out.decode("utf-8")
    # 事件之间必须仍由空行分隔（旧版 join 语义会吞掉一个换行）
    assert SEP in text.split("B")[0], text
    assert "".join(s.content_parts) == "AB"
    print("2b. 事件终止空行不丢失: OK")


# ============ 3. parse_buffer 交错标记 ============

def test_interleaved_markers():
    m = StreamPatternMatcher()
    # 正文出现在思维链之前：旧版切片会把 a0 丢掉
    buf = 'a0:"正文1"' + NL + 'ag:"思考1"' + NL + 'a0:"正文2"' + NL
    parsed, rest = m.parse_buffer(buf)
    assert parsed.content_chunks == ["正文1", "正文2"], parsed.content_chunks
    assert parsed.reasoning_chunks == ["思考1"], parsed.reasoning_chunks

    # 交错 + finish + 图片 + 截断尾部
    buf2 = ('ag:"t1"' + NL + 'a0:"c1"' + NL
            + 'a2:[{"type":"image","image":"https://x/1.png"}]' + NL
            + 'ad:{"finishReason":"stop","usage":{"promptTokens":3}}' + NL
            + 'a0:"未闭合')
    parsed2, rest2 = m.parse_buffer(buf2)
    assert parsed2.reasoning_chunks == ["t1"]
    assert parsed2.content_chunks == ["c1"]
    assert parsed2.image_urls == ["https://x/1.png"]
    assert parsed2.finish_reason == "stop"
    assert parsed2.usage_info == {"promptTokens": 3}
    assert rest2.endswith('a0:"未闭合'), rest2  # 截断内容保留待拼全

    # 截断的 JSON 载荷保留
    parsed3, rest3 = m.parse_buffer('ad:{"finishReason":"stop","usage":{"pro')
    assert parsed3.finish_reason is None
    assert rest3 == 'ad:{"finishReason":"stop","usage":{"pro'
    print("3. parse_buffer 交错/截断: OK")


# ============ 4. _extract_complete_sse_events 偏移扫描 ============

def test_sse_event_scan_offset():
    e1 = b"data: {\"a\":1}\n\n"
    e2 = b"event: x\ndata: {\"b\":2}\n\n"
    partial = b"data: {\"unfin"

    # 模拟流式追加 + 偏移推进（与 call_api_passthrough 的用法一致）
    buffer = b""
    scan_start = 0
    collected = []
    for chunk in [e1[:5], e1[5:] + e2[:3], e2[3:], partial]:
        buffer += chunk
        events, buffer = _extract_complete_sse_events(buffer, scan_start)
        scan_start = max(0, len(buffer) - 3)
        collected.extend(events)
    assert collected == [e1, e2], collected
    assert buffer == partial, buffer

    # CRLF 分隔符跨 chunk 切断也不能漏
    crlf_event = b"data: {\"c\":3}\r\n\r\n"
    buffer, scan_start, collected = b"", 0, []
    for chunk in [crlf_event[:-2], crlf_event[-2:]]:
        buffer += chunk
        events, buffer = _extract_complete_sse_events(buffer, scan_start)
        scan_start = max(0, len(buffer) - 3)
        collected.extend(events)
    assert collected == [b"data: {\"c\":3}\n\n"], collected
    print("4. SSE 事件偏移扫描: OK")


# ============ 5. 工具参数转义解码回归 ============

def test_tool_args_unescape_regression():
    s = make_session()
    chunk = {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": "{\"q\":\"\\u4f60\\u597d\"}"}}]}}]}
    out = s.process_sse_chunk((DATA + json.dumps(chunk) + SEP).encode()).decode()
    assert "你好" in out, out
    print("5. 工具参数转义解码回归: OK")


if __name__ == "__main__":
    test_sse_lines_pending_consumption()
    test_split_chunk_no_byte_loss()
    test_split_chunk_event_boundary()
    test_interleaved_markers()
    test_sse_event_scan_offset()
    test_tool_args_unescape_regression()
    print("ALL BUGFIX TESTS PASSED")
