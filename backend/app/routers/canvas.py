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
            task = await canvas_runner.run_single_node(project_id, payload.node_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"mode": "node", "nodeId": payload.node_id, "taskId": task.id}

    canvas_runner.start_full_graph(project_id)
    return {"mode": "graph", "nodeId": None, "taskId": None}


@router.get("/{project_id}/status")
async def canvas_status(project_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    """每个可执行节点的最新任务状态与产物。

    媒体节点返回产物 URL；文档节点（自动链）额外返回正文预览，
    前端据此在节点与浮框里直接展示生成的 Markdown。
    """
    rows = await db.execute(
        select(Task).where(Task.canvas_project_id == project_id).order_by(Task.id.desc())
    )
    node_status: dict[str, dict] = {}
    for t in rows.scalars().all():
        nid = t.canvas_node_id
        if nid and nid not in node_status:
            node_status[nid] = {
                "taskId": t.id,
                "status": t.status,
                "assetId": None,
                "error": t.error,
                "progress": t.progress,
            }

    asset_rows = await db.execute(
        select(Asset).where(Asset.kind.in_(["image", "video", "document"]))
    )
    asset_by_task: dict[int, Asset] = {}
    for a in asset_rows.scalars().all():
        if a.task_id is not None:
            asset_by_task.setdefault(a.task_id, a)

    for nid, st in node_status.items():
        a = asset_by_task.get(st["taskId"])
        if a is None:
            continue
        st["assetId"] = a.id
        st["assetKind"] = a.kind
        st["assetUrl"] = f"/media/{a.filename}"
        st["assetName"] = a.original_name
        if a.kind == "document" and st["status"] == "completed":
            st["text"] = _read_text_preview(a.filename)
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
