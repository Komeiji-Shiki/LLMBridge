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
from utils.upstream_fields import omit_configured_fields

from utils.usage_tokens import MODE_MERGE, apply_usage_tokens
from converters import is_content_parts
from converters.media import FILE_PART_TYPES, file_to_responses
from converters.message_shape import MID_SYSTEM_HOIST, SYSTEM_ROLES, inline_mid_system
from core.conversion_report import note_loss
from services.model_capabilities import drop_unsupported_sampling, supported_effort


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
        elif part_type in FILE_PART_TYPES:
            converted = file_to_responses(part)
            if converted is None:
                raise ResponsesBridgeError(f"{part_type} 内容块缺少文件数据。")
            result.append(converted)
        else:
            # 音视频 Responses 收不了：留一句占位，模型才知道这里本来有东西。
            note_loss(f"content.{part_type}", f"Responses 上游不接受 {part_type} 内容块，已用文字占位")
            result.append({"type": text_type, "text": f"[{part_type} 内容未发送：当前上游不支持]"})
    return result


def _chat_tool_to_responses(tool: Any) -> Dict[str, Any]:
    if not isinstance(tool, dict):
        raise ResponsesBridgeError("Chat tools 中的每项必须是对象。")
    if tool.get('type') == 'custom' and isinstance(tool.get('custom'), dict):
        # Chat 把字段包在 custom 里，Responses 是扁平的。
        return {'type': 'custom', **copy.deepcopy(tool['custom'])}
    if tool.get('type') not in (None, 'function'):
        return copy.deepcopy(tool)
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
    for field in ("strict", "async"):
        if field in function:
            result[field] = copy.deepcopy(function[field])
        elif field in tool:
            result[field] = copy.deepcopy(tool[field])
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
    custom = tool_call.get("custom")
    if tool_call.get("type") == "custom" and isinstance(custom, dict):
        return {"type": "custom_tool_call", "id": "ctc_" + uuid.uuid4().hex[:24], "status": "completed",
                "call_id": call_id, "name": custom.get("name", ""), "input": custom.get("input", "")}
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

    # 中段系统消息留在原位：instructions 排在最前，随提醒变化会让整段缓存失效。
    mid_system_mode = endpoint_config.get("mid_system_mode")
    custom_call_ids = set()
    conversation_started = False
    for message in inline_mid_system(messages, mid_system_mode, role="developer"):
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        content = message.get("content", "")

        if role in SYSTEM_ROLES:
            text = _stringify(content)
            if conversation_started and mid_system_mode != MID_SYSTEM_HOIST:
                input_items.append({"type": "message", "role": "developer",
                                    "content": [{"type": "input_text", "text": text}]})
            elif text:
                instructions.append(text)
            continue
        conversation_started = True

        native_output = (message.get('provider_metadata') or {}).get('responses_output') if isinstance(message.get('provider_metadata'), dict) else None
        if role == 'assistant' and isinstance(native_output, list):
            input_items.extend(copy.deepcopy(native_output))
            custom_call_ids.update(item.get('call_id') for item in native_output
                                   if isinstance(item, dict) and item.get('type') == 'custom_tool_call')
            continue
        native_items = (message.get('provider_metadata') or {}).get('responses_items') if isinstance(message.get('provider_metadata'), dict) else None
        if role == 'assistant' and isinstance(native_items, list):
            input_items.extend(copy.deepcopy(native_items))
            continue

        if role == "tool":
            call_id = message.get("tool_call_id")
            if not call_id:
                raise ResponsesBridgeError("tool 消息缺少 tool_call_id。")
            input_items.append({
                "type": "custom_tool_call_output" if call_id in custom_call_ids else "function_call_output",
                "call_id": call_id,
                # 多模态工具结果必须保留内容块，JSON 字符串无法作为图片输入。
                "output": (_chat_content_to_responses(content, "tool")
                           if is_content_parts(content) else _stringify(content)),
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
                    if tool_call.get("type") == "custom":
                        custom_call_ids.add(tool_call.get("id"))
                    input_items.append(_function_call_to_responses(tool_call))

    request: Dict[str, Any] = {
        "model": model,
        "input": input_items,
        "stream": bool(chat_request.get("stream", False)),
        "store": bool(endpoint_config.get("responses_store", False)),
    }
    from services.provider_capabilities import provider_name
    if provider_name(endpoint_config) == 'deepseek':
        request.pop('store', None)
    if instructions:
        request["instructions"] = "\n\n".join(instructions)

    for key in ("temperature", "top_p", "user", "metadata", "prompt_cache_key", "prompt_cache_retention",
                "prompt_cache_options", "service_tier", "safety_identifier", "top_logprobs"):
        if key in chat_request:
            request[key] = copy.deepcopy(chat_request[key])
    if chat_request.get("logprobs"):
        request["include"] = ["message.output_text.logprobs"]
    for key in ("stop", "seed", "frequency_penalty", "presence_penalty", "logit_bias", "modalities", "audio", "prediction"):
        if chat_request.get(key) is not None:
            note_loss(key, f"Responses 上游没有对应的 {key} 字段，未发送")
    if (chat_request.get("n") or 1) > 1:
        note_loss("n", "Responses 上游只返回一个候选")

    max_output_tokens = chat_request.get("max_completion_tokens")
    if max_output_tokens is None:
        max_output_tokens = chat_request.get("max_tokens")
    if max_output_tokens is not None:
        request["max_output_tokens"] = max_output_tokens

    reasoning_effort = supported_effort(model, chat_request.get("reasoning_effort") or endpoint_config.get("reasoning_effort"))
    drop_unsupported_sampling(request, model, reasoning_effort)
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
    web_search_options = chat_request.get("web_search_options")
    if isinstance(web_search_options, dict):
        request.setdefault("tools", []).append({"type": "web_search", **copy.deepcopy(web_search_options)})
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
    return omit_configured_fields(request, endpoint_config)


def _extract_output_text(item: Dict[str, Any]) -> str:
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        str(part.get("text", ""))
        for part in content
        if isinstance(part, dict) and part.get("type") in ("output_text", "text")
    )


def _extract_refusal(item: Dict[str, Any]) -> str:
    content = item.get("content")
    return "".join(str(part.get("refusal", "")) for part in content if isinstance(part, dict)
                   and part.get("type") == "refusal") if isinstance(content, list) else ""


def _chat_annotations(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Responses 的引用是扁平字段，Chat 包在 url_citation 里。"""
    annotations = []
    for part in item.get("content") or []:
        for annotation in (part.get("annotations") or []) if isinstance(part, dict) else []:
            converted = _chat_annotation(annotation)
            if converted:
                annotations.append(converted)
    return annotations


def _chat_annotation(annotation: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
        return None
    return {"type": "url_citation", "url_citation": {key: annotation[key] for key in
            ("url", "title", "start_index", "end_index") if key in annotation}}


def _image_part(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    result = item.get("result")
    if not isinstance(result, str) or not result:
        return None
    # 生图结果没有声明类型时按默认输出格式。
    mime_type = f"image/{item.get('output_format') or 'png'}"
    return {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{result}"}}


def _extract_reasoning_text(item: Dict[str, Any]) -> str:
    summary = item.get("summary")
    if not isinstance(summary, list):
        return ""
    return "".join(
        str(part.get("text", ""))
        for part in summary
        if isinstance(part, dict) and part.get("type") in ("summary_text", "text")
    )


def _usage_to_chat(
    usage: Any,
    completion_tokens_mode: str = MODE_MERGE,
) -> Dict[str, Any]:
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
    if isinstance(input_details, dict):
        result["prompt_tokens_details"] = {
            key: int(input_details[key] or 0) for key in
            ('cached_tokens', 'cache_write_tokens', 'cache_write_1h_tokens') if key in input_details
        }
    output_details = usage.get("output_tokens_details")
    if isinstance(output_details, dict) and output_details.get("reasoning_tokens") is not None:
        result["completion_tokens_details"] = {
            "reasoning_tokens": int(output_details.get("reasoning_tokens", 0) or 0)
        }
        result["reasoning_tokens"] = int(output_details.get("reasoning_tokens", 0) or 0)
    # Responses 的 output_tokens 已含思考，按模型配置决定 Chat 侧给总量还是正文
    apply_usage_tokens(result, completion_tokens_mode)
    return result


def convert_responses_response_to_chat(
    responses_response: Dict[str, Any],
    request_model: str,
    completion_tokens_mode: str = MODE_MERGE,
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
    refusal_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    annotations: List[Dict[str, Any]] = []
    images: List[Dict[str, Any]] = []

    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            text_parts.append(_extract_output_text(item))
            refusal_parts.append(_extract_refusal(item))
            annotations.extend(_chat_annotations(item))
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
        elif item_type == "custom_tool_call":
            tool_calls.append({
                "id": item.get("call_id") or item.get("id") or "call_" + uuid.uuid4().hex[:24],
                "type": "custom",
                "custom": {"name": item.get("name", ""), "input": item.get("input", "")},
            })
        elif item_type == "image_generation_call" and _image_part(item):
            images.append(_image_part(item))

    content = "".join(text_parts)
    if not content and isinstance(responses_response.get("output_text"), str):
        content = responses_response["output_text"]
    refusal = "".join(refusal_parts)
    # 很多客户端只读 content：拒答同时写进正文，否则用户只看到一片空白。
    message: Dict[str, Any] = {"role": "assistant", "content": content or refusal}
    # Native output carries reasoning signatures, citations and server tool calls.
    # Clients that retain this extension can return it verbatim on the next turn.
    message['provider_metadata'] = {'responses_output': copy.deepcopy(output)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls
    if refusal:
        message["refusal"] = refusal
    if annotations:
        message["annotations"] = annotations
    if images:
        message["images"] = images

    status = responses_response.get("status")
    incomplete_details = responses_response.get("incomplete_details")
    incomplete_reason = incomplete_details.get("reason") if isinstance(incomplete_details, dict) else None
    if tool_calls:
        finish_reason = "tool_calls"
    elif refusal:
        finish_reason = "content_filter"
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
        "usage": _usage_to_chat(responses_response.get("usage"), completion_tokens_mode),
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
    def __init__(self, request_model: str, completion_tokens_mode: str = MODE_MERGE):
        self.request_model = request_model
        self.completion_tokens_mode = completion_tokens_mode
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
        self.native_response = None
        self.output_items = {}
        self.refused = False
        self.saw_output_text = False
        self.custom_tools = set()
        self.error = None

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
            self.error = error
            return [f"data: {json.dumps({'error': error}, ensure_ascii=False)}\n\n".encode("utf-8")]

        if not event_type and (
            event.get("object") == "response" or isinstance(event.get("output"), list)
        ):
            self.native_response = copy.deepcopy(event)
            chat_response = convert_responses_response_to_chat(
                event, self.request_model, self.completion_tokens_mode)
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
                chunks.append(self.chunk({"content": content, **({"refusal": message["refusal"]} if message.get("refusal") else {})}))
            for field in ("images", "annotations"):
                if message.get(field):
                    chunks.append(self.chunk({field: message[field]}))
            for position, tool_call in enumerate(message.get("tool_calls") or []):
                if not isinstance(tool_call, dict):
                    continue
                if tool_call.get("type") == "custom":
                    _, index = self._tool_index({"id": tool_call.get("id"), "output_index": position})
                    chunks.append(self.chunk({"tool_calls": [{**tool_call, "index": index}]}))
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
            if message.get('provider_metadata'):
                chunks.append(self.chunk({'provider_metadata': message['provider_metadata']}))
            return chunks

        if event_type in ("response.created", "response.in_progress"):
            return []

        if event_type == "response.output_item.added":
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if item.get("type") == "custom_tool_call":
                key, index = self._tool_index({**item, "output_index": event.get("output_index")})
                self.tool_ids[key] = item.get("call_id") or item.get("id") or "call_" + uuid.uuid4().hex[:24]
                self.tool_names[key] = item.get("name", "")
                self.tool_arguments.setdefault(key, "")
                self.custom_tools.add(key)
                return [self.chunk({"tool_calls": [{"index": index, "id": self.tool_ids[key], "type": "custom",
                                                    "custom": {"name": self.tool_names[key], "input": ""}}]})]
            if item.get("type") != "function_call":
                return [self.chunk({'provider_event': {key: copy.deepcopy(value) for key, value in event.items() if key != '_event_type'}})]
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
            if isinstance(delta, str) and delta:
                self.saw_output_text = True
                return [self.chunk({"content": delta})]
            return []

        if event_type == "response.refusal.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                self.refused = True
                return [self.chunk({"content": delta, "refusal": delta})]
            return []

        if event_type == "response.output_text.annotation.added":
            annotation = _chat_annotation(event.get("annotation"))
            return [self.chunk({"annotations": [annotation]})] if annotation else []

        if event_type == "response.custom_tool_call_input.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                return []
            key, index = self._tool_index(event)
            self.tool_arguments[key] = self.tool_arguments.get(key, "") + delta
            return [self.chunk({"tool_calls": [{"index": index, "custom": {"input": delta}}]})]

        if event_type in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
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
            if item:
                self.output_items[event.get('output_index', len(self.output_items))] = copy.deepcopy(item)
            if item.get("type") == "reasoning":
                return [self.chunk({'provider_event': {key: copy.deepcopy(value) for key, value in event.items() if key != '_event_type'}})]
            if item.get("type") == "image_generation_call" and _image_part(item):
                return [self.chunk({"images": [_image_part(item)]})]
            if item.get("type") == "custom_tool_call":
                key, index = self._tool_index({**item, "output_index": event.get("output_index")})
                text = item.get("input")
                if isinstance(text, str) and not self.tool_arguments.get(key):
                    self.tool_arguments[key] = text
                    return [self.chunk({"tool_calls": [{"index": index, "custom": {"input": text}}]})]
                return []
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
            self.native_response = copy.deepcopy(response)
            if not self.native_response.get('output') and self.output_items:
                self.native_response['output'] = [self.output_items[i] for i in sorted(self.output_items)]
            response = self.native_response
            self.usage = _usage_to_chat(response.get("usage"), self.completion_tokens_mode)
            if event_type == "response.incomplete":
                details = response.get("incomplete_details")
                reason = details.get("reason") if isinstance(details, dict) else None
                self.finish_reason = "content_filter" if reason == "content_filter" else "length"
            chunks = []
            # 个别上游和中转不发增量，只在终态给完整输出，正文和调用不能因此丢掉。
            if not self.saw_output_text and not self.refused or not self.tool_indexes:
                recovered = convert_responses_response_to_chat(response, self.request_model, self.completion_tokens_mode)
                message = recovered["choices"][0]["message"]
                if message.get("content") and not self.saw_output_text and not self.refused:
                    self.refused = bool(message.get("refusal"))
                    chunks.append(self.chunk({"content": message["content"], **(
                        {"refusal": message["refusal"]} if message.get("refusal") else {})}))
                for position, call in enumerate(message.get("tool_calls") or [] if not self.tool_indexes else []):
                    key, index = self._tool_index({"id": call.get("id"), "output_index": position})
                    self.tool_ids[key] = call.get("id")
                    if call.get("type") == "custom":
                        self.custom_tools.add(key)
                        self.tool_names[key] = call["custom"].get("name", "")
                        self.tool_arguments[key] = call["custom"].get("input", "")
                    else:
                        self.tool_names[key] = call["function"].get("name", "")
                        self.tool_arguments[key] = call["function"].get("arguments", "")
                    chunks.append(self.chunk({"tool_calls": [{**call, "index": index}]}))
            if event_type == "response.completed":
                self.finish_reason = "tool_calls" if self.tool_indexes else "content_filter" if self.refused else "stop"
            chunks.append(self.chunk({'provider_metadata': {'responses_output': copy.deepcopy(response.get('output') or [])}}))
            return chunks

        if event_type in ("response.failed", "error"):
            self.failed = True
            error = event.get("error")
            if error is None and isinstance(event.get("response"), dict):
                error = event["response"].get("error")
            if error is None and event_type == "error" and event.get("message"):
                # Responses stream errors place message/code/param at the top level.
                error = {"message": event["message"], "type": "api_error"}
                for key in ("code", "param"):
                    if key in event:
                        error[key] = event[key]
            error = error if isinstance(error, dict) else {"message": str(error or "上游 Responses 请求失败"), "type": "api_error"}
            self.error = error
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
    completion_tokens_mode: str = MODE_MERGE,
    on_response=None,
) -> StreamingResponse:
    """将上游 Responses SSE 转成 OpenAI Chat Completions SSE。"""
    async def generator():
        builder = _ChatStreamBuilder(request_model, completion_tokens_mode)
        yield builder.chunk({"role": "assistant"})
        async for event in iter_responses_sse_events(source):
            if event is _STREAM_DONE:
                break
            chunks = builder.process(event)
            if on_response is not None and builder.native_response is not None and not builder.failed:
                # Persist before exposing the terminal chunk: a client may send
                # the next request as soon as it sees that chunk.
                response, builder.native_response = builder.native_response, None
                await on_response(response)
            for chunk in chunks:
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


async def collect_responses_stream_to_chat(
    source: Any,
    request_model: str,
    completion_tokens_mode: str = MODE_MERGE,
) -> Dict[str, Any]:
    """收集上游 Responses SSE，用于 force_stream 导致的非流式客户端。"""
    chunks: List[Dict[str, Any]] = []
    async for event in iter_responses_sse_events(source):
        if event is _STREAM_DONE:
            break
        if isinstance(event, dict):
            chunks.append(event)

    builder = _ChatStreamBuilder(request_model, completion_tokens_mode)
    message: Dict[str, Any] = {"role": "assistant", "content": ""}
    text_parts = []
    reasoning_parts = []
    refusal_parts = []
    annotations = []
    images = []
    tool_calls = []
    for event in chunks:
        # 构建器产出的增量就是客户端会看到的内容，这里只需要把它们累加起来。
        for chunk in builder.process(event):
            payload = json.loads(chunk.decode("utf-8")[len("data: "):])
            choices = payload.get("choices") or []
            delta = choices[0].get("delta", {}) if choices else {}
            if isinstance(delta.get("content"), str):
                text_parts.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                reasoning_parts.append(delta["reasoning_content"])
            if isinstance(delta.get("refusal"), str):
                refusal_parts.append(delta["refusal"])
            annotations.extend(delta.get("annotations") or [])
            images.extend(delta.get("images") or [])
        event_type = event.get("_event_type") or event.get("type", "")
        if event_type in ('response.completed', 'response.incomplete'):
            message['provider_metadata'] = {'responses_output': copy.deepcopy((event.get('response') or {}).get('output') or [])}
    if builder.failed:
        # 失败不能伪装成正常结束，半截内容也不算成功。
        return {"error": builder.error if isinstance(builder.error, dict) else {
            "message": str(builder.error or "上游 Responses 请求失败"), "type": "api_error"}}
    message["content"] = "".join(text_parts)
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if refusal_parts:
        message["refusal"] = "".join(refusal_parts)
    if annotations:
        message["annotations"] = annotations
    if images:
        message["images"] = images
    for key, index in sorted(builder.tool_indexes.items(), key=lambda pair: pair[1]):
        call_id = builder.tool_ids.get(key) or "call_" + uuid.uuid4().hex[:24]
        if key in builder.custom_tools:
            tool_calls.append({"id": call_id, "type": "custom", "custom": {
                "name": builder.tool_names.get(key, ""), "input": builder.tool_arguments.get(key, "")}})
            continue
        tool_calls.append({
            "id": call_id,
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
