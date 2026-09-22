"""本机引擎：清单、这台机器的体检、下载（续传 + 校验）、删除。

这一层是批次 9 的地基（`#40`），后面几档能力（本机配音、超分、补帧、去字幕高质量档）
都从这一页往下接。三条口径：

1. **一次给全**。清单、体积、sha256、授权、硬件档位建议、装没装、下到哪儿了，
   一个接口全给出去——前端不另抄一份表（抄一份就会出现「界面能点、后端不认」）。
2. **能不能装要在点之前说清**。这台机器跑不了的（比如没有 Vulkan 却要装 ncnn-vulkan 那一档），
   按钮就是灰的，并写出为什么；而不是等他点了、下完几百 MB 才报错。
3. **下载是后台的事**。几百 MB 的下载不能让一个请求挂着，所以这里只起任务、
   状态由前端轮询拿（与任务中心同一条口径）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.services import engine_install as ei
from app.services import local_engines as le

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/engines", tags=["engines"])


def _hw_dict(hw: le.Hardware) -> dict:
    return {
        "gpuNames": list(hw.gpu_names),
        "gpuText": hw.gpu_text,
        "hasGpu": hw.has_gpu,
        "nvidia": hw.nvidia,
        "vulkan": hw.vulkan,
        "vulkanDevice": hw.vulkan_device,
        "cores": hw.cores,
        "ramGb": hw.ram_gb,
    }


async def _payload(db: AsyncSession, *, force_hw: bool = False) -> dict:
    hw = ei.hardware(force=force_hw)
    rows = ei.rows()
    installed_count = sum(1 for r in rows if r["installed"])
    return {
        "hardware": _hw_dict(hw),
        "headline": le.headline(hw, installed_count, len(rows)),
        "engines": rows,
        "job": ei.job_snapshot(),
        "phaseLabels": ei.PHASE_LABELS,
        "installedCount": installed_count,
        "installedServices": await ei.installed_services(db),
        # 本机配音那条线：引擎装好没有 / 接成模型服务没有 / 有哪些音色
        "localTts": await ei.local_tts_status(db),
        "totalSizeText": le.human_size(le.total_size()),
    }


@router.get("")
async def list_engines(db: AsyncSession = Depends(get_db)) -> dict:
    """本机引擎页要的全部东西（不下载、不校验哈希，只读盘）。"""
    return await _payload(db)


@router.post("/hardware/refresh")
async def refresh_hardware(db: AsyncSession = Depends(get_db)) -> dict:
    """用户点了「重新检测」（刚插上显卡 / 刚装完驱动时用）。"""
    return await _payload(db, force_hw=True)


@router.post("/{key}/download")
async def download(key: str) -> dict:
    """开始下载。**同一时间只允许一条**，已经在下了会明确拒绝。"""
    try:
        job = await ei.start(key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"job": job, "phaseLabels": ei.PHASE_LABELS}


@router.post("/{key}/cancel")
async def cancel(key: str) -> dict:
    """停掉这一条。已经下到的部分留着，下次接着下。"""
    try:
        job = ei.cancel(key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"job": job}


@router.post("/{key}/remove")
async def remove(key: str) -> dict:
    """删掉装好的那一份与压缩包，把磁盘空间收回来。"""
    try:
        return await ei.remove(key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/{key}/verify")
async def verify(key: str) -> dict:
    """把已经下好的压缩包重新算一遍 sha256（怀疑文件坏了时用）。"""
    try:
        return await ei.verify(key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ---- 本机配音：把装好的引擎接成一条模型服务 ----


@router.get("/local-tts")
async def local_tts(db: AsyncSession = Depends(get_db)) -> dict:
    """本机配音这条线的状态（引擎装好没有、接入没有、有哪些音色）。"""
    return await ei.local_tts_status(db)


@router.post("/local-tts/connect")
async def connect_local_tts(db: AsyncSession = Depends(get_db)) -> dict:
    """一键接入：接完之后配音页、样片旁白、画布逐镜对白都能选「本机跑」。"""
    try:
        return await ei.connect_local_tts(db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/local-tts/disconnect")
async def disconnect_local_tts(db: AsyncSession = Depends(get_db)) -> dict:
    """停用（不删：已经生成过的配音资产还挂在它名下）。"""
    return await ei.disconnect_local_tts(db)
