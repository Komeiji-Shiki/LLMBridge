"""Gemini 工具调用转换：不编造函数名和参数，对不上的数据要带着原因暴露出来。"""
import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from converters.gemini_interactions import convert_interactions_to_openai_response
from services.direct_api_service import DirectAPIService

TOOL_CALL = {'id': 'call_8f3a91c2', 'type': 'function', 'function': {'name': 'get_weather', 'arguments': '{"city":"Paris"}'}}


def fake_session():
    upstream = MagicMock(status=200)
    upstream.json = AsyncMock(return_value={'candidates': [{'content': {'parts': [{'text': 'ok'}]}, 'finishReason': 'STOP'}]})
    session = MagicMock()
    session.post.return_value.__aenter__ = AsyncMock(return_value=upstream)
    session.post.return_value.__aexit__ = AsyncMock(return_value=False)
    return session


def convert(messages):
    session = fake_session()

    async def run():
        return [item async for item in DirectAPIService(session).call_gemini_native_api(
            'test-key', 'demo', messages, stream=False)]
    return asyncio.run(run()), session


def sent_contents(session):
    return json.loads(session.post.call_args.kwargs['data'])['contents']


def convert_response(function_call):
    return DirectAPIService(MagicMock()).convert_gemini_response_to_openai(
        {'candidates': [{'content': {'parts': [{'functionCall': function_call}]}}]}, 'demo', 'r')


def test_tool_result_without_matching_call_is_rejected_before_upstream():
    chunks, session = convert([{'role': 'user', 'content': 'hi'},
                               {'role': 'tool', 'tool_call_id': 'call_8f3a91c2', 'content': 'sunny'}])
    assert len(chunks) == 1
    assert chunks[0]['error']['type'] == 'invalid_request_error'
    assert 'call_8f3a91c2' in chunks[0]['error']['message']
    session.post.assert_not_called()


def test_tool_result_takes_function_name_from_matching_call():
    chunks, session = convert([{'role': 'user', 'content': 'hi'},
                               {'role': 'assistant', 'content': '', 'tool_calls': [TOOL_CALL]},
                               {'role': 'tool', 'tool_call_id': 'call_8f3a91c2', 'content': 'sunny'}])
    assert 'error' not in chunks[0]
    response = sent_contents(session)[-1]['parts'][0]['functionResponse']
    assert response['name'] == 'get_weather' and response['id'] == 'call_8f3a91c2'


@pytest.mark.parametrize('arguments', ['{"city": ', '[1, 2]', 'null'])
def test_history_arguments_that_are_not_an_object_are_logged_and_sent_empty(arguments, caplog):
    call = {**TOOL_CALL, 'function': {'name': 'get_weather', 'arguments': arguments}}
    with caplog.at_level(logging.WARNING):
        _, session = convert([{'role': 'user', 'content': 'hi'},
                              {'role': 'assistant', 'content': '', 'tool_calls': [call]},
                              {'role': 'tool', 'tool_call_id': 'call_8f3a91c2', 'content': 'sunny'}])
    assert sent_contents(session)[1]['parts'][0]['functionCall']['args'] == {}
    assert any('get_weather' in record.getMessage() for record in caplog.records)


def test_history_without_arguments_is_not_a_warning(caplog):
    call = {**TOOL_CALL, 'function': {'name': 'get_time', 'arguments': ''}}
    with caplog.at_level(logging.WARNING):
        convert([{'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': '', 'tool_calls': [call]},
                 {'role': 'tool', 'tool_call_id': 'call_8f3a91c2', 'content': '12:00'}])
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_response_function_call_without_name_is_reported():
    with pytest.raises(ValueError, match='没有 name'):
        convert_response({'args': {'city': 'Paris'}})


@pytest.mark.parametrize('args', ['{"city": "Paris"}', [1, 2]])
def test_response_function_call_with_non_object_args_is_reported(args):
    with pytest.raises(ValueError, match='args 不是对象'):
        convert_response({'name': 'get_weather', 'args': args})


def test_response_function_call_without_args_means_no_arguments():
    call = convert_response({'name': 'get_time'})['choices'][0]['message']['tool_calls'][0]
    assert call['function'] == {'name': 'get_time', 'arguments': '{}'}


def test_interactions_function_call_with_non_object_arguments_is_reported():
    interaction = {'steps': [{'type': 'function_call', 'id': 'fc1', 'name': 'get_weather', 'arguments': '{"city": "Paris"}'}]}
    with pytest.raises(ValueError, match='arguments 不是对象'):
        convert_interactions_to_openai_response(interaction, 'demo', 'r')
