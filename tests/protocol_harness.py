"""协议矩阵的测试台：本地 HTTP 假上游 + 真实路由，客户端和上游之间不打桩。

一轮回答用 Turn 描述，序列化成各上游协议的线路格式；客户端收到的内容再读回 Seen，
这样每个“客户端 × 上游”组合都能用同一组断言比较语义。
"""
import asyncio
from dataclasses import dataclass, field
import json
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from fastapi import FastAPI
import httpx

CLIENTS = ('chat', 'anthropic', 'responses')
UPSTREAMS = ('chat', 'anthropic', 'responses', 'gemini', 'interactions')
MODEL = 'matrix-model'


@dataclass
class Turn:
    reasoning: str = ''
    text: str = ''
    calls: List[Tuple[str, str, dict]] = field(default_factory=list)
    stop: str = 'end'  # end / tool / length / refusal
    input_tokens: int = 100
    output_tokens: int = 20


@dataclass
class Reply:
    """假上游对一次请求的回应。events 按流式事件逐个写出，(事件名, 数据) 或裸数据。"""
    status: int = 200
    body: Any = None
    events: Optional[list] = None
    headers: Dict[str, str] = field(default_factory=dict)


# ---- Turn → 各上游协议的线路格式 ----

def _chat_tool_calls(turn):
    return [{'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}
            for call_id, name, args in turn.calls]


_CHAT_FINISH = {'end': 'stop', 'tool': 'tool_calls', 'length': 'length', 'refusal': 'content_filter'}


def chat_reply(turn, stream):
    usage = {'prompt_tokens': turn.input_tokens, 'completion_tokens': turn.output_tokens,
             'total_tokens': turn.input_tokens + turn.output_tokens}
    base = {'id': 'chatcmpl-up', 'model': 'upstream', 'created': 1}
    finish = _CHAT_FINISH[turn.stop]
    if not stream:
        message = {'role': 'assistant', 'content': turn.text or None}
        if turn.reasoning:
            message['reasoning_content'] = turn.reasoning
        if turn.calls:
            message['tool_calls'] = _chat_tool_calls(turn)
        return Reply(body={**base, 'object': 'chat.completion', 'usage': usage,
                           'choices': [{'index': 0, 'message': message, 'finish_reason': finish}]})

    def chunk(delta, reason=None):
        return {**base, 'object': 'chat.completion.chunk',
                'choices': [{'index': 0, 'delta': delta, 'finish_reason': reason}]}
    events = [chunk({'role': 'assistant'})]
    if turn.reasoning:
        events.append(chunk({'reasoning_content': turn.reasoning}))
    if turn.text:
        events.append(chunk({'content': turn.text}))
    for index, call in enumerate(_chat_tool_calls(turn)):
        name_part = {**call, 'function': {'name': call['function']['name'], 'arguments': ''}}
        events.append(chunk({'tool_calls': [{'index': index, **name_part}]}))
        events.append(chunk({'tool_calls': [{'index': index, 'function': {'arguments': call['function']['arguments']}}]}))
    events.append(chunk({}, finish))
    events.append({**base, 'object': 'chat.completion.chunk', 'choices': [], 'usage': usage})
    events.append('[DONE]')
    return Reply(events=events)


_ANTHROPIC_STOP = {'end': 'end_turn', 'tool': 'tool_use', 'length': 'max_tokens', 'refusal': 'refusal'}


def anthropic_reply(turn, stream):
    blocks = []
    if turn.reasoning:
        blocks.append({'type': 'thinking', 'thinking': turn.reasoning, 'signature': 'sig-anthropic'})
    if turn.text:
        blocks.append({'type': 'text', 'text': turn.text})
    blocks.extend({'type': 'tool_use', 'id': call_id, 'name': name, 'input': args} for call_id, name, args in turn.calls)
    stop = _ANTHROPIC_STOP[turn.stop]
    if not stream:
        return Reply(body={'id': 'msg_up', 'type': 'message', 'role': 'assistant', 'model': 'upstream', 'content': blocks,
                           'stop_reason': stop, 'stop_sequence': None,
                           'usage': {'input_tokens': turn.input_tokens, 'output_tokens': turn.output_tokens}})
    events = [('message_start', {'type': 'message_start', 'message': {
        'id': 'msg_up', 'type': 'message', 'role': 'assistant', 'model': 'upstream', 'content': [], 'stop_reason': None,
        'usage': {'input_tokens': turn.input_tokens, 'output_tokens': 0}}})]
    for index, block in enumerate(blocks):
        kind = block['type']
        start = {'thinking': {'type': 'thinking', 'thinking': '', 'signature': ''}, 'text': {'type': 'text', 'text': ''},
                 'tool_use': {'type': 'tool_use', 'id': block.get('id'), 'name': block.get('name'), 'input': {}}}[kind]
        events.append(('content_block_start', {'type': 'content_block_start', 'index': index, 'content_block': start}))
        deltas = {'thinking': [{'type': 'thinking_delta', 'thinking': block.get('thinking')},
                               {'type': 'signature_delta', 'signature': block.get('signature')}],
                  'text': [{'type': 'text_delta', 'text': block.get('text')}],
                  'tool_use': [{'type': 'input_json_delta', 'partial_json': json.dumps(block.get('input'))}]}[kind]
        events.extend(('content_block_delta', {'type': 'content_block_delta', 'index': index, 'delta': delta}) for delta in deltas)
        events.append(('content_block_stop', {'type': 'content_block_stop', 'index': index}))
    events.append(('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': stop, 'stop_sequence': None},
                                     'usage': {'output_tokens': turn.output_tokens}}))
    events.append(('message_stop', {'type': 'message_stop'}))
    return Reply(events=events)


