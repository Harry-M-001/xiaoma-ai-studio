"""补帧接口：看能补到多少帧、派一条补帧任务。

这一版只有**本机一条路**（RIFE）。走 ComfyUI 补帧要往它那儿传视频，而现有的工作流
适配器只做图片上传，所以那条这一版**不做**——界面上会明确写出来，而不是灰着不说原因。

**我们也没有随包给出 ComfyUI 的补帧模板**，这一点是刻意的：社区那套补帧节点
（`ComfyUI-Frame-Interpolation`）要用户自己装，而模板里的节点名与连线**没法在这儿
验证**。发一份没验过的模板，用户拿到的是「导入就报错」，比不发更糟。
想走那条路的话，`docs/comfyui.md` 里写了该装什么，工作流自己在画布上连一次即可。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import Asset, Task
from app.routers.generation import _task_to_out
from app.schemas import InterpolateIn, TaskOut
from app.services import (
    engine_install,
    ffmpeg_service,
    interpolate as ip,
    local_engines as le,
    storage,
    upscale as up,
    upscale_run as urun,
)
from app.services.runner import runner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/interpolate", tags=["interpolate"])


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


async def _load_video(db: AsyncSession, asset_id: int) -> tuple[Asset, dict]:
    asset = await db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    if asset.kind != "video":
        raise HTTPException(status_code=400, detail="补帧是给视频用的；图片请用「放大」")
    src = storage.abs_path(asset.filename)
    if not src.exists():
        raise HTTPException(status_code=400, detail="这个资产的原始文件不在磁盘上了")
    info = await ffmpeg_service.probe(src)
    frames = 0
    fps = float(info.get("fps") or 0)
    duration = float(info.get("duration") or 0)
    if fps and duration:
        frames = int(round(duration * fps))
    return asset, {
        "width": int(info.get("width") or 0),
        "height": int(info.get("height") or 0),
        "fps": fps,
        "duration": duration,
        "frames": frames,
        "size": int(asset.size or 0),
        "name": asset.prompt or asset.original_name or f"资产 {asset.id}",
    }


@router.get("/options")
async def options(asset_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    """补帧弹窗要的一切。一次给全。"""
    asset, source = await _load_video(db, asset_id)
    if source["frames"] <= 0 or source["fps"] <= 0:
        raise HTTPException(status_code=400, detail="读不出这段视频的帧率与帧数，没法补帧")

    installed = {e.key for e in le.engines() if engine_install.installed(e)}
    hw = engine_install.hardware()
    ready, reason, action, where = ip.check_ready(installed=installed, vulkan=hw.vulkan)
    engine = le.by_key(ip.ENGINE_KEY)

    models: list[dict] = []
    for m in ip.models():
        models.append({
            "key": m.key,
            "label": m.label,
            "note": m.note,
            "custom": m.custom,
            "note2xOnly": m.note_2x_only,
            # **目标帧率跟着模型走**：只做 2 倍的模型只给一个选项。
            # 给不出来却列在界面上，就是「选了就报错」。
            "targets": ip.target_counts(source["fps"], model=m),
        })
    default_model = ip.default_model()
    spec = next((m for m in ip.models() if m.key == default_model), None)
    targets = ip.target_counts(source["fps"], model=spec) if spec else []
    # 默认目标：优先 60（最常见的诉求），没有就取最大的那个
    default_target = 60 if 60 in targets else (targets[-1] if targets else 0)

    devices = await urun.devices(ip.ENGINE_KEY) if ready and models else []

    notes: list[str] = []
    if models and source["frames"]:
        notes.append(
            f"这一段是 {source['frames']} 帧 / {source['fps']:g}fps"
            f"（{source['duration']:.1f} 秒）。补帧只提高帧的密度——"
            "画幅不变、时长不变、音轨也不是重新生成的。"
        )
        if spec:
            notes.append(f"{spec.label} → {default_target} 帧：" + ip.estimate_text(
                spec.key, source["width"], source["height"], source["frames"]))
        slow = ip.find_model("rife-v2.3")
        if spec and spec.custom and slow:
            notes.append(
                "补到任意帧数只有 v4 系（rife-v4 / rife-v4.6）做得到："
                "上游对别的模型直接拒绝这个参数。选到只做 2 倍的模型时，"
                "目标帧率会被锁成 2 倍并说明原因。"
            )
    if not ready and reason:
        notes.append(reason)

    return {
        "kind": "video",
        "source": {"id": asset.id, **source},
        "available": bool(ready and models),
        "reason": "" if (ready and models) else (reason or "这台机器上还没有可用的补帧模型"),
        "action": action,
        "actionRoute": where,
        "engineLabel": engine.label if engine else "RIFE 补帧",
        "models": models,
        "defaultModel": default_model,
        "defaultTarget": default_target,
        "devices": [
            {"id": d.id, "name": d.name, "usable": True,
             "recommended": d.recommended, "note": d.note}
            for d in devices
        ],
        "deviceAuto": True,
        "deviceNote": up.device_note(devices),
        "limits": {"maxOutFrames": ip.MAX_OUT_FRAMES, "maxFactor": ip.MAX_FACTOR},
        "notes": [n for n in notes if n],
    }


@router.post("/video", response_model=TaskOut)
async def interpolate_video(
    payload: InterpolateIn, db: AsyncSession = Depends(get_db)
) -> TaskOut:
    """派一条补帧任务。

    **同一时间只允许一条**（与超分共用一把锁）：两者都吃本机 GPU，
    一起跑只会互相拖慢、把显存顶满，还会同时往磁盘里写两套帧。
    """
    asset, source = await _load_video(db, payload.asset_id)

    installed = {e.key for e in le.engines() if engine_install.installed(e)}
    ready, reason, _action, _where = ip.check_ready(
        installed=installed, vulkan=engine_install.hardware().vulkan)
    if not ready:
        raise HTTPException(status_code=400, detail=reason)

    spec = ip.find_model(payload.model) or ip.find_model(ip.default_model())
    if spec is None:
        raise HTTPException(status_code=400, detail="这台机器上一个可用的补帧模型都没有")

    problem = ip.target_problem(source["frames"], source["fps"], payload.target, model=spec)
    if problem:
        raise HTTPException(status_code=400, detail=problem)

    busy = runner.upscale_busy()
    if busy:
        raise HTTPException(
            status_code=409,
            detail=f"已经有一条本机重活在跑了（任务 {busy}）。它很吃显卡，等它跑完再派下一条。",
        )

    params = {
        "op": "interpolate",
        "asset_id": asset.id,
        "model": spec.key,
        "target": int(payload.target),
        "gpu": int(payload.gpu),
        "srcFps": source["fps"],
    }
    task = Task(
        kind="interpolate",
        status="pending",
        model=f"rife:{spec.key}",
        prompt=asset.prompt or asset.original_name or "",
        params_json=json.dumps(params, ensure_ascii=False),
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    await runner.start_or_fail("interpolate", task.id)
    await db.refresh(task)
    return await _task_to_out(db, task)
