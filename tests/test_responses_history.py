import asyncio
import copy
import json
from unittest.mock import AsyncMock, patch

import pytest

from core.conversation_store import ConversationStore
from core.request_context import RequestContext, credential_identity, current_request
from converters.responses_bridge import convert_chat_request_to_responses, build_chat_streaming_response_from_responses
from routes._direct_api_responses import handle_responses_native_direct
from services.request_execution import retry_before_output
from services.responses_history import ResponsesHistory, history_boundaries
from test_responses_bridge import _FakeMonitoring


def response(text='answer', cipher='opaque-original'):
    return {'id': 'resp_test', 'object': 'response', 'status': 'completed', 'model': 'upstream', 'output': [
        {'type': 'reasoning', 'id': 'rs_original', 'summary': [], 'encrypted_content': cipher},
        {'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
         'content': [{'type': 'output_text', 'text': text, 'annotations': []}]},
    ]}


def context(**kwargs):
    return RequestContext(authenticated=True, owner_id='owner', model='public',
        endpoint={'api_type': 'responses_native', 'api_base_url': 'https://example.test/v1'},
        credential_fingerprint=credential_identity('key-a'), **kwargs)


def history(ctx, messages, **fields):
    request = {'model': 'public', 'messages': messages, **fields}
    upstream = convert_chat_request_to_responses(request, 'upstream', ctx.endpoint)
    return ResponsesHistory(ctx, request, 'upstream', ctx.endpoint, upstream)


@pytest.fixture
def store(tmp_path, monkeypatch):
    value = ConversationStore(tmp_path / 'conversations.db')
    monkeypatch.setattr('services.responses_history.conversation_store', value)
    # Exercise production transport without writing to the singleton database.
    monkeypatch.setattr('core.conversation_store.conversation_store', value)
    return value


def test_appended_turns_restore_original_outputs_without_client_extensions(store):
    async def run():
        messages = [{'role': 'user', 'content': 'first'}]
        first = response()
        await history(context(), messages).remember(first)
        messages += [{'role': 'assistant', 'content': 'answer'}, {'role': 'user', 'content': 'second'}, {'role': 'user', 'content': 'additional instructions'}]
        restored = await history(context(), messages).restore_input()
        assert restored[1:3] == first['output']
        assert [i['content'][0]['text'] for i in restored[-2:]] == ['second', 'additional instructions']
        second = response('second answer', 'opaque-second')
        await history(context(), messages).remember(second)
        messages += [{'role': 'assistant', 'content': 'second answer'}, {'role': 'user', 'content': 'third'}]
        restored = await history(context(), messages).restore_input()
        assert [i['encrypted_content'] for i in restored if i.get('type') == 'reasoning'] == ['opaque-original', 'opaque-second']
        assert all('provider_metadata' not in m for m in messages)
    asyncio.run(run())