def _responses_output(turn):
    output = []
    if turn.reasoning:
        output.append({'type': 'reasoning', 'id': 'rs_1', 'encrypted_content': 'cipher',
                       'summary': [{'type': 'summary_text', 'text': turn.reasoning}]})
    content = []
    if turn.text:
        content.append({'type': 'refusal', 'refusal': turn.text} if turn.stop == 'refusal'
                       else {'type': 'output_text', 'text': turn.text, 'annotations': []})
    if content:
        output.append({'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed', 'content': content})
    output.extend({'type': 'function_call', 'id': 'fc_' + call_id, 'call_id': call_id, 'name': name,
                   'arguments': json.dumps(args), 'status': 'completed'} for call_id, name, args in turn.calls)
    return output


def responses_reply(turn, stream):
    final = {'id': 'resp_up', 'object': 'response', 'model': 'upstream', 'output': _responses_output(turn),
             'status': 'incomplete' if turn.stop == 'length' else 'completed',
             'incomplete_details': {'reason': 'max_output_tokens'} if turn.stop == 'length' else None,
             'usage': {'input_tokens': turn.input_tokens, 'output_tokens': turn.output_tokens,
                       'total_tokens': turn.input_tokens + turn.output_tokens}}
    if not stream:
        return Reply(body=final)
    created = {**final, 'status': 'in_progress', 'output': [], 'usage': None}
    events = [('response.created', {'type': 'response.created', 'response': created})]
    for index, item in enumerate(final['output']):
        empty = {**item, 'content': [], 'summary': []} if item['type'] != 'function_call' else {**item, 'arguments': ''}
        events.append(('response.output_item.added', {'type': 'response.output_item.added', 'output_index': index, 'item': empty}))
        if item['type'] == 'reasoning':
            events.append(('response.reasoning_summary_text.delta', {
                'type': 'response.reasoning_summary_text.delta', 'output_index': index, 'summary_index': 0, 'delta': turn.reasoning}))
        elif item['type'] == 'function_call':
            events.append(('response.function_call_arguments.delta', {
                'type': 'response.function_call_arguments.delta', 'output_index': index, 'item_id': item['id'], 'delta': item['arguments']}))
        elif item['content'][0]['type'] == 'refusal':
            events.append(('response.refusal.delta', {
                'type': 'response.refusal.delta', 'output_index': index, 'content_index': 0, 'delta': turn.text}))
        else:
            events.append(('response.output_text.delta', {
                'type': 'response.output_text.delta', 'output_index': index, 'content_index': 0, 'delta': turn.text}))
        events.append(('response.output_item.done', {'type': 'response.output_item.done', 'output_index': index, 'item': item}))
    terminal = 'response.incomplete' if turn.stop == 'length' else 'response.completed'
    events.append((terminal, {'type': terminal, 'response': final}))
    return Reply(events=events)


