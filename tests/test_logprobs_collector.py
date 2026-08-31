"""DeepSeek logprobs 蒸馏采集器测试。

覆盖：
- 仅对 DeepSeek 官方域名 / 模型启用
- 请求侧强制注入 logprobs + top_logprobs
- SSE 事件旁路采集并可从客户端响应里剥离 logprobs
- finish_reason 训练候选分类
- raw / normalized JSONL 落盘
"""
import asyncio
import gzip
import json
from pathlib import Path

import pytest

from core.config_loader import CONFIG
from services.logprobs_collector import (
    LogprobsCollector,
    create_logprobs_collector,
    get_collection_config,
    should_collect,
)


def _collection_cfg(monkeypatch, tmp_path: Path, **overrides):
    cfg = {
        "enabled": True,
        "output_dir": str(tmp_path),
        "api_base_hosts": ["api.deepseek.com"],
        "top_logprobs": 20,
        "strip_logprobs_from_client": True,
        "record_raw_stream": True,
        "require_logprobs_to_write": True,
        "compress": False,
    }
    cfg.update(overrides)
    monkeypatch.setitem(CONFIG, "deepseek_logprobs", cfg)
    return get_collection_config()


def _make_collector(cfg, strip_from_client=True):
    collector = LogprobsCollector(
        request_id="test-request",
        model_name="deepseek-v4-flash",
        target_model_id="deepseek-v4-flash",
        display_name="DeepSeek V4 Flash",
        cfg=cfg,
        strip_from_client=strip_from_client,
    )
    collector.record_upstream_request(
        {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}], "logprobs": True, "top_logprobs": 20},
        {},
    )
    return collector


def _read_jsonl(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_should_collect_official_deepseek_only(monkeypatch, tmp_path):
    cfg = _collection_cfg(monkeypatch, tmp_path)

    assert should_collect(cfg, "https://api.deepseek.com/v1", "deepseek-v4-flash", "ds", {}) is True
    assert should_collect(cfg, "https://api.deepseek.com", "deepseek-reasoner", "ds", {}) is True
    assert should_collect(cfg, "https://example.com/v1", "deepseek-v4-flash", "ds", {}) is False
    assert should_collect(cfg, "https://api.deepseek.com/v1", "other-model", "other", {"collect_logprobs": False}) is False
    assert should_collect(cfg, "https://api.deepseek.com/v1", "other-model", "other", {"collect_logprobs": True}) is True


def test_create_logprobs_collector_injects_request(monkeypatch, tmp_path):
    _collection_cfg(monkeypatch, tmp_path)
    request_body = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}

    collector = create_logprobs_collector(
        passthrough_request=request_body,
        openai_req={"model": "deepseek-chat", "messages": []},
        model_name="deepseek-chat",
        target_model_id="deepseek-chat",
        display_name="DeepSeek Chat",
        api_base_url="https://api.deepseek.com/v1",
        endpoint_config={},
        request_id="rid",
        endpoint_path="/chat/completions",
    )

    assert collector is not None
    assert request_body["logprobs"] is True
    assert request_body["top_logprobs"] == 20
    assert collector.strip_from_client is True


def test_client_requested_logprobs_is_not_stripped(monkeypatch, tmp_path):
    _collection_cfg(monkeypatch, tmp_path)
    request_body = {"model": "deepseek-chat", "messages": [], "logprobs": True}

    collector = create_logprobs_collector(
        passthrough_request=request_body,
        openai_req={"model": "deepseek-chat", "messages": [], "logprobs": True},
        model_name="deepseek-chat",
        target_model_id="deepseek-chat",
        display_name="DeepSeek Chat",
        api_base_url="https://api.deepseek.com/v1",
        endpoint_config={},
        request_id="rid",
        endpoint_path="/chat/completions",
    )

    assert collector is not None
    assert collector.strip_from_client is False


def test_stream_capture_collects_logprobs_and_writes_jsonl(monkeypatch, tmp_path):
    cfg = _collection_cfg(monkeypatch, tmp_path)
    collector = _make_collector(cfg)

    event = {
        "id": "cmpl-1",
        "created": 123,
        "model": "deepseek-v4-flash",
        "system_fingerprint": "fp",
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        "choices": [{
            "index": 0,
            "delta": {"content": "你好", "reasoning_content": "思考"},
            "finish_reason": "stop",
            "logprobs": {
                "reasoning_content": [{"token": "思考", "logprob": -0.1, "bytes": [1, 2], "top_logprobs": [{"token": "思考", "logprob": -0.1}]}],
                "content": [{"token": "你好", "logprob": -0.2, "bytes": [3, 4], "top_logprobs": [{"token": "你好", "logprob": -0.2}]}],
            },
        }],
    }

    assert collector.capture_stream_event(event) is True
    assert "logprobs" not in event["choices"][0]
    asyncio.run(collector.finish(completed=True))

    normalized_files = list((tmp_path / "normalized").glob("*.jsonl"))
    raw_files = list((tmp_path / "raw").glob("*.jsonl"))
    assert normalized_files and raw_files

    normalized = _read_jsonl(normalized_files[0])[0]
    assert normalized["request"]["messages"] == [{"role": "user", "content": "hi"}]
    assert normalized["response"]["content"] == "你好"
    assert normalized["response"]["reasoning_content"] == "思考"
    assert normalized["response"]["finish_reason"] == "stop"
    assert normalized["logprobs"]["content"][0]["token"] == "你好"
    assert normalized["logprobs"]["reasoning_content"][0]["token"] == "思考"
    assert normalized["training"]["candidate"] is True
    assert normalized["training"]["category"] == "chat"

    raw = _read_jsonl(raw_files[0])[0]
    assert raw["stream_events"][0]["logprobs"]["content"][0]["token"] == "你好"


