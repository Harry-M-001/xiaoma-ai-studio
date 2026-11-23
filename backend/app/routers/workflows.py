"""工作流模板 API：列表 / 保存 / 依赖预检 / 插入画布。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.services import workflow_template_service as templates

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


class TemplateIn(BaseModel):
    name: str
    description: str = ""
    nodes: list[dict] = []
    edges: list[dict] = []
    inputs: list[dict] = []


class PreflightIn(BaseModel):
    template: str


class InsertIn(BaseModel):
    template: str
    values: dict = {}
    # 前端带着自己看到的画布版本：不一致就 409，不悄悄把模板插进别人改过的画布
    baseRevision: int | None = None


@router.get("/templates")
async def list_templates(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """内置模板在前，自己存的在后。"""
    return await templates.list_templates(db)


@router.post("/templates")
async def create_template(payload: TemplateIn, db: AsyncSession = Depends(get_db)) -> dict:
    """把画布上选中的一段存成模板。"""
    try:
        return await templates.create_template(db, payload.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/preflight")
async def preflight(payload: PreflightIn, db: AsyncSession = Depends(get_db)) -> dict:
    """插入前先看依赖：缺哪些能力、哪个节点引用了不存在的模型。"""
    try:
        return await templates.preflight(db, payload.template)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/projects/{project_id}/insert")
async def insert_template(
    project_id: int, payload: InsertIn, db: AsyncSession = Depends(get_db)
) -> dict:
    """把模板插进项目画布（新 id、右移让位、填输入、补可用模型）。"""
    try:
        return await templates.instantiate(
            db, project_id, payload.template, payload.values, payload.baseRevision
        )
    except ValueError as e:
        if "画布已在别处被修改" in str(e):
            raise HTTPException(status_code=409, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
