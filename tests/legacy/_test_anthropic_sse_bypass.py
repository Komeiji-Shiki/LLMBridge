"""测试 anthropic_passthrough 旁路解析器的修复：

1. 跨 chunk 被切断的 SSE data 行不再丢失（监控日志 arguments 缺中段的根因）
2. UTF-8 多字节字符切在 chunk 边界不产生 U+FFFD
3. 模型输出的 \\uXXXX 转义 JSON 规范化为中文明文
4. 非法 JSON 原样保留（不被规范化吞掉）
5. flush：未收到 content_block_stop 时参数落位
6. 回归：text/thinking/usage 累积不受影响
"""
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

from converters.anthropic_openai import (
    extract_anthropic_sse_content,
    flush_anthropic_sse_state,
    _normalize_tool_args_str,
)


def new_state():
    return {
        "content_parts": [],
        "reasoning_parts": [],
        "tool_calls": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "upstream_usage": {},
    }


def sse(obj, event=None) -> bytes:
    prefix = f"event: {event}\n" if event else ""
    return (prefix + "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


def tool_block_events(args_str: str, name="apply_diff", split_partial=None):
    """构造一个完整 tool_use block 的 SSE 字节流。
    split_partial: 把 partial_json 切成多个 input_json_delta 事件。
    """
    out = [sse({"type": "content_block_start", "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": name, "input": {}}})]
    parts = split_partial if split_partial else [args_str]
    for p in parts:
        out.append(sse({"type": "content_block_delta", "index": 0,
                        "delta": {"type": "input_json_delta", "partial_json": p}}))
    out.append(sse({"type": "content_block_stop", "index": 0}))
    return b"".join(out)


def test_1_cross_chunk_line_split():
    """data 行被 TCP chunk 边界切断：旧版丢弃整行，新版应完整累积"""
    args = json.dumps({"path": "test.md", "hunks": [{"oldContent": "列宁晚年著作分析" * 50}]},
                      ensure_ascii=False)
    stream = tool_block_events(args)
    state = new_state()
    # 模拟恶劣的 TCP 分块：每 37 字节一块（必然切断行和多字节字符）
    for i in range(0, len(stream), 37):
        extract_anthropic_sse_content(stream[i:i + 37], state)
    flush_anthropic_sse_state(state)

    assert len(state["tool_calls"]) == 1, state["tool_calls"]
    got = state["tool_calls"][0]["function"]["arguments"]
    assert json.loads(got) == json.loads(args), f"arguments 内容不一致:\n{got[:200]}"
    assert "\ufffd" not in got, "存在 UTF-8 截断替换符"
    print("1. 跨 chunk 行切断: OK")


def test_2_multibyte_boundary():
    """中文字符多字节序列正好切在 chunk 边界"""
    state = new_state()
    payload = sse({"type": "content_block_delta", "index": 0,
                   "delta": {"type": "text_delta", "text": "你好世界测试内容"}})
    # 找一个中文字符的字节中间位置切断
    cut = payload.find("你好".encode("utf-8")) + 1  # 切在"你"的第2个字节前
    extract_anthropic_sse_content(payload[:cut], state)
    extract_anthropic_sse_content(payload[cut:], state)
    flush_anthropic_sse_state(state)
    content = "".join(state["content_parts"])
    assert content == "你好世界测试内容", repr(content)
    assert "\ufffd" not in content
    print("2. UTF-8 多字节边界: OK")


def test_3_unicode_escape_normalization():
    """模型用 ASCII 转义输出的 arguments 规范化为中文明文"""
    # 模型输出风格：{"path": "设定库/00.md", "old": "\u5217\u5b81\u665a\u5e74"}
    raw_args = '{"path": "设定库/00.md", "old": "\\u5217\\u5b81\\u665a\\u5e74"}'
    stream = tool_block_events(raw_args)
    state = new_state()
    extract_anthropic_sse_content(stream, state)
    flush_anthropic_sse_state(state)
    got = state["tool_calls"][0]["function"]["arguments"]
    assert "\\u5217" not in got, f"未规范化: {got}"
    assert "列宁晚年" in got, f"中文缺失: {got}"
    assert json.loads(got)["old"] == "列宁晚年"
    print("3. \\uXXXX 转义规范化: OK")


