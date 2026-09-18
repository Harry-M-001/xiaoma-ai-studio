"""生成任务与资产库接口：图片 / 视频生成、任务查询、资产上传下载。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import settings
from app.database import SessionLocal
from app.deps import require_auth
from app.models import Asset, Task
from app.schemas import (
    AssetBrief,
    AssetList,
    AssetOut,
    ImageBatchGenerateIn,
    ImageBatchPreflightIn,
    ImageGenerateIn,
    ImagePreflightIn,
    PreflightOut,
    PreflightWarningOut,
    TaskOut,
    TaskRetryIn,
    VideoGenerateIn,
    VideoPreflightIn,
    asset_to_out,
)
from app.services import log_service, option_service, preflight, storage
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
        retry_of_task_id=task.retry_of_task_id,
    )


def _asset_to_out(a: Asset) -> AssetOut:
    # 转换只留一份（在 schemas 里）：配音接口也用同一个，多一处复制就多一处漂移
    return asset_to_out(a)


# ---------- 生成前软校验 ----------

def _to_preflight_out(report: preflight.Report) -> PreflightOut:
    return PreflightOut(
        warnings=[
            PreflightWarningOut(
                code=w.code, level=w.level, message=w.message, suggestion=w.suggestion
            )
            for w in report.warnings
        ]
    )


@router.post("/images/preflight", response_model=PreflightOut)
async def preflight_images(
    payload: ImagePreflightIn, db: AsyncSession = Depends(get_db)
) -> PreflightOut:
    """生图前预检。只回告警，永远不拦人（blocking 恒为 False）。"""
    report = await preflight.check_image(
        db, prompt=payload.prompt, model_key=payload.model_key, n=payload.n,
        ref_asset_ids=payload.ref_asset_ids,
    )
    return _to_preflight_out(report)


@router.post("/images/batch/preflight", response_model=PreflightOut)
async def preflight_image_batch(
    payload: ImageBatchPreflightIn, db: AsyncSession = Depends(get_db)
) -> PreflightOut:
    report = await preflight.check_image_batch(
        db, prompts=payload.prompts, model_keys=payload.model_keys, n=payload.n
    )
    return _to_preflight_out(report)


@router.post("/videos/preflight", response_model=PreflightOut)
async def preflight_videos(
    payload: VideoPreflightIn, db: AsyncSession = Depends(get_db)
) -> PreflightOut:
    report = await preflight.check_video(
        db, prompt=payload.prompt, model_key=payload.model_key,
        first_frame_asset_id=payload.first_frame_asset_id,
        ratio=payload.ratio, resolution=payload.resolution,
    )
    return _to_preflight_out(report)


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
    await runner.start_or_fail("image", task.id)
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
        await runner.start_or_fail(t.kind, t.id)
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
    await runner.start_or_fail("video", task.id)
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


@router.get("/tasks/{task_id}/logs")
async def get_task_logs(task_id: int) -> dict:
    """这个任务执行时产生的日志。

    给用户一个「不用来问作者也能自己看清发生了什么」的入口。日志在写入文件时已经
    脱敏，内存里留的这一份同源，所以可以直接展示。
    只在服务运行期间保留（重启后内存里的会丢，文件里的还在）。
    """
    lines = log_service.task_logs(task_id)
    return {"taskId": task_id, "count": len(lines), "lines": lines}


@router.post("/tasks/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(task_id: int, db: AsyncSession = Depends(get_db)) -> TaskOut:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status in ("pending", "processing"):
        task.status = "cancelled"
        task.error = "已手动取消"
        # 与 runner 用同一个时钟：created_at 是库里写的 UTC，这里也必须写 UTC，
        # 否则同一行两个时间列会差出一个时区
        task.completed_at = utcnow()
        await db.commit()
    return await _task_to_out(db, task)


@router.post("/tasks/{task_id}/retry", response_model=TaskOut)
async def retry_task(
    task_id: int,
    body: TaskRetryIn | None = None,
    db: AsyncSession = Depends(get_db),
) -> TaskOut:
    """按**当时的参数快照**重新发起，原记录保留作对照。

    关键点是「快照」而不是「重跑」：
    - 型号、提示词、params_json 全部沿用，只把 `body` 里显式给出的字段盖上去；
    - **画布归属也一起带过去**，重跑出来的产物照常回到原节点的产物区，
      不会变成一条谁也找不到的孤立记录；
    - 按类型分发（文本 / 工作流 / 视频 / 图片），不会把 ComfyUI 任务当图片跑。
    """
    src = await db.get(Task, task_id)
    if src is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if src.status in ("pending", "processing"):
        raise HTTPException(status_code=400, detail="任务仍在进行中，无法重试")

    params = json.loads(src.params_json or "{}")
    if body is not None:
        for key in ("n", "size", "duration", "ratio", "resolution"):
            value = getattr(body, key, None)
            if value is not None:
                params[key] = value

    override_prompt = (body.prompt or "").strip() if body is not None else ""
    task = Task(
        kind=src.kind,
        status="pending",
        service_id=src.service_id,
        model=src.model,
        prompt=override_prompt or src.prompt,
        params_json=json.dumps(params, ensure_ascii=False),
        canvas_project_id=src.canvas_project_id,
        canvas_node_id=src.canvas_node_id,
        retry_of_task_id=src.id,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    await runner.start_or_fail(task.kind, task.id)
    return await _task_to_out(db, task)


@router.post("/tasks/{task_id}/reveal")
async def reveal_task(task_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    """在系统的文件管理器里定位到该任务的产物。

    自托管应用的产物就在本机，用户经常想直接去文件夹里翻，「产物在哪」是个真问题。
    只允许打开**数据目录以内**的位置：这类「用系统命令打开路径」的接口一旦能指到
    任意路径，就等于给了一个任意路径的暴露口子。
    """
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    rows = (await db.execute(select(Asset).where(Asset.task_id == task_id))).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail="这个任务还没有产物")

    root = settings.data_dir.resolve()
    target = (settings.data_dir / rows[0].filename).resolve()
    if root not in target.parents and target != root:
        raise HTTPException(status_code=400, detail="产物路径不在数据目录内")

    return {
        "ok": storage.reveal_in_file_manager(target),
        "path": str(target.parent),
        "name": target.name,
    }


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
        # 顺手记下宽高：图生视频前要用首帧比例跟画幅对帐
        **storage.image_size_kwargs(content, ct),
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
