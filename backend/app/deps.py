"""公共依赖：数据库会话 + 可选访问口令校验。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import SessionLocal
from app.security import AUTH_COOKIE, verify_auth_token


async def get_db() -> AsyncIterator[AsyncSession]:
    """请求级数据库会话。"""
    async with SessionLocal() as db:
        yield db


def token_candidates(request: Request) -> tuple[str, ...]:
    """请求里可能携带的令牌，按「谁最可信」排在前面。

    三种来源都要认：
    1. Authorization: Bearer <token>（前端 fetch / SSE）
    2. ?access_token=<token>（浏览器直链下载，加不了请求头）
    3. HttpOnly Cookie（`<img>` / `<video>` / `<audio>` 由浏览器自己发起，
       连 query 都改不了，只能靠登录时种下的 Cookie）
    """
    auth = request.headers.get("authorization", "")
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    return (
        bearer,
        request.query_params.get("access_token") or "",
        request.cookies.get(AUTH_COOKIE) or "",
    )


def has_valid_token(request: Request) -> bool:
    """未设置 APP_PASSWORD 时全放行；设置后任一来源的令牌有效即可。"""
    if not settings.APP_PASSWORD:
        return True
    return any(token and verify_auth_token(token) for token in token_candidates(request))


async def require_auth(request: Request) -> None:
    if not has_valid_token(request):
        raise HTTPException(status_code=401, detail="未授权，请先输入访问口令")
