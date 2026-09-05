"""
OpenAI Responses API ↔ 内部 Chat Completions 协议转换。

项目内部的执行链路统一使用 Chat Completions 格式，因此本模块只负责：
- 将 Responses 请求转换为内部 messages 请求；
- 将内部 chat.completion 转换为 Responses response；
- 将内部 Chat Completions SSE 转换为 Responses SSE 事件。

第一阶段明确保持无状态：previous_response_id、store=true、background 和
conversation 不在这里伪装支持，调用方应由路由转换为 400 错误。
"""
from __future__ import annotations

import codecs
import copy
import json
import re
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from utils.usage_tokens import total_output_tokens

from fastapi.responses import JSONResponse, StreamingResponse, Response


_SSE_EVENT_SPLIT = re.compile(r"\r?\n\r?\n")
_STREAM_DONE = object()


class ResponsesRequestError(ValueError):
    """Responses 请求无法转换为内部 Chat Completions 请求。"""


def _unsupported(field: str) -> None:
    raise ResponsesRequestError(
        f"Responses API 字段 '{field}' 当前需要有状态支持，第一阶段暂不支持。"
    )


def _stringify_output(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _convert_input_content_part(part: Any) -> Optional[Dict[str, Any]]:
    if isinstance(part, str):
        return {"type": "text", "text": part}
    if not isinstance(part, dict):
        return None

    part_type = part.get("type", "input_text")
    if part_type in ("input_text", "text", "output_text"):
        return {"type": "text", "text": str(part.get("text", ""))}

    if part_type in ("input_image", "image_url"):
        image_url = part.get("image_url") or part.get("url")
        if isinstance(image_url, dict):
            url = image_url.get("url")
            detail = image_url.get("detail")
        else:
            url = image_url
            detail = part.get("detail")
        if not isinstance(url, str) or not url:
            if part.get("file_id"):
                raise ResponsesRequestError("input_image.file_id 当前暂不支持，请使用 image_url。")
            raise ResponsesRequestError("input_image 缺少有效的 image_url。")
        converted = {"url": url}
        if detail is not None:
            converted["detail"] = detail
        return {"type": "image_url", "image_url": converted}

    if part_type in ("input_file", "file", "file_search"):
        raise ResponsesRequestError("input_file 和文件工具当前暂不支持。")

    raise ResponsesRequestError(f"不支持的 Responses 输入内容类型: {part_type}")


def _convert_message_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if not isinstance(content, list):
        return _stringify_output(content)

    parts = []
    for part in content:
        converted = _convert_input_content_part(part)
        if converted is not None:
            parts.append(converted)
    if not parts:
        return ""
    if len(parts) == 1 and parts[0].get("type") == "text":
        return parts[0].get("text", "")
    return parts


def _convert_message_item(item: Dict[str, Any]) -> Dict[str, Any]:
    role = item.get("role", "user")
    if role == "developer":
        role = "system"
    if role not in ("system", "user", "assistant"):
        raise ResponsesRequestError(f"消息使用了不支持的 role: {role}")

    message: Dict[str, Any] = {
        "role": role,
        "content": _convert_message_content(item.get("content", "")),
    }
    if item.get("name"):
        message["name"] = item["name"]
    return message


def _convert_function_call_item(item: Dict[str, Any]) -> Dict[str, Any]:
    name = item.get("name")
    call_id = item.get("call_id") or item.get("id")
    if not name or not call_id:
        raise ResponsesRequestError("function_call 必须包含 name 和 call_id。")
    arguments = item.get("arguments", "{}")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }],
    }


def _convert_function_call_output_item(item: Dict[str, Any]) -> Dict[str, Any]:
    call_id = item.get("call_id")
    if not call_id:
        raise ResponsesRequestError("function_call_output 必须包含 call_id。")
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": _stringify_output(item.get("output", "")),
    }


