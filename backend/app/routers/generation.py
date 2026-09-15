"""生成任务与资产库接口：图片 / 视频生成、任务查询、资产上传下载。"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.deps import require_auth
from app.models import Asset, Task
from app.schemas import (
    AssetBrief,
    AssetList,
    AssetOut,
    ImageBatchGenerateIn,
    ImageGenerateIn,
    TaskOut,
    VideoGenerateIn,
)
from app.services import option_service, storage
from app.services.runner import runner

router = APIRouter(prefix="/api", tags=["generation"], dependencies=[Depends(require_auth)])

DEFAULT_MAX_UPLOAD_MB = 10


def _max_upload_bytes() -> int:
    """上传上限取自配置表，可在「系统设置」中调整。"""
    from app.services.config_center_service import runtime_value

    try:
        mb = int(runtime_value("limits.upload_max_mb", DEFAULT_MAX_UPLOAD_MB) or DEFAULT_MAX_UPLOAD_MB)
    except (TypeError, ValueError):
        mb = DEFAULT_MAX_UPLOAD_MB
    return max(1, mb) * 1024 * 1024


def _batch_max_tasks() -> int:
    """单次批量提交的任务数上限，可在「系统设置」中调整。"""
    from app.services.config_center_service import runtime_value

    try:
        v = int(runtime_value("limits.batch_max_tasks", 20) or 20)
    except (TypeError, ValueError):
        v = 20
    return max(1, v)


async def get_db() -> AsyncSession:
    async with SessionLocal() as db:
        yield db


# ---------- 序列化 ----------

async def _task_to_out(db: AsyncSession, task: Task) -> TaskOut:
    rows = await db.execute(select(Asset).where(Asset.task_id == task.id))
    assets = [
        AssetBrief(
            id=a.id,
            kind=a.kind,
            url=f"/media/{a.filename}",
            width=a.width,
            height=a.height,
        )
        for a in rows.scalars().all()
    ]
    return TaskOut(
        id=task.id,
        kind=task.kind,
        status=task.status,
        service_id=task.service_id,
        model=task.model,
        prompt=task.prompt,
        params=json.loads(task.params_json or "{}"),
        error=task.error,
        progress=task.progress,
        assets=assets,
        created_at=task.created_at,
        completed_at=task.completed_at,
    )


def _asset_to_out(a: Asset) -> AssetOut:
    return AssetOut(
        id=a.id,
        kind=a.kind,
        url=f"/media/{a.filename}",
        original_name=a.original_name,
        content_type=a.content_type,
        size=a.size,
        source=a.source,
        prompt=a.prompt,
        width=a.width,
        height=a.height,
        duration=a.duration,
        task_id=a.task_id,
        created_at=a.created_at,
    )


# ---------- 图片 ----------

@router.post("/images/generations", response_model=TaskOut)
async def generate_images(
    payload: ImageGenerateIn, db: AsyncSession = Depends(get_db)
) -> TaskOut:
    # 提前校验模型可用，配置错误直接 400，不产生脏任务
    from app.services import provider_store

    resolved = await provider_store.resolve_model(db, payload.model_key, "image")
    for aid in payload.ref_asset_ids:
        ref = await db.get(Asset, aid)
        if ref is None or ref.kind != "image":
            raise HTTPException(status_code=400, detail="参考图不存在或不是图片")

    # 参数档位以配置表为准，代码里不写死清单
    size = await option_service.validate_option(db, "image_size", payload.size, "图片尺寸")
    count = await option_service.validate_int_option(db, "image_count", payload.n, "生成张数")

    task = Task(
        kind="image",
        status="pending",
        service_id=resolved.service.id,
        model=payload.model_key,
        prompt=payload.prompt,
        params_json=json.dumps(
            {"size": size, "n": count, "ref_asset_ids": payload.ref_asset_ids},
            ensure_ascii=False,
        ),
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    runner.start_image(task.id)
    return await _task_to_out(db, task)


@router.post("/images/batch", response_model=list[TaskOut])
async def batch_generate_images(
    payload: ImageBatchGenerateIn, db: AsyncSession = Depends(get_db)
) -> list[TaskOut]:
    """批量生图：每行提示词 × 每个选中模型 = 一个任务，一次请求原子提交。"""
    from app.services import provider_store

    prompts = [p.strip() for p in payload.prompts if p and p.strip()]
    if not prompts:
        raise HTTPException(status_code=400, detail="至少需要一条提示词")

    # 去重保序，避免同一模型重复提交
    model_keys = list(dict.fromkeys(payload.model_keys))
    total = len(prompts) * len(model_keys)
    max_tasks = _batch_max_tasks()
    if total > max_tasks:
        raise HTTPException(
            status_code=400,
            detail=f"批量任务数 {total} 超过上限 {max_tasks}（提示词 × 模型），可分批提交",
        )

    resolved_list = []
    for mk in model_keys:
        resolved_list.append((mk, await provider_store.resolve_model(db, mk, "image")))

    for aid in payload.ref_asset_ids:
        ref = await db.get(Asset, aid)
        if ref is None or ref.kind != "image":
            raise HTTPException(status_code=400, detail="参考图不存在或不是图片")

    size = await option_service.validate_option(db, "image_size", payload.size, "图片尺寸")
    count = await option_service.validate_int_option(db, "image_count", payload.n, "生成张数")

    created: list[Task] = []
    for p in prompts:
        for mk, resolved in resolved_list:
            task = Task(
                kind="image",
                status="pending",
                service_id=resolved.service.id,
                model=mk,
                prompt=p,
                params_json=json.dumps(
                    {"size": size, "n": count, "ref_asset_ids": payload.ref_asset_ids},
                    ensure_ascii=False,
                ),
            )
            db.add(task)
            created.append(task)
    await db.commit()
    for t in created:
        await db.refresh(t)
        runner.start_image(t.id)
    return [await _task_to_out(db, t) for t in created]


# ---------- 视频 ----------

@router.post("/videos/generations", response_model=TaskOut)
async def generate_videos(
    payload: VideoGenerateIn, db: AsyncSession = Depends(get_db)
) -> TaskOut:
    from app.services import provider_store

    resolved = await provider_store.resolve_model(db, payload.model_key, "video")
    if payload.first_frame_asset_id is not None:
        ff = await db.get(Asset, payload.first_frame_asset_id)
        if ff is None or ff.kind != "image":
            raise HTTPException(status_code=400, detail="首帧图片不存在或不是图片")

    # 参数档位以配置表为准
    duration = await option_service.validate_int_option(db, "video_duration", payload.duration, "视频时长")
    ratio = await option_service.validate_option(db, "video_ratio", payload.ratio, "画幅比例")
    resolution = await option_service.validate_option(
        db, "video_resolution", payload.resolution, "清晰度"
    )

    task = Task(
        kind="video",
        status="pending",
        service_id=resolved.service.id,
        model=payload.model_key,
        prompt=payload.prompt,
        params_json=json.dumps(
            {
                "first_frame_asset_id": payload.first_frame_asset_id,
                "duration": duration,
                "ratio": ratio,
                "resolution": resolution,
            },
            ensure_ascii=False,
        ),
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    runner.start_video(task.id)
    return await _task_to_out(db, task)


# ---------- 任务 ----------

@router.get("/tasks", response_model=list[TaskOut])
async def list_tasks(
    kind: str | None = None, limit: int = 50, db: AsyncSession = Depends(get_db)
) -> list[TaskOut]:
    stmt = select(Task).order_by(Task.id.desc()).limit(min(limit, 200))
    if kind:
        stmt = stmt.where(Task.kind == kind)
    rows = await db.execute(stmt)
    return [await _task_to_out(db, t) for t in rows.scalars().all()]


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: int, db: AsyncSession = Depends(get_db)) -> TaskOut:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return await _task_to_out(db, task)


@router.post("/tasks/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(task_id: int, db: AsyncSession = Depends(get_db)) -> TaskOut:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status in ("pending", "processing"):
        task.status = "cancelled"
        task.error = "已手动取消"
        task.completed_at = datetime.now()
        await db.commit()
    return await _task_to_out(db, task)


@router.post("/tasks/{task_id}/retry", response_model=TaskOut)
async def retry_task(task_id: int, db: AsyncSession = Depends(get_db)) -> TaskOut:
    """按原参数重新发起：复制原任务生成新任务，原记录保留作对照。"""
    src = await db.get(Task, task_id)
    if src is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if src.status in ("pending", "processing"):
        raise HTTPException(status_code=400, detail="任务仍在进行中，无法重试")

    task = Task(
        kind=src.kind,
        status="pending",
        service_id=src.service_id,
        model=src.model,
        prompt=src.prompt,
        params_json=src.params_json,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    if task.kind == "video":
        runner.start_video(task.id)
    else:
        runner.start_image(task.id)
    return await _task_to_out(db, task)


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    """删除任务记录；已生成的产物保留在资产库。"""
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    await db.delete(task)
    await db.commit()
    return {"ok": True}


# ---------- 资产库 ----------

@router.get("/assets", response_model=AssetList)
async def list_assets(
    kind: str | None = None,
    source: str | None = None,
    limit: int = 60,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
) -> AssetList:
    conds = []
    if kind:
        conds.append(Asset.kind == kind)
    if source:
        conds.append(Asset.source == source)

    count_stmt = select(func.count()).select_from(Asset)
    list_stmt = select(Asset).order_by(Asset.id.desc()).limit(limit).offset(offset)
    for c in conds:
        count_stmt = count_stmt.where(c)
        list_stmt = list_stmt.where(c)
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (await db.execute(list_stmt)).scalars().all()
    return AssetList(items=[_asset_to_out(a) for a in rows], total=total)


@router.post("/assets/upload", response_model=AssetOut)
async def upload_asset(
    file: UploadFile = File(...), db: AsyncSession = Depends(get_db)
) -> AssetOut:
    content = await file.read()
    limit = _max_upload_bytes()
    if len(content) > limit:
        raise HTTPException(
            status_code=400, detail=f"文件过大（上限 {limit // 1024 // 1024}MB）"
        )
    ct = (file.content_type or "").lower()
    kind = storage.guess_kind(ct)
    if kind != "image":
        raise HTTPException(status_code=400, detail="目前仅支持上传图片素材")
    rel = storage.save_bytes(content, ct)
    asset = Asset(
        kind="image",
        filename=rel,
        original_name=file.filename or rel.split("/")[-1],
        content_type=ct,
        size=len(content),
        source="uploaded",
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return _asset_to_out(asset)


@router.get("/assets/{asset_id}/download")
async def download_asset(asset_id: int, db: AsyncSession = Depends(get_db)) -> FileResponse:
    asset = await db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    path = storage.abs_path(asset.filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件已被移动或删除")
    return FileResponse(path, media_type=asset.content_type, filename=asset.original_name)


@router.delete("/assets/{asset_id}")
async def delete_asset(asset_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    asset = await db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    storage.delete(asset.filename)
    await db.delete(asset)
    await db.commit()
    return {"ok": True}
