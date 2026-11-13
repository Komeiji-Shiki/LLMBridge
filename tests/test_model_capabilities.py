"""模型限制表、消息形状和文件内容块的基础行为。"""
import base64
from datetime import date

import pytest

from converters.media import (
    extract_file, file_part, file_to_anthropic, file_to_gemini, file_to_interactions, file_to_responses,
)
from converters.message_shape import inline_mid_system, merge_assistant_items, rewrite_trailing_prefill
from core.conversion_report import ConversionRejected, conversion_notes, note_loss, raise_if_strict, warnings_header_value
from core.request_context import RequestContext, current_request
from services.model_capabilities import (
    budget_to_effort, drop_unsupported_sampling, gemini_route_target, limits_for, sanitize_anthropic_body,
    supported_effort,
)


@pytest.fixture
def context():
    value = RequestContext()
    token = current_request.set(value)
    yield value
    current_request.reset(token)


@pytest.mark.parametrize('model, sampling, prefill, budget, disable', [
    ('claude-opus-5-5', False, False, False, False),
    ('claude-sonnet-5-5', False, False, False, False),
    ('claude-haiku-5-5', False, False, False, True),
    ('claude-opus-5', False, False, False, True),
    ('claude-opus-4-7', False, False, False, True),
    ('claude-sonnet-4-6', True, False, True, True),
    ('claude-opus-4-5-20251101', True, True, True, True),
    ('claude-fable-5-1', False, False, False, False),
    ('claude-mythos-preview', False, False, True, False),
    ('anthropic/claude-opus-4.7', False, False, False, True),
    ('claude-3-5-sonnet-20241022', True, True, True, True),
])
def test_claude_limits_follow_thinking_table(model, sampling, prefill, budget, disable):
    limits = limits_for(model)
    assert (limits.sampling, limits.prefill, limits.thinking_budget, limits.thinking_disable) == (
        sampling, prefill, budget, disable)


def test_adaptive_thinking_starts_at_claude_46():
    assert limits_for('claude-sonnet-4-5').adaptive_thinking is False
    assert limits_for('claude-sonnet-4-6').adaptive_thinking is True


def test_gemini_generation_limits():
    assert limits_for('gemini-3.8-flash').prefill is False
    assert limits_for('gemini-3.5-flash-lite').sampling is False
    assert limits_for('gemini-3-flash-preview').sampling is True
    assert limits_for('gemini-2.5-flash').function_response_media is False
    assert limits_for('gemini-3-flash-preview').function_response_media is True


@pytest.mark.parametrize('model, effort, expected', [
    ('gpt-6-astra', 'none', 'low'),
    ('gpt-6.1-sol', 'minimal', 'low'),
    ('gpt-6-luna', 'none', 'none'),
    ('claude-opus-5', 'minimal', 'low'),
    ('claude-opus-5', 'max', 'max'),
    ('deepseek-chat', 'minimal', 'minimal'),
])
def test_effort_moves_to_nearest_supported(context, model, effort, expected):
    assert supported_effort(model, effort) == expected


def test_openai_chat_tool_rules():
    assert limits_for('gpt-6-astra').chat_tools == 'responses_only'
    assert limits_for('gpt-6-sol').chat_tools == 'effort_none_only'
    assert limits_for('gpt-4.1').chat_tools == 'free'


def test_sampling_dropped_by_model_and_effort(context):
    assert drop_unsupported_sampling({'temperature': 0.5, 'top_k': 3, 'seed': 1}, 'claude-opus-5') == {'seed': 1}
    assert drop_unsupported_sampling({'temperature': 0.5}, 'gpt-6-luna') == {}
    assert drop_unsupported_sampling({'temperature': 0.5}, 'gpt-6-luna', 'none') == {'temperature': 0.5}
    assert drop_unsupported_sampling({'temperature': 0.5}, 'gpt-5.2') == {'temperature': 0.5}
    assert drop_unsupported_sampling({'temperature': 0.5}, 'gpt-5.2', 'high') == {}
    assert drop_unsupported_sampling({'temperature': 0.5}, 'deepseek-chat') == {'temperature': 0.5}
    assert {note['field'] for note in conversion_notes()} == {'temperature', 'top_k'}


