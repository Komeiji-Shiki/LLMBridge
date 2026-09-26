import asyncio
import copy
import json

import pytest

from core.conversation_store import ConversationStore
from core.request_context import RequestContext
from services.gemini_history import (
    GeminiThoughtHistory,
    GeminiTurnAssembler,
    history_boundaries,
)


def chunk(parts):
    return {'candidates': [{'content': {'role': 'model', 'parts': parts}}]}


def streamed_response():
    """真实流式形态：思考增量 → 正文增量 → 末尾纯签名分片。"""
    return [
        chunk([{'thought': True, 'text': '想了'}]),
        chunk([{'thought': True, 'text': '半天'}]),
        chunk([{'text': '你好'}]),
        chunk([{'text': '呀'}]),
        {'candidates': [{'content': {'role': 'model', 'parts': [{'text': '', 'thoughtSignature': 'sig-1'}]},
                         'finishReason': 'STOP'}]},
    ]


def signature_only_response():
    return [
        chunk([{'text': '普通回答'}]),
        {'candidates': [{'content': {'role': 'model', 'parts': [{'text': '', 'thoughtSignature': 'sig-x'}]},
                         'finishReason': 'STOP'}]},
    ]


def build_assembler(*responses):
    assembler = GeminiTurnAssembler()
    for response in responses:
        for value in response:
            assembler.feed(value)
    return assembler


def context(owner_id='owner'):
    return RequestContext(authenticated=True, owner_id=owner_id, model='alias',
                          endpoint={'api_type': 'gemini_native', 'model_id': 'target'})


def bind_store(monkeypatch, tmp_path):
    store = ConversationStore(tmp_path / 'conversations.db')
    monkeypatch.setattr('services.gemini_history.conversation_store', store)
    return store


@pytest.mark.parametrize('parts', [
    [{'text': '回答', 'thoughtSignature': 'text-signature'}],
    [{'thought': True, 'text': '思考'}, {'text': '回答'},
     {'text': '', 'thoughtSignature': 'tail-signature'}],
    [{'thought': True, 'text': '先想', 'thoughtSignature': 'first'},
     {'thought': True, 'text': '再想', 'thoughtSignature': 'second'}, {'text': '回答'}],
    [{'functionCall': {'name': 'lookup', 'args': {'q': 1}}, 'thoughtSignature': 'first-call'},
     {'functionCall': {'name': 'lookup', 'args': {'q': 1}}}],
    [{'text': '图片'}, {'inlineData': {'mimeType': 'image/png', 'data': 'AAAA'},
                       'thoughtSignature': 'image-signature'}],
])
def test_review_restores_original_signed_parts(parts, tmp_path, monkeypatch):
    bind_store(monkeypatch, tmp_path)

    async def run():
        body = {'contents': [{'role': 'user', 'parts': [{'text': '问题'}]}]}
        response = chunk(parts)
        response['candidates'][0]['finishReason'] = 'STOP'
        history = GeminiThoughtHistory(context(), {'model_id': 'target'}, body)
        await history.remember(body['contents'], build_assembler([response]))
        stripped = [{key: value for key, value in part.items() if key != 'thoughtSignature'}
                    for part in parts]
        client = {'contents': body['contents'] + [{'role': 'model', 'parts': stripped}]}
        count = await history.restore(client)
        assert count == sum(bool(part.get('thoughtSignature')) for part in parts)
        assert client['contents'][-1]['parts'] == parts
    asyncio.run(run())


def test_review_changed_image_does_not_restore(tmp_path, monkeypatch):
    bind_store(monkeypatch, tmp_path)

    async def run():
        body = {'contents': [{'role': 'user', 'parts': [
            {'text': '描述图片'}, {'inlineData': {'mimeType': 'image/png', 'data': 'AAAA'}}]}]}
        history = GeminiThoughtHistory(context(), {}, body)
        await history.remember(body['contents'], build_assembler(signature_only_response()))
        client = copy.deepcopy(body)
        client['contents'][0]['parts'][1]['inlineData']['data'] = 'BBBB'
        client['contents'].append({'role': 'model', 'parts': [{'text': '普通回答'}]})
        assert await history.restore(client) == 0
    asyncio.run(run())