def _convert_input_item(item: Any) -> Dict[str, Any]:
    if isinstance(item, str):
        return {"role": "user", "content": item}
    if not isinstance(item, dict):
        raise ResponsesRequestError("input 数组中的每一项必须是对象。")

    item_type = item.get("type")
    if item_type == "function_call":
        return _convert_function_call_item(item)
    if item_type == "function_call_output":
        return _convert_function_call_output_item(item)
    if item_type in ("reasoning", "computer_call", "item_reference"):
        raise ResponsesRequestError(f"输入项类型 '{item_type}' 当前暂不支持。")
    if item_type in ("input_text", "input_image"):
        return {
            "role": "user",
            "content": _convert_message_content([item]),
        }
    return _convert_message_item(item)


def _convert_tool(tool: Any) -> Dict[str, Any]:
    if not isinstance(tool, dict):
        raise ResponsesRequestError("tools 中的每一项必须是对象。")
    if tool.get("type") != "function":
        raise ResponsesRequestError(
            f"Responses 内置工具 '{tool.get('type', 'unknown')}' 当前暂不支持。"
        )

    function = tool.get("function")
    if isinstance(function, dict):
        converted_function = dict(function)
    else:
        converted_function = {
            key: tool.get(key)
            for key in ("name", "description", "parameters", "strict")
            if key in tool
        }
    if not converted_function.get("name"):
        raise ResponsesRequestError("function tool 缺少 name。")
    converted_function.setdefault("parameters", {})
    return {"type": "function", "function": converted_function}


def _convert_tool_choice(tool_choice: Any) -> Any:
    if isinstance(tool_choice, str):
        if tool_choice not in ("auto", "none", "required"):
            raise ResponsesRequestError(f"不支持的 tool_choice: {tool_choice}")
        return tool_choice
    if not isinstance(tool_choice, dict):
        raise ResponsesRequestError("tool_choice 必须是字符串或对象。")

    choice_type = tool_choice.get("type")
    if choice_type != "function":
        raise ResponsesRequestError(
            f"Responses tool_choice 类型 '{choice_type}' 当前暂不支持。"
        )
    name = tool_choice.get("name")
    if not name and isinstance(tool_choice.get("function"), dict):
        name = tool_choice["function"].get("name")
    if not name:
        raise ResponsesRequestError("function tool_choice 缺少 name。")
    return {"type": "function", "function": {"name": name}}


def _convert_text_format(text_config: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(text_config, dict):
        return None
    output_format = text_config.get("format")
    if not isinstance(output_format, dict):
        return None
    format_type = output_format.get("type")
    if format_type == "json_object":
        return {"type": "json_object"}
    if format_type == "json_schema":
        schema = {
            key: output_format[key]
            for key in ("name", "description", "schema", "strict")
            if key in output_format
        }
        return {"type": "json_schema", "json_schema": schema}
    if format_type in (None, "text"):
        return None
    raise ResponsesRequestError(f"不支持的 text.format.type: {format_type}")


def convert_responses_to_chat_request(responses_request: Dict[str, Any]) -> Dict[str, Any]:
    """将 Responses API 请求转换为内部 Chat Completions 请求。"""
    if not isinstance(responses_request, dict):
        raise ResponsesRequestError("请求体必须是 JSON 对象。")

    model = responses_request.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ResponsesRequestError("Responses 请求缺少有效的 model。")

    if responses_request.get("previous_response_id"):
        _unsupported("previous_response_id")
    if responses_request.get("conversation"):
        _unsupported("conversation")
    if responses_request.get("store") is True:
        _unsupported("store=true")
    if responses_request.get("background") is True:
        _unsupported("background=true")

    input_value = responses_request.get("input")
    if isinstance(input_value, str):
        messages = [{"role": "user", "content": input_value}]
    elif isinstance(input_value, list):
        messages = [_convert_input_item(item) for item in input_value]
    elif input_value is None:
        raise ResponsesRequestError("Responses 请求缺少 input。")
    else:
        raise ResponsesRequestError("input 必须是字符串或数组。")

    instructions = responses_request.get("instructions")
    if instructions is not None:
        if isinstance(instructions, str):
            instruction_text = instructions
        elif isinstance(instructions, list):
            instruction_text = _convert_message_content(instructions)
        else:
            raise ResponsesRequestError("instructions 必须是字符串或内容数组。")
        if instruction_text:
            messages.insert(0, {"role": "system", "content": instruction_text})

    if not messages:
        raise ResponsesRequestError("input 不能为空。")

    chat_request: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": bool(responses_request.get("stream", False)),
    }

    for key in ("temperature", "top_p", "stop", "user", "metadata", "parallel_tool_calls"):
        if key in responses_request:
            chat_request[key] = copy.deepcopy(responses_request[key])

    if "max_output_tokens" in responses_request:
        chat_request["max_completion_tokens"] = responses_request["max_output_tokens"]

    reasoning = responses_request.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort") is not None:
        chat_request["reasoning_effort"] = reasoning["effort"]

    if "verbosity" in responses_request:
        chat_request["verbosity"] = responses_request["verbosity"]
    elif isinstance(responses_request.get("text"), dict) and "verbosity" in responses_request["text"]:
        chat_request["verbosity"] = responses_request["text"]["verbosity"]

    text_format = _convert_text_format(responses_request.get("text"))
    if text_format is not None:
        chat_request["response_format"] = text_format

    tools = responses_request.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            raise ResponsesRequestError("tools 必须是数组。")
        chat_request["tools"] = [_convert_tool(tool) for tool in tools]

    if "tool_choice" in responses_request:
        chat_request["tool_choice"] = _convert_tool_choice(responses_request["tool_choice"])

    return chat_request


