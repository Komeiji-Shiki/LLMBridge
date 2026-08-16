"""
管理面板路由
处理模型配置、系统概览、Token统计等管理功能
"""
import asyncio
import json
import logging
import time
import aiohttp
from datetime import datetime
from pathlib import Path
from typing import Optional
from core.config_loader import (
    CONFIG, MODEL_ENDPOINT_MAP, MODEL_NAME_TO_ID_MAP,
    load_config, load_model_endpoint_map, _parse_jsonc,
)
from core.model_archive import (
    is_model_archived, set_archive_flags,
    find_inactive_models, build_last_used_map,
)
from core.app_state import get_app_state
from core.db_stats import stats_db, get_exchange_rates
from modules.monitoring import monitoring_service, MonitorConfig
from modules.token_counter import (
    estimate_message_tokens, estimate_tokens, get_token_counter_info,
    get_all_tokenizers_status, calculate_tokens_for_text, compare_tokenizers,
    install_tokenizer_package, add_custom_tokenizer, delete_custom_tokenizer,
    list_custom_tokenizers,
)
from utils.jsonc_edit import (
    atomic_write_json, atomic_write_text, set_jsonc_value, set_jsonc_values,
)
from ._direct_api_utils import set_sticky_current_key
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.responses import Response

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin"])

_app_state = get_app_state()

MODEL_ENDPOINT_MAP_FILE = 'model_endpoint_map.json'
CONFIG_FILE = 'config.jsonc'

# asyncio.Lock 串行化对 model_endpoint_map.json 的读写（update/delete/reorder 并发调用）
_MODEL_ENDPOINT_MAP_LOCK = asyncio.Lock()


# ============================================================================
# 配置文件读写（同步 IO 一律走线程池，写入一律原子）
# ============================================================================
# 🔧 修复：旧版在 async 端点里直接 open()/json.dump() 读写 model_endpoint_map.json
# （已 160KB+）与 config.jsonc。两个问题：
#   1. 同步磁盘 IO 阻塞事件循环，管理面板保存一次配置会卡住所有并发流式请求；
#   2. 直接覆盖写非原子，进程在写入途中被强杀会留下半截文件，整套模型配置丢失。

def _read_text_sync(path: str) -> str:
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


async def read_text_file(path: str) -> str:
    return await asyncio.to_thread(_read_text_sync, path)


async def read_json_file(path: str):
    return json.loads(await read_text_file(path))


async def write_json_file(path: str, data) -> None:
    await asyncio.to_thread(atomic_write_json, path, data)


async def write_text_file(path: str, content: str) -> None:
    await asyncio.to_thread(atomic_write_text, path, content)

# 🔧 性能修复：改为 asyncio.Lock，避免在 async 函数中阻塞事件循环
_ADMIN_STATS_CACHE_LOCK = asyncio.Lock()

# 改用 TTLCache 防止缓存无限增长（旧版裸 dict 无上限，换区间永久留一条）
from cachetools import TTLCache
_ADMIN_STATS_CACHE = {
    "overview": TTLCache(maxsize=256, ttl=10),
    "request_stats": TTLCache(maxsize=256, ttl=15),
    "token_stats": TTLCache(maxsize=256, ttl=15)
}


def _build_admin_cache_key(*parts) -> str:
    return json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)


async def _get_admin_cached_response(cache_name: str, cache_key: str):
    # TTLCache 自带 TTL 自动逐出，不需要手动检查时间戳
    async with _ADMIN_STATS_CACHE_LOCK:
        bucket = _ADMIN_STATS_CACHE[cache_name]
        return bucket.get(cache_key)


async def _set_admin_cached_response(cache_name: str, cache_key: str, value):
    async with _ADMIN_STATS_CACHE_LOCK:
        _ADMIN_STATS_CACHE[cache_name][cache_key] = value


async def admin_dashboard():
    """返回管理界面HTML页面"""
    try:
        with open('admin.html', 'r', encoding='utf-8') as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>管理界面文件未找到</h1><p>请确保 admin.html 文件在正确的位置。</p>",
            status_code=404
        )


async def get_models_config(
    MODEL_ENDPOINT_MAP: dict,
    MODEL_NAME_TO_ID_MAP: dict,
    load_model_endpoint_map_func
):
    """获取所有模型配置"""
    load_model_endpoint_map_func()
    return {
        "model_endpoint_map": MODEL_ENDPOINT_MAP,
        "models": MODEL_NAME_TO_ID_MAP
    }


