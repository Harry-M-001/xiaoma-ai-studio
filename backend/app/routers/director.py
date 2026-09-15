"""导演台 v1：本地视频粗剪（导入 → 入/出点截取 → 片段合并导出）。

所有处理在服务端用 FFmpeg 完成，产物统一进资产库。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import Asset
from app.schemas import AssetOut, DirectorExtractIn, DirectorMergeIn, DirectorProbeIn, DirectorThumbnailIn
from app.services import ffmpeg_service, storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/director", tags=["director"])

_DEFAULT_VIDEO_LIMIT_MB = 500


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


def _asset_out(a: Asset) -> AssetOut:
    return AssetOut(
        id=a.id,
        kind=a.kind,
        url=f"/media/{a.filename}",
        filename=a.filename,
        original_name=a.original_name,
        content_type=a.content_type,
        size=a.size,
        source=a.source,
        prompt=a.prompt,
        width=a.width,
        height=a.height,
        duration=a.duration,
        created_at=a.created_at,
    )


def _video_upload_limit() -> int:
    from app.services.config_center_service import runtime_value

    try:
        v = int(runtime_value("limits.video_upload_max_mb", _DEFAULT_VIDEO_LIMIT_MB) or _DEFAULT_VIDEO_LIMIT_MB)
    except (TypeError, ValueError):
        v = _DEFAULT_VIDEO_LIMIT_MB
    return max(1, v)


async def _get_video_asset(db: AsyncSession, asset_id: int) -> Asset:
    a = await db.get(Asset, asset_id)
    if a is None or a.kind != "video":
        raise HTTPException(status_code=404, detail="视频资产不存在")
    return a


@router.get("/ffmpeg")
async def ffmpeg_status() -> dict:
    """FFmpeg 能力检测：前端据此决定是否显示安装指引。"""
    ok, version = await ffmpeg_service.ffmpeg_available()
    return {"available": ok, "version": version}


@router.get("/videos")
async def list_videos(db: AsyncSession = Depends(get_db)) -> list[AssetOut]:
    """可用的视频素材（导入 / 截取 / 合并产物），最新在前。"""
    rows = await db.execute(
        select(Asset)
        .where(Asset.kind == "video")
        .order_by(Asset.id.desc())
        .limit(50)
    )
    return [_asset_out(a) for a in rows.scalars().all()]


@router.post("/import", response_model=AssetOut)
async def import_video(
    file: UploadFile = File(...), db: AsyncSession = Depends(get_db)
) -> AssetOut:
    available, _ = await ffmpeg_service.ffmpeg_available()
    if not available:
        raise HTTPException(status_code=503, detail="服务器未安装 FFmpeg，无法使用导演台")

    ct = (file.content_type or "").lower()
    if not ct.startswith("video/"):
        raise HTTPException(status_code=400, detail="请上传视频文件（mp4 / webm / mov 等）")

    limit = _video_upload_limit() * 1024 * 1024
    content = await file.read()
    if len(content) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"视频超过大小上限 {_video_upload_limit()} MB，可在「系统设置 → 限制」调整",
        )

    rel = storage.save_bytes(content, ct)
    path = storage.abs_path(rel)
    meta = await ffmpeg_service.probe(path)
    asset = Asset(
        kind="video",
        filename=rel,
        original_name=file.filename or "imported.mp4",
        content_type=ct,
        size=len(content),
        source="uploaded",
        width=meta.get("width"),
        height=meta.get("height"),
        duration=int(meta["duration"]) if meta.get("duration") else None,
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return _asset_out(asset)


@router.post("/probe")
async def probe_video(payload: DirectorProbeIn, db: AsyncSession = Depends(get_db)) -> dict:
    a = await _get_video_asset(db, payload.asset_id)
    meta = await ffmpeg_service.probe(storage.abs_path(a.filename))
    return meta


@router.post("/extract", response_model=AssetOut)
async def extract_clip(
    payload: DirectorExtractIn, db: AsyncSession = Depends(get_db)
) -> AssetOut:
    a = await _get_video_asset(db, payload.asset_id)
    if payload.end <= payload.start:
        raise HTTPException(status_code=400, detail="出点必须晚于入点")
    if payload.start < 0:
        raise HTTPException(status_code=400, detail="入点不能为负数")

    try:
        out_path = await ffmpeg_service.extract_clip(
            storage.abs_path(a.filename), payload.start, payload.end
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    rel = ffmpeg_service._save_asset_file(out_path, "mp4")
    asset = Asset(
        kind="video",
        filename=rel,
        original_name=f"clip_{payload.start:.1f}-{payload.end:.1f}s.mp4",
        content_type="video/mp4",
        size=out_path.stat().st_size,
        source="clip",
        width=a.width,
        height=a.height,
        duration=int(payload.end - payload.start),
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return _asset_out(asset)


@router.post("/thumbnail", response_model=AssetOut)
async def extract_thumbnail(
    payload: DirectorThumbnailIn, db: AsyncSession = Depends(get_db)
) -> AssetOut:
    a = await _get_video_asset(db, payload.asset_id)
    try:
        out_path = await ffmpeg_service.extract_frame(storage.abs_path(a.filename), payload.t)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    rel = ffmpeg_service._save_asset_file(out_path, "jpg")
    asset = Asset(
        kind="image",
        filename=rel,
        original_name=f"frame_{payload.t:.1f}s.jpg",
        content_type="image/jpeg",
        size=out_path.stat().st_size,
        source="frame",
        width=a.width,
        height=a.height,
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return _asset_out(asset)


@router.post("/merge", response_model=AssetOut)
async def merge_videos(
    payload: DirectorMergeIn, db: AsyncSession = Depends(get_db)
) -> AssetOut:
    if len(payload.asset_ids) < 2:
        raise HTTPException(status_code=400, detail="合并至少需要 2 个片段")

    assets: list[Asset] = []
    for aid in payload.asset_ids:
        a = await _get_video_asset(db, aid)
        assets.append(a)

    try:
        out_path = await ffmpeg_service.merge_videos(
            [storage.abs_path(a.filename) for a in assets]
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    rel = ffmpeg_service._save_asset_file(out_path, "mp4")
    total = sum(a.duration or 0 for a in assets)
    asset = Asset(
        kind="video",
        filename=rel,
        original_name=f"merged_{len(assets)}clips.mp4",
        content_type="video/mp4",
        size=out_path.stat().st_size,
        source="merged",
        width=assets[0].width,
        height=assets[0].height,
        duration=int(total) if total else None,
        prompt=" / ".join(a.original_name for a in assets)[:2000],
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return _asset_out(asset)
