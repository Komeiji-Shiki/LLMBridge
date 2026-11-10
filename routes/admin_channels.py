"""
渠道管理接口：/api/admin/channels*

渠道表存在 model_channels.json，模型配置里用 channel 字段引用（见 core/model_channels.py）。
两个文件的读-改-写共用 admin_routes 的同一把锁，改完立即重新加载。
"""
import asyncio
import logging
import os
import shutil
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from core.config_loader import load_model_endpoint_map
from core.model_channels import (
    CHANNELS_FILE, adopt_suggestions, assign_channel, channel_usage,
    new_channel_id, normalize_channel, remove_channel, suggest_channels,
)
from routes import admin_routes
from routes.admin_routes import MODEL_ENDPOINT_MAP_FILE, _read_admin_json

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin"])

BACKUP_DIR = '.migration_backups'


async def _read_both():
    model_map = await admin_routes.read_json_file(MODEL_ENDPOINT_MAP_FILE)
    return model_map, await admin_routes.read_channels_file()


async def _write_both(model_map, channels, channels_first: bool):
    """两个文件按“被引用的一方先存在、后消失”的顺序写。

    新增引用时先写渠道，解除引用时先写模型；中途被热重载读到，模型引用的渠道也一定存在。
    """
    writes = [(CHANNELS_FILE, channels), (MODEL_ENDPOINT_MAP_FILE, model_map)]
    for path, data in writes if channels_first else reversed(writes):
        if data is not None:
            await admin_routes.write_json_file(path, data)
    await asyncio.to_thread(load_model_endpoint_map, True)
    await admin_routes._invalidate_admin_stats_cache()


def _backup_sync() -> str:
    target = os.path.join(BACKUP_DIR, datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    os.makedirs(target, exist_ok=True)
    for path in (MODEL_ENDPOINT_MAP_FILE, CHANNELS_FILE):
        if os.path.exists(path):
            shutil.copy2(path, target)
    return target


def _suggestion_summary(suggestion: dict) -> dict:
    channel = suggestion['channel']
    return {
        'channel_id': suggestion['channel_id'],
        'name': channel['name'],
        'api_base_url': channel.get('api_base_url', ''),
        'key_count': len(channel.get('api_keys', [])),
        'models': suggestion['models'],
    }


async def list_channels():
    """渠道表，以及每个渠道被哪些模型引用"""
    model_map, channels = await _read_both()
    return {"channels": channels, "usage": channel_usage(model_map)}


async def save_channel(request: Request):
    """新建或修改渠道。body: {"id": 可选, "channel": {...}}，不带 id 时新建"""
    data = await _read_admin_json(request)
    channel_id = data.get("id")
    try:
        channel = normalize_channel(data.get("channel"))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    async with admin_routes._MODEL_ENDPOINT_MAP_LOCK:
        channels = await admin_routes.read_channels_file()
        if channel_id and channel_id not in channels:
            raise HTTPException(status_code=404, detail=f"渠道 {channel_id} 不存在")
        if any(other_id != channel_id and other.get("name") == channel["name"] for other_id, other in channels.items()):
            raise HTTPException(status_code=409, detail=f"已经有一个叫 {channel['name']} 的渠道")
        channel_id = channel_id or new_channel_id(channels)
        channels[channel_id] = channel
        await _write_both(None, channels, channels_first=True)

    logger.info(f"✅ 渠道已保存: {channel['name']} ({channel_id})")
    return {"status": "success", "id": channel_id, "message": f"渠道 {channel['name']} 已保存"}


async def delete_channel(request: Request):
    """删除渠道。还在用它的模型各自留一份地址和 Key"""
    data = await _read_admin_json(request)
    channel_id = data.get("id")

    async with admin_routes._MODEL_ENDPOINT_MAP_LOCK:
        model_map, channels = await _read_both()
        if channel_id not in channels:
            raise HTTPException(status_code=404, detail=f"渠道 {channel_id} 不存在")
        name = channels[channel_id].get("name", channel_id)
        new_map, new_channels, detached = remove_channel(model_map, channels, channel_id)
        await _write_both(new_map if detached else None, new_channels, channels_first=False)

    logger.info(f"✅ 渠道已删除: {name}，{len(detached)} 个模型改为自带地址和 Key")
    return {"status": "success", "detached": detached, "message": f"渠道 {name} 已删除"}


async def set_models_channel(request: Request):
    """让一批模型改用某个渠道。body: {"model_names": [...], "channel": 渠道 id 或 null（脱离渠道）}"""
    data = await _read_admin_json(request)
    model_names = data.get("model_names")
    channel_id = data.get("channel") or None
    if not isinstance(model_names, list) or not model_names or not all(isinstance(name, str) for name in model_names):
        raise HTTPException(status_code=400, detail="缺少有效的 model_names 列表")

    async with admin_routes._MODEL_ENDPOINT_MAP_LOCK:
        model_map, channels = await _read_both()
        if channel_id and channel_id not in channels:
            raise HTTPException(status_code=404, detail=f"渠道 {channel_id} 不存在")
        new_map, changed = assign_channel(model_map, model_names, channel_id, channels)
        if changed:
            await _write_both(new_map, None, channels_first=True)

    action = f"改用渠道 {channels[channel_id]['name']}" if channel_id else "脱离渠道"
    logger.info(f"✅ 模型{action}: {', '.join(changed) or '(无变更)'}")
    return {"status": "success", "changed": changed, "message": f"{len(changed)} 个模型已{action}"}


async def get_channel_suggestions():
    """预览：现有模型里哪些连接配置相同、可以整理成渠道"""
    model_map, channels = await _read_both()
    return {"suggestions": [_suggestion_summary(item) for item in suggest_channels(model_map, channels)]}


async def adopt_channel_suggestions():
    """按预览结果建渠道并让模型引用。动手前把两个文件备份到 .migration_backups"""
    async with admin_routes._MODEL_ENDPOINT_MAP_LOCK:
        model_map, channels = await _read_both()
        new_map, new_channels, suggestions = adopt_suggestions(model_map, channels)
        if not suggestions:
            return {"status": "success", "suggestions": [], "message": "没有可以整理的模型"}
        backup = await asyncio.to_thread(_backup_sync)
        await _write_both(new_map, new_channels, channels_first=True)

    models = sum(len(item['models']) for item in suggestions)
    logger.info(f"✅ 已整理出 {len(suggestions)} 个渠道，涉及 {models} 个模型；整理前的配置备份在 {backup}")
    return {
        "status": "success",
        "suggestions": [_suggestion_summary(item) for item in suggestions],
        "backup": backup,
        "message": f"已整理出 {len(suggestions)} 个渠道，涉及 {models} 个模型",
    }


# ============================================================================
# 端点注册
# ============================================================================

@router.get("/api/admin/channels")
async def list_channels_endpoint():
    return await list_channels()


@router.post("/api/admin/channels")
async def save_channel_endpoint(request: Request):
    return await save_channel(request)


@router.post("/api/admin/channels/delete")
async def delete_channel_endpoint(request: Request):
    return await delete_channel(request)


@router.get("/api/admin/channels/suggest")
async def get_channel_suggestions_endpoint():
    return await get_channel_suggestions()


@router.post("/api/admin/channels/adopt")
async def adopt_channel_suggestions_endpoint():
    return await adopt_channel_suggestions()


@router.post("/api/admin/models/channel")
async def set_models_channel_endpoint(request: Request):
    return await set_models_channel(request)
