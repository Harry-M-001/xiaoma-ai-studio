"""ComfyUI 工作流管理：上传 / 列表 / 删除 / 重新解析。

上传 workflow_api.json → 后端解析参数映射表（模型/步数/CFG/采样器/宽高/帧数/
提示词/seed/参考图），画布浮框按映射表动态渲染表单；ComfyUI 在线时附带
select 下拉选项（模型列表、采样器枚举，来自 /object_info）。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import ComfyWorkflow, ProviderService
from app.schemas import ModelSpec
from app.services import comfy_workflow_service as svc
from app.services import config_center_service as cc
from app.services import provider_store

router = APIRouter(prefix="/api/comfy", tags=["comfy"])

ROOT_CONFIG_KEY = "comfy.root"


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


# ---------- 根目录：扫描 ComfyUI 已保存的工作流 ----------


async def _configured_root(db: AsyncSession) -> str:
    cfg = await cc.get_config_map(db)
    return str(cfg.get(ROOT_CONFIG_KEY) or "")


def _model_name(workflow_id: int) -> str:
    return f"comfy:{workflow_id}"


def _sync_model_spec(provider: ProviderService, workflow: ComfyWorkflow) -> None:
    """把导入的工作流挂成这个服务下的一个「模型」，图片/视频页直接能选。"""
    name = _model_name(workflow.id)
    modality = workflow.output_kind if workflow.output_kind in ("image", "video") else "image"
    models = [m for m in provider_store.parse_models(provider.models_json) if m.name != name]
    models.append(
        ModelSpec(name=name, modality=modality, label=f"{workflow.name}（ComfyUI）")
    )
    provider.models_json = json.dumps([m.model_dump() for m in models], ensure_ascii=False)


def _drop_model_spec(provider: ProviderService, workflow_id: int) -> None:
    name = _model_name(workflow_id)
    models = [m for m in provider_store.parse_models(provider.models_json) if m.name != name]
    provider.models_json = json.dumps([m.model_dump() for m in models], ensure_ascii=False)


@router.get("/root")
async def get_root(db: AsyncSession = Depends(get_db)) -> dict:
    return {"root": await _configured_root(db)}


@router.post("/root")
async def set_root(payload: dict, db: AsyncSession = Depends(get_db)) -> dict:
    root = str(payload.get("root") or "").strip()
    if root:
        path = Path(root).expanduser()
        if not path.is_dir():
            raise HTTPException(status_code=400, detail=f"目录不存在：{root}")
        root = str(path.resolve())
    await cc.set_config_value(db, ROOT_CONFIG_KEY, root)
    return {"ok": True, "root": root}


@router.post("/scan")
async def scan_saved(payload: dict | None = None, db: AsyncSession = Depends(get_db)) -> dict:
    """扫描根目录下保存的工作流（只读，不导入）。"""
    root = str((payload or {}).get("root") or "").strip() or await _configured_root(db)
    if not root:
        raise HTTPException(status_code=400, detail="请先填写 ComfyUI 根目录")
    try:
        items = svc.scan_workflows(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"root": root, "items": items}


@router.post("/import")
async def import_saved(payload: dict, db: AsyncSession = Depends(get_db)) -> list[dict]:
    """把扫描结果里的工作流导入工作流库，并挂成该服务下的模型。"""
    paths = [str(p) for p in (payload.get("paths") or []) if str(p).strip()]
    if not paths:
        raise HTTPException(status_code=400, detail="没有选择要导入的工作流")

    provider = await _first_comfy_provider(db)
    if payload.get("provider_id"):
        try:
            provider = await svc.find_comfy_provider(db, int(payload["provider_id"]))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if provider is None:
        raise HTTPException(
            status_code=400,
            detail="请先在「模型服务」中添加一个 ComfyUI 服务（地址默认 http://127.0.0.1:8188）",
        )

    root = await _configured_root(db)
    base = Path(root).resolve() if root else None

    imported: list[dict] = []
    object_info = await _object_info(db, provider.id)
    for raw in paths:
        path = Path(raw).resolve()
        if base is None or (path != base and base not in path.parents):
            raise HTTPException(status_code=400, detail=f"工作流不在配置的根目录里：{raw}")
        if path.suffix.lower() != ".json" or not path.is_file():
            raise HTTPException(status_code=400, detail=f"不是可读的 JSON 文件：{raw}")
        try:
            graph = json.loads(path.read_text(encoding="utf-8"))
            api_graph = svc.convert_ui_to_api(graph)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"{path.name}：{exc}")

        existing = (
            await db.execute(
                select(ComfyWorkflow).where(
                    ComfyWorkflow.provider_id == provider.id,
                    ComfyWorkflow.name == path.stem,
                )
            )
        ).scalars().first()
        if existing is None:
            row = await svc.create_workflow(
                db,
                name=path.stem,
                provider_id=provider.id,
                graph=api_graph,
                object_info=object_info,
            )
        else:
            parsed = svc.parse_workflow(api_graph, object_info)
            existing.graph_json = json.dumps(api_graph, ensure_ascii=False)
            existing.param_map_json = json.dumps(parsed["paramMap"], ensure_ascii=False)
            existing.output_kind = parsed["outputKind"]
            await db.commit()
            await db.refresh(existing)
            row = existing

        _sync_model_spec(provider, row)
        imported.append(svc.workflow_out(row))

    await db.commit()
    return imported


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
        provider = await svc.find_comfy_provider(db, provider_id)
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
    # 与「扫描目录导入」同一条口径：上传完必须挂成这个服务下的模型，
    # 否则图片/视频节点根本选不到它（画布会报「模型已从服务配置中移除」）。
    _sync_model_spec(provider, row)
    await db.commit()
    await db.refresh(row)
    return svc.workflow_out(row)


@router.delete("/workflows/{workflow_id}")
async def delete_workflow(workflow_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(ComfyWorkflow, workflow_id)
    if row is None:
        raise HTTPException(status_code=404, detail="工作流不存在")
    provider = await db.get(ProviderService, row.provider_id)
    if provider is not None:
        _drop_model_spec(provider, row.id)
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
    # 顺手补登记：老版本上传的工作流可能没挂成模型，刷新一次就补回来
    provider = await db.get(ProviderService, refreshed.provider_id)
    if provider is not None:
        _sync_model_spec(provider, refreshed)
        await db.commit()
    return svc.workflow_out(refreshed)
