"""Agent 思考模式：开关能不能用、思维链怎么回传、上游到底认不认这个参数。

三类断言各自守着一条实测结论（2026-11-20 用库里的 DeepSeek Key 真跑过）：

1. **默认是开的**：DeepSeek V4 不传 `thinking` 就已经在思考，所以「关掉」必须
   显式下发 `disabled`，不能靠省略参数。
2. **带 tools 的请求必须回传历史 `reasoning_content`**：漏了直接 400
   （error: `reasoning_content` ... must be passed back to the API）。所以
   `messages_from_steps` 要把落库的 reasoning 原样拼回去。
3. **别家不下发**：`thinking` 是 DeepSeek 家的入参形状，发给不认识的端点会 400，
   所以判据与界面开关共用同一份（`provider_store.supports_thinking`）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402

from app.models import AgentSession, AgentStep, ProviderService  # noqa: E402
from app.providers.openai_compat import OpenAICompatAdapter  # noqa: E402
from app.services.agent_runner import messages_from_steps  # noqa: E402
from app.services.provider_store import supports_thinking  # noqa: E402


def _session() -> AgentSession:
    s = AgentSession(id=1, title="t", model_key="", canvas_project_id=None, agent_project_id=None)
    s.auto_approve = False
    s.thinking = True
    return s


def _step(**kw) -> AgentStep:
    base = dict(id=1, session_id=1, seq=1, role="assistant", content="", tool_name="",
                tool_args="", tool_result="", tier="", status="ok", duration_ms=0,
                tokens=0, reasoning="")
    base.update(kw)
    return AgentStep(**base)


def test_supports_thinking_only_for_deepseek_endpoints():
    deepseek = ProviderService(name="DeepSeek", kind="openai", base_url="https://api.deepseek.com/v1")
    other = ProviderService(name="某中转", kind="openai", base_url="https://api.example.com/v1")
    ark = ProviderService(name="方舟", kind="ark", base_url="https://ark.cn-beijing.volces.com/api/v3")
    assert supports_thinking(deepseek, "deepseek-v4-flash") is True
    assert supports_thinking(other, "deepseek-v4-flash") is True  # 按模型名也认
    assert supports_thinking(other, "gpt-4o") is False
    # 方舟即使填了 deepseek 字样也不开：类型不是 OpenAI 兼容，入参形状不一样
    assert supports_thinking(ark, "deepseek-v4-flash") is False


def test_history_replays_reasoning_content_for_tool_turns():
    """带工具调用的一轮：assistant 正文与「发起调用」那个回合都要带 reasoning_content。"""
    rows = [
        _step(id=1, seq=1, role="assistant", content="我先读一下画布。", reasoning="用户要看画布，先 get_canvas。"),
        _step(id=2, seq=2, role="tool", tool_name="get_canvas", tool_args='{"project_id": 1}',
              tool_result='{"ok": true}', tier="read", reasoning="用户要看画布，先 get_canvas。"),
        _step(id=3, seq=3, role="tool", tool_name="list_projects", tool_args="{}",
              tool_result='{"ok": true}', tier="read", reasoning=""),
    ]
    msgs = messages_from_steps(_session(), rows)
    assistants = [m for m in msgs if m["role"] == "assistant"]
    assert assistants[0]["content"] == "我先读一下画布。"
    assert assistants[0]["reasoning_content"] == "用户要看画布，先 get_canvas。"
    # 第二个是 tool 步合成出来的「发起调用」回合
    assert assistants[1]["tool_calls"][0]["function"]["name"] == "get_canvas"
    assert assistants[1]["reasoning_content"] == "用户要看画布，先 get_canvas。"
    # 同一次响应里后面的调用不重复挂（只有第一个回合需要）
    assert "reasoning_content" not in assistants[2]


def test_history_skips_empty_assistant_but_keeps_reasoning_step_out():
    """只有思维链没有正文的那一步不进消息数组（空 assistant 消息会被网关拒）。"""
    rows = [_step(id=1, seq=1, role="assistant", content="", reasoning="只想不说")]
    msgs = messages_from_steps(_session(), rows)
    assert all(m["role"] != "assistant" for m in msgs)
    # 但界面上要看得到：落库的那一步本身带着 reasoning
    assert rows[0].reasoning == "只想不说"


def test_replay_reasoning_fills_empty_key_even_without_reasoning():
    """思考模式开着时，历史里每个 assistant 消息都要有这个键——空串也放行，缺键 400。"""
    rows = [
        _step(id=1, seq=1, role="assistant", content="答一句", reasoning=""),
        _step(id=2, seq=2, role="tool", tool_name="list_projects", tool_args="{}",
              tool_result='{"ok": true}', status="ok", reasoning=""),
    ]
    msgs = messages_from_steps(_session(), rows, replay_reasoning=True)
    assistants = [m for m in msgs if m["role"] == "assistant"]
    assert all("reasoning_content" in m for m in assistants)
    assert all(m["reasoning_content"] == "" for m in assistants)

    plain = messages_from_steps(_session(), rows)
    assert all("reasoning_content" not in m for m in plain if m["role"] == "assistant")


def test_the_runner_continues_when_the_model_only_announces_the_next_step():
    """「我现在去做…」然后收尾：runner 要替它续跑一次，而不是把半句话当答案。

    这是用户实际遇到的形态（2026-11-20）：时间线停在「我现在做零成本体检。」，
    界面上没有执行图标，看起来就像暂停了。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import Base
    from app.services import agent_runner

    class FakeAdapter:
        def __init__(self):
            self.calls = 0
            self.messages: list[list[dict]] = []

        def supports_thinking(self, model: str) -> bool:
            return False

        async def chat_once(self, model, messages, tools=None, thinking=None):
            self.messages.append(messages)
            self.calls += 1
            if self.calls == 1:
                return {
                    "content": "画布落定好了（id=22，8 节点 / 7 连线）。我现在做零成本体检。",
                    "reasoning": "",
                    "tool_calls": [],
                }
            return {"content": "体检完成：0 条告警。", "reasoning": "", "tool_calls": []}

        async def close(self):
            pass

    adapter = FakeAdapter()

    async def fake_resolve(db, session):
        return adapter, "fake-model", "Fake"

    async def case():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as db:
                session = AgentSession(
                    title="t", model_key="2:x", thinking=False,
                    canvas_project_id=22, agent_project_id=None,
                )
                db.add(session)
                await db.commit()
                await db.refresh(session)

                original = agent_runner.resolve_brain
                agent_runner.resolve_brain = fake_resolve
                try:
                    out = await agent_runner.run_loop(db, session)
                finally:
                    agent_runner.resolve_brain = original

                assert out["status"] == "done"
                assert out["rounds"] == 2, "没有续跑，半句话被当成了最终答案"
                assert out["reply"] == "体检完成：0 条告警。"
                # 续跑提示只在这一次请求里出现，不落成用户消息
                assert any(
                    "不要只说明计划" in str(m.get("content") or "")
                    for m in adapter.messages[1]
                )
        finally:
            await engine.dispose()

    asyncio.run(case())


