"""FFmpeg 并发闸门：不管同时来多少请求，同时跑的外部进程不超过上限。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.services import ffmpeg_service  # noqa: E402


class _FakeProc:
    def __init__(self, tracker: dict[str, int]) -> None:
        self.returncode = 0
        self._tracker = tracker

    async def communicate(self):
        await asyncio.sleep(0.05)
        self._tracker["active"] -= 1
        return b"", b""

    def kill(self) -> None:
        self.returncode = -9


def test_only_two_ffmpeg_processes_run_at_once(monkeypatch):
    tracker = {"active": 0, "peak": 0}
    monkeypatch.setattr(ffmpeg_service, "_FFMPEG_LIMIT", 2)
    monkeypatch.setattr(ffmpeg_service, "_FFMPEG_SEM", None)
    monkeypatch.setattr(ffmpeg_service, "_FFMPEG_LOOP", None)

    async def fake_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        tracker["active"] += 1
        tracker["peak"] = max(tracker["peak"], tracker["active"])
        return _FakeProc(tracker)

    monkeypatch.setattr(ffmpeg_service.asyncio, "create_subprocess_exec", fake_exec)

    async def scenario():
        return await asyncio.gather(
            *(ffmpeg_service._run(["ffmpeg", "-version"], 5) for _ in range(6))
        )

    results = asyncio.run(scenario())
    assert all(ok for ok, _ in results)
    assert tracker["peak"] == 2, f"同时跑了 {tracker['peak']} 个 ffmpeg，闸门没生效"
