"""画布 API：契约下发 / 文档存取 / 节点执行 / 状态查询。

连线校验已前移到前端本地（契约驱动）；后端在保存时 validate_document 兜底。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import Asset, Project, Task
from app.registry.canvas_nodes import (
    CANVAS_SCHEMA_VERSION,
    NODE_SCHEMAS,
    normalize_type,
    validate_document,
)
from app.services import canvas_agent, canvas_runner, storage

router = APIRouter(prefix="/api/canvas", tags=["canvas"])

# 状态查询一次最多回看多少个任务（资产链批量运行后单节点任务很多）
_MAX_STATUS_TASKS = 2000


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


class CanvasDocIn(BaseModel):
    nodes: list[dict] = []
    edges: list[dict] = []
    viewport: dict = {}


class RunNodeIn(BaseModel):
    node_id: str | None = None  # 不传 = 整图执行


class AgentPlanIn(BaseModel):
    brief: str = ""
    model_key: str = ""


@router.get("/contract")
async def canvas_contract() -> dict:
    """节点契约下发：前端属性面板与连线校验的数据源。"""
    return {"schemaVersion": CANVAS_SCHEMA_VERSION, "nodeSchemas": NODE_SCHEMAS}


@router.post("/agent/plan")
async def canvas_agent_plan(payload: AgentPlanIn, db: AsyncSession = Depends(get_db)) -> dict:
    """自然语言搭画布：**只出草稿**，不改画布、不建任务。

    落定交给前端（它本来就有画布状态与自动保存）。放服务端「先存草稿再让用户确认」
    会多出一份需要清理的中间态，而用户直接关掉页面就没人清。
    """
    draft = await canvas_agent.plan(db, payload.brief, model_key=payload.model_key)
    return draft.to_dict()


def _aggregate_status(tasks: list[Task]) -> dict:
    """把一批任务汇总成一个节点状态。

    资产设定图一行一个任务，只要还有一行在跑，节点就显示「运行中」，
    不能因为最后一行先跑完就误报已完成。
    """
    if not tasks:
        return {"status": "pending", "progress": 0, "error": None}

    running = [t for t in tasks if t.status in ("pending", "processing")]
    if running:
        avg = sum(t.progress or 0 for t in tasks) // len(tasks)
        return {"status": "processing", "progress": max(1, avg), "error": None}

    failed = [t for t in tasks if t.status in ("failed", "cancelled")]
    if failed:
        return {
            "status": "failed",
            "progress": sum(t.progress or 0 for t in tasks) // len(tasks),
            "error": next((t.error for t in failed if t.error), "部分资产生成失败"),
        }

    return {"status": "completed", "progress": 100, "error": None}


@router.get("/{project_id}")
async def get_canvas(project_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    if not project.canvas_json:
        return {"schemaVersion": CANVAS_SCHEMA_VERSION, "nodes": [], "edges": [], "viewport": {}}
    doc = json.loads(project.canvas_json)
    # 旧类型归一（imageEdit → image），前端契约表里已无旧键
    for n in doc.get("nodes", []):
        if isinstance(n, dict) and n.get("type"):
            n["type"] = normalize_type(n["type"])
    return doc


@router.put("/{project_id}")
async def save_canvas(project_id: int, doc: CanvasDocIn, db: AsyncSession = Depends(get_db)) -> dict:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    payload = {
        "schemaVersion": CANVAS_SCHEMA_VERSION,
        "nodes": doc.nodes,
        "edges": doc.edges,
        "viewport": doc.viewport,
    }
    try:
        nodes, edges = validate_document(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    project.canvas_json = json.dumps(
        {**payload, "nodes": nodes, "edges": edges}, ensure_ascii=False
    )
    await db.commit()
    return {"ok": True}


@router.get("/{project_id}/preview")
async def preview_canvas(project_id: int) -> dict:
    """整图执行前的预估：会派多少任务、花多少次调用、哪些节点已有产物会被重做。

    给「运行整图」的二次确认弹窗用。纯读：不建任务、不写库、不调模型。
    """
    try:
        return await canvas_runner.preview_graph(project_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{project_id}/run")
async def run_canvas(
    project_id: int, payload: RunNodeIn, db: AsyncSession = Depends(get_db)
) -> dict:
    project = await db.get(Project, project_id)
    if project is None or not project.canvas_json:
        raise HTTPException(status_code=404, detail="画布不存在")

    if payload.node_id:
        try:
            tasks = await canvas_runner.run_single_node(project_id, payload.node_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {
            "mode": "node",
            "nodeId": payload.node_id,
            # 资产设定图节点一行一个任务，所以除了首个任务 id，还把总数带回去
            "taskId": tasks[0].id if tasks else None,
            "taskIds": [t.id for t in tasks],
            "taskCount": len(tasks),
        }

    canvas_runner.start_full_graph(project_id)
    return {"mode": "graph", "nodeId": None, "taskId": None, "taskIds": [], "taskCount": 0}


@router.get("/{project_id}/status")
async def canvas_status(project_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    """每个可执行节点的最新任务状态与产物。

    媒体节点返回产物 URL（资产链节点一次运行有多张，全部返回）；
    文档节点（自动链）额外返回正文预览，前端据此在节点与浮框里直接展示生成的 Markdown。
    """
    rows = await db.execute(
        select(Task)
        .where(Task.canvas_project_id == project_id)
        .order_by(Task.id.desc())
        .limit(_MAX_STATUS_TASKS)
    )
    tasks_by_node: dict[str, list[Task]] = {}
    for t in rows.scalars().all():
        if t.canvas_node_id:
            tasks_by_node.setdefault(t.canvas_node_id, []).append(t)

    node_status: dict[str, dict] = {}
    node_batch: dict[str, list[Task]] = {}
    for nid, tasks in tasks_by_node.items():
        latest = tasks[0]
        batch = canvas_runner.read_task_params(latest).get("asset_batch")
        # 资产链节点一批任务 = 一次运行；其余节点只看最新一个任务
        batch_tasks = (
            [t for t in tasks if canvas_runner.read_task_params(t).get("asset_batch") == batch]
            if batch
            else [latest]
        )
        node_batch[nid] = batch_tasks
        node_status[nid] = {
            "taskId": latest.id,
            **_aggregate_status(batch_tasks),
            "assetId": None,
            "taskCount": len(batch_tasks),
            "injectedNames": canvas_runner.read_task_params(latest).get("injected_names") or [],
        }

    asset_rows = await db.execute(
        select(Asset).where(Asset.kind.in_(["image", "video", "document"]))
    )
    asset_by_task: dict[int, list[Asset]] = {}
    for a in asset_rows.scalars().all():
        if a.task_id is not None:
            asset_by_task.setdefault(a.task_id, []).append(a)

    for nid, st in node_status.items():
        # 按任务顺序铺平产物（同一批任务是按资产表行序 / 镜号建的），
        # 每个产物带上展示标签（镜号 / 资产名），前端网格才认得出哪张是哪张
        views: list[dict] = []
        doc_asset: Asset | None = None
        for t in sorted(node_batch[nid], key=lambda x: x.id):
            tparams = canvas_runner.read_task_params(t)
            for a in asset_by_task.get(t.id, []):
                views.append(canvas_runner.asset_view(a, tparams))
                if doc_asset is None and a.kind == "document":
                    doc_asset = a
        if not views:
            continue
        first = views[0]
        st["assetId"] = first["id"]
        st["assetKind"] = first["kind"]
        st["assetUrl"] = first["url"]
        st["assetName"] = first["name"]
        st["assets"] = views
        if doc_asset is not None and st["status"] == "completed":
            st["text"] = _read_text_preview(doc_asset.filename)
    return {"nodes": node_status}


_DOC_PREVIEW_CHARS = 6000


def _read_text_preview(filename: str) -> str:
    """读取文档产物前若干字符（本地文件，失败不影响其它节点状态）。"""
    try:
        text = storage.abs_path(filename).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) <= _DOC_PREVIEW_CHARS:
        return text
    return text[:_DOC_PREVIEW_CHARS] + "\n\n…（预览已截断，完整内容见资产库）"
