import copy
import pytest
from services.provider_capabilities import apply_native_tool_defaults, diagnose_model, tools_for


def test_openai_hosted_shell_and_tool_search_defaults():
    result = apply_native_tool_defaults({'input': 'hello'}, {'provider': 'openai', 'api_type': 'responses_native', 'native_tools': ['shell', 'tool_search']})
    assert result['tools'] == [{'type': 'shell', 'environment': {'type': 'container_auto'}}, {'type': 'tool_search'}]


@pytest.mark.parametrize('provider,tool', [('deepseek', 'web_search'), ('openai', 'code_interpreter'), ('qwen', 'web_extractor')])
def test_native_responses_tools_are_provider_specific(provider, tool):
    config = {'provider': provider, 'api_type': 'responses_native', 'native_tools': [tool]}
    result = apply_native_tool_defaults({'input': 'hello'}, config)
    assert result['tools'][0]['type'] == tool
    assert tools_for({'provider': 'deepseek', 'api_type': 'responses_native'}) == ['web_search']


def test_caller_tools_and_schema_are_preserved():
    body = {'tools': [{'type': 'function', 'name': 'x', 'strict': True, 'parameters': {'$ref': '#'}}]}
    original = copy.deepcopy(body)
    result = apply_native_tool_defaults(body, {'provider': 'openai', 'api_type': 'responses_native', 'native_tools': ['web_search']})
    assert result == original
    assert body == original


def test_qwen_chat_uses_bailian_parameters_and_respects_explicit_off():
    config = {'provider': 'qwen', 'api_type': 'direct_api', 'native_tools': ['web_search']}
    assert apply_native_tool_defaults({}, config)['enable_search'] is True
    assert apply_native_tool_defaults({'enable_search': False}, config)['enable_search'] is False


def test_gemini_protocols_have_distinct_tool_shapes():
    config = {'provider': 'gemini', 'api_type': 'gemini_native', 'native_tools': ['google_search', 'code_execution']}
    assert apply_native_tool_defaults({}, config)['tools'] == [{'googleSearch': {}}, {'codeExecution': {}}]
    config['upstream_protocol'] = 'interactions'
    assert apply_native_tool_defaults({}, config)['tools'] == [{'type': 'google_search'}, {'type': 'code_execution'}]


def test_diagnosis_does_not_claim_live_verification_or_expose_keys():
    result = diagnose_model('demo', {'provider': 'deepseek', 'api_type': 'responses_native', 'api_key': 'secret'})
    assert result['native_tools'] == ['web_search']
    assert result['live_verified'] is False
    assert 'secret' not in str(result)
