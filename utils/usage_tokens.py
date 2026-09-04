"""上游 usage 的思考 token 归一化。

不同上游对 completion_tokens 是否包含思考 token 的约定并不一致：

- OpenAI 官方与多数兼容 API：completion_tokens 已含 reasoning_tokens，
  满足 total_tokens == prompt_tokens + completion_tokens
- 部分中转与自建网关：completion_tokens 只统计正文，思考量放在
  completion_tokens_details.reasoning_tokens，
  满足 total_tokens == prompt_tokens + completion_tokens + reasoning_tokens

模型配置项 completion_tokens_mode 决定下发给下游的 completion_tokens 语义：

- merge（默认）：completion_tokens = 正文 + 思考，即真实总输出
- separate：completion_tokens 只含正文，思考量仅在 details 中体现

两种模式下 completion_tokens_details.reasoning_tokens 都保留，
total_tokens 都等于真实总量（输入 + 正文 + 思考）。计费与监控统计始终使用
真实总输出，因此切换该配置只改变下游看到的数字口径，不改变成本。

merge 模式是否相加依据 total_tokens 的算术关系推断，避免对已经把思考算进
completion_tokens 的标准 OpenAI 响应重复相加。
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

MODE_MERGE = "merge"
MODE_SEPARATE = "separate"

# 上游可能存放思考 token 的位置，按兼容性优先级排列
_REASONING_DETAIL_KEYS = ("completion_tokens_details", "output_tokens_details")
_REASONING_DETAIL_FIELDS = ("reasoning_tokens", "reasoning_output")
_REASONING_TOP_FIELDS = (
    "reasoning_tokens",
    "reasoning_output",
    "thoughts_token_count",
    "thoughtsTokenCount",
    "total_thought_tokens",
)


def as_int(value: Any) -> int:
    """把 usage 字段转成非负整数；None/False/异常值一律按 0 处理。

    上游偶发返回 "0"、1.0、True 等形态，直接参与加法会抛 TypeError。
    """
    if value is None or value is False:
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def get_completion_tokens_mode(endpoint_config: Optional[Dict[str, Any]]) -> str:
    """读取模型级 completion_tokens_mode，非法值回落到默认 merge。"""
    mode = (endpoint_config or {}).get("completion_tokens_mode")
    return mode if mode in (MODE_MERGE, MODE_SEPARATE) else MODE_MERGE


def extract_reasoning_tokens(usage: Any) -> int:
    """提取思考 token 数。

    兼容三种上游形态：标准 completion_tokens_details / Responses 风格的
    output_tokens_details / 部分网关直接放在 usage 顶层。
    """
    if not isinstance(usage, dict):
        return 0
    for detail_key in _REASONING_DETAIL_KEYS:
        details = usage.get(detail_key)
        if isinstance(details, dict):
            for field in _REASONING_DETAIL_FIELDS:
                value = as_int(details.get(field))
                if value > 0:
                    return value
    for field in _REASONING_TOP_FIELDS:
        value = as_int(usage.get(field))
        if value > 0:
            return value
    return 0


def extract_completion_tokens(usage: Any) -> int:
    """读取上游 completion_tokens（兼容 Responses 风格的 output_tokens 命名）。"""
    if not isinstance(usage, dict):
        return 0
    return as_int(usage.get("completion_tokens")) or as_int(usage.get("output_tokens"))


def extract_prompt_tokens(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    return as_int(usage.get("prompt_tokens")) or as_int(usage.get("input_tokens"))


def completion_excludes_reasoning(
    prompt_tokens: int,
    completion_tokens: int,
    reasoning_tokens: int,
    total_tokens: int,
) -> bool:
    """判断上游 completion_tokens 是否不含思考量（即需要相加）。

    total_tokens 可用时按算术关系判定，两种形态互斥因此不存在歧义；
    total_tokens 缺失时退化为"正文比思考还短必然是分开的"这一保守判据。
    """
    if reasoning_tokens <= 0:
        return False
    if total_tokens > 0:
        if total_tokens >= prompt_tokens + completion_tokens + reasoning_tokens:
            return True
        if total_tokens <= prompt_tokens + completion_tokens:
            return False
    return completion_tokens < reasoning_tokens


@dataclass(frozen=True)
class UsageTokens:
    prompt_tokens: int
    content_tokens: int
    reasoning_tokens: int
    output_tokens: int
    reported_completion_tokens: int
    total_tokens: int
    changed: bool


def resolve_usage_tokens(usage: Any, mode: str = MODE_MERGE) -> UsageTokens:
    """按模式解析 usage 的 token 口径，不修改传入对象。"""
    prompt = extract_prompt_tokens(usage)
    completion = extract_completion_tokens(usage)
    reasoning = extract_reasoning_tokens(usage)
    total = as_int(usage.get("total_tokens")) if isinstance(usage, dict) else 0

    if completion_excludes_reasoning(prompt, completion, reasoning, total):
        content = completion
    else:
        content = max(completion - reasoning, 0)
    output = content + reasoning
    reported = output if mode != MODE_SEPARATE else content
    if prompt > 0:
        resolved_total = max(total, prompt + output)
    else:
        resolved_total = max(total, output)
    return UsageTokens(
        prompt_tokens=prompt,
        content_tokens=content,
        reasoning_tokens=reasoning,
        output_tokens=output,
        reported_completion_tokens=reported,
        total_tokens=resolved_total,
        changed=False,
    )


def apply_usage_tokens(usage: Any, mode: str = MODE_MERGE) -> UsageTokens:
    """按模式把 token 口径写回 usage 字典（就地修改），返回解析结果。

    无思考 token 时原样返回不做任何改动，保证非思考模型零风险。
    该操作幂等：对已归一的结果再次调用，输出保持不变。
    """
    tokens = resolve_usage_tokens(usage, mode)
    if tokens.reasoning_tokens <= 0 or not isinstance(usage, dict):
        return tokens

    changed = False
    # 上游可能用 Chat 命名（completion_tokens）或 Responses 命名（output_tokens），
    # details 键位必须跟着配对，否则会出现 output_tokens + completion_tokens_details
    # 这种两套命名混在一起的怪体
    if "completion_tokens" in usage:
        target_key, detail_key = "completion_tokens", "completion_tokens_details"
    elif "output_tokens" in usage:
        target_key, detail_key = "output_tokens", "output_tokens_details"
    else:
        target_key, detail_key = None, "completion_tokens_details"

    if target_key and as_int(usage.get(target_key)) != tokens.reported_completion_tokens:
        usage[target_key] = tokens.reported_completion_tokens
        changed = True

    if tokens.total_tokens > 0 and as_int(usage.get("total_tokens")) != tokens.total_tokens:
        usage["total_tokens"] = tokens.total_tokens
        changed = True

    details = usage.get(detail_key)
    if not isinstance(details, dict):
        details = {}
    if as_int(details.get("reasoning_tokens")) != tokens.reasoning_tokens:
        details["reasoning_tokens"] = tokens.reasoning_tokens
        usage[detail_key] = details
        changed = True

    # 上游原本在顶层给过 reasoning_tokens 的内部/网关形态，保持同步不产生两套数字
    if "reasoning_tokens" in usage and as_int(usage.get("reasoning_tokens")) != tokens.reasoning_tokens:
        usage["reasoning_tokens"] = tokens.reasoning_tokens
        changed = True

    return UsageTokens(
        prompt_tokens=tokens.prompt_tokens,
        content_tokens=tokens.content_tokens,
        reasoning_tokens=tokens.reasoning_tokens,
        output_tokens=tokens.output_tokens,
        reported_completion_tokens=tokens.reported_completion_tokens,
        total_tokens=tokens.total_tokens,
        changed=changed,
    )


def total_output_tokens(usage: Any) -> int:
    """真实输出 token（正文 + 思考）。

    用于计费与监控：无论下游看到的 completion_tokens 是合并口径还是正文口径，
    统计侧还原出的总量都相同，切换配置不会改变成本。
    """
    return resolve_usage_tokens(usage, MODE_MERGE).output_tokens


def compose_chat_usage(
    prompt_tokens: int,
    output_tokens: int,
    reasoning_tokens: int = 0,
    cached_tokens: int = 0,
    completion_mode: str = MODE_MERGE,
    total_tokens: int = 0,
) -> Dict[str, Any]:
    """组装下发给下游的 OpenAI 风格 usage 字典。

    output_tokens 传真实总输出（含思考），由本函数按 completion_mode 决定
    completion_tokens 写总量还是正文量，思考量始终保留在 details 中。
    """
    prompt_tokens = as_int(prompt_tokens)
    output_tokens = as_int(output_tokens)
    reasoning_tokens = as_int(reasoning_tokens)
    cached_tokens = as_int(cached_tokens)
    content_tokens = max(output_tokens - reasoning_tokens, 0)
    completion_tokens = (
        content_tokens if completion_mode == MODE_SEPARATE else output_tokens
    )
    usage: Dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": as_int(total_tokens) or prompt_tokens + max(output_tokens, content_tokens),
    }
    if cached_tokens > 0:
        usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    if reasoning_tokens > 0:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    return usage
