"""导演台上传：大文件要边收边写，超限立刻停，不留半个文件。"""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.routers import director  # noqa: E402


def test_oversized_video_stops_midstream_and_leaves_no_partial_file(monkeypatch):
    """上传 4 MB、上限 1 MB：读完 2 MB 左右就该停，storage 里连 `.part` 都不能剩。"""

    async def fake_available():
        return True, "test-ffmpeg"

    monkeypatch.setattr(director.ffmpeg_service, "ffmpeg_available", fake_available)
    monkeypatch.setattr(director, "_video_upload_limit", lambda: 1)

    payload = io.BytesIO(b"x" * (4 * 1024 * 1024))
    upload = UploadFile(
        file=payload,
        filename="big.mp4",
        headers=Headers({"content-type": "video/mp4"}),
    )
    assert upload.size is None, "这个用例要覆盖流式路径，不能被前置大小拦截"

    with pytest.raises(HTTPException) as err:
        asyncio.run(director.import_video(upload, None))
    assert err.value.status_code == 413
    # 超限后仍在读第二块（1 MB）就停，说明不是先整段读进内存再判断
    assert payload.tell() <= 2 * 1024 * 1024 + 1024

    storage_root = settings.storage_dir
    left = [p for p in storage_root.rglob("*") if p.is_file()]
    assert left == [], f"超限上传留下了文件：{left}"