@pytest.mark.parametrize('change', ['owner', 'model', 'endpoint', 'credential', 'edited', 'truncated', 'tools'])
def test_changed_scope_or_history_does_not_restore(store, change):
    async def run():
        await history(context(), [{'role': 'user', 'content': 'first'}]).remember(response())
        messages = [{'role': 'user', 'content': 'first'}, {'role': 'assistant', 'content': 'answer'}, {'role': 'user', 'content': 'next'}]
        ctx = context()
        fields = {}
        if change == 'owner': ctx.owner_id = 'someone-else'
        if change == 'model': ctx.model = 'another-model'
        if change == 'endpoint': ctx.endpoint['api_base_url'] = 'https://other.test/v1'
        if change == 'credential': ctx.credential_fingerprint = credential_identity('key-b')
        if change == 'edited': messages[0]['content'] = 'changed'
        if change == 'truncated': messages = messages[1:]
        if change == 'tools': fields['tools'] = [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object'}}}]
        restored = await history(ctx, messages, **fields).restore_input()
        assert not any(i.get('type') == 'reasoning' for i in restored)
    asyncio.run(run())


def test_ambiguous_prefix_remains_ambiguous_and_explicit_metadata_wins(store):
    async def run():
        original = [{'role': 'user', 'content': 'same'}]
        await history(context(), original).remember(response(cipher='branch-a'))
        await history(context(), original).remember(response(cipher='branch-b'))
        await history(context(), original).remember(response(cipher='branch-a'))
        messages = original + [{'role': 'assistant', 'content': 'answer'}, {'role': 'user', 'content': 'next'}]
        restored = await history(context(), messages).restore_input()
        assert not any(i.get('type') == 'reasoning' for i in restored)
        messages[1]['provider_metadata'] = {'responses_output': response(cipher='client-explicit')['output']}
        restored = await history(context(), messages).restore_input()
        assert restored[1]['encrypted_content'] == 'client-explicit'
    asyncio.run(run())


def test_explicit_sessions_and_expiry_are_respected(store):
    async def run():
        messages = [{'role': 'user', 'content': 'same'}]
        await history(context(session_id='one', explicit_session=True), messages).remember(response())
        messages += [{'role': 'assistant', 'content': 'answer'}]
        assert any(i.get('type') == 'reasoning' for i in await history(context(session_id='one', explicit_session=True), messages).restore_input())
        assert not any(i.get('type') == 'reasoning' for i in await history(context(session_id='two', explicit_session=True), messages).restore_input())
        with store.connection() as connection:
            connection.execute('UPDATE response_prefixes SET touched=0')
        assert not any(i.get('type') == 'reasoning' for i in await history(context(session_id='one', explicit_session=True), messages).restore_input())
        store.cleanup()
        with store.connection() as connection:
            assert connection.execute('SELECT count(*) FROM response_prefixes').fetchone()[0] == 0
    asyncio.run(run())


def test_text_block_representation_and_tool_boundaries(store):
    async def run():
        messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'lookup'}]}]
        native = response(text='')
        native['output'] = [native['output'][0], {'type': 'function_call', 'call_id': 'call-1', 'name': 'lookup', 'arguments': '{}'}]
        await history(context(), messages).remember(native)
        messages = [{'role': 'user', 'content': 'lookup'}, {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'result'}, {'role': 'user', 'content': 'continue'}]
        restored = await history(context(), messages).restore_input()
        assert restored[1:3] == native['output']
        assert restored[3]['type'] == 'function_call_output' and restored[3]['call_id'] == 'call-1'
    asyncio.run(run())


@pytest.mark.parametrize('streaming', [False, True])
def test_actual_handler_and_retry_boundary_restore_phone_history(store, streaming):
    class Service:
        def __init__(self): self.requests = []
        @retry_before_output
        async def call_api_passthrough(self, *, request_body, api_key, **kwargs):
            self.requests.append(copy.deepcopy(request_body))
            native = response()
            if request_body.get('stream'):
                events = [{'type': 'response.output_text.delta', 'delta': 'answer'},
                          {'type': 'response.completed', 'response': native}]
                for event in events:
                    yield ('data: ' + json.dumps(event) + '\n\n').encode()
                yield b'data: [DONE]\n\n'
            else:
                yield json.dumps(native).encode()
    async def run():
        service = Service()
        messages = [{'role': 'user', 'content': 'first'}]
        for _ in range(2):
            ctx = context()
            token = current_request.set(ctx)
            try:
                result = await handle_responses_native_direct(openai_req={'model': 'public', 'messages': messages, 'stream': streaming},
                    model_name='public', target_model_id='upstream', display_name='public', api_base_url='https://example.test/v1',
                    api_key='key-a', endpoint_config=ctx.endpoint, pricing_config={}, monitoring_service=_FakeMonitoring(), direct_api_service=service,
                    estimate_message_tokens_func=lambda *args: 1, estimate_tokens_func=lambda *args: 1)
                if streaming:
                    async for chunk in result.body_iterator:
                        if b'[DONE]' in chunk:
                            # Saved before handing the terminal marker to the phone.
                            assert list(store.response_prefixes(ctx.owner_id, ctx.responses_history.scope(),
                                [p for _, p in history_boundaries(messages + [{'role': 'assistant', 'content': 'answer'}])]).values())
                messages += [{'role': 'assistant', 'content': 'answer'}, {'role': 'user', 'content': 'next'}]
            finally: current_request.reset(token)
        assert not any(i.get('type') == 'reasoning' for i in service.requests[0]['input'])
        assert service.requests[1]['input'][1:3] == response()['output']
    asyncio.run(run())


