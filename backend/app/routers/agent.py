"""Agent 工作台接口（2.0）：会话、步骤、发消息、确认不可逆操作。

三条口径：

1. **一轮对话可能停两次**：模型说要跑整图 → 这里返回 `need_confirm` 与一份代价预估 →
   人点确认后再调 `/confirm` 继续。所以「发消息」不是一次请求跑完就算了，界面要能渲染中间态。
2. **返回完整步骤而不是增量**：步骤量很小（一屏几十条），而增量协议要维护游标、
   一旦断线就要重同步。这里每次把 `steps` 整体给出去，前端替换即可。
3. **工具清单一起下发**：界面上要能看见「它现在能做什么、哪些要人点」，
   而不是让人猜。工具表由 `agent_tools.describe()` 现生成。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.models import AgentProject, AgentSession, AgentStep
from app.providers.base import AdapterError
from app.schemas import ModelOption
from app.services import agent_runner, agent_skills, agent_tools, provider_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])


# ============================================================ 出入参


class SessionIn(BaseModel):
    title: str = ""
    model_key: str = ""
    project_id: int | None = None
    agent_project_id: int | None = None


class SessionPatch(BaseModel):
    title: str | None = None
    model_key: str | None = None
    project_id: int | None = None
    agent_project_id: int | None = None


class AgentProjectIn(BaseModel):
    name: str
    description: str = ""
    canvas_project_id: int | None = None


class AgentProjectPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    canvas_project_id: int | None = None


class MessageIn(BaseModel):
    content: str


class ConfirmIn(BaseModel):
    step_id: int
    approve: bool = True


# ============================================================ 序列化


def _step_out(s: AgentStep) -> dict[str, Any]:
    def _load(raw: str) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw

    return {
        "id": s.id,
        "seq": s.seq,
        "role": s.role,
        "content": s.content,
        "toolName": s.tool_name,
        "toolArgs": _load(s.tool_args) if s.tool_args else None,
        "toolResult": _load(s.tool_result) if s.tool_result else None,
        "tier": s.tier,
        "tierLabel": agent_tools.TIER_LABELS.get(s.tier, s.tier) if s.tier else "",
        "status": s.status,
        "durationMs": s.duration_ms,
        "tokens": s.tokens,
        "createdAt": s.created_at.isoformat() if s.created_at else "",
    }


def _session_out(s: AgentSession) -> dict[str, Any]:
    return {
        "id": s.id,
        "title": s.title,
        "modelKey": s.model_key,
        "projectId": s.canvas_project_id,
        "agentProjectId": s.agent_project_id,
        "createdAt": s.created_at.isoformat() if s.created_at else "",
        "updatedAt": s.updated_at.isoformat() if s.updated_at else "",
    }


def _agent_project_out(p: AgentProject, sessions: int = 0) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description or "",
        "canvasProjectId": p.canvas_project_id,
        "sessions": sessions,
        "createdAt": p.created_at.isoformat() if p.created_at else "",
        "updatedAt": p.updated_at.isoformat() if p.updated_at else "",
    }


async def _steps(db: AsyncSession, session_id: int) -> list[AgentStep]:
    return list((await db.execute(
        select(AgentStep).where(AgentStep.session_id == session_id).order_by(AgentStep.id)
    )).scalars().all())


async def _session_counts(db: AsyncSession) -> dict[int, int]:
    rows = (await db.execute(
        select(AgentSession.agent_project_id, AgentSession.id)
    )).all()
    out: dict[int, int] = {}
    for pid, _sid in rows:
        if pid is not None:
            out[int(pid)] = out.get(int(pid), 0) + 1
    return out


async def _resolve_canvas_id(db: AsyncSession, session: AgentSession) -> int | None:
    """会话能用哪张自由画布：项目绑定优先（与工具层同一口径）。"""
    if session.agent_project_id:
        project = await db.get(AgentProject, session.agent_project_id)
        if project is not None and project.canvas_project_id:
            return project.canvas_project_id
    return session.canvas_project_id


async def _payload(db: AsyncSession, session: AgentSession) -> dict[str, Any]:
    steps = await _steps(db, session.id)
    pending = next((s for s in steps if s.role == "tool" and s.status == "pending"), None)
    project = (
        await db.get(AgentProject, session.agent_project_id)
        if session.agent_project_id else None
    )
    out = _session_out(session)
    # 前端要显示「这次会话实际在动哪张画布」：项目绑定与会话兜底合并成同一个数
    out["canvasId"] = await _resolve_canvas_id(db, session)
    return {
        "session": out,
        "agentProject": _agent_project_out(project) if project else None,
        "steps": [_step_out(s) for s in steps],
        "pending": _step_out(pending) if pending else None,
    }


# ============================================================ 元信息


@router.get("/meta")
async def meta(db: AsyncSession = Depends(get_db)) -> dict:
    """页面打开时要的东西：可选模型、工具清单、项目与会话。"""
    rows = await provider_store.list_services(db)
    models = provider_store.list_model_options(rows, "text")
    sessions = list((await db.execute(
        select(AgentSession).order_by(AgentSession.updated_at.desc(), AgentSession.id.desc()).limit(100)
    )).scalars().all())
    counts = await _session_counts(db)
    projects = list((await db.execute(
        select(AgentProject).order_by(AgentProject.updated_at.desc(), AgentProject.id.desc())
    )).scalars().all())
    return {
        "models": [
            ModelOption(
                key=m.key, service_id=m.service_id, service_name=m.service_name,
                name=m.name, label=m.label, modality=m.modality,
            ).model_dump()
            for m in models
        ],
        "tools": agent_tools.describe(),
        "skills": agent_skills.metadata(),
        "tiers": agent_tools.TIER_LABELS,
        "projects": [_agent_project_out(p, counts.get(p.id, 0)) for p in projects],
        "sessions": [_session_out(s) for s in sessions],
        "ready": bool(models),
        "problem": "" if models else "还没有可用的文本模型：到「模型服务」接入一个（支持工具调用的最好）。",
    }


# ============================================================ Agent 项目


@router.get("/projects")
async def list_agent_projects(db: AsyncSession = Depends(get_db)) -> list[dict]:
    counts = await _session_counts(db)
    rows = (await db.execute(
        select(AgentProject).order_by(AgentProject.updated_at.desc(), AgentProject.id.desc())
    )).scalars().all()
    return [_agent_project_out(p, counts.get(p.id, 0)) for p in rows]


@router.post("/projects")
async def create_agent_project(
    payload: AgentProjectIn, db: AsyncSession = Depends(get_db)
) -> dict:
    name = payload.name.strip()[:120]
    if not name:
        raise HTTPException(status_code=400, detail="项目名不能为空")
    if payload.canvas_project_id is not None:
        from app.models import Project

        if await db.get(Project, payload.canvas_project_id) is None:
            raise HTTPException(status_code=400, detail="画布不存在")
    project = AgentProject(
        name=name,
        description=(payload.description or "").strip()[:300],
        canvas_project_id=payload.canvas_project_id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return _agent_project_out(project)


@router.patch("/projects/{project_id}")
async def patch_agent_project(
    project_id: int, payload: AgentProjectPatch, db: AsyncSession = Depends(get_db)
) -> dict:
    project = await db.get(AgentProject, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    if payload.name is not None:
        project.name = payload.name.strip()[:120] or project.name
    if payload.description is not None:
        project.description = payload.description.strip()[:300]
    if payload.canvas_project_id is not None:
        project.canvas_project_id = payload.canvas_project_id or None
    await db.commit()
    await db.refresh(project)
    counts = await _session_counts(db)
    return _agent_project_out(project, counts.get(project.id, 0))


@router.delete("/projects/{project_id}")
async def delete_agent_project(project_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    """删项目：**里面的会话不删，退回「未归档」**。

    级联删会话等于把用户的历史思考一起删了，而他想删的通常只是「这个归类」。
    """
    project = await db.get(AgentProject, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    rows = list((await db.execute(
        select(AgentSession).where(AgentSession.agent_project_id == project_id)
    )).scalars().all())
    for s in rows:
        s.agent_project_id = None
    await db.delete(project)
    await db.commit()
    return {"ok": True, "unfiled": len(rows)}


# ============================================================ 会话


@router.post("/sessions")
async def create_session(payload: SessionIn, db: AsyncSession = Depends(get_db)) -> dict:
    # 项目必须存在：画布 id 写错时，后面每一步都会报「画布不存在」，不如在这里拦住
    if payload.project_id is not None:
        from app.models import Project

        if await db.get(Project, payload.project_id) is None:
            raise HTTPException(status_code=400, detail="画布不存在")
    if payload.agent_project_id is not None:
        if await db.get(AgentProject, payload.agent_project_id) is None:
            raise HTTPException(status_code=400, detail="Agent 项目不存在")
    session = AgentSession(
        title=(payload.title or "").strip() or "新会话",
        model_key=payload.model_key.strip(),
        canvas_project_id=payload.project_id,
        agent_project_id=payload.agent_project_id,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return await _payload(db, session)


@router.get("/sessions")
async def list_sessions(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(
        select(AgentSession).order_by(AgentSession.updated_at.desc(), AgentSession.id.desc()).limit(50)
    )).scalars().all()
    return [_session_out(s) for s in rows]


@router.get("/sessions/{session_id}")
async def get_session(session_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    session = await db.get(AgentSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return await _payload(db, session)


@router.patch("/sessions/{session_id}")
async def patch_session(
    session_id: int, payload: SessionPatch, db: AsyncSession = Depends(get_db)
) -> dict:
    session = await db.get(AgentSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if payload.title is not None:
        session.title = payload.title.strip()[:120] or "新会话"
    if payload.model_key is not None:
        session.model_key = payload.model_key.strip()
    if payload.project_id is not None:
        session.canvas_project_id = payload.project_id or None
    if payload.agent_project_id is not None:
        if payload.agent_project_id and await db.get(AgentProject, payload.agent_project_id) is None:
            raise HTTPException(status_code=400, detail="Agent 项目不存在")
        session.agent_project_id = payload.agent_project_id or None
    await db.commit()
    await db.refresh(session)
    return await _payload(db, session)


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    session = await db.get(AgentSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    await db.execute(AgentStep.__table__.delete().where(AgentStep.session_id == session_id))
    await db.delete(session)
    await db.commit()
    return {"ok": True}


# ============================================================ 发消息 / 确认


def _fail(e: Exception) -> HTTPException:
    if isinstance(e, (AdapterError, ValueError)):
        return HTTPException(status_code=400, detail=str(e))
    logger.warning("Agent 执行失败：%s", e, exc_info=True)
    return HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: int, payload: MessageIn, db: AsyncSession = Depends(get_db)
) -> dict:
    session = await db.get(AgentSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    pending = (await db.execute(
        select(AgentStep).where(
            AgentStep.session_id == session_id,
            AgentStep.status == "pending",
        )
    )).scalars().first()
    if pending is not None:
        # 上一轮还在等人确认：再发消息会让那一步永远悬着
        raise HTTPException(
            status_code=409,
            detail="上一次调用还在等你确认（跑整图/跑节点），先点确认或拒绝，再继续说。",
        )
    try:
        result = await agent_runner.send(db, session, payload.content)
    except Exception as e:  # noqa: BLE001
        raise _fail(e) from e
    await db.refresh(session)
    body = await _payload(db, session)
    body["run"] = result
    return body


@router.post("/sessions/{session_id}/confirm")
async def confirm_step(
    session_id: int, payload: ConfirmIn, db: AsyncSession = Depends(get_db)
) -> dict:
    session = await db.get(AgentSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    try:
        result = await agent_runner.confirm(db, session, payload.step_id, payload.approve)
    except Exception as e:  # noqa: BLE001
        raise _fail(e) from e
    await db.refresh(session)
    body = await _payload(db, session)
    body["run"] = result
    return body