async def update_model_config(
    request: Request,
    load_model_endpoint_map_func
):
    """更新模型端点配置（支持模型名称重命名）"""
    try:
        data = await request.json()
        model_name = data.get("model_name")
        old_model_name = data.get("old_model_name")
        config = data.get("config")
        
        if not model_name or config is None:
            raise HTTPException(status_code=400, detail="缺少必要参数")

        async with _MODEL_ENDPOINT_MAP_LOCK:
            # 读取现有配置
            current_config = await read_json_file(MODEL_ENDPOINT_MAP_FILE)

            if old_model_name:
                if old_model_name not in current_config:
                    raise HTTPException(status_code=404, detail=f"原模型 {old_model_name} 不存在")
                
                if old_model_name != model_name and model_name in current_config:
                    raise HTTPException(status_code=409, detail=f"模型 {model_name} 已存在，请使用其他名称")
                
                if old_model_name != model_name:
                    # 保持原有顺序，在旧位置替换为新名称
                    updated_config = {}
                    for existing_name, existing_config in current_config.items():
                        if existing_name == old_model_name:
                            updated_config[model_name] = config
                        else:
                            updated_config[existing_name] = existing_config
                    current_config = updated_config
                    message = f"模型 {old_model_name} 已重命名为 {model_name}"
                else:
                    current_config[model_name] = config
                    message = f"模型 {model_name} 配置已更新"
            else:
                # 新增或直接覆盖
                current_config[model_name] = config
                message = f"模型 {model_name} 配置已更新"

            # 写入文件（原子写，避免半截文件毁掉整套模型配置）
            await write_json_file(MODEL_ENDPOINT_MAP_FILE, current_config)

            # 重新加载配置
            await asyncio.to_thread(load_model_endpoint_map_func)

        return {"status": "success", "message": message}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新模型配置失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def delete_model_config(
    request: Request,
    load_model_endpoint_map_func
):
    """删除模型端点配置"""
    try:
        body = await request.json()
        model_name = body.get("model_name")
        if not model_name:
            raise HTTPException(status_code=400, detail="缺少 model_name 字段")

        # 读取现有配置
        current_config = await read_json_file(MODEL_ENDPOINT_MAP_FILE)

        if model_name not in current_config:
            raise HTTPException(status_code=404, detail=f"模型 {model_name} 不存在")

        # 删除配置
        del current_config[model_name]

        # 写入文件
        await write_json_file(MODEL_ENDPOINT_MAP_FILE, current_config)

        # 重新加载配置
        await asyncio.to_thread(load_model_endpoint_map_func)

        return {"status": "success", "message": f"模型 {model_name} 已删除"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除模型配置失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def reorder_models(
    request: Request,
    load_model_endpoint_map_func
):
    """重新排序模型端点配置"""
    try:
        data = await request.json()
        new_order = data.get("order")
        
        if not new_order or not isinstance(new_order, list):
            raise HTTPException(status_code=400, detail="缺少有效的order参数")
        
        # 读取现有配置
        current_config = await read_json_file(MODEL_ENDPOINT_MAP_FILE)

        # 校验所有模型名称都存在（允许只提交部分模型，如仅活跃模型）
        for model_name in new_order:
            if model_name not in current_config:
                raise HTTPException(status_code=400, detail=f"模型 {model_name} 不存在于配置中")

        # 创建新的有序字典：先按提交顺序排，未提交的模型（如归档区不参与拖拽）
        # 按原顺序追加到末尾，保证前端只提交活跃区顺序时不会丢归档模型
        reordered_config = {}
        for model_name in new_order:
            reordered_config[model_name] = current_config[model_name]
        for model_name, model_config in current_config.items():
            if model_name not in reordered_config:
                reordered_config[model_name] = model_config

        # 写入文件
        await write_json_file(MODEL_ENDPOINT_MAP_FILE, reordered_config)

        # 重新加载配置
        await asyncio.to_thread(load_model_endpoint_map_func)

        logger.info(f"✅ 模型顺序已更新: {' -> '.join(new_order)}")
        
        return {
            "status": "success",
            "message": f"已重新排序 {len(new_order)} 个模型",
            "order": new_order
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"重新排序模型失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))



# ============================================================================
# 模型归档（archived）
# ============================================================================

async def set_models_archive(
    request: Request,
    load_model_endpoint_map_func
):
    """批量归档/恢复模型。

    body: {"model_names": ["a", "b"], "archived": true|false}
    archived 缺省为 true（一键归档）。归档后的模型：
    - 不出现在 /v1/models、/v1beta/models 列表
    - 直接调用 chat/completions 等端点返回 404 模型不存在
    - 管理面板模型列表折叠展示，可随时恢复
    """
    try:
        data = await request.json()
        model_names = data.get("model_names")
        archived = bool(data.get("archived", True))
        action = "归档" if archived else "恢复"

        if not model_names or not isinstance(model_names, list):
            raise HTTPException(status_code=400, detail="缺少有效的 model_names 列表")
        model_names = [str(n) for n in model_names]

        async with _MODEL_ENDPOINT_MAP_LOCK:
            current_config = await read_json_file(MODEL_ENDPOINT_MAP_FILE)
            new_config, changed = set_archive_flags(current_config, model_names, archived)

            if changed:
                await write_json_file(MODEL_ENDPOINT_MAP_FILE, new_config)
                await asyncio.to_thread(load_model_endpoint_map_func)

        logger.info(f"✅ 模型{action}: {', '.join(changed) or '(无变更)'}")
        return {
            "status": "success",
            "message": f"已{action} {len(changed)} 个模型",
            "changed": changed,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"模型归档操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def run_auto_archive_task(days: int) -> dict:
    """扫描并归档超过 days 天未调用的模型（手动触发与后台任务共用）。

    无调用记录的模型（可能是新配置）跳过；已归档的跳过。
    返回 {"archived": [模型名...], "total_inactive": N, "total_archived": N}
    """
    if days <= 0:
        raise HTTPException(status_code=400, detail="days 必须为正整数")

    async with _MODEL_ENDPOINT_MAP_LOCK:
        current_config = await read_json_file(MODEL_ENDPOINT_MAP_FILE)
        last_used = build_last_used_map(stats_db, monitoring_service)
        inactive = find_inactive_models(current_config, last_used, days)

        if inactive:
            new_config, changed = set_archive_flags(current_config, inactive, True)
            if changed:
                await write_json_file(MODEL_ENDPOINT_MAP_FILE, new_config)
                await asyncio.to_thread(load_model_endpoint_map)
        else:
            changed = []

    logger.info(f"[MODEL_ARCHIVE] 自动归档扫描完成: {days}天阈值, 命中 {len(inactive)} 个, 实际归档 {len(changed)} 个")
    return {
        "archived": changed,
        "total_inactive": len(inactive),
        "total_archived": sum(1 for c in current_config.values() if is_model_archived(c)),
    }


async def trigger_auto_archive(request: Request):
    """手动触发一次自动归档扫描。body: {"days": 30}（缺省用配置值）"""
    try:
        data = await request.json()
        days = data.get("days")
        if days is None:
            days = (CONFIG.get("auto_archive") or {}).get("days", 30)
        days = int(days)
    except Exception:
        raise HTTPException(status_code=400, detail="days 参数无效")

    result = await run_auto_archive_task(days)
    return {
        "status": "success",
        "message": (
            f"扫描完成：{result['total_inactive']} 个模型超过 {days} 天未调用，"
            f"本次归档 {len(result['archived'])} 个（当前共已归档 {result['total_archived']} 个）"
        ),
        **result,
        "days": days,
    }


def _get_archive_config() -> dict:
    """读取自动归档配置（config.jsonc 的 auto_archive 键）"""
    cfg = CONFIG.get("auto_archive")
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "days": int(cfg.get("days", 30) or 30),
    }


async def get_archive_config():
    """获取自动归档配置"""
    return _get_archive_config()


async def update_archive_config(request: Request):
    """保存自动归档配置并立即生效。

    body: {"enabled": true, "days": 30}
    保存后若 enabled=true 立即执行一次扫描，不用等后台任务的下一个周期。
    """
    try:
        data = await request.json()
        enabled = bool(data.get("enabled", False))
        days = int(data.get("days", 30) or 30)
        if days <= 0:
            raise HTTPException(status_code=400, detail="days 必须为正整数")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="参数无效")

    # 写入 config.jsonc（保留注释、原有格式以及未在面板中编辑的配置项），再热重载
    existing_config = CONFIG.get("auto_archive")
    new_config = dict(existing_config) if isinstance(existing_config, dict) else {}
    new_config.update({"enabled": enabled, "days": days})
    content = await read_text_file(CONFIG_FILE)
    updated = await asyncio.to_thread(set_jsonc_value, content, "auto_archive", new_config)
    await write_text_file(CONFIG_FILE, updated)
    await asyncio.to_thread(load_config)

    message = f"自动归档已{'启用' if enabled else '停用'}（{days} 天未调用）"
    logger.info(f"[MODEL_ARCHIVE] 配置更新: {new_config}")

    # 启用后立即执行一次扫描，让配置立刻生效
    if enabled:
        try:
            result = await run_auto_archive_task(days)
            message += f"，本次扫描归档 {len(result['archived'])} 个模型"
        except Exception as e:
            logger.error(f"[MODEL_ARCHIVE] 启用后立即扫描失败: {e}", exc_info=True)

    return {"status": "success", "message": message, "config": new_config}

async def get_config(CONFIG: dict):
    """获取config.jsonc配置"""
    try:
        content = await read_text_file(CONFIG_FILE)
        return {"content": content, "config": CONFIG}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def update_config(
    request: Request,
    _parse_jsonc_func,
    load_config_func
):
    """更新config.jsonc配置"""
    try:
        data = await request.json()
        content = data.get("content")
        partial = data.get("config")

        if content:
            # 源码编辑模式：整体覆盖，用户看到什么就写入什么
            try:
                _parse_jsonc_func(content)
            except json.JSONDecodeError as e:
                raise HTTPException(status_code=400, detail=f"配置格式错误: {e}")
            new_content = content
        elif isinstance(partial, dict) and partial:
            # 🔧 表单模式：只定点替换给出的顶层键。
            # 旧版前端把表单收集到的对象 JSON.stringify 后当 content 提交，
            # 等于用一份无注释的纯 JSON 覆盖 config.jsonc —— 在配置页点一次
            # "保存"，整份配置文件的注释就全部消失了。
            original = await read_text_file(CONFIG_FILE)
            # 只替换值真正发生变化的键：表单会回传全部字段，对未改动的键做
            # 同值替换会白白重排它们的格式（内联对象被展开成多行等）
            try:
                current = _parse_jsonc_func(original)
            except json.JSONDecodeError:
                current = {}
            changed = {k: v for k, v in partial.items() if current.get(k) != v}
            if not changed:
                return {"status": "success", "message": "配置无变化"}
            new_content = set_jsonc_values(original, changed)
            logger.info(f"[CONFIG] 表单模式更新 {len(changed)} 个字段: {', '.join(sorted(changed))}")
            try:
                _parse_jsonc_func(new_content)
            except json.JSONDecodeError as e:
                logger.error(f"生成的 config.jsonc 非法，已放弃写入: {e}")
                raise HTTPException(status_code=500, detail=f"配置写入前校验失败: {e}")
        else:
            raise HTTPException(status_code=400, detail="缺少配置内容")

        # 写入文件（原子写）
        await write_text_file(CONFIG_FILE, new_content)

        # 重新加载配置
        await asyncio.to_thread(load_config_func, True)

        return {"status": "success", "message": "配置已更新"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新配置失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def get_overview(
    monitoring_service,
    stats_db,
    MonitorConfig,
    browser_ws,
    browser_connections: dict,
    browser_connections_lock,
    tab_connection_times: dict,
    tab_request_counts: dict,
    CONFIG: dict,
    MODEL_ENDPOINT_MAP: dict
):
    """获取系统概览信息"""
    # 🔧 A1 修复：使用 async 版本，避免 threading.Lock 阻塞事件循环
    summary = await monitoring_service.get_summary_async()
    
    # 请求统计：优先走缓存，避免重启后首屏冷查询
    stats_from_source = await _get_admin_cached_response("overview", "stats")
    if stats_from_source is None:
        stats_from_source = summary['stats']  # 默认使用内存统计作为后备
        
        try:
            if stats_db.enabled:
                db_stats = await stats_db.get_request_summary_async()
                if db_stats:
                    stats_from_source = {
                        "total_requests": db_stats.get('total_requests', 0),
                        "success_requests": db_stats.get('success_requests', 0),
                        "failed_requests": db_stats.get('failed_requests', 0)
                    }
                    logger.debug(f"[OVERVIEW] ✅ 从SQLite轻量汇总读取")
                else:
                    logger.warning(f"[OVERVIEW] SQLite查询失败，尝试从stats.json读取")
                    raise Exception("SQLite查询失败")
            else:
                stats_path = MonitorConfig.LOG_DIR / MonitorConfig.STATS_FILE
                if stats_path.exists():
                    with open(stats_path, 'r', encoding='utf-8') as f:
                        stats_data = json.load(f)
                    stats_from_source = {
                        "total_requests": stats_data.get('total_requests_all_time', 0),
                        "success_requests": stats_data.get('total_success_all_time', 0),
                        "failed_requests": stats_data.get('total_failed_all_time', 0)
                    }
                else:
                    logger.warning(f"[OVERVIEW] stats.json不存在，使用内存统计")
        except Exception as e:
            try:
                stats_path = MonitorConfig.LOG_DIR / MonitorConfig.STATS_FILE
                if stats_path.exists():
                    with open(stats_path, 'r', encoding='utf-8') as f:
                        stats_data = json.load(f)
                    stats_from_source = {
                        "total_requests": stats_data.get('total_requests_all_time', 0),
                        "success_requests": stats_data.get('total_success_all_time', 0),
                        "failed_requests": stats_data.get('total_failed_all_time', 0)
                    }
                else:
                    logger.error(f"[OVERVIEW] 所有数据源都失败，使用内存统计: {e}")
            except Exception as fallback_error:
                logger.error(f"[OVERVIEW] stats.json读取也失败，使用内存统计: {fallback_error}")
        
        await _set_admin_cached_response("overview", "stats", stats_from_source)
    
    # 获取标签页信息
    async with browser_connections_lock:
        tabs_info = []
        current_time = time.time()
        
        for tab_id, ws in browser_connections.items():
            connection_start = tab_connection_times.get(tab_id, current_time)
            connected_duration = current_time - connection_start
            load = tab_request_counts.get(tab_id, 0)
            
            tabs_info.append({
                "tab_id": tab_id,
                "connected": ws.client_state.name == 'CONNECTED' if ws else False,
                "active_requests": load,
                "connected_duration": connected_duration
            })
    
    return {
        "browser_connected": browser_ws is not None,
        "total_tabs": len(browser_connections),
        "tabs": tabs_info,
        "stats": stats_from_source,  # 使用与请求趋势相同的数据源
        "model_stats": summary['model_stats'],
        "active_requests": summary['active_requests_list'],
        "mode": {
            "mode": CONFIG.get("id_updater_last_mode", "direct_chat"),
            "target": CONFIG.get("id_updater_battle_target", "A")
        },
        "total_models": len(MODEL_ENDPOINT_MAP)
    }


async def get_tokenizer_info_api(get_token_counter_info_func):
    """获取tokenizer信息"""
    try:
        info = get_token_counter_info_func()
        return info
    except Exception as e:
        logger.error(f"获取tokenizer信息失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def get_all_tokenizers_status_api(get_all_tokenizers_status_func):
    """获取所有分词器的详细状态"""
    try:
        status = get_all_tokenizers_status_func()
        return status
    except Exception as e:
        logger.error(f"获取分词器状态失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def calculate_tokens_api(
    request: Request,
    calculate_tokens_for_text_func
):
    """计算文本的token数量"""
    try:
        data = await request.json()
        text = data.get("text", "")
        tokenizers = data.get("tokenizers", None)  # 可选：指定使用哪些分词器
        
        if not text:
            raise HTTPException(status_code=400, detail="缺少text参数")
        
        result = calculate_tokens_for_text_func(text, tokenizers)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"计算token失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def compare_tokenizers_api(
    request: Request,
    compare_tokenizers_func
):
    """对比两种分词器的结果"""
    try:
        data = await request.json()
        text = data.get("text", "")
        tokenizer1 = data.get("tokenizer1", "tiktoken_cl100k")
        tokenizer2 = data.get("tokenizer2", "estimate")
        
        if not text:
            raise HTTPException(status_code=400, detail="缺少text参数")
        
        result = compare_tokenizers_func(text, tokenizer1, tokenizer2)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"对比分词器失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def install_tokenizer_api(
    request: Request,
    install_tokenizer_package_func,
    get_all_tokenizers_status_func
):
    """安装分词器包"""
    try:
        data = await request.json()
        package_name = data.get("package", "")
        
        if not package_name:
            raise HTTPException(status_code=400, detail="缺少package参数")
        
        # 执行安装（subprocess.run 最长 120s，必须走线程池）
        result = await asyncio.to_thread(install_tokenizer_package_func, package_name)
        
        if result['success']:
            # 安装成功后获取最新状态
            new_status = await asyncio.to_thread(get_all_tokenizers_status_func)
            result['tokenizers_status'] = new_status
            logger.info(f"✅ 分词器 {package_name} 安装成功")
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"安装分词器失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def get_tokenizer_mappings(_parse_jsonc_func):
    """获取所有tokenizer映射配置"""
    try:
        content = await read_text_file(CONFIG_FILE)
        config = _parse_jsonc_func(content)

        # 获取tokenizer_config，如果不存在则返回空字典
        return config.get('tokenizer_config', {})
    except Exception as e:
        logger.error(f"获取tokenizer映射失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def update_all_tokenizer_mappings(
    request: Request,
    _parse_jsonc_func,
    load_config_func
):
    """批量更新所有模型的tokenizer配置"""
    try:
        data = await request.json()
        tokenizer_config = data.get("tokenizer_config")
        
        if not isinstance(tokenizer_config, dict):
            raise HTTPException(status_code=400, detail="缺少有效的tokenizer_config参数")

        # 读取当前配置
        content = await read_text_file(CONFIG_FILE)

        # 🔧 修复：旧版把 JSONC 解析成 dict 后用 json.dump 覆盖回去，
        # config.jsonc 里全部说明性注释被一次保存彻底抹掉（这个文件的注释
        # 就是本项目的配置文档）。改为只在原始文本上替换 tokenizer_config
        # 这一个键的值，其余字节（含注释、缩进、键序）原样保留。
        updated = set_jsonc_value(content, 'tokenizer_config', tokenizer_config)

        # 写回前先校验替换结果仍是合法 JSONC，避免写坏运行中的配置
        try:
            _parse_jsonc_func(updated)
        except json.JSONDecodeError as e:
            logger.error(f"生成的 config.jsonc 非法，已放弃写入: {e}")
            raise HTTPException(status_code=500, detail=f"配置写入前校验失败: {e}")

        await write_text_file(CONFIG_FILE, updated)

        # 重新加载配置
        await asyncio.to_thread(load_config_func, True)

        logger.info(f"✅ 已批量保存 {len(tokenizer_config)} 个模型的tokenizer配置")
        for model, tokenizer in tokenizer_config.items():
            logger.info(f"  - {model}: {tokenizer}")
        
        return {
            "status": "success",
            "message": f"已保存 {len(tokenizer_config)} 个模型的分词器配置",
            "count": len(tokenizer_config)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新tokenizer配置失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def merge_model_stats(
    request: Request,
    stats_db
):
    """合并多个模型的统计数据"""
    try:
        data = await request.json()
        source_models = data.get("source_models", [])
        target_model = data.get("target_model", "")
        
        if not source_models or len(source_models) < 2:
            raise HTTPException(status_code=400, detail="至少需要选择2个模型进行合并")
        
        if not target_model:
            raise HTTPException(status_code=400, detail="缺少目标模型名称")
        
        # 调用数据库合并函数
        if stats_db.enabled:
            result = await asyncio.to_thread(stats_db.merge_models, source_models, target_model)
            if result:
                logger.info(f"✅ 成功合并 {len(source_models)} 个模型到 '{target_model}'")
                return {
                    "status": "success",
                    "message": f"已合并 {len(source_models)} 个模型",
                    "merged_count": result.get("merged_count", len(source_models)),
                    "target_model": target_model
                }
            else:
                raise HTTPException(status_code=500, detail="数据库合并操作失败")
        else:
            raise HTTPException(status_code=503, detail="SQLite数据库未启用，无法合并统计数据")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"合并模型统计失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def delete_model_stats(
    request: Request,
    stats_db
):
    """删除指定模型的统计数据"""
    try:
        data = await request.json()
        models = data.get("models", [])
        
        if not models:
            raise HTTPException(status_code=400, detail="未指定要删除的模型")
        
        # 调用数据库删除函数
        if stats_db.enabled:
            result = await asyncio.to_thread(stats_db.delete_models, models)
            if result:
                logger.info(f"✅ 成功删除 {len(models)} 个模型的统计数据")
                return {
                    "status": "success",
                    "message": f"已删除 {len(models)} 个模型的统计数据",
                    "deleted_count": result.get("deleted_count", len(models)),
                    "models": models
                }
            else:
                raise HTTPException(status_code=500, detail="数据库删除操作失败")
        else:
            raise HTTPException(status_code=503, detail="SQLite数据库未启用，无法删除统计数据")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除模型统计失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def get_request_stats(
    start_time: str,
    end_time: str,
    stats_db,
    monitoring_service,
    MonitorConfig
):
    """获取请求次数统计，支持日期范围过滤"""
    cache_key = _build_admin_cache_key(start_time, end_time, stats_db.enabled)
    cached_response = await _get_admin_cached_response("request_stats", cache_key)
    if cached_response is not None:
        logger.debug("[REQUEST_STATS] 命中短时缓存")
        return cached_response

    try:
        # 优先使用SQLite数据库
        if stats_db.enabled:
            db_stats = await stats_db.get_request_stats_async(start_time, end_time)
            if db_stats:
                logger.info(f"[REQUEST_STATS] ✅ 从SQLite读取统计数据")
                await _set_admin_cached_response("request_stats", cache_key, db_stats)
                return db_stats
            else:
                logger.warning(f"[REQUEST_STATS] SQLite查询失败，回退到JSON日志")
        
        # 回退：使用JSON日志（原有逻辑）
        logger.info(f"[REQUEST_STATS] 从JSON日志读取统计数据")
        
        # 从stats.json读取总体统计
        stats_path = MonitorConfig.LOG_DIR / MonitorConfig.STATS_FILE
        total_requests = 0
        success_requests = 0
        failed_requests = 0
        
        if stats_path.exists() and not (start_time or end_time):
            # 如果没有日期过滤，直接使用stats.json的数据
            with open(stats_path, 'r', encoding='utf-8') as f:
                stats_data = json.load(f)
            
            total_requests = stats_data.get('total_requests_all_time', 0)
            success_requests = stats_data.get('total_success_all_time', 0)
            failed_requests = stats_data.get('total_failed_all_time', 0)
            
            logger.info(f"[REQUEST_STATS] 从stats.json读取: 总数={total_requests}, 成功={success_requests}, 失败={failed_requests}")
        
        # 按日期聚合请求统计（用于趋势图）
        # 🔧 性能修复：read_recent_logs 内部有同步文件 I/O，用 asyncio.to_thread 避免阻塞事件循环
        recent_logs = await asyncio.to_thread(monitoring_service.log_manager.read_recent_logs, "requests", 10000)
        logger.info(f"[REQUEST_STATS] 读取到 {len(recent_logs)} 条请求日志用于趋势分析")
        
        # 日期过滤
        if start_time or end_time:
            filtered_logs = []
            for log_entry in recent_logs:
                timestamp = log_entry.get('timestamp', 0)
                if timestamp:
                    log_date = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d')
                    
                    if start_time and log_date < start_time:
                        continue
                    if end_time and log_date > end_time:
                        continue
                    
                    filtered_logs.append(log_entry)
            
            recent_logs = filtered_logs
            logger.info(f"[REQUEST_STATS] 日期过滤后剩余 {len(recent_logs)} 条记录")
            
            # 重新计算过滤后的总数
            total_requests = len(recent_logs)
            success_requests = sum(1 for log in recent_logs if log.get('success', True))
            failed_requests = total_requests - success_requests
        
        # 按日期聚合
        daily_request_stats = {}
        for log_entry in recent_logs:
            timestamp = log_entry.get('timestamp', 0)
            if not timestamp:
                continue
            
            success = log_entry.get('success', True)
            date_str = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d')
            
            if date_str not in daily_request_stats:
                daily_request_stats[date_str] = {
                    'date': date_str,
                    'total': 0,
                    'success': 0,
                    'failed': 0
                }
            
            daily_request_stats[date_str]['total'] += 1
            if success:
                daily_request_stats[date_str]['success'] += 1
            else:
                daily_request_stats[date_str]['failed'] += 1
        
        # 转换为列表并按日期排序
        daily_stats_list = list(daily_request_stats.values())
        daily_stats_list.sort(key=lambda x: x['date'])
        
        result = {
            "daily_stats": daily_stats_list,
            "total_requests": total_requests,
            "success_requests": success_requests,
            "failed_requests": failed_requests
        }
        await _set_admin_cached_response("request_stats", cache_key, result)
        return result
    except Exception as e:
        logger.error(f"获取请求统计失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def get_token_stats(
    start_date: str,
    end_date: str,
    start_time: str,
    end_time: str,
    rpm_period: str,
    stats_db,
    monitoring_service,
    MODEL_ENDPOINT_MAP: dict,
    estimate_message_tokens_func,
    estimate_tokens_func
):
    """获取token用量统计，支持日期范围过滤
    
    Args:
        rpm_period: RPM/TPM 计算的时间周期，'day'=24小时, 'hour'=1小时。仅影响 RPM/TPM 计算，不影响其他统计。
    """
    # 兼容性：支持start_date/end_date（前端）和start_time/end_time（SQLite）
    filter_start = start_time or start_date
    filter_end = end_time or end_date

    cache_key = _build_admin_cache_key(filter_start, filter_end, rpm_period, stats_db.enabled)
    cached_response = await _get_admin_cached_response("token_stats", cache_key)
    if cached_response is not None:
        logger.debug("[TOKEN_STATS] 命中短时缓存")
        return cached_response

    # 优先使用 SQLite 数据库
    # 🔧 修复：旧版只在 SQLite 命中时 return，未启用 / 查询为空 / 抛异常时
    # 分别落到函数末尾隐式返回 None（前端拿到 null 后 data.total_tokens 直接
    # TypeError，整个统计页白屏）；且 except 分支 return 之后还挂着永不执行的
    # logger.error + raise。现在所有分支都必然返回结构完整的响应。
    if stats_db.enabled:
        try:
            db_stats = await stats_db.get_token_stats_async(
                filter_start, filter_end, MODEL_ENDPOINT_MAP, rpm_period)
            if db_stats:
                logger.info("[TOKEN_STATS] ✅ 从SQLite读取统计数据")
                await _set_admin_cached_response("token_stats", cache_key, db_stats)
                return db_stats
            logger.warning("[TOKEN_STATS] SQLite 未返回数据，回退到内存统计概览")
        except Exception as e:
            logger.error(f"[TOKEN_STATS] SQLite 查询异常，回退到内存统计概览: {e}", exc_info=True)

    # 回退：只返回内存中的模型统计概览（无详细 token 聚合）
    result = await _build_memory_token_stats(monitoring_service, rpm_period)
    await _set_admin_cached_response("token_stats", cache_key, result)
    return result


async def _build_memory_token_stats(monitoring_service, rpm_period: Optional[str]) -> dict:
    """SQLite 不可用时的降级统计：仅有请求数，token/成本聚合置零。

    字段与 StatsDB.get_token_stats 的返回保持一致，保证前端渲染路径统一。
    """
    model_stats_list = await monitoring_service.get_model_stats_async()
    stats_list = [
        {
            'model': s['model'],
            'display_name': s['model'],
            'request_count': s['total_requests'],
            'input_tokens': 0,
            'output_tokens': 0,
            'cached_tokens': 0,
            'cached_cost': 0,
            'total_tokens': 0,
            'input_cost': 0,
            'output_cost': 0,
            'total_cost': 0,
            'currency': 'USD',
            'rpm': 0,
            'tpm': 0,
        }
        for s in model_stats_list
    ]
    stats_list.sort(key=lambda x: x['request_count'], reverse=True)

    usd_to_cny, cny_to_usd = get_exchange_rates()
    zero_cost = {'input_cost': 0, 'output_cost': 0, 'total_cost': 0}
    return {
        "model_stats": stats_list,
        "daily_stats": [],
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cached_tokens": 0,
        "total_tokens": 0,
        "input_cost": 0,
        "output_cost": 0,
        "total_cost": 0,
        "currency": "USD",
        "cost_usd": dict(zero_cost),
        "cost_cny": dict(zero_cost),
        "cost_by_currency": {},
        "exchange_rate": {"USD_TO_CNY": usd_to_cny, "CNY_TO_USD": cny_to_usd},
        "rate_stats": {
            "period": "hour" if rpm_period == "hour" else "day",
            "minutes": 60.0 if rpm_period == "hour" else 1440.0,
            "request_count": 0,
            "total_tokens": 0,
        },
        "models_count": len(stats_list),
        "degraded": True,
    }



async def export_report(
    stats_db,
    monitoring_service,
    MODEL_ENDPOINT_MAP: dict,
    start_date: str = None,
    end_date: str = None
):
    """导出Token使用/成本报告为CSV文件"""
    import csv
    import io

    try:
        # 优先使用SQLite
        if stats_db.enabled:
            db_stats = await stats_db.get_token_stats_async(start_date, end_date, MODEL_ENDPOINT_MAP, 'day')
            if db_stats and db_stats.get('model_stats'):
                model_stats = db_stats['model_stats']
                output = io.StringIO()
                writer = csv.writer(output)
                # 写入表头
                writer.writerow([
                    '模型', '请求数', '输入Tokens', '输出Tokens', '缓存命中Tokens',
                    '总Tokens', '输入成本(USD)', '缓存成本(USD)', '输出成本(USD)', '总成本(USD)',
                    '货币', '平均Token/请求'
                ])
                for stat in model_stats:
                    writer.writerow([
                        stat.get('display_name', stat.get('model', '')),
                        stat.get('request_count', 0),
                        stat.get('input_tokens', 0),
                        stat.get('output_tokens', 0),
                        stat.get('cached_tokens', 0),
                        stat.get('total_tokens', 0),
                        round(stat.get('input_cost', 0), 6),
                        round(stat.get('cached_cost', 0), 6),
                        round(stat.get('output_cost', 0), 6),
                        round(stat.get('total_cost', 0), 6),
                        stat.get('currency', 'USD'),
                        stat.get('request_count', 0) > 0 and round(stat.get('total_tokens', 0) / stat.get('request_count', 0)) or 0
                    ])

                csv_content = output.getvalue()
                output.close()
                return Response(
                    content=csv_content,
                    media_type="text/csv; charset=utf-8-sig",
                    headers={"Content-Disposition": "attachment; filename=token_report.csv"}
                )

        # 回退：从内存中的模型统计导出
        # 🔧 性能修复：用 asyncio.to_thread 包装，避免 threading.Lock 阻塞事件循环
        model_stats = await asyncio.to_thread(monitoring_service.get_model_stats)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            '模型', '总请求数', '成功请求数', '失败请求数', '平均耗时(ms)'
        ])
        for stat in model_stats:
            writer.writerow([
                stat.get('model', ''),
                stat.get('total_requests', 0),
                stat.get('success_requests', 0),
                stat.get('failed_requests', 0),
                round(stat.get('avg_duration', 0) * 1000, 2)
            ])
        csv_content = output.getvalue()
        output.close()
        return Response(
            content=csv_content,
            media_type="text/csv; charset=utf-8-sig",
            headers={"Content-Disposition": "attachment; filename=token_report.csv"}
        )
    except Exception as e:
        logger.error(f"导出报告失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def add_custom_tokenizer_api(
    request: Request,
    add_custom_tokenizer_func
):
    """添加自定义分词器"""
    try:
        data = await request.json()
        name = data.get("name", "")
        source_type = data.get("source_type", "huggingface")
        source = data.get("source", "")
        display_name = data.get("display_name", None)
        description = data.get("description", None)
        supported_models = data.get("supported_models", [])
        
        if not name or not source:
            raise HTTPException(status_code=400, detail="缺少必要参数: name, source")
        
        result = await asyncio.to_thread(
            add_custom_tokenizer_func,
            name=name,
            source_type=source_type,
            source=source,
            display_name=display_name,
            description=description,
            supported_models=supported_models
        )
        
        if result['success']:
            logger.info(f"✅ 自定义分词器 {name} 添加成功")
        else:
            logger.warning(f"❌ 自定义分词器 {name} 添加失败: {result.get('error')}")
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"添加自定义分词器失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def delete_custom_tokenizer_api(
    name: str,
    delete_custom_tokenizer_func
):
    """删除自定义分词器"""
    try:
        if not name:
            raise HTTPException(status_code=400, detail="缺少分词器名称")
        
        result = await asyncio.to_thread(delete_custom_tokenizer_func, name)
        
        if result['success']:
            logger.info(f"✅ 自定义分词器 {name} 删除成功")
        else:
            logger.warning(f"❌ 自定义分词器 {name} 删除失败: {result.get('error')}")
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除自定义分词器失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def list_custom_tokenizers_api(list_custom_tokenizers_func):
    """列出所有自定义分词器"""
    try:
        result = await asyncio.to_thread(list_custom_tokenizers_func)
        return result
    except Exception as e:
        logger.error(f"列出自定义分词器失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))



async def test_model_keys(
    request: Request,
    direct_api_service,
):
    """测试单个模型配置中的所有 API Key 是否有效（并行发送请求）。

    前端从编辑对话框中读取当前配置，POST 到此接口。
    对每个 key 分别发送一次测试请求。
    """
    data = await request.json()

    api_keys = data.get("api_keys", [])
    api_base_url = data.get("api_base_url", "").strip()
    model_id = data.get("model_id", "unknown")
    api_type = data.get("api_type", "direct_api")
    default_endpoint = "/responses" if api_type == "responses_native" else (
        "/messages" if api_type == "anthropic_native" else "/chat/completions"
    )
    endpoint_path = data.get("endpoint_path") or default_endpoint
    if endpoint_path and not endpoint_path.startswith("/"):
        endpoint_path = "/" + endpoint_path

    is_gemini = api_type == "gemini_native"
    is_responses = api_type == "responses_native"

    if not api_keys:
        return {"status": "info", "message": "没有配置任何 API Key", "results": []}

    # 复用全局共享连接池，与真实请求使用相同的 TLS 校验和连接配置。
    shared_session = _app_state.server.aiohttp_session

    async def _test_one_key(key: str, index: int):
        """测试单个 key。"""
        result = {
            "index": index,
            "key_preview": key[:6] + "..." + key[-4:] if len(key) > 10 else "***",
            "status": "unknown",
            "error": None,
            "response_time_ms": None,
        }

        try:
            started_at = time.time()

            if is_gemini:
                generator = direct_api_service.call_gemini_native_api(
                    api_key=key,
                    model=model_id,
                    messages=[{"role": "user", "content": "Hi"}],
                    stream=False,
                    base_url=api_base_url if api_base_url else None,
                    thinking_config={"includeThoughts": False},
                )
                response = None
                async for chunk in generator:
                    response = chunk
                    break

                result["response_time_ms"] = round((time.time() - started_at) * 1000, 1)
                if response and "error" not in response:
                    result["status"] = "ok"
                else:
                    error = response.get("error", {}) if response else "无响应"
                    if isinstance(error, dict):
                        error = str(error.get("message", str(error)))
                    result["status"] = "error"
                    result["error"] = str(error)[:200]
            else:
                if not api_base_url:
                    result["status"] = "error"
                    result["error"] = "缺少 api_base_url，无法测试"
                    return result
                if shared_session is None:
                    result["status"] = "error"
                    result["error"] = "HTTP 连接池尚未初始化，请稍后重试"
                    return result

                url = f"{api_base_url.rstrip('/')}{endpoint_path}"
                body = (
                    {
                        "model": model_id,
                        "input": "Hi",
                        "max_output_tokens": 5,
                        "store": False,
                    }
                    if is_responses
                    else {
                        "model": model_id,
                        "messages": [{"role": "user", "content": "Hi"}],
                        "max_tokens": 5,
                    }
                )
                headers = {"Content-Type": "application/json"}
                if key:
                    headers["Authorization"] = f"Bearer {key}"

                async with shared_session.post(
                    url,
                    json=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30, connect=10),
                ) as response:
                    result["response_time_ms"] = round((time.time() - started_at) * 1000, 1)
                    if response.status == 200:
                        result["status"] = "ok"
                    else:
                        try:
                            error_data = await response.json()
                            error_obj = error_data.get("error")
                            if isinstance(error_obj, dict):
                                error_message = str(error_obj.get("message", response.status))
                            else:
                                error_message = str(
                                    error_obj or error_data.get("detail") or response.status
                                )
                        except Exception:
                            error_message = f"HTTP {response.status}"
                        result["status"] = "error"
                        result["error"] = error_message[:200]
        except asyncio.TimeoutError:
            result["status"] = "timeout"
            result["error"] = "请求超时（30s）"
        except Exception as exc:
            result["status"] = "error"
            result["error"] = str(exc)[:200]

        return result

    raw_results = await asyncio.gather(
        *(_test_one_key(key, index) for index, key in enumerate(api_keys)),
        return_exceptions=True,
    )

    results = []
    for index, item in enumerate(raw_results):
        if isinstance(item, Exception):
            results.append({
                "index": index,
                "key_preview": "???",
                "status": "error",
                "error": str(item)[:200],
                "response_time_ms": None,
            })
        else:
            results.append(item)

    ok_count = sum(1 for item in results if item["status"] == "ok")
    error_count = sum(1 for item in results if item["status"] == "error")
    timeout_count = sum(1 for item in results if item["status"] == "timeout")

    return {
        "status": "done",
        "total": len(results),
        "ok": ok_count,
        "error": error_count,
        "timeout": timeout_count,
        "results": results,
        "message": f"测试完成: {ok_count}/{len(results)} 个 Key 有效",
    }


