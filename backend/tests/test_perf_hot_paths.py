"""首屏热点：Ollama 探测不再阻塞、静态资源有压缩与长缓存。"""

from __future__ import annotations

import asyncio
import glob
import sys
import time
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.main import app  # noqa: E402
from app.services import ollama_service  # noqa: E402


def test_setup_status_ollama_probe_never_blocks(monkeypatch):
    """没有缓存时立刻回「检测中」，不等 4 秒的端口探测。"""
    monkeypatch.setattr(ollama_service, "_cache", None)
    monkeypatch.setattr(ollama_service, "_probing", False)
    scheduled: list[str] = []
    monkeypatch.setattr(ollama_service, "_schedule_probe", lambda host: scheduled.append(host))

    started = time.monotonic()
    info = asyncio.run(ollama_service.detect(cached_only=True))
    elapsed = time.monotonic() - started

    assert info["error"] == "detecting", info
    assert elapsed < 0.2, elapsed
    assert scheduled, "应当顺手在后台补一次探测"


def test_stale_ollama_result_is_served_immediately(monkeypatch):
    """缓存过期也先给旧值（stale-while-revalidate），不要让首屏等探测。"""
    stale = {"running": True, "baseUrl": ollama_service.DEFAULT_HOST, "models": ["qwen3:4b"], "error": ""}
    monkeypatch.setattr(
        ollama_service, "_cache",
        (time.monotonic() - 9999, ollama_service.DEFAULT_HOST, stale),
    )
    monkeypatch.setattr(ollama_service, "_probing", False)
    calls: list[str] = []
    monkeypatch.setattr(ollama_service, "_schedule_probe", lambda host: calls.append(host))

    started = time.monotonic()
    info = asyncio.run(ollama_service.detect(cached_only=True))
    elapsed = time.monotonic() - started

    assert info["running"] is True and info["models"] == ["qwen3:4b"], info
    assert elapsed < 0.2, elapsed
    assert calls, "过期缓存应当触发后台刷新"


def test_hashed_assets_are_gzipped_and_cached_forever():
    assets = glob.glob(str(BACKEND.parent / "frontend" / "webroot" / "assets" / "index-*.js"))
    if not assets:
        return  # 未构建前端时跳过（CI 里先 npm run build）
    name = Path(assets[0]).name

    async def case() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/assets/{name}", headers={"Accept-Encoding": "gzip"})
            assert resp.status_code == 200, resp.text
            assert resp.headers.get("content-encoding") == "gzip", dict(resp.headers)
            assert "immutable" in (resp.headers.get("cache-control") or ""), dict(resp.headers)
            # `.js` 的 MIME 由标准库 mimetypes 给：Linux 上多为 text/javascript，
            # Windows 注册表会给 application/javascript，两者都是对的（浏览器都认）。
            ctype = resp.headers.get("content-type", "")
            assert ctype.startswith(("text/javascript", "application/javascript")), dict(resp.headers)

    asyncio.run(case())