def _response_id(source_id: Optional[str] = None) -> str:
    if isinstance(source_id, str) and source_id:
        if source_id.startswith("resp_"):
            return source_id
        if source_id.startswith("chatcmpl-"):
            return "resp_" + source_id[len("chatcmpl-"):]
    return "resp_" + uuid.uuid4().hex


def _message_text(message: Dict[str, Any]) -> str:
    content = message.get("content", "") if isinstance(message, dict) else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in ("text", "output_text")
        )
    return ""


def _message_reasoning(message: Dict[str, Any]) -> str:
    if not isinstance(message, dict):
        return ""
    reasoning = message.get("reasoning_content") or message.get("reasoning")
    return reasoning if isinstance(reasoning, str) else ""


def _message_tool_calls(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(message, dict):
        return []
    raw = message.get("tool_calls") or []
    if isinstance(raw, dict):
        raw = [raw]
    return [call for call in raw if isinstance(call, dict)] if isinstance(raw, list) else []


def _usage_to_responses(usage: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(usage, dict):
        return None
    input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
    # Responses 的 output_tokens 官方语义包含思考量：内部 usage 即使按 separate
    # 口径只记了正文，这里也必须还原成正文 + 思考
    output_tokens = total_output_tokens(usage)
    total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens) or 0)
    result: Dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens") is not None:
        result["input_tokens_details"] = {
            "cached_tokens": int(prompt_details.get("cached_tokens", 0) or 0)
        }
    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, dict) and completion_details.get("reasoning_tokens") is not None:
        result["output_tokens_details"] = {
            "reasoning_tokens": int(completion_details.get("reasoning_tokens", 0) or 0)
        }
    return result