@pytest.mark.parametrize('finish_reason', [None, 'MAX_TOKENS', 'SAFETY'])
def test_review_unfinished_response_is_not_remembered(finish_reason, tmp_path, monkeypatch):
    bind_store(monkeypatch, tmp_path)

    async def run():
        body = {'contents': [{'role': 'user', 'parts': [{'text': '问题'}]}]}
        response = signature_only_response()
        candidate = response[-1]['candidates'][0]
        candidate.pop('finishReason')
        if finish_reason:
            candidate['finishReason'] = finish_reason
        history = GeminiThoughtHistory(context(), {}, body)
        await history.remember(body['contents'], build_assembler(response))
        client = {'contents': body['contents'] + [{'role': 'model', 'parts': [{'text': '普通回答'}]}]}
        assert await history.restore(client) == 0
    asyncio.run(run())


def test_review_multiple_candidates_restore_independently(tmp_path, monkeypatch):
    bind_store(monkeypatch, tmp_path)

    async def run():
        body = {'contents': [{'role': 'user', 'parts': [{'text': '问题'}]}]}
        response = {'candidates': [
            {'index': index, 'content': {'parts': [{'text': text}, {'text': '', 'thoughtSignature': text}]},
             'finishReason': 'STOP'} for index, text in enumerate(['回答甲', '回答乙'])]}
        history = GeminiThoughtHistory(context(), {}, body)
        await history.remember(body['contents'], build_assembler([response]))
        for text in ['回答甲', '回答乙']:
            client = {'contents': body['contents'] + [{'role': 'model', 'parts': [{'text': text}]}]}
            assert await history.restore(client) == 1
            assert client['contents'][-1]['parts'][-1]['thoughtSignature'] == text
    asyncio.run(run())


def test_review_cached_content_binds_scope():
    first = GeminiThoughtHistory(context(), {}, {'cachedContent': 'cachedContents/first'})
    second = GeminiThoughtHistory(context(), {}, {'cachedContent': 'cachedContents/second'})
    assert first.scope() != second.scope()


@pytest.mark.parametrize('thoughts', [
    [{'thought': True, 'text': '改写的思考'}],
    [{'thought': True, 'text': 'think one'}, {'thought': True, 'text': 'think two'}],
])
def test_restore_rejects_edited_thoughts_and_removed_internal_space(thoughts, tmp_path, monkeypatch):
    bind_store(monkeypatch, tmp_path)

    async def run():
        body = {'contents': [{'role': 'user', 'parts': [{'text': '问题'}]}]}
        response = chunk([{'thought': True, 'text': 'think one ', 'thoughtSignature': 'first'},
                          {'thought': True, 'text': 'think two', 'thoughtSignature': 'second'},
                          {'text': '回答'}])
        response['candidates'][0]['finishReason'] = 'STOP'
        history = GeminiThoughtHistory(context(), {}, body)
        await history.remember(body['contents'], build_assembler([response]))
        client = {'contents': body['contents'] + [{'role': 'model', 'parts': thoughts + [{'text': '回答'}]}]}
        original = copy.deepcopy(client)
        assert await history.restore(client) == 0
        assert client == original
    asyncio.run(run())


def test_assembler_merges_thought_deltas_and_trailing_signature():
    assembler = build_assembler(streamed_response())
    assert list(assembler.completed_parts()) == [[
        {'thought': True, 'text': '想了半天'}, {'text': '你好呀'},
        {'text': '', 'thoughtSignature': 'sig-1'}]]


def test_assembler_signature_without_thought_stays_trailing():
    assembler = build_assembler(signature_only_response())
    assert assembler.has_signatures() is True
    assert list(assembler.completed_parts()) == [[
        {'text': '普通回答'}, {'text': '', 'thoughtSignature': 'sig-x'}]]


def test_assembler_call_signature_and_streamed_arguments():
    assembler = build_assembler([
        chunk([{'functionCall': {'id': 'c1', 'name': 'lookup', 'args': '{"q":'}}]),
        chunk([{'functionCall': {'id': 'c1', 'name': 'lookup', 'args': '1}'}, 'thoughtSignature': 'sig-c'}]),
        {'candidates': [{'finishReason': 'STOP'}]},
    ])
    assert list(assembler.completed_parts()) == [[{
        'functionCall': {'id': 'c1', 'name': 'lookup', 'args': {'q': 1}}, 'thoughtSignature': 'sig-c'}]]