# ============================================================
# API Key 余额查询（DeepSeek 等支持 /user/balance 的 API）
# ============================================================

async def query_key_balance(request: Request):
    """查询 API Key 的余额（目前仅支持 api_base_url 含 deepseek 的配置）

    前端 POST 当前模型的所有 key + api_base_url，后端并行查询余额。
    """
    data = await request.json()
    api_keys = data.get("api_keys", [])
    api_base_url = data.get("api_base_url", "").strip()

    if not api_keys:
        return {"status": "info", "message": "没有配置任何 API Key", "results": []}

    if not api_base_url:
        return {"status": "error", "message": "缺少 api_base_url，无法查询余额。请先在模型配置中填写 API Base URL。", "results": []}

    # 仅 deepseek 系 API 支持余额查询
    base_lower = api_base_url.lower()
    if "deepseek" not in base_lower:
        return {
            "status": "unsupported",
            "message": "当前仅 DeepSeek API 支持余额查询。其他 API 暂不支持 /user/balance 接口。",
            "results": []
        }

    shared_session = _app_state.server.aiohttp_session
    if shared_session is None:
        return {"status": "error", "message": "HTTP 连接池尚未初始化，请稍后重试", "results": []}

    # 🔧 余额接口在 API 根路径下（/user/balance），而非模型端点路径下。
    # 用户可能配置了 /beta、/v1 等子路径，需提取 origin 后拼接。
    from urllib.parse import urlparse
    parsed = urlparse(api_base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    balance_url = f"{origin}/user/balance"

    async def _query_one(key: str, index: int):
        preview = key[:6] + "..." + key[-4:] if len(key) > 10 else "***"
        result = {
            "index": index,
            "key_preview": preview,
            "status": "unknown",
            "balance": None,
            "error": None,
        }

        try:
            headers = {"Authorization": f"Bearer {key}"}
            async with shared_session.get(
                balance_url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10, connect=5)
            ) as resp:
                body = await resp.json()

                if resp.status == 200:
                    is_avail = body.get("is_available", False)
                    infos = body.get("balance_infos", [])
                    result["status"] = "ok"
                    result["balance"] = {
                        "is_available": is_avail,
                        "infos": [
                            {
                                "currency": info.get("currency", "?"),
                                "total": info.get("total_balance", "0"),
                                "granted": info.get("granted_balance", "0"),
                                "topped_up": info.get("topped_up_balance", "0"),
                            }
                            for info in infos
                        ] if infos else []
                    }
                else:
                    err_obj = body.get("error", {})
                    if isinstance(err_obj, dict):
                        em = str(err_obj.get("message", resp.status))
                    else:
                        em = str(err_obj or body.get("detail") or f"HTTP {resp.status}")
                    result["status"] = "error"
                    result["error"] = em[:200]

        except asyncio.TimeoutError:
            result["status"] = "timeout"
            result["error"] = "请求超时（10s）"
        except aiohttp.ClientError as e:
            result["status"] = "error"
            result["error"] = f"连接失败: {str(e)[:150]}"
        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)[:200]

        return result

    tasks = [_query_one(k, i) for i, k in enumerate(api_keys)]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    for i, r in enumerate(raw_results):
        if isinstance(r, Exception):
            results.append({
                "index": i, "key_preview": "???", "status": "error",
                "error": str(r)[:200], "balance": None
            })
        else:
            results.append(r)

    ok = sum(1 for r in results if r["status"] == "ok")
    has_balance = sum(1 for r in results if r.get("balance"))

    return {
        "status": "done",
        "total": len(results),
        "ok": ok,
        "has_balance": has_balance,
        "results": results,
        "message": f"查询完成: {ok}/{len(results)} 个 Key 有余额信息"
    }


