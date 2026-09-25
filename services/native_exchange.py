"""Native protocol forwarding shared by the Playground and Gemini entry point."""
import asyncio
import copy
import json
import uuid
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from core.config_loader import CONFIG
from core.request_context import RequestContext, current_request, credential_identity
from services.protocol_events import payloads, error_status
from services.provider_capabilities import protocol_name, apply_native_tool_defaults
from utils.usage_tokens import resolve_usage_tokens


class ContextStreamingResponse(StreamingResponse):
    def __init__(self, *args, context, **kwargs):
        self.request_context = context
        super().__init__(*args, **kwargs)

    async def __call__(self, scope, receive, send):
        token = current_request.set(self.request_context)
        try:
            return await super().__call__(scope, receive, send)
        finally:
            current_request.reset(token)


def native_usage(value):
    usage = value.get('usageMetadata') or value.get('usage') or (value.get('response') or value.get('interaction') or value.get('message') or {}).get('usage') or {}
    if 'promptTokenCount' in usage:
        return usage.get('promptTokenCount', 0), (usage.get('candidatesTokenCount') or 0) + (usage.get('thoughtsTokenCount') or 0), usage.get('cachedContentTokenCount', 0), usage
    if 'total_input_tokens' in usage:
        return usage.get('total_input_tokens', 0), (usage.get('total_output_tokens') or 0) + (usage.get('total_thought_tokens') or 0), usage.get('total_cached_tokens', 0), usage
    if 'cache_read_input_tokens' in usage or 'cache_creation_input_tokens' in usage:
        return (usage.get('input_tokens') or 0) + (usage.get('cache_read_input_tokens') or 0) + (usage.get('cache_creation_input_tokens') or 0), usage.get('output_tokens', 0), usage.get('cache_read_input_tokens', 0), usage
    resolved = resolve_usage_tokens(usage)
    details = usage.get('prompt_tokens_details') or usage.get('input_tokens_details') or {}
    cached = details.get('cached_tokens') or usage.get('prompt_cache_hit_tokens') or 0
    return resolved.prompt_tokens, resolved.output_tokens, cached, usage


def text_from_native(value):
    if value.get('type') in ('response.output_text.delta', 'response.reasoning_text.delta', 'response.reasoning_summary_text.delta'):
        return str(value.get('delta') or '')
    if value.get('type') == 'content_block_delta':
        delta = value.get('delta') or {}
        return delta.get('text') or delta.get('thinking') or ''
    if value.get('candidates'):
        return ''.join(str(part.get('text') or '') for candidate in value['candidates'] for part in (candidate.get('content') or {}).get('parts') or [])
    if value.get('choices'):
        choice = value['choices'][0]
        message = choice.get('delta') or choice.get('message') or {}
        return message.get('content') or ''
    if isinstance(value.get('content'), list):
        return ''.join(block.get('text') or '' for block in value['content'] if isinstance(block, dict))
    if value.get('output'):
        return ''.join(block.get('text') or '' for item in value['output'] for block in item.get('content', []) if isinstance(block, dict))
    return ''


