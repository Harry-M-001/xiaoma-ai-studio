"""模型服务的增删改查、密钥解密与适配器装配。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import security
from app.models import ProviderService
from app.providers.ark import DEFAULT_ARK_BASE
from app.providers.base import AdapterError, BaseAdapter
from app.registry import adapters
from app.schemas import ModelOption, ModelSpec, ProviderIn, ProviderOut


def _parse_models(raw: str) -> list[ModelSpec]:
    """解析模型清单。

    能力类型不再限定为 text/image/video —— 只要声明了 name 与非空 modality
    就接受，具体能力的合法性由 `modalities` 表（配置层）决定。
    """
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    out: list[ModelSpec] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        modality = str(item.get("modality") or "").strip()
        if name and modality:
            out.append(
                ModelSpec(name=name, modality=modality, label=str(item.get("label") or ""))
            )
    return out


def to_out(row: ProviderService) -> ProviderOut:
    return ProviderOut(
        id=row.id,
        name=row.name,
        kind=row.kind,  # type: ignore[arg-type]
        base_url=row.base_url,
        enabled=bool(row.enabled),
        sort_order=row.sort_order,
        models=_parse_models(row.models_json),
        has_api_key=bool(row.api_key_enc),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def list_services(db: AsyncSession) -> list[ProviderService]:
    rows = await db.execute(
        select(ProviderService).order_by(ProviderService.sort_order, ProviderService.id)
    )
    return list(rows.scalars().all())


async def list_out(db: AsyncSession) -> list[ProviderOut]:
    return [to_out(r) for r in await list_services(db)]


async def create(db: AsyncSession, payload: ProviderIn) -> ProviderOut:
    row = ProviderService(
        name=payload.name.strip(),
        kind=payload.kind,
        base_url=payload.base_url.strip(),
        api_key_enc=security.encrypt((payload.api_key or "").strip()),
        enabled=payload.enabled,
        sort_order=payload.sort_order,
        models_json=json.dumps([m.model_dump() for m in payload.models], ensure_ascii=False),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return to_out(row)


async def update(db: AsyncSession, row: ProviderService, payload: ProviderIn) -> ProviderOut:
    row.name = payload.name.strip()
    row.kind = payload.kind
    row.base_url = payload.base_url.strip()
    if payload.api_key:  # 留空表示保持原 Key 不变
        row.api_key_enc = security.encrypt(payload.api_key.strip())
    row.enabled = payload.enabled
    row.sort_order = payload.sort_order
    row.models_json = json.dumps([m.model_dump() for m in payload.models], ensure_ascii=False)
    await db.commit()
    await db.refresh(row)
    return to_out(row)


def build_adapter(row: ProviderService) -> BaseAdapter:
    """按协议名装配适配器 —— 走注册表，新增协议无需改这里。"""
    api_key = security.decrypt(row.api_key_enc)
    base_url = row.base_url or (DEFAULT_ARK_BASE if row.kind == "ark" else "")
    return adapters.build_adapter(row.kind, base_url, api_key)


@dataclass
class ResolvedModel:
    service: ProviderService
    adapter: BaseAdapter
    model_name: str
    modality: str
    label: str


def _make_key(service_id: int, model_name: str) -> str:
    return f"{service_id}:{model_name}"


def list_model_options(rows: list[ProviderService], modality: str | None = None) -> list[ModelOption]:
    options: list[ModelOption] = []
    for row in rows:
        if not row.enabled:
            continue
        for spec in _parse_models(row.models_json):
            if modality and spec.modality != modality:
                continue
            options.append(
                ModelOption(
                    key=_make_key(row.id, spec.name),
                    service_id=row.id,
                    service_name=row.name,
                    name=spec.name,
                    label=spec.label or spec.name,
                    modality=spec.modality,
                )
            )
    return options


async def resolve_model(
    db: AsyncSession, model_key: str, expected_modality: str
) -> ResolvedModel:
    """把前端的 "service_id:model_name" 解析为可用的适配器。"""
    if ":" not in model_key:
        raise AdapterError("模型参数格式不正确，请重新选择模型")
    sid_str, model_name = model_key.split(":", 1)
    try:
        sid = int(sid_str)
    except ValueError as e:
        raise AdapterError("模型参数格式不正确") from e

    row = await db.get(ProviderService, sid)
    if row is None:
        raise AdapterError("模型服务不存在，可能已被删除")
    if not row.enabled:
        raise AdapterError(f"模型服务「{row.name}」已停用，请在模型服务中启用")

    spec = next(
        (m for m in _parse_models(row.models_json) if m.name == model_name),
        None,
    )
    if spec is None:
        raise AdapterError("模型已从服务配置中移除，请重新选择")
    if spec.modality != expected_modality:
        raise AdapterError("所选模型与当前能力不匹配，请重新选择模型")

    return ResolvedModel(
        service=row,
        adapter=build_adapter(row),
        model_name=model_name,
        modality=spec.modality,
        label=spec.label or model_name,
    )