def test_budget_to_effort_tiers():
    assert [budget_to_effort(value) for value in (None, 0, 2000, 8000, 31999)] == [None, 'none', 'low', 'medium', 'high']


def test_mid_system_stays_in_place():
    messages = [{'role': 'system', 'content': 'main'}, {'role': 'user', 'content': 'q'},
                {'role': 'system', 'content': [{'type': 'text', 'text': '<system-reminder>r</system-reminder>'}]},
                {'role': 'system', 'content': []}, {'role': 'user', 'content': 'q2'}]
    shaped = inline_mid_system(messages)
    assert [m['role'] for m in shaped] == ['system', 'user', 'user', 'user']
    assert shaped[2]['content'] == '<system-reminder>r</system-reminder>'
    assert inline_mid_system(messages, 'hoist') == messages
    assert inline_mid_system(messages, role='developer')[2] == {
        'role': 'developer', 'content': '<system-reminder>r</system-reminder>'}
    assert inline_mid_system([{'role': 'user', 'content': 'q'}, {'role': 'system', 'content': 'plain'}])[1]['content'] == (
        '<system-reminder>\nplain\n</system-reminder>')


def test_mid_system_waits_for_tool_results():
    messages = [{'role': 'user', 'content': 'q'},
                {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'a'}, {'id': 'b'}]},
                {'role': 'tool', 'tool_call_id': 'a', 'content': '1'},
                {'role': 'system', 'content': '<r/>'},
                {'role': 'tool', 'tool_call_id': 'b', 'content': '2'},
                {'role': 'user', 'content': 'next'}]
    assert [m.get('tool_call_id') or m['content'] for m in inline_mid_system(messages)[2:]] == ['a', 'b', '<r/>', 'next']


def test_trailing_prefill_rewrite(context):
    messages = [{'role': 'user', 'content': 'q'}, {'role': 'assistant', 'content': 'partial'}]
    assert rewrite_trailing_prefill(messages, True) == messages
    rewritten = rewrite_trailing_prefill(messages, False)
    assert rewritten[-1]['role'] == 'user' and rewritten[-1]['content'].endswith('partial')
    assert rewrite_trailing_prefill([{'role': 'user', 'content': 'q'}, {'role': 'assistant', 'content': ' '}], False) == [
        {'role': 'user', 'content': 'q'}]
    calls = [{'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'a'}]}]
    assert rewrite_trailing_prefill(calls, False) == calls
    assert conversion_notes()[0]['field'] == 'messages[-1]'


def test_merge_assistant_items_keeps_one_turn_together():
    merged = merge_assistant_items([
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': '', 'provider_metadata': {'anthropic_blocks': [{'type': 'thinking'}]}},
        {'role': 'assistant', 'content': 'text'},
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'a'}]},
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'b'}]},
        {'role': 'tool', 'tool_call_id': 'a', 'content': '1'},
        {'role': 'assistant', 'content': 'after'},
    ])
    assert [m['role'] for m in merged] == ['user', 'assistant', 'tool', 'assistant']
    assert merged[1]['content'] == 'text'
    assert [call['id'] for call in merged[1]['tool_calls']] == ['a', 'b']
    assert merged[1]['provider_metadata'] == {'anthropic_blocks': [{'type': 'thinking'}]}


PDF = {'type': 'file', 'file': {'file_data': 'data:application/pdf;base64,QUJD', 'filename': 'a.pdf'}}