# ============================================================================
# 端点注册（依赖从模块单例 / AppState 自取）
# ============================================================================

@router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard_endpoint():
    return await admin_dashboard()


@router.get("/token_calculator", response_class=HTMLResponse)
async def token_calculator_endpoint():
    """返回Token计算器页面"""
    try:
        with open('token_calculator.html', 'r', encoding='utf-8') as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>Token计算器页面未找到</h1><p>请确保 token_calculator.html 文件在正确的位置。</p>",
            status_code=404
        )


@router.get("/api/admin/models")
async def get_models_config_endpoint():
    return await get_models_config(
        MODEL_ENDPOINT_MAP, MODEL_NAME_TO_ID_MAP, load_model_endpoint_map
    )


@router.post("/api/admin/models")
async def update_model_config_endpoint(request: Request):
    return await update_model_config(request, load_model_endpoint_map)


@router.post("/api/admin/models/delete")
async def delete_model_config_endpoint(request: Request):
    return await delete_model_config(request, load_model_endpoint_map)


@router.post("/api/admin/models/reorder")
async def reorder_models_endpoint(request: Request):
    return await reorder_models(request, load_model_endpoint_map)


@router.post("/api/admin/models/archive")
async def set_models_archive_endpoint(request: Request):
    return await set_models_archive(request, load_model_endpoint_map)


