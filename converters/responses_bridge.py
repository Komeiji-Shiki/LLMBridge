"""上游 OpenAI Responses API 与内部 Chat Completions 的双向桥接。"""
from __future__ import annotations

import codecs
import copy
import json
import re
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from fastapi.responses import StreamingResponse


_SSE_EVENT_SPLIT = re.compile(r"\r?\n\r?\n")
_STREAM_DONE = object()


class ResponsesBridgeError(ValueError):
    """Responses 上游响应或请求无法转换。"""


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _chat_content_to_responses(content: Any, role: str) -> List[Dict[str, Any]]:
    text_type = "output_text" if role == "assistant" else "input_text"
    if isinstance(content, str):
        return [{"type": text_type, "text": content}] if content else []
    if content is None:
        return []
    if not isinstance(content, list):
        return [{"type": text_type, "text": _stringify(content)}]

    result: List[Dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            result.append({"type": text_type, "text": part})
            continue
        if not isinstance(part, dict):
            continue
        part_type = part.get("type", "text")
        if part_type in ("text", "input_text", "output_text"):
            result.append({
                "type": text_type,
                "text": str(part.get("text", "")),
            })
        elif part_type in ("image_url", "input_image"):
            image = part.get("image_url") or part.get("url")
            if isinstance(image, dict):
                image_url = image.get("url")
                detail = image.get("detail")
            else:
                image_url = image
                detail = part.get("detail")
            if not isinstance(image_url, str) or not image_url:
                raise ResponsesBridgeError("Chat 图片消息缺少有效的 image_url。")
            converted = {"type": "input_image", "image_url": image_url}
            if detail is not None:
                converted["detail"] = detail
            result.append(converted)
        else:
            raise ResponsesBridgeError(f"不支持转换到 Responses 的内容类型: {part_type}")
    return result


def _chat_tool_to_responses(tool: Any) -> Dict[str, Any]:
    if not isinstance(tool, dict):
        raise ResponsesBridgeError("Chat tools 中的每项必须是对象。")
    function = tool.get("function")
    if not isinstance(function, dict):
        function = tool
    if not function.get("name"):
        raise ResponsesBridgeError("Chat function tool 缺少 name。")
    result = {
        "type": "function",
        "name": function["name"],
        "description": function.get("description", ""),
        "parameters": copy.deepcopy(function.get("parameters", {})),
    }
    if "strict" in function:
        result["strict"] = function["strict"]
    return result


def _chat_tool_choice_to_responses(choice: Any) -> Any:
    if isinstance(choice, str):
        return choice
    if not isinstance(choice, dict):
        return choice
    if choice.get("type") != "function":
        return choice
    function = choice.get("function") if isinstance(choice.get("function"), dict) else {}
    name = choice.get("name") or function.get("name")
    return {"type": "function", "name": name} if name else "auto"


def _chat_response_format_to_text(response_format: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(response_format, dict):
        return None
    format_type = response_format.get("type")
    if format_type == "json_object":
        return {"format": {"type": "json_object"}}
    if format_type == "json_schema":
        schema = response_format.get("json_schema")
        if isinstance(schema, dict):
            return {"format": {
                "type": "json_schema",
                **copy.deepcopy(schema),
            }}
    return None


def _function_call_to_responses(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
    call_id = tool_call.get("id") or "call_" + uuid.uuid4().hex[:24]
    arguments = function.get("arguments", "{}")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    return {
        "type": "function_call",
        "id": "fc_" + uuid.uuid4().hex[:24],
        "status": "completed",
        "call_id": call_id,
        "name": function.get("name", ""),
        "arguments": arguments,
    }


def convert_chat_request_to_responses(
    chat_request: Dict[str, Any],
    target_model_id: Optional[str] = None,
    endpoint_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """将内部 Chat Completions 请求转换为上游 Responses 请求。"""
    if not isinstance(chat_request, dict):
        raise ResponsesBridgeError("Chat 请求必须是 JSON 对象。")
    model = target_model_id or chat_request.get("model")
    if not isinstance(model, str) or not model:
        raise ResponsesBridgeError("Chat 请求缺少有效的 model。")

    endpoint_config = endpoint_config if isinstance(endpoint_config, dict) else {}
    instructions: List[str] = []
    input_items: List[Dict[str, Any]] = []

    messages = chat_request.get("messages")
    if not isinstance(messages, list):
        raise ResponsesBridgeError("Chat 请求缺少有效的 messages。")

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        content = message.get("content", "")

        if role in ("system", "developer"):
            text = _stringify(content)
            if text:
                instructions.append(text)
            continue

        if role == "tool":
            call_id = message.get("tool_call_id")
            if not call_id:
                raise ResponsesBridgeError("tool 消息缺少 tool_call_id。")
            input_items.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": _stringify(content),
            })
            continue

        if role not in ("user", "assistant"):
            raise ResponsesBridgeError(f"不支持转换到 Responses 的消息 role: {role}")

        content_parts = _chat_content_to_responses(content, role)
        if content_parts:
            input_items.append({
                "type": "message",
                "role": role,
                "content": content_parts,
            })

        if role == "assistant":
            for tool_call in message.get("tool_calls") or []:
                if isinstance(tool_call, dict):
                    input_items.append(_function_call_to_responses(tool_call))

    request: Dict[str, Any] = {
        "model": model,
        "input": input_items,
        "stream": bool(chat_request.get("stream", False)),
        "store": bool(endpoint_config.get("responses_store", False)),
    }
    if instructions:
        request["instructions"] = "\n\n".join(instructions)

    for key in ("temperature", "top_p", "user", "metadata"):
        if key in chat_request:
            request[key] = copy.deepcopy(chat_request[key])

    max_output_tokens = chat_request.get("max_completion_tokens")
    if max_output_tokens is None:
        max_output_tokens = chat_request.get("max_tokens")
    if max_output_tokens is not None:
        request["max_output_tokens"] = max_output_tokens

    reasoning_effort = chat_request.get("reasoning_effort") or endpoint_config.get("reasoning_effort")
    reasoning_summary = endpoint_config.get("responses_reasoning_summary")
    if reasoning_effort or reasoning_summary:
        reasoning: Dict[str, Any] = {}
        if reasoning_effort:
            reasoning["effort"] = reasoning_effort
        if reasoning_summary:
            reasoning["summary"] = reasoning_summary
        request["reasoning"] = reasoning

    verbosity = chat_request.get("verbosity") or endpoint_config.get("verbosity")
    if verbosity:
        request["text"] = {"verbosity": verbosity}

    response_text = _chat_response_format_to_text(chat_request.get("response_format"))
    if response_text:
        request.setdefault("text", {}).update(response_text)

    tools = chat_request.get("tools")
    if isinstance(tools, list):
        request["tools"] = [_chat_tool_to_responses(tool) for tool in tools]
    if "tool_choice" in chat_request:
        request["tool_choice"] = _chat_tool_choice_to_responses(chat_request["tool_choice"])
    if "parallel_tool_calls" in chat_request:
        request["parallel_tool_calls"] = chat_request["parallel_tool_calls"]

    custom_params = endpoint_config.get("custom_params")
    if isinstance(custom_params, dict):
        request.update(copy.deepcopy(custom_params))
    extra_body_params = endpoint_config.get("extra_body_params")
    if isinstance(extra_body_params, dict):
        request.update(copy.deepcopy(extra_body_params))
    request["model"] = model
    request["stream"] = bool(chat_request.get("stream", False))
    return request


def _extract_output_text(item: Dict[str, Any]) -> str:
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        str(part.get("text", ""))
        for part in content
        if isinstance(part, dict) and part.get("type") in ("output_text", "text")
    )


def _extract_reasoning_text(item: Dict[str, Any]) -> str:
    summary = item.get("summary")
    if not isinstance(summary, list):
        return ""
    return "".join(
        str(part.get("text", ""))
        for part in summary
        if isinstance(part, dict) and part.get("type") in ("summary_text", "text")
    )


def _usage_to_chat(usage: Any) -> Dict[str, Any]:
    if not isinstance(usage, dict):
        return {}
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens) or 0)
    result = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    input_details = usage.get("input_tokens_details")
    if isinstance(input_details, dict) and input_details.get("cached_tokens") is not None:
        result["prompt_tokens_details"] = {
            "cached_tokens": int(input_details.get("cached_tokens", 0) or 0)
        }
    output_details = usage.get("output_tokens_details")
    if isinstance(output_details, dict) and output_details.get("reasoning_tokens") is not None:
        result["completion_tokens_details"] = {
            "reasoning_tokens": int(output_details.get("reasoning_tokens", 0) or 0)
        }
        result["reasoning_tokens"] = int(output_details.get("reasoning_tokens", 0) or 0)
    return result


