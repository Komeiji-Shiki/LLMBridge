"""上游只在流尾报用量时，Anthropic message_start 按上一轮的真实输入量给出估计。"""
import asyncio
import json

import pytest
from fastapi.responses import StreamingResponse

from converters.anthropic_openai import build_anthropic_streaming_response
from converters.gemini_interactions import InteractionsStreamConverter
from core.request_context import RequestContext, current_request
from services import prompt_usage_hint
from services.prompt_usage_hint import PromptUsageHint
from utils.usage_tokens import rough_token_count


def _turn(text, input_tokens, cached_tokens):
    return [
        {'event_type': 'step.start', 'index': 0, 'step': {'type': 'model_output'}},
        {'event_type': 'step.delta', 'index': 0, 'delta': {'type': 'text', 'text': text}},
        {'event_type': 'step.stop', 'index': 0},
        {'event_type': 'interaction.completed', 'interaction': {'status': 'completed', 'usage': {
            'total_input_tokens': input_tokens, 'total_cached_tokens': cached_tokens,
            'total_output_tokens': 3, 'total_tokens': input_tokens + 3}}},
    ]


@pytest.fixture(autouse=True)
def context():
    prompt_usage_hint._known_inputs.clear()
    token = current_request.set(RequestContext())
    yield
    current_request.reset(token)


def _usage(messages, events):
    converter = InteractionsStreamConverter('m', 'req')
    chunks = [chunk for event in events for chunk in converter.feed(event)]

    async def body():
        for chunk in chunks:
            yield f'data: {json.dumps(chunk, ensure_ascii=False)}\n\n'
        yield 'data: [DONE]\n\n'

    response = build_anthropic_streaming_response(StreamingResponse(body(), media_type='text/event-stream'), 'm',
                                                  prompt_hint=PromptUsageHint('m', messages))

    async def collect():
        return [item async for item in response.body_iterator]
    payloads = [json.loads(line[6:]) for raw in asyncio.run(collect())
                for line in (raw.decode() if isinstance(raw, bytes) else raw).splitlines() if line.startswith('data: ')]
    start = next(item for item in payloads if item.get('type') == 'message_start')['message']['usage']
    delta = next(item for item in payloads if item.get('type') == 'message_delta')['usage']
    return start, delta


FIRST = [{'role': 'user', 'content': [{'type': 'text', 'text': '读一下文件', 'cache_control': {'type': 'ephemeral'}}]}]
REPLY = {'role': 'assistant', 'content': [
    {'type': 'thinking', 'thinking': '很长的思考' * 50, 'signature': 's'},
    {'type': 'tool_use', 'id': 't1', 'name': 'Read', 'input': {'path': 'a.txt'}}]}
RESULT = {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1',
                                       'content': [{'type': 'text', 'text': 'hello world'}],
                                       'cache_control': {'type': 'ephemeral'}}]}


def test_next_request_starts_with_previous_real_input_plus_appended_part():
    start, delta = _usage(FIRST, _turn('好', 1000, 0))
    assert start['input_tokens'] == 0
    assert delta['input_tokens'] == 1000

    # cache_control 每轮挪到最后一条消息上，不影响前缀
    follow_up = [{'role': 'user', 'content': [{'type': 'text', 'text': '读一下文件'}]}, REPLY, RESULT]
    start, delta = _usage(follow_up, _turn('完成', 1200, 900))
    appended = rough_token_count(json.dumps({'path': 'a.txt'})) + rough_token_count('hello world')
    assert start == {'input_tokens': 1000 + appended, 'cache_creation_input_tokens': 0,
                     'cache_read_input_tokens': 0, 'output_tokens': 0}
    assert (delta['input_tokens'], delta['cache_read_input_tokens']) == (300, 900)


def test_changed_history_gets_no_estimate():
    _usage(FIRST, _turn('好', 1000, 0))
    edited = [{'role': 'user', 'content': '换了一个问题'}, REPLY, RESULT]
    start, _ = _usage(edited, _turn('完成', 1200, 0))
    assert start['input_tokens'] == 0


def test_missing_final_usage_is_not_remembered():
    _usage(FIRST, _turn('好', 1000, 0))
    PromptUsageHint('m', [*FIRST, REPLY, RESULT]).remember({'input_tokens': 0, 'cache_read_input_tokens': 0})
    # 中间一轮没有记录，退回到第一轮的真实输入量往后估
    hint = PromptUsageHint('m', [*FIRST, REPLY, RESULT, REPLY])
    assert hint.input_tokens == 1000 + rough_token_count('hello world') + 2 * rough_token_count(json.dumps({'path': 'a.txt'}))
