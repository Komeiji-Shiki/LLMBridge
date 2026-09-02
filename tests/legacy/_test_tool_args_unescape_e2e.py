# -*- coding: utf-8 -*-
"""工具参数 \\uXXXX 转义提前解码的端到端回归测试

覆盖三条给下游转发的流式链路（转义均故意切断在两个增量之间）：
1. OpenAI 上游 → Anthropic 下游（converters.build_anthropic_streaming_response）
2. Anthropic 上游 → OpenAI 下游（routes._direct_api_anthropic.build_openai_stream_from_anthropic）
3. OpenAI 上游 → OpenAI 下游直通（PassthroughStreamSession.process_sse_chunk）

/v1/messages 直通链路的 AnthropicSSEToolArgsRewriter 已由 _test_json_unescape.py 覆盖。
"""
import asyncio
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

from converters.anthropic_openai import build_anthropic_streaming_response
from routes._direct_api_anthropic import build_openai_stream_from_anthropic
from routes._direct_api_stream_session import PassthroughStreamSession

# 原始参数 JSON 文本（模型 ASCII 转义风格），转义切断在两段增量之间
ARGS_PART1 = '{"old": "\\u52'
ARGS_PART2 = '17\\u5b81\\u665a\\u5e74"}'
ARGS_PLAIN = '{"old": "列宁晚年"}'

PASSED = []
FAILED = []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


# ────────────────────────────────────────────────
# 公共 stub
# ────────────────────────────────────────────────

class StubMonitoring:
    def __init__(self):
        self.end_calls = []

    def request_end(self, **kwargs):
        self.end_calls.append(kwargs)

    async def broadcast_to_monitors(self, payload):
        pass


class StubDirectApi:
    def calculate_cost(self, **kwargs):
        return {}

    def split_thinking_content(self, content, sep):
        return "", content


def stub_estimate_messages(messages, model=None):
    return 100


def stub_estimate_tokens(text, model=None):
    return len(text) // 4


def collect_openai_tool_args(raw: bytes) -> str:
    """从 OpenAI SSE 字节流中拼接 tool_calls arguments 增量。"""
    parts = []
    for line in raw.decode("utf-8").split("\n"):
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        try:
            obj = json.loads(line[6:])
        except Exception:
            continue
        for choice in obj.get("choices", []):
            for tc in (choice.get("delta") or {}).get("tool_calls") or []:
                args = (tc.get("function") or {}).get("arguments")
                if args:
                    parts.append(args)
    return "".join(parts)


# ────────────────────────────────────────────────
print("1) OpenAI 上游 → Anthropic 下游（tool_calls → input_json_delta）")


class _FakeResp:
    def __init__(self, chunks):
        async def _gen():
            for c in chunks:
                yield c
        self.body_iterator = _gen()


def _oai_sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