_GEMINI_FINISH = {'end': 'STOP', 'tool': 'STOP', 'length': 'MAX_TOKENS', 'refusal': 'SAFETY'}


def gemini_reply(turn, stream):
    usage = {'promptTokenCount': turn.input_tokens, 'candidatesTokenCount': turn.output_tokens,
             'totalTokenCount': turn.input_tokens + turn.output_tokens}
    parts = []
    if turn.reasoning:
        parts.append({'text': turn.reasoning, 'thought': True})
    if turn.text:
        parts.append({'text': turn.text})
    for index, (call_id, name, args) in enumerate(turn.calls):
        call_part = {'functionCall': {'id': call_id, 'name': name, 'args': args}}
        if index == 0:
            call_part['thoughtSignature'] = 'sig-gemini'
        parts.append(call_part)
    finish = _GEMINI_FINISH[turn.stop]
    if not stream:
        return Reply(body={'candidates': [{'content': {'role': 'model', 'parts': parts}, 'finishReason': finish}],
                           'usageMetadata': usage})
    events = [{'candidates': [{'content': {'role': 'model', 'parts': [part]}}]} for part in parts]
    events.append({'candidates': [{'finishReason': finish}], 'usageMetadata': usage})
    return Reply(events=events)


def _interaction_steps(turn):
    steps = []
    if turn.reasoning:
        steps.append({'type': 'thought', 'signature': 'sig-interactions', 'summary': [{'type': 'text', 'text': turn.reasoning}]})
    if turn.text:
        steps.append({'type': 'model_output', 'content': [{'type': 'text', 'text': turn.text}]})
    steps.extend({'type': 'function_call', 'id': call_id, 'name': name, 'arguments': args} for call_id, name, args in turn.calls)
    return steps


def interactions_reply(turn, stream, status=None, token=None, errors=None):
    status = status or {'end': 'completed', 'tool': 'requires_action', 'length': 'incomplete'}.get(turn.stop, 'completed')
    usage = {'total_input_tokens': turn.input_tokens, 'total_output_tokens': turn.output_tokens,
             'total_tokens': turn.input_tokens + turn.output_tokens}
    extra = {'continuation_token': token} if token else {}
    if errors:
        extra['errors'] = errors
    if not stream:
        return Reply(body={'id': 'int_up', 'status': status, 'steps': _interaction_steps(turn), 'usage': usage, **extra})
    events = [{'event_type': 'interaction.created', 'interaction': {'id': 'int_up', 'status': 'in_progress'}}]
    for index, step in enumerate(_interaction_steps(turn)):
        if step['type'] == 'thought':
            events.append({'event_type': 'step.start', 'index': index, 'step': {'type': 'thought'}})
            events.append({'event_type': 'step.delta', 'index': index, 'delta': {
                'type': 'thought_summary', 'content': {'type': 'text', 'text': turn.reasoning}}})
            events.append({'event_type': 'step.delta', 'index': index, 'delta': {
                'type': 'thought_signature', 'signature': step['signature']}})
        elif step['type'] == 'model_output':
            events.append({'event_type': 'step.start', 'index': index, 'step': {'type': 'model_output'}})
            events.append({'event_type': 'step.delta', 'index': index, 'delta': {'type': 'text', 'text': turn.text}})
        else:
            events.append({'event_type': 'step.start', 'index': index, 'step': {
                'type': 'function_call', 'id': step['id'], 'name': step['name'], 'arguments': {}}})
            # 参数分两段到达，网关要逐段转发、客户端拼回
            arguments = json.dumps(step['arguments'])
            middle = len(arguments) // 2
            events.extend({'event_type': 'step.delta', 'index': index, 'delta': {
                'type': 'arguments_delta', 'arguments': piece}} for piece in (arguments[:middle], arguments[middle:]))
        events.append({'event_type': 'step.stop', 'index': index})
    events.append({'event_type': 'interaction.completed', 'interaction': {'id': 'int_up', 'status': status, 'usage': usage, **extra}})
    return Reply(events=events)


REPLY_BUILDERS = {'chat': chat_reply, 'anthropic': anthropic_reply, 'responses': responses_reply,
                  'gemini': gemini_reply, 'interactions': interactions_reply}


