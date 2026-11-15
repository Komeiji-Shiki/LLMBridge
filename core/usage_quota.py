"""
用量额度：访客 Key、模型、渠道共用的周期统计与上限判断

额度配置统一写成一个对象：
    {"period": "day", "cost_limit": 5, "cost_currency": "USD", "token_limit": 0, "request_limit": 0}
period 是统计周期（total 累计 / day / week / month，按服务器本地时间切分），
各类上限为 0 表示不限制，任一项用完即暂停。

用量记录 {"period_start", "requests", "tokens", "cost": {货币: 金额}} 由调用方保存；
本模块只有纯函数，不做文件 IO 与加锁。
"""
import math
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

QUOTA_PERIODS = ("total", "day", "week", "month")
COST_CURRENCIES = ("USD", "CNY")
LIMIT_FIELDS = {"cost": "cost_limit", "tokens": "token_limit", "requests": "request_limit"}
PERIOD_LABELS = {"total": "累计", "day": "今日", "week": "本周", "month": "本月"}
QUOTA_LABELS = {"cost": "费用", "tokens": "Token", "requests": "请求次数"}


def normalize_quota(data: Any) -> Optional[Dict[str, Any]]:
    """校验并整理额度配置，格式不对时抛 ValueError；没有任何上限的累计额度等于不设，返回 None。"""
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError("quota 必须是 JSON 对象")
    unknown = set(data) - {"period", "cost_currency", *LIMIT_FIELDS.values()}
    if unknown:
        raise ValueError(f"quota 不支持 {', '.join(sorted(unknown))}")
    period = data.get("period", "total")
    if period not in QUOTA_PERIODS:
        raise ValueError(f"quota.period 只能是 {', '.join(QUOTA_PERIODS)}")
    currency = data.get("cost_currency", "USD")
    if currency not in COST_CURRENCIES:
        raise ValueError(f"quota.cost_currency 只能是 {', '.join(COST_CURRENCIES)}")
    quota = {"period": period, "cost_currency": currency}
    for kind, field in LIMIT_FIELDS.items():
        value = data.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"quota.{field} 必须是非负数")
        quota[field] = float(value) if kind == "cost" else int(value)
    if period == "total" and not has_limits(quota):
        return None
    return quota


def has_limits(quota: Optional[Dict[str, Any]]) -> bool:
    return bool(quota) and any(quota.get(field) for field in LIMIT_FIELDS.values())


def period_bounds(period: str, now: float) -> Tuple[Optional[float], Optional[float]]:
    """按服务器本地时间切分自然周期，返回 (起点, 下次重置)；累计周期没有自动边界。"""
    if period not in ("day", "week", "month"):
        return None, None
    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        start, end = today, today + timedelta(days=1)
    elif period == "week":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=7)
    else:
        start = today.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
    return start.timestamp(), end.timestamp()


def current_usage(usage: Optional[Dict[str, Any]], period: str, now: float) -> Dict[str, Any]:
    """取当前周期的用量；还没有记录或已跨入新周期时返回清零的新记录（调用方据此判断要不要保存）。"""
    start, _ = period_bounds(period, now)
    if usage and (start is None or usage.get("period_start") == start):
        return usage
    return {"period_start": start or now, "requests": 0, "tokens": 0, "cost": {}}


def cost_of(cost_info: Optional[Dict[str, Any]]) -> Tuple[float, str]:
    """从请求的计费结果取 (费用, 货币)；只区分 USD 和 CNY。"""
    cost_info = cost_info or {}
    return float(cost_info.get("total_cost") or 0), "CNY" if cost_info.get("currency") == "CNY" else "USD"


def add_usage(usage: Dict[str, Any], tokens: int, cost: float, currency: str) -> None:
    usage["tokens"] += tokens
    if cost:
        usage["cost"][currency] = usage["cost"].get(currency, 0.0) + cost


def cost_in(currency: str, costs: Dict[str, float]) -> float:
    """把按货币分开累计的费用折算成额度货币，汇率取当前配置。"""
    other = "CNY" if currency == "USD" else "USD"
    total = costs.get(currency, 0.0)
    if costs.get(other):
        from core.db_stats import get_exchange_rates
        usd_to_cny, cny_to_usd = get_exchange_rates()
        total += costs[other] * (usd_to_cny if other == "USD" else cny_to_usd)
    return total


def snapshot(quota: Optional[Dict[str, Any]], usage: Dict[str, Any], now: float) -> Dict[str, Any]:
    """当前周期的用量、上限和已用尽的项目，费用按额度货币折算。"""
    quota = quota or {}
    period = quota.get("period", "total")
    currency = quota.get("cost_currency", "USD")
    used = {"cost": cost_in(currency, usage["cost"]), "tokens": usage["tokens"], "requests": usage["requests"]}
    limits = {kind: quota.get(field) or 0 for kind, field in LIMIT_FIELDS.items()}
    return {
        "period": period,
        "period_start": usage["period_start"],
        "resets_at": period_bounds(period, now)[1],
        "currency": currency,
        "used": used,
        "limits": limits,
        "exhausted": [kind for kind, limit in limits.items() if limit and used[kind] >= limit],
    }


def format_quota(kind: str, value: float, currency: str) -> str:
    if kind == "cost":
        return ("¥" if currency == "CNY" else "$") + f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{int(value):,}"


def format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def exhausted_message(subject: str, quota: Dict[str, Any]) -> str:
    """额度用尽的提示：写明哪一项、用了多少、何时恢复。subject 如“此 API Key”“模型 gpt-5”。"""
    currency = quota["currency"]
    details = "，".join(
        f"{QUOTA_LABELS[kind]} {format_quota(kind, quota['used'][kind], currency)}"
        f" / {format_quota(kind, quota['limits'][kind], currency)}"
        for kind in quota["exhausted"]
    )
    when = f"将于 {format_time(quota['resets_at'])} 恢复" if quota["resets_at"] else "请联系管理员调整额度"
    return f"{subject} {PERIOD_LABELS.get(quota['period'], '')}额度已用完（{details}），{when}。"
