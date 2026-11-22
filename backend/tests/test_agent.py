"""Agent 工作台（2.0）的行为测试：工具清单、历史重建、草稿落定的文档形状、接线。

运行：venv/Scripts/python tests/test_agent.py

不跑真模型：这条链上最贵、最不稳定的一段恰好也是「最难在单测里断言」的一段。
这里守的是**确定性**的三块——工具契约、上下文重建、写库形状；真机走查单独做。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import AgentSession, AgentStep, Project, ProviderService  # noqa: E402
from app.registry.canvas_nodes import NODE_SCHEMAS, validate_document  # noqa: E402
from app.services import agent_runner, agent_tools  # noqa: E402


# ================================================================ 1. 工具契约


def test_tool_names_and_tiers_are_well_formed():
    """工具名唯一、权限级别合法、参数是对象、不可逆的必须有预览。"""
    names = [t.name for t in agent_tools.TOOLS]
    assert len(names) == len(set(names)), names
    assert 8 <= len(names) <= 28, f"工具数量不在合理区间：{len(names)}"
    for t in agent_tools.TOOLS:
        assert t.tier in (agent_tools.TIER_READ, agent_tools.TIER_WRITE,
                          agent_tools.TIER_IRREVERSIBLE), t.name
        assert t.parameters.get("type") == "object", t.name
        assert t.description and len(t.description) > 8, t.name
        if t.tier == agent_tools.TIER_IRREVERSIBLE:
            assert t.preview is not None, f"{t.name} 不可逆却没有代价预览"


def test_the_tool_set_covers_the_canvas_loop():
    """画布这条线上，读 / 建 / 草稿 / 落定 / 改 / 连线 / 执行 一个都不能少。"""
    names = {t.name for t in agent_tools.TOOLS}
    for required in (
        "list_agent_projects", "create_agent_project",
        "list_projects", "get_canvas", "summarize_canvas", "preview_canvas", "lint_canvas",
        "create_project", "draft_canvas", "apply_canvas_draft", "update_node", "connect_nodes",
        "add_storyboard_image",
        "run_canvas", "run_node", "canvas_status", "wait_for_tasks",
        "list_skills", "load_skill", "get_task", "get_asset", "list_engines",
        "list_prompt_templates",
    ):
        assert required in names, f"缺少工具 {required}"


def test_storyboard_image_tool_is_reversible_and_needs_a_source():
    """铺分镜图是可撤销工具（不是花钱操作），但必须指明接在哪个上游后面。"""
    spec = agent_tools.by_name("add_storyboard_image")
    assert spec is not None, "没有「铺分镜图」这个工具"
    assert spec.tier == agent_tools.TIER_WRITE, "铺节点不该是不可逆操作"
    assert spec.parameters["required"] == ["source_node_id"]
    assert "storyboardImage" in (agent_runner.build_system_prompt(
        AgentSession(id=1, title="t", model_key="")
    )), "系统提示词的节点类型表里没有分镜图"


# ================================================================ 1b. 技能（渐进披露）


def test_skills_have_metadata_and_body():
    """一个技能至少要有 name/description（第一层）与正文（第二层），否则等于没有。"""
    from app.services import agent_skills

    items = agent_skills.load_all()
    assert len(items) >= 3, [s["key"] for s in items]
    keys = [s["key"] for s in items]
    assert len(keys) == len(set(keys))
    for s in items:
        assert s["name"] and s["description"], s["key"]
        assert len(s["body"]) > 200, f"{s['key']} 正文太短，等于没写"
        # 描述要写清「什么时候用」，否则模型判断不了相关性
        assert "时" in s["description"] or "先加载" in s["description"], s["description"]


def test_skill_lookup_and_prompt_block():
    from app.services import agent_skills

    first = agent_skills.metadata()[0]
    got = agent_skills.get(first["key"])
    assert got and got["body"]
    assert agent_skills.get("no_such_skill") is None

    block = agent_skills.prompt_block()
    for item in agent_skills.metadata():
        assert item["key"] in block and item["name"] in block
    # 正文不进提示词（那是第二层），否则就不是渐进披露了
    assert agent_skills.metadata()[0]["description"] in block


def test_load_skill_is_safe_and_actionable():
    """未知技能要报错并**列出可选项**；存在时返回正文（超长截断）。"""
    import asyncio

    ok = asyncio.run(agent_tools.execute(None, "load_skill", {"key": "short-video"}))  # type: ignore[arg-type]
    assert ok["ok"] is True
    assert ok["data"]["body"] and "短视频" in ok["data"]["body"]

    bad = asyncio.run(agent_tools.execute(None, "load_skill", {"key": "nope"}))  # type: ignore[arg-type]
    assert bad["ok"] is False
    assert "short-video" in bad["hint"], bad["hint"]


def test_openai_schema_shape():
    """下发给模型的就是标准的 function schema：名称、描述、JSON Schema 三件。"""
    for item in agent_tools.openai_tools():
        assert item["type"] == "function"
        fn = item["function"]
        assert fn["name"] and fn["description"]
        assert fn["parameters"]["type"] == "object"


def test_the_system_prompt_says_the_hard_rules():
    """提示词里必须写清「不可逆要人点」「事实只信工具」「只调一个工具」——这三条是护栏。"""
    text = agent_runner.build_system_prompt(
        AgentSession(id=1, title="t", model_key="", canvas_project_id=7)
    )
    assert "run_canvas" in text and "确认" in text
    assert "只信工具返回" in text
    assert "每次只调一个工具" in text
    assert "wait_for_tasks" in text, "提示词没教它用等待工具，它会退回连点 get_task"
    assert "id=7" in text, "绑定画布必须写在提示词里，否则模型每次都要先猜"
    # 技能的第一层（名字 + 描述）必须常驻，否则模型不知道有 load_skill 可调
    assert "可用技能" in text and "load_skill" in text


def test_a_reply_that_announces_the_next_step_is_treated_as_unfinished():
    """「我现在去做体检。」然后收尾 = 最像暂停的一种失败，得能认出来并替它续跑。

    认的范围要窄：把下一步交给用户（「你可以…」）或征求意见（「要不要…？」）不算。
    """
    assert agent_runner.looks_like_unfinished_plan(
        "画布已经落定好了（id=22，8 节点 / 7 连线），还没跑任何东西。我现在做零成本体检。"
    )
    assert agent_runner.looks_like_unfinished_plan("落定了：8 节点、7 连线。先做零成本体检。")
    assert agent_runner.looks_like_unfinished_plan("草稿没问题，我现在开始逐镜补跑。")

    assert not agent_runner.looks_like_unfinished_plan("接下来你可以点运行整图。")
    assert not agent_runner.looks_like_unfinished_plan("要不要我继续跑？")
    assert not agent_runner.looks_like_unfinished_plan("已经跑完了，结果在任务中心。")
    assert not agent_runner.looks_like_unfinished_plan("")
    # 续跑有上限，不能变成死循环
    assert 1 <= agent_runner.MAX_NUDGES <= 3
    assert "不要只说明计划" in agent_runner.NUDGE_TEXT


def test_wait_for_tasks_returns_as_soon_as_the_task_finishes():
    """等任务工具必须看到别的连接写进去的新状态（SQLite 读快照不能一直不更新）。"""
    import asyncio
    import tempfile
    import time
    from pathlib import Path as _Path

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import Base
    from app.models import Task
    from app.services.agent_tools import ToolContext, _wait_for_tasks

    async def case():
        url = f"sqlite+aiosqlite:///{_Path(tempfile.mkdtemp(), 'wait.db').as_posix()}"
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as reader, maker() as writer:
                task = Task(kind="text", status="processing", model="2:x", prompt="x",
                            progress=10, canvas_project_id=22)
                sess = AgentSession(title="t", model_key="", canvas_project_id=22)
                reader.add_all([task, sess])
                await reader.commit()
                await reader.refresh(task)
                await reader.refresh(sess)
                task_id = task.id  # 后面 reader 会结束事务，先把 id 拿住

                async def finish():
                    await asyncio.sleep(1.2)
                    row = await writer.get(Task, task_id)
                    row.status = "completed"
                    row.progress = 100
                    await writer.commit()

                finisher = asyncio.create_task(finish())
                ctx = ToolContext(db=reader, session=sess)
                started = time.monotonic()
                out = await _wait_for_tasks(ctx, {"task_ids": [task_id], "timeout_seconds": 10})
                elapsed = time.monotonic() - started
                await finisher

                assert out["data"]["done"] is True
                assert out["data"]["tasks"][0]["status"] == "completed"
                assert elapsed >= 2, "没等到就返回了：读事务没结束，看到的是旧快照"
                # 等完之后调用方还要接着用 session（add_step 会读 session.id）：
                # 结束事务不能用 rollback，否则对象被 expire，下一句就是 MissingGreenlet
                assert sess.canvas_project_id == 22
        finally:
            await engine.dispose()

    asyncio.run(case())


def test_wait_for_tasks_follows_a_graph_run_that_is_still_dispatching():
    """整图是边跑边派下游的：任务表暂时空了，不代表这一跑结束。

    实测（2026-11-21 真机全流程）：第一个节点跑完的瞬间任务表是空的，
    wait_for_tasks 立刻回报 done，模型就以为整条链跑完了。项目级等待必须跟着
    这一跑的活跃状态走；只是一直活跃时，到点要如实回 still_dispatching。
    """
    import asyncio
    import tempfile
    from pathlib import Path as _Path

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import Base
    from app.models import AgentSession
    from app.services import canvas_runner
    from app.services.agent_tools import ToolContext, _wait_for_tasks

    async def case():
        url = f"sqlite+aiosqlite:///{_Path(tempfile.mkdtemp(), 'wait_run.db').as_posix()}"
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        original = canvas_runner.active_run_for
        try:
            async with maker() as db:
                sess = AgentSession(title="t", model_key="", canvas_project_id=22)
                db.add(sess)
                await db.commit()
                await db.refresh(sess)

                # 一跑还在派下游（任务表为空）→ 必须继续等，而不是立刻 done
                calls = {"n": 0}

                def active_then_done(pid: int) -> str:
                    calls["n"] += 1
                    return "run-x" if calls["n"] <= 2 else ""

                canvas_runner.active_run_for = active_then_done
                out = await _wait_for_tasks(
                    ToolContext(db=db, session=sess),
                    {"project_id": 22, "timeout_seconds": 8},
                )
                assert out["data"]["done"] is True
                assert out["data"]["still_dispatching"] is False
                assert out["data"]["waited_ms"] >= 3000, "没跟着整图等，提前回报了"

                # 一直活跃：到点要如实说还在派，而不是假装跑完
                canvas_runner.active_run_for = lambda pid: "run-x"
                timed = await _wait_for_tasks(
                    ToolContext(db=db, session=sess),
                    {"project_id": 22, "timeout_seconds": 5},
                )
                assert timed["data"]["timed_out"] is True
                assert timed["data"]["still_dispatching"] is True
                assert timed["data"]["done"] is False
        finally:
            canvas_runner.active_run_for = original
            await engine.dispose()

    asyncio.run(case())


def test_goal_mode_does_not_tell_the_user_to_use_goal_again_on_round_limit():
    """轮数上限那句「请用 /goal 重发一次」只在普通模式成立。

    /goal 已经会持续推进，用户在监测中看到这句话会以为它停了（真机对照测试里就出现了）。
    """
    import asyncio
    import tempfile
    from pathlib import Path as _Path

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import Base
    from app.models import AgentSession, AgentStep
    from app.services import agent_runner

    class FakeAdapter:
        def supports_thinking(self, model: str) -> bool:
            return False

        async def chat_once(self, model, messages, tools=None, thinking=None):
            # 一直调只读工具：刚好把 MAX_ROUNDS 走满
            return {
                "content": "",
                "reasoning": "",
                "tool_calls": [{"name": "list_projects", "arguments": "{}"}],
            }

        async def close(self):
            pass

    adapter = FakeAdapter()

    async def fake_resolve(db, session):
        return adapter, "fake-model", "Fake"

    async def case():
        url = f"sqlite+aiosqlite:///{_Path(tempfile.mkdtemp(), 'goal_rounds.db').as_posix()}"
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        original = agent_runner.resolve_brain
        agent_runner.resolve_brain = fake_resolve
        try:
            async with maker() as db:
                session = AgentSession(title="t", model_key="2:x", thinking=False,
                                       canvas_project_id=22, agent_project_id=None)
                db.add(session)
                await db.commit()
                await db.refresh(session)

                out = await agent_runner.run_loop(db, session, mode="goal")
                assert out["status"] == "max_rounds"

                rows = (await db.execute(
                    select(AgentStep).where(AgentStep.session_id == session.id)
                )).scalars().all()
                notes = [r.content for r in rows if r.role == "assistant" and r.content]
                assert all("/goal" not in text for text in notes), (
                    f"目标模式到头还在让用户重发 /goal：{notes}"
                )
        finally:
            agent_runner.resolve_brain = original
            await engine.dispose()

    asyncio.run(case())


def test_goal_monitor_count_check_does_not_expire_the_session():
    """/goal 的「还有几个任务在跑」也不能把调用方的 session 过期，否则下一句就炸。"""
    import asyncio
    import tempfile
    from pathlib import Path as _Path

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import Base

    async def case():
        url = f"sqlite+aiosqlite:///{_Path(tempfile.mkdtemp(), 'goal.db').as_posix()}"
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as db:
                sess = AgentSession(title="t", model_key="", canvas_project_id=22)
                db.add(sess)
                await db.commit()
                await db.refresh(sess)

                count = await agent_runner._running_tasks(db, 22)
                assert count == 0
                # 模拟监测循环结束后的下一步：读 session.id 不能触发 IO
                assert sess.id and sess.canvas_project_id == 22
        finally:
            await engine.dispose()

    asyncio.run(case())


def test_slash_commands_are_parsed_from_the_message_head():
    """`/spec` `/plan` `/goal` 只认开头；不认识的斜杠原样当普通消息。"""
    assert agent_runner.parse_command("/goal 做一条 10 秒视频") == ("goal", "做一条 10 秒视频")
    assert agent_runner.parse_command("  /Spec 先写规范") == ("spec", "先写规范")
    assert agent_runner.parse_command("/plan") == ("plan", "")
    assert agent_runner.parse_command("/unknown x") == ("", "/unknown x")
    assert agent_runner.parse_command("普通消息") == ("", "普通消息")


def test_each_mode_writes_its_own_rule_into_the_system_prompt():
    base = AgentSession(id=1, title="t", model_key="")
    assert "/spec 模式" in agent_runner.build_system_prompt(base, mode="spec")
    assert "/plan 模式" in agent_runner.build_system_prompt(base, mode="plan")
    assert "/goal 模式" in agent_runner.build_system_prompt(base, mode="goal")
    assert "当前模式" not in agent_runner.build_system_prompt(base, mode="")


def test_the_system_prompt_scopes_the_context_to_the_agent_project():
    """项目要写进提示词：不同项目的会话不能互相看见对方的进展，模型也不该假装知道。"""
    from app.models import AgentProject

    session = AgentSession(id=1, title="t", model_key="", canvas_project_id=None, agent_project_id=5)
    project = AgentProject(id=5, name="短剧 A · 前期", description="只做选角与设定", canvas_project_id=9)
    text = agent_runner.build_system_prompt(session, project)
    assert "短剧 A · 前期" in text and "id=5" in text
    assert "只做选角与设定" in text
    assert "id=9" in text, "画布应从项目继承"
    assert "别的项目" in text

    # 未归档会话要说清自己是未归档，而不是假装有项目
    loose = agent_runner.build_system_prompt(AgentSession(id=2, title="t", model_key=""))
    assert "未归档" in loose


# ================================================================ 1c. 审批方式（手动 / 自动）


def test_irreversible_tools_need_a_human_unless_auto_approved():
    """默认手动：不可逆必须停下；自动审批才放行。只读/可撤销本来就不该拦。"""
    assert agent_runner.needs_human_approval("irreversible", False) is True
    assert agent_runner.needs_human_approval("irreversible", True) is False
    assert agent_runner.needs_human_approval("write", False) is False
    assert agent_runner.needs_human_approval("read", True) is False


def test_the_system_prompt_matches_the_approval_mode():
    """提示词必须与真实行为一致：自动审批时还说「让用户点确认」，模型会一直等一个不会出现的弹窗。"""
    manual = agent_runner.build_system_prompt(
        AgentSession(id=1, title="t", model_key="", auto_approve=False)
    )
    auto = agent_runner.build_system_prompt(
        AgentSession(id=1, title="t", model_key="", auto_approve=True)
    )
    assert "让用户点确认" in manual
    assert "自动审批" in auto and "直接执行" in auto
    assert "让用户点确认" not in auto


# ================================================================ 2. 上下文重建


def _step(**kw) -> AgentStep:
    base = dict(id=1, session_id=1, seq=1, role="assistant", content="", tool_name="",
                tool_args="", tool_result="", tier="", status="ok", duration_ms=0, tokens=0)
    base.update(kw)
    return AgentStep(**base)


def test_history_rebuilds_tool_pairs_from_steps():
    """一次工具调用要还原成 `assistant(tool_calls)` + `tool(result)` 两条，id 稳定可复现。"""
    session = AgentSession(id=1, title="t", model_key="", canvas_project_id=None)
    rows = [
        _step(id=1, seq=1, role="user", content="看看画布"),
        _step(id=2, seq=2, role="tool", tool_name="list_projects", tool_args="{}",
              tool_result=json.dumps({"ok": True, "data": {"projects": []}}), tier="read"),
    ]
    msgs = agent_runner.messages_from_steps(session, rows)
    assert msgs[0]["role"] == "system"
    assert msgs[1] == {"role": "user", "content": "看看画布"}
    assert msgs[2]["role"] == "assistant"
    call = msgs[2]["tool_calls"][0]
    assert call["id"] == "call_2" and call["function"]["name"] == "list_projects"
    # 带 tool_calls 的 assistant 消息**不能是空 content**：KIE 这类网关会直接拒绝
    # （Message content is blank），而那是 HTTP 200 里的业务错误，很难看出来
    assert str(msgs[2].get("content") or "").strip(), msgs[2]
    assert msgs[3]["role"] == "tool" and msgs[3]["tool_call_id"] == "call_2"
    assert json.loads(msgs[3]["content"])["ok"] is True


def test_pending_steps_are_not_fed_back_and_rejected_ones_are():
    """等待确认的那步不进上下文；被拒绝的要变成一条「用户拒绝」的结果（让模型换做法）。"""
    session = AgentSession(id=1, title="t", model_key="", canvas_project_id=None)
    pending = _step(id=3, seq=3, role="tool", tool_name="run_canvas", tool_args="{}",
                    tool_result="{}", tier="irreversible", status="pending")
    msgs = agent_runner.messages_from_steps(session, [pending])
    assert len(msgs) == 1, "pending 不该出现在上下文里"

    rejected = _step(id=4, seq=4, role="tool", tool_name="run_canvas", tool_args="{}",
                     tool_result="{}", tier="irreversible", status="rejected")
    msgs = agent_runner.messages_from_steps(session, [rejected])
    tool_msg = next(m for m in msgs if m["role"] == "tool")
    assert "用户拒绝" in tool_msg["content"]


def test_long_tool_results_are_clipped_in_context():
    """大结果只把前 4000 字给模型（指针代替载荷），但库里仍然留着全文。"""
    session = AgentSession(id=1, title="t", model_key="", canvas_project_id=None)
    big = "x" * 9000
    step = _step(id=5, seq=5, role="tool", tool_name="get_canvas", tool_args="{}",
                 tool_result=big, tier="read")
    msgs = agent_runner.messages_from_steps(session, [step])
    tool_msg = next(m for m in msgs if m["role"] == "tool")
    assert len(tool_msg["content"]) < len(big)
    assert "截断" in tool_msg["content"]


# ================================================================ 3. 草稿落定的文档形状


def _draft() -> dict:
    return {
        "nodes": [
            {"id": "n1", "kind": "idea", "data": {"prompt": "外卖侠", "shotCount": 8},
             "position": {"x": 0, "y": 0}},
            {"id": "n2", "kind": "storyboard", "data": {"prompt": ""},
             "position": {"x": 320, "y": 0}},
        ],
        "edges": [{"from": "n1", "to": "n2"}],
    }


class _Project:
    canvas_json = None
    name = "测试"


def test_draft_becomes_a_valid_canvas_document():
    """落库形状必须与前端保存的一致：`type` 是真实节点类型，`schema` 不落库，边缘带 handle。"""
    doc = agent_tools._doc_from_draft(
        _draft(), _Project(), {"idea": "2:deepseek-chat", "storyboard": "2:deepseek-chat"}
    )
    assert {n["type"] for n in doc["nodes"]} == {"idea", "storyboard"}
    for n in doc["nodes"]:
        assert n["data"]["nodeType"] == n["type"]
        assert "schema" not in n["data"], "运行时缓存不该进库"
        assert n["data"]["model_key"] == "2:deepseek-chat"
    assert doc["edges"][0]["sourceHandle"] == NODE_SCHEMAS["idea"]["handles"]["sources"][0]["id"]
    assert doc["edges"][0]["targetHandle"] == NODE_SCHEMAS["storyboard"]["handles"]["targets"][0]["id"]
    nodes, edges = validate_document(doc)
    assert len(nodes) == 2 and len(edges) == 1


def test_appending_a_draft_shifts_it_right_of_existing_content():
    """第二次落草稿不能压在旧节点上（用户看到的是「画布被搞乱了」）。"""
    class _Busy:
        canvas_json = json.dumps({
            "schemaVersion": 1,
            "nodes": [{"id": "old", "type": "text", "position": {"x": 1200, "y": 0}, "data": {}}],
            "edges": [],
            "viewport": {},
        })
        name = "测试"

    doc = agent_tools._doc_from_draft(
        _draft(), _Busy(), {"idea": "", "storyboard": ""}
    )
    new_nodes = [n for n in doc["nodes"] if n["id"] != "old"]
    assert all(n["position"]["x"] > 1200 for n in new_nodes), [n["position"] for n in new_nodes]


def test_a_draft_with_unknown_kinds_is_dropped_not_crashing():
    draft = {
        "nodes": [{"id": "n1", "kind": "no_such_kind", "data": {}, "position": {"x": 0, "y": 0}}],
        "edges": [],
    }
    doc = agent_tools._doc_from_draft(draft, _Project(), {})
    assert doc["nodes"] == []


# ================================================================ 4. 接线


def test_the_router_and_page_are_wired():
    main = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    api = (BACKEND.parent / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
    page = (BACKEND.parent / "frontend" / "src" / "pages" / "AgentPage.tsx").read_text(
        encoding="utf-8"
    )
    assert "agent.router" in main, "路由没注册"
    assert '"/api/agent/meta"' in api and "/api/agent/sessions" in api
    assert "/api/agent/projects" in api, "项目两个接口没接"
    assert "agentSend" in api and "agentConfirm" in api
    assert "还没做" not in page, "占位页文案还在，说明页面没换掉"
    assert "确认执行" in page, "缺少不可逆操作的确认入口"
    assert "新任务" in page, "缺任务入口"
    assert "技能（按需加载" in page, "能力面板里没有技能分组"
    assert "自动审批" in page and "手动审批" in page, "缺少审批方式切换"
    assert "auto_approve" in api and "autoApprove" in page, "审批方式没接进接口/类型"

    migration = BACKEND / "alembic" / "versions" / "0018_agent_tables.py"
    assert migration.exists(), "缺少会话/步骤两张表的迁移"
    text = migration.read_text(encoding="utf-8")
    assert "agent_sessions" in text and "agent_steps" in text

    projects = BACKEND / "alembic" / "versions" / "0019_agent_projects.py"
    assert projects.exists(), "缺少 Agent 项目表的迁移"
    ptext = projects.read_text(encoding="utf-8")
    assert "agent_projects" in ptext and "agent_project_id" in ptext

    approval = BACKEND / "alembic" / "versions" / "0020_agent_auto_approve.py"
    assert approval.exists(), "缺少审批方式的迁移"
    assert "auto_approve" in approval.read_text(encoding="utf-8")


# ================================================================ 12. 换模型


def _agent_db_run(scenario) -> None:  # noqa: ANN001
    async def go() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            async with maker() as db:
                await scenario(db)
        finally:
            await engine.dispose()

    asyncio.run(go())


def _video_canvas(ark_key: str) -> str:
    return json.dumps(
        {
            "schemaVersion": 1,
            "nodes": [
                {
                    "id": "n6",
                    "type": "video",
                    "position": {"x": 0, "y": 0},
                    "data": {"prompt": "一个人在说话", "model_key": ark_key, "duration": 5},
                }
            ],
            "edges": [],
            "viewport": {},
        },
        ensure_ascii=False,
    )


def test_agent_can_switch_a_nodes_model():
    """`update_node` 必须能换模型——不然 Agent 只能让用户回画布面板手点。

    顺带守住时长：3 秒写进只认 5/10 的 Seedance 1.0 要被拦住，不能悄悄落库。
    """

    async def scenario(db) -> None:  # noqa: ANN001
        ark = ProviderService(
            name="方舟", kind="ark", base_url="https://ark.cn-beijing.volces.com/api/v3",
            models_json=json.dumps([
                {"name": "doubao-seedance-1-0-pro-250528", "modality": "video", "label": "Seedance 1.0"},
            ]),
        )
        kie = ProviderService(
            name="KIE", kind="kie", base_url="https://api.kie.ai",
            models_json=json.dumps([
                {"name": "bytedance/seedance-2-5", "modality": "video", "label": "Seedance 2.5"},
            ]),
        )
        db.add_all([ark, kie])
        await db.commit()
        await db.refresh(ark)
        await db.refresh(kie)

        project = Project(name="p", canvas_json=_video_canvas(f"{ark.id}:doubao-seedance-1-0-pro-250528"))
        db.add(project)
        await db.commit()
        await db.refresh(project)
        session = AgentSession(id=1, title="t", model_key="", canvas_project_id=project.id)
        ctx = agent_tools.ToolContext(db=db, session=session)

        bad = await agent_tools.execute(ctx, "update_node", {
            "project_id": project.id, "node_id": "n6", "params": {"duration": 3},
        })
        assert bad["ok"] is False, bad
        assert "5–10" in bad["error"], bad

        got = await agent_tools.execute(ctx, "update_node", {
            "project_id": project.id, "node_id": "n6",
            "model_key": f"{kie.id}:bytedance/seedance-2-5",
            "params": {"duration": 20},
        })
        assert got["ok"] is True, got
        data = json.loads(project.canvas_json)["nodes"][0]["data"]
        assert data["model_key"] == f"{kie.id}:bytedance/seedance-2-5", data
        assert data["duration"] == 20, data

        missing = await agent_tools.execute(ctx, "update_node", {
            "project_id": project.id, "node_id": "n6", "model_key": "999:not-exist",
        })
        assert missing["ok"] is False, missing
        assert "list_models" in (missing.get("hint") or ""), missing

    _agent_db_run(scenario)


def test_applied_draft_keeps_an_explicit_model():
    """草稿里指明模型就用它；没指明才回落到默认模型。"""
    from app.services.agent_tools import _doc_from_draft

    project = Project(id=1, name="p", canvas_json=None)
    draft = {
        "nodes": [
            {"id": "n1", "kind": "video",
             "data": {"prompt": "x", "model_key": "8:bytedance/seedance-2-5", "duration": 20}},
            {"id": "n2", "kind": "video", "data": {"prompt": "y"}},
        ],
        "edges": [],
    }
    doc = _doc_from_draft(draft, project, {"video": "1:doubao-seedance-1-0-pro-250528"})
    a, b = (n["data"] for n in doc["nodes"])
    assert a["model_key"] == "8:bytedance/seedance-2-5", a
    assert a["duration"] == 20, a
    assert b["model_key"] == "1:doubao-seedance-1-0-pro-250528", b


def test_update_node_exposes_model_key_in_its_schema():
    spec = agent_tools.by_name("update_node")
    assert spec is not None
    assert "model_key" in spec.parameters["properties"], spec.parameters
    assert "model_key" in spec.description, "工具描述里不写，模型就不会用"


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