@router.post("/api/admin/models/auto_archive")
async def trigger_auto_archive_endpoint(request: Request):
    return await trigger_auto_archive(request)


@router.get("/api/admin/models/archive_config")
async def get_archive_config_endpoint():
    return await get_archive_config()


@router.post("/api/admin/models/archive_config")
async def update_archive_config_endpoint(request: Request):
    return await update_archive_config(request)


@router.get("/api/admin/config")
async def get_config_endpoint():
    return await get_config(CONFIG)


@router.post("/api/admin/config")
async def update_config_endpoint(request: Request):
    return await update_config(request, _parse_jsonc, load_config)


@router.get("/api/admin/overview")
async def get_overview_endpoint():
    conn = _app_state.connection
    return await get_overview(
        monitoring_service, stats_db, MonitorConfig, conn.browser_ws_ref['ws'],
        conn.browser_connections, conn.browser_connections_lock, conn.tab_connection_times,
        conn.tab_request_counts, CONFIG, MODEL_ENDPOINT_MAP
    )


@router.get("/api/admin/tokenizer_info")
async def get_tokenizer_info_endpoint():
    return await get_tokenizer_info_api(get_token_counter_info)


@router.get("/api/admin/tokenizer_mappings")
async def get_tokenizer_mappings_endpoint():
    return await get_tokenizer_mappings(_parse_jsonc)


