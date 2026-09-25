import asyncio
import json

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


def test_assembler_merges_thought_deltas_and_trailing_signature():
    assembler = build_assembler(streamed_response())
    record = assembler.record()
    assert record['thoughts'] == [{'text': '想了半天', 'signature': 'sig-1'}]
    assert record['calls'] == []
    assert record['trailing'] == []
    parts = assembler.assembled_parts()
    assert parts[0] == {'thought': True, 'text': '想了半天', 'thoughtSignature': 'sig-1'}
    assert parts[1] == {'text': '你好呀'}


def test_assembler_signature_without_thought_stays_trailing():
    assembler = build_assembler(signature_only_response())
    record = assembler.record()
    assert record['thoughts'] == []
    assert record['trailing'] == ['sig-x']
    assert assembler.has_signatures() is True
    assert assembler.assembled_parts() == [{'text': '普通回答'}]


def test_assembler_call_signature_and_streamed_arguments():
    assembler = build_assembler([
        chunk([{'functionCall': {'id': 'c1', 'name': 'lookup', 'args': '{"q":'}}]),
        chunk([{'functionCall': {'id': 'c1', 'name': 'lookup', 'args': '1}'}, 'thoughtSignature': 'sig-c'}]),
    ])
    record = assembler.record()
    assert record['calls'] == [{'id': 'c1', 'name': 'lookup', 'args': '{"q":1}', 'signature': 'sig-c'}]
    assert record['thoughts'] == []


def test_digest_ignores_thoughts_signatures_and_media_but_not_edits():
    base = [
        {'role': 'user', 'parts': [{'text': '你好'}]},
        {'role': 'model', 'parts': [{'thought': True, 'text': '想'}, {'text': '答'},
                                    {'inlineData': {'mimeType': 'image/png', 'data': 'AAAA'}},
                                    {'thoughtSignature': 's'}]},
    ]
    without_thought = [
        {'role': 'user', 'parts': [{'text': '你好'}]},
        {'role': 'model', 'parts': [{'text': '答'}]},
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
    assert digests['base'] == digests['without_thought'] == digests['without_media']
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
        assert kept['contents'][1]['parts'][0]['thoughtSignature'] == 'sig-1'
        assert kept['contents'][1]['parts'][1] == {'text': '你好呀'}

        dropped = {'contents': [{'role': 'user', 'parts': [{'text': '你好'}]},
                                {'role': 'model', 'parts': [{'text': '你好呀'}]}]}
        count = await GeminiThoughtHistory(ctx, {'model_id': 'target'}, dropped).restore(dropped)
        assert count == 1
        assert dropped['contents'][1]['parts'][0] == {'thought': True, 'text': '想了半天', 'thoughtSignature': 'sig-1'}

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