def test_digest_ignores_thoughts_signatures_but_not_media_or_edits():
    base = [
        {'role': 'user', 'parts': [{'text': '你好'}]},
        {'role': 'model', 'parts': [{'thought': True, 'text': '想'}, {'text': '答'},
                                    {'inlineData': {'mimeType': 'image/png', 'data': 'AAAA'}},
                                    {'thoughtSignature': 's'}]},
    ]
    without_thought = [
        {'role': 'user', 'parts': [{'text': '你好'}]},
        {'role': 'model', 'parts': [{'text': '答'},
                                    {'inlineData': {'mimeType': 'image/png', 'data': 'AAAA'}}]},
    ]
    without_media = [
        {'role': 'user', 'parts': [{'text': '你好'}]},
        {'role': 'model', 'parts': [{'thought': True, 'text': '想'}, {'text': '答'}]},
    ]
    edited = [
        {'role': 'user', 'parts': [{'text': '你好'}]},
        {'role': 'model', 'parts': [{'text': '答!'}]},
    ]
    digests = {name: [digest for _, digest in history_boundaries(value)]
               for name, value in (('base', base), ('without_thought', without_thought),
                                   ('without_media', without_media), ('edited', edited))}
    assert digests['base'] == digests['without_thought']
    assert digests['base'] != digests['without_media']
    assert digests['edited'] != digests['base']


def test_digest_normalizes_model_text_edges_only():
    first = [
        {'role': 'user', 'parts': [{'text': ' 保持原样 '}]},
        {'role': 'model', 'parts': [{'text': 'answer\r\nline\r\n'}]},
    ]
    second = [
        {'role': 'user', 'parts': [{'text': ' 保持原样 '}]},
        {'role': 'model', 'parts': [{'text': '\nanswer\nline'}]},
    ]
    edited_user = [
        {'role': 'user', 'parts': [{'text': '保持原样'}]},
        {'role': 'model', 'parts': [{'text': 'answer\nline'}]},
    ]
    first_digest = [digest for _, digest in history_boundaries(first)]
    second_digest = [digest for _, digest in history_boundaries(second)]
    edited_digest = [digest for _, digest in history_boundaries(edited_user)]
    assert first_digest == second_digest
    assert edited_digest != first_digest


def test_restore_round_trip_and_insert_and_edit_skip(tmp_path, monkeypatch):
    bind_store(monkeypatch, tmp_path)

    async def run():
        ctx = context()
        body = {'contents': [{'role': 'user', 'parts': [{'text': '你好'}]}]}
        await GeminiThoughtHistory(ctx, {'model_id': 'target'}, body).remember(
            body['contents'], build_assembler(streamed_response()))

        kept = {'contents': [{'role': 'user', 'parts': [{'text': '你好'}]},
                             {'role': 'model', 'parts': [{'thought': True, 'text': '想了半天'}, {'text': '你好呀'}]}]}
        count = await GeminiThoughtHistory(ctx, {'model_id': 'target'}, kept).restore(kept)
        assert count == 1
        assert kept['contents'][1]['parts'][-1] == {'text': '', 'thoughtSignature': 'sig-1'}
        assert kept['contents'][1]['parts'][0] == {'thought': True, 'text': '想了半天'}
        assert kept['contents'][1]['parts'][1] == {'text': '你好呀'}

        dropped = {'contents': [{'role': 'user', 'parts': [{'text': '你好'}]},
                                {'role': 'model', 'parts': [{'text': '你好呀'}]}]}
        count = await GeminiThoughtHistory(ctx, {'model_id': 'target'}, dropped).restore(dropped)
        assert count == 1
        assert dropped['contents'][1]['parts'][0] == {'thought': True, 'text': '想了半天'}
        assert dropped['contents'][1]['parts'][-1] == {'text': '', 'thoughtSignature': 'sig-1'}

        edited = {'contents': [{'role': 'user', 'parts': [{'text': '你好'}]},
                               {'role': 'model', 'parts': [{'text': '你好呀!!'}]}]}
        count = await GeminiThoughtHistory(ctx, {'model_id': 'target'}, edited).restore(edited)
        assert count == 0
        assert all('thoughtSignature' not in part for part in edited['contents'][1]['parts'])

        # 客户端已自带签名时不改任何字段
        signed = {'contents': [{'role': 'user', 'parts': [{'text': '你好'}]},
                               {'role': 'model', 'parts': [{'thought': True, 'text': '想了半天',
                                                            'thoughtSignature': 'client-sig'}, {'text': '你好呀'}]}]}
        count = await GeminiThoughtHistory(ctx, {'model_id': 'target'}, signed).restore(signed)
        assert count == 0
        assert signed['contents'][1]['parts'][0]['thoughtSignature'] == 'client-sig'
    asyncio.run(run())


