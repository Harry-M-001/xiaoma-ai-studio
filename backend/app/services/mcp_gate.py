"""dsh sidecar 的确认门：**阻塞式**，不是令牌。

模型（dsh 里的 DeepSeek）只能「发起」一次跑图；批不批由人点。这里的实现刻意用
「挂起的工具调用 + 界面确认」而不是「签发令牌」：

- 令牌一旦签发就可以被模型自己带回来复用，等于模型自批；
- 挂起调用最多等 `CONFIRM_TIMEOUT` 秒，超时按「没执行」返回，不会悄悄放行；
- 确认动作只存在于界面 REST 接口里，**绝不**作为 MCP 工具暴露。

挂起的每一步都落成 `agent_steps` 里一条 `status="pending"` 的记录，挂在一条
「dsh 高级模式」会话下：既能被现有界面看板读到，也天然留下审计（参数、预览、结果、耗时）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import SessionLocal
from app.models import AgentSession, AgentStep, Project
from app.services import agent_runner, agent_tools, canvas_runner

logger = logging.getLogger("xiaoma.mcp")

#: sidecar 会话的固定标题：界面上能一眼看出这条任务不是主 Agent 发的
SIDECAR_TITLE = "dsh 高级模式"

#: 一次工具调用最多挂多久（与 dsh 配置的 toolCallTimeoutMs 对齐）
CONFIRM_TIMEOUT = 120.0

#: step_id → 唤醒事件与决定（只在本进程内；重启后挂起的调用按超时处理）
_waiters: dict[int, asyncio.Event] = {}
_decisions: dict[int, bool] = {}
_registered: dict[int, float] = {}


async def _sidecar_session(db: AsyncSession, project_id: int) -> AgentSession:
    """同一张画布复用同一条 sidecar 会话，避免工作区里堆满一次性任务。"""
    row = (await db.execute(
        select(AgentSession)
        .where(
            AgentSession.title == SIDECAR_TITLE,
            AgentSession.canvas_project_id == project_id,
        )
        .order_by(AgentSession.id.desc())
        .limit(1)
    )).scalars().first()
    if row is not None:
        return row
    session = AgentSession(
        title=SIDECAR_TITLE,
        canvas_project_id=project_id,
        model_key="",
        auto_approve=False,
    )
    db.add(session)
    await db.flush()
    await db.commit()
    return session


def _preview_of(step: AgentStep) -> dict:
    try:
        payload = json.loads(step.tool_result or "{}")
    except (TypeError, ValueError):
        return {}
    preview = payload.get("preview")
    return preview if isinstance(preview, dict) else {}


def _totals_of(preview: dict) -> dict:
    totals = preview.get("totals")
    return totals if isinstance(totals, dict) else {}


async def request_run(
    project_id: int,
    *,
    timeout: float | None = None,
    maker: async_sessionmaker | None = None,
) -> dict:
    """发起一次「跑整图」，挂起等人在界面点确认。

    返回的是与普通工具一致的 `{ok, data?, error?, hint?}`；
    拒绝与超时都返回 `ok: false`，并且**一条任务都不派**。
    """
    maker = maker or SessionLocal
    wait_seconds = CONFIRM_TIMEOUT if timeout is None else timeout

    async with maker() as db:
        project = await db.get(Project, int(project_id))
        if project is None:
            return {"ok": False, "error": f"画布 {project_id} 不存在。"}
        try:
            preview = await canvas_runner.preview_graph(project.id)
        except ValueError as e:
            return {"ok": False, "error": str(e), "hint": "先把画布上的节点连好再跑。"}

        calls = int(_totals_of(preview).get("calls") or 0)
        session = await _sidecar_session(db, project.id)
        step = await agent_runner.stage_step(
            db,
            session.id,
            role="tool",
            tool_name="run_canvas",
            tool_args=json.dumps({"project_id": project.id}, ensure_ascii=False),
            tool_result=json.dumps(
                {"preview": preview, "source": "mcp"}, ensure_ascii=False
            ),
            tier=agent_tools.TIER_IRREVERSIBLE,
            status="pending",
        )

        event = asyncio.Event()
        _waiters[step.id] = event
        _registered[step.id] = time.monotonic()
        # 先登记、后提交：界面轮询看到这条 pending 时，等待者一定已经就位
        await db.commit()
        logger.info(
            "mcp run_canvas 等待确认 step=%s project=%s calls=%s",
            step.id, project.id, calls,
        )

        try:
            await asyncio.wait_for(event.wait(), wait_seconds)
        except asyncio.TimeoutError:
            await _finish(
                db, step, "timeout",
                {"ok": False, "error": f"等待用户确认超过 {int(wait_seconds)} 秒，没有执行。"},
            )
            return {
                "ok": False,
                "error": f"等待用户确认超过 {int(wait_seconds)} 秒，没有执行。",
                "hint": "需要执行时让用户在小马AI工坊里点「确认执行」。",
            }
        finally:
            _waiters.pop(step.id, None)
            _registered.pop(step.id, None)

        approved = _decisions.pop(step.id, False)
        if not approved:
            await _finish(
                db, step, "rejected",
                {"ok": False, "error": "用户拒绝了这次执行，没有派发任何任务。"},
            )
            return {"ok": False, "error": "用户拒绝了这次执行，没有派发任何任务。"}

        await db.refresh(step)
        if step.status != "pending":
            return {"ok": False, "error": "这一步已经处理过了。"}

        ctx = agent_tools.ToolContext(
            db=db,
            session=session,
            model_key=session.model_key,
            agent_project=await agent_runner.project_of(db, session),
        )
        started = time.monotonic()
        try:
            # 预览已经给人看过：确认时按「当时看到的调用数」放行预算闸
            outcome = await agent_tools.execute(
                ctx, "run_canvas", {"project_id": project.id, "ack_calls": calls}
            )
        except agent_tools.ToolError as e:
            outcome = {"ok": False, "error": str(e), "hint": e.hint}
        except Exception as e:  # noqa: BLE001  执行期异常也要落进这一步，别让 UI 悬着
            logger.warning("mcp run_canvas 执行失败：%s", e, exc_info=True)
            outcome = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        await _finish(
            db, step, "ok" if outcome.get("ok") else "error", outcome,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        logger.info("mcp run_canvas step=%s 已执行 ok=%s", step.id, outcome.get("ok"))
        return outcome


async def _finish(
    db: AsyncSession, step: AgentStep, status: str, result: dict, duration_ms: int = 0
) -> None:
    step.status = status
    step.tool_result = json.dumps(
        {**result, "source": "mcp"} if isinstance(result, dict) else {"result": result},
        ensure_ascii=False,
    )
    if duration_ms:
        step.duration_ms = duration_ms
    await db.commit()


def resolve(step_id: int, approve: bool) -> bool:
    """界面点了确认 / 拒绝。返回 False = 这次确认已经结束（超时或已被处理）。"""
    event = _waiters.get(int(step_id))
    if event is None:
        return False
    _decisions[int(step_id)] = bool(approve)
    event.set()
    return True


def _remaining(step_id: int, timeout: float = CONFIRM_TIMEOUT) -> int:
    started = _registered.get(step_id)
    if started is None:
        return 0
    return max(0, int(timeout - (time.monotonic() - started)))


async def pending(db: AsyncSession, *, timeout: float = CONFIRM_TIMEOUT) -> list[dict]:
    """界面要轮询的「待确认」列表。

    顺手清理两种僵尸：进程重启后没人等的 pending、以及已经过了超时时间的 pending。
    """
    rows = (await db.execute(
        select(AgentStep)
        .where(AgentStep.status == "pending", AgentStep.tool_name == "run_canvas")
        .order_by(AgentStep.id)
    )).scalars().all()

    out: list[dict] = []
    for step in rows:
        session = await db.get(AgentSession, step.session_id)
        if session is None or session.title != SIDECAR_TITLE:
            continue
        left = _remaining(step.id, timeout)
        if step.id not in _waiters:
            if left <= 0:
                await _finish(
                    db, step, "timeout",
                    {"ok": False, "error": f"等待用户确认超过 {int(timeout)} 秒，没有执行。"},
                )
            # 极短窗口内（刚建好 step、还没登记事件）先不展示，也不误判
            continue
        preview = _preview_of(step)
        totals = _totals_of(preview)
        project = await db.get(Project, session.canvas_project_id) if session.canvas_project_id else None
        out.append({
            "stepId": step.id,
            "sessionId": session.id,
            "projectId": session.canvas_project_id,
            "projectName": project.name if project is not None else "",
            "calls": int(totals.get("calls") or 0),
            "rerun": int(totals.get("rerunNodes") or 0),
            "blocked": int(totals.get("blockedNodes") or 0),
            "createdAt": step.created_at.isoformat() if step.created_at else "",
            "remaining": left,
        })
    return out


def cleanup_stale() -> None:
    """进程退出前清空等待表；正在挂起的调用会随进程一起结束。"""
    for event in _waiters.values():
        event.set()
    _waiters.clear()
    _decisions.clear()
    _registered.clear()
