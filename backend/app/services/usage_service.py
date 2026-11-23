"""用量统计：这一段实际发出去多少请求、成了多少、花了多久、上游回报了多少用量。

口径先写清楚，免得把「我们发了几次」误当成「供应商账单」：
- **实际请求** = 任务表里的行：一个任务就是我们向上游发起的一次调用（失败也算；
  文档节点分块续写时一块一个任务）。
- **计划请求** = 整图运行受理时的预估（canvas_runs.expected）。它和实际对得上对不上，
  正是「这一跑到底发了几次」要看的东西。
- **上游回报用量**：只有 Agent 步骤落了 token 数（供应商没回就没有）；不猜。
- **估算费用**：本机不内置价目表——价格随服务商、模型、时段变化，硬编码一个数字
  只会给出看起来精确、实际没用的结论。这里如实说明「不估算」，让用户拿请求数去对账。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.models import AgentStep, CanvasRun, ProviderService, Task

_DONE = ("completed",)
_FAILED = ("failed", "cancelled")
_RUNNING = ("pending", "processing")


def _duration_ms(task: Task) -> int | None:
    if not task.created_at or not task.completed_at:
        return None
    try:
        return max(0, int((task.completed_at - task.created_at).total_seconds() * 1000))
    except TypeError:
        return None


async def summarize(
    db: AsyncSession, *, days: int = 7, project_id: int | None = None
) -> dict[str, Any]:
    days = max(1, min(int(days or 7), 365))
    since = utcnow() - timedelta(days=days)

    stmt = select(Task).where(Task.created_at >= since)
    if project_id is not None:
        stmt = stmt.where(Task.canvas_project_id == project_id)
    tasks = list((await db.execute(stmt)).scalars().all())

    services = {
        s.id: s.name
        for s in (await db.execute(select(ProviderService))).scalars().all()
    }

    status: dict[str, int] = {}
    by_kind: dict[str, dict[str, int]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    durations: list[int] = []
    for t in tasks:
        status[t.status] = status.get(t.status, 0) + 1
        k = t.kind or "unknown"
        slot = by_kind.setdefault(k, {"kind": k, "total": 0, "failed": 0})
        slot["total"] += 1
        if t.status in _FAILED:
            slot["failed"] += 1
        d = _duration_ms(t)
        if d is not None and t.status in _DONE:
            durations.append(d)
        key = t.model or "(未记录)"
        m = by_model.setdefault(
            key,
            {
                "modelKey": key,
                "label": key.split(":", 1)[-1] or key,
                "service": services.get(t.service_id or 0, ""),
                "total": 0,
                "failed": 0,
                "durationMs": 0,
                "timed": 0,
            },
        )
        m["total"] += 1
        if t.status in _FAILED:
            m["failed"] += 1
        if d is not None and t.status in _DONE:
            m["durationMs"] += d
            m["timed"] += 1

    for m in by_model.values():
        m["avgDurationMs"] = int(m["durationMs"] / m["timed"]) if m["timed"] else None
        m.pop("timed", None)
        m.pop("durationMs", None)

    run_stmt = select(CanvasRun).where(CanvasRun.created_at >= since)
    if project_id is not None:
        run_stmt = run_stmt.where(CanvasRun.project_id == project_id)
    runs = list((await db.execute(run_stmt)).scalars().all())
    run_ids = {r.run_id for r in runs}
    actual_for_runs = sum(1 for t in tasks if t.run_id and t.run_id in run_ids)

    token_row = await db.execute(
        select(AgentStep.tokens).where(AgentStep.created_at >= since)
    )
    agent_tokens = sum(int(x or 0) for x in token_row.scalars().all())

    return {
        "since": since.isoformat(),
        "days": days,
        "requests": {
            "total": len(tasks),
            "completed": sum(status.get(s, 0) for s in _DONE),
            "failed": sum(status.get(s, 0) for s in _FAILED),
            "running": sum(status.get(s, 0) for s in _RUNNING),
            "avgDurationMs": int(sum(durations) / len(durations)) if durations else None,
        },
        "byKind": sorted(by_kind.values(), key=lambda x: -x["total"]),
        "byModel": sorted(by_model.values(), key=lambda x: -x["total"])[:10],
        "runs": {
            "count": len(runs),
            "plannedCalls": sum(int(r.expected or 0) for r in runs),
            "actualCalls": actual_for_runs,
        },
        "agentTokens": agent_tokens,
        "cost": {
            "ready": False,
            "currency": "",
            "estimate": None,
            "note": "价格随服务商与模型而异，本机不内置价目表，因此只统计实际请求与上游回报用量，"
            "不估算费用。需要核对账单时，请以服务商控制台为准。",
        },
    }