async def case_openai_to_anthropic():
    chunks = [
        _oai_sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1",
             "function": {"name": "apply_diff", "arguments": ARGS_PART1}}]}}]}),
        _oai_sse({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": ARGS_PART2}}]}}]}),
        _oai_sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        b"data: [DONE]\n\n",
    ]
    resp = build_anthropic_streaming_response(_FakeResp(chunks), "test-model")
    out = []
    async for part in resp.body_iterator:
        out.append(part if isinstance(part, str) else part.decode("utf-8"))
    raw = "".join(out)

    partials = []
    for line in raw.split("\n"):
        if not line.startswith("data: "):
            continue
        obj = json.loads(line[6:])
        if (obj.get("type") == "content_block_delta"
                and obj.get("delta", {}).get("type") == "input_json_delta"):
            partials.append(obj["delta"]["partial_json"])
    combined = "".join(partials)
    check("partial_json 拼接为明文中文", combined == ARGS_PLAIN, repr(combined))
    check("语义与原始参数等价", json.loads(combined) == json.loads(ARGS_PART1 + ARGS_PART2))


# ────────────────────────────────────────────────
print_case2 = "2) Anthropic 上游 → OpenAI 下游（input_json_delta → tool_calls）"


def _anthropic_sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")


async def case_anthropic_to_openai():
    print(print_case2)
    chunks = [
        _anthropic_sse("message_start", {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 10}}}),
        _anthropic_sse("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "tu_1", "name": "apply_diff"}}),
        _anthropic_sse("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": ARGS_PART1}}),
        _anthropic_sse("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": ARGS_PART2}}),
        _anthropic_sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _anthropic_sse("message_delta", {
            "type": "message_delta", "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 20}}),
        _anthropic_sse("message_stop", {"type": "message_stop"}),
    ]

    async def fake_upstream():
        for c in chunks:
            yield c

    monitoring = StubMonitoring()
    response = build_openai_stream_from_anthropic(
        api_iterator=fake_upstream(),
        model_name="claude-fable-5",
        request_id="test-req-0002",
        monitoring_service=monitoring,
        endpoint_config={},
        pricing_config={},
        direct_api_service=StubDirectApi(),
        estimate_message_tokens_func=stub_estimate_messages,
        estimate_tokens_func=stub_estimate_tokens,
        openai_req={"messages": [{"role": "user", "content": "hi"}], "stream": True},
        full_messages=[],
    )
    out = []
    async for chunk in response.body_iterator:
        out.append(chunk if isinstance(chunk, bytes) else str(chunk).encode())
    raw = b"".join(out)

    combined = collect_openai_tool_args(raw)
    check("arguments 拼接为明文中文", combined == ARGS_PLAIN, repr(combined))

    end = monitoring.end_calls[0] if monitoring.end_calls else {}
    logged = (end.get("response_tool_calls") or [{}])[0].get("function", {}).get("arguments", "")
    check("监控记录的参数同为明文", logged == ARGS_PLAIN, repr(logged))


# ────────────────────────────────────────────────
print_case3 = "3) OpenAI 上游 → OpenAI 下游直通（PassthroughStreamSession）"


def make_session():
    return PassthroughStreamSession(
        request_id="test-req-0003",
        display_name="test-model",
        openai_req={"messages": [{"role": "user", "content": "hi"}], "stream": True},
        endpoint_config={},
        pricing_config={},
        thinking_separator=None,
        monitoring_service=StubMonitoring(),
        direct_api_service=StubDirectApi(),
        estimate_message_tokens_func=stub_estimate_messages,
        estimate_tokens_func=stub_estimate_tokens,
        full_messages=[],
    )


def case_openai_passthrough():
    print(print_case3)
    session = make_session()
    chunk1 = _oai_sse({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "call_1",
         "function": {"name": "apply_diff", "arguments": ARGS_PART1}}]}}]})
    chunk2 = _oai_sse({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": ARGS_PART2}}]}}]})

    forwarded = session.process_sse_chunk(chunk1) + session.process_sse_chunk(chunk2)
    combined = collect_openai_tool_args(forwarded)
    check("转发给下游的 arguments 为明文", combined == ARGS_PLAIN, repr(combined))

    # 累积器内部结构不做强假设：用 flush + finalize 的公开语义校验
    session._flush_tool_args_tails()
    from routes._direct_api_utils import finalize_tool_calls
    final = finalize_tool_calls(session.tool_call_accumulator) or []
    logged = (final[0].get("function", {}) or {}).get("arguments", "") if final else ""
    check("监控累积器中的参数同为明文", logged == ARGS_PLAIN, repr(logged))

    # 无转义内容走快速路径：字节原样返回（不重编码）
    session2 = make_session()
    plain_chunk = _oai_sse({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "call_2", "function": {"name": "foo", "arguments": '{"a": 1}'}}]}}]})
    check("无转义参数原样透传", session2.process_sse_chunk(plain_chunk) == plain_chunk)


async def main():
    await case_openai_to_anthropic()
    await case_anthropic_to_openai()
    case_openai_passthrough()
    print(f"\n通过 {len(PASSED)} 项, 失败 {len(FAILED)} 项")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
