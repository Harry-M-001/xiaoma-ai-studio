"""ComfyUI 工作流管理：上传 / 列表 / 删除 / 重新解析。

上传 workflow_api.json → 后端解析参数映射表（模型/步数/CFG/采样器/宽高/帧数/
提示词/seed/参考图），画布浮框按映射表动态渲染表单；ComfyUI 在线时附带
select 下拉选项（模型列表、采样器枚举，来自 /object_info）。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import ComfyWorkflow, ProviderService
from app.services import comfy_workflow_service as svc

router = APIRouter(prefix="/api/comfy", tags=["comfy"])


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


async def _first_comfy_provider(db: AsyncSession) -> ProviderService | None:
    rows = await db.execute(
        select(ProviderService)
        .where(ProviderService.kind == "comfyui", ProviderService.enabled == True)  # noqa: E712
        .order_by(ProviderService.id)
    )
    return rows.scalars().first()


async def _object_info(db: AsyncSession, provider_id: int | None) -> dict:
    """尽力拉取 /object_info（ComfyUI 未启动时返回空表，select 退化为手填）。"""
    row: ProviderService | None = None
    if provider_id:
        row = await db.get(ProviderService, provider_id)
    else:
        row = await _first_comfy_provider(db)
    if row is None or row.kind != "comfyui":
        return {}
    from app.providers.comfyui import ComfyUIAdapter

    adapter = ComfyUIAdapter(row.base_url, "")
    try:
        return await adapter.get_object_info()
    except Exception:  # noqa: BLE001 - 离线解析是合法状态
        return {}
    finally:
        await adapter.close()


@router.get("/workflows")
async def list_workflows(provider_id: int | None = None, db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = await svc.list_workflows(db, provider_id)
    return [svc.workflow_out(r) for r in rows]


@router.post("/workflows")
async def upload_workflow(
    file: UploadFile,
    name: str = "",
    provider_id: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    if provider_id is None:
        row = await _first_comfy_provider(db)
        if row is None:
            raise HTTPException(status_code=400, detail="请先在「模型服务」中添加一个 ComfyUI 服务（地址默认 http://127.0.0.1:8188）")
        provider_id = row.id

    try:
        await svc.find_comfy_provider(db, provider_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="工作流文件超过 5MB")
    try:
        graph = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"JSON 解析失败：{e}")

    object_info = await _object_info(db, provider_id)
    try:
        row = await svc.create_workflow(
            db,
            name=name or (file.filename or "未命名工作流").removesuffix(".json") or "未命名工作流",
            provider_id=provider_id,
            graph=graph,
            object_info=object_info,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return svc.workflow_out(row)


@router.delete("/workflows/{workflow_id}")
async def delete_workflow(workflow_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(ComfyWorkflow, workflow_id)
    if row is None:
        raise HTTPException(status_code=404, detail="工作流不存在")
    await db.delete(row)
    await db.commit()
    return {"ok": True}


@router.post("/workflows/{workflow_id}/refresh")
async def refresh_workflow(workflow_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    """ComfyUI 启动后重新解析：补全模型/采样器下拉选项。"""
    row = await db.get(ComfyWorkflow, workflow_id)
    if row is None:
        raise HTTPException(status_code=404, detail="工作流不存在")
    object_info = await _object_info(db, row.provider_id)
    refreshed = await svc.refresh_workflow(db, row, object_info)
    return svc.workflow_out(refreshed)