def test_spec_mode_blocks_write_tools_and_says_why():
    """/spec 只出方案：模型真要写画布时，调用要被挡回去并记成一条可见的失败步。"""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import Base
    from app.models import Project
    from app.services import agent_runner

    class FakeAdapter:
        def __init__(self):
            self.calls = 0
            self.messages: list[list[dict]] = []

        def supports_thinking(self, model: str) -> bool:
            return False

        async def chat_once(self, model, messages, tools=None, thinking=None):
            self.messages.append(messages)
            self.calls += 1
            if self.calls == 1:
                return {
                    "content": "",
                    "reasoning": "",
                    "tool_calls": [{
                        "name": "create_project",
                        "arguments": '{"name": "spec 里不该真建"}',
                    }],
                }
            return {"content": "规范如下……请确认。", "reasoning": "", "tool_calls": []}

        async def close(self):
            pass

    adapter = FakeAdapter()

    async def fake_resolve(db, session):
        return adapter, "fake-model", "Fake"

    async def case():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as db:
                session = AgentSession(title="t", model_key="2:x", thinking=False,
                                       canvas_project_id=22, agent_project_id=None)
                db.add(session)
                await db.commit()
                await db.refresh(session)

                original = agent_runner.resolve_brain
                agent_runner.resolve_brain = fake_resolve
                try:
                    out = await agent_runner.run_loop(db, session, mode="spec")
                finally:
                    agent_runner.resolve_brain = original

                assert out["status"] == "done"
                assert out["reply"] == "规范如下……请确认。"
                assert "/spec 模式" in adapter.messages[0][0]["content"]

                steps = (await db.execute(
                    select(AgentStep).where(AgentStep.session_id == session.id)
                )).scalars().all()
                blocked = [s for s in steps if s.tool_name == "create_project"]
                assert blocked and blocked[0].status == "error"
                assert "blocked_by_mode" in (blocked[0].tool_result or "")
                # 真的没有建项目
                assert (await db.execute(select(Project))).scalars().all() == []
        finally:
            await engine.dispose()

    asyncio.run(case())


