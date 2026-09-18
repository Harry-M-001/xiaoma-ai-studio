"""#30 预算闸与运行对账的回归测试（直接 python 运行）。

运行：venv/Scripts/python tests/test_run_budget.py

这一版做的是「跑之前拦一下、跑完之后对一次账」，所以测三件事：

1. **闸门的分寸**：只在**已经确定**超限时要求再确认；「还有节点的次数要等上游」不能当成超限
   （那样一个还没跑过的项目会永远点不动「运行整图」）。
2. **这一跑的账要能算出来**：`run_id` 必须盖到这一跑派出的每一条任务上，
   否则事后只能捞「这个项目历来所有任务」，跟「刚才这一跑」混在一起就没法对账。
3. **对账要把差得多的说出来**：实际比预估多，通常是重试或上游切了更多块；
   比预估少，通常是失败或整段跳过。两种都不能只写一句「完成」。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import Asset, Project, Task  # noqa: E402
from app.services import canvas_runner, provider_store, run_budget  # noqa: E402

# 整图执行在测试里必然「等待超时」（把等待预算压到 0 了），别让它刷一屏警告
logging.getLogger("app.services.canvas_runner").setLevel(logging.CRITICAL)

ROUTER_SRC = (BACKEND / "app" / "routers" / "canvas.py").read_text(encoding="utf-8")
RUNNER_SRC = (BACKEND / "app" / "services" / "canvas_runner.py").read_text(encoding="utf-8")


def _body(name: str, src: str = RUNNER_SRC) -> str:
    """按函数名切出源码正文（到下一个顶层 def / async def 之前）。"""
    start = src.index(f"async def {name}(") if f"async def {name}(" in src else src.index(f"def {name}(")
    ends = [
        p
        for p in (src.find("\nasync def ", start + 1), src.find("\ndef ", start + 1))
        if p != -1
    ]
    return src[start : min(ends) if ends else len(src)]

SHEET = """### 镜头1 | 全景 | 缓慢推近 | 4s
- 画面：小焰站在花园中央

