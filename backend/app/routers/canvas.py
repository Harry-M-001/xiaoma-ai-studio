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
from app.services import canvas_runner, storage

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


@router.get("/contract")
async def canvas_contract() -> dict:
    """节点契约下发：前端属性面板与连线校验的数据源。"""
    return {"schemaVersion": CANVAS_SCHEMA_VERSION, "nodeSchemas": NODE_SCHEMAS}


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
        # 按任务顺序铺平产物（同一批任务是按资产表行序建的）
        assets: list[Asset] = []
        for t in sorted(node_batch[nid], key=lambda x: x.id):
            assets.extend(asset_by_task.get(t.id, []))
        if not assets:
            continue
        first = assets[0]
        st["assetId"] = first.id
        st["assetKind"] = first.kind
        st["assetUrl"] = f"/media/{first.filename}"
        st["assetName"] = first.name or first.original_name
        st["assets"] = [
            {
                "id": a.id,
                "url": f"/media/{a.filename}",
                "kind": a.kind,
                "name": a.name or a.original_name,
                "category": a.category,
            }
            for a in assets
        ]
        if first.kind == "document" and st["status"] == "completed":
            st["text"] = _read_text_preview(first.filename)
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