def test_goal_monitor_wakes_on_progress_then_does_one_final_review():
    """/goal：任务在跑时只等，数量变化才叫模型；全部停了做一次收尾检查。"""
    from app.services import agent_runner

    session = AgentSession(id=1, title="t", model_key="", canvas_project_id=22)
    counts = [1, 1, 2, 0]  # 初始 + 三轮循环
    kickoffs: list[str] = []

    async def fake_running(db, canvas_id):
        return counts.pop(0) if counts else 0

    async def fake_run_loop(db, session, **kwargs):
        kickoffs.append(str(kwargs.get("kickoff") or ""))
        if "还有" in kickoffs[-1]:
            return {"status": "done", "toolCalls": 1, "rounds": 1, "reply": ""}
        return {"status": "done", "toolCalls": 0, "rounds": 1, "reply": "目标完成。"}

    original_running = agent_runner._running_tasks
    original_loop = agent_runner.run_loop
    agent_runner._running_tasks = fake_running
    agent_runner.run_loop = fake_run_loop
    try:
        out = asyncio.run(agent_runner.monitor_goal(
            None, session, asyncio.Event(), canvas_id=22,
            poll_seconds=0.001, max_minutes=1,
        ))
    finally:
        agent_runner._running_tasks = original_running
        agent_runner.run_loop = original_loop

    assert out["status"] == "done"
    assert out["reply"] == "目标完成。"
    assert len(kickoffs) == 2, kickoffs
    assert "还有 2 个任务在跑" in kickoffs[0], kickoffs[0]
    assert "没有任务在跑了" in kickoffs[1], kickoffs[1]


def test_goal_monitor_stops_when_cancelled():
    from app.services import agent_runner

    session = AgentSession(id=1, title="t", model_key="", canvas_project_id=22)
    cancel = asyncio.Event()
    cancel.set()

    async def fake_running(db, canvas_id):
        return 0

    original = agent_runner._running_tasks
    agent_runner._running_tasks = fake_running
    try:
        out = asyncio.run(agent_runner.monitor_goal(
            None, session, cancel, canvas_id=22,
            poll_seconds=0.001, max_minutes=1,
        ))
    finally:
        agent_runner._running_tasks = original
    assert out["status"] == "cancelled"


