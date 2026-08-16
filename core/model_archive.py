"""
模型归档核心逻辑

归档状态直接写在 model_endpoint_map.json 每个模型配置的 archived 字段上
（与 /v1/models 列表接口、config 热重载共用同一数据源，不引入第二份状态）。

本模块只包含纯函数与数据查询，不做文件 IO 与加锁；
文件读写/锁/配置重载由调用方（admin_routes / 后台任务）注入，避免循环依赖。
"""
import logging
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def is_model_archived(config) -> bool:
    """判断模型配置是否已归档。

    兼容两种形态：
    - dict：直接读 archived 字段
    - list（多端点轮询）：取第一个元素判断（与 models_api._is_archived 语义一致）
    """
    if isinstance(config, dict):
        return config.get("archived", False)
    if isinstance(config, list) and config:
        first = config[0] if isinstance(config[0], dict) else {}
        return first.get("archived", False)
    return False


def set_archive_flags(model_map: dict, model_names, archived: bool):
    """批量设置/清除 archived 标志，返回 (新 map, 实际变更的模型名列表)。

    list（多端点）配置的所有元素都会设置/清除，保证轮询到任意端点时
    归档判断结果一致；dict 配置只维护顶层字段。
    不存在的模型名静默忽略。
    """
    new_map = dict(model_map)
    changed: List[str] = []

    for name in model_names:
        if name not in new_map:
            continue
        cfg = new_map[name]

        if isinstance(cfg, dict):
            if bool(cfg.get("archived", False)) != archived:
                new_cfg = dict(cfg)
                if archived:
                    new_cfg["archived"] = True
                else:
                    new_cfg.pop("archived", None)
                new_map[name] = new_cfg
                changed.append(name)

        elif isinstance(cfg, list):
            new_list = []
            dirty = False
            for item in cfg:
                if isinstance(item, dict):
                    item = dict(item)
                    if bool(item.get("archived", False)) != archived:
                        dirty = True
                        if archived:
                            item["archived"] = True
                        else:
                            item.pop("archived", None)
                new_list.append(item)
            if dirty:
                new_map[name] = new_list
                changed.append(name)

    return new_map, changed


def _collect_model_candidate_keys(model_name: str, cfg) -> set:
    """收集一个模型在统计日志中可能使用的全部标识（配置键/display_name/model_id）。

    统计落库的 model 字段大部分链路取 display_name（默认=配置键），
    少部分链路可能记录 model_id，因此用多候选 key 匹配提高召回率。
    """
    candidates = {model_name}
    if isinstance(cfg, dict):
        if cfg.get("display_name"):
            candidates.add(cfg["display_name"])
        if cfg.get("model_id"):
            candidates.add(cfg["model_id"])
    elif isinstance(cfg, list) and cfg and isinstance(cfg[0], dict):
        first = cfg[0]
        if first.get("display_name"):
            candidates.add(first["display_name"])
        if first.get("model_id"):
            candidates.add(first["model_id"])
    return candidates


def find_inactive_models(model_map: dict, last_used_map: dict, days: int) -> List[str]:
    """找出超过 days 天未被调用且当前未归档的模型。

    - last_used_map: {统计标识: 最后调用时间戳}，来自 build_last_used_map
    - 每个模型用配置键/display_name/model_id 多候选匹配，取最大时间戳
    - 完全没有调用记录的模型跳过（可能是刚配置的新模型，无法判断闲置）
    """
    if days <= 0:
        return []
    cutoff = time.time() - days * 86400
    inactive: List[str] = []

    for name, cfg in model_map.items():
        if is_model_archived(cfg):
            continue

        last_ts = None
        for cand in _collect_model_candidate_keys(name, cfg):
            ts = last_used_map.get(cand)
            if ts is not None:
                last_ts = ts if last_ts is None else max(last_ts, ts)

        if last_ts is None:
            continue  # 无调用记录，无法判断，跳过
        if last_ts < cutoff:
            inactive.append(name)

    return inactive


def build_last_used_map(stats_db, monitoring_service) -> Dict[str, float]:
    """构建 {模型统计标识: 最后调用时间戳}。

    数据源优先级：
    1. SQLite requests 表（GROUP BY model 取 MAX(timestamp)），数据全、可跨重启
    2. 内存 recent_requests（数据库不可用时的兜底，只有最近 2000 条）
    """
    result: Dict[str, float] = {}

    # SQLite 优先（有索引 idx_model_timestamp 支撑 GROUP BY）
    try:
        if stats_db is not None and stats_db._check_enabled():
            conn = stats_db._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT model, MAX(timestamp) FROM requests GROUP BY model")
            for row in cursor.fetchall():
                result[row[0]] = row[1]
            if result:
                return result
    except Exception as e:
        logger.warning(f"[MODEL_ARCHIVE] SQLite 查询模型最后调用时间失败，回退内存统计: {e}")

    # 兜底：内存 recent_requests（deque of dict，含 model/timestamp 字段）
    try:
        if monitoring_service is not None:
            for req in list(getattr(monitoring_service, "recent_requests", None) or []):
                model = req.get("model") if isinstance(req, dict) else None
                ts = req.get("timestamp") if isinstance(req, dict) else None
                if model and ts:
                    result[model] = max(result.get(model, 0), ts)
    except Exception as e:
        logger.warning(f"[MODEL_ARCHIVE] 内存统计兜底失败: {e}")

    return result