def endpoint_for(upstream, base_url, **overrides):
    config = {
        'chat': {'api_type': 'direct_api', 'passthrough': True, 'api_base_url': base_url + '/v1', 'model_id': 'upstream-chat'},
        'anthropic': {'api_type': 'anthropic_native', 'api_base_url': base_url + '/v1', 'model_id': 'claude-sonnet-4-5'},
        'responses': {'api_type': 'responses_native', 'api_base_url': base_url + '/v1', 'model_id': 'upstream-responses'},
        'gemini': {'api_type': 'gemini_native', 'api_base_url': base_url + '/v1beta', 'model_id': 'gemini-3.8-flash'},
        'interactions': {'api_type': 'gemini_native', 'upstream_protocol': 'interactions',
                         'api_base_url': base_url + '/v1beta', 'model_id': 'gemini-3.8-flash'},
    }[upstream]
    return {**config, 'api_key': 'upstream-secret', **overrides}


# ---- 假上游 ----

class FakeUpstream:
    """按顺序回放 replies，记录收到的每个请求。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests: List[dict] = []

    async def handle(self, request):
        body = await request.read()
        self.requests.append({'method': request.method, 'path': request.path, 'query': dict(request.query),
                              'headers': dict(request.headers), 'body': json.loads(body) if body else None})
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if reply.events is None:
            return web.json_response(reply.body, status=reply.status, headers=reply.headers)
        response = web.StreamResponse(status=reply.status, headers={'Content-Type': 'text/event-stream', **reply.headers})
        await response.prepare(request)
        for event in reply.events:
            if event == '[DONE]':
                text = 'data: [DONE]\n\n'
            elif isinstance(event, tuple):
                text = f'event: {event[0]}\ndata: {json.dumps(event[1], ensure_ascii=False)}\n\n'
            else:
                text = f'data: {json.dumps(event, ensure_ascii=False)}\n\n'
            await response.write(text.encode())
        await response.write_eof()
        return response


# ---- 网关 ----

def build_gateway(monkeypatch):
    from core.request_middleware import GatewayRequestMiddleware
    from routes import api_routes, responses_api, stored_objects_api, token_count_api
    monitor = MagicMock()
    monitor.broadcast_to_monitors = AsyncMock()
    monkeypatch.setattr(api_routes, 'CONFIG', {})
    monkeypatch.setattr(api_routes.api_key_manager, 'has_keys', lambda: False)
    monkeypatch.setattr(api_routes, '_check_verification_cooldown', lambda: None)
    monkeypatch.setattr(api_routes, 'monitoring_service', monitor)
    monkeypatch.setattr(responses_api, 'monitoring_service', monitor)
    app = FastAPI()
    app.add_middleware(GatewayRequestMiddleware)
    app.include_router(api_routes.router)
    app.include_router(responses_api.router)
    app.include_router(token_count_api.router)
    app.include_router(stored_objects_api.router)
    return app, monitor


CLIENT_PATHS = {'chat': '/v1/chat/completions', 'anthropic': '/v1/messages', 'responses': '/v1/responses'}


async def _exchange(app, monkeypatch, upstream_name, replies, calls, overrides):
    from services.direct_api_service import DirectAPIService
    from routes import api_routes
    fake = FakeUpstream(replies)
    upstream_app = web.Application(client_max_size=64 * 1024 * 1024)
    upstream_app.router.add_route('*', '/{tail:.*}', fake.handle)
    server = TestServer(upstream_app)
    await server.start_server()
    session = aiohttp.ClientSession()
    try:
        base_url = str(server.make_url('')).rstrip('/')
        config = endpoint_for(upstream_name, base_url, **overrides)
        monkeypatch.setitem(api_routes.MODEL_ENDPOINT_MAP, MODEL, config)
        monkeypatch.setattr(api_routes._app_state.server, 'direct_api_service', DirectAPIService(session))
        transport = httpx.ASGITransport(app=app)
        responses = []
        async with httpx.AsyncClient(transport=transport, base_url='http://gateway', timeout=30) as client:
            for method, path, body, headers in calls:
                responses.append(await client.request(method, path, json=body, headers=headers or {}))
        return responses, fake
    finally:
        await session.close()
        await server.close()


def exchange(app, monkeypatch, upstream, replies, path, body, overrides=None, headers=None):
    """跑一次完整请求，返回 (客户端响应, 假上游)。"""
    responses, fake = asyncio.run(_exchange(app, monkeypatch, upstream, replies, [('POST', path, body, headers)],
                                            overrides or {}))
    return responses[0], fake


def exchange_many(app, monkeypatch, upstream, replies, calls, overrides=None):
    """同一个假上游上依次跑多个请求，calls 是 (方法, 路径, 正文, 请求头)；返回 (响应列表, 假上游)。"""
    return asyncio.run(_exchange(app, monkeypatch, upstream, replies, calls, overrides or {}))


# ---- 客户端读回 ----

@dataclass
class Seen:
    text: str = ''
    reasoning: str = ''
    calls: List[Tuple[str, str, dict]] = field(default_factory=list)
    stop: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    error: Optional[str] = None


def _sse_data(text):
    for block in text.replace('\r\n', '\n').split('\n\n'):
        for line in block.split('\n'):
            if line.startswith('data: ') and line[6:].strip() != '[DONE]':
                yield json.loads(line[6:])


def _args(raw):
    return json.loads(raw) if raw else {}


def read_chat(response, stream):
    seen = Seen()
    finish = {'stop': 'end', 'tool_calls': 'tool', 'length': 'length', 'content_filter': 'refusal'}
    if response.status_code >= 400:
        seen.error = response.text
        return seen
    if not stream:
        body = response.json()
        if body.get('error'):
            seen.error = json.dumps(body, ensure_ascii=False)
            return seen
        choice = body['choices'][0]
        message = choice['message']
        seen.text = message.get('content') or ''
        seen.reasoning = message.get('reasoning_content') or ''
        seen.calls = [(call['id'], call['function']['name'], _args(call['function']['arguments']))
                      for call in message.get('tool_calls') or []]
        seen.stop = finish.get(choice.get('finish_reason'))
        usage = body.get('usage') or {}
        seen.input_tokens, seen.output_tokens = usage.get('prompt_tokens'), usage.get('completion_tokens')
        return seen
    calls: Dict[int, dict] = {}
    for chunk in _sse_data(response.text):
        if chunk.get('error'):
            seen.error = json.dumps(chunk['error'], ensure_ascii=False)
            continue
        if chunk.get('usage'):
            seen.input_tokens, seen.output_tokens = chunk['usage'].get('prompt_tokens'), chunk['usage'].get('completion_tokens')
        for choice in chunk.get('choices') or []:
            delta = choice.get('delta') or {}
            seen.text += delta.get('content') or ''
            seen.reasoning += delta.get('reasoning_content') or ''
            for call in delta.get('tool_calls') or []:
                slot = calls.setdefault(call['index'], {'id': None, 'name': '', 'arguments': ''})
                slot['id'] = call.get('id') or slot['id']
                function = call.get('function') or {}
                slot['name'] += function.get('name') or ''
                slot['arguments'] += function.get('arguments') or ''
            if choice.get('finish_reason'):
                seen.stop = finish.get(choice['finish_reason'])
    seen.calls = [(calls[index]['id'], calls[index]['name'], _args(calls[index]['arguments'])) for index in sorted(calls)]
    return seen


_ANTHROPIC_SEEN = {'end_turn': 'end', 'tool_use': 'tool', 'max_tokens': 'length', 'refusal': 'refusal', 'stop_sequence': 'end'}


def read_anthropic(response, stream):
    seen = Seen()
    if response.status_code >= 400:
        seen.error = response.text
        return seen
    if not stream:
        body = response.json()
        if body.get('type') == 'error' or body.get('error'):
            seen.error = json.dumps(body, ensure_ascii=False)
            return seen
        for block in body.get('content') or []:
            if block['type'] == 'text':
                seen.text += block['text']
            elif block['type'] == 'thinking':
                seen.reasoning += block.get('thinking') or ''
            elif block['type'] == 'tool_use':
                seen.calls.append((block['id'], block['name'], block.get('input') or {}))
        seen.stop = _ANTHROPIC_SEEN.get(body.get('stop_reason'))
        usage = body.get('usage') or {}
        seen.input_tokens, seen.output_tokens = usage.get('input_tokens'), usage.get('output_tokens')
        return seen
    blocks: Dict[int, dict] = {}
    for event in _sse_data(response.text):
        kind = event.get('type')
        if kind == 'error' or event.get('error'):
            seen.error = json.dumps(event, ensure_ascii=False)
        elif kind == 'message_start':
            seen.input_tokens = (event['message'].get('usage') or {}).get('input_tokens')
        elif kind == 'content_block_start':
            blocks[event['index']] = {**event['content_block'], 'partial': ''}
        elif kind == 'content_block_delta':
            delta = event['delta']
            block = blocks[event['index']]
            if delta['type'] == 'text_delta':
                seen.text += delta['text']
            elif delta['type'] == 'thinking_delta':
                seen.reasoning += delta['thinking']
            elif delta['type'] == 'input_json_delta':
                block['partial'] += delta['partial_json']
        elif kind == 'message_delta':
            seen.stop = _ANTHROPIC_SEEN.get(event['delta'].get('stop_reason'))
            usage = event.get('usage') or {}
            seen.output_tokens = usage.get('output_tokens', seen.output_tokens)
            seen.input_tokens = usage.get('input_tokens', seen.input_tokens)
    seen.calls = [(block['id'], block['name'], _args(block['partial']) or block.get('input') or {})
                  for _, block in sorted(blocks.items()) if block['type'] == 'tool_use']
    return seen


def _read_responses_object(seen, body):
    refused = False
    for item in body.get('output') or []:
        if item['type'] == 'message':
            for part in item.get('content') or []:
                if part['type'] == 'output_text':
                    seen.text += part['text']
                elif part['type'] == 'refusal':
                    seen.text += part['refusal']
                    refused = True
        elif item['type'] == 'reasoning':
            seen.reasoning += ''.join(part.get('text', '') for part in item.get('summary') or [])
            seen.reasoning += ''.join(part.get('text', '') for part in item.get('content') or [])
        elif item['type'] == 'function_call':
            seen.calls.append((item['call_id'], item['name'], _args(item.get('arguments'))))
    reason = (body.get('incomplete_details') or {}).get('reason')
    if body.get('status') == 'failed':
        seen.error = json.dumps(body.get('error'), ensure_ascii=False)
    elif refused or reason == 'content_filter':
        seen.stop = 'refusal'
    elif reason == 'max_output_tokens':
        seen.stop = 'length'
    else:
        seen.stop = 'tool' if seen.calls else 'end'
    usage = body.get('usage') or {}
    seen.input_tokens, seen.output_tokens = usage.get('input_tokens'), usage.get('output_tokens')


def read_responses(response, stream):
    seen = Seen()
    if response.status_code >= 400:
        seen.error = response.text
        return seen
    if not stream:
        body = response.json()
        if body.get('error'):
            seen.error = json.dumps(body, ensure_ascii=False)
            return seen
        _read_responses_object(seen, body)
        return seen
    final = None
    for event in _sse_data(response.text):
        kind = event.get('type')
        if kind in ('response.completed', 'response.incomplete', 'response.failed'):
            final = event['response']
        elif kind == 'error' or event.get('error'):
            seen.error = json.dumps(event, ensure_ascii=False)
    if final is not None:
        _read_responses_object(seen, final)
    elif seen.error is None:
        seen.error = 'Responses 流没有终态事件'
    return seen


READERS = {'chat': read_chat, 'anthropic': read_anthropic, 'responses': read_responses}


# ---- 上游收到的正文 → 统一的对话序列 ----
# 每项是 ('user'|'assistant', 'text', 文本)、('reminder', 文本)、('call', id, 名字, 参数)
# 或 ('result', id, 文本, 是否报错)。系统提示单独返回；纯空白文本是网关为满足协议补的占位，不计入。

TOOL_ERROR_PREFIX = '[tool_result_error] '
_REMINDER_OPEN, _REMINDER_CLOSE = '<system-reminder>', '</system-reminder>'


def _text_item(role, text):
    stripped = text.strip()
    if stripped.startswith(_REMINDER_OPEN) and stripped.endswith(_REMINDER_CLOSE):
        return ('reminder', stripped[len(_REMINDER_OPEN):-len(_REMINDER_CLOSE)].strip())
    return (role, 'text', text)


def _plain(content):
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return _plain(content.get('text') or content.get('content') or '')
    if isinstance(content, list):
        return ''.join(_plain(part) for part in content if not isinstance(part, dict)
                       or part.get('type') in (None, 'text', 'input_text', 'output_text'))
    return '' if content is None else str(content)


def _result(call_id, text, is_error=False):
    if text.startswith(TOOL_ERROR_PREFIX):
        text, is_error = text[len(TOOL_ERROR_PREFIX):], True
    return ('result', call_id, text, bool(is_error))


def wire_chat(body):
    system, turns = '', []
    for message in body.get('messages') or []:
        role, content = message.get('role'), message.get('content')
        if role in ('system', 'developer'):
            if turns:
                turns.append(('reminder', _plain(content)))
            else:
                system += _plain(content)
        elif role == 'tool':
            turns.append(_result(message.get('tool_call_id'), _plain(content)))
        else:
            parts = content if isinstance(content, list) else [content]
            for part in parts:
                text = _plain(part)
                if text:
                    turns.append(_text_item(role, text))
            for call in message.get('tool_calls') or []:
                turns.append(('call', call['id'], call['function']['name'], json.loads(call['function']['arguments'] or '{}')))
    return system, turns


def wire_anthropic(body):
    system = _plain(body.get('system'))
    turns = []
    for message in body.get('messages') or []:
        role, content = message.get('role'), message.get('content')
        if role == 'system':
            turns.append(('reminder', _plain(content)))
            continue
        for block in content if isinstance(content, list) else [{'type': 'text', 'text': content}]:
            kind = block.get('type')
            if kind == 'text' and block.get('text', '').strip():
                turns.append(_text_item(role, block['text']))
            elif kind == 'tool_use':
                turns.append(('call', block['id'], block['name'], block.get('input') or {}))
            elif kind == 'tool_result':
                turns.append(_result(block['tool_use_id'], _plain(block.get('content')), block.get('is_error')))
    return system, turns


def wire_responses(body):
    system, turns = _plain(body.get('instructions')), []
    for item in body.get('input') or []:
        kind = item.get('type') or 'message'
        if kind == 'message':
            role = item.get('role')
            if role in ('system', 'developer'):
                if turns:
                    turns.append(('reminder', _plain(item.get('content'))))
                else:
                    system += _plain(item.get('content'))
            elif _plain(item.get('content')):
                turns.append(_text_item(role, _plain(item.get('content'))))
        elif kind == 'function_call':
            turns.append(('call', item['call_id'], item['name'], json.loads(item.get('arguments') or '{}')))
        elif kind == 'function_call_output':
            turns.append(_result(item['call_id'], _plain(item.get('output'))))
    return system, turns


def wire_gemini(body):
    system = ''.join(part.get('text', '') for part in (body.get('systemInstruction') or {}).get('parts') or [])
    turns = []
    for content in body.get('contents') or []:
        role = 'assistant' if content.get('role') == 'model' else 'user'
        for part in content.get('parts') or []:
            if part.get('text', '').strip() and not part.get('thought'):
                turns.append(_text_item(role, part['text']))
            elif 'functionCall' in part:
                call = part['functionCall']
                turns.append(('call', call.get('id'), call.get('name'), call.get('args') or {}))
            elif 'functionResponse' in part:
                response = part['functionResponse'].get('response') or {}
                is_error = 'error' in response
                turns.append(_result(part['functionResponse'].get('id'),
                                     _plain(response.get('error') if is_error else response.get('content')), is_error))
    return system, turns


def wire_interactions(body):
    system, turns = _plain(body.get('system_instruction')), []
    for step in body.get('input') or []:
        kind = step.get('type')
        if kind in ('user_input', 'model_output'):
            role = 'assistant' if kind == 'model_output' else 'user'
            for block in step.get('content') or []:
                if block.get('type') == 'text' and block.get('text', '').strip():
                    turns.append(_text_item(role, block['text']))
        elif kind == 'function_call':
            turns.append(('call', step.get('id'), step.get('name'), step.get('arguments') or {}))
        elif kind == 'function_result':
            turns.append(_result(step.get('call_id'), _plain(step.get('result')), step.get('is_error')))
    return system, turns


WIRE_READERS = {'chat': wire_chat, 'anthropic': wire_anthropic, 'responses': wire_responses,
                'gemini': wire_gemini, 'interactions': wire_interactions}


def upstream_body(fake):
    """假上游收到的最后一个请求正文。"""
    return fake.requests[-1]['body']
