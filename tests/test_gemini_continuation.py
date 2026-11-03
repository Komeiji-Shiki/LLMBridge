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
