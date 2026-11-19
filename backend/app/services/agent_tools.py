"""Agent 的工具层：**注册表 + 三级权限 + 统一结果**。

三条口径（每条都对应一个真会踩的坑）：

1. **工具不是全开的。** 工坊有 118 个接口，全给模型的结果是「选错」概率随工具数上升，
   而一次循环几十次调用就是真金白银。所以这里只收 15 个，且按用途分组；
   模型想知道更多时，先让用户说清楚要做什么。
2. **参数白名单从注册表现取，不在这里另抄一份。** 节点类型与可调参数都来自
   `NODE_SCHEMAS` 与 `canvas_agent._agent_params`——抄一份的结局是「模型能填、后端不认」。
3. **结果统一成 `{ok, data?, error?, hint?}`。** 模型看得懂、界面能回放、
   测试能断言。错误里必须带 `hint`（能照着做的那句话），否则模型只会换个说法再错一次。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentProject, AgentSession, AgentStep, Asset, Project, Prompt, Task
from app.registry.canvas_nodes import (
    CANVAS_SCHEMA_VERSION,
    NODE_SCHEMAS,
    is_hidden_kind,
    normalize_type,
    preset_of,
    validate_document,
)
from app.services import agent_skills, canvas_agent, canvas_runner, engine_install, local_engines
from app.services import provider_store

logger = logging.getLogger("xiaoma.agent")

TIER_READ = "read"
TIER_WRITE = "write"
TIER_IRREVERSIBLE = "irreversible"

TIER_LABELS = {TIER_READ: "只读", TIER_WRITE: "可撤销", TIER_IRREVERSIBLE: "不可逆（要人点）"}


class ToolError(Exception):
    """工具执行失败，但**用户能看懂、模型能改**的那类。

    与真异常分开：真异常要进日志（带堆栈），这一类只把话说回去。
    """

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


@dataclass
class ToolContext:
    db: AsyncSession
    session: AgentSession
    model_key: str = ""
    #: 会话所属的 Agent 项目（可为空：v2.0.0 之前的历史会话没有项目）
    agent_project: AgentProject | None = None

    @property
    def project_id(self) -> int | None:
        """这次会话该动哪张自由画布：先看项目，再看会话自己的兜底值。"""
        if self.agent_project is not None and self.agent_project.canvas_project_id:
            return self.agent_project.canvas_project_id
        return self.session.canvas_project_id


@dataclass(frozen=True)
class ToolSpec:
    name: str
    label: str
    description: str
    tier: str
    parameters: dict[str, Any]
    handler: Callable[[ToolContext, dict], Awaitable[dict]]
    # 不可逆工具在「等人确认」时先算一份代价（纯读），给确认弹窗用
    preview: Callable[[ToolContext, dict], Awaitable[dict]] | None = None

    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ============================================================ 小工具


def _trim(text: Any, limit: int = 160) -> str:
    s = str(text or "").strip().replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _no_args() -> dict:
    return {"type": "object", "properties": {}}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


async def _get_project(ctx: ToolContext, args: dict) -> Project:
    pid = args.get("project_id") or ctx.project_id
    if not pid:
        raise ToolError(
            "这次会话还没有绑定画布，你也没有指定 project_id。",
            hint="先调用 create_project 建一张，或从 list_projects 里挑一张再把 id 传进来。",
        )
    project = await ctx.db.get(Project, int(pid))
    if project is None:
        raise ToolError(f"画布 {pid} 不存在。", hint="用 list_projects 看现在有哪些画布。")
    return project


def _doc(project: Project) -> dict:
    if not project.canvas_json:
        return {"schemaVersion": CANVAS_SCHEMA_VERSION, "nodes": [], "edges": [], "viewport": {}}
    try:
        doc = json.loads(project.canvas_json)
    except (TypeError, ValueError):
        return {"schemaVersion": CANVAS_SCHEMA_VERSION, "nodes": [], "edges": [], "viewport": {}}
    doc.setdefault("schemaVersion", CANVAS_SCHEMA_VERSION)
    doc.setdefault("nodes", [])
    doc.setdefault("edges", [])
    return doc


async def _model_for(db: AsyncSession, kind: str) -> str:
    """按节点类型挑一个本机可用的模型（与前端 `modelKeyForNodeType` 同一口径）。"""
    rows = await provider_store.list_services(db)
    want = "text"
    if kind == "video":
        want = "video"
    elif kind in ("image", "assetImage", "storyboardImage"):
        want = "image"
    elif kind == "workflow":
        return ""
    option = next(iter(provider_store.list_model_options(rows, want)), None)
    return option.key if option else ""


def _node_label(kind: str) -> str:
    return str((NODE_SCHEMAS.get(kind) or {}).get("label") or kind)


# ============================================================ 只读工具


async def _list_skills(ctx: ToolContext, args: dict) -> dict:
    return {
        "ok": True,
        "data": {"skills": agent_skills.metadata()},
        "hint": "先看描述判断相不相关；相关再 load_skill 取正文，不要凭名字猜内容。",
    }


async def _load_skill(ctx: ToolContext, args: dict) -> dict:
    key = str(args.get("key") or "").strip()
    skill = agent_skills.get(key)
    if skill is None:
        names = "、".join(s["key"] for s in agent_skills.metadata()) or "（还没有技能）"
        raise ToolError(f"没有这个技能：{key}", hint=f"现有技能：{names}")
    body = skill["body"]
    truncated = len(body) > agent_skills.MAX_BODY_CHARS
    return {
        "ok": True,
        "data": {
            "key": skill["key"],
            "name": skill["name"],
            "body": body[: agent_skills.MAX_BODY_CHARS],
            "truncated": truncated,
        },
        "hint": "按这份正文的步骤做；它与工具返回冲突时**以工具返回为准**。",
    }


async def _get_asset(ctx: ToolContext, args: dict) -> dict:
    asset = await ctx.db.get(Asset, int(args.get("asset_id") or 0))
    if asset is None:
        raise ToolError("资产不存在。", hint="用 list_assets 看现有哪些 id。")
    return {
        "ok": True,
        "data": {
            "id": asset.id,
            "kind": asset.kind,
            "url": f"/media/{asset.filename}",
            "name": asset.name or "",
            "category": asset.category or "",
            "original_name": asset.original_name or "",
            "width": getattr(asset, "width", None),
            "height": getattr(asset, "height", None),
            "created_at": asset.created_at.isoformat() if asset.created_at else "",
        },
    }


async def _get_task(ctx: ToolContext, args: dict) -> dict:
    task = await ctx.db.get(Task, int(args.get("task_id") or 0))
    if task is None:
        raise ToolError("任务不存在。", hint="用 list_tasks 看最近的 id。")
    data = {
        "id": task.id,
        "kind": task.kind,
        "status": task.status,
        "progress": task.progress,
        "model": task.model,
        "error": _trim(task.error, 200),
        "project_id": getattr(task, "canvas_project_id", None),
        "node_id": getattr(task, "canvas_node_id", None),
    }
    for field in ("asset_id", "result_asset_id", "progress_text"):
        if hasattr(task, field):
            data[field] = getattr(task, field)
    return {"ok": True, "data": data}


async def _list_engines(ctx: ToolContext, args: dict) -> dict:
    rows = []
    for engine in local_engines.engines():
        state = engine_install.state(engine)
        rows.append({
            "key": engine.key,
            "label": engine.label,
            "kind": engine.kind_label,
            "size": engine.size_text,
            "installed": bool(state.get("installed")),
            "level": state.get("level"),
            "reason": _trim(state.get("reason"), 120),
        })
    return {
        "ok": True,
        "data": {"engines": rows},
        "hint": "没装的本机引擎只说明「可以装」，不要替用户点下载。",
    }


async def _list_prompt_templates(ctx: ToolContext, args: dict) -> dict:
    limit = min(30, max(1, int(args.get("limit") or 10)))
    stmt = select(Prompt).where(Prompt.enabled.is_(True)).order_by(Prompt.sort_order, Prompt.id).limit(limit)
    rows = (await ctx.db.execute(stmt)).scalars().all()
    return {
        "ok": True,
        "data": {
            "prompts": [
                {
                    "id": p.id,
                    "key": p.key,
                    "title": p.title,
                    "modality": p.modality,
                    "tags": p.tags,
                    "description": _trim(p.description, 80),
                    "content": _trim(p.content, 300),
                }
                for p in rows
            ]
        },
        "hint": "需要把模板套到某个节点时，把 content 里换成用户的具体描述，再用 update_node 写入。",
    }


async def _list_projects(ctx: ToolContext, args: dict) -> dict:
    rows = (await ctx.db.execute(select(Project).order_by(Project.updated_at.desc()))).scalars().all()
    items = []
    for p in rows:
        doc = _doc(p)
        items.append({
            "id": p.id,
            "name": p.name,
            "nodes": len(doc["nodes"]),
            "edges": len(doc["edges"]),
            "updated_at": p.updated_at.isoformat() if p.updated_at else "",
        })
    return {"ok": True, "data": {"projects": items}, "hint": "要用哪张就把它的 id 传给其他工具。"}


async def _get_canvas(ctx: ToolContext, args: dict) -> dict:
    project = await _get_project(ctx, args)
    doc = _doc(project)
    nodes = []
    for n in doc["nodes"]:
        data = n.get("data") or {}
        kind = normalize_type(str(n.get("type") or ""))
        nodes.append({
            "id": n.get("id"),
            "type": kind,
            "label": _node_label(kind),
            "prompt": _trim(data.get("prompt"), 120),
            "params": {
                k: v for k, v in data.items()
                if k not in ("prompt", "schema", "nodeType") and isinstance(v, (str, int, float, bool))
            },
        })
    return {
        "ok": True,
        "data": {
            "project_id": project.id,
            "name": project.name,
            "nodes": nodes,
            "edges": [{"from": e.get("source"), "to": e.get("target")} for e in doc["edges"]],
        },
    }


async def _summarize_canvas(ctx: ToolContext, args: dict) -> dict:
    project = await _get_project(ctx, args)
    doc = _doc(project)
    if not doc["nodes"]:
        return {
            "ok": True,
            "data": {"summary": f"画布「{project.name}」还是空的。", "project_id": project.id},
        }
    linked = {e.get("source") for e in doc["edges"]} | {e.get("target") for e in doc["edges"]}
    lines = [f"画布「{project.name}」（id={project.id}）：{len(doc['nodes'])} 个节点、{len(doc['edges'])} 条连线"]
    for i, n in enumerate(doc["nodes"], 1):
        data = n.get("data") or {}
        kind = normalize_type(str(n.get("type") or ""))
        prompt = _trim(data.get("prompt"), 40)
        mark = "" if n.get("id") in linked else "（未连线）"
        lines.append(f"{i}. {_node_label(kind)} [{n.get('id')}]{mark}：{prompt}")
    return {"ok": True, "data": {"summary": "\n".join(lines), "project_id": project.id}}


async def _preview_canvas(ctx: ToolContext, args: dict) -> dict:
    project = await _get_project(ctx, args)
    try:
        preview = await canvas_runner.preview_graph(project.id)
    except ValueError as e:
        raise ToolError(str(e), hint="先看看画布内容（get_canvas），确认节点都连上了。") from e
    totals = preview.get("totals") or {}
    nodes = [
        {
            "id": item.get("nodeId") or item.get("id"),
            "label": item.get("label") or "",
            "status": item.get("status") or "",
            "willRun": item.get("willRun"),
            "reason": item.get("reason") or "",
        }
        for item in (preview.get("nodes") or [])[:20]
    ]
    return {
        "ok": True,
        "data": {"project_id": project.id, "totals": totals, "gate": preview.get("gate") or {}, "nodes": nodes},
        "hint": "这些是**预估**：真跑之前用户还要点一次确认。",
    }


async def _lint_canvas(ctx: ToolContext, args: dict) -> dict:
    project = await _get_project(ctx, args)
    try:
        report = await canvas_runner.lint_graph(project.id)
    except ValueError as e:
        raise ToolError(str(e), hint="先确认画布里已经有分镜节点。") from e
    issues = [
        {"level": i.get("level"), "message": i.get("message"), "suggestion": i.get("suggestion")}
        for i in (report.get("issues") or [])[:10]
    ]
    return {
        "ok": True,
        "data": {"project_id": project.id, "count": len(report.get("issues") or []), "issues": issues},
        "hint": "体检只告警、不阻断；要不要改由用户决定。",
    }


async def _list_assets(ctx: ToolContext, args: dict) -> dict:
    limit = min(30, max(1, int(args.get("limit") or 10)))
    kind = str(args.get("kind") or "").strip()
    stmt = select(Asset).order_by(Asset.id.desc()).limit(limit)
    if kind:
        stmt = stmt.where(Asset.kind == kind)
    rows = (await ctx.db.execute(stmt)).scalars().all()
    return {
        "ok": True,
        "data": {
            "assets": [
                {
                    "id": a.id,
                    "kind": a.kind,
                    "url": f"/media/{a.filename}",
                    "name": a.name or "",
                    "category": a.category or "",
                    "original_name": a.original_name or "",
                    "created_at": a.created_at.isoformat() if a.created_at else "",
                }
                for a in rows
            ]
        },
    }


async def _list_tasks(ctx: ToolContext, args: dict) -> dict:
    limit = min(30, max(1, int(args.get("limit") or 10)))
    status = str(args.get("status") or "").strip()
    stmt = select(Task).order_by(Task.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(Task.status == status)
    rows = (await ctx.db.execute(stmt)).scalars().all()
    return {
        "ok": True,
        "data": {
            "tasks": [
                {
                    "id": t.id,
                    "kind": t.kind,
                    "status": t.status,
                    "progress": t.progress,
                    "model": t.model,
                    "error": _trim(t.error, 80),
                }
                for t in rows
            ]
        },
    }


async def _list_models(ctx: ToolContext, args: dict) -> dict:
    rows = await provider_store.list_services(ctx.db)
    options = provider_store.list_model_options(rows, str(args.get("modality") or "text"))
    return {
        "ok": True,
        "data": {
            "models": [
                {"key": o.key, "name": o.name, "service": o.service_name, "label": o.label}
                for o in options
            ]
        },
        "hint": "没有模型时可让用户到「模型服务」接入；这一步不要替用户决定花钱。",
    }


# ============================================================ 可撤销的写工具


async def _list_agent_projects(ctx: ToolContext, args: dict) -> dict:
    rows = (await ctx.db.execute(
        select(AgentProject).order_by(AgentProject.updated_at.desc(), AgentProject.id.desc())
    )).scalars().all()
    items = []
    for p in rows:
        count = (await ctx.db.execute(
            select(AgentSession.id).where(AgentSession.agent_project_id == p.id)
        )).scalars().all()
        items.append({
            "id": p.id,
            "name": p.name,
            "description": _trim(p.description, 80),
            "canvas_project_id": p.canvas_project_id,
            "sessions": len(count),
        })
    return {
        "ok": True,
        "data": {"projects": items, "current": ctx.agent_project.id if ctx.agent_project else None},
        "hint": "一次任务 = 一个会话。同一个项目的会话共享上下文归属，不同项目互不串。",
    }


async def _create_agent_project(ctx: ToolContext, args: dict) -> dict:
    name = _trim(args.get("name"), 120).strip()
    if not name:
        raise ToolError("项目名不能为空。", hint="给一个能认出来的名字，比如「短剧 A · 前期」。")
    existing = (await ctx.db.execute(
        select(AgentProject).where(AgentProject.name == name)
    )).scalars().first()
    if existing is not None:
        return {
            "ok": True,
            "data": {"agent_project_id": existing.id, "name": existing.name, "created": False},
            "hint": "已经有同名项目了，直接用这个 id；要新建请换个名字。",
        }
    canvas_id = args.get("canvas_project_id")
    if canvas_id is not None:
        if await ctx.db.get(Project, int(canvas_id)) is None:
            raise ToolError(f"画布 {canvas_id} 不存在。", hint="先 list_projects 看看有哪些。")
    project = AgentProject(
        name=name,
        description=_trim(args.get("description"), 300),
        canvas_project_id=int(canvas_id) if canvas_id is not None else None,
    )
    ctx.db.add(project)
    await ctx.db.commit()
    await ctx.db.refresh(project)
    return {
        "ok": True,
        "data": {
            "agent_project_id": project.id,
            "name": project.name,
            "canvas_project_id": project.canvas_project_id,
            "created": True,
        },
        "hint": "项目建好了，空的。用户可以在里面开任务（会话）；**当前这个会话没有挪过去**，"
                "别对外说它已经在项目里。",
    }


async def _create_project(ctx: ToolContext, args: dict) -> dict:
    name = _trim(args.get("name"), 100).strip()
    if not name:
        raise ToolError("画布名不能为空。", hint="给一个能认出来的名字，比如「外卖侠 60 秒短剧」。")
    existing = (await ctx.db.execute(select(Project).where(Project.name == name))).scalars().first()
    if existing is not None:
        # 幂等：同名不重复建，把已有的 id 还回去——模型重复调用时不会造出一堆同名画布。
        # 顺手把绑定写上去：不绑的话后面每一步都得显式传 project_id，模型只要漏一次就白跑。
        if ctx.agent_project is not None:
            ctx.agent_project.canvas_project_id = existing.id
        else:
            ctx.session.canvas_project_id = existing.id
        await ctx.db.commit()
        return {
            "ok": True,
            "data": {"project_id": existing.id, "name": existing.name, "created": False},
            "hint": "已经有同名画布了，直接用这个 id；要新建请换个名字。",
        }
    project = Project(name=name, description=_trim(args.get("description"), 300), status="active")
    project.canvas_json = json.dumps(
        {"schemaVersion": CANVAS_SCHEMA_VERSION, "nodes": [], "edges": [], "viewport": {}},
        ensure_ascii=False,
    )
    ctx.db.add(project)
    await ctx.db.commit()
    await ctx.db.refresh(project)
    # 画布绑定优先写到项目上（会话继承项目的画布）；未归档会话才写自己身上
    if ctx.agent_project is not None:
        ctx.agent_project.canvas_project_id = project.id
    else:
        ctx.session.canvas_project_id = project.id
    await ctx.db.commit()
    return {"ok": True, "data": {"project_id": project.id, "name": project.name, "created": True}}


async def _draft_canvas(ctx: ToolContext, args: dict) -> dict:
    brief = str(args.get("brief") or "").strip()
    if len(brief) < 6:
        raise ToolError("需求太短，猜不出要搭什么。", hint="让用户补一句：题材、多长、要不要出图/出片。")
    draft = await canvas_agent.plan(ctx.db, brief, model_key=ctx.model_key)
    payload = draft.to_dict()
    if payload.get("status") != "ok":
        return {
            "ok": False,
            "error": payload.get("summary") or "没能生成草稿",
            "hint": payload.get("nextAction") or "把需求说具体一点再试。",
            "data": payload,
        }
    return {
        "ok": True,
        "data": {
            "project_id": ctx.project_id,
            "draft": payload,
            "node_count": len(payload.get("nodes") or []),
            "nodes": [
                {
                    "id": n.get("id"),
                    "kind": n.get("kind"),
                    "label": _node_label(str(n.get("kind"))),
                    "prompt": _trim((n.get("data") or {}).get("prompt"), 60),
                }
                for n in payload.get("nodes") or []
            ],
            "edge_count": len(payload.get("edges") or []),
        },
        "hint": "草稿还没落到画布上。确认没问题就调用 apply_canvas_draft。",
    }


async def _last_draft(ctx: ToolContext) -> dict:
    """取这次会话里**最近一次成功的**草稿。

    不让模型自己把整份草稿当参数回传：那份 JSON 有几千 token，回传一次既贵又容易写坏，
    还给了它「偷偷改草稿」的机会。这里用步骤 id 当指针，等价于 2026 年那套「指针代替载荷」。
    """
    step = (await ctx.db.execute(
        select(AgentStep)
        .where(AgentStep.session_id == ctx.session.id, AgentStep.tool_name == "draft_canvas")
        .order_by(AgentStep.id.desc())
        .limit(1)
    )).scalars().first()
    if step is None or step.status != "ok":
        raise ToolError("这次会话里还没有可用的画布草稿。", hint="先调用 draft_canvas 生成一份。")
    try:
        result = json.loads(step.tool_result)
    except (TypeError, ValueError):
        raise ToolError("上一份草稿读不出来了。", hint="重新调用 draft_canvas 生成一次。")
    draft = ((result.get("data") or {}).get("draft")) or {}
    if not draft.get("nodes"):
        raise ToolError("上一份草稿是空的。", hint="重新调用 draft_canvas 生成一次。")
    return draft


def _doc_from_draft(draft: dict, project: Project, model_keys: dict[str, str]) -> dict:
    """草稿 → 画布文档。节点/连线的形状与前端 `mergeCanvasContent` 对齐。"""
    existing = _doc(project)
    offset_x = 0.0
    if existing["nodes"]:
        offset_x = max(float((n.get("position") or {}).get("x") or 0) for n in existing["nodes"]) + 360
    stamp = str(int(time.time() * 1000))[-6:]
    kind_of: dict[str, str] = {}
    nodes = []
    for n in draft.get("nodes") or []:
        kind = normalize_type(str(n.get("kind") or ""))
        # 旧草稿（M5 之前）里可能还写着配方节点：落定时改写成承载它的原语
        if is_hidden_kind(kind):
            kind = preset_of(kind) or kind
        if kind not in NODE_SCHEMAS:
            continue
        nid = f"ag_{n.get('id')}_{stamp}"
        kind_of[str(n.get("id"))] = kind
        pos = n.get("position") or {}
        data = {**(n.get("data") or {}), "model_key": model_keys.get(kind, ""), "nodeType": kind}
        # 落库的形状与前端 `canvasChain.toCanvasDocNodes` 一致：
        # `type` 必须是**真实节点类型**（不是画布内部的 "contract"），`schema` 是运行时缓存不落库。
        data.pop("schema", None)
        data.pop("status", None)
        nodes.append({
            "id": nid,
            "type": kind,
            "position": {
                "x": float(pos.get("x") or 0) + offset_x,
                "y": float(pos.get("y") or 0),
            },
            "data": data,
        })
    edges = []
    for i, e in enumerate(draft.get("edges") or []):
        frm = kind_of.get(str(e.get("from") or e.get("source") or ""))
        to = kind_of.get(str(e.get("to") or e.get("target") or ""))
        if not frm or not to:
            continue
        edges.append({
            "id": f"ag_e_{i}_{stamp}",
            "source": f"ag_{e.get('from') or e.get('source')}_{stamp}",
            "sourceHandle": (NODE_SCHEMAS[frm].get("handles", {}).get("sources") or [{}])[0].get("id"),
            "target": f"ag_{e.get('to') or e.get('target')}_{stamp}",
            "targetHandle": (NODE_SCHEMAS[to].get("handles", {}).get("targets") or [{}])[0].get("id"),
        })
    doc = {
        "schemaVersion": CANVAS_SCHEMA_VERSION,
        "nodes": [*existing["nodes"], *nodes],
        "edges": [*existing["edges"], *edges],
        "viewport": existing.get("viewport") or {},
    }
    return doc


async def _apply_canvas_draft(ctx: ToolContext, args: dict) -> dict:
    project = await _get_project(ctx, args)
    before = len(_doc(project)["nodes"])
    draft = await _last_draft(ctx)
    model_keys = {
        kind: await _model_for(ctx.db, kind)
        for kind in {normalize_type(str(n.get("kind") or "")) for n in draft.get("nodes") or []}
    }
    doc = _doc_from_draft(draft, project, model_keys)
    try:
        nodes, edges = validate_document(doc)
    except ValueError as e:
        raise ToolError(f"草稿落定失败：{e}", hint="把需求说具体一点，重新生成一份草稿。") from e
    project.canvas_json = json.dumps(
        {**doc, "nodes": nodes, "edges": edges}, ensure_ascii=False
    )
    await ctx.db.commit()
    missing = [kind for kind, key in model_keys.items() if kind != "workflow" and not key]
    return {
        "ok": True,
        "data": {
            "project_id": project.id,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "added": len(nodes) - before,
        },
        "hint": (
            f"已落到画布（现在共 {len(nodes)} 个节点）。"
            + (f"缺这些模型：{'、'.join(missing)}，跑之前要先接入。" if missing else "")
        ),
    }


async def _update_node(ctx: ToolContext, args: dict) -> dict:
    project = await _get_project(ctx, args)
    node_id = str(args.get("node_id") or "").strip()
    doc = _doc(project)
    node = next((n for n in doc["nodes"] if n.get("id") == node_id), None)
    if node is None:
        raise ToolError(f"画布里没有节点 {node_id}。", hint="用 get_canvas 看一遍节点 id。")
    kind = normalize_type(str(node.get("type") or ""))
    if kind not in NODE_SCHEMAS:
        raise ToolError(f"节点类型 {kind} 不认识。", hint="只改契约表里有的节点。")
    data = dict(node.get("data") or {})
    if "prompt" in args:
        data["prompt"] = str(args.get("prompt") or "")
    params = args.get("params") or {}
    if not isinstance(params, dict):
        raise ToolError("params 必须是对象。", hint="例如 {\"size\":\"1024x1024\"}。")
    allowed = canvas_agent._agent_params(kind)  # 与画布 Agent 共用同一份白名单
    ignored = []
    for key, value in params.items():
        if key not in allowed or isinstance(value, (dict, list)):
            ignored.append(key)
            continue
        data[key] = value
    node["data"] = data
    try:
        nodes, edges = validate_document(doc)
    except ValueError as e:
        raise ToolError(f"改完之后文档不合法：{e}", hint="把参数改回原样或换成合法取值。") from e
    project.canvas_json = json.dumps({**doc, "nodes": nodes, "edges": edges}, ensure_ascii=False)
    await ctx.db.commit()
    return {
        "ok": True,
        "data": {"project_id": project.id, "node_id": node_id, "ignored_params": ignored},
        "hint": "只有白名单里的参数会被写进去；被忽略的键在 ignored_params 里。",
    }


async def _add_image_group(ctx: ToolContext, args: dict) -> dict:
    """在上游节点后面接一个图片组（1→N 出图），可撤销。"""
    project = await _get_project(ctx, args)
    source = str(args.get("source_node_id") or "").strip()
    doc = _doc(project)
    src = next((n for n in doc["nodes"] if n.get("id") == source), None)
    if src is None:
        raise ToolError(f"画布里没有节点 {source}。", hint="先用 get_canvas 看一遍节点 id。")
    kind = normalize_type(str(src.get("type") or ""))

    model_key = str(args.get("model_key") or "").strip()
    if not model_key:
        rows = await provider_store.list_services(ctx.db)
        option = next(iter(provider_store.list_model_options(rows, "image")), None)
        if option is None:
            raise ToolError(
                "还没有可用的图片模型。",
                hint="到「模型服务」接入一个，或在参数里指定 model_key。",
            )
        model_key = option.key

    stamp = int(time.time() * 1000)
    node_id = f"ag_ig_{stamp}"
    pos = src.get("position") or {}
    data: dict = {"model_key": model_key}
    for key, arg in (("prompt", "prompt"), ("styleKey", "style_key"), ("shotLimit", "shot_limit")):
        value = args.get(arg)
        if value not in (None, ""):
            data[key] = value
    doc["nodes"].append({
        "id": node_id,
        "type": "imageGroup",
        "position": {"x": float(pos.get("x") or 0) + 380, "y": float(pos.get("y") or 0)},
        "data": data,
    })
    doc["edges"].append({
        "id": f"ag_c_{stamp}",
        "source": source,
        "sourceHandle": (NODE_SCHEMAS[kind].get("handles", {}).get("sources") or [{}])[0].get("id"),
        "target": node_id,
        "targetHandle": "in-any",
    })
    try:
        nodes, edges = validate_document(doc)
    except ValueError as e:
        raise ToolError(
            f"加上图片组之后文档不合法：{e}",
            hint="确认上游节点的输出端口类型（文本或图片都能接）。",
        ) from e
    project.canvas_json = json.dumps({**doc, "nodes": nodes, "edges": edges}, ensure_ascii=False)
    await ctx.db.commit()
    return {
        "ok": True,
        "data": {"project_id": project.id, "node_id": node_id, "source": source, "model_key": model_key},
        "hint": "图片组已接在上游后面；跑之前用 preview_canvas 看要几次调用。",
    }


async def _connect_nodes(ctx: ToolContext, args: dict) -> dict:
    project = await _get_project(ctx, args)
    src = str(args.get("source") or "").strip()
    dst = str(args.get("target") or "").strip()
    doc = _doc(project)
    kinds = {n.get("id"): normalize_type(str(n.get("type") or "")) for n in doc["nodes"]}
    if src not in kinds or dst not in kinds:
        raise ToolError("连线两端必须都在画布里。", hint="用 get_canvas 核对节点 id。")
    if src == dst:
        raise ToolError("不能自己连自己。", hint="换一个目标节点。")
    if any(e.get("source") == src and e.get("target") == dst for e in doc["edges"]):
        return {"ok": True, "data": {"project_id": project.id, "duplicated": True}, "hint": "这条线已经有了。"}
    doc["edges"].append({
        "id": f"ag_c_{int(time.time() * 1000)}",
        "source": src,
        "sourceHandle": (NODE_SCHEMAS[kinds[src]].get("handles", {}).get("sources") or [{}])[0].get("id"),
        "target": dst,
        "targetHandle": (NODE_SCHEMAS[kinds[dst]].get("handles", {}).get("targets") or [{}])[0].get("id"),
    })
    try:
        nodes, edges = validate_document(doc)
    except ValueError as e:
        raise ToolError(f"这条连线不合法：{e}", hint="看看两端的端口类型对不对（文本→文本、图片→图片）。") from e
    project.canvas_json = json.dumps({**doc, "nodes": nodes, "edges": edges}, ensure_ascii=False)
    await ctx.db.commit()
    return {"ok": True, "data": {"project_id": project.id, "source": src, "target": dst}}


# ============================================================ 不可逆工具（要人点）


async def _preview_run_canvas(ctx: ToolContext, args: dict) -> dict:
    project = await _get_project(ctx, args)
    try:
        preview = await canvas_runner.preview_graph(project.id)
    except ValueError as e:
        raise ToolError(str(e), hint="先确认画布里有可执行的节点。") from e
    totals = preview.get("totals") or {}
    return {
        "calls": int(totals.get("calls") or 0),
        "pending": int(totals.get("pendingNodes") or 0),
        "redo": int(totals.get("rerunNodes") or 0),
        "blocked": int(totals.get("blockedNodes") or 0),
        "dialogue": int(totals.get("dialogueCalls") or 0),
        "project_id": project.id,
        "project_name": project.name,
        "gate": preview.get("gate") or {},
    }


async def _run_canvas(ctx: ToolContext, args: dict) -> dict:
    project = await _get_project(ctx, args)
    try:
        run_id = canvas_runner.start_full_graph(
            project.id, expected=int(args.get("ack_calls") or 0)
        )
    except RuntimeError as e:
        # 「已经有一跑在跑」这类状态冲突，不是参数问题
        raise ToolError(f"没能开始执行：{e}", hint="等上一次跑完，或先去画布看看进度。") from e
    return {
        "ok": True,
        "data": {"project_id": project.id, "run_id": run_id},
        "hint": "任务已经派出去，用 list_tasks 或 canvas_status 看进度；**不要**再说一遍「已经跑完了」。",
    }


async def _preview_run_node(ctx: ToolContext, args: dict) -> dict:
    project = await _get_project(ctx, args)
    node_id = str(args.get("node_id") or "").strip()
    doc = _doc(project)
    node = next((n for n in doc["nodes"] if n.get("id") == node_id), None)
    if node is None:
        raise ToolError(f"画布里没有节点 {node_id}。", hint="用 get_canvas 看节点 id。")
    return {
        "calls": 1,
        "node": _node_label(normalize_type(str(node.get("type") or ""))),
        "project_id": project.id,
        "project_name": project.name,
        "gate": {},
    }


async def _run_node(ctx: ToolContext, args: dict) -> dict:
    project = await _get_project(ctx, args)
    node_id = str(args.get("node_id") or "").strip()
    try:
        tasks = await canvas_runner.run_single_node(project.id, node_id, canvas_runner.new_run_id())
    except ValueError as e:
        raise ToolError(f"没能执行这个节点：{e}", hint="先看画布上这个节点还缺什么输入。") from e
    return {
        "ok": True,
        "data": {
            "project_id": project.id,
            "node_id": node_id,
            "task_ids": [t.id for t in tasks],
            "task_count": len(tasks),
        },
        "hint": "任务已派出；用 list_tasks 看结果，不要臆造产物。",
    }


async def _canvas_status(ctx: ToolContext, args: dict) -> dict:
    project = await _get_project(ctx, args)
    doc = _doc(project)
    rows = (await ctx.db.execute(
        select(Task).where(Task.canvas_project_id == project.id).order_by(Task.id.desc()).limit(40)
    )).scalars().all()
    by_node: dict[str, list[Task]] = {}
    for t in rows:
        if t.canvas_node_id:
            by_node.setdefault(t.canvas_node_id, []).append(t)
    out = []
    for n in doc["nodes"]:
        tasks = by_node.get(str(n.get("id"))) or []
        if not tasks:
            continue
        latest = tasks[0]
        out.append({
            "node_id": n.get("id"),
            "label": _node_label(normalize_type(str(n.get("type") or ""))),
            "status": latest.status,
            "progress": latest.progress,
            "error": _trim(latest.error, 80),
            "task_id": latest.id,
        })
    return {"ok": True, "data": {"project_id": project.id, "nodes": out}}


# ============================================================ 注册表


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "list_agent_projects", "列 Agent 项目", "列出 Agent 项目（任务容器）与各自的会话数。"
        "不同项目上下文分开：做另一件事时先看有没有对应项目。",
        TIER_READ, _no_args(), _list_agent_projects,
    ),
    ToolSpec(
        "create_agent_project", "新建 Agent 项目", "新建一个 Agent 项目（任务的容器），可同时指定它绑定哪张自由画布。",
        TIER_WRITE,
        _obj({
            "name": {"type": "string"},
            "description": {"type": "string"},
            "canvas_project_id": {"type": "integer", "description": "要绑定的自由画布 id（可空）"},
        }, ["name"]),
        _create_agent_project,
    ),
    ToolSpec(
        "list_skills", "列技能", "列出内置技能（只有名字与描述）。判断相关后再 load_skill 取正文。",
        TIER_READ, _no_args(), _list_skills,
    ),
    ToolSpec(
        "load_skill", "加载技能", "按 key 取技能正文：做法、检查步骤与常见坑。做视频/体检/一致性检查前先加载。",
        TIER_READ, _obj({"key": {"type": "string"}}, ["key"]), _load_skill,
    ),
    ToolSpec(
        "list_projects", "列出画布", "列出所有自由画布（id / 名称 / 节点数）。不知道要动哪张时先调它。",
        TIER_READ, _no_args(), _list_projects,
    ),
    ToolSpec(
        "get_canvas", "读画布", "读一张画布的节点（id / 类型 / 提示词 / 参数）与连线。改之前必须先读。",
        TIER_READ,
        _obj({"project_id": {"type": "integer", "description": "画布 id；不填就用这次会话绑定的那张"}}),
        _get_canvas,
    ),
    ToolSpec(
        "summarize_canvas", "画布摘要", "把画布压成每节点一行的人话摘要，适合先向用户复述现状。",
        TIER_READ,
        _obj({"project_id": {"type": "integer"}}),
        _summarize_canvas,
    ),
    ToolSpec(
        "preview_canvas", "成本预估", "只算不跑：会执行几个节点、至少几次调用、哪些节点已有产物会被重做。",
        TIER_READ,
        _obj({"project_id": {"type": "integer"}}),
        _preview_canvas,
    ),
    ToolSpec(
        "lint_canvas", "分镜体检", "零成本检查镜头语言（景别单调 / 运镜雷同 / AI 腔），只告警不阻断。",
        TIER_READ,
        _obj({"project_id": {"type": "integer"}}),
        _lint_canvas,
    ),
    ToolSpec(
        "list_assets", "列资产", "列出最近的资产（图片 / 视频 / 音频 / 文档），可按 kind 过滤。",
        TIER_READ,
        _obj({"kind": {"type": "string", "enum": ["image", "video", "audio", "other"]},
             "limit": {"type": "integer"}}),
        _list_assets,
    ),
    ToolSpec(
        "list_tasks", "列任务", "列出最近的任务与状态，可按 status 过滤（pending/processing/completed/failed）。",
        TIER_READ,
        _obj({"status": {"type": "string"}, "limit": {"type": "integer"}}),
        _list_tasks,
    ),
    ToolSpec(
        "get_task", "查任务", "看单个任务的详情（状态、进度、错误、关联产物）。跑完核对用。",
        TIER_READ, _obj({"task_id": {"type": "integer"}}, ["task_id"]), _get_task,
    ),
    ToolSpec(
        "get_asset", "查资产", "看单个资产的元信息（kind / url / 尺寸 / 来源）。",
        TIER_READ, _obj({"asset_id": {"type": "integer"}}, ["asset_id"]), _get_asset,
    ),
    ToolSpec(
        "list_engines", "列本机引擎", "列出本机引擎（配音/抠图/超分/补帧等）与是否已装、这台机器能不能跑。",
        TIER_READ, _no_args(), _list_engines,
    ),
    ToolSpec(
        "list_prompt_templates", "列提示词模板", "列出可用的提示词模板（标题/适用场景/正文前 300 字）。",
        TIER_READ, _obj({"limit": {"type": "integer"}}), _list_prompt_templates,
    ),
    ToolSpec(
        "list_models", "列模型", "列出当前可用的模型（默认文本）。模型不支持工具调用时要如实告诉用户。",
        TIER_READ,
        _obj({"modality": {"type": "string", "enum": ["text", "image", "video", "audio"]}}),
        _list_models,
    ),
    ToolSpec(
        "create_project", "新建画布", "新建一张自由画布（同名不重复建，会返回已有的 id）。",
        TIER_WRITE,
        _obj({"name": {"type": "string"}, "description": {"type": "string"}}, ["name"]),
        _create_project,
    ),
    ToolSpec(
        "draft_canvas", "生成画布草稿", "用一句话需求生成一份画布草稿（节点 + 连线 + 说明），**不落定**。",
        TIER_WRITE,
        _obj({"brief": {"type": "string", "description": "用户的需求原话，越具体越好"}}, ["brief"]),
        _draft_canvas,
    ),
    ToolSpec(
        "apply_canvas_draft", "落定草稿", "把这次会话里最近一份 draft_canvas 草稿写到画布上。",
        TIER_WRITE,
        _obj({"project_id": {"type": "integer"}}),
        _apply_canvas_draft,
    ),
    ToolSpec(
        "update_node", "改节点", "改单个节点的提示词或参数（参数只接受契约里允许的键）。",
        TIER_WRITE,
        _obj({
            "project_id": {"type": "integer"},
            "node_id": {"type": "string"},
            "prompt": {"type": "string"},
            "params": {"type": "object"},
        }, ["node_id"]),
        _update_node,
    ),
    ToolSpec(
        "connect_nodes", "连线", "把两个节点连起来（端口类型不匹配会被拒绝）。",
        TIER_WRITE,
        _obj({"project_id": {"type": "integer"}, "source": {"type": "string"}, "target": {"type": "string"}},
             ["source", "target"]),
        _connect_nodes,
    ),
    ToolSpec(
        "add_image_group", "铺图片组",
        "在上游节点后面接一个图片组：按上游表格逐行出图（一个节点产出 N 张、按行号保序、"
        "每行只挂它提到的资产设定图）。比让模型现场铺 N 个并列图片节点更省、更稳。",
        TIER_WRITE,
        _obj(
            {
                "project_id": {"type": "integer"},
                "source_node_id": {"type": "string"},
                "model_key": {"type": "string"},
                "prompt": {"type": "string"},
                "style_key": {"type": "string"},
                "shot_limit": {"type": "integer"},
            },
            ["source_node_id"],
        ),
        _add_image_group,
    ),
    ToolSpec(
        "canvas_status", "画布进度", "看每个节点最近一次任务的状态与错误。跑完用它核对，别凭记忆说结果。",
        TIER_READ,
        _obj({"project_id": {"type": "integer"}}),
        _canvas_status,
    ),
    ToolSpec(
        "run_canvas", "执行整图", "执行整张画布。**不可逆、会花钱**，系统会先让用户确认。",
        TIER_IRREVERSIBLE,
        _obj({"project_id": {"type": "integer"}, "ack_calls": {"type": "integer"}}),
        _run_canvas,
        _preview_run_canvas,
    ),
    ToolSpec(
        "run_node", "执行单节点", "只执行一个节点。**不可逆、会花钱**，系统会先让用户确认。",
        TIER_IRREVERSIBLE,
        _obj({"project_id": {"type": "integer"}, "node_id": {"type": "string"}}, ["node_id"]),
        _run_node,
        _preview_run_node,
    ),
)

_INDEX: dict[str, ToolSpec] = {t.name: t for t in TOOLS}


def by_name(name: str) -> ToolSpec | None:
    return _INDEX.get(str(name or "").strip())


def openai_tools() -> list[dict]:
    return [t.openai_schema() for t in TOOLS]


def describe() -> list[dict]:
    """给界面看的工具清单（分组按权限）。"""
    return [
        {"name": t.name, "label": t.label, "tier": t.tier, "tierLabel": TIER_LABELS.get(t.tier, t.tier),
         "description": t.description}
        for t in TOOLS
    ]


async def execute(ctx: ToolContext, name: str, args: dict) -> dict:
    """执行一个工具。**任何异常都转成 {ok:false}**——绝不让一次工具失败把整轮打崩。"""
    spec = by_name(name)
    if spec is None:
        return {"ok": False, "error": f"没有叫 {name} 的工具", "hint": "只调用工具清单里的名字。"}
    started = time.monotonic()
    try:
        result = await spec.handler(ctx, args)
    except ToolError as e:
        result = {"ok": False, "error": str(e), "hint": e.hint}
    except Exception as e:  # noqa: BLE001
        logger.warning("工具 %s 执行失败：%s", name, e, exc_info=True)
        result = {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "hint": "这是内部错误；换个做法或让用户看看日志。",
        }
    result.setdefault("elapsed_ms", int((time.monotonic() - started) * 1000))
    return result
