"""镜头工作台：稳定 shot_id、按镜补跑、人工审核。

首版只补三件事（路线图定义）：稳定镜头 ID、审核状态、按镜补跑。
这里的验收对准「改第 7 镜」那条：补跑一镜只派这一镜的任务，其余镜头不重做、
已选结果不动；老项目第一次打开时按顺序补号，不改写分镜内容。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import Asset, Project, ProjectShot, Task  # noqa: E402
from app.services import canvas_runner, provider_store  # noqa: E402

logging.getLogger("app.services.canvas_runner").setLevel(logging.CRITICAL)

SHEET = """### 镜头1 | 全景 | 缓慢推近 | 4s
- 画面：小焰站在花园中央
- 台词：小焰：你好

### 镜头2 | 中景 | 固定 | 3s
- 画面：阿蓝从树后探出头

### 镜头3 | 特写 | 推进 | 2s
- 画面：两人对视
"""


class _FakeResolved:
    class _Svc:
        id = 1

    service = _Svc()
    adapter = None
    model_name = "fake-model"


def _run(fn):  # noqa: ANN001
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    real_session = canvas_runner.SessionLocal
    real_resolve = provider_store.resolve_model
    real_start = canvas_runner.runner.start_or_fail
    started: list[int] = []

    async def fake_resolve(db, model_key, modality):  # noqa: ANN001, ARG001
        return _FakeResolved()

    async def fake_start(kind, task_id):  # noqa: ANN001, ARG001
        started.append(task_id)
        return True

    async def scenario():  # noqa: ANN202
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        canvas_runner.SessionLocal = maker
        provider_store.resolve_model = fake_resolve
        canvas_runner.runner.start_or_fail = fake_start
        try:
            await fn(maker, started)
        finally:
            canvas_runner.SessionLocal = real_session
            provider_store.resolve_model = real_resolve
            canvas_runner.runner.start_or_fail = real_start
            await engine.dispose()

    asyncio.run(scenario())


def _doc() -> dict:
    return {
        "schemaVersion": 1,
        "nodes": [
            {
                "id": "sb",
                "type": "storyboard",
                "position": {"x": 0, "y": 0},
                "data": {"docText": SHEET, "prompt": "", "model_key": "m1"},
            },
            {
                "id": "simg",
                "type": "storyboardImage",
                "position": {"x": 300, "y": 0},
                "data": {"model_key": "m1", "size": "1024x1024", "n": 1, "shotLimit": 6},
            },
        ],
        "edges": [{"id": "e1", "source": "sb", "target": "simg"}],
        "viewport": {},
    }


async def _seed(maker) -> int:  # noqa: ANN001
    async with maker() as db:
        p = Project(name="镜头台测试", canvas_json=json.dumps(_doc(), ensure_ascii=False))
        db.add(p)
        await db.commit()
        await db.refresh(p)
        pid = p.id
        # 上游分镜节点跑过一次：正文在产物里，节点上也有一份 docText
        t = Task(
            kind="text",
            status="completed",
            service_id=1,
            model="m1",
            prompt="",
            params_json="{}",
            canvas_project_id=pid,
            canvas_node_id="sb",
            completed_at=datetime(2026, 11, 23, 10, 0, 0),
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)
        db.add(
            Asset(
                kind="document",
                filename="sheet.md",
                original_name="sheet.md",
                content_type="text/markdown",
                size=10,
                task_id=t.id,
            )
        )
        await db.commit()
    return pid


def test_shot_ids_are_stable_and_backfilled_in_order():
    """第一次出片按镜号补号；再解析一次不能换号（换号等于审核记录全丢）。"""
    async def scenario(maker, started):  # noqa: ANN001, ARG001
        pid = await _seed(maker)
        await canvas_runner.run_shot(pid, "1", node_id="simg")
        await canvas_runner.run_shot(pid, "2", node_id="simg")

        async with maker() as db:
            rows = (
                await db.execute(
                    select(ProjectShot).where(ProjectShot.project_id == pid).order_by(ProjectShot.id)
                )
            ).scalars().all()
        assert [r.shot_no for r in rows] == ["1", "2", "3"], "补号没有覆盖整张分镜表"
        before = {r.shot_no: r.shot_id for r in rows}

        # 再解析一次（换一个节点跑）：身份必须还是原来那三个
        await canvas_runner.run_shot(pid, "3", node_id="simg")
        async with maker() as db:
            rows = (
                await db.execute(
                    select(ProjectShot).where(ProjectShot.project_id == pid).order_by(ProjectShot.id)
                )
            ).scalars().all()
        assert {r.shot_no: r.shot_id for r in rows} == before, "shot_id 不稳定"
        assert rows[0].dialogue.startswith("小焰"), "对白没有登记进镜头记录"

    _run(scenario)


def test_rerunning_one_shot_only_creates_one_task_for_that_shot():
    """改第 7 镜只补这一镜：任务数必须是 1，参数里带的是这一镜的镜号。"""
    async def scenario(maker, started):  # noqa: ANN001
        pid = await _seed(maker)
        await canvas_runner.run_shot(pid, "2", node_id="simg")
        assert len(started) == 1, f"按镜补跑派了 {len(started)} 个任务"

        async with maker() as db:
            rows = (await db.execute(select(Task).where(Task.canvas_project_id == pid))).scalars().all()
        shot_tasks = [t for t in rows if canvas_runner.read_task_params(t).get("shot_no") == "2"]
        assert len(shot_tasks) == 1
        assert shot_tasks[0].run_id, "按镜补跑的任务没盖上 run_id"

    _run(scenario)


def test_rerunning_an_unknown_shot_is_rejected():
    async def scenario(maker, started):  # noqa: ANN001, ARG001
        pid = await _seed(maker)
        with pytest.raises(ValueError) as e:
            await canvas_runner.run_shot(pid, "99", node_id="simg")
        assert "找不到" in str(e.value)

    _run(scenario)


def test_workbench_lists_shots_with_candidates_and_status():
    """工作台每行要能拿到：身份、审核状态、首帧候选、视频候选、对白。"""
    async def scenario(maker, started):  # noqa: ANN001, ARG001
        pid = await _seed(maker)
        await canvas_runner.run_shot(pid, "1", node_id="simg")
        async with maker() as db:
            t = Task(
                kind="image",
                status="completed",
                service_id=1,
                model="m1",
                prompt="",
                params_json=json.dumps({"shot_no": "1"}),
                canvas_project_id=pid,
                canvas_node_id="simg",
                completed_at=datetime(2026, 11, 23, 11, 0, 0),
            )
            db.add(t)
            await db.commit()
            await db.refresh(t)
            db.add(
                Asset(
                    kind="image",
                    filename="shot1.png",
                    original_name="shot1.png",
                    content_type="image/png",
                    size=1,
                    task_id=t.id,
                )
            )
            await db.commit()

        async with maker() as db:
            data = await canvas_runner.list_shots(db, pid)
        assert [s["no"] for s in data["shots"]] == ["1", "2", "3"]
        first = data["shots"][0]
        assert first["shotId"] and first["status"] == "pending"
        assert len(first["images"]) == 1 and first["images"][0]["url"].startswith("/media/")
        assert data["videoNodeId"] == "" and data["imageNodeId"] == "simg"

    _run(scenario)


def test_review_status_is_validated_and_saved():
    async def scenario(maker, started):  # noqa: ANN001, ARG001
        pid = await _seed(maker)
        await canvas_runner.run_shot(pid, "1", node_id="simg")
        async with maker() as db:
            shot_id = (
                await db.execute(
                    select(ProjectShot.shot_id).where(
                        ProjectShot.project_id == pid, ProjectShot.shot_no == "1"
                    )
                )
            ).scalar_one()
            out = await canvas_runner.set_shot_status(db, pid, shot_id, "approved")
        assert out["status"] == "approved"

        async with maker() as db:
            with pytest.raises(ValueError):
                await canvas_runner.set_shot_status(db, pid, shot_id, "随便写的")
            with pytest.raises(ValueError):
                await canvas_runner.set_shot_status(db, pid, "no-such-shot", "approved")

    _run(scenario)


def test_rerunning_a_shot_resets_its_review_status():
    """补跑出新候选后，旧的「通过」不能继续给新片背书。"""
    async def scenario(maker, started):  # noqa: ANN001, ARG001
        pid = await _seed(maker)
        await canvas_runner.run_shot(pid, "1", node_id="simg")
        async with maker() as db:
            shot_id = (
                await db.execute(
                    select(ProjectShot.shot_id).where(
                        ProjectShot.project_id == pid, ProjectShot.shot_no == "1"
                    )
                )
            ).scalar_one()
            await canvas_runner.set_shot_status(db, pid, shot_id, "approved")

        await canvas_runner.run_shot(pid, "1", node_id="simg")
        async with maker() as db:
            status = (
                await db.execute(
                    select(ProjectShot.status).where(ProjectShot.shot_id == shot_id)
                )
            ).scalar_one()
        assert status == "pending", "补跑之后审核状态没有回到待审"

    _run(scenario)
