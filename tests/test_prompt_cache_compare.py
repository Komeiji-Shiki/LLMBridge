"""Tests for utils.prompt_cache_compare and the monitor compare route."""
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from utils.prompt_cache_compare import (
    cache_summary,
    compare,
    compare_messages,
    compare_params,
    compare_tools,
    compute_tps,
    infer,
)


def _log(request_id, messages, cached=100, prompt_input=1000, **extra):
    log = {
        "request_id": request_id,
        "model": "demo-model",
        "timestamp": 1700000000.0,
        "end_timestamp": 1700000010.0,
        "input_tokens": prompt_input,
        "output_tokens": 50,
        "cached_tokens": cached,
        "duration": 10.0,
        "request_messages": messages,
        "temperature": 0.7,
    }
    log.update(extra)
    return log


def test_strict_append_detected_and_break_point_located():
    old = _log("a", [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}])
    new = _log("b", [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"},
                     {"role": "assistant", "content": "hello"}, {"role": "user", "content": "more"}],
               cached=200)
    result = compare_messages(old, new)
    assert result["strict_append"] is True
    assert result["first_difference"] is None
    assert result["common_identical"] is True
    assert [m["index"] for m in result["appended"]] == [2, 3]


def test_first_differing_message_reports_offset_and_context():
    old = _log("a", [{"role": "user", "content": "hello world"}])
    new = _log("b", [{"role": "user", "content": "hello WORLD"}])
    result = compare_messages(old, new)
    assert result["strict_append"] is False
    first = result["first_difference"]
    assert first["index"] == 0
    assert first["old_role"] == "user"
    assert first["old_sha256"] != first["new_sha256"]
    assert isinstance(first["canonical_char_offset"], int)
    assert "hello" in (first["old_context"] or "")


