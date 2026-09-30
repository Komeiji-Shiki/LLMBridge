"""复用 HTTP Responses 的用量统计，每个 WebSocket 响应单独完成一次记录。"""
import copy
import json
import uuid

from modules.monitoring import monitoring_service


class ResponsesWebSocketMonitor:
    def __init__(self, alias, config, service):
        self.alias, self.config, self.service = alias, config, service
        self.requests = {}
        self.lanes = {}
        self.active = {}

    def request(self, event):
        if event.get("type") == "response.create":
            lane = event.get("stream_id") if isinstance(event.get("stream_id"), str) else ""
            self.requests[lane] = copy.deepcopy(event)

    async def event(self, raw):
        from routes.responses_api import _apply_responses_sse_event, _responses_input_to_messages
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeError):
            return
        if not isinstance(event, dict):
            return
        response = event.get("response")
        response = response if isinstance(response, dict) else {}
        lane = event.get("stream_id") if isinstance(event.get("stream_id"), str) else ""
        response_id = response.get("id") or event.get("response_id") or self.lanes.get(lane)
        if not isinstance(response_id, str):
            return
        if event.get("type") == "response.created" and response_id:
            if response_id in self.active:
                return
            self.lanes[lane] = response_id
            messages = _responses_input_to_messages(self.requests.get(lane, {}))
            stats = {"content_parts": [], "reasoning_parts": [], "input_tokens": 0,
                     "output_tokens": 0, "cached_tokens": 0, "upstream_usage": None,
                     "error_message": None, "stream_complete": False, "saw_completed": False}
            request_id = uuid.uuid4().hex
            self.active[response_id] = (request_id, messages, stats)
            monitoring_service.request_start(request_id=request_id, model=self.config.get("display_name") or self.alias,
                                             messages_count=len(messages), mode="responses_websocket", messages=messages,
                                             params={"response_id": response_id, "transport": "websocket"})
        entry = self.active.get(response_id)
        if entry is None:
            return
        _apply_responses_sse_event(event, entry[2])
        if event.get("type") in ("response.completed", "response.incomplete", "response.failed", "error"):
            await self.finish(response_id)

    async def finish(self, response_id, error=None):
        from modules.token_counter import estimate_message_tokens, estimate_tokens
        from routes.responses_api import _complete_responses_monitoring
        entry = self.active.pop(response_id, None)
        if entry is None:
            return
        request_id, messages, stats = entry
        error = stats["error_message"] or error
        await _complete_responses_monitoring(
            monitoring_service=monitoring_service, direct_api_service=self.service,
            request_id=request_id, success=stats["saw_completed"] and not error,
            display_name=self.config.get("display_name") or self.alias,
            pricing_config=self.config.get("pricing", {}),
            estimate_message_tokens_func=estimate_message_tokens, estimate_tokens_func=estimate_tokens,
            full_messages=messages, content="".join(stats["content_parts"]),
            reasoning="".join(stats["reasoning_parts"]), input_tokens=stats["input_tokens"],
            output_tokens=stats["output_tokens"], cached_tokens=stats["cached_tokens"],
            upstream_usage=stats["upstream_usage"], error=error,
        )

    async def close(self):
        for response_id in list(self.active):
            await self.finish(response_id, "WebSocket 在响应完成前关闭。")
