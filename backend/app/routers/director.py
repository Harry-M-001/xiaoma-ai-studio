"""导演台 v1：本地视频粗剪（导入 → 入/出点截取 → 片段合并导出）。

所有处理在服务端用 FFmpeg 完成，产物统一进资产库。
"""

from __future__ import annotations

import base64
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import Asset
from app.schemas import (
    AssetOut,
    DirectorExtractIn,
    DirectorMergeIn,
    DirectorProbeIn,
    DirectorThumbnailIn,
    RemovalApplyIn,
    RemovalCheckIn,
    RemovalFrameIn,
    RemovalPreviewIn,
)
from app.services import (
    ffmpeg_service,
    image_size,
    storage,
    subtitle_fonts,
    subtitle_removal,
    subtitles,
    transitions,
)

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
    # 源视频没记宽高时（v1.1.7 之前入库的都在此列）去问一次产物：
    # 与截帧同一处口径——不照抄可能为空的来源，否则这段的分辨率一路是空的。
    width, height = a.width, a.height
    if not width or not height:
        meta = await ffmpeg_service.probe(storage.abs_path(rel))
        width = meta.get("width") or width
        height = meta.get("height") or height
    asset = Asset(
        kind="video",
        filename=rel,
        original_name=f"clip_{payload.start:.1f}-{payload.end:.1f}s.mp4",
        content_type="video/mp4",
        size=out_path.stat().st_size,
        source="clip",
        width=width,
        height=height,
        # 时长仍按用户点的区间算：probe 出来的是编码后的实际长度（9.98 之类），
        # 拿它取整会让「截了 10 秒」显示成 9 秒
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
    # 帧的尺寸以文件为准：照抄视频资产的宽高会踩两个坑——老视频根本没记宽高
    # （于是截出来的图也没宽高），而下游的首帧预检读不到尺寸就**静默不提醒**。
    picked = image_size.read_image_size_from_file(storage.abs_path(rel))
    asset = Asset(
        kind="image",
        filename=rel,
        original_name=f"frame_{payload.t:.1f}s.jpg",
        content_type="image/jpeg",
        size=out_path.stat().st_size,
        source="frame",
        width=picked[0] if picked else a.width,
        height=picked[1] if picked else a.height,
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return _asset_out(asset)


@router.get("/transitions")
async def list_transitions() -> dict:
    """转场与转场音效的可选项（给前端的两个下拉用）。

    预设表在 `services/transitions.py` 里——那是唯一一份，前端不另抄一份：
    抄一份的话，界面能选出一种后端不认识的转场，而报错要到合并时才知道。
    """
    return {
        "presets": [dict(p) for p in transitions.PRESETS],
        "sfx": [dict(p) for p in transitions.SFX_PRESETS],
        "seconds": {
            "min": transitions.MIN_SECONDS,
            "max": transitions.MAX_SECONDS,
            "default": transitions.DEFAULT_SECONDS,
        },
    }


@router.post("/subtitle-removal/frame")
async def removal_frame(payload: RemovalFrameIn, db: AsyncSession = Depends(get_db)) -> dict:
    """取一帧用来框选字幕区域，并把这一框上的可选项**一次回齐**。

    为什么一次回齐（帧 + 尺寸 + 推荐框 + 四种手法能不能用）：这些量彼此相关，
    分几次拿就会出现「框按旧尺寸画、手法按新尺寸判」这种对不上的中间状态。

    尺寸回的是**源视频**的像素（框坐标系），显示用的图另缩到 1280 以内——
    前端按显示尺寸换算去画框。
    """
    a = await _get_video_asset(db, payload.asset_id)
    path = storage.abs_path(a.filename)
    info = await ffmpeg_service.probe(path)
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    if width <= 0 or height <= 0:
        raise HTTPException(status_code=400, detail="读不出这个视频的尺寸，没法框选")
    duration = float(info.get("duration") or 0)
    # 时间点夹在片长之内：拖到最右端时那一帧可能取不到，回退一点点
    t = min(max(0.0, float(payload.t)), max(0.0, duration - 0.05)) if duration else 0.0
    try:
        raw, width, height = await ffmpeg_service.removal_frame(path, t)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    box = subtitle_removal.default_box(width, height)
    return {
        "image": "data:image/jpeg;base64," + base64.b64encode(raw).decode(),
        "width": width,
        "height": height,
        "seconds": round(t, 3),
        "duration": round(duration, 3),
        "box": box,
        "methods": subtitle_removal.method_rows(box, width, height),
        "defaultMethod": subtitle_removal.default_method(box, width, height),
    }


def _resolve_removal(box_raw: dict, method: str, width: int, height: int) -> tuple[dict, str, dict]:
    """把「框 + 手法」收拾成一个能跑的请求，收拾不了就报能看懂的话。

    返回（夹好的框，手法 key，这一手法的行）。三件事都在这里判：框夹进画面、
    手法认不认识、在当前框上能不能用。
    """
    box = subtitle_removal.sanitize_box(box_raw, width, height)
    problem = subtitle_removal.box_problem(box, width, height)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    method = (method or "").strip() or subtitle_removal.default_method(box, width, height)
    rows = {r["key"]: r for r in subtitle_removal.method_rows(box, width, height)}
    row = rows.get(method)
    if row is None:
        raise HTTPException(status_code=400, detail=f"不认识的手法：{method}")
    if not row["available"]:
        raise HTTPException(status_code=400, detail=str(row["reason"]))
    return box, method, row


@router.post("/subtitle-removal/check")
async def removal_check(payload: RemovalCheckIn, db: AsyncSession = Depends(get_db)) -> dict:
    """只算「这个框上四种手法各能不能用」。**不碰 ffmpeg**，所以拖动松手后可以随便调。

    为什么要有它：可选项随框变（框挪到画面中间时「裁掉」就不能用了）。界面上那份
    可用性要是只在打开弹窗时算一次，用户拖动之后看到的还是旧结论——于是「界面说能选、
    点了才报错」。这类不一致比直接不给选更让人不信任。
    """
    a = await _get_video_asset(db, payload.asset_id)
    info = await ffmpeg_service.probe(storage.abs_path(a.filename))
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    if width <= 0 or height <= 0:
        raise HTTPException(status_code=400, detail="读不出这个视频的尺寸")
    box = subtitle_removal.sanitize_box(payload.box.model_dump(), width, height)
    return {
        "box": box,
        "methods": subtitle_removal.method_rows(box, width, height),
        "defaultMethod": subtitle_removal.default_method(box, width, height),
    }


@router.post("/subtitle-removal/preview")
async def removal_preview(payload: RemovalPreviewIn, db: AsyncSession = Depends(get_db)) -> dict:
    """把去字幕**真渲一帧**出来看。

    只渲一帧而不是整段：整段要几十秒到几分钟，而用户是拖着框反复看的。
    预览与成品走**同一套滤镜参数**（`subtitle_removal.filter_args`），所以「看着行」
    就等于「导出来行」——这一点和字幕版式预览是同一条口径。
    """
    a = await _get_video_asset(db, payload.asset_id)
    path = storage.abs_path(a.filename)
    info = await ffmpeg_service.probe(path)
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    if width <= 0 or height <= 0:
        raise HTTPException(status_code=400, detail="读不出这个视频的尺寸，没法预览")

    box, method, row = _resolve_removal(payload.box.model_dump(), payload.method, width, height)
    try:
        raw = await ffmpeg_service.render_removal_preview(
            path, payload.t, box=box, method=method, width=width, height=height
        )
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "image": "data:image/jpeg;base64," + base64.b64encode(raw).decode(),
        "box": box,
        "method": method,
        "label": row["label"],
        # 手法的那句取舍要跟着预览一起回来：用户看着效果，同时看到代价
        "note": row["hint"],
        "methods": subtitle_removal.method_rows(box, width, height),
    }


@router.post("/subtitle-removal", response_model=AssetOut)
async def remove_subtitles(payload: RemovalApplyIn, db: AsyncSession = Depends(get_db)) -> AssetOut:
    """按这个框与手法把整段的字幕去掉，产物进资产库。

    产物是**新资产**，不动原片：手法有四种、框也可能要试几次，把原片覆盖掉就等于
    让用户没法回头（与产物版本栈、候选定稿是同一条口径：原来的东西不许被悄悄改掉）。
    """
    a = await _get_video_asset(db, payload.asset_id)
    path = storage.abs_path(a.filename)
    info = await ffmpeg_service.probe(path)
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    if width <= 0 or height <= 0:
        raise HTTPException(status_code=400, detail="读不出这个视频的尺寸，去不了字幕")

    box, method, row = _resolve_removal(payload.box.model_dump(), payload.method, width, height)
    try:
        out_path = await ffmpeg_service.remove_subtitles(
            path, box=box, method=method, width=width, height=height
        )
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=500, detail=str(e))

    rel = ffmpeg_service._save_asset_file(out_path, "mp4")
    # 尺寸以**产物**为准：裁掉那一路会把画面变矮，照抄源视频的宽高就是错的
    meta = await ffmpeg_service.probe(storage.abs_path(rel))
    used = Path(a.original_name or "video").stem
    asset = Asset(
        kind="video",
        filename=rel,
        original_name=f"{used}_去字幕-{row['label']}.mp4",
        content_type="video/mp4",
        size=out_path.stat().st_size,
        source="clip",
        width=meta.get("width") or width,
        height=meta.get("height") or height,
        duration=int(float(meta.get("duration") or 0)) or a.duration,
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    logger.info("去字幕产物入库：%s（手法 %s，框 %s）", asset.original_name, method, box)
    return _asset_out(asset)


async def _clip_seconds(payload: DirectorMergeIn, db: AsyncSession) -> list[float | None]:
    """逐段量时长。**转场位置就是按它算出来的**，所以量不出来就不能硬接
    （见 `transitions.check_clips`），这里如实把 None 交回去让校验去拦。"""
    out: list[float | None] = []
    for aid in payload.asset_ids:
        asset = await _get_video_asset(db, aid)
        info = await ffmpeg_service.probe(storage.abs_path(asset.filename))
        out.append(info.get("duration"))
    return out


@router.post("/merge/preview")
async def merge_preview(payload: DirectorMergeIn, db: AsyncSession = Depends(get_db)) -> dict:
    """合并前先算账：成片多长、比硬切短多少、这样接行不行。

    与 `/merge` 用**同一套口径**（`services/transitions.py`），不另算一份——
    两处各算一次的话，「弹窗里写 13 秒、导出来 12.5 秒」是最难解释的那种不一致。

    这一条是纯读：不写盘、不改资产、不动 ffmpeg 编码（只 ffprobe 量时长）。
    """
    durations = await _clip_seconds(payload, db)
    key = transitions.sanitize_key(payload.transition)
    seconds = transitions.sanitize_seconds(payload.transition_seconds)
    usable = [d for d in durations if d]
    return {
        "transition": key,
        "sfx": transitions.sanitize_sfx(payload.sfx),
        "transitionSeconds": seconds,
        "clipSeconds": [round(d, 2) if d else None for d in durations],
        "hardCutSeconds": round(sum(usable), 2),
        "totalSeconds": round(transitions.total_seconds(usable, key, seconds), 2),
        # 成片会变短多少秒：xfade 是把相邻两段交叠，不是插一段新的
        "shortfallSeconds": round(transitions.shortfall(usable, key, seconds), 2),
        "problem": transitions.check_clips(durations, key, seconds),
    }


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

    key = transitions.sanitize_key(payload.transition)
    seconds = transitions.sanitize_seconds(payload.transition_seconds)

    # 转场放不下 / 时长读不出来：这是**输入问题**，回 400 并说清哪一段，
    # 而不是扔给 ffmpeg 报一句 Invalid duration（用户看不懂那句话）
    durations = [await ffmpeg_service.probe(storage.abs_path(a.filename)) for a in assets]
    problem = transitions.check_clips([d.get("duration") for d in durations], key, seconds)
    if problem:
        raise HTTPException(status_code=400, detail=problem)

    # 转场音效：内置合成 或 资产库里的一条音频（给了后者就以它为准）
    sfx_path, sfx_label, sfx_tmp_dir = await _resolve_sfx(payload, key, seconds, db)

    try:
        out_path = await ffmpeg_service.merge_videos(
            [storage.abs_path(a.filename) for a in assets],
            transition=key,
            transition_seconds=seconds,
            sfx_path=sfx_path,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if sfx_tmp_dir is not None:
            # 合成音效只是这次合并的中间产物，用完就删（它不该堆在 storage/tmp 里）
            shutil.rmtree(sfx_tmp_dir, ignore_errors=True)

    rel = ffmpeg_service._save_asset_file(out_path, "mp4")
    # 成片时长**以量出来的为准**，不按「各段相加」估：加了转场之后确实会短一截，
    # 估算值万一和实际差一点，下游（导演台列表、以后的对账）就会一直差着
    real = (await ffmpeg_service.probe(out_path)).get("duration")
    total = real or transitions.total_seconds(
        [d.get("duration") or 0 for d in durations], key, seconds
    )

    # ---- 字幕（可选）：合完再烧，时间轴按**成片**里各段的区间算
    subtitle_note = ""
    burned = await _burn_subtitles_if_asked(payload, out_path, durations, key, seconds)
    if burned is not None:
        rel, subtitle_note, out_path = burned

    label = transitions.preset(key)["label"]
    note = f"{len(assets)} 段 · 转场 {label} {seconds:g}s"
    if sfx_label:
        note += f" · 音效 {sfx_label}"
    if subtitle_note:
        note += f" · 字幕 {subtitle_note}"
    asset = Asset(
        kind="video",
        filename=rel,
        original_name=f"merged_{len(assets)}clips.mp4",
        content_type="video/mp4",
        size=out_path.stat().st_size,
        source="merged",
        width=assets[0].width,
        height=assets[0].height,
        duration=int(round(total)) if total else None,
        # 来路写进 prompt：资产库里一眼能看出这条片子是怎么接的（和样片同一个思路）
        prompt=f"{note} ｜ " + " / ".join(a.original_name for a in assets)[:1800],
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return _asset_out(asset)


async def _burn_subtitles_if_asked(
    payload: DirectorMergeIn,
    merged: Path,
    durations: list[dict],
    key: str,
    seconds: float,
) -> tuple[str, str, Path] | None:
    """要字幕就烧上去；不要就回 None。回 `(相对路径, 落库说明, 烧完的成片路径)`。

    三件必须做对的事：

    1. **时间轴按「成片里各段的区间」算**（`transitions.clip_spans`），不是各段时长累加。
       配了转场之后成片会变短，累加会让字幕一段比一段提前——三段 0.5 秒转场、第三段
       就偏了 1 秒，用户看到的是「字幕比画面早出来一截」。
    2. **先拦住再干活**：字体没下 / 字体损坏 / 没有 libass，都在烧之前报错，
       而不是让 libass 静默回落系统字体（那会出一部「有字幕但字体不对」的片子）。
    3. **烧完要以新的成片为准重新起个名字再落库**：烧字幕是重编码，产物是**另一个文件**，
       沿用旧文件名会让 `size` 对不上（而 size 是要给用户看的）。
    """
    texts = list(payload.subtitles or [])
    if not texts or not any(str(t or "").strip() for t in texts):
        return None

    lens = [float(d.get("duration") or 0) for d in durations]
    spans = transitions.clip_spans(lens, key, seconds)
    cues = subtitles.cues_from_spans(spans, texts)
    problem = subtitles.check_cues(cues)
    if problem:
        raise HTTPException(status_code=400, detail=problem)

    style_key = subtitles.resolve_style(payload.subtitle_style)
    ass = subtitles.build_ass(
        cues,
        style_key=style_key,
        width=int(durations[0].get("width") or 1280),
        height=int(durations[0].get("height") or 720),
        size_scale=payload.subtitle_scale,
    )
    fonts = subtitles.fonts_used(style_key)
    # 字体这条链的体检放在最前面：它要么过、要么给出一句人话，不让 ffmpeg 去报。
    # **先预热滤镜探测**：`check_ready` 里的滤镜判据读的是缓存，缓存没预热时会把
    # 「还没探过」当成「没有滤镜」，报一句完全错误的理由。
    await ffmpeg_service.warm_subtitle_filter()
    trouble = subtitle_fonts.check_ready(fonts)
    if trouble:
        raise HTTPException(status_code=400, detail=trouble)

    try:
        out = await ffmpeg_service.burn_subtitles(merged, ass, font_keys=fonts)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # 没字幕那版是中间产物：留着只会让 storage 里多一份没人认领的大文件
        merged.unlink(missing_ok=True)

    rel = ffmpeg_service._save_asset_file(out, "mp4")
    st = subtitles.style(style_key) or {}
    return rel, f"{len(cues)} 条 · {st.get('label') or style_key}", out


async def _resolve_sfx(
    payload: DirectorMergeIn, key: str, seconds: float, db: AsyncSession
) -> tuple[Path | None, str, Path | None]:
    """把「转场音效」解析成一个要混进去的音频文件。

    返回 `(文件路径或 None, 用于落库说明的标签, 用完要删的临时目录或 None)`。
    两条路：**选资产库里的音频**（用户自己的素材）优先；否则用内置配方现场合成。

    选错资产 / 文件不在了都直接报错，不静默降级成「没有音效」——
    那样用户会以为音效生效了，听不出来才发现。
    """
    if transitions.is_cut(key):
        return None, "", None

    if payload.sfx_asset_id is not None:
        picked = await db.get(Asset, payload.sfx_asset_id)
        if picked is None:
            raise HTTPException(status_code=400, detail="选的那条音效资产已经不在了，回资产库另挑一条")
        if picked.kind != "audio":
            raise HTTPException(status_code=400, detail="转场音效要选一条「音频」资产")
        path = storage.abs_path(picked.filename)
        if not path.exists():
            raise HTTPException(status_code=400, detail="选的那条音效文件不在了，换一条再试")
        return path, (picked.name or picked.original_name), None

    kind = transitions.sanitize_sfx(payload.sfx)
    if not kind:
        return None, "", None
    try:
        wav = await ffmpeg_service.synth_sfx(kind, transitions.sfx_seconds(seconds))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    label = next(p["label"] for p in transitions.SFX_PRESETS if p["key"] == kind)
    return wav, label, wav.parent
