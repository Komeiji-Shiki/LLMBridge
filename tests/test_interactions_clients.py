"""Interactions 上游的事件流接到各客户端协议的真实流式构建器，检查客户端最终看到什么。"""
import asyncio
import json

import pytest
from fastapi.responses import StreamingResponse

from converters.anthropic_openai import build_anthropic_streaming_response
from converters.gemini_interactions import InteractionsStreamConverter
from converters.responses_openai import build_responses_streaming_response
from core.request_context import RequestContext, current_request

TOOL_TURN = [
    {'event_type': 'step.start', 'index': 0, 'step': {'type': 'model_output'}},
    {'event_type': 'step.delta', 'index': 0, 'delta': {'type': 'text', 'text': '查一下'}},
    {'event_type': 'step.stop', 'index': 0},
    {'event_type': 'step.start', 'index': 1, 'step': {'type': 'function_call', 'id': 'a', 'name': 'f', 'arguments': {}}},
    {'event_type': 'step.delta', 'index': 1, 'delta': {'type': 'arguments_delta', 'arguments': '{"x": 1}'}},
    {'event_type': 'step.stop', 'index': 1},
    {'event_type': 'step.start', 'index': 2, 'step': {'type': 'function_call', 'id': 'b', 'name': 'g', 'arguments': {}}},
    {'event_type': 'step.delta', 'index': 2, 'delta': {'type': 'arguments_delta', 'arguments': '{"y": 2}'}},
    {'event_type': 'step.stop', 'index': 2},
    {'event_type': 'interaction.completed', 'interaction': {
        'status': 'requires_action', 'usage': {'total_input_tokens': 10, 'total_output_tokens': 4, 'total_tokens': 14}}},
]


@pytest.fixture(autouse=True)
def context():
    token = current_request.set(RequestContext())
    yield
    current_request.reset(token)


def _chat_stream(events):
    converter = InteractionsStreamConverter('m', 'req')
    chunks = [chunk for event in events for chunk in converter.feed(event)]

    async def body():
        for chunk in chunks:
            yield f'data: {json.dumps(chunk, ensure_ascii=False)}\n\n'
        yield 'data: [DONE]\n\n'
    return StreamingResponse(body(), media_type='text/event-stream')


def _sse_payloads(response):
    async def collect():
        return [item async for item in response.body_iterator]
    payloads = []
    for raw in asyncio.run(collect()):
        text = raw.decode() if isinstance(raw, bytes) else raw
        payloads.extend(json.loads(line[6:]) for line in text.splitlines()
                        if line.startswith('data: ') and line != 'data: [DONE]')
    return payloads


def test_anthropic_client_receives_separate_tool_blocks_and_tool_use_stop():
    events = _sse_payloads(build_anthropic_streaming_response(_chat_stream(TOOL_TURN), 'm'))
    blocks = {}
    for event in events:
        if event.get('type') == 'content_block_start':
            blocks[event['index']] = {**event['content_block'], 'partial': ''}
        elif event.get('type') == 'content_block_delta':
            delta = event['delta']
            blocks[event['index']]['partial'] += delta.get('partial_json') or delta.get('text') or ''
    tools = [block for block in blocks.values() if block['type'] == 'tool_use']
    assert [tool['name'] for tool in tools] == ['f', 'g']
    assert [json.loads(tool['partial']) for tool in tools] == [{'x': 1}, {'y': 2}]
    assert next(block for block in blocks.values() if block['type'] == 'text')['partial'] == '查一下'
    assert next(event for event in events if event.get('type') == 'message_delta')['delta']['stop_reason'] == 'tool_use'


def test_responses_client_receives_each_function_call():
    request = {'model': 'm', 'input': 'q', 'stream': True}
    events = _sse_payloads(build_responses_streaming_response(_chat_stream(TOOL_TURN), request, 'm'))
    completed = next(event for event in events if event.get('type') == 'response.completed')['response']
    calls = [item for item in completed['output'] if item['type'] == 'function_call']
    assert [(call['call_id'], call['name']) for call in calls] == [('a', 'f'), ('b', 'g')]
    assert [json.loads(call['arguments']) for call in calls] == [{'x': 1}, {'y': 2}]
    assert completed['usage']['input_tokens'] == 10


def test_clients_see_the_failure_instead_of_a_blank_success():
    failed = [
        {'event_type': 'step.start', 'index': 0, 'step': {'type': 'model_output'}},
        {'event_type': 'step.delta', 'index': 0, 'delta': {'type': 'text', 'text': '半截'}},
        {'event_type': 'step.stop', 'index': 0},
        {'event_type': 'interaction.completed', 'interaction': {
            'status': 'failed', 'errors': [{'code': 'internal', 'message': '上游炸了'}]}},
    ]
    anthropic = _sse_payloads(build_anthropic_streaming_response(_chat_stream(failed), 'm'))
    assert any(event.get('type') == 'error' and '上游炸了' in json.dumps(event, ensure_ascii=False) for event in anthropic)
    assert not any(event.get('type') == 'message_delta' and event['delta'].get('stop_reason') == 'end_turn'
                   for event in anthropic)
    responses = _sse_payloads(build_responses_streaming_response(_chat_stream(failed), {'model': 'm', 'input': 'q'}, 'm'))
    assert any(event.get('type') == 'response.failed' for event in responses)
    assert not any(event.get('type') == 'response.completed' for event in responses)
