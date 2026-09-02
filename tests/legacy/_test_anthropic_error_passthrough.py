# -*- coding: utf-8 -*-
"""
回归测试：anthropic 格式调用时上游错误必须传回客户端且监控记 failed

复现场景（2026-07-17 22:52 请求 39b6b8e8）：
  客户端 anthropic /v1/messages → 转 OpenAI → handle_anthropic_native_from_openai
  → call_api_passthrough 透传上游 /messages → 上游 400（图片超5MB）
  → call_api_passthrough 输出 SSE 包装错误块 data: {"error":...}\n\ndata: [DONE]\n\n

修复前：首块检测只解析裸 JSON 漏检 SSE 包装块；流内错误事件无 type 字段被静默丢弃
       → 监控记 success、客户端收到空消息看不到报错
"""
import asyncio
import json
import sys

sys.path.insert(0, ".")

from routes._direct_api_utils import detect_first_chunk_error
from routes._direct_api_anthropic import build_openai_stream_from_anthropic
from converters.anthropic_openai import build_anthropic_streaming_response

# 主人日志中的真实上游错误（OpenRouter /messages 端点，无 type 字段）
UPSTREAM_ERROR = {
    "error": {
        "message": "Provider returned error",
        "code": 400,
        "metadata": {
            "raw": '{"type":"error","error":{"type":"invalid_request_error","message":"messages.2.content.1.image.source.base64: image exceeds 5 MB maximum: 6801748 bytes > 5242880 bytes"},"request_id":"req_vrtx_011Cd7pTaaVjzC3VaAS1xYqE"}',
            "provider_name": "Google",
            "is_byok": False,
        },
    },
    "user_id": "org_3EN5RjMkQ9PBNQqNpSqzi8wdclM",
}
SSE_WRAPPED_ERROR = (
    "data: " + json.dumps(UPSTREAM_ERROR, ensure_ascii=False) + "\n\ndata: [DONE]\n\n"
).encode("utf-8")

PASSED = []
FAILED = []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


# ────────────────────────────────────────────────
print("1) detect_first_chunk_error：三种首块形态")

is_err, norm = detect_first_chunk_error(SSE_WRAPPED_ERROR)
check("SSE 包装错误块被识别", is_err)
check("错误 message 已合并 metadata.raw 详情",
      is_err and "image exceeds 5 MB" in norm["error"]["message"],
      str(norm)[:200] if norm else "None")

is_err2, _ = detect_first_chunk_error(json.dumps(UPSTREAM_ERROR).encode("utf-8"))
check("裸 JSON 错误体被识别", is_err2)

normal_chunk = b'data: {"choices":[{"delta":{"content":"hi"},"index":0}]}\n\n'
is_err3, _ = detect_first_chunk_error(normal_chunk)
check("正常 OpenAI SSE 块不误报", not is_err3)

anthropic_start = b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","usage":{"input_tokens":10}}}\n\n'
is_err4, _ = detect_first_chunk_error(anthropic_start)
check("正常 Anthropic SSE 块不误报", not is_err4)


# ────────────────────────────────────────────────
print("2) stream_generator：流中出现无 type 错误事件（绕过首块检测的场景）")


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
    return 4443


def stub_estimate_tokens(text, model=None):
    return len(text) // 4


async def run_stream_case(chunks):
    """把上游字节块喂给 build_openai_stream_from_anthropic，收集 OpenAI SSE 输出"""
    async def fake_upstream():
        for c in chunks:
            yield c

    monitoring = StubMonitoring()
    response = build_openai_stream_from_anthropic(
        api_iterator=fake_upstream(),
        model_name="claude-fable-5",
        request_id="test-req-0001",
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
    return b"".join(out), monitoring


async def main():
    # 场景 A：正常流先输出一点内容，然后上游中途给出无 type 的错误事件
    normal_then_error = [
        b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":100}}}\n\n',
        b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
        ("data: " + json.dumps(UPSTREAM_ERROR, ensure_ascii=False) + "\n\n").encode("utf-8"),
    ]
    out_bytes, monitoring = await run_stream_case(normal_then_error)
    out_text = out_bytes.decode("utf-8")

    error_chunks = [
        json.loads(line[5:].strip())
        for line in out_text.splitlines()
        if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]")
    ]
    error_chunk = next((c for c in error_chunks if "error" in c), None)
    check("OpenAI SSE 输出中包含 error chunk", error_chunk is not None, out_text[:300])
    check("error chunk 无双层嵌套（error.message 为字符串）",
          error_chunk is not None and isinstance(error_chunk["error"].get("message"), str),
          str(error_chunk)[:200] if error_chunk else "None")
    check("error message 含 metadata.raw 详情",
          error_chunk is not None and "image exceeds 5 MB" in error_chunk["error"]["message"])
    check("监控记录为 failed",
          len(monitoring.end_calls) == 1 and monitoring.end_calls[0]["success"] is False,
          str(monitoring.end_calls)[:200])

    # 场景 B：Anthropic 原生 error 事件（带 event: error + type:error）回归
    native_error = [
        b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\n\n',
    ]
    out_bytes_b, monitoring_b = await run_stream_case(native_error)
    out_text_b = out_bytes_b.decode("utf-8")
    check("原生 error 事件仍被识别（回归）",
          '"Overloaded"' in out_text_b and monitoring_b.end_calls[0]["success"] is False,
          out_text_b[:200])

    # 场景 C：正常完整流不受影响（回归）
    normal_full = [
        b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":10}}}\n\n',
        b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
        b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}\n\n',
        b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}\n\n',
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]
    out_bytes_c, monitoring_c = await run_stream_case(normal_full)
    out_text_c = out_bytes_c.decode("utf-8")
    check("正常流输出内容不受影响（回归）",
          '"content": "Hello"' in out_text_c or '"content":"Hello"' in out_text_c)
    check("正常流监控记录为 success（回归）",
          monitoring_c.end_calls[0]["success"] is True)

    # ────────────────────────────────────────────────
    print("3) 串联 build_anthropic_streaming_response：anthropic 客户端视角")

    async def fake_upstream_err():
        for c in normal_then_error:
            yield c

    monitoring_d = StubMonitoring()
    openai_stream = build_openai_stream_from_anthropic(
        api_iterator=fake_upstream_err(),
        model_name="claude-fable-5",
        request_id="test-req-0002",
        monitoring_service=monitoring_d,
        endpoint_config={},
        pricing_config={},
        direct_api_service=StubDirectApi(),
        estimate_message_tokens_func=stub_estimate_messages,
        estimate_tokens_func=stub_estimate_tokens,
        openai_req={"messages": [{"role": "user", "content": "hi"}], "stream": True},
        full_messages=[],
    )
    anthropic_response = build_anthropic_streaming_response(
        openai_streaming_response=openai_stream,
        request_model="claude-fable-5",
    )
    final_parts = []
    async for chunk in anthropic_response.body_iterator:
        final_parts.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
    final_text = "".join(final_parts)

    check("客户端收到 event: error 事件", "event: error" in final_text, final_text[:300])
    check("event: error 的 payload 为 Anthropic 错误格式且含详情",
          '"type": "error"' in final_text and "image exceeds 5 MB" in final_text,
          final_text[:400])


asyncio.run(main())

print()
print(f"通过 {len(PASSED)} 项, 失败 {len(FAILED)} 项")
if FAILED:
    print("失败项:", FAILED)
    sys.exit(1)
