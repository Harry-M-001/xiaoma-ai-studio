"""媒体授权：`<img>` / `<video>` 带不了 Authorization 头，靠登录下发的 Cookie 过关。

为什么单测这一块：媒体原片、缩略图是浏览器**直接**发起的请求，不走前端的 fetch，
所以改不改 URL、加不加请求头都由不得我们。一旦这里漏了，症状是「界面里图片全裂、
视频转圈」，而接口测试全绿——口令一开就踩。反过来，校验做重了（比如只认 Bearer）
会让拖动进度条失效，因为 Range 请求也得过同一道门。

真跑 ffmpeg 的部分不在这里：缩略图缩不出来会退回原图，所以只要校验与 Range 对，链路就通。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from starlette.testclient import TestClient

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.deps import require_auth  # noqa: E402
from app.main import AuthStaticFiles  # noqa: E402
from app.routers import system  # noqa: E402
from app.security import AUTH_COOKIE, issue_auth_token  # noqa: E402


def _app(tmp_path: Path) -> FastAPI:
    """最小的真身：同一个登录接口 + 同一个媒体守卫 + 一条挂了鉴权的缩略图路由。"""
    storage = tmp_path / "storage"
    storage.mkdir(parents=True)
    (storage / "clip.mp4").write_bytes(bytes(range(10)))

    app = FastAPI()
    app.include_router(system.router)
    app.mount("/media", AuthStaticFiles(directory=storage), name="media")

    @app.get("/media-thumb/{rel:path}", dependencies=[Depends(require_auth)])
    async def thumb(rel: str) -> dict:  # noqa: ARG001
        return {"ok": True}

    return app


@pytest.fixture()
def client(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        yield c


def _login(client: TestClient, password: str):
    return client.post("/api/auth/login", json={"password": password})


# ------------------------------------------------------------------ 登录下发 Cookie


def test_login_sets_httponly_cookie(client, monkeypatch):
    monkeypatch.setattr(settings, "APP_PASSWORD", "pw")
    resp = _login(client, "pw")
    assert resp.status_code == 200, resp.text
    assert resp.json()["token"], "口令登录仍然要回 token（接口调用走 Bearer）"
    cookie = resp.headers.get("set-cookie", "")
    assert f"{AUTH_COOKIE}=" in cookie, cookie
    assert "httponly" in cookie.lower(), f"媒体 Cookie 必须是 HttpOnly：{cookie}"


def test_wrong_password_gets_no_cookie(client, monkeypatch):
    monkeypatch.setattr(settings, "APP_PASSWORD", "pw")
    resp = _login(client, "bad")
    assert resp.status_code == 401
    assert AUTH_COOKIE not in resp.cookies


def test_login_with_password_off_clears_the_cookie(client, monkeypatch):
    """关掉口令后旧 Cookie 要作废：否则关了口令还留着一把 30 天的钥匙。"""
    monkeypatch.setattr(settings, "APP_PASSWORD", "")
    client.cookies.set(AUTH_COOKIE, "stale")
    resp = _login(client, "")
    assert resp.status_code == 200
    assert resp.json()["token"] == ""
    cookie = resp.headers.get("set-cookie", "").lower()
    assert f"{AUTH_COOKIE}=" in cookie and "max-age=0" in cookie, cookie


# ------------------------------------------------------------------ 媒体授权


def test_media_is_open_when_password_is_off(client, monkeypatch):
    monkeypatch.setattr(settings, "APP_PASSWORD", "")
    assert client.get("/media/clip.mp4").status_code == 200


def test_media_is_rejected_when_password_is_on_and_nobody_logged_in(client, monkeypatch):
    monkeypatch.setattr(settings, "APP_PASSWORD", "pw")
    resp = client.get("/media/clip.mp4")
    assert resp.status_code == 401, resp.text


def test_media_accepts_the_login_cookie(client, monkeypatch):
    monkeypatch.setattr(settings, "APP_PASSWORD", "pw")
    assert _login(client, "pw").status_code == 200
    resp = client.get("/media/clip.mp4")
    assert resp.status_code == 200, resp.text


def test_media_accepts_a_token_in_the_query_string(client, monkeypatch):
    """复制直链 / 右键另存为时不一定带 Cookie，`?access_token=` 这条老路不能断。"""
    monkeypatch.setattr(settings, "APP_PASSWORD", "pw")
    token = issue_auth_token()
    assert client.get(f"/media/clip.mp4?access_token={token}").status_code == 200


def test_media_rejects_a_forged_token(client, monkeypatch):
    monkeypatch.setattr(settings, "APP_PASSWORD", "pw")
    assert client.get("/media/clip.mp4?access_token=fake.token").status_code == 401
    client.cookies.set(AUTH_COOKIE, "fake.token")
    assert client.get("/media/clip.mp4").status_code == 401


def test_range_requests_still_get_206_after_auth(client, monkeypatch):
    """校验不能把 Range 吃掉：视频拖动进度条靠它，丢了就是「能播但拖不动」。"""
    monkeypatch.setattr(settings, "APP_PASSWORD", "pw")
    assert _login(client, "pw").status_code == 200
    resp = client.get("/media/clip.mp4", headers={"Range": "bytes=0-3"})
    assert resp.status_code == 206, resp.text
    assert resp.content == bytes(range(4))
    assert resp.headers["content-range"] == "bytes 0-3/10"
    assert resp.headers.get("accept-ranges") == "bytes"


# ------------------------------------------------------------------ 缩略图


def test_thumbnail_is_guarded_too(client, monkeypatch):
    """缩略图同样泄露画面，且不走 `/media` 那道门，必须单独挂鉴权。"""
    monkeypatch.setattr(settings, "APP_PASSWORD", "pw")
    assert client.get("/media-thumb/clip.mp4").status_code == 401
    assert _login(client, "pw").status_code == 200
    assert client.get("/media-thumb/clip.mp4").status_code == 200


# ------------------------------------------------------------------ 线缆检查


def test_the_real_app_mounts_the_guarded_media_directory():
    """上面测的是组件；这里钉住 `main.py` 真的用了它，免得换个 mount 就悄悄失守。"""
    from starlette.routing import Mount

    from app.main import app, media_thumb

    media_mounts = [
        r for r in app.routes if isinstance(r, Mount) and getattr(r, "path", "") == "/media"
    ]
    assert media_mounts, "没找到 /media 挂载点"
    assert isinstance(media_mounts[0].app, AuthStaticFiles), "/media 没走带鉴权的 StaticFiles"

    thumb_route = next(
        r for r in app.routes if getattr(r, "endpoint", None) is media_thumb
    )
    assert thumb_route.dependant.dependencies, "缩略图接口没挂 require_auth"
