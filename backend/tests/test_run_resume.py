"""整图中断续跑：已完成节点不重复付费，说不清的节点不自动重发。

为什么这块要单独钉：整图是**边跑边派**的，进程一死，内存里的进度就没了。
以前的处理是把没远程 ID 的任务一律标失败、画布从此不再自动往下走——用户只能重新点一次
「运行整图」，而那一跑会把**已经出过图的节点**也重派一遍，等于同样的钱付两遍。
现在按 `canvas_runs` / `run_steps` 的检查点接着走，本文件钉住四条边界：

1. 已完成的节点被复用（不新建任务、不重新提交）；
2. 重启前就派过任务、只是还没轮到上游的（pending）只补入队，不重造任务；
3. 进了 processing 却拿不到远程 ID 的标 `unknown`，**不自动重发**，下游也不许继续花钱；
4. 对账之后没跑完的整图要真的接着走，跑完的要把这一跑收尾。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import Asset, CanvasRun, Project, RunStep, Task  # noqa: E402
from app.services import canvas_runner, provider_store  # noqa: E402
from app.services import runner as runner_mod  # noqa: E402

# 整图在测试里靠压缩等待预算跑完，别让「等待超时」的警告刷屏
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
    started: list[tuple[str, int]] = []

    async def fake_resolve(db, model_key, modality):  # noqa: ANN001, ARG001
        return _FakeResolved()

    async def fake_start(kind, task_id):  # noqa: ANN001
        """把任务当成「上游秒回」：直接完成并产出一条产物，省掉真调用。"""
        started.append((kind, task_id))
        async with maker() as db:
            t = await db.get(Task, task_id)
            if t is not None and t.status == "pending":
                t.status = "completed"
                t.completed_at = datetime(2026, 11, 23, 12, 0, 0)
                db.add(
                    Asset(
                        kind="image" if kind not in ("text", "video") else kind,
                        filename=f"out-{task_id}.bin",
                        original_name=f"out-{task_id}",
                        content_type="",
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
        # 让等待循环至少转一圈（fake_start 已经把任务标完成，第一圈就会退出）
        canvas_runner._NODE_WAIT_TIMEOUT = 2
        canvas_runner._POLL_INTERVAL = 0.01
        try:
            await fn(maker, started)
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


async def _seed_project(maker, doc: dict, name: str = "续跑测试") -> int:  # noqa: ANN001
    async with maker() as db:
        p = Project(name=name, canvas_json=json.dumps(doc, ensure_ascii=False))
        db.add(p)
        await db.commit()
        await db.refresh(p)
        return p.id


async def _add_completed_node(  # noqa: ANN001
    maker, project_id: int, run_id: str, node_id: str, *, step_status: str = "succeeded"
) -> Task:
    """造一个「上一跑已经把该节点跑完」的现场：完成的任务 + 一条检查点。

    检查点里的指纹必须是**这一节点真实的输入指纹**——续跑只复用「同输入」的结果，
    随手写个假串会让节点被当成「输入变了」而重跑，测不到复用。
    """
    async with maker() as db:
        t = Task(
            kind="image",
            status="completed",
            service_id=1,
            model="m1",
            prompt="",
            params_json="{}",
            canvas_project_id=project_id,
            canvas_node_id=node_id,
            run_id=run_id,
            completed_at=datetime(2026, 11, 23, 11, 0, 0),
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)
        db.add(
            Asset(
                kind="image",
                filename=f"old-{t.id}.png",
                original_name="old",
                content_type="image/png",
                size=1,
                task_id=t.id,
            )
        )
        db.add(
            RunStep(
                run_id=run_id,
                project_id=project_id,
                node_id=node_id,
                input_hash=canvas_runner.node_input_hash(_image_node(node_id), []),
                status=step_status,
                task_ids=json.dumps([t.id]),
            )
        )
        await db.commit()
        return t


async def _tasks_of(maker, project_id: int, node_id: str) -> list[Task]:  # noqa: ANN001
    async with maker() as db:
        rows = await db.execute(
            select(Task).where(
                Task.canvas_project_id == project_id, Task.canvas_node_id == node_id
            )
        )
        return list(rows.scalars().all())


def test_input_hash_is_stable_and_moves_with_inputs():
    """同一个节点同一份上游 → 同一个指纹；改提示词或换上游产物 → 必须变。"""
    node = _image_node("n1")
    assert canvas_runner.node_input_hash(node, []) == canvas_runner.node_input_hash(node, [])

    changed = _image_node("n1", prompt="一只狗")
    assert canvas_runner.node_input_hash(changed, []) != canvas_runner.node_input_hash(node, [])

    class _A:
        id = 7

    assert canvas_runner.node_input_hash(node, []) != canvas_runner.node_input_hash(
        node, [("up", _image_node("up"), [_A()])]
    )


def test_completed_node_is_reused_instead_of_rerun():
    """重启后续跑：已经跑完的节点不能再派一次任务（那就是付第二遍钱）。"""
    async def scenario(maker, started):  # noqa: ANN001
        doc = _doc(
            [_image_node("n1"), _image_node("n2")],
            [{"id": "e1", "source": "n1", "target": "n2"}],
        )
        pid = await _seed_project(maker, doc)
        old = await _add_completed_node(maker, pid, "r1", "n1")

        await canvas_runner._run_full_graph(pid, "r1", expected=2)

        n1_tasks = await _tasks_of(maker, pid, "n1")
        assert [t.id for t in n1_tasks] == [old.id], "已完成的节点被重新派了任务"
        assert all(tid != old.id for _kind, tid in started), "已完成节点又被提交给上游"
        assert await _tasks_of(maker, pid, "n2"), "没跑过的下游节点没有被接着跑"

    _run(scenario)


def test_changed_inputs_are_rerun_instead_of_reused():
    """改过提示词的节点不能被「已完成」顶替：输入变了就是另一件事，要重派一次。"""
    async def scenario(maker, started):  # noqa: ANN001
        doc = _doc([_image_node("n1", "一只猫")])
        pid = await _seed_project(maker, doc)
        await _add_completed_node(maker, pid, "r1", "n1")

        # 模拟「崩溃之后用户改了提示词再续跑」
        async with maker() as db:
            p = await db.get(Project, pid)
            p.canvas_json = json.dumps(_doc([_image_node("n1", "一只狗")]), ensure_ascii=False)
            await db.commit()

        await canvas_runner._run_full_graph(pid, "r1", expected=1)

        tasks = await _tasks_of(maker, pid, "n1")
        assert len(tasks) == 2, "输入变了却仍然复用了旧结果"
        assert started, "输入变了却没有重新提交"

    _run(scenario)


def test_unknown_step_is_not_resent_and_blocks_downstream():
    """拿不准上游收没收到时：不自动重发，也不让下游拿半成品继续花下游的钱。"""
    async def scenario(maker, started):  # noqa: ANN001
        doc = _doc(
            [_image_node("n1"), _image_node("n2")],
            [{"id": "e1", "source": "n1", "target": "n2"}],
        )
        pid = await _seed_project(maker, doc)
        await _add_completed_node(maker, pid, "r1", "n1", step_status="unknown")

        await canvas_runner._run_full_graph(pid, "r1", expected=2)

        assert not started, f"不确定的节点被自动重发了：{started}"
        assert not await _tasks_of(maker, pid, "n2"), "上游不确定时下游仍然派了任务"

    _run(scenario)


def test_pending_tasks_are_requeued_not_recreated():
    """重启前就落库、但还没轮到上游的任务：补入队即可，不能重新建一条。"""
    async def scenario(maker, started):  # noqa: ANN001
        doc = _doc([_image_node("n1")])
        pid = await _seed_project(maker, doc)
        async with maker() as db:
            t = Task(
                kind="image",
                status="pending",
                service_id=1,
                model="m1",
                prompt="一只猫",
                params_json="{}",
                canvas_project_id=pid,
                canvas_node_id="n1",
                run_id="r1",
            )
            db.add(t)
            await db.commit()
            await db.refresh(t)
            db.add(
                RunStep(
                    run_id="r1",
                    project_id=pid,
                    node_id="n1",
                    input_hash=canvas_runner.node_input_hash(_image_node("n1"), []),
                    status="submitted",
                    task_ids=json.dumps([t.id]),
                )
            )
            await db.commit()
            task_id = t.id

        await canvas_runner._run_full_graph(pid, "r1", expected=1)

        assert [tid for _k, tid in started] == [task_id], f"补入队的不对：{started}"
        assert [t.id for t in await _tasks_of(maker, pid, "n1")] == [task_id], "任务被重造了一份"

    _run(scenario)


def test_reconcile_marks_unconfirmable_tasks_unknown():
    """对账口径：完成了算成功，pending 等下轮，processing 没远程 ID 只能算不确定。"""
    async def scenario(maker, started):  # noqa: ANN001, ARG001
        doc = _doc([_image_node("n1"), _image_node("n2"), _image_node("n3")])
        pid = await _seed_project(maker, doc)

        async with maker() as db:
            rows = []
            for nid, status, remote in (
                ("n1", "completed", None),
                ("n2", "processing", None),
                ("n3", "pending", None),
            ):
                t = Task(
                    kind="image", status=status, service_id=1, model="m1", prompt="",
                    params_json="{}", canvas_project_id=pid, canvas_node_id=nid,
                    run_id="r1", remote_job_id=remote,
                )
                db.add(t)
                await db.flush()
                rows.append((nid, t.id))
            for nid, tid in rows:
                db.add(
                    RunStep(
                        run_id="r1", project_id=pid, node_id=nid,
                        input_hash="h", status="submitted", task_ids=json.dumps([tid]),
                    )
                )
            await db.commit()

        cont = await canvas_runner._reconcile_run("r1", pid)
        assert cont is True, "还有 pending 的任务在等，整图应当继续"

        async with maker() as db:
            steps = {
                s.node_id: s
                for s in (
                    await db.execute(select(RunStep).where(RunStep.run_id == "r1"))
                ).scalars().all()
            }
        assert steps["n1"].status == "succeeded"
        assert steps["n2"].status == "unknown"
        assert steps["n3"].status == "submitted", "还好好的 pending 不该被改掉"

    _run(scenario)


def test_resume_runs_picks_up_unfinished_graphs_and_closes_finished_ones():
    """启动恢复：该接的接上，已经没事可做的要把这一跑收尾，不能永远挂着 running。"""
    async def scenario(maker, started):  # noqa: ANN001
        doc = _doc([_image_node("n1")])
        pid = await _seed_project(maker, doc)
        async with maker() as db:
            db.add(CanvasRun(run_id="resume-me", project_id=pid, expected=1, status="running"))
            db.add(CanvasRun(run_id="already-done", project_id=pid, expected=1, status="running"))
            await db.commit()
        # already-done 这一跑的唯一节点已经完成：恢复时应当直接收尾
        await _add_completed_node(maker, pid, "already-done", "n1")

        resumed = await canvas_runner.resume_runs()
        assert resumed == 1, f"应当只接上没跑完的那一跑，实际 {resumed}"

        for _ in range(300):
            await asyncio.sleep(0.01)
            if not canvas_runner.run_progress("resume-me")["active"]:
                break
        assert canvas_runner.run_progress("resume-me")["active"] is False, "接上的那一跑没有收尾"
        assert started, "接上的那一跑什么任务都没派"

        async with maker() as db:
            done_run = await db.get(CanvasRun, "already-done")
            resumed_run = await db.get(CanvasRun, "resume-me")
        assert done_run.status == "finished"
        assert resumed_run.status == "finished"

    _run(scenario)


def test_run_start_and_finish_are_persisted():
    """跑的起止要落库：重启后能判断「这一跑当时还在跑」，而不是一概当作结束。"""
    async def scenario(maker, started):  # noqa: ANN001
        pid = await _seed_project(maker, _doc([_image_node("n1")]))
        await canvas_runner._persist_run_start("r9", pid, 3)
        async with maker() as db:
            row = await db.get(CanvasRun, "r9")
        assert row is not None and row.status == "running" and row.expected == 3

        await canvas_runner._persist_run_finish("r9")
        async with maker() as db:
            row = await db.get(CanvasRun, "r9")
        assert row.status == "finished"

    _run(scenario)


# ------------------------------------------------------------------ 重启时任务怎么处理


def test_restart_requeues_pending_and_marks_unconfirmable_processing():
    """重启后：pending 重新入队（上游没收到过），processing 无远程 ID 标不确定。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    real_session = runner_mod.SessionLocal
    real_start = runner_mod.runner.start_or_fail
    real_reattach = runner_mod.runner.reattach
    started: list[int] = []
    reattached: list[int] = []

    async def fake_start(kind, task_id):  # noqa: ANN001, ARG001
        started.append(task_id)
        return True

    def fake_reattach(task_id):  # noqa: ANN001
        reattached.append(task_id)
        return True

    async def scenario():  # noqa: ANN202
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with maker() as db:
            pending = Task(kind="image", status="pending", service_id=1, model="m1",
                           prompt="", params_json="{}")
            unknown = Task(kind="image", status="processing", service_id=1, model="m1",
                           prompt="", params_json="{}")
            video = Task(kind="video", status="processing", service_id=1, model="m1",
                         prompt="", params_json="{}", remote_job_id="up-1")
            db.add_all([pending, unknown, video])
            await db.commit()
            ids = (pending.id, unknown.id, video.id)

        runner_mod.SessionLocal = maker
        runner_mod.runner.start_or_fail = fake_start
        runner_mod.runner.reattach = fake_reattach
        try:
            await runner_mod.runner.recover()
        finally:
            runner_mod.SessionLocal = real_session
            runner_mod.runner.start_or_fail = real_start
            runner_mod.runner.reattach = real_reattach

        assert started == [ids[0]], f"pending 任务没有重新入队：{started}"
        assert reattached == [ids[2]], f"有远程 ID 的视频没有续挂：{reattached}"
        async with maker() as db:
            t_unknown = await db.get(Task, ids[1])
            t_pending = await db.get(Task, ids[0])
        assert t_unknown.status == "failed"
        assert t_unknown.error == runner_mod.RESTART_UNKNOWN_ERROR
        assert t_pending.status == "pending", "补入队的任务不该被顺手标失败"
        await engine.dispose()

    asyncio.run(scenario())
