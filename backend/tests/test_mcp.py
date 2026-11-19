"""dsh sidecar 的 HTTP MCP 与阻塞式确认门（直接 python 运行）。

运行：venv/Scripts/python tests/test_mcp.py

守三件事：

1. **协议面**：initialize / tools/list / tools/call 的形状能被标准 MCP 客户端消费；
2. **确认门**：run 发起后挂起，界面批准才执行、拒绝/超时一条任务都不派；
3. **模型无法自批**：工具清单里没有任何「批准」类工具，批准只存在于界面 REST。

另外用静态断言钉住前端：全局轮询确认卡真的挂在外壳上，而不是只在 Agent 页里。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import AgentSession, AgentStep, Project  # noqa: E402
from app.routers import mcp  # noqa: E402
from app.services import agent_tools, mcp_gate  # noqa: E402

ROOT = BACKEND.parent
SRC = ROOT / "frontend" / "src"


def _run(fn):
    # 用临时文件而不是 :memory:：确认门会「挂起一个会话 + 另一个会话轮询」，
    # 内存库在 StaticPool 下只有一条连接，两个会话交错会把 refresh 打崩。
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            await fn(maker)
        finally:
            await engine.dispose()

    try:
        asyncio.run(scenario())
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


DOC = {
    "schemaVersion": 1,
    "nodes": [
        {"id": "n1", "type": "image", "position": {"x": 0, "y": 0},
         "data": {"nodeType": "image", "prompt": "一只猫", "model_key": "1:i"}},
    ],
    "edges": [],
    "viewport": {},
}


async def _project(maker, name: str = "MCP 测试") -> int:
    async with maker() as db:
        p = Project(name=name, canvas_json=json.dumps(DOC, ensure_ascii=False))
        db.add(p)
        await db.commit()
        await db.refresh(p)
        return p.id


async def _wait_pending(maker, want: int = 1, tries: int = 100) -> list[dict]:
    for _ in range(tries):
        async with maker() as db:
            items = await mcp_gate.pending(db)
        if len(items) >= want:
            return items
        await asyncio.sleep(0.01)
    raise AssertionError("确认门没有出现待确认项")


# ---------- 协议面 ----------


def test_initialize_and_tool_list_are_well_formed():
    async def scenario(_maker):
        init = await mcp._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        body = json.loads(init.body)
        assert body["result"]["protocolVersion"]
        assert "tools" in body["result"]["capabilities"]

        listing = await mcp._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = json.loads(listing.body)["result"]["tools"]
        names = {t["name"] for t in tools}
        assert names == {
            "xms_list_projects",
            "xms_get_canvas",
            "xms_summarize_canvas",
            "xms_preview_canvas",
            "xms_run_canvas",
        }
        # 模型不能自批：清单里没有任何「批准 / 确认」类工具
        assert not any("confirm" in n or "approve" in n for n in names)
        for tool in tools:
            assert tool["inputSchema"]["type"] == "object"
            assert tool["description"]

    _run(scenario)


def test_a_readonly_tool_returns_text_content():
    async def scenario(maker):
        pid = await _project(maker)
        original = mcp.SessionLocal
        mcp.SessionLocal = maker
        try:
            resp = await mcp._handle({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "xms_list_projects", "arguments": {}},
            })
        finally:
            mcp.SessionLocal = original
        body = json.loads(resp.body)["result"]
        assert body["isError"] is False
        payload = json.loads(body["content"][0]["text"])
        assert payload["ok"] is True
        assert [p["id"] for p in payload["data"]["projects"]] == [pid]

    _run(scenario)


def test_unknown_tool_is_an_error_not_a_crash():
    async def scenario(_maker):
        resp = await mcp._handle({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "xms_rm_rf", "arguments": {}},
        })
        body = json.loads(resp.body)["result"]
        assert body["isError"] is True
        assert "不存在" in body["content"][0]["text"]

    _run(scenario)


# ---------- 阻塞式确认门 ----------


def test_run_waits_and_executes_only_after_approval():
    async def scenario(maker):
        pid = await _project(maker)
        seen: list[dict] = []

        async def fake_preview(_pid):
            return {"totals": {"calls": 2, "rerunNodes": 1, "blockedNodes": 0}, "gate": {}}

        async def fake_execute(_ctx, name, args):
            seen.append({"name": name, "args": args})
            return {"ok": True, "data": {"run_id": "r1"}}

        old_preview, old_exec = mcp_gate.canvas_runner.preview_graph, agent_tools.execute
        mcp_gate.canvas_runner.preview_graph = fake_preview
        agent_tools.execute = fake_execute
        try:
            task = asyncio.create_task(mcp_gate.request_run(pid, timeout=5, maker=maker))
            items = await _wait_pending(maker)
            assert items[0]["calls"] == 2
            assert items[0]["rerun"] == 1
            assert items[0]["projectId"] == pid
            assert not seen, "确认之前就执行了"

            assert mcp_gate.resolve(items[0]["stepId"], True) is True
            outcome = await task
        finally:
            mcp_gate.canvas_runner.preview_graph = old_preview
            agent_tools.execute = old_exec

        assert outcome["ok"] is True
        assert seen and seen[0]["name"] == "run_canvas"
        # 预算闸按「确认时看到的次数」放行
        assert seen[0]["args"]["ack_calls"] == 2
        async with maker() as db:
            step = await db.get(AgentStep, items[0]["stepId"])
            assert step.status == "ok"

    _run(scenario)


def test_reject_and_timeout_do_not_execute():
    async def scenario(maker):
        pid = await _project(maker)
        calls: list[str] = []

        async def fake_preview(_pid):
            return {"totals": {"calls": 1}, "gate": {}}

        async def fake_execute(*_a, **_k):
            calls.append("ran")
            return {"ok": True}

        old_preview, old_exec = mcp_gate.canvas_runner.preview_graph, agent_tools.execute
        mcp_gate.canvas_runner.preview_graph = fake_preview
        agent_tools.execute = fake_execute
        try:
            task = asyncio.create_task(mcp_gate.request_run(pid, timeout=5, maker=maker))
            items = await _wait_pending(maker)
            mcp_gate.resolve(items[0]["stepId"], False)
            rejected = await task

            timed_out = await mcp_gate.request_run(pid, timeout=0.05, maker=maker)
        finally:
            mcp_gate.canvas_runner.preview_graph = old_preview
            agent_tools.execute = old_exec

        assert rejected["ok"] is False and "拒绝" in rejected["error"]
        assert timed_out["ok"] is False and "没有执行" in timed_out["error"]
        assert calls == [], "拒绝或超时之后仍然执行了"

        async with maker() as db:
            rows = (await db.execute(
                AgentStep.__table__.select().where(AgentStep.tool_name == "run_canvas")
            )).all()
            statuses = sorted(r.status for r in rows)
            assert statuses == ["rejected", "timeout"], statuses

    _run(scenario)


def test_the_sidecar_session_is_reused_per_canvas():
    """同一张画布多次发起确认复用一条「dsh 高级模式」会话，不把工作区刷满。"""

    async def scenario(maker):
        pid = await _project(maker)

        async def fake_preview(_pid):
            return {"totals": {"calls": 0}, "gate": {}}

        old_preview = mcp_gate.canvas_runner.preview_graph
        mcp_gate.canvas_runner.preview_graph = fake_preview
        try:
            for _ in range(2):
                task = asyncio.create_task(mcp_gate.request_run(pid, timeout=5, maker=maker))
                items = await _wait_pending(maker)
                mcp_gate.resolve(items[0]["stepId"], False)
                await task
        finally:
            mcp_gate.canvas_runner.preview_graph = old_preview

        async with maker() as db:
            sessions = (await db.execute(
                AgentSession.__table__.select().where(AgentSession.title == mcp_gate.SIDECAR_TITLE)
            )).all()
        assert len(sessions) == 1, [s.title for s in sessions]

    _run(scenario)


def test_confirmation_api_reports_finished_confirmations():
    """界面重复点、或超时之后才点，都只能得到 409，不会二次执行。"""
    from types import SimpleNamespace

    from fastapi import HTTPException

    local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    try:
        asyncio.run(mcp.confirm(99999, mcp.ConfirmationIn(approve=True), local))
    except HTTPException as e:
        assert e.status_code == 409
    else:
        raise AssertionError("不存在的确认项应该返回 409")


def test_mcp_refuses_non_loopback_clients():
    """P1 没有 token：MCP 与确认接口都不能跟着 HOST 暴露到局域网。"""
    from types import SimpleNamespace

    from fastapi import HTTPException

    far = SimpleNamespace(client=SimpleNamespace(host="192.168.1.20"))
    try:
        mcp._require_local(far)
    except HTTPException as e:
        assert e.status_code == 403
    else:
        raise AssertionError("非本机连接应该被拒绝")


# ---------- 前端静态检查 ----------


def test_frontend_polls_confirmations_globally():
    app = (SRC / "App.tsx").read_text(encoding="utf-8")
    gate = (SRC / "components" / "McpGate.tsx").read_text(encoding="utf-8")
    api = (SRC / "api.ts").read_text(encoding="utf-8")
    assert "McpGate" in app and "<McpGate />" in app, "确认卡没有挂到应用外壳上"
    assert "mcpConfirmations:" in api and "mcpConfirm:" in api
    assert "api.mcpConfirmations()" in gate and "api.mcpConfirm(" in gate
    assert "setTimeout" in gate, "没有轮询"


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
