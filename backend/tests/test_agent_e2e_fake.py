"""Agent 端到端「假模型 + 假画布任务」测试：不花钱、不碰真实库。

跑法：venv/Scripts/python -m pytest tests/test_agent_e2e_fake.py -q

三块都换成假的，但**走真实代码路径**：
- 模型：脚本化 FakeAdapter（按「已经拿到几个工具结果」决定下一步调什么）
- 画布任务：run_node / run_canvas 只建一条 processing 任务，0.2 秒后自动完成
- 数据库：临时文件 SQLite + 依赖覆盖，永远不碰 data/xiaoma.db

守的是代理编排本身：/goal 会不会推进下游、会不会空转、/spec 挡不挡写操作、
重复查询挡不挡、停止按钮灵不灵、没绑定画布会不会误判。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.database import Base  # noqa: E402
from app.deps import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AgentProject, AgentSession, AgentStep, Project, Task  # noqa: E402
from app.routers import agent as agent_router  # noqa: E402
from app.services import agent_runner, canvas_runner  # noqa: E402


# ================================================================ 脚手架


class FakeAdapter:
    """脚本化模型：policy(已有工具结果数) -> 下一条响应。"""

    def __init__(self, policy):
        self.policy = policy
        self.calls = 0

    def supports_thinking(self, model: str) -> bool:
        return False

    async def chat_once(self, model, messages, tools=None, thinking=None):
        self.calls += 1
        used = sum(1 for m in messages if m.get("role") == "tool")
        return self.policy(used)

    async def close(self):
        pass


def _reply(content="", *, name="", args=None):
    calls = []
    if name:
        calls.append({"name": name, "arguments": json.dumps(args or {}, ensure_ascii=False)})
    return {"content": content, "reasoning": "", "tool_calls": calls}


async def _complete_later(maker, task_id: int, delay: float = 0.2) -> None:
    await asyncio.sleep(delay)
    async with maker() as db:
        task = await db.get(Task, task_id)
        if task is not None and task.status in ("pending", "processing"):
            task.status = "completed"
            task.progress = 100
            await db.commit()


class Harness:
    """一套临时库 + 假画布 + 假任务执行器。"""

    def __init__(self, *, with_canvas: bool = True):
        self.with_canvas = with_canvas

    async def __aenter__(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "e2e.db"
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.db_path.as_posix()}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.maker() as db:
            self.canvas_id = self.agent_project_id = None
            if self.with_canvas:
                doc = {
                    "schemaVersion": 1,
                    "nodes": [
                        {"id": "n1", "type": "idea", "position": {"x": 0, "y": 0},
                         "data": {"nodeType": "idea", "prompt": "起步"}},
                        {"id": "n2", "type": "novel", "position": {"x": 300, "y": 0},
                         "data": {"nodeType": "novel", "prompt": "往下写"}},
                    ],
                    "edges": [],
                    "viewport": {},
                }
                canvas = Project(name="假画布", canvas_json=json.dumps(doc, ensure_ascii=False))
                db.add(canvas)
                await db.commit()
                await db.refresh(canvas)
                project = AgentProject(name="假项目", canvas_project_id=canvas.id)
                db.add(project)
                await db.commit()
                await db.refresh(project)
                self.canvas_id = canvas.id
                self.agent_project_id = project.id

        async def _get_db():
            async with self.maker() as db:
                yield db

        app.dependency_overrides[get_db] = _get_db
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()
        app.dependency_overrides.pop(get_db, None)
        agent_router._GOAL_RUNS.clear()
        agent_router._GOAL_PENDING.clear()
        await self.engine.dispose()

    # ---- 假任务执行器

    def install_fake_canvas(self):
        maker = self.maker

        async def fake_run_node(project_id: int, node_id: str, run_id: str):
            async with maker() as db:
                task = Task(kind="text", status="processing", model="fake",
                            prompt="假模型执行", canvas_project_id=project_id,
                            canvas_node_id=node_id, progress=10)
                db.add(task)
                await db.commit()
                await db.refresh(task)
                task_id = task.id
            asyncio.create_task(_complete_later(maker, task_id))
            return [type("T", (), {"id": task_id})()]

        def fake_start_full_graph(project_id: int, expected: int = 0):
            # 这个函数在真实代码里是同步的（只派任务、返回 run_id），假实现也必须同步
            conn = sqlite3.connect(self.db_path)
            try:
                ids = []
                for node_id in ("n1", "n2"):
                    cur = conn.execute(
                        "INSERT INTO tasks (kind, status, model, prompt, params_json, progress, "
                        "canvas_project_id, canvas_node_id) "
                        "VALUES ('text', 'processing', 'fake', '假模型执行', '{}', 10, ?, ?)",
                        (project_id, node_id),
                    )
                    ids.append(int(cur.lastrowid))
                conn.commit()
            finally:
                conn.close()
            for task_id in ids:
                asyncio.create_task(_complete_later(maker, task_id))
            return "fake-run"

        async def fake_preview(project_id: int):
            return {
                "totals": {"calls": 2, "pendingNodes": 2, "rerunNodes": 0,
                           "blockedNodes": 0, "dialogueCalls": 0},
                "nodes": [],
                "gate": {},
            }

        self._saved = (
            canvas_runner.run_single_node, canvas_runner.start_full_graph,
            canvas_runner.preview_graph,
        )
        canvas_runner.run_single_node = fake_run_node
        canvas_runner.start_full_graph = fake_start_full_graph
        canvas_runner.preview_graph = fake_preview

    def restore_fake_canvas(self):
        (
            canvas_runner.run_single_node, canvas_runner.start_full_graph,
            canvas_runner.preview_graph,
        ) = self._saved

    # ---- 会话

    async def new_session(self, *, with_canvas: bool = True, auto_approve: bool = True) -> int:
        body = {
            "model_key": "fake:model",
            "auto_approve": auto_approve,
            "thinking": False,
        }
        if with_canvas:
            body["agent_project_id"] = self.agent_project_id
        resp = await self.client.post("/api/agent/sessions", json=body)
        assert resp.status_code == 200, resp.text
        return resp.json()["session"]["id"]

    async def send(self, sid: int, content: str) -> dict:
        resp = await self.client.post(f"/api/agent/sessions/{sid}/messages",
                                      json={"content": content})
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def steps(self, sid: int) -> list[dict]:
        resp = await self.client.get(f"/api/agent/sessions/{sid}")
        assert resp.status_code == 200, resp.text
        return resp.json()["steps"]


def _install_model(policy) -> tuple[FakeAdapter, object]:
    adapter = FakeAdapter(policy)
    original = agent_runner.resolve_brain

    async def fake_resolve(db, session):
        return adapter, "fake", "Fake"

    agent_runner.resolve_brain = fake_resolve
    return adapter, original


def _restore_model(original) -> None:
    agent_runner.resolve_brain = original


def _run(coro_fn):
    asyncio.run(coro_fn())


# ================================================================ 场景


def test_goal_runs_the_whole_fake_canvas_until_done():
    """/goal：跑整图 → 等任务 → 收尾；不能误报「没有绑定画布」，也不能空转。"""

    async def case():
        async with Harness(with_canvas=True) as h:
            h.install_fake_canvas()

            def policy(used: int):
                if used == 0:
                    return _reply(name="run_canvas",
                                  args={"project_id": h.canvas_id, "ack_calls": 2})
                if used == 1:
                    return _reply(name="wait_for_tasks",
                                  args={"project_id": h.canvas_id, "timeout_seconds": 10})
                return _reply("全部跑完了：2 个节点完成。")

            adapter, original = _install_model(policy)
            try:
                sid = await h.new_session()
                body = await h.send(sid, "/goal 把假画布整条跑完")
                text = " ".join(s["content"] or "" for s in body["steps"])
                tools = [s["toolName"] for s in body["steps"] if s["role"] == "tool"]

                assert body["run"]["status"] == "done", body["run"]
                assert "run_canvas" in tools and "wait_for_tasks" in tools, tools
                assert "没有绑定画布" not in text
                assert "全部跑完了" in text
                assert adapter.calls <= 5, f"/goal 空转了 {adapter.calls} 次模型调用"
            finally:
                _restore_model(original)
                h.restore_fake_canvas()

    _run(case)


def test_goal_walks_a_multi_node_chain_one_step_at_a_time():
    """逐节点：跑 n1 → 等 → 跑 n2 → 等 → 收尾。"""

    async def case():
        async with Harness(with_canvas=True) as h:
            h.install_fake_canvas()

            def policy(used: int):
                if used == 0:
                    return _reply(name="run_node", args={"project_id": h.canvas_id, "node_id": "n1"})
                if used == 1:
                    return _reply(name="wait_for_tasks",
                                  args={"project_id": h.canvas_id, "timeout_seconds": 10})
                if used == 2:
                    return _reply(name="run_node", args={"project_id": h.canvas_id, "node_id": "n2"})
                if used == 3:
                    return _reply(name="wait_for_tasks",
                                  args={"project_id": h.canvas_id, "timeout_seconds": 10})
                return _reply("两个节点都完成了。")

            adapter, original = _install_model(policy)
            try:
                sid = await h.new_session()
                body = await h.send(sid, "/goal 逐个节点做完")
                tools = [s["toolName"] for s in body["steps"] if s["role"] == "tool"]
                assert body["run"]["status"] == "done", body["run"]
                assert tools.count("run_node") == 2, tools
                assert "两个节点都完成了" in " ".join(s["content"] or "" for s in body["steps"])
            finally:
                _restore_model(original)
                h.restore_fake_canvas()

    _run(case)


def test_spec_mode_blocks_writes_even_on_the_api_path():
    async def case():
        async with Harness(with_canvas=True) as h:
            def policy(used: int):
                if used == 0:
                    return _reply(name="create_project", args={"name": "不该真建"})
                return _reply("规范如下：……请确认。")

            _, original = _install_model(policy)
            try:
                sid = await h.new_session(with_canvas=False)
                body = await h.send(sid, "/spec 先写规范")
                blocked = [s for s in body["steps"]
                           if s["role"] == "tool" and s["toolName"] == "create_project"]
                assert blocked and blocked[0]["status"] == "error"
                assert "blocked_by_mode" in json.dumps(blocked[0]["toolResult"], ensure_ascii=False)

                async with h.maker() as db:
                    assert (await db.execute(select(Project))).scalars().all() != []  # 只有假画布
                    assert len((await db.execute(select(Project))).scalars().all()) == 1
            finally:
                _restore_model(original)

    _run(case)


def test_repeated_get_task_calls_are_blocked():
    async def case():
        async with Harness(with_canvas=False) as h:
            def policy(used: int):
                return _reply(name="get_task", args={"task_id": 999})

            _, original = _install_model(policy)
            try:
                sid = await h.new_session(with_canvas=False)
                body = await h.send(sid, "一直查任务 999")
                blocked = [s for s in body["steps"]
                           if s["role"] == "tool" and "同一个查询" in json.dumps(
                               s["toolResult"], ensure_ascii=False)]
                assert blocked, "重复查询没有被挡回"
                assert body["run"]["status"] == "max_rounds", body["run"]
            finally:
                _restore_model(original)

    _run(case)


def test_agent_reconsiders_a_failed_action_before_finishing():
    async def case():
        async with Harness(with_canvas=True) as h:
            responses = iter([
                _reply(name="update_node", args={"node_id": "missing", "prompt": "新开场"}),
                _reply("改好了。"),
                _reply(name="get_canvas"),
                _reply(name="update_node", args={"node_id": "n1", "prompt": "新开场"}),
                _reply(name="get_canvas"),
                _reply("已核对，开场节点已更新。"),
            ])
            adapter, original = _install_model(lambda _: next(responses))
            try:
                sid = await h.new_session()
                body = await h.send(sid, "把开场节点的提示词改为新开场")
                assert body["run"]["reply"] == "已核对，开场节点已更新。"
                assert body["run"]["writeCalls"] == 1
                assert adapter.calls == 6
                tools = [s for s in body["steps"] if s["role"] == "tool"]
                assert [(s["toolName"], s["status"]) for s in tools] == [
                    ("update_node", "error"), ("get_canvas", "ok"),
                    ("update_node", "ok"), ("get_canvas", "ok"),
                ]
                assert all(s["content"] != "改好了。" for s in body["steps"])
            finally:
                _restore_model(original)

    _run(case)


def test_successful_edit_allows_verifying_with_the_same_query():
    async def case():
        async with Harness(with_canvas=True) as h:
            responses = iter([
                _reply(),
                _reply(name="get_canvas"),
                _reply(name="get_canvas"),
                _reply(name="update_node", args={"node_id": "n1", "prompt": "新开场"}),
                _reply(name="get_canvas"),
                _reply("已核对。"),
            ])
            _, original = _install_model(lambda _: next(responses))
            try:
                sid = await h.new_session()
                body = await h.send(sid, "调整开场并检查结果")
                assert body["run"]["reply"] == "已核对。"
                reads = [s for s in body["steps"] if s["toolName"] == "get_canvas"]
                assert [s["status"] for s in reads] == ["ok", "ok", "ok"]
            finally:
                _restore_model(original)

    _run(case)


def test_stop_button_interrupts_a_wait_and_ends_the_goal():
    async def case():
        async with Harness(with_canvas=True) as h:
            h.install_fake_canvas()
            # 先放一条永远 processing 的任务，让 wait_for_tasks 卡住
            async with h.maker() as db:
                task = Task(kind="text", status="processing", model="fake",
                            prompt="永不完成", canvas_project_id=h.canvas_id,
                            canvas_node_id="n1", progress=10)
                db.add(task)
                await db.commit()

            def policy(used: int):
                if used == 0:
                    return _reply(name="wait_for_tasks",
                                  args={"project_id": h.canvas_id, "timeout_seconds": 60})
                return _reply("等到了。")

            _, original = _install_model(policy)
            try:
                sid = await h.new_session()
                request = asyncio.create_task(h.send(sid, "/goal 等这个任务"))
                for _ in range(40):
                    await asyncio.sleep(0.05)
                    if sid in agent_router._GOAL_RUNS:
                        break
                assert sid in agent_router._GOAL_RUNS, "goal 没有进入监测"
                stop = await h.client.post(f"/api/agent/sessions/{sid}/goal/stop")
                assert stop.status_code == 200
                body = await asyncio.wait_for(request, timeout=10)
                assert body["run"]["status"] == "cancelled", body["run"]
            finally:
                _restore_model(original)
                h.restore_fake_canvas()

    _run(case)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
