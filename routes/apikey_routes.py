"""
API Key 管理路由
/api/admin/api_keys 系列端点（由 WebAccessKeyMiddleware 保护）

🔧 性能修复：create/update/delete/reload 等操作在持锁状态下同步读写磁盘，
直接在事件循环调用会阻塞所有并发流式请求，统一移入线程池执行。
"""
import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from core.api_key_manager import api_key_manager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["api-keys"])


def _validate_key_payload(data: dict, *, require_name: bool = True) -> dict:
    """校验 API Key 请求体，返回清洗后的数据。

    Args:
        require_name: True 时 name 为必填（POST 创建），False 时 name 可选（PUT 更新）
    """
    cleaned = {}

    name = (data.get("name") or "").strip()
    if require_name and not name:
        raise HTTPException(status_code=400, detail="API Key 名称不能为空")
    if name:
        cleaned["name"] = name

    if "allowed_models" in data:
        allowed_models = data["allowed_models"]
        if not isinstance(allowed_models, list):
            raise HTTPException(status_code=400, detail="allowed_models 必须是一个列表")
        cleaned["allowed_models"] = allowed_models

    if "rpm_limit" in data:
        try:
            rpm_limit = int(data["rpm_limit"])
            if rpm_limit < 0:
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="rpm_limit 必须是非负整数")
        cleaned["rpm_limit"] = rpm_limit

    if "description" in data:
        cleaned["description"] = str(data["description"])

    if "enabled" in data:
        cleaned["enabled"] = bool(data["enabled"])

    return cleaned


@router.get("/api/admin/api_keys")
async def list_api_keys():
    """列出所有 API Key（需要管理员权限，由 WebAccessKeyMiddleware 保护）"""
    keys = await asyncio.to_thread(api_key_manager.list_keys)
    return {"keys": keys, "total": len(keys)}


@router.post("/api/admin/api_keys")
async def create_api_key(request: Request):
    """创建新的 API Key"""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无效的 JSON 请求体")

    # 验证并清洗 payload
    cleaned = _validate_key_payload(data, require_name=True)

    key_info = await asyncio.to_thread(
        api_key_manager.create_key,
        name=cleaned["name"],
        allowed_models=cleaned.get("allowed_models", []),
        rpm_limit=cleaned.get("rpm_limit", 0),
        enabled=cleaned.get("enabled", True),
        description=cleaned.get("description", ""),
    )

    return {"success": True, "key": key_info, "message": "API Key 创建成功。请保存好 secret，它只会显示一次！"}


@router.post("/api/admin/api_keys/reload")
async def reload_api_keys():
    """重新加载 API Key 配置"""
    await asyncio.to_thread(api_key_manager.reload)
    keys = await asyncio.to_thread(api_key_manager.list_keys)
    return {"success": True, "message": f"已重新加载 {len(keys)} 个 API Key", "total": len(keys)}


@router.get("/api/admin/api_keys/{key_id}")
async def get_api_key(key_id: str):
    """获取单个 API Key 的详细信息"""
    key_info = api_key_manager.get_key_info(key_id)
    if key_info is None:
        raise HTTPException(status_code=404, detail=f"API Key '{key_id}' 不存在")
    return {"key": key_info}


@router.put("/api/admin/api_keys/{key_id}")
async def update_api_key(key_id: str, request: Request):
    """更新 API Key 配置"""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无效的 JSON 请求体")

    # 验证并清洗 payload（更新时 name 非必填）
    cleaned = _validate_key_payload(data, require_name=False)

    result = await asyncio.to_thread(api_key_manager.update_key, key_id, cleaned)
    if result is None:
        raise HTTPException(status_code=404, detail=f"API Key '{key_id}' 不存在")

    return {"success": True, "key": result}


@router.delete("/api/admin/api_keys/{key_id}")
async def delete_api_key(key_id: str):
    """删除 API Key"""
    if await asyncio.to_thread(api_key_manager.delete_key, key_id):
        return {"success": True, "message": f"API Key '{key_id}' 已删除"}
    else:
        raise HTTPException(status_code=404, detail=f"API Key '{key_id}' 不存在")
