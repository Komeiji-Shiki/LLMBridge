"""Gemini Long Decode 续传：请求原样重发、用量合并、异常不伪装成正常结束。"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.exchange_archive import upstream_snapshot
from core.request_context import RequestContext, current_request
from services.direct_api_service import DirectAPIService
from services.gemini_continuation import GeminiContinuation
from services.gemini_history import GeminiTurnAssembler
from services.protocol_events import gemini_finish_to_openai, gemini_termination
from utils.api_pricing import calculate_api_cost

MESSAGES = [{'role': 'user', 'content': '写长文'}]


def chunk(text=None, finish=None, token=None, usage=None):
    candidate = {}
    if text is not None:
        candidate['content'] = {'role': 'model', 'parts': [{'text': text}]}
    if finish:
        candidate['finishReason'] = finish
    if token:
        candidate['continuationToken'] = token
    value = {'candidates': [candidate]}
    if usage:
        value['usageMetadata'] = usage
    return value


def usage(prompt=100, cached=0, candidates=0, thoughts=0):
    return {'promptTokenCount': prompt, 'cachedContentTokenCount': cached, 'candidatesTokenCount': candidates,
            'thoughtsTokenCount': thoughts, 'totalTokenCount': prompt + candidates + thoughts}


def session_with(*hops):
    """每个 hop 是一个流式事件列表、非流式响应 dict，或 (HTTP 状态码, 正文)。"""
    responses = []
    for hop in hops:
        if isinstance(hop, tuple):
            response = MagicMock(status=hop[0])
            response.text = AsyncMock(return_value=hop[1])
        else:
            response = MagicMock(status=200)
            response.json = AsyncMock(return_value=hop)

            async def stream(events=hop):
                for event in events:
                    yield ('data: ' + json.dumps(event) + '\n\n').encode()

            response.content.iter_any = stream
        responses.append(response)
    session = MagicMock()
    session.post.return_value.__aenter__ = AsyncMock(side_effect=responses)
    session.post.return_value.__aexit__ = AsyncMock(return_value=False)
    return session


def bodies(session):
    return [json.loads(call.kwargs['data']) for call in session.post.call_args_list]


def collect(session, stream=True, **kwargs):
    async def run():
        return [item async for item in DirectAPIService(session).call_gemini_native_api(
            'test-key', 'demo', MESSAGES, stream=stream, max_tokens=262144, **kwargs)]
    return asyncio.run(run())


def text_of(chunks):
    return ''.join(part.get('text', '') for item in chunks for candidate in item.get('candidates', [])
                   for part in (candidate.get('content') or {}).get('parts', []))


@pytest.mark.parametrize('protocol', ['generate_content', 'interactions'])
def test_converted_request_is_archived_without_inline_media_bytes(protocol):
    data = 'QUJD' * 1000
    messages = [{'role': 'user', 'content': [
        {'type': 'text', 'text': '听这段'},
        {'type': 'input_audio', 'input_audio': {'data': data, 'format': 'wav'}},
    ]}]
    session = MagicMock()
    response = MagicMock(status=200)
    response.json = AsyncMock(return_value={'candidates': [], 'id': 'test', 'status': 'completed', 'steps': []})
    session.post.return_value.__aenter__ = AsyncMock(return_value=response)
    session.post.return_value.__aexit__ = AsyncMock(return_value=False)
    context = RequestContext(owner_id='owner')
    service = DirectAPIService(session)
    method = service.call_gemini_native_api if protocol == 'generate_content' else service.call_gemini_interactions_api

    async def run():
        token = current_request.set(context)
        try:
            return [item async for item in method('test-key', 'demo', messages)]
        finally:
            current_request.reset(token)

    assert not any('error' in item for item in asyncio.run(run()))
    sent = json.loads(session.post.call_args.kwargs['data'])
    stored = json.dumps(context.upstream_request, ensure_ascii=False)
    assert data in session.post.call_args.kwargs['data'].decode()
    assert data not in stored
    assert '"omitted_chars": 4000' in stored
    # 除媒体内容外，归档的就是发往上游的正文
    assert context.upstream_request == upstream_snapshot(sent)


def rating(category, probability, score=None, **extra):
    value = {'category': category, 'probability': probability, 'severity': 'HARM_SEVERITY_LOW', **extra}
    return value if score is None else {**value, 'probabilityScore': score}


def test_safety_block_saves_reason_and_highest_scores_for_each_category():
    low = rating('HARM_CATEGORY_HARASSMENT', 'LOW', .2)
    high = rating('HARM_CATEGORY_HARASSMENT', 'HIGH', .9)
    blocked = rating('HARM_CATEGORY_SEXUALLY_EXPLICIT', 'MEDIUM', .6, blocked=True)
    first = chunk('一半')
    first['candidates'][0]['safetyRatings'] = [high]
    last = chunk(finish='SAFETY', usage={'promptTokenCount': 5})
    last['candidates'][0]['safetyRatings'] = [low, blocked]
    session = session_with([first, last])
    context = RequestContext(owner_id='owner')

    async def run():
        token = current_request.set(context)
        try:
            return [item async for item in DirectAPIService(session).call_gemini_native_api(
                'test-key', 'demo', MESSAGES, stream=True)]
        finally:
            current_request.reset(token)

    error = asyncio.run(run())[-1]['error']
    assert error['message'] == 'Gemini 上游终止生成：SAFETY（HARM_CATEGORY_SEXUALLY_EXPLICIT: MEDIUM）'
    assert context.outcome['safety'] == {
        'blocked': {'source': 'response', 'reason': 'SAFETY', 'message': None},
        'response_ratings': {'HARM_CATEGORY_HARASSMENT': high, 'HARM_CATEGORY_SEXUALLY_EXPLICIT': blocked},
    }


def test_prompt_block_is_attributed_to_the_prompt():
    from services.protocol_events import merge_gemini_safety
    ratings = [rating('HARM_CATEGORY_HATE_SPEECH', 'NEGLIGIBLE')]
    details = merge_gemini_safety(None, {'promptFeedback': {
        'blockReason': 'PROHIBITED_CONTENT', 'blockReasonMessage': '说明', 'safetyRatings': ratings}})
    assert details == {'blocked': {'source': 'prompt', 'reason': 'PROHIBITED_CONTENT', 'message': '说明'},
                       'prompt_ratings': {'HARM_CATEGORY_HATE_SPEECH': ratings[0]}}


def test_passing_response_keeps_scores_without_a_block():
    from services.protocol_events import merge_gemini_safety
    passing = rating('HARM_CATEGORY_HARASSMENT', 'NEGLIGIBLE', .002)
    value = chunk('好', 'STOP')
    value['candidates'][0]['safetyRatings'] = [passing]
    assert merge_gemini_safety(None, value) == {'response_ratings': {'HARM_CATEGORY_HARASSMENT': passing}}
    assert merge_gemini_safety(None, chunk('好')) is None
    assert merge_gemini_safety(None, {'candidates': ['oops'], 'promptFeedback': 'bad'}) is None


def test_stream_resends_original_request_with_only_the_latest_token():
    session = session_with(
        [chunk('甲'), chunk('乙', 'CONTINUATION', 'token-1', usage(candidates=10, thoughts=5))],
        [chunk('丙', 'CONTINUATION', 'token-2', usage(cached=80, candidates=3))],
        [chunk('丁', 'STOP', usage=usage(cached=90, candidates=2))],
    )
    result = collect(session, tools=[{'type': 'function', 'function': {'name': 'lookup', 'parameters': {}}}])

    first, second, third = bodies(session)
    assert 'continuationToken' not in first
    assert second == {**first, 'continuationToken': 'token-1'}
    assert third == {**first, 'continuationToken': 'token-2'}
    assert second['generationConfig']['maxOutputTokens'] == 262144
    assert text_of(result) == '甲乙丙丁'
    assert [c.get('finishReason') for item in result for c in item['candidates'] if c.get('finishReason')] == ['STOP']


def test_stream_usage_sums_hops_but_reports_first_prompt():
    session = session_with(
        [chunk('a', usage=usage(candidates=4)), chunk('b', 'CONTINUATION', 'token-1', usage(candidates=10, thoughts=5))],
        [chunk('c', usage=usage(cached=80, candidates=1)), chunk('d', 'STOP', usage=usage(cached=80, candidates=3))],
    )
    final = collect(session)[-1]['usageMetadata']
    # 第二跳内部累计，只取最后值；跨跳才相加
    assert final['candidatesTokenCount'] == 13
    assert final['thoughtsTokenCount'] == 5
    assert final['promptTokenCount'] == 100 and final['cachedContentTokenCount'] == 0
    assert final['totalTokenCount'] == 118
    assert final['billedPromptTokenCount'] == 200 and final['billedCachedTokenCount'] == 80
    assert final['continuationHops'] == 1


def test_non_stream_merges_parts_from_all_hops():
    session = session_with(
        chunk('上半', 'CONTINUATION', 'token-1', usage(candidates=32768)),
        chunk('下半', 'STOP', usage=usage(candidates=8)),
    )
    result = collect(session, stream=False)
    assert len(result) == 1
    openai = DirectAPIService(MagicMock()).convert_gemini_response_to_openai(result[0], 'demo', 'r')
    assert openai['choices'][0]['message']['content'] == '上半下半'
    assert openai['choices'][0]['finish_reason'] == 'stop'
    assert openai['usage']['completion_tokens'] == 32776
    assert bodies(session)[1]['continuationToken'] == 'token-1'


def test_single_hop_response_is_left_untouched():
    response = chunk('完整', 'STOP', usage=usage(candidates=2))
    original = json.loads(json.dumps(response))
    assert collect(session_with([response]))[0] == original
    assert collect(session_with(json.loads(json.dumps(original))), stream=False)[0] == original


@pytest.mark.parametrize('hop', [
    chunk('截断', 'CONTINUATION'),
    chunk('截断', 'CONTINUATION', 'token-1'),
], ids=['missing-token', 'hops-disabled'])
def test_unresumable_continuation_is_reported_as_truncated(hop):
    session = session_with([hop])
    with patch.dict('services.direct_api_service.CONFIG', {'gemini_max_continuation_hops': 0}):
        result = collect(session)
    assert session.post.call_count == 1
    assert result[-1]['candidates'][0]['finishReason'] == 'CONTINUATION'
    converted = DirectAPIService(MagicMock()).convert_gemini_response_to_openai(result[-1], 'demo', 'r', is_stream_chunk=True)
    assert converted['choices'][0]['finish_reason'] == 'length'


def test_repeated_token_stops_instead_of_looping():
    session = session_with(
        [chunk('a', 'CONTINUATION', 'same')],
        [chunk(None, 'CONTINUATION', 'same')],
    )
    result = collect(session)
    assert session.post.call_count == 2
    assert result[-1]['candidates'][0]['finishReason'] == 'CONTINUATION'


def test_stale_checkpoint_token_is_not_used_to_resume():
    """检查点之后又输出了内容，用旧 token 续传会重复这段内容。"""
    session = session_with([chunk('a', token='checkpoint'), chunk('b'), chunk(None, 'CONTINUATION')])
    result = collect(session)
    assert session.post.call_count == 1
    assert result[-1]['candidates'][0]['finishReason'] == 'CONTINUATION'


def test_token_arriving_just_before_finish_chunk_is_used():
    session = session_with([chunk('a', token='token-1'), chunk(None, 'CONTINUATION')], [chunk('b', 'STOP')])
    collect(session)
    assert bodies(session)[1]['continuationToken'] == 'token-1'


def test_later_hop_failure_surfaces_error_after_streamed_output():
    session = session_with(
        [chunk('a', 'CONTINUATION', 'token-1', usage(candidates=10))],
        (500, json.dumps({'error': {'message': 'boom', 'code': 500}})),
    )
    result = collect(session)
    assert text_of(result[:-1]) == 'a'
    assert result[-1]['error']['message'] == 'boom'


def test_later_hop_failure_keeps_billed_usage_in_non_stream():
    session = session_with(
        chunk('a', 'CONTINUATION', 'token-1', usage(candidates=10)),
        (503, ''),
    )
    result = collect(session, stream=False)
    assert len(result) == 1 and result[0]['error']['code'] == 503
    assert result[0]['usageMetadata']['candidatesTokenCount'] == 10
    assert result[0]['usageMetadata']['billedPromptTokenCount'] == 100


def test_second_hop_generation_block_is_reported_as_error():
    session = session_with(
        [chunk('a', 'CONTINUATION', 'token-1')],
        [chunk(None, 'PROHIBITED_CONTENT')],
    )
    assert collect(session)[-1]['error']['reason'] == 'PROHIBITED_CONTENT'


def test_retry_executor_treats_resumed_hops_as_one_stream():
    """中间跳不是终态：第二跳半路断开要报错，不能当成正常结束。"""
    async def run(hops):
        context = RequestContext()
        token = current_request.set(context)
        try:
            session = session_with(*hops)
            result = [item async for item in DirectAPIService(session).call_gemini_native_api(
                'test-key', 'demo', MESSAGES, stream=True)]
            return result, context
        finally:
            current_request.reset(token)

    complete, context = asyncio.run(run([[chunk('a', 'CONTINUATION', 'token-1')], [chunk('b', 'STOP')]]))
    assert text_of(complete) == 'ab' and not any('error' in item for item in complete)
    assert context.outcome['stop_reason'] == 'STOP'

    broken, _ = asyncio.run(run([[chunk('a', 'CONTINUATION', 'token-1')], [chunk('b')]]))
    assert text_of(broken) == 'ab'
    assert '终态' in broken[-1]['error']['message']


def test_thought_history_sees_one_complete_turn():
    continuation = GeminiContinuation(max_hops=2)
    assembler = GeminiTurnAssembler()
    for value in (
        chunk('前半', 'CONTINUATION', 'token-1'),
        {'candidates': [{'content': {'parts': [{'text': '后半', 'thoughtSignature': 'sig'}]}, 'finishReason': 'STOP'}]},
    ):
        continuation.feed(value)
        assembler.feed(value)
    turn = assembler.candidates[0]
    assert turn.finish_reason == 'STOP'
    assert ''.join(part['text'] for part in turn.parts) == '前半后半'


@pytest.mark.parametrize('reason,finish', [
    ('STOP', 'stop'), ('MAX_TOKENS', 'length'), ('CONTINUATION', 'length'),
    ('SAFETY', 'content_filter'), ('RECITATION', 'content_filter'), ('PROHIBITED_CONTENT', 'content_filter'),
    ('MALFORMED_FUNCTION_CALL', None), ('OTHER', None), ('TOOL_USE', None), ('SOME_FUTURE_REASON', None),
])
def test_finish_reason_table(reason, finish):
    assert gemini_finish_to_openai(reason) == finish


@pytest.mark.parametrize('reason', ['MALFORMED_FUNCTION_CALL', 'OTHER', 'TOOL_USE', 'SOME_FUTURE_REASON'])
def test_unknown_or_abnormal_reason_never_looks_like_normal_stop(reason):
    value = {'candidates': [{'finishReason': reason}]}
    assert gemini_termination(value)[1]['reason'] == reason
    converted = DirectAPIService(MagicMock()).convert_gemini_response_to_openai(value, 'demo', 'r', is_stream_chunk=True)
    assert converted['choices'][0]['finish_reason'] is None


@pytest.mark.parametrize('reason', ['STOP', 'MAX_TOKENS', 'CONTINUATION', 'FINISH_REASON_UNSPECIFIED'])
def test_normal_reasons_are_not_errors(reason):
    assert gemini_termination({'candidates': [{'finishReason': reason}]}) == (reason, None)


def test_response_conversion_failure_is_not_swallowed():
    with pytest.raises(AttributeError):
        DirectAPIService(MagicMock()).convert_gemini_response_to_openai(
            {'candidates': [{'content': {'parts': [{'functionCall': None}]}}]}, 'demo', 'r')


def test_pricing_tier_uses_single_request_input():
    pricing = {'input': 1, 'output': 0, 'unit': 1, 'input_tiers': [{'min_input_tokens': 150, 'input': 10}]}
    assert calculate_api_cost(200, 0, pricing)['input_cost'] == 2000
    assert calculate_api_cost(200, 0, pricing, tier_input_tokens=100)['input_cost'] == 200


def test_native_gemini_client_receives_token_untouched():
    """原生协议客户端自己决定是否续传，网关不能吞掉 token。"""
    from services.native_exchange import forward_native_exchange

    async def run():
        service = MagicMock()
        service.calculate_cost.return_value = {}
        monitor = MagicMock()
        monitor.broadcast_to_monitors = AsyncMock()
        upstream_response = chunk('一段', 'CONTINUATION', 'token-1')

        async def upstream(**kwargs):
            yield json.dumps(upstream_response).encode()

        service.call_api_passthrough = upstream
        config = {'provider': 'gemini', 'api_type': 'gemini_native', 'api_key': 'test',
                  'api_base_url': 'https://example.test'}
        response = await forward_native_exchange({'contents': []}, config, 'alias', service, monitor)
        return response.status_code, json.loads(response.body)

    status, body = asyncio.run(run())
    assert status == 200
    assert body['candidates'][0]['continuationToken'] == 'token-1'
    assert body['candidates'][0]['finishReason'] == 'CONTINUATION'


# ---- Interactions：网关替 Chat 中转的客户端跑续传循环 ----

def hop_usage(inputs=100, cached=0, output=0, thought=0):
    return {'total_input_tokens': inputs, 'total_cached_tokens': cached, 'total_output_tokens': output,
            'total_thought_tokens': thought, 'total_tokens': inputs + output + thought}


def hop_events(text, status='completed', token=None, usage_value=None, start=0):
    interaction = {'id': 'hop', 'status': status, 'usage': usage_value or hop_usage()}
    if token:
        interaction['continuation_token'] = token
    return [{'event_type': 'interaction.created', 'interaction': {'id': 'hop', 'status': 'in_progress'}},
            {'event_type': 'step.start', 'index': start, 'step': {'type': 'model_output'}},
            {'event_type': 'step.delta', 'index': start, 'delta': {'type': 'text', 'text': text}},
            {'event_type': 'step.stop', 'index': start},
            {'event_type': 'interaction.completed', 'interaction': interaction}]


def hop_response(steps, status='completed', token=None, usage_value=None):
    value = {'id': 'hop', 'status': status, 'steps': steps, 'usage': usage_value or hop_usage()}
    if token:
        value['continuation_token'] = token
    return value


def output_step(text):
    return {'type': 'model_output', 'content': [{'type': 'text', 'text': text}]}


def collect_interactions(session, stream=True, **kwargs):
    async def run():
        return [item async for item in DirectAPIService(session).call_gemini_interactions_api(
            'test-key', 'gemini-4-argon', MESSAGES, stream=stream, max_tokens=262144, **kwargs)]
    return asyncio.run(run())


def to_chat(events):
    from converters.gemini_interactions import InteractionsStreamConverter
    converter = InteractionsStreamConverter('m', 'r')
    return [chunk for event in events for chunk in converter.feed(event)]


def chat_text(chunks):
    return ''.join(chunk['choices'][0]['delta'].get('content') or '' for chunk in chunks if chunk.get('choices'))


def finishes(chunks):
    return [chunk['choices'][0]['finish_reason'] for chunk in chunks
            if chunk.get('choices') and chunk['choices'][0].get('finish_reason')]


def test_interactions_stream_hops_read_as_one_stream_and_resend_the_original_request():
    session = session_with(hop_events('第一跳', 'requires_continuation', 'token-1'),
                           hop_events('第二跳', 'requires_continuation', 'token-2'),
                           hop_events('第三跳'))
    events = collect_interactions(session)
    sent = bodies(session)
    assert [body.get('continuation_token') for body in sent] == [None, 'token-1', 'token-2']
    # 无状态模式每跳都重发原始提示，除 token 外不变
    assert all(body['store'] is False for body in sent)
    assert {key: value for key, value in sent[2].items() if key != 'continuation_token'} == sent[0]
    # 中间跳的结束事件不能让下游以为回答完了
    assert [event['interaction']['status'] for event in events
            if event.get('event_type') == 'interaction.completed'] == ['completed']
    chunks = to_chat(events)
    assert chat_text(chunks) == '第一跳第二跳第三跳'
    assert finishes(chunks) == ['stop']


def test_interactions_stream_usage_sums_hops_but_reports_first_prompt():
    session = session_with(
        hop_events('一', 'requires_continuation', 't1', hop_usage(100, 0, 50, 10)),
        hop_events('二', 'completed', None, hop_usage(160, 100, 30, 5)))
    events = collect_interactions(session)
    usage_value = next(event['interaction']['usage'] for event in events
                       if event.get('event_type') == 'interaction.completed')
    assert usage_value['total_input_tokens'] == 260 and usage_value['billedPromptTokenCount'] == 260
    assert usage_value['billedCachedTokenCount'] == 100
    chat_usage = next(chunk['usage'] for chunk in to_chat(events) if chunk.get('usage'))
    assert chat_usage['prompt_tokens'] == 100
    assert chat_usage['completion_tokens'] == 95
    assert chat_usage['total_tokens'] == 195


def test_interactions_non_stream_merges_steps_from_all_hops():
    from converters.gemini_interactions import convert_interactions_to_openai_response
    session = session_with(
        hop_response([{'type': 'thought', 'signature': 'sig-1', 'summary': [{'type': 'text', 'text': '想'}]},
                      output_step('第一跳')], 'requires_continuation', 't1', hop_usage(100, 0, 50, 10)),
        hop_response([output_step('第二跳'), {'type': 'function_call', 'id': 'c', 'name': 'f', 'arguments': {'a': 1}}],
                     'requires_action', None, hop_usage(160, 100, 30, 5)))
    result = collect_interactions(session, stream=False)
    assert len(result) == 1
    assert bodies(session)[1]['continuation_token'] == 't1'
    response = convert_interactions_to_openai_response(result[0], 'm', 'r')
    choice = response['choices'][0]
    assert choice['message']['content'] == '第一跳第二跳'
    assert choice['message']['reasoning_content'] == '想'
    assert choice['finish_reason'] == 'tool_calls'
    assert [step['type'] for step in choice['message']['provider_metadata']['interactions_steps']] == [
        'thought', 'model_output', 'model_output', 'function_call']
    assert response['usage']['prompt_tokens'] == 100 and response['usage']['completion_tokens'] == 95


def test_single_hop_interaction_is_left_untouched():
    value = hop_response([output_step('一次完成')], 'completed', None, hop_usage(10, 0, 5))
    session = session_with(value)
    assert collect_interactions(session, stream=False) == [value]
    assert len(bodies(session)) == 1


def test_interactions_without_a_token_is_reported_as_truncated():
    from core.conversion_report import conversion_notes
    token = current_request.set(RequestContext())
    try:
        chunks = to_chat(collect_interactions(session_with(hop_events('一', 'requires_continuation'))))
        assert finishes(chunks) == ['length']
        assert any(note['field'] == 'continuation' for note in conversion_notes())
    finally:
        current_request.reset(token)


def test_interactions_repeated_token_stops_instead_of_looping():
    session = session_with(hop_events('一', 'requires_continuation', 'same'),
                           hop_events('二', 'requires_continuation', 'same'))
    chunks = to_chat(collect_interactions(session))
    assert len(bodies(session)) == 2 and finishes(chunks) == ['length']


def test_interactions_hop_limit_stops_with_length():
    session = session_with(hop_events('一', 'requires_continuation', 't1'),
                           hop_events('二', 'requires_continuation', 't2'))
    with patch.dict('services.direct_api_service.CONFIG', {'gemini_max_continuation_hops': 1}):
        chunks = to_chat(collect_interactions(session))
    assert len(bodies(session)) == 2 and finishes(chunks) == ['length']
    assert chat_text(chunks) == '一二'


def test_interactions_later_hop_failure_keeps_output_and_billed_usage():
    session = session_with(hop_events('已输出', 'requires_continuation', 't1', hop_usage(100, 0, 50)),
                           (503, json.dumps({'error': {'message': '过载', 'code': 503}})))
    events = collect_interactions(session)
    assert chat_text(to_chat(events[:-1])) == '已输出'
    assert events[-1]['error']['message'] == '过载'
    assert events[-1]['usageMetadata']['promptTokenCount'] == 100
    assert events[-1]['usageMetadata']['billedPromptTokenCount'] == 100
    assert events[-1]['usageMetadata']['candidatesTokenCount'] == 50


def test_interactions_later_hop_failed_status_is_an_error_with_billed_usage():
    session = session_with(hop_events('已输出', 'requires_continuation', 't1', hop_usage(100, 0, 50)),
                           hop_events('', 'failed', None, hop_usage(150, 100, 0)))
    chunks = to_chat(collect_interactions(session))
    assert chunks[-1]['error']['code'] == 502
    assert chunks[-1]['usageMetadata']['billedPromptTokenCount'] == 250
    assert finishes(chunks) == []


def test_interactions_non_stream_later_hop_failure_keeps_billed_usage():
    session = session_with(hop_response([output_step('一')], 'requires_continuation', 't1', hop_usage(100, 0, 50)),
                           (500, json.dumps({'error': {'message': '炸了', 'code': 500}})))
    result = collect_interactions(session, stream=False)
    assert result[0]['error']['message'] == '炸了'
    assert result[0]['usageMetadata']['billedPromptTokenCount'] == 100


def _dropping_session(*hops):
    """每个 hop 是 (事件列表, 是否在发完后断线)，断线用 ClientPayloadError 模拟。"""
    import aiohttp
    responses = []
    for events, drop in hops:
        response = MagicMock(status=200)

        async def stream(events=events, drop=drop):
            for event in events:
                yield ('data: ' + json.dumps(event) + '\n\n').encode()
            if drop:
                raise aiohttp.ClientPayloadError('连接中断')

        response.content.iter_any = stream
        responses.append(response)
    session = MagicMock()
    session.post.return_value.__aenter__ = AsyncMock(side_effect=responses)
    session.post.return_value.__aexit__ = AsyncMock(return_value=False)
    return session


def test_interactions_disconnect_at_checkpoint_resumes_without_duplicates():
    first = [{'event_type': 'step.start', 'index': 0, 'step': {'type': 'model_output'}},
             {'event_type': 'step.delta', 'index': 0, 'delta': {'type': 'text', 'text': '检查点之前'}},
             {'event_type': 'interaction.status_update', 'status': 'in_progress', 'continuation_token': 'checkpoint',
              'usage': hop_usage(100, 0, 40)}]
    session = _dropping_session((first, True), (hop_events('接着写'), False))
    chunks = to_chat(collect_interactions(session))
    assert bodies(session)[1]['continuation_token'] == 'checkpoint'
    assert chat_text(chunks) == '检查点之前接着写'
    assert finishes(chunks) == ['stop']


def test_interactions_disconnect_after_new_output_reports_error_instead_of_duplicating():
    first = [{'event_type': 'step.start', 'index': 0, 'step': {'type': 'model_output'}},
             {'event_type': 'interaction.status_update', 'status': 'in_progress', 'continuation_token': 'checkpoint'},
             {'event_type': 'step.delta', 'index': 0, 'delta': {'type': 'text', 'text': '检查点之后的新内容'}}]
    session = _dropping_session((first, True))
    events = collect_interactions(session)
    assert len(bodies(session)) == 1
    assert 'error' in events[-1]


def test_interactions_retry_executor_treats_resumed_hops_as_one_stream():
    session = session_with(hop_events('一', 'requires_continuation', 't1'), hop_events('二'))
    context = RequestContext(endpoint={'auto_retry': {'enabled': True, 'max_retries': 2, 'retry_on_other_errors': True}})
    token = current_request.set(context)
    try:
        events = collect_interactions(session)
    finally:
        current_request.reset(token)
    assert len(bodies(session)) == 2 and len(context.attempts) == 1
    assert chat_text(to_chat(events)) == '一二'


def test_interactions_thought_signatures_from_every_hop_survive_for_replay():
    from converters.gemini_interactions import convert_oai_messages_to_interactions

    def thought(text, signature):
        return [{'event_type': 'step.start', 'index': 0, 'step': {'type': 'thought'}},
                {'event_type': 'step.delta', 'index': 0,
                 'delta': {'type': 'thought_summary', 'content': {'type': 'text', 'text': text}}},
                {'event_type': 'step.delta', 'index': 0, 'delta': {'type': 'thought_signature', 'signature': signature}},
                {'event_type': 'step.stop', 'index': 0}]

    first = [*thought('甲', 's1'), {'event_type': 'interaction.completed', 'interaction': {
        'status': 'requires_continuation', 'continuation_token': 't1', 'usage': hop_usage()}}]
    second = [*thought('乙', 's2'), *hop_events('答', start=1)[1:]]
    token = current_request.set(RequestContext(authenticated=True))
    try:
        chunks = to_chat(collect_interactions(session_with(first, second)))
        reasoning = ''.join(chunk['choices'][0]['delta'].get('reasoning_content') or ''
                            for chunk in chunks if chunk.get('choices'))
        assert reasoning == '甲乙'
        steps, _ = convert_oai_messages_to_interactions(
            [{'role': 'assistant', 'content': '答', 'reasoning_content': reasoning}])
        assert [step.get('signature') for step in steps if step['type'] == 'thought'] == ['s1', 's2']
    finally:
        current_request.reset(token)


# ---- generateContent 客户端接 Interactions 上游：和原生入口一样由客户端决定是否续传 ----

def test_generate_content_client_receives_continuation_and_can_send_it_back():
    from converters.gemini_interactions import convert_gemini_gc_to_interactions, convert_interactions_to_gemini_gc
    response = convert_interactions_to_gemini_gc(hop_response([output_step('一段')], 'requires_continuation', 'token-1'))
    candidate = response['candidates'][0]
    assert candidate['finishReason'] == 'CONTINUATION' and candidate['continuationToken'] == 'token-1'
    body = convert_gemini_gc_to_interactions({'contents': 'hi', 'continuationToken': 'token-1'}, 'm')
    assert body['continuation_token'] == 'token-1' and 'continuationToken' not in body


def test_generate_content_stream_exposes_the_token_on_the_finish_chunk():
    from converters.gemini_interactions import InteractionsToGeminiGCConverter
    converter = InteractionsToGeminiGCConverter()
    chunks = [item for event in hop_events('一段', 'requires_continuation', 'token-1') for item in converter.feed(event)]
    finish = next(item for item in chunks if item.get('candidates') and item['candidates'][0].get('finishReason'))
    assert finish['candidates'][0]['finishReason'] == 'CONTINUATION'
    assert finish['candidates'][0]['continuationToken'] == 'token-1'