def test_repeated_read_calls_are_blocked_and_max_rounds_leaves_a_visible_note():
    """同一个只读查询连叫三次就挡回；轮数到顶时要留一句人话，而不是静默停住。"""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import Base
    from app.services import agent_runner

    class FakeAdapter:
        def supports_thinking(self, model: str) -> bool:
            return False

        async def chat_once(self, model, messages, tools=None, thinking=None):
            return {
                "content": "",
                "reasoning": "",
                "tool_calls": [{"name": "list_skills", "arguments": "{}"}],
            }

        async def close(self):
            pass

    async def fake_resolve(db, session):
        return FakeAdapter(), "fake-model", "Fake"

    async def case():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as db:
                session = AgentSession(title="t", model_key="2:x", thinking=False)
                db.add(session)
                await db.commit()
                await db.refresh(session)

                original = agent_runner.resolve_brain
                agent_runner.resolve_brain = fake_resolve
                try:
                    out = await agent_runner.run_loop(db, session)
                finally:
                    agent_runner.resolve_brain = original

                assert out["status"] == "max_rounds"
                steps = (await db.execute(
                    select(AgentStep).where(AgentStep.session_id == session.id)
                )).scalars().all()
                tools = [s for s in steps if s.role == "tool"]
                assert len([s for s in tools if s.status == "ok"]) == 2, "前两次应当真的执行"
                assert len([s for s in tools if "同一个查询" in (s.tool_result or "")]) == 6
                note = [s for s in steps if s.role == "assistant" and "/goal" in s.content]
                assert note, "轮数到顶后没有留下可见的说明"
        finally:
            await engine.dispose()

    asyncio.run(case())


def test_run_loop_separates_write_calls_from_read_checks():
    """`writeCalls` 只数会改状态 / 花钱的调用，/goal 用它判断「还在干活吗」。"""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import Base
    from app.models import Project
    from app.services import agent_runner

    class FakeAdapter:
        def __init__(self):
            self.calls = 0

        def supports_thinking(self, model: str) -> bool:
            return False

        async def chat_once(self, model, messages, tools=None, thinking=None):
            self.calls += 1
            if self.calls == 1:
                return {
                    "content": "",
                    "reasoning": "",
                    "tool_calls": [{
                        "name": "create_project",
                        "arguments": '{"name": "写操作计数"}',
                    }],
                }
            return {"content": "建好了。", "reasoning": "", "tool_calls": []}

        async def close(self):
            pass

    adapter = FakeAdapter()

    async def fake_resolve(db, session):
        return adapter, "fake-model", "Fake"

    async def case():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as db:
                session = AgentSession(title="t", model_key="2:x", thinking=False)
                db.add(session)
                await db.commit()
                await db.refresh(session)

                original = agent_runner.resolve_brain
                agent_runner.resolve_brain = fake_resolve
                try:
                    out = await agent_runner.run_loop(db, session)
                finally:
                    agent_runner.resolve_brain = original

                assert out["status"] == "done"
                assert out["toolCalls"] == 1
                assert out["writeCalls"] == 1
                assert (await db.execute(select(Project))).scalars().all()
        finally:
            await engine.dispose()

    asyncio.run(case())


def test_goal_review_stops_when_only_read_checks_happen():
    """收尾检查里只查了一次 canvas_status 不算「还在干活」——否则会空转到时限。"""
    from app.services import agent_runner

    session = AgentSession(id=1, title="t", model_key="", canvas_project_id=22)
    kickoffs: list[str] = []

    async def fake_running(_db, _canvas):
        return 0

    async def fake_loop(_db, _session, **kwargs):
        kickoffs.append(str(kwargs.get("kickoff") or ""))
        return {
            "status": "done",
            "toolCalls": 3,      # 查了 canvas_status / get_canvas
            "writeCalls": 0,     # 但没有任何写操作
            "reply": "目标已完成。",
            "rounds": 2,
        }

    original_running = agent_runner._running_tasks
    original_loop = agent_runner.run_loop
    agent_runner._running_tasks = fake_running
    agent_runner.run_loop = fake_loop
    try:
        out = asyncio.run(agent_runner.monitor_goal(
            None, session, asyncio.Event(), poll_seconds=0.001, max_minutes=1,
        ))
    finally:
        agent_runner._running_tasks = original_running
        agent_runner.run_loop = original_loop

    assert out["reply"] == "目标已完成。"
    assert len(kickoffs) == 1, f"只读核对不该让监测继续空转：{kickoffs}"