def convert_responses_response_to_chat(
    responses_response: Dict[str, Any],
    request_model: str,
) -> Dict[str, Any]:
    """将完整的上游 Responses 响应转换为 Chat Completions JSON。"""
    if not isinstance(responses_response, dict):
        raise ResponsesBridgeError("Responses 上游返回的响应不是 JSON 对象。")
    if responses_response.get("error") is not None:
        return responses_response

    output = responses_response.get("output")
    if not isinstance(output, list):
        output = []
    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []

    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            text_parts.append(_extract_output_text(item))
        elif item_type == "reasoning":
            reasoning_parts.append(_extract_reasoning_text(item))
        elif item_type == "function_call":
            arguments = item.get("arguments", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            tool_calls.append({
                "id": item.get("call_id") or item.get("id") or "call_" + uuid.uuid4().hex[:24],
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": arguments,
                },
            })

    content = "".join(text_parts)
    if not content and isinstance(responses_response.get("output_text"), str):
        content = responses_response["output_text"]
    message: Dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls

    status = responses_response.get("status")
    incomplete_details = responses_response.get("incomplete_details")
    incomplete_reason = incomplete_details.get("reason") if isinstance(incomplete_details, dict) else None
    if tool_calls:
        finish_reason = "tool_calls"
    elif status == "incomplete" or incomplete_details:
        finish_reason = "content_filter" if incomplete_reason == "content_filter" else "length"
    else:
        finish_reason = "stop"

    response_id = responses_response.get("id") or uuid.uuid4().hex
    if response_id.startswith("resp_"):
        response_id = "chatcmpl-" + response_id[len("resp_"):]
    elif not response_id.startswith("chatcmpl-"):
        response_id = "chatcmpl-" + response_id

    return {
        "id": response_id,
        "object": "chat.completion",
        "created": int(responses_response.get("created_at", time.time()) or time.time()),
        "model": responses_response.get("model") or request_model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": _usage_to_chat(responses_response.get("usage")),
    }