@router.post("/api/admin/tokenizer_mappings")
async def update_tokenizer_mappings_endpoint(request: Request):
    return await update_all_tokenizer_mappings(request, _parse_jsonc, load_config)


@router.get("/api/admin/tokenizers_status")
async def get_all_tokenizers_status_endpoint():
    """获取所有分词器的详细状态"""
    return await get_all_tokenizers_status_api(get_all_tokenizers_status)


@router.post("/api/admin/calculate_tokens")
async def calculate_tokens_endpoint(request: Request):
    """计算文本的token数量"""
    return await calculate_tokens_api(request, calculate_tokens_for_text)


@router.post("/api/admin/compare_tokenizers")
async def compare_tokenizers_endpoint(request: Request):
    """对比两种分词器的结果"""
    return await compare_tokenizers_api(request, compare_tokenizers)


@router.post("/api/admin/install_tokenizer")
async def install_tokenizer_endpoint(request: Request):
    """安装分词器包"""
    return await install_tokenizer_api(
        request, install_tokenizer_package, get_all_tokenizers_status
    )


@router.post("/api/admin/custom_tokenizers")
async def add_custom_tokenizer_endpoint(request: Request):
    """添加自定义分词器"""
    return await add_custom_tokenizer_api(request, add_custom_tokenizer)