def test_file_conversions_between_protocols():
    assert extract_file(PDF) == {'mime_type': 'application/pdf', 'data': 'QUJD', 'filename': 'a.pdf'}
    assert extract_file({'type': 'input_file', 'file_url': 'https://x/a.pdf'})['uri'] == 'https://x/a.pdf'
    document = {'type': 'document', 'title': 't.pdf',
                'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': 'QUJD'}}
    assert file_part(extract_file(document)) == {
        'type': 'file', 'file': {'file_data': 'data:application/pdf;base64,QUJD', 'filename': 't.pdf'}}
    assert file_to_responses(PDF) == {'type': 'input_file', 'file_data': 'data:application/pdf;base64,QUJD', 'filename': 'a.pdf'}
    assert file_to_gemini(PDF) == {'inlineData': {'mimeType': 'application/pdf', 'data': 'QUJD'}}
    assert file_to_interactions(PDF) == {'type': 'document', 'data': 'QUJD', 'mime_type': 'application/pdf'}
    assert file_to_anthropic(PDF)['source'] == {'type': 'base64', 'media_type': 'application/pdf', 'data': 'QUJD'}


def test_text_and_unsupported_files_for_anthropic():
    text = base64.b64encode('内容'.encode()).decode()
    part = {'type': 'file', 'file': {'file_data': f'data:text/plain;base64,{text}', 'filename': 'a.txt'}}
    assert file_to_anthropic(part)['source'] == {'type': 'text', 'media_type': 'text/plain', 'data': '内容'}
    office = {'type': 'file', 'file': {'file_data': 'data:application/msword;base64,QUJD'}}
    assert file_to_anthropic(office) is None
    assert file_to_gemini({'type': 'file', 'file': {'file_id': 'file_1'}}) is None


def test_audio_documents_stay_with_audio_path():
    assert extract_file({'type': 'document', 'source': {'type': 'base64', 'media_type': 'audio/wav', 'data': 'QQ=='}}) is None


def test_strict_mode_and_warning_header(context):
    raise_if_strict({'strict_conversion': True})
    note_loss('tools[0]', '无法转换')
    note_loss('tools[0]', '无法转换')
    raise_if_strict({})
    with pytest.raises(ConversionRejected, match='tools'):
        raise_if_strict({'strict_conversion': True})
    assert b'tools[0]' in warnings_header_value()
    merged = warnings_header_value(b'[{"field":"metadata","applied":false,"message":"x"}]')
    assert merged.index(b'metadata') < merged.index(b'tools[0]')


@pytest.mark.parametrize('model, effort, expected', [
    ('claude-sonnet-5-5', 'high', {'type': 'between_tools'}),
    ('claude-sonnet-5-5', 'max', None),
    ('claude-opus-5-5', 'high', None),
    ('claude-haiku-5-5', 'high', {'type': 'disabled'}),
])
def test_disabled_thinking_follows_model_table(context, model, effort, expected):
    body = sanitize_anthropic_body({'thinking': {'type': 'disabled'}, 'output_config': {'effort': effort}}, model)
    assert body.get('thinking') == expected


@pytest.mark.parametrize('model, target', [
    ('gemini-2.5-pro', 'gemini-3.8-flash'),
    ('gemini-2.5-flash', 'gemini-3.5-flash-lite'),
    ('gemini-2.5-flash-lite', None),
    ('gemini-2.5-flash-image', None),
    ('gemini-3.8-flash', None),
])
def test_gemini_25_route_targets_follow_changelog(model, target):
    assert gemini_route_target(model) == target


def test_gemini_25_limits_switch_on_the_routing_date(monkeypatch):
    class Before(date):
        @classmethod
        def today(cls):
            return date(2026, 11, 30)

    class After(date):
        @classmethod
        def today(cls):
            return date(2026, 12, 1)

    monkeypatch.setattr('services.model_capabilities.date', Before)
    assert limits_for('gemini-2.5-pro').thinking_budget and limits_for('gemini-2.5-pro').prefill
    monkeypatch.setattr('services.model_capabilities.date', After)
    routed = limits_for('gemini-2.5-pro')
    assert not routed.thinking_budget and not routed.prefill and not routed.sampling