def test_restore_trailing_signature_only(tmp_path, monkeypatch):
    bind_store(monkeypatch, tmp_path)

    async def run():
        ctx = context()
        body = {'contents': [{'role': 'user', 'parts': [{'text': '讲个笑话'}]}]}
        await GeminiThoughtHistory(ctx, {'model_id': 'target'}, body).remember(
            body['contents'], build_assembler(signature_only_response()))
        client = {'contents': [{'role': 'user', 'parts': [{'text': '讲个笑话'}]},
                               {'role': 'model', 'parts': [{'text': '普通回答'}]}]}
        count = await GeminiThoughtHistory(ctx, {'model_id': 'target'}, client).restore(client)
        assert count == 1
        assert client['contents'][1]['parts'][-1] == {'thoughtSignature': 'sig-x', 'text': ''}
    asyncio.run(run())


def test_restore_matches_and_signs_function_call(tmp_path, monkeypatch):
    bind_store(monkeypatch, tmp_path)

    async def run():
        ctx = context()
        body = {'contents': [{'role': 'user', 'parts': [{'text': '查一下'}]}]}
        await GeminiThoughtHistory(ctx, {'model_id': 'target'}, body).remember(
            body['contents'], build_assembler([
                chunk([{'functionCall': {'id': 'c1', 'name': 'lookup', 'args': {'q': 1}}}]),
                chunk([{'functionCall': {'id': 'c1', 'name': 'lookup', 'args': {}}, 'thoughtSignature': 'sig-c'}]),
                {'candidates': [{'finishReason': 'STOP'}]},
            ]))
        client = {'contents': [{'role': 'user', 'parts': [{'text': '查一下'}]},
                               {'role': 'model', 'parts': [{'functionCall': {'id': 'c1', 'name': 'lookup', 'args': {'q': 1}}}]},
                               {'role': 'user', 'parts': [{'functionResponse': {'name': 'lookup', 'id': 'c1',
                                                                                'response': {'result': 'ok'}}}]}]}
        count = await GeminiThoughtHistory(ctx, {'model_id': 'target'}, client).restore(client)
        assert count == 1
        assert client['contents'][1]['parts'][0]['thoughtSignature'] == 'sig-c'
    asyncio.run(run())


def test_conflicting_records_disable_restore(tmp_path, monkeypatch):
    bind_store(monkeypatch, tmp_path)

    async def run():
        ctx = context()
        body = {'contents': [{'role': 'user', 'parts': [{'text': '你好'}]}]}
        history = GeminiThoughtHistory(ctx, {'model_id': 'target'}, body)
        await history.remember(body['contents'], build_assembler(streamed_response()))
        conflict = [
            chunk([{'thought': True, 'text': '想了半天'}]),
            chunk([{'text': '你好呀'}]),
            {'candidates': [{'content': {'role': 'model', 'parts': [{'text': '', 'thoughtSignature': 'sig-2'}]},
                             'finishReason': 'STOP'}]},
        ]
        await history.remember(body['contents'], build_assembler(conflict))
        client = {'contents': [{'role': 'user', 'parts': [{'text': '你好'}]},
                               {'role': 'model', 'parts': [{'thought': True, 'text': '想了半天'}, {'text': '你好呀'}]}]}
        count = await GeminiThoughtHistory(ctx, {'model_id': 'target'}, client).restore(client)
        assert count == 0
    asyncio.run(run())


def test_restore_requires_authenticated_owner(tmp_path, monkeypatch):
    bind_store(monkeypatch, tmp_path)

    async def run():
        ctx = context()
        body = {'contents': [{'role': 'user', 'parts': [{'text': '你好'}]}]}
        await GeminiThoughtHistory(ctx, {'model_id': 'target'}, body).remember(
            body['contents'], build_assembler(streamed_response()))
        anonymous = RequestContext(owner_id='owner', model='alias',
                                   endpoint={'api_type': 'gemini_native', 'model_id': 'target'})
        client = {'contents': [{'role': 'user', 'parts': [{'text': '你好'}]},
                               {'role': 'model', 'parts': [{'marker': True, 'text': '想了半天'}, {'text': '你好呀'}]}]}
        count = await GeminiThoughtHistory(anonymous, {'model_id': 'target'}, client).restore(client)
        assert count == 0
        assert 'thoughtSignature' not in client['contents'][1]['parts'][0]
    asyncio.run(run())