@router.delete("/api/admin/custom_tokenizers/{name}")
async def delete_custom_tokenizer_endpoint(name: str):
    """删除自定义分词器"""
    return await delete_custom_tokenizer_api(name, delete_custom_tokenizer)


@router.get("/api/admin/custom_tokenizers")
async def list_custom_tokenizers_endpoint():
    """列出所有自定义分词器"""
    return await list_custom_tokenizers_api(list_custom_tokenizers)

@router.post("/api/admin/test_model_keys")
async def test_model_keys_endpoint(request: Request):
    """测试单个模型配置中的所有 API Key（并行请求）。"""
    return await test_model_keys(request, _app_state.server.direct_api_service)


@router.post("/api/admin/query_key_balance")
async def query_key_balance_endpoint(request: Request):
    """查询 API Key 的余额（目前仅 DeepSeek）"""
    return await query_key_balance(request)


@router.post("/api/admin/set_sticky_key")
async def set_sticky_key_endpoint(request: Request):
    """将指定 key 设为 sticky 轮询的当前 key（查余额后自动粘性到余额最多的 key）"""
    data = await request.json()
    model_name = (data.get("model_name") or "").strip()
    api_key = (data.get("api_key") or "").strip()

    if not model_name:
        return {"status": "error", "message": "缺少 model_name，无法设置粘性 Key"}
    if not api_key:
        return {"status": "error", "message": "缺少 api_key，无法设置粘性 Key"}

    ok = await set_sticky_current_key(model_name, api_key)
    if ok:
        preview = api_key[:6] + "..." + api_key[-4:] if len(api_key) > 10 else "***"
        return {"status": "done", "message": f"已粘性到 Key: {preview}"}
    return {"status": "error", "message": "设置粘性 Key 失败"}


