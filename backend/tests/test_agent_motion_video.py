"""Agent data-to-video rendering and evidence scoped to one canvas run."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.database import Base
from app.models import AgentSession, Asset, Task
from app.services import agent_tools, canvas_runner


async def with_database(case):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await case(db)
    finally:
        await engine.dispose()


def test_wait_reports_only_the_selected_runs_tasks_and_assets(monkeypatch):
    async def case(db):
        run = "selected"
        tasks = [
            Task(kind="image", status="completed", model="fake", prompt="", canvas_project_id=1, run_id=run),
            Task(kind="video", status="failed", model="fake", prompt="", canvas_project_id=1, run_id=run),
            Task(kind="image", status="completed", model="fake", prompt="", canvas_project_id=1, run_id="old"),
            Task(kind="image", status="completed", model="fake", prompt="", canvas_project_id=2, run_id=run),
        ]
        db.add_all(tasks)
        await db.flush()
        for task in tasks:
            if task.status == "completed":
                db.add(Asset(kind="image", filename=f"{task.id}.png", task_id=task.id))
        await db.commit()
        monkeypatch.setattr(canvas_runner, "_ACTIVE_RUNS", {})
        out = await agent_tools.execute(
            agent_tools.ToolContext(db, AgentSession(canvas_project_id=1)),
            "wait_for_tasks", {"run_id": run},
        )
        data = out["data"]
        assert data["done"] and data["failed_count"] == 1
        assert data["task_count"] == 2 and data["asset_count"] == 1
        assert {t["id"] for t in data["tasks"]} == {tasks[0].id, tasks[1].id}
        assert data["assets"][0]["task_id"] == tasks[0].id

    asyncio.run(with_database(case))


def test_showcase_video_returns_verified_stereo_and_registers_asset(monkeypatch, tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("requires FFmpeg and FFprobe")
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))

    async def case(db):
        cancel = asyncio.Event()
        ctx = agent_tools.ToolContext(db, AgentSession(), cancel=cancel)
        invalid = await agent_tools.execute(ctx, "render_showcase_video", {
            "template": "gel_pen", "seconds": 12,
        })
        assert not invalid["ok"] and "seconds" in invalid["error"]
        cancel.set()
        cancelled = await agent_tools.execute(ctx, "render_showcase_video", {"template": "gel_pen"})
        assert not cancelled["ok"] and "取消" in cancelled["error"]
        cancel.clear()
        result = await agent_tools.execute(ctx, "render_showcase_video", {
            "template": "gel_pen", "name": "书写样片", "title": "把灵感写下来",
        })
        assert result["ok"], result
        data = result["data"]
        assert (data["width"], data["height"], data["seconds"], data["fps"]) == (1280, 720, 10, 24)
        assert (data["codec"], data["audio_codec"], data["audio_channels"]) == ("h264", "aac", 2)
        assert data["title"] == "把灵感写下来"
        asset = await db.get(Asset, data["id"])
        assert (settings.storage_dir / asset.filename).is_file()
        assert asset.duration == 10 and asset.name == "书写样片"

    asyncio.run(with_database(case))


def test_motion_video_renders_two_chinese_scenes_and_registers_asset(monkeypatch, tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        pytest.skip("requires FFmpeg and FFprobe")
    filters = subprocess.check_output([ffmpeg, "-hide_banner", "-filters"], text=True, errors="replace")
    if "drawtext" not in filters:
        pytest.skip("requires FFmpeg drawtext")
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))

    async def case(db):
        ctx = agent_tools.ToolContext(db, AgentSession())
        invalid = await agent_tools.execute(ctx, "render_motion_video", {
            "scenes": [{"title": "测试", "background": "#123456,drawtext"}],
        })
        assert not invalid["ok"] and "background" in invalid["error"]
        out = await agent_tools.execute(ctx, "render_motion_video", {
            "ratio": "9:16",
            "scenes": [
                {"title": "雨夜便利店：100% [片头]", "seconds": 1},
                {"title": "橘猫的夜班", "subtitle": "第二幕", "seconds": 1, "motion": "fade"},
            ],
        })
        assert out["ok"], out
        data = out["data"]
        assert (data["width"], data["height"], data["fps"], data["seconds"]) == (720, 1280, 30, 2)
        asset = await db.get(Asset, data["id"])
        video = settings.storage_dir / asset.filename
        assert video.is_file() and video.stat().st_size == asset.size > 0

        invalid = await agent_tools.execute(ctx, "render_motion_video", {
            "scenes": [{"title": "价格", "layout": "offer", "price": "¥19.90"}],
        })
        assert not invalid["ok"] and "cta" in invalid["error"]
        rich = await agent_tools.execute(ctx, "render_motion_video", {
            "scenes": [
                {"title": "数据", "layout": "bars", "seconds": 1,
                 "bars": [{"label": "Q1", "value": 24}, {"label": "Q2", "value": 60}]},
                {"title": "研磨", "layout": "step", "seconds": 1, "step": 2},
                {"title": "周末咖啡", "layout": "offer", "seconds": 1,
                 "price": "¥19.90", "cta": "立即领取"},
                {"title": "直播", "layout": "countdown", "seconds": 2},
            ],
        })
        assert rich["ok"], rich
        rendered = rich["data"]
        assert (rendered["seconds"], rendered["fps"], rendered["codec"]) == (5, 30, "h264")
        chart, step, offer, countdown = rendered["timeline"]
        assert [bar["value"] for bar in chart["bars"]] == [24, 60]
        assert step["step"] == 2
        assert (offer["price"], offer["cta"]) == ("¥19.90", "立即领取")
        assert countdown["countdown"] == {"from": 2, "to": 1, "interval_seconds": 1}

    asyncio.run(with_database(case))
