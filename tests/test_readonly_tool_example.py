import asyncio
from unittest.mock import AsyncMock
import pytest
from docs.examples.llm_readonly_tools import ToolContext, execute_readonly_tool


def test_tool_catalog_filters_permissions_and_never_returns_keys():
    context = ToolContext({'public': {'api_type': 'direct_api', 'api_key': 'secret'},
                           'private': {'api_type': 'direct_api'}}, {'public'}, False, AsyncMock())
    result = asyncio.run(execute_readonly_tool('bridge_list_models', {}, context))
    assert result == {'models': ['public']}
    result = asyncio.run(execute_readonly_tool('bridge_describe_model', {'model': 'public'}, context))
    assert 'secret' not in str(result)
    with pytest.raises(PermissionError):
        asyncio.run(execute_readonly_tool('bridge_describe_model', {'model': 'private'}, context))
    with pytest.raises(PermissionError):
        asyncio.run(execute_readonly_tool('bridge_usage_summary', {'start_date': '2026-09-01', 'end_date': '2026-09-05'}, context))
    context.usage_reader.assert_not_called()


def test_admin_usage_tool_checks_dates_and_uses_reader():
    reader = AsyncMock(return_value={'requests': 10})
    context = ToolContext({}, None, True, reader)
    args = {'start_date': '2026-09-01', 'end_date': '2026-09-05'}
    assert asyncio.run(execute_readonly_tool('bridge_usage_summary', args, context)) == {'requests': 10}
    reader.assert_awaited_once_with('2026-09-01', '2026-09-05')
    with pytest.raises(ValueError):
        asyncio.run(execute_readonly_tool('run_shell', {}, context))