async def _iter_source_chunks(source: Any) -> AsyncIterator[bytes]:
    body = getattr(source, "body", None)
    iterator = getattr(source, "body_iterator", None)
    if body is not None and iterator is None:
        yield body if isinstance(body, bytes) else str(body).encode("utf-8")
        return
    if iterator is not None:
        async for chunk in iterator:
            yield chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
        return
    async for chunk in source:
        yield chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")


async def iter_responses_sse_events(source: Any) -> AsyncIterator[Any]:
    """解析 Responses SSE，事件中附加 `_event_type` 便于转换。"""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    saw_sse = False

    async for raw_chunk in _iter_source_chunks(source):
        buffer += decoder.decode(raw_chunk, final=False)
        while True:
            match = _SSE_EVENT_SPLIT.search(buffer)
            if not match:
                break
            block, buffer = buffer[:match.start()], buffer[match.end():]
            event_type = ""
            data_lines = []
            for line in block.splitlines():
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if not data_lines:
                bare_payload = block.strip()
                if bare_payload:
                    try:
                        bare_data = json.loads(bare_payload)
                        if isinstance(bare_data, dict):
                            yield bare_data
                    except json.JSONDecodeError:
                        pass
                continue
            saw_sse = True
            payload = "\n".join(data_lines).strip()
            if payload == "[DONE]":
                yield _STREAM_DONE
                return
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                if event_type:
                    data["_event_type"] = event_type
                yield data

    buffer += decoder.decode(b"", final=True)
    if buffer.strip():
        if saw_sse:
            data_lines = [line[5:].lstrip() for line in buffer.splitlines() if line.startswith("data:")]
            payload = "\n".join(data_lines).strip()
        else:
            payload = buffer.strip()
        if payload == "[DONE]":
            yield _STREAM_DONE
        elif payload:
            try:
                data = json.loads(payload)
                if isinstance(data, dict):
                    yield data
            except json.JSONDecodeError:
                pass


