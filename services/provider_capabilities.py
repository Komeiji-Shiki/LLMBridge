"""Documented provider tool profiles. Configuration is not a live capability probe."""
import copy
from urllib.parse import urlparse

PROVIDERS = {
    'openai': {'name': 'OpenAI', 'docs': 'https://developers.openai.com/api/docs/guides/tools',
               'responses_tools': ['web_search', 'file_search', 'code_interpreter', 'image_generation', 'mcp', 'shell', 'tool_search']},
    'qwen': {'name': '阿里百炼', 'docs': 'https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-responses',
             'responses_tools': ['web_search', 'web_extractor', 'code_interpreter'], 'chat_tools': ['web_search']},
    'deepseek': {'name': 'DeepSeek 官方', 'docs': 'https://api-docs.deepseek.com/zh-cn/guides/responses_api/#tools',
                 'responses_tools': ['web_search'], 'stateless_responses': True},
    'gemini': {'name': 'Google Gemini', 'docs': 'https://ai.google.dev/gemini-api/docs/tools',
               'gemini_tools': ['google_search', 'url_context', 'code_execution', 'google_maps', 'file_search']},
    'custom': {'name': '自定义兼容服务', 'docs': None},
}

GEMINI_TOOL_FIELDS = {'google_search': 'googleSearch', 'url_context': 'urlContext',
                      'code_execution': 'codeExecution', 'google_maps': 'googleMaps', 'file_search': 'fileSearch'}


def provider_name(config):
    explicit = config.get('provider')
    if explicit in PROVIDERS:
        return explicit
    host = (urlparse(config.get('api_base_url') or '').hostname or '').lower()
    if host == 'api.openai.com': return 'openai'
    if host == 'api.deepseek.com': return 'deepseek'
    if host == 'generativelanguage.googleapis.com' or (not host and config.get('api_type') == 'gemini_native'): return 'gemini'
    if host.endswith('.aliyuncs.com') or host in ('dashscope.aliyuncs.com', 'dashscope-intl.aliyuncs.com'): return 'qwen'
    return 'custom'


def protocol_name(config):
    if config.get('api_type') == 'gemini_native':
        return 'interactions' if config.get('upstream_protocol') == 'interactions' else 'gemini'
    return {'responses_native': 'responses', 'anthropic_native': 'anthropic'}.get(config.get('api_type'), 'chat')


def tools_for(config):
    profile = PROVIDERS[provider_name(config)]
    protocol = protocol_name(config)
    category = 'gemini' if protocol in ('gemini', 'interactions') else protocol
    return list(profile.get(category + '_tools', []))


def validate_tool_config(config):
    provider = config.get('provider')
    if provider is not None and provider not in PROVIDERS:
        raise ValueError('未知供应商配置')
    selected = config.get('native_tools', [])
    options = config.get('native_tool_options', {})
    if not isinstance(selected, list) or not all(isinstance(tool, str) for tool in selected):
        raise ValueError('native_tools 必须是工具名称列表')
    if not isinstance(options, dict) or not all(isinstance(value, dict) for value in options.values()):
        raise ValueError('native_tool_options 必须是以工具名称为键、参数对象为值的对象')
    invalid = set(selected) - set(tools_for(config))
    if invalid:
        raise ValueError('供应商与协议不支持工具：' + ', '.join(sorted(invalid)))


def apply_native_tool_defaults(body, config, protocol=None):
    """Only supply defaults when the caller did not provide a tools field."""
    result = copy.deepcopy(body)
    selected = config.get('native_tools') or []
    if not selected:
        return result
    protocol = protocol or protocol_name(config)
    invalid = set(selected) - set(tools_for(config))
    if invalid:
        raise ValueError('当前供应商/协议不支持配置的原生工具: ' + ', '.join(sorted(invalid)))
    options = config.get('native_tool_options') or {}
    if protocol == 'chat' and provider_name(config) == 'qwen':
        if 'web_search' in selected:
            result.setdefault('enable_search', True)
            if 'web_search' in options:
                result.setdefault('search_options', copy.deepcopy(options['web_search']))
        return result
    if 'tools' in result:
        return result
    tools = []
    for name in selected:
        detail = copy.deepcopy(options.get(name) or {})
        if protocol == 'gemini':
            tools.append({GEMINI_TOOL_FIELDS[name]: detail})
        else:
            if name == 'code_interpreter' and provider_name(config) == 'openai':
                detail.setdefault('container', {'type': 'auto'})
            if name == 'shell' and provider_name(config) == 'openai':
                detail.setdefault('environment', {'type': 'container_auto'})
            tools.append({**detail, 'type': name})
    result['tools'] = tools
    return result


def diagnose_model(alias, config):
    provider = provider_name(config)
    protocol = protocol_name(config)
    issues = []
    if not config.get('api_base_url') and protocol not in ('gemini', 'interactions'):
        issues.append({'level': 'error', 'message': '缺少上游地址'})
    if not config.get('api_key') and not config.get('api_keys'):
        issues.append({'level': 'info', 'message': '未配置上游 Key；仅适合无需认证的上游'})
    if provider == 'custom':
        issues.append({'level': 'info', 'message': '请选择实际供应商以显示已核对的原生工具；不会根据模型名推测'})
    for tool in config.get('native_tools') or []:
        if tool not in tools_for(config):
            issues.append({'level': 'error', 'message': f'{protocol} 协议未列出原生工具 {tool}'})
    if provider == 'deepseek' and protocol == 'responses':
        issues.append({'level': 'info', 'message': '官方支持 web_search；previous_response_id、store、background 等不受上游支持'})
    return {'model': alias, 'provider': provider, 'protocol': protocol,
            'native_tools': tools_for(config), 'configured_tools': config.get('native_tools') or [],
            'docs': PROVIDERS[provider]['docs'], 'evidence': 'official_documentation_and_configuration',
            'live_verified': False, 'issues': issues}
