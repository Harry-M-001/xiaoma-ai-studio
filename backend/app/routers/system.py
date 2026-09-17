"""系统接口：健康检查、应用信息、访问口令登录。"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException

from app import APP_NAME, __version__
from app.config import settings
from app.deps import require_auth
from app.schemas import AppInfo, LoginIn, LoginOut
from app.security import issue_auth_token

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health() -> dict:
    """存活探针。顺带带上队列概况：出问题时第一眼要看的就是「在跑几个、排了几个」。"""
    from app.services.runner import runner

    return {"status": "ok", "queue": runner.queue_stats()}


@router.get("/app/info", response_model=AppInfo)
async def app_info() -> AppInfo:
    """站点信息。名称取自配置表，可在「系统设置」中修改。"""
    from app.services.config_center_service import runtime_value

    return AppInfo(
        name=str(runtime_value("app.name", APP_NAME) or APP_NAME),
        version=__version__,
        auth_required=bool(settings.APP_PASSWORD),
    )


@router.post("/auth/login", response_model=LoginOut)
async def login(payload: LoginIn) -> LoginOut:
    if not settings.APP_PASSWORD:
        return LoginOut(token="")
    if not hmac.compare_digest(payload.password, settings.APP_PASSWORD):
        raise HTTPException(status_code=401, detail="口令不正确")
    return LoginOut(token=issue_auth_token())


@router.get("/auth/check", dependencies=[Depends(require_auth)])
async def auth_check() -> dict:
    return {"ok": True}
