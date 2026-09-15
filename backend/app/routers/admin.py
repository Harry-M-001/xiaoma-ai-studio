"""通用配置管理接口（受访问口令保护）。

所有已注册的配置表共用这一套接口：

    GET    /api/admin/schema                     已注册的表清单（含字段定义）
    GET    /api/admin/schema/{table}             列表
    POST   /api/admin/schema/{table}             新增
    GET    /api/admin/schema/{table}/audit       变更审计
    POST   /api/admin/schema/{table}/rollback/{log_id}   回滚
    PUT    /api/admin/schema/{table}/{row_id}    修改（需带 version）
    DELETE /api/admin/schema/{table}/{row_id}    删除

新增一张配置表不需要动这里的任何一行代码。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, require_auth
from app.registry.schema_registry import SCHEMA_REGISTRY, TableSpec
from app.services import config_center_service as config_center

router = APIRouter(
    prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_auth)]
)


def _resolve(table: str) -> TableSpec:
    spec = SCHEMA_REGISTRY.get(table)
    if spec is None:
        available = "、".join(sorted(SCHEMA_REGISTRY)) or "（空）"
        raise HTTPException(
            status_code=404, detail=f"未注册的配置表「{table}」，可用：{available}"
        )
    return spec


def _fail(exc: config_center.ConfigError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


@router.get("/schema")
async def list_schemas() -> list[dict]:
    """返回所有已注册配置表的元信息，前端据此动态生成管理界面。"""
    return [config_center.spec_to_meta(s) for s in SCHEMA_REGISTRY.values()]


@router.get("/schema/{table}")
async def list_rows(table: str, db: AsyncSession = Depends(get_db)) -> list[dict]:
    spec = _resolve(table)
    return await config_center.list_rows(db, spec)


@router.post("/schema/{table}")
async def create_row(
    table: str, payload: dict[str, Any], db: AsyncSession = Depends(get_db)
) -> dict:
    spec = _resolve(table)
    try:
        return await config_center.create_row(db, spec, payload)
    except config_center.ConfigError as e:
        raise _fail(e) from e


@router.get("/schema/{table}/audit")
async def list_audit(
    table: str, limit: int = 50, db: AsyncSession = Depends(get_db)
) -> list[dict]:
    _resolve(table)
    return await config_center.list_audit(db, table, limit)


@router.post("/schema/{table}/rollback/{log_id}")
async def rollback(
    table: str, log_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    spec = _resolve(table)
    try:
        return await config_center.rollback_row(db, spec, log_id)
    except config_center.ConfigError as e:
        raise _fail(e) from e


@router.put("/schema/{table}/{row_id}")
async def update_row(
    table: str, row_id: int, payload: dict[str, Any], db: AsyncSession = Depends(get_db)
) -> dict:
    spec = _resolve(table)
    try:
        return await config_center.update_row(db, spec, row_id, payload)
    except config_center.ConfigError as e:
        raise _fail(e) from e


@router.delete("/schema/{table}/{row_id}")
async def delete_row(
    table: str, row_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    spec = _resolve(table)
    try:
        await config_center.delete_row(db, spec, row_id)
    except config_center.ConfigError as e:
        raise _fail(e) from e
    return {"ok": True}