def test_length_finish_reason_is_not_training_candidate(monkeypatch, tmp_path):
    cfg = _collection_cfg(monkeypatch, tmp_path)
    collector = _make_collector(cfg)

    collector.capture_stream_event({
        "choices": [{
            "index": 0,
            "delta": {"content": "截断"},
            "finish_reason": "length",
            "logprobs": {"content": [{"token": "截断", "logprob": -0.1, "top_logprobs": []}]},
        }],
        "usage": {"prompt_tokens": 1},
    })
    asyncio.run(collector.finish(completed=True))

    normalized = _read_jsonl(next((tmp_path / "normalized").glob("*.jsonl")))[0]
    assert normalized["training"]["candidate"] is False
    assert normalized["training"]["category"] == "length"


def test_stream_session_integrates_collector_and_strips(monkeypatch, tmp_path):
    from routes._direct_api_stream_session import PassthroughStreamSession

    cfg = _collection_cfg(monkeypatch, tmp_path)
    collector = _make_collector(cfg)

    class _Monitoring:
        async def broadcast_to_monitors(self, payload):
            return None

        def request_end(self, **kwargs):
            self.last_kwargs = kwargs

    class _DirectService:
        def calculate_cost(self, **kwargs):
            return {}

    monitoring = _Monitoring()
    direct_service = _DirectService()
    session = PassthroughStreamSession(
        request_id="test-request",
        display_name="DeepSeek V4 Flash",
        openai_req={"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]},
        endpoint_config={},
        pricing_config={},
        thinking_separator=None,
        monitoring_service=monitoring,
        direct_api_service=direct_service,
        estimate_message_tokens_func=lambda *a, **k: 1,
        estimate_tokens_func=lambda *a, **k: 1,
        full_messages=[],
        logprobs_collector=collector,
    )

    event = {
        "id": "cmpl-stream",
        "choices": [{
            "index": 0,
            "delta": {"content": "好", "reasoning_content": "想"},
            "logprobs": {"content": [{"token": "好", "logprob": -0.1}]},
        }],
    }
    chunk_bytes = ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8")
    processed = session.process_sse_chunk(chunk_bytes)
    assert b'"logprobs"' not in processed

    session.input_tokens = 2
    session.output_tokens = 2
    session.request_success = True
    session.stream_completed = True
    asyncio.run(session.finalize())

    normalized = _read_jsonl(next((tmp_path / "normalized").glob("*.jsonl")))[0]
    assert normalized["response"]["content"] == "好"
    assert normalized["logprobs"]["content"][0]["token"] == "好"


def test_non_stream_capture_and_redacts_base64_images(monkeypatch, tmp_path):
    cfg = _collection_cfg(monkeypatch, tmp_path)
    collector = LogprobsCollector(
        request_id="rid",
        model_name="deepseek-chat",
        target_model_id="deepseek-chat",
        display_name="DeepSeek Chat",
        cfg=cfg,
        strip_from_client=True,
    )
    collector.record_upstream_request({
        "model": "deepseek-chat",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + ("A" * 100)}},
            ],
        }],
        "logprobs": True,
        "top_logprobs": 20,
    }, {})

    response = {
        "id": "cmpl-2",
        "model": "deepseek-chat",
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        "choices": [{
            "index": 0,
            "message": {"content": "好", "reasoning_content": ""},
            "finish_reason": "stop",
            "logprobs": {"content": [{"token": "好", "logprob": -0.3}], "reasoning_content": []},
        }],
    }
    collector.capture_non_stream_response(response)
    assert collector.strip_non_stream_response(response) is True
    assert "logprobs" not in response["choices"][0]
    asyncio.run(collector.finish(completed=True))

    normalized = _read_jsonl(next((tmp_path / "normalized").glob("*.jsonl")))[0]
    image_url = normalized["request"]["messages"][0]["content"][1]["image_url"]["url"]
    assert "redacted" in image_url
    assert "AAAA" not in image_url
