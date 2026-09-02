"""SSE 粘连事件重构行为验证

覆盖 process_sse_chunk 重构后的关键行为：
1. 正常行原样透传（字节级不变）
2. 粘连两个 JSON 事件全部保留、独立转发
3. 字符串值内部的花括号不再被误切
4. 合法前缀 + 垃圾尾部：前缀保留，垃圾丢弃且留痕
5. 完全无法解析的行原样透传
6. [DONE] 过滤等既有行为不回归
7. 粘连行中的 usage 提取（含 upstream_usage 原生记录）
"""
import json

from routes._direct_api_stream_session import PassthroughStreamSession

DATA = "da" "ta: "
SEP = chr(10) * 2


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


def delta_chunk(content):
    return {"choices": [{"delta": {"content": content}, "finish_reason": None}]}


def to_bytes(text):
    return text.encode("utf-8")


def test_passthrough_unmodified():
    s = make_session()
    raw = to_bytes(DATA + json.dumps(delta_chunk("hello")) + SEP)
    out = s.process_sse_chunk(raw)
    assert out == raw, (out, raw)
    assert "".join(s.content_parts) == "hello"
    print("1. 正常行原样透传: OK")


def test_glued_events_both_kept():
    s = make_session()
    glued = json.dumps(delta_chunk("AAA")) + json.dumps(delta_chunk("BBB"))
    out = s.process_sse_chunk(to_bytes(DATA + glued + SEP)).decode()
    assert "AAA" in out and "BBB" in out, out
    assert "".join(s.content_parts) == "AAABBB"
    assert out.count(DATA) == 2, out
    # 两个事件之间必须有空行分隔（SSE 事件边界）
    assert SEP in out.split("BBB")[0], out
    print("2. 粘连事件两个都保留且独立分隔: OK")


def test_brace_inside_string_value():
    s = make_session()
    tricky = "x}{y"
    raw = to_bytes(DATA + json.dumps(delta_chunk(tricky)) + SEP)
    out = s.process_sse_chunk(raw)
    assert out == raw, out
    assert "".join(s.content_parts) == tricky
    print("3. 字符串值内花括号不误切: OK")


def test_valid_prefix_with_garbage_tail():
    s = make_session()
    raw_line = json.dumps(delta_chunk("CCC")) + "@@@garbage@@@"
    out = s.process_sse_chunk(to_bytes(DATA + raw_line + SEP)).decode()
    assert "CCC" in out, out
    assert "garbage" not in out, out
    assert "".join(s.content_parts) == "CCC"
    print("4. 合法前缀保留、垃圾尾部丢弃: OK")


def test_fully_garbage_line():
    s = make_session()
    raw = to_bytes(DATA + "totally not json" + SEP)
    out = s.process_sse_chunk(raw)
    assert b"totally not json" in out, out
    print("5. 完全垃圾行原样透传: OK")


def test_done_filtered():
    s = make_session()
    raw = to_bytes(DATA + "[DONE]" + SEP)
    out = s.process_sse_chunk(raw)
    assert b"[DONE]" not in out, out
    assert s.upstream_done_received
    print("6. [DONE] 过滤不回归: OK")


def test_usage_in_glued_line():
    s = make_session()
    usage_chunk = {"choices": [],
                   "usage": {"prompt_tokens": 11, "completion_tokens": 7}}
    glued = json.dumps(delta_chunk("Z")) + json.dumps(usage_chunk)
    out = s.process_sse_chunk(to_bytes(DATA + glued + SEP)).decode()
    assert s.upstream_usage == {"prompt_tokens": 11, "completion_tokens": 7}, \
        s.upstream_usage
    assert s.input_tokens == 11 and s.output_tokens == 7
    assert "Z" in out
    print("7. 粘连行中的 usage 正确提取: OK")


if __name__ == "__main__":
    test_passthrough_unmodified()
    test_glued_events_both_kept()
    test_brace_inside_string_value()
    test_valid_prefix_with_garbage_tail()
    test_fully_garbage_line()
    test_done_filtered()
    test_usage_in_glued_line()
    print("ALL SSE GLUE TESTS PASSED")
