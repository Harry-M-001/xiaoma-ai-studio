"""Agent 的本地文件上传：编辑区拖进来的文件先落资产库，再随消息交给模型。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.database import Base  # noqa: E402
from app.deps import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import agent_runner, storage  # noqa: E402


def test_upload_registers_any_file_and_attachment_block_reads_text(monkeypatch):
    import asyncio

    async def case() -> None:
        tmp = Path(tempfile.mkdtemp())
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        def fake_save(data: bytes, content_type: str, preferred_ext: str | None = None) -> str:
            ext = preferred_ext or "bin"
            rel = f"2026-11/upload-{len(data)}.{ext}"
            path = tmp / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return rel

        monkeypatch.setattr(storage, "save_bytes", fake_save)
        monkeypatch.setattr(storage, "abs_path", lambda rel: tmp / rel)

        async def _get_db():
            async with maker() as db:
                yield db

        app.dependency_overrides[get_db] = _get_db
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        try:
            resp = await client.post(
                "/api/agent/files",
                files={"file": ("需求.md", "# 标题\n正文内容".encode(), "text/markdown")},
            )
            assert resp.status_code == 200, resp.text
            asset = resp.json()
            assert asset["kind"] == "document", asset
            assert asset["original_name"] == "需求.md", asset

            async with maker() as db:
                block = await agent_runner.describe_attachments(db, [asset["id"]])
            assert "需求.md" in block
            assert "正文内容" in block, block
            assert f"#{asset['id']}" in block
        finally:
            await client.aclose()
            app.dependency_overrides.pop(get_db, None)
            await engine.dispose()

    asyncio.run(case())


def test_empty_upload_is_rejected():
    import asyncio

    async def case() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        async def _get_db():
            async with maker() as db:
                yield db

        app.dependency_overrides[get_db] = _get_db
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        try:
            resp = await client.post(
                "/api/agent/files", files={"file": ("空.txt", b"", "text/plain")}
            )
            assert resp.status_code == 400, resp.text
        finally:
            await client.aclose()
            app.dependency_overrides.pop(get_db, None)
            await engine.dispose()

    asyncio.run(case())
