"""Responses WebSocket 的认证、路由和原始事件契约。"""
import asyncio
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from routes import api_routes as api
from routes import responses_websocket as ws_route


class FakeUpstream:
    def __init__(self):
        self.sent = []
        self.frames = asyncio.Queue()
        self.close_code = 1000
        self.closed = False

    async def send_json(self, event, **kwargs):
        self.sent.append(event)
        if event["type"] == "response.steer":
            await self.frames.put(SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps({
                "type": "response.steer.accepted", "steer": {"id": "steer-1", "previous_response_id": event["previous_response_id"]}})))
            return
        await self.frames.put(SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps({
            "type": "response.completed", "response": {"id": "resp-1", "model": event["model"],
            "output": event.get("input"), "tools": event.get("tools"), "prompt_cache_key": event.get("prompt_cache_key")}})))

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.frames.get()
        if frame is None:
            raise StopAsyncIteration
        return frame

    async def close(self):
        self.closed = True
        await self.frames.put(None)


class ResponsesWebSocketTests(unittest.TestCase):
    def setUp(self):
        self.peer = FakeUpstream()
        self.session = SimpleNamespace(ws_connect=AsyncMock(return_value=self.peer))
        self.config = {"api_type": "responses_native", "api_base_url": "https://upstream.test/v1",
                       "model_id": "gpt-6-astra", "api_key": "upstream-test-key"}
        self.patches = [
            patch.dict(api.CONFIG, {"api_key": "gateway-test-key"}, clear=True),
            patch.dict(api.MODEL_ENDPOINT_MAP, {"alias": self.config}, clear=True),
            patch.object(api.api_key_manager, "has_keys", return_value=False),
            patch.object(api, "_check_verification_cooldown"),
            patch.object(api, "_app_state", SimpleNamespace(server=SimpleNamespace(direct_api_service=SimpleNamespace(session=self.session)))),
            patch.object(ws_route, "get_api_key", AsyncMock(return_value="upstream-test-key")),
            patch.object(ws_route, "ResponsesWebSocketMonitor"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        ws_route.ResponsesWebSocketMonitor.return_value.event = AsyncMock()
        ws_route.ResponsesWebSocketMonitor.return_value.close = AsyncMock()
        app = FastAPI()
        app.include_router(ws_route.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def connect(self, **headers):
        return self.client.websocket_connect("/v1/responses", headers={"Authorization": "Bearer gateway-test-key", **headers})

    def test_monitor_failure_does_not_interrupt_native_response(self):
        ws_route.ResponsesWebSocketMonitor.return_value.event.side_effect = RuntimeError("synthetic stats failure")
        with self.connect() as client:
            client.send_json({"type": "response.create", "model": "alias", "input": "hello"})
            self.assertEqual(client.receive_json()["type"], "response.completed")

    def test_native_fields_routing_and_steering_share_one_connection(self):
        original = {"type": "reasoning", "id": "rs-original", "encrypted_content": "opaque", "summary": []}
        tools = [{"type": "function", "name": "lookup", "async": True, "parameters": {"type": "object"}}]
        with self.connect(**{"x-client-request-id": "trace", "session_id": "cache-session"}) as client:
            client.send_json({"type": "response.create", "model": "alias", "input": [original], "tools": tools, "prompt_cache_key": "stable"})
            result = client.receive_json()
            self.assertEqual(result["response"]["model"], "alias")
            self.assertEqual(result["response"]["output"], [original])
            self.assertEqual(result["response"]["tools"], tools)
            client.send_json({"type": "response.steer", "previous_response_id": "resp-1", "input": "new direction"})
            self.assertEqual(client.receive_json()["type"], "response.steer.accepted")
            client.send_json({"type": "response.create", "previous_response_id": "resp-1", "input": [{"type": "function_call_output", "call_id": "call-1", "output": "ok"}]})
            self.assertEqual(client.receive_json()["response"]["output"][0]["call_id"], "call-1")
        self.session.ws_connect.assert_awaited_once()
        ws_route.get_api_key.assert_awaited_once()
        headers = self.session.ws_connect.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer upstream-test-key")
        self.assertEqual(headers["session_id"], "cache-session")
        self.assertFalse(any(name.startswith("sec-websocket-") for name in headers))
        self.assertTrue(self.peer.closed)

    def test_wrong_key_rejected_before_upstream(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/v1/responses", headers={"Authorization": "Bearer wrong"}):
                pass
        self.session.ws_connect.assert_not_called()

    def test_bound_model_cannot_be_changed(self):
        with self.connect() as client:
            client.send_json({"type": "response.create", "model": "alias", "input": "one"})
            client.receive_json()
            client.send_json({"type": "response.create", "model": "other", "input": "two"})
            self.assertEqual(client.receive_json()["status"], 400)
            client.send_json({"type": "response.create", "model": "alias", "input": "three"})
            client.receive_json()
        self.assertEqual(len(self.peer.sent), 2)

    def test_handshake_does_not_consume_guest_rpm(self):
        manager = type(api.api_key_manager).__new__(type(api.api_key_manager))
        manager._lock = threading.Lock()
        manager._secret_index = {"guest": "key-1"}
        manager._keys = {"key-1": {"enabled": True, "allowed_models": ["alias"], "rpm_limit": 1}}
        manager._rate_limiter = MagicMock()
        manager._rate_limiter.check_and_consume.return_value = (True, 0)
        self.assertTrue(manager.validate_request("guest", consume=False)[0])
        manager._rate_limiter.check_and_consume.assert_not_called()
        self.assertTrue(manager.validate_request("guest", "alias")[0])
        manager._rate_limiter.check_and_consume.assert_called_once_with("key-1", 1)
        self.assertEqual(manager._keys["key-1"]["total_requests"], 1)

    def test_authentication_is_applied_to_every_generation_and_steer(self):
        original = api._validate_request_api_key
        with patch.object(api, "_validate_request_api_key", wraps=original) as validate:
            with self.connect() as client:
                client.send_json({"type": "response.create", "model": "alias", "input": "one"})
                client.receive_json()
                client.send_json({"type": "response.steer", "previous_response_id": "resp-1", "input": "two"})
                client.receive_json()
        self.assertEqual(len(validate.call_args_list), 3)
        self.assertFalse(validate.call_args_list[0].kwargs["consume_rate_limit"])


class ResponsesWebSocketMonitoringTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_response_is_recorded_once_with_usage(self):
        from services.responses_websocket_monitor import ResponsesWebSocketMonitor
        with patch("services.responses_websocket_monitor.monitoring_service") as monitoring, \
             patch("routes.responses_api._complete_responses_monitoring", new_callable=AsyncMock) as finish:
            observer = ResponsesWebSocketMonitor("alias", {}, MagicMock())
            observer.request({"type": "response.create", "input": "hello"})
            await observer.event(json.dumps({"type": "response.created", "response": {"id": "r1"}}))
            await observer.event(json.dumps({"type": "response.output_text.delta", "delta": "ok"}))
            completed = json.dumps({"type": "response.completed", "response": {"id": "r1", "usage": {
                "input_tokens": 20, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 10}}}})
            await observer.event(completed)
            await observer.event(completed)
            await observer.close()
            monitoring.request_start.assert_called_once()
            finish.assert_awaited_once()
            self.assertEqual(finish.call_args.kwargs["content"], "ok")
            self.assertEqual(finish.call_args.kwargs["cached_tokens"], 10)
            self.assertTrue(finish.call_args.kwargs["success"])

    async def test_disconnect_records_unfinished_response_and_tolerates_unknown_events(self):
        from services.responses_websocket_monitor import ResponsesWebSocketMonitor
        with patch("services.responses_websocket_monitor.monitoring_service"), \
             patch("routes.responses_api._complete_responses_monitoring", new_callable=AsyncMock) as finish:
            observer = ResponsesWebSocketMonitor("alias", {}, MagicMock())
            await observer.event(json.dumps({"type": "future.event", "response": ["opaque"]}))
            await observer.event(json.dumps({"type": "response.created", "response": {"id": "r1"}}))
            await observer.close()
            self.assertFalse(finish.call_args.kwargs["success"])

    def test_chat_tool_extension_keeps_explicit_async(self):
        from converters.responses_bridge import _chat_tool_to_responses
        tool = _chat_tool_to_responses({"type": "function", "function": {
            "name": "lookup", "parameters": {"type": "object"}, "async": True}})
        self.assertTrue(tool["async"])


if __name__ == "__main__":
    unittest.main()


class CleanupCancellationTests(unittest.TestCase):
    """连接收尾期间 handler 被取消（TestClient / 服务关闭的 cancel scope）时的行为。"""

    def test_stop_tasks_keeps_callers_cancellation(self):
        # 自己取消子任务的同一轮里调用方也被取消：gather 会改抛子任务那份不带
        # 信息的 CancelledError，anyio 认不出是自己发起的取消而继续上抛
        async def run():
            loop = asyncio.get_running_loop()
            waiter = loop.create_future()

            async def forward():
                await waiter

            child = asyncio.create_task(forward())
            await asyncio.sleep(0)

            async def owner():
                loop.call_soon(asyncio.current_task().cancel, "cancel-scope")
                try:
                    await ws_route.stop_tasks([child])
                except asyncio.CancelledError as exc:
                    return exc.args
                return None

            self.assertEqual(await asyncio.create_task(owner()), ("cancel-scope",))
            self.assertTrue(child.cancelled())

        asyncio.run(run())

    def test_release_finishes_when_handler_is_cancelled(self):
        async def run():
            upstream = FakeUpstream()
            closing = asyncio.Event()
            finished = asyncio.Event()

            async def close_monitor():
                await closing.wait()
                finished.set()

            monitor = SimpleNamespace(close=AsyncMock(side_effect=close_monitor))

            async def handler():
                await asyncio.shield(ws_route.release(upstream, monitor))

            task = asyncio.create_task(handler())
            await asyncio.sleep(0)
            task.cancel("cancel-scope")
            with self.assertRaises(asyncio.CancelledError):
                await task
            # handler 已经因取消退出，统计收尾仍在独立任务里等待并最终完成
            self.assertTrue(upstream.closed)
            self.assertFalse(finished.is_set())
            closing.set()
            await asyncio.wait_for(finished.wait(), timeout=1)

        asyncio.run(run())
