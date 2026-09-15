"""项目管理：一次完整作品的容器。

画布数据结构等画布模块重做后挂接（canvas_json 预留）；
当前提供名称 / 描述 / 状态的 CRUD。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import Project

router = APIRouter(prefix="/api/projects", tags=["projects"])


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


class ProjectOut(BaseModel):
    id: int
    name: str
    description: str
    status: str
    canvas_ready: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectCreateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=2000)


class ProjectUpdateIn(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=2000)
    status: str | None = Field(None, pattern="^(draft|active|archived)$")


def _out(p: Project) -> ProjectOut:
    return ProjectOut(
        id=p.id,
        name=p.name,
        description=p.description,
        status=p.status,
        canvas_ready=bool(p.canvas_json),
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


@router.get("", response_model=list[ProjectOut])
async def list_projects(db: AsyncSession = Depends(get_db)) -> list[ProjectOut]:
    rows = await db.execute(
        select(Project).where(Project.status != "archived").order_by(Project.updated_at.desc())
    )
    return [_out(p) for p in rows.scalars().all()]


@router.post("", response_model=ProjectOut)
async def create_project(payload: ProjectCreateIn, db: AsyncSession = Depends(get_db)) -> ProjectOut:
    dup = await db.execute(
        select(Project).where(Project.name == payload.name.strip(), Project.status != "archived")
    )
    if dup.scalars().first() is not None:
        raise HTTPException(status_code=400, detail="已有同名项目，请换个名字")

    p = Project(name=payload.name.strip(), description=payload.description.strip())
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return _out(p)


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: int, payload: ProjectUpdateIn, db: AsyncSession = Depends(get_db)
) -> ProjectOut:
    p = await db.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    if payload.name is not None:
        p.name = payload.name.strip()
    if payload.description is not None:
        p.description = payload.description.strip()
    if payload.status is not None:
        p.status = payload.status
    await db.commit()
    await db.refresh(p)
    return _out(p)


@router.delete("/{project_id}")
async def delete_project(project_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    p = await db.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    await db.delete(p)
    await db.commit()
    return {"ok": True}
