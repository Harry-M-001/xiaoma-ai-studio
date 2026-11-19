"""dsh sidecar 的生命周期 API：状态 / 安装 / 启动 / 停止。

它是「本机引擎」同一类能力：可选下载、装在数据目录、不随主包分发。
关掉或没装时，原 Agent 与画布完全不依赖它。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, require_auth
from app.services import harness

router = APIRouter(
    prefix="/api/harness", tags=["harness"], dependencies=[Depends(require_auth)]
)


class StartIn(BaseModel):
    port: int = harness.DEFAULT_PORT


class WorkspaceIn(BaseModel):
    path: str


@router.get("/dirs")
async def dirs(path: str = "") -> dict:
    try:
        return harness.list_dirs(path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/workspace")
async def set_workspace(payload: WorkspaceIn) -> dict:
    try:
        path = harness.set_workspace(payload.path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "path": path}


@router.get("/status")
async def status(port: int = harness.DEFAULT_PORT, db: AsyncSession = Depends(get_db)) -> dict:
    return await harness.status(db, port)


@router.post("/install")
async def install() -> dict:
    return {"job": harness.start_install()}


@router.post("/start")
async def start(payload: StartIn, db: AsyncSession = Depends(get_db)) -> dict:
    return await harness.start(db, payload.port)


@router.post("/stop")
async def stop() -> dict:
    return await harness.stop()