def test_retry_rebuilds_history_after_switching_key(store):
    async def run():
        messages = [{'role': 'user', 'content': 'first'}]
        await history(context(), messages).remember(response())
        ctx = context()
        ctx.endpoint['auto_retry'] = {'enabled': True, 'max_retries': 1, 'retry_delay_seconds': 0, 'retry_on_503': True}
        ctx.responses_history = history(ctx, messages + [{'role': 'assistant', 'content': 'answer'}, {'role': 'user', 'content': 'next'}])
        seen = []
        @retry_before_output
        async def upstream(*, request_body, api_key):
            seen.append(copy.deepcopy(request_body))
            yield json.dumps({'error': {'message': 'unavailable', 'code': 503}} if len(seen) == 1 else response()).encode()
        token = current_request.set(ctx)
        try:
            with patch('routes._direct_api_utils.get_api_key', new=AsyncMock(return_value='key-b')):
                _ = [chunk async for chunk in upstream(request_body={'stream': False, 'input': []}, api_key='key-a')]
        finally: current_request.reset(token)
        assert len(seen) == 2
        assert any(i.get('type') == 'reasoning' for i in seen[0]['input'])
        assert not any(i.get('type') == 'reasoning' for i in seen[1]['input'])
    asyncio.run(run())


@pytest.mark.parametrize('terminal', [False, True])
def test_stream_caches_done_items_only_after_complete_terminal(store, terminal):
    async def run():
        replay = history(context(), [{'role': 'user', 'content': 'first'}])
        async def source():
            for index, item in enumerate(response()['output']):
                yield ('data: ' + json.dumps({'type': 'response.output_item.done', 'output_index': index, 'item': item}) + '\n\n').encode()
            if terminal:
                yield b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
        stream = build_chat_streaming_response_from_responses(source(), 'upstream', on_response=replay.remember)
        _ = [chunk async for chunk in stream.body_iterator]
        next_input = await history(context(), [{'role': 'user', 'content': 'first'}, {'role': 'assistant', 'content': 'answer'}, {'role': 'user', 'content': 'next'}]).restore_input()
        assert any(i.get('type') == 'reasoning' for i in next_input) == terminal
    asyncio.run(run())


def test_persistence_and_concurrent_ambiguity(store):
    from concurrent.futures import ThreadPoolExecutor
    store.remember_response_prefix('owner', 'scope', 'prefix', response()['output'])
    reopened = ConversationStore(store.path)
    assert reopened.response_prefixes('owner', 'scope', ['prefix'])['prefix'] == response()['output']
    with ThreadPoolExecutor(max_workers=2) as executor:
        tasks = [executor.submit(reopened.remember_response_prefix, 'owner', 'scope', 'conflict', response(cipher=cipher)['output'])
                 for cipher in ('branch-a', 'branch-b')]
        for task in tasks: task.result()
    assert reopened.response_prefixes('owner', 'scope', ['conflict']) == {}


def test_database_failure_does_not_leak_state_or_break_chat(store, caplog):
    async def run():
        messages = [{'role': 'user', 'content': 'private-prompt'}, {'role': 'assistant', 'content': 'private-answer'}]
        replay = history(context(), messages)
        with patch.object(store, 'response_prefixes', side_effect=RuntimeError('private-ciphertext')):
            assert not any(i.get('type') == 'reasoning' for i in await replay.restore_input())
        with patch.object(store, 'remember_response_prefix', side_effect=RuntimeError('private-ciphertext')):
            await replay.remember(response(cipher='private-ciphertext'))
        assert 'private-' not in caplog.text
    asyncio.run(run())
