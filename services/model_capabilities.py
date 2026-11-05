"""各模型家族的请求限制，按官方文档整理（核对日期 2026-11-05）。

只记录会导致上游报错或行为变化的差异，表外的模型一律按没有限制处理，原样转发。
来源：
- Claude: platform.claude.com/docs/en/build-with-claude/thinking，working-with-messages
- Gemini: ai.google.dev/gemini-api/docs/thinking，whats-new-gemini-3.6
- OpenAI: developers.openai.com/api/docs/guides/latest-model
"""
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
import re
from typing import Optional, Tuple

from core.conversion_report import note_loss

SAMPLING_FIELDS = ('temperature', 'top_p', 'top_k')
_THINKING_LEVEL_ORDER = ('minimal', 'low', 'medium', 'high')
_EFFORT_ORDER = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')
ANTHROPIC_EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')

# 官方 changelog 2026-11-02：这天起 2.5 Pro 路由到 3.8 Flash，2.5 Flash 路由到 3.5 Flash-Lite，限制随之变化。
GEMINI_25_ROUTED_ON = date(2026, 12, 1)
_GEMINI_25_ROUTES = ((r'gemini-2\.5-pro(?![\w.-]*(image|tts))', 'gemini-3.8-flash'),
                     (r'gemini-2\.5-flash(?![\w.-]*(lite|image|tts))', 'gemini-3.5-flash-lite'))

# 工具调用走 Chat 的限制：不走 Responses 就只能在 reasoning_effort=none 时使用，或完全不可用。
CHAT_TOOLS_FREE = 'free'
CHAT_TOOLS_EFFORT_NONE = 'effort_none_only'
CHAT_TOOLS_RESPONSES_ONLY = 'responses_only'


@dataclass(frozen=True)
class ModelLimits:
    sampling: bool = True              # 接受非默认的 temperature/top_p/top_k
    prefill: bool = True               # 接受以 assistant/model 结尾的请求
    thinking_budget: bool = True       # 接受手动思考预算
    thinking_disable: bool = True      # 可以关闭思考
    adaptive_thinking: bool = True     # Claude 接受 thinking.type=adaptive
    thinking_levels: Optional[Tuple[str, ...]] = None  # Gemini 支持的 thinking_level，None 表示未知
    efforts: Optional[Tuple[str, ...]] = None          # 支持的 effort，None 表示未知
    chat_tools: str = CHAT_TOOLS_FREE
    # 非 none 强度下不接受采样参数；'default_on' 表示未指定强度时也按开启处理。
    sampling_needs_effort_none: Optional[str] = None
    function_response_media: bool = False  # 函数结果可以直接带文件和音视频


def _claude_limits(name: str) -> Optional[ModelLimits]:
    if re.search(r'claude-(fable|mythos)', name):
        preview = 'preview' in name
        return ModelLimits(sampling=False, prefill=False, thinking_budget=preview, thinking_disable=False,
                           efforts=ANTHROPIC_EFFORTS)
    match = re.search(r'claude-(opus|sonnet|haiku)-(\d+)(?:[-.](\d)(?!\d))?', name)
    if not match:
        return None
    family, version = match[1], (int(match[2]), int(match[3] or 0))
    return ModelLimits(
        sampling=version < (4, 7),
        prefill=version < (4, 6),
        thinking_budget=version < (4, 7),
        thinking_disable=not (version == (5, 5) and family in ('opus', 'sonnet')),
        adaptive_thinking=version >= (4, 6),
        efforts=ANTHROPIC_EFFORTS,
    )


# 顺序敏感：更具体的型号放前面。
_GEMINI_LEVELS = (
    (r'gemini-3\.1-flash-lite-image', ('minimal', 'high')),
    (r'gemini-3-pro', ('low', 'high')),
    (r'gemini-3\.1-pro', ('low', 'medium', 'high')),
    (r'gemini-3\.5-flash-lite', ('minimal', 'low', 'medium', 'high')),
    (r'gemini-3\.6', ('minimal', 'low', 'medium', 'high')),
    (r'gemini-3-flash', ('minimal', 'low', 'medium', 'high')),
    (r'gemini-(3\.[7-9]|[4-9])', ('low', 'medium', 'high')),
    (r'gemini-2\.5', ('low', 'medium', 'high')),
)


def _gemini_limits(name: str) -> Optional[ModelLimits]:
    if 'gemini-' not in name:
        return None
    levels = next((levels for pattern, levels in _GEMINI_LEVELS if re.search(pattern, name)), None)
    # 3.6 Flash、3.5 Flash-Lite 起：采样参数被忽略（以后的型号报 400），预填充直接 400，预算换成等级。
    strict = bool(re.search(r'gemini-(3\.[6-9]|3\.5-flash-lite|[4-9])', name))
    modern = bool(re.search(r'gemini-(3|[4-9])', name))
    return ModelLimits(sampling=not strict, prefill=not strict, thinking_budget=not strict,
                       thinking_levels=levels, function_response_media=modern)