@router.get("/api/admin/token_stats")
async def get_token_stats_endpoint(start_date: Optional[str] = None, end_date: Optional[str] = None,
                                   start_time: Optional[str] = None, end_time: Optional[str] = None,
                                   rpm_period: Optional[str] = None):
    return await get_token_stats(
        start_date, end_date, start_time, end_time, rpm_period, stats_db,
        monitoring_service, MODEL_ENDPOINT_MAP, estimate_message_tokens, estimate_tokens
    )


@router.get("/api/admin/export_report")
async def export_report_endpoint(start_date: Optional[str] = None, end_date: Optional[str] = None):
    return await export_report(
        stats_db, monitoring_service, MODEL_ENDPOINT_MAP, start_date, end_date
    )


@router.get("/api/admin/request_stats")
async def get_request_stats_endpoint(start_time: Optional[str] = None, end_time: Optional[str] = None,
                                     start_date: Optional[str] = None, end_date: Optional[str] = None):
    """请求次数统计。

    🔧 修复：管理面板的日期选择器发送的是 start_date/end_date，而旧版端点
    只声明了 start_time/end_time，FastAPI 直接把它们丢弃 —— "请求统计"的
    日期筛选点了完全没反应。这里与 /api/admin/token_stats 端点对齐，
    两组参数名都接受（date 为兼容前端语义的日期粒度，time 为 ISO 时间戳）。
    """
    return await get_request_stats(
        start_time or start_date, end_time or end_date,
        stats_db, monitoring_service, MonitorConfig
    )


@router.post("/api/admin/merge_model_stats")
async def merge_model_stats_endpoint(request: Request):
    return await merge_model_stats(request, stats_db)


@router.post("/api/admin/delete_model_stats")
async def delete_model_stats_endpoint(request: Request):
    return await delete_model_stats(request, stats_db)


async def warmup_admin_cache():
    """预热 admin 首屏缓存，消除重启后的冷启动延迟（由 lifespan 后台任务调用）"""
    await asyncio.sleep(0.5)
    try:
        logger.info("🔥 预热 admin 首屏缓存...")
        # 预热 overview（含 SQLite 汇总查询）
        await get_overview_endpoint()
        # 预热 token_stats（最重的查询：多个 GROUP BY + 成本计算）
        await get_token_stats_endpoint(rpm_period='day')
        # 预热 request_stats
        await get_request_stats_endpoint()
        logger.info("🔥 admin 首屏缓存预热完成")
    except Exception as e:
        logger.warning(f"⚠️ admin 缓存预热失败（不影响使用）: {e}")