def _plain_text(value):
    """把字符串、内容块或字典压成可读文本，用于 system 一类提示的提取。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _plain_text(value.get('parts') or value.get('content') or value.get('text'))
    if isinstance(value, list):
        chunks = []
        for block in value:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict):
                text = block.get('text')
                if isinstance(text, str):
                    chunks.append(text)
        return '\n'.join(chunk for chunk in chunks if chunk)
    return ''


def _content_from_blocks(blocks):
    """把各原生协议的内容块数组转成展示用 content，纯文本收敛成字符串。"""
    if isinstance(blocks, str):
        return blocks
    if isinstance(blocks, dict):
        blocks = [blocks]
    if not isinstance(blocks, list):
        return json.dumps(blocks, ensure_ascii=False) if blocks else ''
    out = []
    for block in blocks:
        if isinstance(block, str):
            if block:
                out.append({'type': 'text', 'text': block})
            continue
        if not isinstance(block, dict):
            continue
        if any(key in block for key in ('inlineData', 'inline_data', 'fileData', 'file_data')) or block.get('type') in ('image', 'image_url', 'input_image', 'input_file'):
            media = block.get('inlineData') or block.get('inline_data') or block.get('fileData') or block.get('file_data') or block
            mime = media.get('mimeType') or media.get('mime_type') or block.get('type') or 'media'
            out.append({'type': 'text', 'text': '[附件: ' + str(mime) + ']'})
            continue
        call = block.get('functionCall') or block.get('function_call')
        if isinstance(call, dict):
            out.append({'type': 'text', 'text': '[functionCall] ' + json.dumps(call, ensure_ascii=False)})
            continue
        response = block.get('functionResponse') or block.get('function_response')
        if isinstance(response, dict):
            out.append({'type': 'text', 'text': '[functionResponse] ' + json.dumps(response, ensure_ascii=False)})
            continue
        text = block.get('text')
        if isinstance(text, str):
            if text:
                out.append({'type': 'text', 'text': text})
            continue
        out.append({'type': 'text', 'text': json.dumps(block, ensure_ascii=False)})
    if not out:
        return ''
    if len(out) == 1:
        return out[0]['text']
    return out


def _messages_from_gemini(body):
    messages = []
    system_text = _plain_text(body.get('systemInstruction'))
    if system_text:
        messages.append({'role': 'system', 'content': system_text})
    contents = body.get('contents')
    if isinstance(contents, str):
        contents = [{'role': 'user', 'parts': [{'text': contents}]}]
    for item in contents or []:
        if isinstance(item, str):
            item = {'role': 'user', 'parts': [{'text': item}]}
        if not isinstance(item, dict):
            continue
        role = item.get('role') or 'user'
        content = _content_from_blocks(item.get('parts'))
        if not content:
            continue
        messages.append({'role': 'assistant' if role == 'model' else role, 'content': content})
    return messages


def _messages_from_interactions(body):
    messages = []
    system_text = _plain_text(body.get('system_instruction'))
    if system_text:
        messages.append({'role': 'system', 'content': system_text})
    for step in body.get('input') or []:
        if not isinstance(step, dict):
            continue
        kind = step.get('type') or ''
        if kind in ('user_input', 'model_output'):
            content = _content_from_blocks(step.get('content'))
            if not content:
                continue
            messages.append({'role': 'assistant' if kind == 'model_output' else 'user', 'content': content})
        elif kind == 'thought':
            text = _plain_text(step.get('summary'))
            if text:
                messages.append({'role': 'assistant', 'content': '[thought] ' + text})
        elif kind == 'function_call':
            messages.append({'role': 'assistant', 'tool_calls': [{
                'id': step.get('id') or step.get('call_id') or '',
                'type': 'function',
                'function': {'name': step.get('name') or '', 'arguments': json.dumps(step.get('arguments') or {}, ensure_ascii=False)}}]})
        elif kind == 'function_result':
            messages.append({'role': 'tool', 'tool_call_id': step.get('call_id') or '',
                             'content': _content_from_blocks(step.get('result')) or ''})
    return messages


def _messages_from_anthropic(body):
    messages = []
    system_text = _plain_text(body.get('system'))
    if system_text:
        messages.append({'role': 'system', 'content': system_text})
    for item in body.get('messages') or []:
        if isinstance(item, dict):
            messages.append(item)
    return messages


def _messages_from_responses(body):
    messages = []
    instructions = _plain_text(body.get('instructions'))
    if instructions:
        messages.append({'role': 'system', 'content': instructions})
    input_items = body.get('input')
    if isinstance(input_items, str):
        input_items = [{'role': 'user', 'content': input_items}]
    for item in input_items or []:
        if isinstance(item, str):
            messages.append({'role': 'user', 'content': item})
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get('type') or 'message'
        if kind == 'message':
            content = _content_from_blocks(item.get('content'))
            if not content:
                continue
            messages.append({'role': item.get('role') or 'user', 'content': content})
        elif kind == 'function_call':
            messages.append({'role': 'assistant', 'tool_calls': [{
                'id': item.get('call_id') or '',
                'type': 'function',
                'function': {'name': item.get('name') or '', 'arguments': item.get('arguments') or '{}'}}]})
        elif kind == 'function_call_output':
            messages.append({'role': 'tool', 'tool_call_id': item.get('call_id') or '',
                             'content': _content_from_blocks(item.get('output')) or ''})
        elif kind == 'reasoning':
            text = _plain_text(item.get('summary'))
            if text:
                messages.append({'role': 'assistant', 'reasoning_content': text})
        else:
            messages.append({'role': 'user', 'content': json.dumps(item, ensure_ascii=False)})
    return messages


def display_messages_for(body, protocol):
    """把原生协议请求体归一化成 [{role, content}] 消息列表，供监控展示和 token 估算。"""
    messages = None
    if isinstance(body, dict):
        if protocol == 'gemini':
            messages = _messages_from_gemini(body)
        elif protocol == 'interactions':
            messages = _messages_from_interactions(body)
        elif protocol == 'anthropic':
            messages = _messages_from_anthropic(body)
        elif protocol == 'responses':
            messages = _messages_from_responses(body)
        else:
            messages = body.get('messages')
    if isinstance(messages, list) and messages:
        return messages
    return [{'role': 'user', 'content': copy.deepcopy(body)}]


async def forward_native_exchange(body, config, model, service, monitor, *, stream=False, context=None, gemini_response=False):
    from routes._direct_api_utils import get_api_key
    from converters.gemini_interactions import convert_gemini_gc_to_interactions, convert_interactions_to_gemini_gc, InteractionsToGeminiGCConverter
    context = context or current_request.get() or RequestContext()
    context.model, context.endpoint = model, copy.deepcopy(config)
    if not context.request_body:
        context.request_body = copy.deepcopy(body)
    token = current_request.set(context)
    iterator = None
    request_id = uuid.uuid4().hex
    ended = False
    started = False
    input_tokens = output_tokens = cached_tokens = 0
    upstream_usage = None
    text_parts = []
    failure = None
    incomplete = False
    observed_tools = set()
    protocol = protocol_name(config)
    params = {'protocol': protocol, 'streaming': stream, 'model_alias': model,
              'caller_id': context.owner_id, 'caller_name': context.owner_name,
              'conversation_id': context.session_id, 'gateway_request_id': context.request_id}
    original_messages = display_messages_for(body, protocol)

    def observe(value):
        nonlocal input_tokens, output_tokens, cached_tokens, upstream_usage, failure, incomplete
        if not isinstance(value, dict):
            return
        if error_status(value):
            failure = str(value.get('error') or value)
        if value.get('type') == 'response.incomplete' or value.get('status') == 'incomplete':
            incomplete = True
        native = value.get('response') or value.get('interaction') or value
        for item in native.get('output', []) or native.get('steps', []):
            kind = item.get('type', '') if isinstance(item, dict) else ''
            if kind.endswith('_call') and kind != 'function_call':
                observed_tools.add(kind)
        event_type = value.get('type') or value.get('event_type') or ''
        if '_call.' in event_type and 'function_call' not in event_type:
            observed_tools.add(event_type.split('.')[1])
        for candidate in native.get('candidates', []):
            if candidate.get('groundingMetadata'):
                observed_tools.add('grounding')
            for part in (candidate.get('content') or {}).get('parts', []):
                if 'codeExecutionResult' in part:
                    observed_tools.add('code_execution')
        inputs, outputs, cached, usage = native_usage(value)
        if usage:
            input_tokens, output_tokens, cached_tokens = max(inputs, input_tokens), max(outputs, output_tokens), max(cached, cached_tokens)
            upstream_usage = {**(upstream_usage or {}), **usage}
        text = text_from_native(value)
        if isinstance(text, str) and text:
            text_parts.append(text)

    async def finish():
        nonlocal ended
        if ended or not started:
            return
        ended = True
        context.mark('finished')
        context.outcome.update(status='cancelled' if failure == 'Client disconnected' else 'failed' if failure else 'incomplete' if incomplete else 'success',
                               observed_native_tools=sorted(observed_tools))
        params['timings'] = context.snapshot()
        cost = service.calculate_cost(input_tokens, output_tokens, config.get('pricing') or {}, cached_tokens=cached_tokens, upstream_usage=upstream_usage)
        monitor.request_end(request_id=request_id, success=failure is None, error=failure,
                            input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=cached_tokens,
                            cost_info=cost, response_content=''.join(text_parts), full_messages=original_messages,
                            upstream_usage=upstream_usage)
        await monitor.broadcast_to_monitors({'type': 'request_end', 'request_id': request_id, 'success': failure is None})

    try:
        key = await get_api_key(model, config.get('api_keys') or config.get('api_key'), strategy=config.get('api_key_strategy', 'round_robin'),
                                cooldown_seconds=int(config.get('api_key_cooldown_seconds') or 172800))
        context.credential_fingerprint = credential_identity(key)
        upstream = copy.deepcopy(body)
        target = config.get('model_id') or model
        base = (config.get('api_base_url') or 'https://generativelanguage.googleapis.com').rstrip('/')
        if protocol in ('gemini', 'interactions'):
            if not base.endswith('/v1beta'):
                base += '/v1beta'
            if protocol == 'interactions':
                if gemini_response:
                    upstream = convert_gemini_gc_to_interactions(upstream, target)
                upstream.update(model=target, stream=stream)
                endpoint = '/interactions' + ('?alt=sse' if stream else '')
            else:
                endpoint = f'/models/{target}:' + ('streamGenerateContent?alt=sse' if stream else 'generateContent')
            headers = {'x-goog-api-key': key}
        else:
            upstream.update(model=target, stream=stream)
            endpoint = config.get('endpoint_path') or {'responses': '/responses', 'anthropic': '/messages'}.get(protocol, '/chat/completions')
            headers = None
        upstream = apply_native_tool_defaults(upstream, config)
        monitor.request_start(request_id=request_id, model=config.get('display_name') or model,
                              messages_count=len(original_messages), messages=original_messages, mode='native_' + protocol, params=params)
        started = True
        await monitor.broadcast_to_monitors({'type': 'request_start', 'request_id': request_id, 'model': model})
        iterator = service.call_api_passthrough(base_url=base, api_key=key, request_body=upstream,
                                                headers=headers, endpoint_path=endpoint, stream_override=stream)
        if not stream:
            raw = b''.join([chunk async for chunk in iterator])
            value = json.loads(raw)
            observe(value)
            await finish()
            status = error_status(value) or 200
            if protocol == 'interactions' and gemini_response and status == 200:
                value = convert_interactions_to_gemini_gc(value)
            return JSONResponse(value, status, headers={'X-Bridge-Session-ID': context.session_id, 'X-Bridge-Request-ID': context.request_id})
        try:
            first = await asyncio.wait_for(anext(iterator), timeout=float(CONFIG.get('first_chunk_timeout_seconds') or 180))
        except (StopAsyncIteration, asyncio.TimeoutError) as error:
            raise HTTPException(502, '上游未返回有效响应') from error
        for value in payloads(first):
            if error_status(value):
                observe(value)
                await finish()
                await iterator.aclose()
                return JSONResponse(value, error_status(value))
        async def generate():
            nonlocal failure
            converter = InteractionsToGeminiGCConverter() if gemini_response and protocol == 'interactions' else None
            async def chunks():
                yield first
                async for chunk in iterator:
                    yield chunk
            try:
                async for chunk in chunks():
                    values = payloads(chunk)
                    for value in values:
                        observe(value)
                    if converter:
                        for value in values:
                            for converted in converter.feed(value):
                                yield ('data: ' + json.dumps(converted, ensure_ascii=False) + '\n\n').encode()
                    else:
                        yield chunk
            except (asyncio.CancelledError, GeneratorExit):
                failure = 'Client disconnected'
                raise
            except Exception as error:
                failure = str(error)
                yield ('data: ' + json.dumps({'error': {'message': '上游响应中断', 'code': 502}}) + '\n\n').encode()
            finally:
                await iterator.aclose()
                await asyncio.shield(finish())
        return ContextStreamingResponse(generate(), context=context, media_type='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'X-Bridge-Session-ID': context.session_id,
                     'X-Bridge-Request-ID': context.request_id})
    except BaseException as error:
        failure = str(error)
        if iterator is not None:
            await iterator.aclose()
        await finish()
        raise
    finally:
        current_request.reset(token)
