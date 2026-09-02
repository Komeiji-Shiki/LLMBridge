# -*- coding: utf-8 -*-
"""Anthropic 流式转换协议边角回归测试
覆盖：
1. 上游首个 tool_call delta 只有 arguments（无 id/name）时，
   先补发 content_block_start 再发 input_json_delta（协议顺序）
2. 块只 stop 一次，且不对未 started 的块发 block_stop
"""
import asyncio
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

from converters.anthropic_openai import build_anthropic_streaming_response


class _FakeResp:
    """模拟上游 OpenAI SSE StreamingResponse"""
    def __init__(self, chunks):
        async def _gen():
            for c in chunks:
                yield c
        self.body_iterator = _gen()


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


async def _collect_events(chunks):
    resp = build_anthropic_streaming_response(_FakeResp(chunks), "test-model")
    events = []
    async for part in resp.body_iterator:
        events.append(part if isinstance(part, str) else part.decode("utf-8"))
    return "".join(events)


# 场景：首个 delta 只有 arguments，第二个 delta 才带 id/name
chunks = [
    _sse({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": '{"a":'}}]}}]}),
    _sse({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "call_1", "function": {"name": "foo", "arguments": "1}"}}]}}]}),
    _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
    b"data: [DONE]\n\n",
]

raw = asyncio.run(_collect_events(chunks))

# 解析出事件序列（event 行）
lines = [l for l in raw.split("\n") if l.startswith("event: ") or l.startswith("data: ")]
event_names = [l[len("event: "):] for l in raw.split("\n") if l.startswith("event: ")]

# 提取 content_block_* 事件的顺序
block_events = [e for e in event_names if e.startswith("content_block")]

# 断言 1：首个 content_block 事件必须是 start（不能上来就是 delta）
assert block_events and block_events[0] == "content_block_start", \
    f"首个块事件应为 content_block_start，实际: {block_events}"

# 断言 2：start/stop 各恰好一次，且 stop 在 delta 之后
assert block_events.count("content_block_start") == 1, block_events
assert block_events.count("content_block_stop") == 1, block_events
assert block_events.index("content_block_stop") > block_events.index("content_block_start")

# 断言 3：两段 arguments 都作为 input_json_delta 发出
data_payloads = [json.loads(l[len("data: "):]) for l in raw.split("\n")
                 if l.startswith("data: ")]
json_deltas = [p for p in data_payloads
               if p.get("type") == "content_block_delta"
               and p.get("delta", {}).get("type") == "input_json_delta"]
combined = "".join(p["delta"]["partial_json"] for p in json_deltas)
assert combined == '{"a":1}', f"arguments 拼接结果: {combined!r}"

# 断言 4：块 start 事件里补上了后到的 id/name 之前的占位（首个 delta 时 id/name 为空）
starts = [p for p in data_payloads if p.get("type") == "content_block_start"]
assert starts[0]["content_block"]["type"] == "tool_use"

# 断言 5：消息收尾完整
assert "message_stop" in event_names
print("Anthropic 流式协议边角测试通过 ✅")