def test_goal_uses_the_canvas_bound_to_the_agent_project():
    """会话自己的 canvas_project_id 常为空，真正绑画布的是 Agent 项目——别误判成「没绑定」。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import Base
    from app.models import AgentProject
    from app.services import agent_runner

    async def case():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as db:
                project = AgentProject(name="p", canvas_project_id=22)
                db.add(project)
                await db.commit()
                await db.refresh(project)
                session = AgentSession(title="t", model_key="",
                                       agent_project_id=project.id, canvas_project_id=None)
                db.add(session)
                await db.commit()
                await db.refresh(session)

                seen: list[int] = []

                async def fake_running(_db, canvas_id):
                    seen.append(canvas_id)
                    return 0

                async def fake_loop(_db, _session, **kwargs):
                    return {"status": "done", "toolCalls": 0, "reply": "完成了", "rounds": 1}

                original_running = agent_runner._running_tasks
                original_loop = agent_runner.run_loop
                agent_runner._running_tasks = fake_running
                agent_runner.run_loop = fake_loop
                try:
                    out = await agent_runner.monitor_goal(
                        db, session, asyncio.Event(), poll_seconds=0.001, max_minutes=1,
                    )
                finally:
                    agent_runner._running_tasks = original_running
                    agent_runner.run_loop = original_loop

                assert out["reply"] == "完成了"
                assert seen and all(cid == 22 for cid in seen), seen
        finally:
            await engine.dispose()

    asyncio.run(case())


def test_goal_without_a_canvas_still_continues_instead_of_bailing():
    """没有画布的目标也要接着做（比如纯文本任务），不能直接甩一句「没有可监测对象」。"""
    from app.services import agent_runner

    session = AgentSession(id=1, title="t", model_key="", canvas_project_id=None)
    kickoffs: list[str] = []

    async def fake_loop(_db, _session, **kwargs):
        kickoffs.append(str(kwargs.get("kickoff") or ""))
        return {"status": "done", "toolCalls": 0, "reply": "做完了", "rounds": 1}

    original = agent_runner.run_loop
    agent_runner.run_loop = fake_loop
    try:
        out = asyncio.run(agent_runner.monitor_goal(
            None, session, asyncio.Event(), poll_seconds=0.001, max_minutes=1,
        ))
    finally:
        agent_runner.run_loop = original

    assert out["reply"] == "做完了"
    assert kickoffs and "没有可监测的画布" in kickoffs[0], kickoffs


def _call(adapter: OpenAICompatAdapter, thinking: bool | None, model: str):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "好",
                    "reasoning_content": "想了一下",
                    "tool_calls": [],
                },
                "finish_reason": "stop",
            }],
            "usage": {"total_tokens": 12},
        })

    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def run():
        try:
            return await adapter.chat_once(model, [{"role": "user", "content": "hi"}],
                                           thinking=thinking)
        finally:
            await adapter.close()

    return asyncio.run(run()), captured


def test_adapter_sends_explicit_thinking_flag_and_reads_reasoning():
    a = OpenAICompatAdapter("https://api.deepseek.com/v1", "sk-test")
    out, cap = _call(a, True, "deepseek-v4-flash")
    assert cap["body"]["thinking"] == {"type": "enabled"}
    assert out["reasoning"] == "想了一下"
    assert out["content"] == "好"

    a2 = OpenAICompatAdapter("https://api.deepseek.com/v1", "sk-test")
    _, cap2 = _call(a2, False, "deepseek-v4-flash")
    assert cap2["body"]["thinking"] == {"type": "disabled"}


def test_adapter_does_not_send_thinking_to_other_providers():
    a = OpenAICompatAdapter("https://api.example.com/v1", "sk-test")
    out, cap = _call(a, True, "gpt-4o")
    assert "thinking" not in cap["body"]
    # 上游就算回了 reasoning_content，也照样归一化成 reasoning（只是不外发参数）
    assert out["reasoning"] == "想了一下"


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