def test_4_invalid_json_preserved():
    """非法 JSON（真损坏/正则内容）原样保留"""
    bad = '{"old": "\\u52 broken'
    assert _normalize_tool_args_str(bad) == bad
    # 合法 JSON 但 \u 是代码内容一部分（正则），解析后应保持语义
    regex_args = '{"pattern": "[^a-z0-9\\\\u4e00-\\\\u9fff]+"}'
    normalized = _normalize_tool_args_str(regex_args)
    assert json.loads(normalized) == json.loads(regex_args)
    print("4. 非法 JSON 保留 / 正则内容语义不变: OK")


def test_5_flush_without_stop():
    """流中断（没收到 content_block_stop）时 flush 应把参数落位"""
    state = new_state()
    extract_anthropic_sse_content(sse({"type": "content_block_start", "index": 0,
                                       "content_block": {"type": "tool_use", "id": "t1",
                                                         "name": "write_file", "input": {}}}), state)
    extract_anthropic_sse_content(sse({"type": "content_block_delta", "index": 0,
                                       "delta": {"type": "input_json_delta",
                                                 "partial_json": '{"path": "a.md"}'}}), state)
    # 不发 stop，直接 flush（模拟断流）
    flush_anthropic_sse_state(state)
    assert state["tool_calls"][0]["function"]["arguments"] == '{"path": "a.md"}'
    print("5. flush 断流兜底: OK")


def test_6_regression_text_thinking_usage():
    """回归：text/thinking/usage 正常累积"""
    state = new_state()
    chunks = [
        sse({"type": "message_start",
             "message": {"usage": {"input_tokens": 100, "cache_read_input_tokens": 30}}}),
        sse({"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "思考中"}}),
        sse({"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "回答内容"}}),
        sse({"type": "message_delta", "usage": {"output_tokens": 55}}),
        b"data: [DONE]\n\n",
    ]
    for c in chunks:
        extract_anthropic_sse_content(c, state)
    flush_anthropic_sse_state(state)
    assert "".join(state["reasoning_parts"]) == "思考中"
    assert "".join(state["content_parts"]) == "回答内容"
    # extract_anthropic_usage_tokens: 总输入 = input + cache_read = 100 + 30
    assert state["input_tokens"] == 130, state["input_tokens"]
    assert state["cached_tokens"] == 30, state["cached_tokens"]
    assert state["output_tokens"] == 55
    assert state["upstream_usage"].get("output_tokens") == 55
    print("6. 回归 text/thinking/usage: OK")


def test_7_multiple_tools_cross_chunk():
    """多个 tool block + 恶劣分块组合"""
    args1 = json.dumps({"cmd": "查询数据" * 20}, ensure_ascii=False)
    args2 = json.dumps({"todos": [{"content": "创建项目骨架（package.json）"}]}, ensure_ascii=False)
    stream = (
        tool_block_events(args1, name="execute_command") +
        tool_block_events(args2, name="todo_write")
    )
    # 修正第二个 block 的 index（构造函数写死 index=0 不影响解析逻辑）
    state = new_state()
    for i in range(0, len(stream), 23):
        extract_anthropic_sse_content(stream[i:i + 23], state)
    flush_anthropic_sse_state(state)
    assert len(state["tool_calls"]) == 2
    assert json.loads(state["tool_calls"][0]["function"]["arguments"]) == json.loads(args1)
    assert json.loads(state["tool_calls"][1]["function"]["arguments"]) == json.loads(args2)
    print("7. 多工具 + 恶劣分块: OK")


if __name__ == "__main__":
    test_1_cross_chunk_line_split()
    test_2_multibyte_boundary()
    test_3_unicode_escape_normalization()
    test_4_invalid_json_preserved()
    test_5_flush_without_stop()
    test_6_regression_text_thinking_usage()
    test_7_multiple_tools_cross_chunk()
    print("\n全部通过 ✓")