def _openai_limits(name: str) -> Optional[ModelLimits]:
    if re.search(r'gpt-6[.-]', name):
        no_none = bool(re.search(r'gpt-6-astra|gpt-6\.1-sol', name))
        efforts = _EFFORT_ORDER[2:] if no_none else ('none', *_EFFORT_ORDER[2:])
        responses_only = bool(re.search(r'gpt-6-astra|gpt-6\.1-(sol|terra)', name))
        return ModelLimits(efforts=efforts, sampling_needs_effort_none='default_on',
                           chat_tools=CHAT_TOOLS_RESPONSES_ONLY if responses_only else CHAT_TOOLS_EFFORT_NONE)
    if re.search(r'gpt-5(\.\d+)?(-|$)', name):
        return ModelLimits(sampling_needs_effort_none='explicit')
    return None


def gemini_route_target(model_id: str) -> Optional[str]:
    """官方会把这个型号路由到哪个新模型；不受影响返回 None。"""
    name = str(model_id or '').lower()
    return next((target for pattern, target in _GEMINI_25_ROUTES if re.search(pattern, name)), None)


@lru_cache(maxsize=512)
def _limits(name: str, routed: bool) -> ModelLimits:
    if routed:
        name = gemini_route_target(name) or name
    return _claude_limits(name) or _gemini_limits(name) or _openai_limits(name) or ModelLimits()


def limits_for(model_id: str) -> ModelLimits:
    return _limits(str(model_id or '').lower(), date.today() >= GEMINI_25_ROUTED_ON)


def _nearest(value: str, allowed, order) -> str:
    """取同序列里最接近的值，距离相同时取较高的一档。"""
    if value in allowed or value not in order:
        return value
    position = order.index(value)
    return min((item for item in allowed if item in order),
               key=lambda item: (abs(order.index(item) - position), -order.index(item)))


def supported_thinking_level(model_id: str, level: Optional[str]) -> Optional[str]:
    """把 Gemini thinking_level（小写）调到模型支持的最近一档。"""
    allowed = limits_for(model_id).thinking_levels
    if not level or not allowed:
        return level
    adjusted = _nearest(str(level).lower(), allowed, _THINKING_LEVEL_ORDER)
    if adjusted != str(level).lower():
        note_loss('thinking_level', f'{model_id} 不支持 {level}，已改用 {adjusted}')
    return adjusted


def supported_effort(model_id: str, effort: Optional[str], default_levels: Optional[Tuple[str, ...]] = None) -> Optional[str]:
    """把 reasoning effort 调到模型支持的最近一档；型号未知时按 default_levels。"""
    allowed = limits_for(model_id).efforts or default_levels
    if not effort or not allowed:
        return effort
    adjusted = _nearest(str(effort).lower(), allowed, _EFFORT_ORDER)
    if adjusted != str(effort).lower():
        note_loss('reasoning_effort', f'{model_id} 不支持 {effort}，已改用 {adjusted}')
    return adjusted


def budget_to_effort(budget: Optional[int]) -> Optional[str]:
    """手动思考预算没有等价字段时的最近档位，分界取各家文档里常见的默认预算。"""
    if budget is None:
        return None
    budget = int(budget)
    if budget <= 0:
        return 'none'
    if budget <= 4096:
        return 'low'
    return 'medium' if budget <= 16384 else 'high'


def sanitize_anthropic_body(body: dict, model_id: str) -> dict:
    """就地修正 Anthropic 请求里模型一定会拒绝的字段。在合并端点自定义参数之前调用。"""
    limits = limits_for(model_id)
    drop_unsupported_sampling(body, model_id)
    thinking = body.get('thinking')
    if isinstance(thinking, dict):
        if thinking.get('type') == 'disabled' and not limits.thinking_disable:
            del body['thinking']
            note_loss('thinking', f'{model_id} 不能关闭思考，已保持默认')
        elif thinking.get('type') == 'enabled' and not limits.thinking_budget:
            # 新模型思考强度由 effort 决定，预算只能换算成最接近的档位。
            effort = budget_to_effort(thinking.get('budget_tokens'))
            body['thinking'] = {key: value for key, value in thinking.items() if key != 'budget_tokens'}
            body['thinking']['type'] = 'adaptive'
            output = dict(body.get('output_config') or {})
            if effort and effort != 'none':
                output.setdefault('effort', effort)
                body['output_config'] = output
            note_loss('thinking.budget_tokens', f'{model_id} 不接受手动思考预算，已改用 adaptive')
    return body


def drop_unsupported_sampling(body: dict, model_id: str, effort: Optional[str] = None) -> dict:
    """就地去掉模型不接受的采样参数。调用方应在合并端点自定义参数之前执行，让显式配置优先。"""
    limits = limits_for(model_id)
    fields = ()
    if not limits.sampling:
        fields = SAMPLING_FIELDS
    elif limits.sampling_needs_effort_none and effort != 'none':
        if limits.sampling_needs_effort_none == 'default_on' or effort:
            fields = ('temperature', 'top_p', 'top_logprobs', 'logprobs')
    for field in fields:
        if field in body:
            del body[field]
            note_loss(field, f'{model_id} 不接受此参数，未发送到上游')
    return body