### 镜头2 | 中景 | 固定 | 3s
- 画面：阿蓝从树后探出头
"""


# ================================================================ 纯逻辑


def test_gate_never_blocks_when_limit_is_zero():
    """没设上限就不该有任何反应——默认值必须是「不限制」。"""
    state = run_budget.gate(999, 5, limit=0)
    assert state["exceeds"] is False
    assert state["uncertain"] is False
    assert state["ack"] == 0
    assert run_budget.gate_message(state) == ""


def test_gate_exceeds_only_when_it_is_certain():
    """确定超限才要求再确认；「还没算出来」只提示。"""
    over = run_budget.gate(30, 0, limit=20)
    assert over["exceeds"] is True and over["uncertain"] is False

    under = run_budget.gate(10, 0, limit=20)
    assert under["exceeds"] is False and under["uncertain"] is False

    unknown = run_budget.gate(10, 3, limit=20)
    assert unknown["exceeds"] is False, "还不确定就当成超限，会让新项目永远点不动"
    assert unknown["uncertain"] is True

    # 已经确定超限时，即使还有待定节点也仍然算「确定超限」
    both = run_budget.gate(30, 3, limit=20)
    assert both["exceeds"] is True


def test_gate_ack_is_the_estimated_call_count():
    """回传值就是调用次数：接口拿它和服务端此刻重算的数比，所以预览之后改画布也挡得住。"""
    assert run_budget.gate(30, 0, limit=20)["ack"] == 30
    assert run_budget.gate(10, 0, limit=20)["ack"] == 0
    assert run_budget.gate(30, 0, limit=0)["ack"] == 0


def test_gate_message_says_different_things_for_the_two_cases():
    """「超了」和「可能更多」是两件事，说法一样就是在吓人。"""
    over = run_budget.gate_message(run_budget.gate(30, 0, limit=20))
    unsure = run_budget.gate_message(run_budget.gate(10, 3, limit=20))
    assert "超过你设的上限 20 次" in over
    assert "超过" not in unsure, f"「还不确定」不该写成超限：{unsure}"
    assert "可能更多" in unsure
    assert over != unsure


def test_verdict_flags_big_deltas_and_failures():
    same = run_budget.verdict(12, 12)
    assert same["level"] == "ok" and same["delta"] == 0 and "一致" in same["text"]

    plus = run_budget.verdict(12, 18)
    assert plus["level"] == "warn" and "多 6 次" in plus["text"] and "重试" in plus["text"]

    minus = run_budget.verdict(12, 5)
    assert minus["level"] == "warn" and "少 7 次" in minus["text"]

    # 差一两次属于正常波动，不必喊
    assert run_budget.verdict(12, 13)["level"] == "ok"

    with_failures = run_budget.verdict(12, 12, failed=3)
    assert "3 条失败" in with_failures["text"], "失败任务照样计费，必须单独说一句"


def test_budget_survives_broken_config():
    """配置写坏了就当作「不限制」——不能因为读不到一个数字就把用户挡在门外。"""
    from app.services import config_center_service

    real = config_center_service.runtime_value
    try:
        for bad in ("abc", None, "", -5, "3.5"):
            config_center_service.runtime_value = lambda *a, _v=bad, **k: _v  # type: ignore[assignment]
            assert run_budget.call_budget() == 0, f"配置是 {bad!r} 时应当按「不限制」处理"
        config_center_service.runtime_value = lambda *a, **k: 25  # type: ignore[assignment]
        assert run_budget.call_budget() == 25
    finally:
        config_center_service.runtime_value = real  # type: ignore[assignment]


# ================================================================ 接线


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
    real_budget = run_budget.call_budget

    async def fake_resolve(db, model_key, modality):  # noqa: ANN001
        return _FakeResolved()

    async def fake_start(kind, task_id):  # noqa: ANN001
        return True

    async def scenario():  # noqa: ANN202
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        canvas_runner.SessionLocal = maker
        provider_store.resolve_model = fake_resolve
        canvas_runner.runner.start_or_fail = fake_start
        # 整图执行里等任务完成的那段：测试里把预算压到 0，避免为一个必然超时的循环等 15 分钟
        canvas_runner._NODE_WAIT_TIMEOUT = 0
        canvas_runner._POLL_INTERVAL = 0.01
        try:
            await fn(maker)
        finally:
            canvas_runner.SessionLocal = real_session
            provider_store.resolve_model = real_resolve
            canvas_runner.runner.start_or_fail = real_start
            canvas_runner._NODE_WAIT_TIMEOUT = real_timeout
            canvas_runner._POLL_INTERVAL = real_poll
            run_budget.call_budget = real_budget  # type: ignore[assignment]
            await engine.dispose()

    asyncio.run(scenario())


def _doc(nodes: list[dict], edges: list[dict] | None = None) -> dict:
    return {"schemaVersion": 1, "nodes": nodes, "edges": edges or [], "viewport": {}}


def _doc_node(nid: str, ntype: str = "idea") -> dict:
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"prompt": "写点什么", "model_key": "m1", "chunkCount": 2}}


def _image_node(nid: str) -> dict:
    return {"id": nid, "type": "image", "position": {"x": 0, "y": 0},
            "data": {"prompt": "一只猫", "model_key": "m1", "n": 2}}


def _sheet_node(nid: str) -> dict:
    return {"id": nid, "type": "storyboard", "position": {"x": 0, "y": 0},
            "data": {"docText": SHEET, "prompt": ""}}


def _simg_node(nid: str) -> dict:
    return {"id": nid, "type": "storyboardImage", "position": {"x": 0, "y": 0},
            "data": {"model_key": "m1", "size": "1024x1024", "n": 1, "shotLimit": 6}}


async def _seed(maker, doc: dict) -> int:  # noqa: ANN001
    async with maker() as db:
        p = Project(name="预算闸测试", canvas_json=json.dumps(doc, ensure_ascii=False))
        db.add(p)
        await db.commit()
        await db.refresh(p)
        return p.id


async def _give_upstream_doc(maker, project_id: int, node_id: str) -> None:  # noqa: ANN001
    """给上游文档节点造一条「已生成」的记录。

    `run_single_node` 要求上游**跑过**（有产物），否则直接报「请先运行它们」；
    正文本身由节点上的 `docText` 提供，所以产物文件不必真实存在。
    """
    async with maker() as db:
        t = Task(kind="text", status="completed", service_id=1, model="m1", prompt="",
                 params_json="{}", canvas_project_id=project_id, canvas_node_id=node_id)
        db.add(t)
        await db.commit()
        await db.refresh(t)
        db.add(Asset(kind="document", filename="sheet.md", original_name="sheet.md",
                     content_type="text/markdown", size=10, task_id=t.id))
        await db.commit()


def test_preview_reports_calls_and_the_gate():
    """预估除了任务数，还要给出「调用次数」与闸门状态。"""
    async def scenario(maker):  # noqa: ANN001
        pid = await _seed(maker, _doc([_doc_node("n1"), _image_node("n2")]))
        run_budget.call_budget = lambda: 1  # type: ignore[assignment]
        preview = await canvas_runner.preview_graph(pid)

        calls = preview["totals"]["calls"]
        assert calls == preview["totals"]["tasks"] > 0, "调用次数没算出来"
        gate = preview["gate"]
        assert gate["limit"] == 1 and gate["exceeds"] is True
        assert gate["ack"] == calls, "回传值应当是这一跑的调用次数"
        assert any("上限" in n["text"] for n in preview["notes"]), "超限没出现在告警里"

    _run(scenario)


def test_preview_gate_is_quiet_under_the_limit():
    """没超限就不该多出一句告警——否则每次运行都在喊。"""
    async def scenario(maker):  # noqa: ANN001
        pid = await _seed(maker, _doc([_doc_node("n1")]))
        run_budget.call_budget = lambda: 100  # type: ignore[assignment]
        preview = await canvas_runner.preview_graph(pid)
        assert preview["gate"]["exceeds"] is False
        assert not any("上限" in n["text"] for n in preview["notes"])

    _run(scenario)


def test_single_node_run_stamps_every_task():
    """单节点一次派多个任务时，每一条都要盖上同一个 run_id。"""
    async def scenario(maker):  # noqa: ANN001
        doc = _doc([_sheet_node("sb"), _simg_node("simg")],
                   [{"id": "e1", "source": "sb", "target": "simg"}])
        pid = await _seed(maker, doc)
        await _give_upstream_doc(maker, pid, "sb")
        tasks = await canvas_runner.run_single_node(pid, "simg", "run-abc")
        assert len(tasks) == 2, "两镜应当派两个任务"
        async with maker() as db:
            rows = (await db.execute(select(Task).where(Task.run_id == "run-abc"))).scalars().all()
        assert len(rows) == 2, "有任务没盖上 run_id"

    _run(scenario)


def test_run_id_is_optional_so_old_callers_keep_working():
    """不传 run_id 时行为不变（既有调用方与测试不受影响）。"""
    async def scenario(maker):  # noqa: ANN001
        pid = await _seed(maker, _doc([_image_node("n1")]))
        tasks = await canvas_runner.run_single_node(pid, "n1")
        assert all(t.run_id is None for t in tasks)

    _run(scenario)


def test_full_graph_run_returns_an_id_and_stamps_its_tasks():
    """整图运行也要有自己的 run_id，并且盖到它派出的每一条任务上。"""
    async def scenario(maker):  # noqa: ANN001
        pid = await _seed(maker, _doc([_image_node("n1")]))
        run_id = canvas_runner.new_run_id()
        assert run_id and len(run_id) <= 32
        await canvas_runner._run_full_graph(pid, run_id)
        async with maker() as db:
            rows = (await db.execute(select(Task).where(Task.run_id == run_id))).scalars().all()
        assert rows, "整图没派出任务，测不到盖戳"
        assert all(t.run_id == run_id for t in rows)

    _run(scenario)


def test_new_run_ids_are_unique():
    ids = {canvas_runner.new_run_id() for _ in range(200)}
    assert len(ids) == 200


async def _add_task(
    maker,  # noqa: ANN001
    *,
    run_id: str,
    status: str,
    kind: str = "image",
    node_id: str = "n1",
    project_id: int | None = None,
    asset: str = "",
    duration: int = 0,
) -> None:
    async with maker() as db:
        t = Task(
            kind=kind, status=status, service_id=1, model="m1", prompt="",
            params_json="{}", run_id=run_id, canvas_project_id=project_id,
            canvas_node_id=node_id,
            created_at=datetime(2026, 9, 18, 10, 0, 0),
            completed_at=datetime(2026, 9, 18, 10, 1, 0) if status == "completed" else None,
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)
        if asset:
            db.add(Asset(
                kind=asset, filename=f"x-{t.id}", original_name="x", content_type="",
                size=10, task_id=t.id, duration=duration or None,
            ))
            await db.commit()


def test_run_summary_counts_only_this_run():
    """只算这一跑：混进别的 run_id、别的项目都不能影响结论。"""
    async def scenario(maker):  # noqa: ANN001
        doc = _doc([_image_node("n1"), _simg_node("simg")])
        pid = await _seed(maker, doc)
        other = await _seed(maker, doc)

        await _add_task(maker, run_id="r1", status="completed", node_id="n1", project_id=pid, asset="image")
        await _add_task(maker, run_id="r1", status="completed", kind="video", node_id="simg",
                        project_id=pid, asset="video", duration=5)
        await _add_task(maker, run_id="r1", status="failed", node_id="simg", project_id=pid)
        # 干扰项：同一项目里别的 run、别的项目里同名的 run
        await _add_task(maker, run_id="r2", status="completed", node_id="n1", project_id=pid)
        await _add_task(maker, run_id="r1", status="completed", node_id="n1", project_id=other)

        s = await canvas_runner.run_summary(pid, "r1")
        assert s["calls"] == 3, s
        assert s["completed"] == 2 and s["failed"] == 1
        assert s["finished"] is True and s["running"] == 0
        assert s["byKind"] == {"image": 2, "video": 1}
        assert s["products"] == {"images": 1, "videos": 1, "documents": 0, "videoSeconds": 5}
        assert s["elapsedSec"] == 60
        labels = {n["id"]: n["label"] for n in s["nodes"]}
        assert labels.get("simg") == "分镜图", f"节点名没对上：{labels}"
        assert [n["id"] for n in s["nodes"]][0] == "simg", "任务多的节点应当排前面"

    _run(scenario)


def test_run_summary_says_not_finished_while_tasks_are_running():
    """还在跑的时候要如实说没跑完，不能拿半截数字当结论。"""
    async def scenario(maker):  # noqa: ANN001
        pid = await _seed(maker, _doc([_image_node("n1")]))
        await _add_task(maker, run_id="r1", status="completed", project_id=pid)
        await _add_task(maker, run_id="r1", status="processing", project_id=pid)
        s = await canvas_runner.run_summary(pid, "r1")
        assert s["finished"] is False and s["running"] == 1
        assert s["completed"] == 1

    _run(scenario)


def test_run_summary_rejects_unknown_or_missing_ids():
    async def scenario(maker):  # noqa: ANN001
        pid = await _seed(maker, _doc([_image_node("n1")]))
        for bad in ("", "nope"):
            try:
                await canvas_runner.run_summary(pid, bad)
            except ValueError as e:
                assert "run" in str(e).lower() or "记录" in str(e)
                continue
            raise AssertionError(f"run_id={bad!r} 竟然返回了结果")

    _run(scenario)


def test_router_rechecks_the_estimate_instead_of_trusting_the_client():
    """闸门必须在服务端重算：预览之后用户可能又改了画布，拿过期的确认值放行等于没设闸。"""
    body = _body("run_canvas", ROUTER_SRC)
    assert "ack_calls" in body and "status_code=409" in body
    assert "preview_graph(project_id)" in body, "服务端没有重算预估"
    assert "start_full_graph(project_id" in body
    # 重算出来的那个数要顺手交给这一跑记着：刷新页面后前端靠它把预估带回来
    assert "expected=estimate_calls" in body, "预估没有记到这一跑上"


def test_preview_is_still_read_only_with_the_gate():
    """加了闸门之后，预估依然不写库（它会被 run 接口再调一次，写库就出事了）。"""
    body = _body("preview_graph")
    assert "db.add(" not in body and "db.commit(" not in body


def test_gate_wording_points_at_the_config():
    """说超限就得说清去哪儿改，否则用户只能自己找。"""
    text = run_budget.gate_message(run_budget.gate(30, 0, limit=20))
    assert "系统设置" in text


def test_stamp_run_commits_nothing_when_there_is_no_run():
    """没给 run_id 时不要白白 commit 一次（每个节点都会走到这里）。"""
    body = _body("_stamp_run")
    assert "if not run_id or not tasks:" in body
    assert body.index("return") < body.index("commit"), "先判断再提交，顺序反了会白写一次"


def test_a_graph_still_walking_is_not_reported_as_finished():
    """整图是**边跑边派**的：第一个节点跑完时不能报「跑完了」。

    这条是浏览器验证里真踩到的坑——两个出图节点，第一个跑完（已有任务都结束了）、
    第二个还没派出去，账上就成了「实际 1 次（预估 2 次）」，比不显示更糟。
    """
    async def scenario(maker):  # noqa: ANN001
        pid = await _seed(maker, _doc([_image_node("n1"), _image_node("n2")]))
        canvas_runner._ACTIVE_RUNS.clear()
        run_id = "walking"
        canvas_runner._register_run(run_id, pid, 2)
        # 第一个节点已经派完并结束了，第二个还没轮到
        await _add_task(maker, run_id=run_id, status="completed", node_id="n1", project_id=pid)

        s = await canvas_runner.run_summary(pid, run_id)
        assert s["running"] == 0, s
        assert s["active"] is True and s["finished"] is False, "还在一跑的中间就被当成跑完了"
        assert s["expected"] == 2, "受理时算的预估没带给前端，刷新后就看不到了"

        canvas_runner._finish_run(run_id)
        s2 = await canvas_runner.run_summary(pid, run_id)
        assert s2["finished"] is True and s2["active"] is False

    _run(scenario)
    canvas_runner._ACTIVE_RUNS.clear()


def test_a_graph_that_blows_up_still_gets_marked_finished():
    """中途炸了也必须收尾：漏了这一步，前端会以为还在跑，永远不弹那一跑的实际账。"""
    async def scenario(maker):  # noqa: ANN001
        pid = await _seed(maker, _doc([_image_node("n1")]))
        canvas_runner._ACTIVE_RUNS.clear()
        real = canvas_runner._walk_graph

        async def boom(_project_id, _run_id):  # noqa: ANN001, ANN202
            raise RuntimeError("炸了")

        canvas_runner._walk_graph = boom  # type: ignore[assignment]
        run_id = canvas_runner.new_run_id()
        canvas_runner._register_run(run_id, pid, 3)
        try:
            await canvas_runner._run_full_graph(pid, run_id)
        except RuntimeError:
            pass  # 炸出来是预期内的，这里只关心有没有收尾
        finally:
            canvas_runner._walk_graph = real  # type: ignore[assignment]

        assert canvas_runner.run_progress(run_id) == {"active": False, "expected": 3}

    _run(scenario)
    canvas_runner._ACTIVE_RUNS.clear()


def test_starting_a_graph_registers_the_run_and_clears_it_when_done():
    """受理时就记成「在跑」，整图走完自动摘掉——前端靠这个决定还要不要继续轮询。"""
    async def scenario(maker):  # noqa: ANN001
        pid = await _seed(maker, _doc([_image_node("n1")]))
        canvas_runner._ACTIVE_RUNS.clear()
        run_id = canvas_runner.start_full_graph(pid, expected=1)
        assert canvas_runner.run_progress(run_id) == {"active": True, "expected": 1}, "启动后没记成在跑"

        for _ in range(200):
            await asyncio.sleep(0.01)
            if not canvas_runner.run_progress(run_id)["active"]:
                break
        assert canvas_runner.run_progress(run_id)["active"] is False, "整图走完了还挂着「在跑」"

    _run(scenario)
    canvas_runner._ACTIVE_RUNS.clear()


def test_the_run_registry_does_not_grow_forever():
    """run_id 是查账用的，但不能因为跑得多就一直攒着。"""
    canvas_runner._ACTIVE_RUNS.clear()
    for i in range(canvas_runner._MAX_ACTIVE_RUNS + 20):
        canvas_runner._register_run(f"r{i}", 1, i)
    assert len(canvas_runner._ACTIVE_RUNS) == canvas_runner._MAX_ACTIVE_RUNS
    assert canvas_runner.run_progress("r0")["active"] is False, "最老的应当已经被丢掉"
    canvas_runner._ACTIVE_RUNS.clear()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