def test_tools_identical_different_and_unknown():
    tools = [{"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}]
    assert compare_tools(_log("a", [], tools=tools), _log("b", [], tools=tools))["status"] == "identical"
    changed = [{"type": "function", "function": {"name": "search2", "parameters": {"type": "object"}}}]
    different = compare_tools(_log("a", [], tools=tools), _log("b", [], tools=changed))
    assert different["status"] == "different"
    assert different["first_difference"]["index"] == 0
    unknown = compare_tools(_log("a", []), _log("b", []))
    assert unknown["status"] == "unknown"
    sha_same = compare_tools(_log("a", [], tools_sha256="abc", tools_count=2),
                             _log("b", [], tools_sha256="abc", tools_count=2))
    assert sha_same["status"] == "identical"


def test_params_diff_ignores_volatile_fields():
    old = _log("a", [], cached=500, output_tokens=10, response_content="x")
    new = _log("b", [], cached=100, output_tokens=99, response_content="y")
    result = compare_params(old, new)
    assert result["same"] is True
    changed = _log("b", [], cached=100, temperature=0.9, system_fingerprint="fp2")
    result = compare_params(dict(old, system_fingerprint="fp1"), changed)
    assert result["same"] is False
    assert {d["key"] for d in result["differences"]} == {"temperature", "system_fingerprint"}


def test_params_ignore_measurements_and_request_ids():
    old = _log("a", [], conversation_id="conv-1", gateway_request_id="gw-1",
               timings={"total_ms": 100.0}, request_params={"stream": True},
               total_tokens=1050, stop_reason="end_turn")
    new = _log("b", [], conversation_id="conv-2", gateway_request_id="gw-2",
               timings={"total_ms": 900.0}, request_params={"stream": False},
               total_tokens=1060, stop_reason="stop")
    # measurements / ids / rebuilt views must not count as prompt changes
    assert compare_params(old, new)["same"] is True


def test_identity_reports_attribution_mismatch():
    from utils.prompt_cache_compare import compare_identity
    same = compare_identity(_log("a", [], conversation_id="c1", caller_id="u1"),
                            _log("b", [], conversation_id="c1", caller_id="u1"))
    assert same["same"] is True
    different = compare_identity(_log("a", [], conversation_id="c1"),
                                 _log("b", [], conversation_id="c2"))
    assert different["same"] is False
    assert [f["key"] for f in different["fields"] if not f["same"]] == ["conversation_id"]
    result = compare(_log("a", [], conversation_id="c1", cached=500),
                     _log("b", [], conversation_id="c2", cached=100))
    assert result["identity"]["same"] is False
    assert any("归属不同" in line for line in result["inference"])


def test_cache_summary_hit_rate_and_delta():
    summary = cache_summary(_log("a", [], cached=250), _log("b", [], cached=100))
    assert summary["old"]["hit_rate"] == 25.0
    assert summary["new"]["hit_rate"] == 10.0
    assert summary["cached_delta"] == -150


def test_infer_upstream_break_when_history_intact():
    old = _log("a", [{"role": "user", "content": "hi"}], cached=500)
    new = _log("b", [{"role": "user", "content": "hi"},
                     {"role": "assistant", "content": "yo"},
                     {"role": "user", "content": "again"}], cached=100)
    result = compare(old, new)
    assert any("上游侧" in line for line in result["inference"])
    assert result["summary"]["cached_delta"] == -400


def test_infer_points_at_payload_change():
    old = _log("a", [{"role": "user", "content": "hi"}], cached=500)
    new = _log("b", [{"role": "user", "content": "HI"}], cached=100)
    result = compare(old, new)
    assert any("候选断裂位置" in line for line in result["inference"])


def test_compute_tps_prefers_output_ms_then_duration():
    tps = compute_tps({"output_tokens": 200, "duration": 10.0,
                       "timings": {"output_ms": 5000.0, "first_business_ms": 1200.0}})
    assert tps["decode_tps"] == 40.0
    assert tps["e2e_tps"] == 20.0
    assert tps["basis"] == "output_ms"
    assert tps["ttft_s"] == 1.2
    assert tps["caveat"] is None


def test_compute_tps_flags_non_streaming_decode():
    tps = compute_tps({"output_tokens": 200, "duration": 10.0,
                       "timings": {"output_ms": 50.0}})
    assert tps["decode_tps"] == 4000.0
    assert tps["caveat"] is not None
    assert tps["e2e_tps"] == 20.0


def test_compute_tps_missing_data_never_raises():
    assert compute_tps({})["basis"] == "unavailable"
    assert compute_tps({"output_tokens": 0, "duration": 5})["e2e_tps"] is None
    assert compute_tps({"output_tokens": 10, "duration": 0})["e2e_tps"] is None


def test_compare_route_orders_by_timestamp_and_rejects_same_id():
    from routes import monitor_routes
    old = _log("id-old", [{"role": "user", "content": "same"}], cached=300,
               timestamp=1700000000.0, end_timestamp=1700000005.0)
    new = _log("id-new", [{"role": "user", "content": "same"}], cached=100,
               timestamp=1700000100.0, end_timestamp=1700000105.0)

    class FakeMonitoring:
        def get_request_details(self, request_id):
            return {"id-old": old, "id-new": new}.get(request_id)

    async def fake_compare(a, b):
        return await monitor_routes.compare_request_logs(FakeMonitoring(), a, b)

    import asyncio
    # reversed input order must still report old/new chronologically
    result = asyncio.run(fake_compare("id-new", "id-old"))
    assert result["old_ref"]["request_id"] == "id-old"
    assert result["new_ref"]["request_id"] == "id-new"
    assert result["summary"]["cached_delta"] == -200

    import pytest
    with pytest.raises(Exception):
        asyncio.run(fake_compare("id-old", "id-old"))
    with pytest.raises(Exception):
        asyncio.run(fake_compare("id-old", "missing"))


def test_compare_endpoint_wired_on_router():
    from routes import monitor_routes
    app = FastAPI()
    app.include_router(monitor_routes.router)
    client = TestClient(app)
    response = client.get("/api/monitor/compare?a=x&b=x")
    assert response.status_code == 400
    assert "不同" in response.json()["detail"]
