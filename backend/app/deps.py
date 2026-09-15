"""公共依赖：数据库会话 + 可选访问口令校验。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import SessionLocal
from app.security import verify_auth_token

_bearer = HTTPBearer(auto_error=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """请求级数据库会话。"""
    async with SessionLocal() as db:
        yield db


async def require_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    access_token: str | None = Query(default=None),
) -> None:
    """未设置 APP_PASSWORD 时全放行；设置后必须携带有效令牌。

    令牌支持两种传递方式：
    1. Authorization: Bearer <token>（接口调用）
    2. ?access_token=<token>（浏览器直接下载文件等无法加头的场景）
    """
    if not settings.APP_PASSWORD:
        return
    token = creds.credentials if creds is not None else access_token
    if not token or not verify_auth_token(token):
        raise HTTPException(status_code=401, detail="未授权，请先输入访问口令")
