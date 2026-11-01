"""末尾 assistant 预填充（content / reasoning_content）的单元测试。

覆盖 build_prefill_message 的三种组合，以及经透传准备后 DeepSeek 的
prefix 标记与 reasoning_content 占位不会冲掉预填充的思维链。
"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes._direct_api_passthrough import _prepare_passthrough_request
from routes._direct_api_utils import build_prefill_message


def test_no_prefill_configured():
    assert build_prefill_message({}) is None
    assert build_prefill_message({'prefill_content': '  \n', 'prefill_reasoning_content': ''}) is None
    assert build_prefill_message({'prefill_content': None, 'prefill_reasoning_content': 123}) is None


def test_content_only_keeps_legacy_message_shape():
    assert build_prefill_message({'prefill_content': '<think>'}) == {
        'role': 'assistant', 'content': '<think>'}


def test_reasoning_only_leaves_content_empty():
    assert build_prefill_message({'prefill_reasoning_content': '先分析需求，'}) == {
        'role': 'assistant', 'content': '', 'reasoning_content': '先分析需求，'}


def test_both_merge_into_one_message():
    assert build_prefill_message({
        'prefill_content': '答案：', 'prefill_reasoning_content': '想完了。'}) == {
        'role': 'assistant', 'content': '答案：', 'reasoning_content': '想完了。'}


def test_deepseek_reasoning_prefill_survives_passthrough_preparation():
    config = {'model_family': 'deepseek', 'enable_prefix': True}
    history = {'role': 'assistant', 'content': '旧回答'}
    request = {'messages': [
        {'role': 'user', 'content': '问题'},
        history,
        {'role': 'user', 'content': '追问'},
        build_prefill_message({'prefill_reasoning_content': '先分析需求，'}),
    ]}
    original = copy.deepcopy(request)

    prepared = _prepare_passthrough_request(request, 'demo', 'deepseek-v4', config)

    last = prepared['messages'][-1]
    assert last['reasoning_content'] == '先分析需求，'
    assert last['prefix'] is True
    # 历史 assistant 仍按 DeepSeek 要求补占位，不受预填充影响
    assert prepared['messages'][1]['reasoning_content'] == ' '
    assert request == original


def test_deepseek_content_only_prefill_still_gets_blank_reasoning_placeholder():
    config = {'model_family': 'deepseek', 'enable_prefix': True}
    request = {'messages': [
        {'role': 'user', 'content': '问题'},
        build_prefill_message({'prefill_content': '```python\n'}),
    ]}

    last = _prepare_passthrough_request(request, 'demo', 'deepseek-v4', config)['messages'][-1]

    assert last['content'] == '```python\n'
    assert last['prefix'] is True
    assert last['reasoning_content'] == ' '
