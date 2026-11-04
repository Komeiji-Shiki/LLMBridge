"""Documented provider tool profiles. Configuration is not a live capability probe."""
import copy
from urllib.parse import urlparse

from core.constants import GeminiDefaults

PROVIDERS = {
    'openai': {'name': 'OpenAI', 'docs': 'https://developers.openai.com/api/docs/guides/tools',
               'responses_tools': ['web_search', 'file_search', 'code_interpreter', 'image_generation', 'mcp', 'shell', 'tool_search']},
    'qwen': {'name': '阿里百炼', 'docs': 'https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-responses',
             'responses_tools': ['web_search', 'web_extractor', 'code_interpreter'], 'chat_tools': ['web_search']},
    'deepseek': {'name': 'DeepSeek 官方', 'docs': 'https://api-docs.deepseek.com/zh-cn/guides/responses_api/#tools',
                 'responses_tools': ['web_search'], 'stateless_responses': True},
    'gemini': {'name': 'Google Gemini', 'docs': 'https://ai.google.dev/gemini-api/docs/tools',
               'gemini_tools': ['google_search', 'url_context', 'code_execution', 'google_maps', 'file_search']},
    'openrouter': {'name': 'OpenRouter', 'docs': 'https://openrouter.ai/docs/use-cases/reasoning-tokens'},
    'workbuddy': {'name': 'WorkBuddy', 'docs': None},
    'custom': {'name': '自定义兼容服务', 'docs': None},
}

# WorkBuddy 国内版、国际版和 CodeBuddy 共用的上游域名。
WORKBUDDY_DOMAINS = ('workbuddy.cn', 'workbuddy.ai', 'codebuddy.cn', 'codebuddy.ai', 'copilot.tencent.com')

# 同一模型经不同中转访问时协议细节不变，而中转地址看不出模型来源，所以按模型家族判断。
MODEL_FAMILIES = ('gemini', 'deepseek', 'qwen')
NO_MODEL_FAMILY = 'other'

GEMINI_TOOL_FIELDS = {'google_search': 'googleSearch', 'url_context': 'urlContext',
                      'code_execution': 'codeExecution', 'google_maps': 'googleMaps', 'file_search': 'fileSearch'}


def provider_name(config):
    explicit = config.get('provider')
    if explicit in PROVIDERS:
        return explicit
    host = (urlparse(config.get('api_base_url') or '').hostname or '').lower()
    if host == 'api.openai.com': return 'openai'
    if host == 'api.deepseek.com': return 'deepseek'
    if host == GeminiDefaults.HOST or (not host and config.get('api_type') == 'gemini_native'): return 'gemini'
    if host.endswith('.aliyuncs.com') or host in ('dashscope.aliyuncs.com', 'dashscope-intl.aliyuncs.com'): return 'qwen'
    if host == 'openrouter.ai' or host.endswith('.openrouter.ai'): return 'openrouter'
    if any(host == domain or host.endswith('.' + domain) for domain in WORKBUDDY_DOMAINS): return 'workbuddy'
    return 'custom'


def model_family(config, *model_names):
    """判断端点实际跑的模型家族，返回 None 表示不需要任何家族专属处理。

    优先级：端点显式配置 model_family > 模型名 > 官方供应商地址。模型名排在
    供应商前面，是因为百炼这类平台也托管其他家族的模型。
    """
    explicit = config.get('model_family')
    if explicit:
        return None if explicit == NO_MODEL_FAMILY else explicit
    names = [str(name).lower() for name in model_names if name]
    for family in MODEL_FAMILIES:
        if any(family in name for name in names):
            return family
    provider = provider_name(config)
    return provider if provider in MODEL_FAMILIES else None


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
    family = config.get('model_family')
    if family and family not in (*MODEL_FAMILIES, NO_MODEL_FAMILY):
        raise ValueError('未知模型家族配置')
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