class _ChatStreamBuilder:
    def __init__(self, request_model: str):
        self.request_model = request_model
        self.response_id = "chatcmpl-" + uuid.uuid4().hex
        self.model = request_model
        self.created = int(time.time())
        self.finish_reason = "stop"
        self.usage: Dict[str, Any] = {}
        self.tool_indexes: Dict[str, int] = {}
        self.tool_ids: Dict[str, str] = {}
        self.tool_names: Dict[str, str] = {}
        self.tool_arguments: Dict[str, str] = {}
        self.next_tool_index = 0
        self.failed = False

    def chunk(self, delta: Dict[str, Any], finish_reason: Optional[str] = None) -> bytes:
        payload = {
            "id": self.response_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    def usage_chunk(self) -> bytes:
        payload = {
            "id": self.response_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [],
            "usage": self.usage,
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    def _tool_index(self, item: Dict[str, Any]) -> Tuple[str, int]:
        key = str(
            item.get("id")
            or item.get("item_id")
            or item.get("call_id")
            or item.get("output_index", self.next_tool_index)
        )
        if key not in self.tool_indexes:
            self.tool_indexes[key] = self.next_tool_index
            self.next_tool_index += 1
        return key, self.tool_indexes[key]

    def process(self, event: Dict[str, Any]) -> List[bytes]:
        if not isinstance(event, dict):
            return []
        event_type = event.get("_event_type") or event.get("type", "")
        if not event_type and event.get("error") not in (None, "", {}):
            self.failed = True
            error = event["error"] if isinstance(event["error"], dict) else {
                "message": str(event["error"]),
                "type": "api_error",
            }
            return [f"data: {json.dumps({'error': error}, ensure_ascii=False)}\n\n".encode("utf-8")]

        if not event_type and (
            event.get("object") == "response" or isinstance(event.get("output"), list)
        ):
            chat_response = convert_responses_response_to_chat(event, self.request_model)
            choice = (chat_response.get("choices") or [{}])[0]
            message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            self.usage = chat_response.get("usage") if isinstance(chat_response.get("usage"), dict) else {}
            self.finish_reason = choice.get("finish_reason") or "stop"
            chunks = []
            reasoning = message.get("reasoning_content") or message.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                chunks.append(self.chunk({"reasoning_content": reasoning}))
            content = message.get("content")
            if isinstance(content, str) and content:
                chunks.append(self.chunk({"content": content}))
            for position, tool_call in enumerate(message.get("tool_calls") or []):
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
                key, index = self._tool_index({
                    "id": tool_call.get("id"),
                    "output_index": position,
                })
                self.tool_ids[key] = tool_call.get("id") or "call_" + uuid.uuid4().hex[:24]
                self.tool_names[key] = function.get("name", "")
                self.tool_arguments[key] = function.get("arguments", "")
                chunks.append(self.chunk({
                    "tool_calls": [{
                        "index": index,
                        "id": self.tool_ids[key],
                        "type": "function",
                        "function": {
                            "name": self.tool_names[key],
                            "arguments": self.tool_arguments[key],
                        },
                    }],
                }))
            return chunks

        if event_type in ("response.created", "response.in_progress"):
            return []

        if event_type == "response.output_item.added":
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if item.get("type") != "function_call":
                return []
            key, index = self._tool_index({**item, "output_index": event.get("output_index")})
            self.tool_ids[key] = item.get("call_id") or item.get("id") or "call_" + uuid.uuid4().hex[:24]
            self.tool_names[key] = item.get("name", "")
            self.tool_arguments.setdefault(key, "")
            return [self.chunk({
                "tool_calls": [{
                    "index": index,
                    "id": self.tool_ids[key],
                    "type": "function",
                    "function": {"name": self.tool_names[key], "arguments": ""},
                }],
            })]

        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            return [self.chunk({"content": delta})] if isinstance(delta, str) and delta else []

        if event_type == "response.reasoning_summary_text.delta":
            delta = event.get("delta")
            return [self.chunk({"reasoning_content": delta})] if isinstance(delta, str) and delta else []

        if event_type == "response.function_call_arguments.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                return []
            key, index = self._tool_index(event)
            self.tool_arguments[key] = self.tool_arguments.get(key, "") + delta
            return [self.chunk({
                "tool_calls": [{
                    "index": index,
                    "function": {"arguments": delta},
                }],
            })]

        if event_type == "response.output_item.done":
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if item.get("type") != "function_call":
                return []
            key, index = self._tool_index({**item, "output_index": event.get("output_index")})
            call_id = item.get("call_id") or item.get("id")
            if call_id:
                self.tool_ids[key] = call_id
            if item.get("name"):
                self.tool_names[key] = item["name"]
            arguments = item.get("arguments")
            if isinstance(arguments, str) and not self.tool_arguments.get(key):
                self.tool_arguments[key] = arguments
                return [self.chunk({
                    "tool_calls": [{
                        "index": index,
                        "function": {"arguments": arguments},
                    }],
                })]
            return []

        if event_type in ("response.completed", "response.incomplete"):
            response = event.get("response") if isinstance(event.get("response"), dict) else {}
            self.usage = _usage_to_chat(response.get("usage"))
            if event_type == "response.incomplete":
                details = response.get("incomplete_details")
                reason = details.get("reason") if isinstance(details, dict) else None
                self.finish_reason = "content_filter" if reason == "content_filter" else "length"
            else:
                self.finish_reason = "tool_calls" if self.tool_indexes else "stop"
            return []

        if event_type in ("response.failed", "error"):
            self.failed = True
            error = event.get("error")
            if error is None and isinstance(event.get("response"), dict):
                error = event["response"].get("error")
            error = error if isinstance(error, dict) else {"message": str(error or "上游 Responses 请求失败"), "type": "api_error"}
            payload = {"error": error}
            return [f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")]
        return []

    @staticmethod
    def _response_id(response_id: Any) -> str:
        if isinstance(response_id, str) and response_id:
            if response_id.startswith("chatcmpl-"):
                return response_id
            if response_id.startswith("resp_"):
                return "chatcmpl-" + response_id[len("resp_"):]
            return "chatcmpl-" + response_id
        return "chatcmpl-" + uuid.uuid4().hex

    def final_chunks(self) -> List[bytes]:
        chunks: List[bytes] = []
        if self.failed:
            chunks.append(b"data: [DONE]\n\n")
            return chunks
        chunks.append(self.chunk({}, finish_reason=self.finish_reason))
        if self.usage:
            chunks.append(self.usage_chunk())
        chunks.append(b"data: [DONE]\n\n")
        return chunks


def build_chat_streaming_response_from_responses(
    source: Any,
    request_model: str,
) -> StreamingResponse:
    """将上游 Responses SSE 转成 OpenAI Chat Completions SSE。"""
    async def generator():
        builder = _ChatStreamBuilder(request_model)
        yield builder.chunk({"role": "assistant"})
        async for event in iter_responses_sse_events(source):
            if event is _STREAM_DONE:
                break
            for chunk in builder.process(event):
                yield chunk
        for chunk in builder.final_chunks():
            yield chunk

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked",
        },
    )


async def collect_responses_stream_to_chat(source: Any, request_model: str) -> Dict[str, Any]:
    """收集上游 Responses SSE，用于 force_stream 导致的非流式客户端。"""
    chunks: List[Dict[str, Any]] = []
    async for event in iter_responses_sse_events(source):
        if event is _STREAM_DONE:
            break
        if isinstance(event, dict):
            chunks.append(event)

    builder = _ChatStreamBuilder(request_model)
    for event in chunks:
        builder.process(event)
    message: Dict[str, Any] = {"role": "assistant", "content": ""}
    text_parts = []
    reasoning_parts = []
    tool_calls = []
    for event in chunks:
        event_type = event.get("_event_type") or event.get("type", "")
        if event_type == "response.output_text.delta" and isinstance(event.get("delta"), str):
            text_parts.append(event["delta"])
        elif event_type == "response.reasoning_summary_text.delta" and isinstance(event.get("delta"), str):
            reasoning_parts.append(event["delta"])
    message["content"] = "".join(text_parts)
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    for key, index in sorted(builder.tool_indexes.items(), key=lambda pair: pair[1]):
        tool_calls.append({
            "id": builder.tool_ids.get(key) or "call_" + uuid.uuid4().hex[:24],
            "type": "function",
            "function": {
                "name": builder.tool_names.get(key, ""),
                "arguments": builder.tool_arguments.get(key, ""),
            },
        })
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": builder.response_id,
        "object": "chat.completion",
        "created": builder.created,
        "model": builder.model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": builder.finish_reason,
        }],
        "usage": builder.usage,
    }


__all__ = [
    "ResponsesBridgeError",
    "convert_chat_request_to_responses",
    "convert_responses_response_to_chat",
    "build_chat_streaming_response_from_responses",
    "collect_responses_stream_to_chat",
    "iter_responses_sse_events",
]
