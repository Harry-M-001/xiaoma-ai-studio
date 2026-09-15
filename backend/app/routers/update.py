"""在线更新 API：版本信息 / 检查更新 / 一键更新。

更新是「改用户自己机器」的操作，所以：
- 需要访问口令（设置了 APP_PASSWORD 时才有约束，纯本机使用默认放行）；
- 真正执行前的安全判断都在 services/update_service 里（工作区必须干净、
  只在 git 克隆安装下执行、Docker 与压缩包安装明确拒绝并给出替代做法）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.deps import require_auth
from app.services import update_service

router = APIRouter(prefix="/api/update", tags=["update"], dependencies=[Depends(require_auth)])


@router.get("/status")
async def update_status() -> dict:
    """当前版本 + 最新版本（10 分钟缓存，避免频繁打上游 API）。"""
    return await update_service.check_update(force=False)


@router.post("/check")
async def check_now() -> dict:
    """强制重新检查（忽略缓存）。"""
    return await update_service.check_update(force=True)


@router.post("/run")
async def run_now() -> dict:
    """一键更新：git pull → 依赖变了才装依赖 → 前端变了才重建。"""
    return await update_service.run_update()
