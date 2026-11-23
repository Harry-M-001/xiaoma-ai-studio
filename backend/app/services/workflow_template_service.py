"""工作流模板：把一小段画布存下来，插到别的项目里复用。

解决的是「同一套成熟链路每次都要重搭一遍」：模板 = 节点 + 连线 + 输入说明。
- **内置三份**（图文广告 / 漫剧分镜 / 逐镜成片）由代码提供，跟自动链是同一套拓扑；
- 用户也可以把画布上选中的一段存成模板；
- 插入时重新分配节点 id、整体往右让开已有内容，并把 `{{输入名}}` 换成用户填的值；
  缺模型的节点按能力自动挑一个可用模型，挑不到就由预检如实说出来。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, WorkflowTemplate
from app.registry.canvas_nodes import (
    CANVAS_SCHEMA_VERSION,
    NODE_SCHEMAS,
    normalize_type,
    validate_document,
)
from app.services import provider_store

# 能力 → 节点类型。只列会真调模型的那几类；成片与工作流由本机执行
_IMAGE_KINDS = {"image", "assetImage", "storyboardImage", "imageGroup"}


def _modality_of(ntype: str) -> str:
    t = normalize_type(ntype)
    if t == "video":
        return "video"
    if t in _IMAGE_KINDS:
        return "image"
    if t in ("workflow", "film", "text"):
        return ""
    return "text"  # idea / novel / script / storyboard / assetSheet 等文档节点


def _requirements(nodes: list[dict]) -> dict[str, Any]:
    mods = sorted({_modality_of(str(n.get("type") or "")) for n in nodes} - {""})
    workflows = [
        int((n.get("data") or {}).get("workflowId") or 0)
        for n in nodes
        if normalize_type(str(n.get("type") or "")) == "workflow"
    ]
    return {"modalities": mods, "workflows": [w for w in workflows if w]}


# ---------------------------------------------------------------- 内置模板


_BUILTINS: list[dict[str, Any]] = [
    {
        "key": "ad_image",
        "name": "图文广告",
        "description": "卖点 → 一句文案 → 一张主图。只有文本与图片模型就能跑。",
        "inputs": [
            {
                "key": "prompt",
                "label": "产品与卖点",
                "hint": "例如：保温杯，24 小时不凉，送给通勤的人",
            }
        ],
        "nodes": [
            ("idea", 0, 0, {}),
            ("image", 1, 0, {"size": "1024x1024", "n": 1}),
        ],
        "edges": [("idea", "image")],
    },
    {
        "key": "storyboard_chain",
        "name": "漫剧分镜",
        "description": "想法 → 小说 → 剧本 → 分镜 → 资产设定图 → 逐镜分镜图。",
        "inputs": [
            {
                "key": "prompt",
                "label": "一句话想法",
                "hint": "例如：外卖小哥其实是隐退的顶级保镖",
            }
        ],
        "nodes": [
            ("idea", 0, 0, {}),
            ("novel", 1, 0, {"chapterCount": 2}),
            ("script", 2, 0, {"sceneCount": 2}),
            ("storyboard", 3, 0, {"shotCount": 6}),
            ("assetSheet", 4, 0, {}),
            ("assetImage", 5, 0, {}),
            ("storyboardImage", 4, 1, {"shotLimit": 6}),
        ],
        "edges": [
            ("idea", "novel"),
            ("novel", "script"),
            ("script", "storyboard"),
            ("storyboard", "assetSheet"),
            ("assetSheet", "assetImage"),
            ("storyboard", "storyboardImage"),
            ("assetImage", "storyboardImage"),
        ],
    },
    {
        "key": "shot_video_chain",
        "name": "逐镜成片",
        "description": "在漫剧分镜之后接逐镜视频与成片：跑通就能拿到一条按顺序合成的粗剪。",
        "inputs": [
            {
                "key": "prompt",
                "label": "一句话想法",
                "hint": "例如：雨夜巷口，撑伞的人没有退一步",
            }
        ],
        "nodes": [
            ("idea", 0, 0, {}),
            ("novel", 1, 0, {"chapterCount": 2}),
            ("script", 2, 0, {"sceneCount": 2}),
            ("storyboard", 3, 0, {"shotCount": 6}),
            ("assetSheet", 4, 0, {}),
            ("assetImage", 5, 0, {}),
            ("storyboardImage", 4, 1, {"shotLimit": 6}),
            ("video", 5, 1, {"shotVideo": "chain", "mode": "first_last"}),
            ("film", 6, 1, {}),
        ],
        "edges": [
            ("idea", "novel"),
            ("novel", "script"),
            ("script", "storyboard"),
            ("storyboard", "assetSheet"),
            ("assetSheet", "assetImage"),
            ("storyboard", "storyboardImage"),
            ("assetImage", "storyboardImage"),
            ("storyboard", "video"),
            ("storyboardImage", "video"),
            ("video", "film"),
        ],
    },
]


def _builtin_doc(defn: dict[str, Any]) -> dict[str, Any]:
    """按类型生成节点与连线；端口取该类型契约里的第一个，和前端自动链同源。"""
    id_by_type: dict[str, str] = {}
    nodes: list[dict] = []
    for i, (ntype, col, row, extra) in enumerate(defn["nodes"]):
        nid = f"b_{defn['key']}_{i}"
        id_by_type.setdefault(ntype, nid)
        data = {"prompt": "", "model_key": ""}
        if normalize_type(ntype) == "video":
            data["mode"] = "text2video"
        data.update(extra)
        nodes.append(
            {
                "id": nid,
                "type": ntype,
                "position": {"x": 80 + int(col) * 300, "y": 140 + int(row) * 240},
                "data": data,
            }
        )
    edges: list[dict] = []
    for i, (src_type, dst_type) in enumerate(defn["edges"]):
        src, dst = id_by_type[src_type], id_by_type[dst_type]
        out = NODE_SCHEMAS[src_type]["handles"].get("sources") or []
        inc = NODE_SCHEMAS[dst_type]["handles"].get("targets") or []
        edges.append(
            {
                "id": f"b_{defn['key']}_e{i}",
                "source": src,
                "sourceHandle": out[0]["id"] if out else None,
                "target": dst,
                "targetHandle": inc[0]["id"] if inc else None,
            }
        )
    return {"nodes": nodes, "edges": edges}


def _builtin_meta(defn: dict[str, Any]) -> dict[str, Any]:
    doc = _builtin_doc(defn)
    return {
        "id": f"builtin:{defn['key']}",
        "source": "builtin",
        "name": defn["name"],
        "description": defn["description"],
        "inputs": defn["inputs"],
        "nodeCount": len(doc["nodes"]),
        "version": 1,
        "requirements": _requirements(doc["nodes"]),
    }


# ---------------------------------------------------------------- 读取 / 新建


def _custom_meta(row: WorkflowTemplate) -> dict[str, Any]:
    try:
        doc = json.loads(row.doc_json or "{}")
    except json.JSONDecodeError:
        doc = {}
    return {
        "id": f"custom:{row.id}",
        "source": "custom",
        "name": row.name,
        "description": row.description,
        "inputs": json.loads(row.inputs_json or "[]"),
        "nodeCount": len(doc.get("nodes") or []),
        "version": row.version,
        "requirements": json.loads(row.requirements_json or "{}"),
    }


async def list_templates(db: AsyncSession) -> list[dict[str, Any]]:
    """内置在前，自己存的在后（新存的排前面）。"""
    out = [_builtin_meta(t) for t in _BUILTINS]
    rows = (
        await db.execute(select(WorkflowTemplate).order_by(WorkflowTemplate.id.desc()))
    ).scalars().all()
    out.extend(_custom_meta(r) for r in rows)
    return out


async def _load(db: AsyncSession, ref: str) -> dict[str, Any]:
    ref = str(ref or "")
    if ref.startswith("builtin:"):
        key = ref.split(":", 1)[1]
        defn = next((t for t in _BUILTINS if t["key"] == key), None)
        if defn is None:
            raise ValueError("内置模板不存在")
        return {
            "name": defn["name"],
            "description": defn["description"],
            "inputs": defn["inputs"],
            "doc": _builtin_doc(defn),
        }
    if ref.startswith("custom:"):
        try:
            row = await db.get(WorkflowTemplate, int(ref.split(":", 1)[1]))
        except ValueError:
            row = None
        if row is None:
            raise ValueError("模板不存在")
        return {
            "name": row.name,
            "description": row.description,
            "inputs": json.loads(row.inputs_json or "[]"),
            "doc": json.loads(row.doc_json or '{"nodes": [], "edges": []}'),
        }
    raise ValueError("模板标识不合法")


async def create_template(db: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    """把一段画布存成模板（前端传选中的节点与它们之间的连线）。"""
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("模板要有个名字")
    doc = {
        "schemaVersion": CANVAS_SCHEMA_VERSION,
        "nodes": payload.get("nodes") or [],
        "edges": payload.get("edges") or [],
    }
    nodes, edges = validate_document(doc)
    if not nodes:
        raise ValueError("模板里至少要有节点")
    inputs = payload.get("inputs") or []
    if not isinstance(inputs, list):
        raise ValueError("输入说明必须是数组")
    row = WorkflowTemplate(
        name=name[:120],
        description=str(payload.get("description") or "")[:2000],
        doc_json=json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False),
        inputs_json=json.dumps(inputs, ensure_ascii=False),
        requirements_json=json.dumps(_requirements(nodes), ensure_ascii=False),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _custom_meta(row)


# ---------------------------------------------------------------- 预检


async def preflight(db: AsyncSession, ref: str) -> dict[str, Any]:
    """插进去之前先看「这台机器能不能跑」：缺哪些能力、哪个节点引用了不存在的模型。"""
    tpl = await _load(db, ref)
    nodes = tpl["doc"].get("nodes") or []
    req = _requirements(nodes)
    options = provider_store.list_model_options(await provider_store.list_services(db))
    have = sorted({o.modality for o in options})
    missing_mods = [m for m in req["modalities"] if m not in have]
    missing_nodes: list[dict] = []
    for n in nodes:
        ntype = normalize_type(str(n.get("type") or ""))
        mod = _modality_of(ntype)
        if not mod:
            continue
        label = NODE_SCHEMAS.get(ntype, {}).get("label") or ntype
        model_key = str((n.get("data") or {}).get("model_key") or "")
        if model_key:
            try:
                await provider_store.resolve_model(db, model_key, mod)
            except Exception:  # noqa: BLE001 - 预检只回答「能不能跑」，不抛错
                missing_nodes.append({"nodeId": n["id"], "label": label, "model": model_key})
        elif mod not in have:
            missing_nodes.append({"nodeId": n["id"], "label": label, "model": ""})
    return {
        "ok": not missing_mods and not missing_nodes,
        "name": tpl["name"],
        "required": req,
        "available": have,
        "missingModalities": missing_mods,
        "missingNodes": missing_nodes,
    }


# ---------------------------------------------------------------- 插入


def _fill_values(node: dict, values: dict[str, Any], *, is_first: bool) -> dict:
    data = dict(node.get("data") or {})
    for key, val in list(data.items()):
        if isinstance(val, str) and "{{" in val:
            for name, given in values.items():
                val = val.replace("{{" + str(name) + "}}", str(given))
            data[key] = val
    # 约定：第一个节点承接「主题输入」；它自己的提示词为空时才填，
    # 避免把模板作者写好的固定文案盖掉
    if is_first and values.get("prompt") and not str(data.get("prompt") or "").strip():
        data["prompt"] = str(values["prompt"])
    return {**node, "data": data}


async def instantiate(
    db: AsyncSession,
    project_id: int,
    ref: str,
    values: dict[str, Any] | None = None,
    base_revision: int | None = None,
) -> dict[str, Any]:
    """把模板插进项目画布：新 id、右移让位、填输入、补可用模型。

    画布保存同样走「比较并交换」：模板插入也是一次写画布，不能绕过修订号。
    """
    values = values or {}
    tpl = await _load(db, ref)
    project = await db.get(Project, project_id)
    if project is None:
        raise ValueError("项目不存在")

    existing = json.loads(project.canvas_json) if project.canvas_json else {}
    old_nodes = existing.get("nodes") or []
    old_edges = existing.get("edges") or []
    offset_x = max((int((n.get("position") or {}).get("x") or 0) for n in old_nodes), default=0) + 360

    options = provider_store.list_model_options(await provider_store.list_services(db))
    first_by_mod: dict[str, str] = {}
    for o in options:
        first_by_mod.setdefault(o.modality, o.key)

    stamp = uuid.uuid4().hex[:8]
    id_map: dict[str, str] = {}
    new_nodes: list[dict] = []
    for i, node in enumerate(tpl["doc"].get("nodes") or []):
        filled = _fill_values(node, values, is_first=(i == 0))
        nid = f"tpl_{stamp}_{i}"
        id_map[str(node["id"])] = nid
        data = dict(filled.get("data") or {})
        mod = _modality_of(str(filled.get("type") or ""))
        if mod and not str(data.get("model_key") or "") and mod in first_by_mod:
            data["model_key"] = first_by_mod[mod]
        pos = filled.get("position") or {"x": 0, "y": 0}
        new_nodes.append(
            {
                "id": nid,
                "type": normalize_type(str(filled.get("type") or "")),
                "position": {"x": int(pos.get("x") or 0) + offset_x, "y": int(pos.get("y") or 0)},
                "data": data,
            }
        )
    new_edges = []
    for i, e in enumerate(tpl["doc"].get("edges") or []):
        src = id_map.get(str(e.get("source")))
        dst = id_map.get(str(e.get("target")))
        if not src or not dst:
            continue
        new_edges.append(
            {
                "id": f"tpl_{stamp}_e{i}",
                "source": src,
                "sourceHandle": e.get("sourceHandle"),
                "target": dst,
                "targetHandle": e.get("targetHandle"),
            }
        )

    merged = {
        "schemaVersion": CANVAS_SCHEMA_VERSION,
        "nodes": old_nodes + new_nodes,
        "edges": old_edges + new_edges,
        "viewport": existing.get("viewport") or {},
    }
    nodes, edges = validate_document(merged)
    body = json.dumps({**merged, "nodes": nodes, "edges": edges}, ensure_ascii=False)

    stmt = (
        update(Project)
        .where(Project.id == project_id)
        .values(canvas_json=body, canvas_revision=Project.canvas_revision + 1)
    )
    if base_revision is not None:
        stmt = stmt.where(Project.canvas_revision == int(base_revision))
    result = await db.execute(stmt)
    await db.commit()
    if result.rowcount == 0:
        await db.refresh(project)
        raise ValueError(
            f"画布已在别处被修改（服务端现在是第 {project.canvas_revision} 版），"
            "模板没有插入。请重新加载画布后再试。"
        )
    await db.refresh(project)
    return {
        "ok": True,
        "template": tpl["name"],
        "nodeIds": [n["id"] for n in new_nodes],
        "revision": project.canvas_revision,
    }
