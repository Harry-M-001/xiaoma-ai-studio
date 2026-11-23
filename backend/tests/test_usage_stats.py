"""用量统计：计划 vs 实际、成败与耗时、上游回报用量，口径不能混。

- 「实际请求」是任务表里的行（一个任务 = 我们发出去的一次调用，失败也算）；
- 「计划」是整图受理时的预估（canvas_runs.expected）；
- 「上游回报用量」只统计真的落了 token 的 Agent 步骤，没上报就不猜；
- 不估算费用（本机没有价目表），接口要如实说「不估算」。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.clock import utcnow  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import AgentStep, CanvasRun, Project, ProviderService, Task  # noqa: E402
from app.services import usage_service  # noqa: E402


def _run(fn):  # noqa: ANN001
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():  # noqa: ANN202
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            await fn(maker)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


async def _seed(maker):  # noqa: ANN001
    """两个项目、四条任务（含一条 30 天前的）、一跑、两步 Agent。"""
    now = utcnow()
    async with maker() as db:
        p1 = Project(name="用量 A")
        p2 = Project(name="用量 B")
        svc = ProviderService(
            name="某云", kind="openai", base_url="https://example.invalid/v1",
            models_json="[]",
        )
        db.add_all([p1, p2, svc])
        await db.commit()
        await db.refresh(p1)
        await db.refresh(p2)
        await db.refresh(svc)

        def task(pid, status, kind, model, *, created, completed=None, run_id=None):
            return Task(
                kind=kind,
                status=status,
                service_id=svc.id,
                model=model,
                prompt="",
                params_json="{}",
                canvas_project_id=pid,
                run_id=run_id,
                created_at=created,
                completed_at=completed,
            )

        ok = task(
            p1.id, "completed", "image", f"{svc.id}:img-1",
            created=now - timedelta(seconds=10), completed=now,
            run_id="run-1",
        )
        bad = task(
            p1.id, "failed", "video", f"{svc.id}:vid-1",
            created=now - timedelta(seconds=5), completed=now,
            run_id="run-1",
        )
        cancelled = task(
            p1.id, "cancelled", "text", f"{svc.id}:txt-1",
            created=now - timedelta(seconds=2),
        )
        old = task(
            p2.id, "completed", "image", f"{svc.id}:img-1",
            created=now - timedelta(days=29), completed=now - timedelta(days=29),
        )
        db.add_all([ok, bad, cancelled, old])

        db.add(CanvasRun(run_id="run-1", project_id=p1.id, expected=5, status="finished"))
        db.add(AgentStep(session_id=1, seq=1, role="assistant", tokens=120))
        db.add(AgentStep(session_id=1, seq=2, role="assistant", tokens=30))
        await db.commit()


def test_usage_counts_requests_success_and_failures():
    async def scenario(maker):  # noqa: ANN001
        await _seed(maker)
        async with maker() as db:
            out = await usage_service.summarize(db, days=7)
        assert out["requests"]["total"] == 3, "29 天前那条不该算进近 7 天"
        assert out["requests"]["completed"] == 1
        assert out["requests"]["failed"] == 2, "失败与取消都算失败，不能漏"
        assert out["requests"]["running"] == 0
        # 只有真正完成且有起止时间的任务参与平均耗时
        assert out["requests"]["avgDurationMs"] == 10000

        kinds = {k["kind"]: k for k in out["byKind"]}
        assert kinds["image"]["total"] == 1 and kinds["image"]["failed"] == 0
        assert kinds["video"]["failed"] == 1
        assert kinds["text"]["failed"] == 1

        models = {m["modelKey"]: m for m in out["byModel"]}
        key = next(k for k in models if k.endswith(":img-1"))
        assert models[key]["total"] == 1 and models[key]["service"] == "某云"
        assert models[key]["label"] == "img-1"

    _run(scenario)


def test_usage_separates_planned_from_actual():
    """「预估 5 次、实际 2 次」要能直接看出来——这就是对账的意义。"""
    async def scenario(maker):  # noqa: ANN001
        await _seed(maker)
        async with maker() as db:
            out = await usage_service.summarize(db, days=7)
        assert out["runs"]["count"] == 1
        assert out["runs"]["plannedCalls"] == 5
        assert out["runs"]["actualCalls"] == 2

    _run(scenario)


def test_usage_reports_upstream_tokens_and_does_not_fake_cost():
    async def scenario(maker):  # noqa: ANN001
        await _seed(maker)
        async with maker() as db:
            out = await usage_service.summarize(db, days=7)
        assert out["agentTokens"] == 150, "Agent 步骤的 token 没有合计"
        assert out["cost"]["ready"] is False
        assert out["cost"]["estimate"] is None
        assert "不估算费用" in out["cost"]["note"]

    _run(scenario)


def test_usage_can_be_scoped_to_one_project_or_a_longer_window():
    async def scenario(maker):  # noqa: ANN001
        await _seed(maker)
        async with maker() as db:
            one = await usage_service.summarize(db, days=7, project_id=1)
        assert one["requests"]["total"] == 3, "项目过滤没生效"
        assert one["runs"]["count"] == 1

        async with maker() as db:
            month = await usage_service.summarize(db, days=30)
        assert month["requests"]["total"] == 4, "30 天窗口应当把旧任务算进来"

    _run(scenario)
