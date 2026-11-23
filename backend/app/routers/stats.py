"""用量接口：任务中心的「近 N 天用量」卡片用。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.deps import require_auth
from app.services import usage_service

router = APIRouter(
    prefix="/api/stats", tags=["stats"], dependencies=[Depends(require_auth)]
)


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


@router.get("/usage")
async def usage(
    days: int = 7,
    project_id: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """近 N 天的实际请求 / 成败 / 耗时 / 计划与实际对账 / Agent 用量。

    只统计数据库里真实存在的东西；估算费用一栏明确返回「未配置价目表」。
    """
    return await usage_service.summarize(db, days=days, project_id=project_id)
