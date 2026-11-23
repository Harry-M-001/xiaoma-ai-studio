"""画布修订号 + 增量重跑：只改一处时，其余节点不跟着重做。

两件事都关乎「钱和别人的改动」：
- 修订号：两个标签页同时开着，晚到的那次保存不能悄悄盖掉别人刚改的内容；
- 增量：整图默认只派输入变过的节点；已有产物且输入没变的直接复用，不重新调用。
  但上游这一跑要重跑时，下游不能按旧输入复用——那会拿上一版的结果顶替。
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

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import Asset, Project, Task  # noqa: E402
from app.routers import canvas as canvas_router  # noqa: E402
from app.services import canvas_runner, provider_store  # noqa: E402

logging.getLogger("app.services.canvas_runner").setLevel(logging.CRITICAL)


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
    real_timeout = canvas_runner._NODE_WAIT_TIMEOUT
    real_poll = canvas_runner._POLL_INTERVAL

    async def fake_resolve(db, model_key, modality):  # noqa: ANN001, ARG001
        return _FakeResolved()

    async def fake_start(kind, task_id):  # noqa: ANN001, ARG001
        async with maker() as db:
            t = await db.get(Task, task_id)
            if t is not None and t.status == "pending":
                t.status = "completed"
                t.completed_at = datetime(2026, 11, 23, 12, 0, 0)
                db.add(
                    Asset(
                        kind="image",
                        filename=f"out-{task_id}.png",
                        original_name=f"out-{task_id}",
                        content_type="image/png",
                        size=1,
                        task_id=task_id,
                    )
                )
                await db.commit()
        return True

    async def scenario():  # noqa: ANN202
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        canvas_runner.SessionLocal = maker
        provider_store.resolve_model = fake_resolve
        canvas_runner.runner.start_or_fail = fake_start
        canvas_runner._NODE_WAIT_TIMEOUT = 2
        canvas_runner._POLL_INTERVAL = 0.01
        try:
            await fn(maker)
        finally:
            canvas_runner.SessionLocal = real_session
            provider_store.resolve_model = real_resolve
            canvas_runner.runner.start_or_fail = real_start
            canvas_runner._NODE_WAIT_TIMEOUT = real_timeout
            canvas_runner._POLL_INTERVAL = real_poll
            canvas_runner._ACTIVE_RUNS.clear()
            await engine.dispose()

    asyncio.run(scenario())


def _doc(nodes: list[dict], edges: list[dict] | None = None) -> dict:
    return {"schemaVersion": 1, "nodes": nodes, "edges": edges or [], "viewport": {}}


def _image_node(nid: str, prompt: str = "一只猫") -> dict:
    return {
        "id": nid,
        "type": "image",
        "position": {"x": 0, "y": 0},
        "data": {"prompt": prompt, "model_key": "m1", "n": 1},
    }


async def _seed(maker, doc: dict) -> int:  # noqa: ANN001
    async with maker() as db:
        p = Project(name="增量测试", canvas_json=json.dumps(doc, ensure_ascii=False))
        db.add(p)
        await db.commit()
        await db.refresh(p)
        return p.id


async def _task_count(maker, project_id: int, node_id: str = "") -> int:  # noqa: ANN001
    async with maker() as db:
        stmt = select(func.count(Task.id)).where(Task.canvas_project_id == project_id)
        if node_id:
            stmt = stmt.where(Task.canvas_node_id == node_id)
        return int((await db.execute(stmt)).scalar_one())


# ------------------------------------------------------------------ 修订号


def test_canvas_revision_rejects_a_stale_save():
    """两个标签页都基于第 0 版保存：后到的那次必须被拒，不能悄悄覆盖。"""
    async def scenario(maker):  # noqa: ANN001
        pid = await _seed(maker, _doc([_image_node("n1")]))
        async with maker() as db:
            first = await canvas_router.save_canvas(
                pid, canvas_router.CanvasDocIn(**{"nodes": [_image_node("n1")], "baseRevision": 0}), db
            )
        assert first["revision"] == 1

        async with maker() as db:
            with pytest.raises(HTTPException) as e:
                await canvas_router.save_canvas(
                    pid,
                    canvas_router.CanvasDocIn(
                        **{"nodes": [_image_node("n1", "来自另一个标签页")], "baseRevision": 0}
                    ),
                    db,
                )
        assert e.value.status_code == 409
        assert "第 1 版" in str(e.value.detail)

        async with maker() as db:
            doc = await canvas_router.get_canvas(pid, db)
        assert doc["revision"] == 1, "被拒绝的保存不该改到版本号"

    _run(scenario)


def test_saving_with_the_current_revision_succeeds():
    async def scenario(maker):  # noqa: ANN001
        pid = await _seed(maker, _doc([_image_node("n1")]))
        async with maker() as db:
            r1 = await canvas_router.save_canvas(
                pid, canvas_router.CanvasDocIn(nodes=[_image_node("n1")], baseRevision=0), db
            )
        async with maker() as db:
            r2 = await canvas_router.save_canvas(
                pid,
                canvas_router.CanvasDocIn(
                    nodes=[_image_node("n1", "改过的提示词")], baseRevision=r1["revision"]
                ),
                db,
            )
        assert r2["revision"] == r1["revision"] + 1
        async with maker() as db:
            doc = await canvas_router.get_canvas(pid, db)
        assert doc["revision"] == 2
        assert doc["nodes"][0]["data"]["prompt"] == "改过的提示词"

    _run(scenario)


def test_legacy_save_without_revision_still_works():
    """老调用方不传 baseRevision：照旧可保存（但要顺带把版本号推进）。"""
    async def scenario(maker):  # noqa: ANN001
        pid = await _seed(maker, _doc([_image_node("n1")]))
        async with maker() as db:
            r = await canvas_router.save_canvas(
                pid, canvas_router.CanvasDocIn(nodes=[_image_node("n1")]), db
            )
        assert r["revision"] == 1

    _run(scenario)


# ------------------------------------------------------------------ 增量重跑


def test_second_run_reuses_unchanged_nodes():
    """同一张画布跑第二遍：输入没变，默认一个任务都不该再派。"""
    async def scenario(maker):  # noqa: ANN001
        pid = await _seed(maker, _doc([_image_node("n1")]))

        await canvas_runner._run_full_graph(pid, "run-a", expected=1, incremental=True)
        assert await _task_count(maker, pid) == 1

        await canvas_runner._run_full_graph(pid, "run-b", expected=0, incremental=True)
        assert await _task_count(maker, pid) == 1, "输入没变却又派了一次（等于花两遍钱）"

        await canvas_runner._run_full_graph(pid, "run-c", expected=1, incremental=False)
        assert await _task_count(maker, pid) == 2, "显式全量重跑没有真的重跑"

    _run(scenario)


def test_changed_node_reruns_and_takes_its_downstream_with_it():
    """改了上游节点：它自己重跑，下游也不能按旧输入复用。"""
    async def scenario(maker):  # noqa: ANN001
        doc = _doc(
            [_image_node("n1"), _image_node("n2")],
            [{"id": "e1", "source": "n1", "target": "n2"}],
        )
        pid = await _seed(maker, doc)
        await canvas_runner._run_full_graph(pid, "run-a", expected=2, incremental=True)
        assert await _task_count(maker, pid) == 2

        async with maker() as db:
            p = await db.get(Project, pid)
            p.canvas_json = json.dumps(
                _doc(
                    [_image_node("n1", "一只狗"), _image_node("n2")],
                    [{"id": "e1", "source": "n1", "target": "n2"}],
                ),
                ensure_ascii=False,
            )
            await db.commit()

        await canvas_runner._run_full_graph(pid, "run-b", expected=2, incremental=True)
        assert await _task_count(maker, pid, "n1") == 2, "改过的上游节点没有重跑"
        assert await _task_count(maker, pid, "n2") == 2, "上游变了，下游却按旧输入复用了"

    _run(scenario)


def test_preview_reports_what_incremental_skips():
    """预估要和真跑同口径：增量跳过几个节点、要调几次，点下去之前就看得到。"""
    async def scenario(maker):  # noqa: ANN001
        pid = await _seed(maker, _doc([_image_node("n1"), _image_node("n2")]))
        await canvas_runner._run_full_graph(pid, "run-a", expected=2, incremental=True)

        inc = await canvas_runner.preview_graph(pid)
        assert inc["totals"]["calls"] == 0
        assert inc["totals"]["skippedNodes"] == 2
        assert inc["totals"]["steps"] == 0
        assert any("输入没有变化" in n["text"] for n in inc["notes"])

        full = await canvas_runner.preview_graph(pid, incremental=False)
        assert full["totals"]["calls"] == 2
        assert full["totals"]["skippedNodes"] == 0
        assert full["totals"]["steps"] == 2

    _run(scenario)


def test_preview_does_not_skip_a_node_whose_upstream_will_rerun():
    """上游这一跑要重跑时，下游即使在预估里也不能算「跳过」。"""
    async def scenario(maker):  # noqa: ANN001
        doc = _doc(
            [_image_node("n1"), _image_node("n2")],
            [{"id": "e1", "source": "n1", "target": "n2"}],
        )
        pid = await _seed(maker, doc)
        await canvas_runner._run_full_graph(pid, "run-a", expected=2, incremental=True)

        async with maker() as db:
            p = await db.get(Project, pid)
            p.canvas_json = json.dumps(
                _doc(
                    [_image_node("n1", "一只狗"), _image_node("n2")],
                    [{"id": "e1", "source": "n1", "target": "n2"}],
                ),
                ensure_ascii=False,
            )
            await db.commit()

        inc = await canvas_runner.preview_graph(pid)
        skipped = [n["label"] for n in inc["nodes"] if n.get("skipped")]
        assert "图片" not in skipped or inc["totals"]["skippedNodes"] <= 1
        assert inc["totals"]["skippedNodes"] <= 1, f"下游被误判为跳过：{skipped}"
        assert inc["totals"]["steps"] >= 2, "改过的节点与它的下游都应算进这一跑"

    _run(scenario)
