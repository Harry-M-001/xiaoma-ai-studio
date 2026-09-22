"""超分接口：看能怎么放大、放大一张图、派一条视频放大任务。

三条路线（本机两档 + 走自己的 ComfyUI）都从这里出去，界面不写死任何一条——
可选项全部由 `/api/upscale/options` 按**这台机器上真实存在的东西**算出来。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import Asset, ProviderService, Task
from app.schemas import AssetOut, TaskOut, UpscaleIn, asset_to_out
from app.routers.generation import _task_to_out
from app.services import (
    comfy_workflow_service,
    engine_install,
    ffmpeg_service,
    image_size,
    local_engines as le,
    storage,
    upscale as up,
    upscale_run as urun,
)
from app.services.runner import runner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/upscale", tags=["upscale"])


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


# ------------------------------------------------------------------ 选项


async def _comfy_route(db: AsyncSession, *, kind: str) -> dict:
    """「走我的 ComfyUI」这条路线。

    **放大这件事能推给 ComfyUI 的就推**（ESRGAN 放大节点在上游社区已经很成熟）。
    我们不复刻那套节点，只做两件事：认一认「接进来了没有」，以及把已经传上来的
    工作流列出来让用户挑一个跑。跑本身走既有的工作流链路（`Task(kind="workflow")`），
    一行新执行代码都不用写。
    """
    row = (
        await db.execute(
            select(ProviderService).where(
                ProviderService.kind == "comfyui", ProviderService.enabled.is_(True)
            )
        )
    ).scalars().first()
    base = {
        "key": up.ROUTE_COMFY,
        "label": up.COMFY_LABEL,
        "engine": "comfyui",
        "engineLabel": "ComfyUI 工作流",
        "kindLabel": "走本机 ComfyUI",
        "videoOk": False,
        "models": [],
        "defaultModel": "",
        "defaultScale": 2,
        "workflows": [],
    }
    if row is None:
        return {
            **base,
            "available": False,
            "reason": "还没接入 ComfyUI。在「模型服务」页填一个地址（默认 "
                      "http://127.0.0.1:8188）就能接进来。",
            "action": "去「模型服务」页接入",
            "actionRoute": "providers",
            "note": "",
        }
    if kind == "video":
        # 视频要走 ComfyUI 就得逐帧下发，那是另一件事（本机两档已经能做），不做承诺
        return {
            **base,
            "available": False,
            "reason": "走 ComfyUI 放大视频要逐帧下发，这一版没做；视频请用本机那两档。",
            "action": "",
            "actionRoute": "",
            "note": "",
        }

    rows = await comfy_workflow_service.list_workflows(db, row.id)
    # 只列**出图**的工作流：放大是「一张图进、一张图出」，出视频的那种选中也没用
    picked = [w for w in rows if (w.output_kind or "image") == "image"]
    workflows = [
        {
            "id": w.id,
            "name": w.name,
            "outputKind": w.output_kind,
            "nodeCount": len(json.loads(w.graph_json or "{}")),
        }
        for w in picked
    ]
    if not workflows:
        return {
            **base,
            "available": False,
            "reason": "ComfyUI 接进来了，但还没上传过出图的工作流。"
                      "在画布上把「上传工作流」做过一次，或按 docs/comfyui.md 里的"
                      "放大模板导入一份。",
            "action": "去画布上传放大工作流",
            "actionRoute": "canvas",
            "note": "",
        }
    return {
        **base,
        "available": True,
        "reason": "",
        "action": "",
        "actionRoute": "",
        "workflows": workflows,
        "note": "按这个工作流自带的默认参数跑；要调参数请在画布上用同一个工作流。",
    }


async def _source_info(db: AsyncSession, asset: Asset) -> dict:
    """这个素材的尺寸与帧数。

    **宽度高度以磁盘上的文件为准**（库里那两个字段是后来才开始记的，
    早期入库的图片是空的）——与 `preflight` 那条「读不到就只能闭嘴」的口径同源。
    """
    src = storage.abs_path(asset.filename)
    kind = "video" if asset.kind == "video" else "image"
    width = int(asset.width or 0)
    height = int(asset.height or 0)
    frames = 0
    fps = 0.0
    duration = int(asset.duration or 0)
    if kind == "video":
        if src.exists():
            info = await ffmpeg_service.probe(src)
            width = int(info.get("width") or width)
            height = int(info.get("height") or height)
            fps = float(info.get("fps") or 0)
            if info.get("duration"):
                duration = int(round(float(info["duration"])))
        frames = int(round(duration * fps)) if fps else 0
    elif (not width or not height) and src.exists():
        got = image_size.read_image_size_from_file(src)
        if got:
            width, height = got
    return {
        "kind": kind,
        "width": width,
        "height": height,
        "frames": frames,
        "fps": fps,
        "duration": duration,
        "size": int(asset.size or 0),
        "name": asset.prompt or asset.original_name or f"资产 {asset.id}",
    }


@router.get("/options")
async def options(asset_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    """「放大」弹窗要的一切。一次给全，省掉对话框里连着发好几个请求。"""
    asset = await db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    if asset.kind not in ("image", "video"):
        raise HTTPException(status_code=400, detail="只有图片和视频能放大")

    source = await _source_info(db, asset)
    kind = source["kind"]
    width, height = source["width"], source["height"]
    if width <= 0 or height <= 0:
        raise HTTPException(status_code=400, detail="读不出这个素材的尺寸，没法放大")

    installed = {e.key for e in le.engines() if engine_install.installed(e)}
    hw = engine_install.hardware()

    routes: list[dict] = []
    devices: list[up.Device] = []
    # 同一份 Model 对象既用来拼界面行、也用来算时长预估：预估是**按模型**算的
    # （同一个包里重网络与轻网络差 75 倍），所以不能只留一个字符串下来。
    spec_by_route: dict[str, dict[str, up.Model]] = {}
    for key in up.LOCAL_ENGINE_ORDER:
        state = up.local_route_state(key, installed=installed, vulkan=hw.vulkan)
        engine = le.by_key(key)
        models = up.models(key)
        # 视频只列对视频合适的模型：waifu2x 的模型是按图片调的，逐帧跑抖动会明显
        if kind == "video":
            video_models = [m for m in models if m.video_ok]
            models = video_models or models
        default = up.default_model(key, video=(kind == "video"))
        if models and not any(m.key == default for m in models):
            default = models[0].key
        model = next((m for m in models if m.key == default), None)
        route_key = up.local_route(key)
        spec_by_route[route_key] = {m.key: m for m in models}
        routes.append({
            "key": route_key,
            "label": engine.label.split("（")[0] if engine else key,
            "engine": key,
            "engineLabel": engine.label if engine else key,
            "kindLabel": "本机跑",
            "available": bool(state.available and models),
            "reason": state.reason,
            "action": state.action,
            "actionRoute": state.action_route,
            "videoOk": True,
            "models": [
                {
                    "key": m.key, "label": m.label, "note": m.note,
                    "scales": list(m.scales), "video": m.video_ok,
                }
                for m in models
            ],
            "defaultModel": default,
            "defaultScale": up.default_scale(model) if model else 2,
            "note": engine.note if engine else "",
            "workflows": [],
        })
        if state.available and models and not devices:
            devices = await urun.devices(key)

    routes.append(await _comfy_route(db, kind=kind))

    # 默认选第一条能用的本机路线；一条都用不了就选第一条（界面会把它显示成不可选）
    default_route = next((r for r in routes if r["available"] and r["engine"] != "comfyui"), None)
    if default_route is None:
        default_route = next((r for r in routes if r["available"]), routes[0])
    default_model = next(
        (m for m in default_route["models"] if m["key"] == default_route["defaultModel"]),
        default_route["models"][0] if default_route["models"] else None,
    )
    default_scale = default_route["defaultScale"] if default_model else 2

    notes: list[str] = []
    if kind == "video":
        frame_text = f"{source['frames']} 帧" if source["frames"] else "帧数读不出来"
        notes.append(
            f"视频是逐帧跑的：这一段 {frame_text}"
            f"（{source['duration']} 秒 / {source['fps']:g}fps）。"
            "本机跑不花钱，但比图片慢得多；下面的时长是按一台常见独显估的，"
            "实际以跑起来的进度为准。"
        )
        if source["frames"] > up.MAX_FRAMES:
            notes.append(up.frames_problem(source["frames"]))

    # **图片也要给预估**。理由是实测出来的：同一个包里「通用照片」比「视频逐帧」慢
    # 约 30 倍（960×544 一张：13 秒 vs 0.4 秒），不写出来用户只会觉得「放一张图怎么这么慢」，
    # 而且没有任何线索告诉他换一款就行。
    for r in routes:
        if not r["available"] or r["engine"] == "comfyui":
            continue
        if kind == "video" and not source["frames"]:
            continue
        m = (spec_by_route.get(r["key"]) or {}).get(r["defaultModel"])
        if m is None:
            continue
        frames = source["frames"] if kind == "video" else 1
        notes.append(
            f"{r['label']} · {m.label} {r['defaultScale']} 倍："
            + up.estimate_text(m, r["defaultScale"], width * height, frames)
        )

    temp = up.temp_bytes(source["frames"] or 1, width, height, default_scale)
    if kind == "video" and temp > up.TEMP_WARN_BYTES:
        notes.append(
            "跑的时候会在临时目录里同时留着原帧与放大帧，大约要 "
            f"{temp / 1024 ** 3:.1f} GB 空闲磁盘；跑完会自动删掉。"
        )

    return {
        "kind": kind,
        "source": {"id": asset.id, **source},
        "routes": routes,
        "devices": [
            {
                "id": d.id,
                "name": d.name,
                "usable": True,
                "recommended": d.recommended,
                "note": d.note,
            }
            for d in devices
        ],
        "deviceAuto": True,
        "deviceNote": up.device_note(devices),
        "limits": {
            "maxFrames": up.MAX_FRAMES,
            "maxOutputPixels": up.MAX_OUTPUT_PIXELS,
            "maxScale": 4,
        },
        "preview": {
            "width": width * default_scale,
            "height": height * default_scale,
        },
        "notes": [n for n in notes if n],
        "tempBytes": temp,
    }


# ------------------------------------------------------------------ 执行


async def _resolve_local(payload: UpscaleIn) -> tuple[str, up.Model]:
    """把请求里的路线校验成一个能跑的组合：`(引擎 key, 模型)`。

    模型与倍数都在这里定死：**不许出现「界面给了这个组合、后端却按另一套跑」**。
    """
    engine_key = up.engine_of(payload.route)
    if not engine_key:
        raise HTTPException(status_code=400, detail="不认识的放大路线")
    installed = {e.key for e in le.engines() if engine_install.installed(e)}
    state = up.local_route_state(engine_key, installed=installed,
                                 vulkan=engine_install.hardware().vulkan)
    if not state.available:
        raise HTTPException(status_code=400, detail=state.reason or "这条路线现在用不了")
    model = up.find_model(engine_key, payload.model) or up.find_model(
        engine_key, up.default_model(engine_key))
    if model is None:
        raise HTTPException(status_code=400, detail="这个引擎上一个可用的模型都没有")
    if payload.scale not in model.scales:
        raise HTTPException(
            status_code=400,
            detail=f"「{model.label}」只支持 {'、'.join(str(s) for s in model.scales)} 倍",
        )
    return engine_key, model


async def _load_source(db: AsyncSession, payload: UpscaleIn) -> tuple[Asset, Path, dict]:
    asset = await db.get(Asset, payload.asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    if asset.kind not in ("image", "video"):
        raise HTTPException(status_code=400, detail="只有图片和视频能放大")
    src = storage.abs_path(asset.filename)
    if not src.exists():
        raise HTTPException(status_code=400, detail="这个资产的原始文件不在磁盘上了")
    info = await _source_info(db, asset)
    problem = up.size_problem(info["width"], info["height"], payload.scale)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    return asset, src, info


def _made_name(asset: Asset, scale: int, ext: str) -> str:
    base = (asset.prompt or asset.original_name or f"asset-{asset.id}").rsplit(".", 1)[0]
    return f"{base}_放大{scale}倍.{ext}"


@router.post("/image", response_model=AssetOut)
async def upscale_image(
    payload: UpscaleIn, db: AsyncSession = Depends(get_db)
) -> AssetOut:
    """同步放大一张图，产物落资产库（原件不动）。

    图片是秒级的，所以走同步——**不占生成并发名额**，也不建任务：
    为一件三秒钟的事在任务中心里留一条记录，只会把任务列表弄脏。
    """
    asset, src, info = await _load_source(db, payload)
    if info["kind"] != "image":
        raise HTTPException(status_code=400, detail="视频请走 /api/upscale/video（它会后台跑）")
    engine_key, model = await _resolve_local(payload)

    try:
        path, width, height = await urun.run_image(
            engine_key, src=src, model=model.key, scale=payload.scale,
            gpu=payload.gpu, tta=payload.tta,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("放大图片失败：asset=%s", payload.asset_id)
        raise HTTPException(status_code=500, detail=str(e) or "放大失败") from e

    rel = urun.saved_rel(path)
    made = Asset(
        kind="image",
        filename=rel,
        original_name=_made_name(asset, payload.scale, "png"),
        content_type="image/png",
        size=path.stat().st_size,
        source="generated",
        width=width,
        height=height,
    )
    db.add(made)
    await db.commit()
    await db.refresh(made)
    return asset_to_out(made)


@router.post("/video", response_model=TaskOut)
async def upscale_video(
    payload: UpscaleIn, db: AsyncSession = Depends(get_db)
) -> TaskOut:
    """派一条视频放大任务，后台逐帧跑。

    **同一时间只允许一条**（409）：它吃本机 GPU，两条一起跑只会互相拖慢，
    而且会同时往磁盘里写两套帧。
    """
    asset, _src, info = await _load_source(db, payload)
    if info["kind"] != "video":
        raise HTTPException(status_code=400, detail="图片请走 /api/upscale/image（它几秒就完）")
    engine_key, model = await _resolve_local(payload)

    busy = runner.upscale_busy()
    if busy:
        raise HTTPException(
            status_code=409,
            detail=f"已经有一条超分在跑了（任务 {busy}）。它很吃显卡，等它跑完再派下一条。",
        )
    frames = up.frames_problem(info["frames"])
    if info["frames"] and frames:
        raise HTTPException(status_code=400, detail=frames)

    params = {
        "asset_id": asset.id,
        "route": up.local_route(engine_key),
        "model": model.key,
        "scale": int(payload.scale),
        "gpu": int(payload.gpu),
    }
    task = Task(
        kind="upscale",
        status="pending",
        model=f"{engine_key}:{model.key}",
        prompt=asset.prompt or asset.original_name or "",
        params_json=json.dumps(params, ensure_ascii=False),
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    await runner.start_or_fail("upscale", task.id)
    await db.refresh(task)
    return await _task_to_out(db, task)


@router.post("/comfy", response_model=TaskOut)
async def upscale_comfy(
    payload: UpscaleIn, db: AsyncSession = Depends(get_db)
) -> TaskOut:
    """走用户自己的 ComfyUI 跑一条已上传的放大工作流。

    **一行执行代码都没新写**：建一条 `Task(kind="workflow")` 交给既有的工作流链路，
    源图作为参考图上传给 ComfyUI，产物由那条链自己登记进资产库。
    """
    asset, _src, info = await _load_source(db, payload)
    if info["kind"] != "image":
        raise HTTPException(status_code=400, detail="走 ComfyUI 这一路只做图片，视频请用本机那两档")
    if payload.workflow_id <= 0:
        raise HTTPException(status_code=400, detail="请先选一个放大工作流")

    wf = await comfy_workflow_service.list_workflows(db)
    picked = next((w for w in wf if w.id == payload.workflow_id), None)
    if picked is None:
        raise HTTPException(status_code=400, detail="这个工作流不存在，可能已被删除")

    params = {
        "workflow_id": picked.id,
        "param_values": payload.param_values or {},
        "ref_asset_ids": [asset.id],
        "upscale_note": f"放大 {payload.scale} 倍（工作流 {picked.name}）",
    }
    task = Task(
        kind="workflow",
        status="pending",
        service_id=picked.provider_id,
        model=f"comfy:{picked.id}",
        prompt=asset.prompt or asset.original_name or "",
        params_json=json.dumps(params, ensure_ascii=False),
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    await runner.start_or_fail("workflow", task.id)
    await db.refresh(task)
    return await _task_to_out(db, task)