def _responses_request_fields(request: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    request = request if isinstance(request, dict) else {}
    result: Dict[str, Any] = {
        "previous_response_id": None,
        "store": False,
        "metadata": copy.deepcopy(request.get("metadata", {}))
        if isinstance(request.get("metadata", {}), dict) else {},
    }
    if "temperature" in request:
        result["temperature"] = request["temperature"]
    if "top_p" in request:
        result["top_p"] = request["top_p"]
    if "max_output_tokens" in request:
        result["max_output_tokens"] = request["max_output_tokens"]
    if isinstance(request.get("reasoning"), dict):
        result["reasoning"] = copy.deepcopy(request["reasoning"])
    if isinstance(request.get("text"), dict):
        result["text"] = copy.deepcopy(request["text"])
    else:
        result["text"] = {"format": {"type": "text"}}
    if "tool_choice" in request:
        result["tool_choice"] = copy.deepcopy(request["tool_choice"])
    else:
        result["tool_choice"] = "auto"
    if isinstance(request.get("tools"), list):
        result["tools"] = copy.deepcopy(request["tools"])
    else:
        result["tools"] = []
    return result


def _build_response_object(
    response_id: str,
    model: str,
    request: Optional[Dict[str, Any]],
    output: List[Dict[str, Any]],
    status: str,
    usage: Any = None,
    incomplete_details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "completed_at": int(time.time()) if status in ("completed", "incomplete", "failed") else None,
        "error": None,
        "incomplete_details": incomplete_details,
        "model": model,
        "output": copy.deepcopy(output),
    }
    result.update(_responses_request_fields(request))
    result["usage"] = _usage_to_responses(usage)
    return result


def _convert_tool_call_to_output_item(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    function = tool_call.get("function", {})
    if not isinstance(function, dict):
        function = {}
    call_id = tool_call.get("id") or "call_" + uuid.uuid4().hex[:24]
    arguments = function.get("arguments", "{}")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    return {
        "id": "fc_" + uuid.uuid4().hex[:24],
        "type": "function_call",
        "status": "completed",
        "call_id": call_id,
        "name": function.get("name", ""),
        "arguments": arguments,
    }


def convert_chat_response_to_responses(
    chat_response: Dict[str, Any],
    request: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """将完整的 Chat Completions JSON 转换为 Responses JSON。"""
    if not isinstance(chat_response, dict):
        raise ResponsesRequestError("上游返回的响应不是 JSON 对象。")
    if chat_response.get("error") is not None:
        return chat_response

    choices = chat_response.get("choices") or []
    first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = first_choice.get("message", {}) if isinstance(first_choice, dict) else {}
    if not isinstance(message, dict):
        message = {}

    output: List[Dict[str, Any]] = []
    reasoning = _message_reasoning(message)
    if reasoning:
        output.append({
            "id": "rs_" + uuid.uuid4().hex[:24],
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": reasoning}],
        })

    text = _message_text(message)
    tool_calls = _message_tool_calls(message)
    if text or not tool_calls:
        output.append({
            "id": "msg_" + uuid.uuid4().hex[:24],
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{
                "type": "output_text",
                "text": text,
                "annotations": [],
            }],
        })
    output.extend(_convert_tool_call_to_output_item(call) for call in tool_calls)

    finish_reason = first_choice.get("finish_reason", "stop")
    incomplete_reason = {"length": "max_output_tokens", "content_filter": "content_filter"}.get(finish_reason)
    status = "incomplete" if incomplete_reason else "completed"
    incomplete_details = {"reason": incomplete_reason} if incomplete_reason else None
    response_id = _response_id(chat_response.get("id"))
    model = chat_response.get("model") or (request or {}).get("model") or "unknown"
    return _build_response_object(
        response_id=response_id,
        model=model,
        request=request,
        output=output,
        status=status,
        usage=chat_response.get("usage"),
        incomplete_details=incomplete_details,
    )


async def read_response_body_bytes(response: Response) -> bytes:
    body = getattr(response, "body", None)
    if body is not None:
        return body if isinstance(body, bytes) else bytes(body)
    iterator = getattr(response, "body_iterator", None)
    if iterator is None:
        return b""
    chunks = []
    async for chunk in iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8"))
    return b"".join(chunks)


async def _iter_response_chunks(response: Response) -> AsyncIterator[bytes]:
    body = getattr(response, "body", None)
    iterator = getattr(response, "body_iterator", None)
    if body is not None and iterator is None:
        yield body if isinstance(body, bytes) else bytes(body)
        return
    if iterator is not None:
        async for chunk in iterator:
            yield chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")


async def iter_chat_sse_payloads(response: Response) -> AsyncIterator[Any]:
    """读取 Chat SSE 或单个 JSON 响应，逐个返回 JSON 负载。"""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    saw_sse = False

    async for raw_chunk in _iter_response_chunks(response):
        buffer += decoder.decode(raw_chunk, final=False)
        while True:
            match = _SSE_EVENT_SPLIT.search(buffer)
            if not match:
                break
            block, buffer = buffer[:match.start()], buffer[match.end():]
            data_lines = [line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")]
            if not data_lines:
                continue
            saw_sse = True
            payload = "\n".join(data_lines).strip()
            if payload == "[DONE]":
                yield _STREAM_DONE
                return
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue

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
                yield json.loads(payload)
            except json.JSONDecodeError:
                pass


async def collect_chat_stream_response(response: Response, model: str) -> Dict[str, Any]:
    """把流式 Chat 响应收集为完整 chat.completion，用于 force_stream 反向转换。"""
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: Dict[str, Dict[str, Any]] = {}
    usage: Optional[Dict[str, Any]] = None
    finish_reason = "stop"
    response_id = None

    async for payload in iter_chat_sse_payloads(response):
        if payload is _STREAM_DONE:
            break
        if not isinstance(payload, dict):
            continue
        if payload.get("error") is not None:
            return payload
        response_id = response_id or payload.get("id")
        if isinstance(payload.get("usage"), dict):
            usage = payload["usage"]
        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            continue
        choice = choices[0]
        finish_reason = choice.get("finish_reason") or finish_reason
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            delta = choice.get("message", {})
        if not isinstance(delta, dict):
            continue
        text = delta.get("content")
        if isinstance(text, str):
            content_parts.append(text)
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str):
            reasoning_parts.append(reasoning)
        raw_tool_calls = delta.get("tool_calls") or []
        if isinstance(raw_tool_calls, dict):
            raw_tool_calls = [raw_tool_calls]
        if isinstance(raw_tool_calls, list):
            for position, call in enumerate(raw_tool_calls):
                if not isinstance(call, dict):
                    continue
                index = str(call.get("index", position))
                current = tool_calls.setdefault(index, {"index": call.get("index", position), "type": "function", "function": {}})
                if call.get("id"):
                    current["id"] = call["id"]
                function = call.get("function")
                if isinstance(function, dict):
                    if function.get("name"):
                        current["function"]["name"] = function["name"]
                    if function.get("arguments") is not None:
                        current["function"]["arguments"] = current["function"].get("arguments", "") + str(function["arguments"])

    ordered_tools = [tool_calls[key] for key in sorted(tool_calls, key=lambda value: int(value) if value.isdigit() else value)]
    message: Dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if ordered_tools:
        message["tool_calls"] = ordered_tools
    return {
        "id": response_id or "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage or {},
    }


def _format_response_event(event_type: str, payload: Dict[str, Any], sequence: int) -> bytes:
    event_payload = dict(payload)
    event_payload.setdefault("type", event_type)
    event_payload.setdefault("sequence_number", sequence)
    return (
        f"event: {event_type}\n"
        f"data: {json.dumps(event_payload, ensure_ascii=False)}\n\n"
    ).encode("utf-8")


class _ResponsesStreamBuilder:
    """将 chat.completion.chunk 增量转换为 Responses 生命周期事件。"""

    def __init__(self, request: Dict[str, Any], model: str):
        self.request = request
        self.model = model
        self.response_id = "resp_" + uuid.uuid4().hex
        self.output: List[Dict[str, Any]] = []
        self.sequence = 0
        self.finish_reason = "stop"
        self.usage: Optional[Dict[str, Any]] = None
        self.active_text: Optional[int] = None
        self.active_reasoning: Optional[int] = None
        self.active_tools: Dict[str, int] = {}
        self._text_content: Dict[int, str] = {}
        self._reasoning_content: Dict[int, str] = {}
        self._tool_arguments: Dict[int, str] = {}

    def event(self, event_type: str, payload: Dict[str, Any]) -> bytes:
        self.sequence += 1
        return _format_response_event(event_type, payload, self.sequence)

    def initial_events(self) -> List[bytes]:
        response = _build_response_object(
            self.response_id, self.model, self.request, [], "in_progress"
        )
        return [
            self.event("response.created", {"response": response}),
            self.event("response.in_progress", {"response": response}),
        ]

    def _start_reasoning(self) -> List[bytes]:
        if self.active_reasoning is not None:
            return []
        events = self._close_text() + self._close_tools()
        index = len(self.output)
        item = {
            "id": "rs_" + uuid.uuid4().hex[:24],
            "type": "reasoning",
            "status": "in_progress",
            "summary": [],
        }
        self.output.append(item)
        self.active_reasoning = index
        self._reasoning_content[index] = ""
        events.append(self.event("response.output_item.added", {"output_index": index, "item": copy.deepcopy(item)}))
        return events

    def _start_text(self) -> List[bytes]:
        if self.active_text is not None:
            return []
        events = self._close_reasoning() + self._close_tools()
        index = len(self.output)
        item = {
            "id": "msg_" + uuid.uuid4().hex[:24],
            "type": "message",
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        }
        self.output.append(item)
        self.active_text = index
        self._text_content[index] = ""
        part = {"type": "output_text", "text": "", "annotations": []}
        item["content"].append(part)
        events.extend([
            self.event("response.output_item.added", {"output_index": index, "item": copy.deepcopy(item)}),
            self.event("response.content_part.added", {
                "output_index": index,
                "content_index": 0,
                "item_id": item["id"],
                "part": copy.deepcopy(part),
            }),
        ])
        return events

    def _start_tool(self, tool_call: Dict[str, Any], position: int) -> Tuple[int, List[bytes]]:
        tool_index = str(tool_call.get("index", position))
        if tool_index in self.active_tools:
            return self.active_tools[tool_index], []
        events = self._close_text() + self._close_reasoning()
        function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
        call_id = tool_call.get("id") or "call_" + uuid.uuid4().hex[:24]
        item = {
            "id": "fc_" + uuid.uuid4().hex[:24],
            "type": "function_call",
            "status": "in_progress",
            "call_id": call_id,
            "name": function.get("name", ""),
            "arguments": "",
        }
        index = len(self.output)
        self.output.append(item)
        self.active_tools[tool_index] = index
        self._tool_arguments[index] = ""
        events.append(self.event("response.output_item.added", {"output_index": index, "item": copy.deepcopy(item)}))
        return index, events

    def _close_text(self) -> List[bytes]:
        if self.active_text is None:
            return []
        index = self.active_text
        item = self.output[index]
        text = self._text_content.get(index, "")
        item["status"] = "completed"
        if item.get("content"):
            item["content"][0]["text"] = text
        events = [
            self.event("response.output_text.done", {
                "output_index": index,
                "content_index": 0,
                "item_id": item["id"],
                "text": text,
            }),
            self.event("response.content_part.done", {
                "output_index": index,
                "content_index": 0,
                "item_id": item["id"],
                "part": copy.deepcopy(item["content"][0]),
            }),
            self.event("response.output_item.done", {"output_index": index, "item": copy.deepcopy(item)}),
        ]
        self.active_text = None
        return events

    def _close_reasoning(self) -> List[bytes]:
        if self.active_reasoning is None:
            return []
        index = self.active_reasoning
        item = self.output[index]
        text = self._reasoning_content.get(index, "")
        item["status"] = "completed"
        if text:
            item["summary"] = [{"type": "summary_text", "text": text}]
        events = []
        if text:
            events.append(self.event("response.reasoning_summary_text.done", {
                "output_index": index,
                "item_id": item["id"],
                "summary_index": 0,
                "text": text,
            }))
        events.append(self.event("response.output_item.done", {"output_index": index, "item": copy.deepcopy(item)}))
        self.active_reasoning = None
        return events

    def _close_tools(self) -> List[bytes]:
        events = []
        for tool_index, output_index in list(self.active_tools.items()):
            item = self.output[output_index]
            item["status"] = "completed"
            item["arguments"] = self._tool_arguments.get(output_index, "")
            events.append(self.event("response.function_call_arguments.done", {
                "item_id": item["id"],
                "output_index": output_index,
                "arguments": item["arguments"],
            }))
            events.append(self.event("response.output_item.done", {
                "output_index": output_index,
                "item": copy.deepcopy(item),
            }))
        self.active_tools.clear()
        return events

    def close_all(self) -> List[bytes]:
        return self._close_reasoning() + self._close_text() + self._close_tools()

    def process_chunk(self, chunk: Dict[str, Any]) -> List[bytes]:
        if not isinstance(chunk, dict):
            return []
        if chunk.get("error") is not None:
            return []
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]

        choices = chunk.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return []
        choice = choices[0]
        self.finish_reason = choice.get("finish_reason") or self.finish_reason
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            delta = choice.get("message", {})
        if not isinstance(delta, dict):
            return []

        events: List[bytes] = []
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            events.extend(self._start_reasoning())
            index = self.active_reasoning
            self._reasoning_content[index] += reasoning
            events.append(self.event("response.reasoning_summary_text.delta", {
                "output_index": index,
                "item_id": self.output[index]["id"],
                "summary_index": 0,
                "delta": reasoning,
            }))

        content = delta.get("content")
        if isinstance(content, str) and content:
            events.extend(self._start_text())
            index = self.active_text
            self._text_content[index] += content
            events.append(self.event("response.output_text.delta", {
                "output_index": index,
                "content_index": 0,
                "item_id": self.output[index]["id"],
                "delta": content,
                "logprobs": [],
            }))

        raw_tool_calls = delta.get("tool_calls") or []
        if isinstance(raw_tool_calls, dict):
            raw_tool_calls = [raw_tool_calls]
        if isinstance(raw_tool_calls, list):
            for position, tool_call in enumerate(raw_tool_calls):
                if not isinstance(tool_call, dict):
                    continue
                index, start_events = self._start_tool(tool_call, position)
                events.extend(start_events)
                function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
                if tool_call.get("id"):
                    self.output[index]["call_id"] = tool_call["id"]
                if function.get("name") and not self.output[index].get("name"):
                    self.output[index]["name"] = function["name"]
                arguments = function.get("arguments")
                if arguments is not None:
                    arguments = str(arguments)
                    self._tool_arguments[index] += arguments
                    events.append(self.event("response.function_call_arguments.delta", {
                        "item_id": self.output[index]["id"],
                        "output_index": index,
                        "delta": arguments,
                    }))
        return events

    def final_event(self) -> bytes:
        incomplete_reason = {"length": "max_output_tokens", "content_filter": "content_filter"}.get(self.finish_reason)
        status = "incomplete" if incomplete_reason else "completed"
        details = {"reason": incomplete_reason} if incomplete_reason else None
        response = _build_response_object(
            self.response_id, self.model, self.request, self.output, status,
            self.usage, details,
        )
        event_type = "response.incomplete" if status == "incomplete" else "response.completed"
        return self.event(event_type, {"response": response})

    def failed_event(self, error: Any) -> bytes:
        error_obj = error if isinstance(error, dict) else {"type": "server_error", "message": str(error)}
        response = _build_response_object(
            self.response_id, self.model, self.request, self.output, "failed", self.usage
        )
        response["error"] = error_obj
        return self.event("response.failed", {"response": response})


def build_responses_streaming_response(
    chat_response: Response,
    request: Dict[str, Any],
    model: str,
) -> StreamingResponse:
    """将内部 Chat SSE/JSON 响应实时转换为 Responses SSE 事件流。"""
    async def response_generator():
        builder = _ResponsesStreamBuilder(request, model)
        for event in builder.initial_events():
            yield event

        async for payload in iter_chat_sse_payloads(chat_response):
            if payload is _STREAM_DONE:
                break
            if not isinstance(payload, dict):
                continue
            if payload.get("error") is not None:
                yield builder.failed_event(payload["error"])
                return
            for event in builder.process_chunk(payload):
                yield event

        for event in builder.close_all():
            yield event
        yield builder.final_event()

    return StreamingResponse(
        response_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked",
        },
    )


def build_responses_error_response(status_code: int, detail: Any) -> JSONResponse:
    if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
        content = detail
    else:
        content = {
            "error": {
                "message": detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False),
                "type": "api_error",
                "code": "api_error",
            }
        }
    return JSONResponse(status_code=status_code, content=content)


__all__ = [
    "ResponsesRequestError",
    "convert_responses_to_chat_request",
    "convert_chat_response_to_responses",
    "collect_chat_stream_response",
    "build_responses_streaming_response",
    "build_responses_error_response",
    "read_response_body_bytes",
]
