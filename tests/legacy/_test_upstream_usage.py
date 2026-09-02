"""upstream_usage 记录链路冒烟测试

覆盖：
1. PassthroughStreamSession._process_usage：原生 usage 深拷贝保存（forward 模式改写前）+ 分批合并
2. extract_anthropic_sse_content：message_start/message_delta 原生 usage 增量合并
3. SQLiteLogger：upstream_usage 列写入、详情读回、分页查询反序列化
"""
import json
import tempfile
from pathlib import Path


def test_stream_session():
    from routes._direct_api_stream_session import PassthroughStreamSession

    session = PassthroughStreamSession(
        request_id="test-req", display_name="test-model", openai_req={"messages": []},
        endpoint_config={"cached_tokens_mode": "forward"}, pricing_config={},
        thinking_separator=None, monitoring_service=None, direct_api_service=None,
        estimate_message_tokens_func=None, estimate_tokens_func=None, full_messages=[],
    )
    chunk = {"usage": {"prompt_tokens": 100, "completion_tokens": 50,
                       "prompt_tokens_details": {"cached_tokens": 30},
                       "cost": 0.0123}}
    session._process_usage(chunk)
    # forward 模式会把 chunk 内 prompt_tokens 改写为 130，原生记录必须保留上游原值 100
    assert session.upstream_usage["prompt_tokens"] == 100, session.upstream_usage
    assert session.upstream_usage["cost"] == 0.0123
    assert chunk["usage"]["prompt_tokens"] == 130  # 既有 forward 修正行为不回归
    # 分批下发的 usage 增量合并
    session._process_usage({"usage": {"total_tokens": 180}})
    assert session.upstream_usage["total_tokens"] == 180
    assert session.upstream_usage["prompt_tokens"] == 100
    print("1. PassthroughStreamSession: OK")


def test_anthropic_sse():
    from converters.anthropic_openai import extract_anthropic_sse_content

    state = {"content_parts": [], "reasoning_parts": [], "tool_calls": [],
             "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
             "upstream_usage": {}}
    start_event = {
        "type": "message_start",
        "message": {"usage": {"input_tokens": 200,
                              "cache_read_input_tokens": 60}},
    }
    delta_event = {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": 77},
    }
    data_prefix = "da" "ta: "
    sse_sep = chr(10) * 2
    ms = data_prefix + json.dumps(start_event) + sse_sep
    md = data_prefix + json.dumps(delta_event) + sse_sep
    extract_anthropic_sse_content(ms.encode(), state)
    extract_anthropic_sse_content(md.encode(), state)
    assert state["upstream_usage"] == {
        "input_tokens": 200, "cache_read_input_tokens": 60, "output_tokens": 77
    }, state["upstream_usage"]
    # extract_anthropic_usage_tokens 的口径是“总输入”：
    # input_tokens(未命中缓存 200) + cache_read_input_tokens(60) = 260
    assert state["input_tokens"] == 260, state["input_tokens"]
    assert state["output_tokens"] == 77, state["output_tokens"]
    assert state["cached_tokens"] == 60, state["cached_tokens"]
    print("2. extract_anthropic_sse_content: OK")


def test_sqlite_logger():
    from modules.monitoring_sqlite import SQLiteLogger

    with tempfile.TemporaryDirectory() as td:
        db = SQLiteLogger(Path(td) / "test.db")
        try:
            entry = {"type": "request_end", "request_id": "rid-1",
                     "timestamp": 1700000000.0, "model": "m", "status": "success",
                     "success": True, "duration": 1.0,
                     "input_tokens": 10, "output_tokens": 5,
                     "upstream_usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                        "cost": 0.01}}
            db.write_request(entry)
            # 无 upstream_usage 的记录（旧行为兼容）
            db.write_request({"type": "request_end", "request_id": "rid-2",
                              "timestamp": 1700000001.0, "model": "m",
                              "status": "success", "success": True, "duration": 1.0})

            detail = db.get_request_details("rid-1")
            assert detail["upstream_usage"] == {
                "prompt_tokens": 10, "completion_tokens": 5, "cost": 0.01}, detail
            assert db.get_request_details("rid-2")["upstream_usage"] is None

            rows = db.query_requests(limit=10)
            by_id = {r["request_id"]: r for r in rows["items"]}
            assert by_id["rid-1"]["upstream_usage"]["cost"] == 0.01
            assert by_id["rid-2"]["upstream_usage"] is None
        finally:
            db.close()
    print("3. SQLiteLogger: OK")


if __name__ == "__main__":
    test_stream_session()
    test_anthropic_sse()
    test_sqlite_logger()
    print("ALL TESTS PASSED")
