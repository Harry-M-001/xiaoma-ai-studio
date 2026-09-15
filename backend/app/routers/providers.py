"""模型服务管理：增删改查、连接测试、模型选项。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.deps import require_auth
from app.providers.base import AdapterError
from app.registry import adapters
from app.schemas import ModelOption, ProviderIn, ProviderOut, ProviderTestIn
from app.services import provider_store

router = APIRouter(prefix="/api/providers", tags=["providers"], dependencies=[Depends(require_auth)])


async def get_db() -> AsyncSession:
    async with SessionLocal() as db:
        yield db


@router.get("", response_model=list[ProviderOut])
async def list_providers(db: AsyncSession = Depends(get_db)) -> list[ProviderOut]:
    return await provider_store.list_out(db)


@router.post("", response_model=ProviderOut)
async def create_provider(
    payload: ProviderIn, db: AsyncSession = Depends(get_db)
) -> ProviderOut:
    return await provider_store.create(db, payload)


@router.put("/{service_id}", response_model=ProviderOut)
async def update_provider(
    service_id: int, payload: ProviderIn, db: AsyncSession = Depends(get_db)
) -> ProviderOut:
    from app.models import ProviderService

    row = await db.get(ProviderService, service_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模型服务不存在")
    return await provider_store.update(db, row, payload)


@router.delete("/{service_id}")
async def delete_provider(service_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    from app.models import ProviderService

    row = await db.get(ProviderService, service_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模型服务不存在")
    await db.delete(row)
    await db.commit()
    return {"ok": True}


@router.post("/test")
async def test_provider(payload: ProviderTestIn) -> dict:
    """测试连接。保存前测试时 api_key 直接取表单值；已保存服务可只传 service_id。"""
    from app.models import ProviderService

    api_key = payload.api_key or ""
    base_url = payload.base_url
    if not api_key and payload.service_id:
        async with SessionLocal() as db:
            row = await db.get(ProviderService, payload.service_id)
            if row is not None:
                from app import security

                api_key = security.decrypt(row.api_key_enc)
                base_url = base_url or row.base_url

    if payload.kind == "ark" and not base_url:
        from app.providers.ark import DEFAULT_ARK_BASE

        base_url = DEFAULT_ARK_BASE

    adapter = adapters.build_adapter(payload.kind, base_url, api_key)
    try:
        await adapter.test_connection(payload.model)
        await adapter.close()
    except AdapterError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "message": "连接正常"}


@router.get("/models", response_model=list[ModelOption])
async def list_models(
    modality: str | None = None, db: AsyncSession = Depends(get_db)
) -> list[ModelOption]:
    rows = await provider_store.list_services(db)
    return provider_store.list_model_options(rows, modality=modality)
