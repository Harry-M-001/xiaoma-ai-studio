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
                ModelSpec(
                    name=name,
                    modality=modality,
                    label=str(item.get("label") or ""),
                    hidden=bool(item.get("hidden")),
                )
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


# 「快速接入」那边要拿预设里的模型清单，走这个公开入口，
# 免得跨模块去碰带下划线的内部函数。
parse_models = _parse_models


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


async def find_by_base_url(db: AsyncSession, base_url: str) -> ProviderService | None:
    """按接入地址找服务。地址相同即视为同一个服务——重复加一份只会让模型下拉出现同名两份。"""
    target = (base_url or "").strip().rstrip("/")
    if not target:
        return None
    for row in await list_services(db):
        if (row.base_url or "").strip().rstrip("/") == target:
            return row
    return None


async def upsert_by_base_url(
    db: AsyncSession,
    *,
    name: str,
    kind: str,
    base_url: str,
    api_key: str,
    models: list[ModelSpec],
    sort_order: int = 0,
) -> ProviderService:
    """「快速接入」的落库口：地址已存在就更新 Key 并补齐模型，否则新建。

    补模型而不是整体覆盖：用户可能在预设之外自己加过模型（比如某个中转站只开了一部分型号），
    覆盖会把它们删掉。
    """
    row = await find_by_base_url(db, base_url)
    if row is None:
        return await create(
            db,
            ProviderIn(
                name=name,
                kind=kind,  # type: ignore[arg-type]
                base_url=base_url,
                api_key=api_key,
                enabled=True,
                sort_order=sort_order,
                models=models,
            ),
        )
    row.name = row.name or name
    row.kind = kind
    row.api_key_enc = security.encrypt(api_key.strip())
    row.enabled = True
    existing = {m.name for m in _parse_models(row.models_json)}
    merged = _parse_models(row.models_json) + [m for m in models if m.name not in existing]
    row.models_json = json.dumps([m.model_dump() for m in merged], ensure_ascii=False)
    await db.commit()
    await db.refresh(row)
    return to_out(row)



@dataclass
class ResolvedModel:
    service: ProviderService
    adapter: BaseAdapter
    model_name: str
    modality: str
    label: str


def _make_key(service_id: int, model_name: str) -> str:
    return f"{service_id}:{model_name}"


def supports_thinking(service: ProviderService, model_name: str) -> bool:
    """这个模型能不能开思考模式（纯判据，与适配器里的判定同一条口径）。

    只认 DeepSeek 的 OpenAI 兼容端点：`thinking` 是它家文档里的入参形状，
    别家收到不认识的字段多半直接 400。判不出来就不给开——比「点了没反应」诚实。
    """
    if service.kind != "openai":
        return False
    return "deepseek" in (service.base_url or "").lower() or model_name.lower().startswith("deepseek")


def video_duration_limit(kind: str, model_name: str) -> tuple[int, int]:
    """这个视频模型的时长区间（秒）。

    时长不是「档位」：同一个下拉里既有只认 5/10 的老模型，也有能到 30 秒的
    Seedance 2.5 / Wan 3.0。把区间放在这里（而不是前端按模型名猜），
    界面和接口校验用的是同一份判据。判不出来时给一个保守但不算死的区间。
    """
    kind = (kind or "").strip().lower()
    name = (model_name or "").strip().lower()

    if kind == "ark":
        if "seedance-2-5" in name:
            return 4, 30
        if "seedance-2-0" in name:
            return 4, 15
        if "seedance-1-5" in name:
            return 4, 12
        if "seedance-1-0" in name:
            return 5, 10  # 1.0 系列只有 5 / 10 两档
        return 4, 15

    if kind == "kie":
        if "seedance-2-5" in name:
            return 4, 30
        if "seedance-2" in name:
            return 4, 15
        if "kling-4" in name:
            return 3, 30
        if "wan/3-0" in name:
            return 2, 30
        return 5, 10  # 老一代（kling v2.5 / seedream v4 视频等）

    if kind == "dashscope":
        if name.startswith("wan3"):
            return 2, 30
        if "kling-v4" in name:
            return 3, 30
        if name.startswith("kling/"):
            return 3, 15
        if "wan2.7-r2v" in name:
            return 2, 10
        if name.startswith("wan2.7"):
            return 2, 15
        return 2, 15

    # 认不出厂商：给 3–15 的常用区间，超出由上游报错；不要在这里装作知道
    return 3, 15


def list_model_options(rows: list[ProviderService], modality: str | None = None) -> list[ModelOption]:
    options: list[ModelOption] = []
    for row in rows:
        if not row.enabled:
            continue
        for spec in _parse_models(row.models_json):
            if modality and spec.modality != modality:
                continue
            # 兼容别名只参与解析，不出现在下拉里（否则一个服务下挂四个同义模型）
            if spec.hidden:
                continue
            low, high = (
                video_duration_limit(row.kind, spec.name)
                if spec.modality == "video"
                else (None, None)
            )
            options.append(
                ModelOption(
                    key=_make_key(row.id, spec.name),
                    service_id=row.id,
                    service_name=row.name,
                    name=spec.name,
                    label=spec.label or spec.name,
                    modality=spec.modality,
                    thinking=supports_thinking(row, spec.name),
                    min_duration=low,
                    max_duration=high,
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

    # ComfyUI「工作流即模型」：模型名形如 comfy:12，图与参数表从工作流库取，
    # 适配器带着它们构造，后面 generate_image / submit_video 就能直接跑。
    if row.kind == "comfyui" and model_name.startswith("comfy:"):
        from app.models import ComfyWorkflow
        from app.providers.comfyui import ComfyUIAdapter

        try:
            workflow_id = int(model_name.split(":", 1)[1])
        except (IndexError, ValueError) as exc:
            raise AdapterError("ComfyUI 工作流模型的编号不正确，请重新扫描导入") from exc
        workflow = await db.get(ComfyWorkflow, workflow_id)
        if workflow is None or workflow.provider_id != row.id:
            raise AdapterError("这个 ComfyUI 工作流已被删除，请到模型服务里重新扫描导入")
        adapter = ComfyUIAdapter(
            row.base_url,
            "",
            workflow=json.loads(workflow.graph_json or "{}"),
            params=json.loads(workflow.param_map_json or "[]"),
        )
        return ResolvedModel(
            service=row,
            adapter=adapter,
            model_name=model_name,
            modality=spec.modality,
            label=spec.label or model_name,
        )

    return ResolvedModel(
        service=row,
        adapter=build_adapter(row),
        model_name=model_name,
        modality=spec.modality,
        label=spec.label or model_name,
    )
