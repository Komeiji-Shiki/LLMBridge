"""
额度管理接口：/api/admin/quotas*

模型和渠道额度的设置随模型、渠道一起保存（quota 字段），这里只负责查看本期用量和重置。
"""
import asyncio

from fastapi import APIRouter, HTTPException, Request

from core.model_quota import model_quota
from routes.admin_routes import _read_admin_json

router = APIRouter(tags=["admin"])


@router.get("/api/admin/quotas")
async def quota_overview():
    """所有设了额度的模型和渠道：{"models": {名称: 用量}, "channels": {渠道 id: 用量}}"""
    return await asyncio.to_thread(model_quota.overview)


@router.post("/api/admin/quotas/reset")
async def reset_quota(request: Request):
    """清零一份额度的本期用量。body: {"subject": "model:模型名" 或 "channel:渠道 id"}"""
    data = await _read_admin_json(request)
    subject = data.get("subject")
    if not isinstance(subject, str) or not subject.startswith(("model:", "channel:")):
        raise HTTPException(status_code=400, detail="subject 必须是 model:模型名 或 channel:渠道 id")
    model_quota.forget(subject)
    await asyncio.to_thread(model_quota.save_if_dirty)
    return {"status": "success", "message": "用量已重置"}
