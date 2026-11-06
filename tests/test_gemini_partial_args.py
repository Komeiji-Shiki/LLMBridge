"""Vertex streamFunctionCallArguments 的参数分片：拼回完整调用，原始响应块不被改动。"""
import copy

from converters.gemini_partial_args import PartialCallMerger
from services.gemini_history import GeminiTurnAssembler


def chunk(*calls, finish=None, signature=None):
    parts = [{'functionCall': call} for call in calls]
    if signature:
        parts[0]['thoughtSignature'] = signature
    candidate = {'content': {'role': 'model', 'parts': parts}}
    if finish:
        candidate['finishReason'] = finish
    return {'candidates': [candidate]}


def merged_calls(chunks):
    merger = PartialCallMerger()
    return [part for value in chunks for candidate in merger.merge_chunk(value)['candidates']
            for part in candidate.get('content', {}).get('parts', [])]


# 官方文档里 controlLight 的分片序列
CONTROL_LIGHT = [
    chunk({'name': 'controlLight', 'partialArgs': [{'jsonPath': '$.brightness', 'numberValue': 50}], 'willContinue': True}),
    chunk({'partialArgs': [{'jsonPath': '$.colorTemperature', 'stringValue': 'wa', 'willContinue': True}], 'willContinue': True}),
    chunk({'partialArgs': [{'jsonPath': '$.colorTemperature', 'stringValue': 'rm', 'willContinue': True}], 'willContinue': True}),
    chunk({'partialArgs': [{'jsonPath': '$.colorTemperature'}], 'willContinue': True}),
    chunk({}),
]


def test_documented_sequence_becomes_one_complete_call():
    assert merged_calls(CONTROL_LIGHT) == [
        {'functionCall': {'name': 'controlLight', 'args': {'brightness': 50, 'colorTemperature': 'warm'}}}]


def test_nested_paths_arrays_and_scalar_kinds():
    fragments = [
        chunk({'name': 'f', 'id': 'c1', 'willContinue': True}, signature='sig'),
        chunk({'partialArgs': [{'jsonPath': '$.location.latitude', 'numberValue': 1.5},
                               {'jsonPath': '$.tags[1]', 'stringValue': 'b'},
                               {'jsonPath': "$['odd key']", 'boolValue': False},
                               {'jsonPath': '$.empty', 'nullValue': 'NULL_VALUE'}], 'willContinue': True}),
        chunk({'partialArgs': [{'jsonPath': '$.tags[0]', 'stringValue': 'a'}]}),
    ]
    assert merged_calls(fragments) == [{'thoughtSignature': 'sig', 'functionCall': {
        'name': 'f', 'id': 'c1',
        'args': {'location': {'latitude': 1.5}, 'tags': ['a', 'b'], 'odd key': False, 'empty': None}}}]


def test_unfinished_call_is_released_when_the_candidate_finishes():
    fragments = [
        chunk({'name': 'f', 'willContinue': True}),
        chunk({'partialArgs': [{'jsonPath': '$.text', 'stringValue': '半', 'willContinue': True}], 'willContinue': True}),
        {'candidates': [{'finishReason': 'MAX_TOKENS'}]},
    ]
    merger = PartialCallMerger()
    last = [merger.merge_chunk(value) for value in fragments][-1]
    assert last['candidates'][0] == {'finishReason': 'MAX_TOKENS', 'content': {
        'role': 'model', 'parts': [{'functionCall': {'name': 'f', 'args': {'text': '半'}}}]}}


def test_ordinary_chunks_pass_through_untouched():
    merger = PartialCallMerger()
    plain = chunk({'name': 'f', 'args': {'x': 1}}, finish='STOP')
    assert merger.merge_chunk(plain) is plain
    fragment = CONTROL_LIGHT[0]
    before = copy.deepcopy(fragment)
    merger.merge_chunk(fragment)
    assert fragment == before


def test_history_assembler_stores_the_merged_call_with_its_signature():
    assembler = GeminiTurnAssembler()
    for value in [chunk({'name': 'f', 'id': 'c1', 'willContinue': True}, signature='sig'),
                  chunk({'partialArgs': [{'jsonPath': '$.x', 'numberValue': 1}], 'willContinue': True}),
                  chunk({}, finish='STOP')]:
        assembler.feed(value)
    assert list(assembler.completed_parts()) == [
        [{'thoughtSignature': 'sig', 'functionCall': {'name': 'f', 'id': 'c1', 'args': {'x': 1}}}]]