def test_scope_binds_system_and_tools(tmp_path, monkeypatch):
    bind_store(monkeypatch, tmp_path)

    async def run():
        ctx = context()
        body = {'contents': [{'role': 'user', 'parts': [{'text': '你好'}]}],
                'systemInstruction': {'parts': [{'text': '系统'}]}}
        await GeminiThoughtHistory(ctx, {'model_id': 'target'}, body).remember(
            body['contents'], build_assembler(streamed_response()))
        changed_system = {'contents': [{'role': 'user', 'parts': [{'text': '你好'}]}],
                          'systemInstruction': {'parts': [{'text': '换了系统'}]}}
        client = {'contents': [{'role': 'user', 'parts': [{'text': '你好'}]},
                               {'role': 'model', 'parts': [{'thought': True, 'text': '想了半天'}, {'text': '你好呀'}]}]}
        count = await GeminiThoughtHistory(ctx, {'model_id': 'target'}, changed_system).restore(client)
        assert count == 0
        assert 'thoughtSignature' not in client['contents'][1]['parts'][0]
        # 系统提示一致时恢复可用
        count = await GeminiThoughtHistory(ctx, {'model_id': 'target'}, body).restore(client)
        assert count == 1
    asyncio.run(run())


def test_restore_accepts_trimmed_thought_text(tmp_path, monkeypatch):
    bind_store(monkeypatch, tmp_path)

    async def run():
        ctx = context()
        body = {'contents': [{'role': 'user', 'parts': [{'text': '你好'}]}]}
        assembler = build_assembler([
            chunk([{'thought': True, 'text': ' 想了'}]),
            chunk([{'thought': True, 'text': '半天 '}]),
            chunk([{'text': '你好呀'}]),
            {'candidates': [{'content': {'role': 'model', 'parts': [{'text': '', 'thoughtSignature': 'sig-1'}]},
                             'finishReason': 'STOP'}]},
        ])
        await GeminiThoughtHistory(ctx, {'model_id': 'target'}, body).remember(body['contents'], assembler)
        client = {'contents': [{'role': 'user', 'parts': [{'text': '你好'}]},
                               {'role': 'model', 'parts': [{'thought': True, 'text': '想了半天'},
                                                           {'text': '你好呀'}]}]}
        count = await GeminiThoughtHistory(ctx, {'model_id': 'target'}, client).restore(client)
        assert count == 1
        assert client['contents'][1]['parts'][0] == {'thought': True, 'text': ' 想了半天 '}
        assert client['contents'][1]['parts'][-1] == {'text': '', 'thoughtSignature': 'sig-1'}
        assert client['contents'][1]['parts'][1] == {'text': '你好呀'}
    asyncio.run(run())


def test_restore_inserts_when_client_drops_thought_and_trims_text(tmp_path, monkeypatch):
    bind_store(monkeypatch, tmp_path)

    async def run():
        ctx = context()
        body = {'contents': [{'role': 'user', 'parts': [{'text': '你好'}]}]}
        assembler = build_assembler([
            chunk([{'thought': True, 'text': '想了半天'}]),
            chunk([{'text': ' 你好呀'}]),
            chunk([{'text': '\n'}]),
            {'candidates': [{'content': {'role': 'model', 'parts': [{'text': '', 'thoughtSignature': 'sig-1'}]},
                             'finishReason': 'STOP'}]},
        ])
        await GeminiThoughtHistory(ctx, {'model_id': 'target'}, body).remember(body['contents'], assembler)
        client = {'contents': [{'role': 'user', 'parts': [{'text': '你好'}]},
                               {'role': 'model', 'parts': [{'text': '你好呀'}]}]}
        count = await GeminiThoughtHistory(ctx, {'model_id': 'target'}, client).restore(client)
        assert count == 1
        assert client['contents'][1]['parts'][0] == {'thought': True, 'text': '想了半天'}
        assert client['contents'][1]['parts'][-1] == {'text': '', 'thoughtSignature': 'sig-1'}
        assert client['contents'][1]['parts'][1] == {'text': ' 你好呀\n'}
    asyncio.run(run())


def test_restore_replaces_whitespace_only_thought_shell(tmp_path, monkeypatch):
    bind_store(monkeypatch, tmp_path)

    async def run():
        ctx = context()
        body = {'contents': [{'role': 'user', 'parts': [{'text': '你好'}]}]}
        assembler = build_assembler(streamed_response())
        await GeminiThoughtHistory(ctx, {'model_id': 'target'}, body).remember(body['contents'], assembler)
        client = {'contents': [{'role': 'user', 'parts': [{'text': '你好'}]},
                               {'role': 'model', 'parts': [{'thought': True, 'text': ' '},
                                                           {'text': '你好呀'}]}]}
        count = await GeminiThoughtHistory(ctx, {'model_id': 'target'}, client).restore(client)
        assert count == 1
        assert len(client['contents'][1]['parts']) == 3
        assert client['contents'][1]['parts'][0] == {'thought': True, 'text': '想了半天'}
        assert client['contents'][1]['parts'][-1] == {'text': '', 'thoughtSignature': 'sig-1'}
    asyncio.run(run())
