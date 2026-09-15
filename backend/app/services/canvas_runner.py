"""画布执行编排：DAG 解析 → 复用统一 Task 体系执行。

设计原则（与星云创一致）：节点执行不另起炉灶，直接映射到 Task
（复用并发闸门 / 重试 / 取消 / 任务中心），节点 → Task 用
canvas_project_id + canvas_node_id 关联，产物通过 Asset.task_id 回溯。

节点语义（契约 v3）：
- text  = 纯素材节点，不执行，只承载文本供下游取用
- idea / novel / script / storyboard = 自动链文档节点，调 LLM 写 Markdown 正文
  （产物流入资产库，同时作为下游节点的输入文本）
- image = 生成+编辑合一：有参考图（上游/手动）即图生图，否则文生图
- video 按 node.data.mode 工作：text2video（纯文本）/ first_last（首帧+尾帧）/
  omni_ref（图片+视频全收，视频编辑类模型）
- workflow = ComfyUI 工作流：按参数映射表注入后提交本机 ComfyUI
- 视频产物原样传下游，不做抽帧转换
"""

from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import Asset, ComfyWorkflow, Project, ProviderService, Task
from app.registry.canvas_nodes import NODE_SCHEMAS, is_doc_kind, is_runnable
from app.services import provider_store, storage
from app.services.config_center_service import runtime_value
from app.services.runner import runner

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 2.0
_NODE_WAIT_TIMEOUT = 900  # 单节点等待上限（秒），视频任务轮询由 runner 自己推进


def _topo_order(nodes: list[dict], edges: list[dict]) -> list[str]:
    """拓扑排序节点 id；文档已过环校验，这里只需稳定排序。"""
    indeg = {n["id"]: 0 for n in nodes}
    out: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for e in edges:
        out[e["source"]].append(e["target"])
        indeg[e["target"]] += 1
    queue = sorted([k for k, v in indeg.items() if v == 0])
    order: list[str] = []
    while queue:
        u = queue.pop(0)
        order.append(u)
        for v in out[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                queue.append(v)
        queue.sort()
    return order


async def _latest_task_assets(db: AsyncSession, project_id: int, node_id: int | str) -> list[Asset]:
    """取某画布节点最近一次成功任务的全部产物（按生成顺序）。"""
    row = await db.execute(
        select(Task)
        .where(
            Task.canvas_project_id == project_id,
            Task.canvas_node_id == str(node_id),
            Task.status == "completed",
        )
        .order_by(Task.id.desc())
        .limit(1)
    )
    task = row.scalars().first()
    if task is None:
        return []
    arow = await db.execute(
        select(Asset).where(Asset.task_id == task.id).order_by(Asset.id.asc())
    )
    return list(arow.scalars().all())


def _text_node_output(node: dict, nodes_by_id: dict[str, dict], edges: list[dict], depth: int = 0) -> str:
    """文本节点输出内容：自身 prompt 优先；为空则继承上游文本节点内容（链式改写）。"""
    own = ((node.get("data") or {}).get("prompt") or "").strip()
    if own or depth > 16:
        return own
    parts: list[str] = []
    for e in edges:
        if e.get("target") != node["id"]:
            continue
        src = nodes_by_id.get(e.get("source"))
        if src is not None and src.get("type") == "text":
            t = _text_node_output(src, nodes_by_id, edges, depth + 1)
            if t:
                parts.append(t)
    return "\n".join(parts)


def _doc_asset_text(assets: list[Asset]) -> str:
    """读取上游文档节点的正文（Markdown 产物落盘在 storage 下）。"""
    parts: list[str] = []
    for a in assets:
        if a.kind != "document":
            continue
        try:
            text = storage.abs_path(a.filename).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


async def _node_inputs(
    db: AsyncSession,
    doc: dict,
    node: dict,
    upstream: list[tuple[str, dict, list[Asset]]],
) -> tuple[str, list[Asset], list[Asset], str]:
    """收集上游输入。

    返回 (prompt, images, videos, upstream_text)：
    - prompt        = 上游文本 + 本节点自己的 prompt（图片 / 视频 / 工作流节点用）
    - upstream_text = 只含上游文本（文本节点链式内容 + 文档节点正文）；
                      文档节点用它当「内容」，自己的 prompt 单独当「补充要求」
    - 图片/视频原样收集（视频不抽帧，由下游模式决定怎么用）
    """
    nodes_by_id = {n["id"]: n for n in doc.get("nodes", [])}
    edges = doc.get("edges", [])
    texts: list[str] = []
    fallback_texts: list[str] = []
    images: list[Asset] = []
    videos: list[Asset] = []

    for _src_id, src_node, assets in upstream:
        src_type = src_node["type"]
        if src_type == "text" or is_doc_kind(src_type):
            t = _text_node_output(src_node, nodes_by_id, edges)
            if is_doc_kind(src_type):
                # 文档节点：优先用已生成的正文，未运行过则回退到节点上的输入
                t = _doc_asset_text(assets) or t
            if t:
                texts.append(t)
            continue
        src_prompt = ((src_node.get("data") or {}).get("prompt") or "").strip()
        if src_prompt:
            fallback_texts.append(src_prompt)
        for a in assets:
            if a.kind == "image":
                images.append(a)
            elif a.kind == "video":
                videos.append(a)

    upstream_text = "\n\n".join(texts).strip()
    own_prompt = ((node.get("data") or {}).get("prompt") or "").strip()
    if own_prompt or texts:
        prompt = "\n".join([*texts, own_prompt]).strip()
    else:
        prompt = "\n".join(fallback_texts).strip()
    return prompt, images, videos, upstream_text


def _manual_ref_ids(data: dict) -> list[int]:
    return [
        int(r["id"])
        for r in (data.get("refImages") or [])
        if isinstance(r, dict) and r.get("id")
    ]


async def _create_node_task(
    db: AsyncSession,
    project_id: int,
    node: dict,
    prompt: str,
    images: list[Asset],
    videos: list[Asset],
    upstream_text: str = "",
) -> Task:
    ntype = node["type"]
    data = node.get("data") or {}

    # 手动选择的参考（图库/上传）优先于上游产物 —— 与 dola-v2 同口径。
    # 注意按 refImages 数组顺序组装（首尾帧模式 [0]=首帧 [1]=尾帧，不能按资产 id 排序）。
    manual_ids = _manual_ref_ids(data)
    if manual_ids:
        arow = await db.execute(select(Asset).where(Asset.id.in_(manual_ids)))
        by_id = {a.id: a for a in arow.scalars().all() if a.kind == "image"}
        picked = [by_id[i] for i in manual_ids if i in by_id]
        if picked:
            images = picked

    # 自动链文档节点：只让 LLM 写 Markdown 正文
    # 内容 = 上游正文（含上游文档产物），补充要求 = 本节点的 prompt
    if is_doc_kind(ntype):
        label = NODE_SCHEMAS[ntype]["label"]
        own_prompt = str(data.get("prompt") or "").strip()
        if not upstream_text.strip() and not own_prompt:
            raise ValueError(f"「{label}」没有输入内容：请填写内容，或连接上游节点")
        model_key = str(data.get("model_key") or "") or str(
            runtime_value("defaults.text_model", "") or ""
        )
        if not model_key:
            raise ValueError(f"请先为「{label}」选择文本模型（也可在系统设置中配置默认文本模型）")
        resolved = await provider_store.resolve_model(db, model_key, "text")
        node_params = {
            k: data.get(k)
            for k in ("chapterCount", "sceneCount", "shotCount")
            if data.get(k) is not None
        }
        task = Task(
            kind="text",
            status="pending",
            service_id=resolved.service.id,
            model=model_key,
            prompt=upstream_text,
            params_json=json.dumps(
                {"agent_key": ntype, "extra": own_prompt, "node_params": node_params},
                ensure_ascii=False,
            ),
            canvas_project_id=project_id,
            canvas_node_id=str(node["id"]),
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        return task

    # ComfyUI 工作流节点：不走模型解析，按工作流 ID + 参数表执行
    if ntype == "workflow":
        wf = await db.get(ComfyWorkflow, int(data.get("workflowId") or 0))
        if wf is None:
            raise ValueError("请先在节点浮框中选择 ComfyUI 工作流")
        provider = await db.get(ProviderService, wf.provider_id)
        if provider is None or not provider.enabled:
            raise ValueError("ComfyUI 服务不可用，请在「模型服务」中检查配置")
        params: dict = {
            "workflow_id": wf.id,
            "param_values": data.get("paramValues") or {},
        }
        if images:
            params["ref_asset_ids"] = [a.id for a in images]
        task = Task(
            kind="workflow",
            status="pending",
            service_id=provider.id,
            model=f"comfy:{wf.id}",
            prompt=prompt,
            params_json=json.dumps(params, ensure_ascii=False),
            canvas_project_id=project_id,
            canvas_node_id=str(node["id"]),
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        return task

    modality = "video" if ntype == "video" else "image"
    model_key = data.get("model_key") or ""
    if not model_key:
        raise ValueError(f"节点「{NODE_SCHEMAS[ntype]['label']}」未选择模型")
    resolved = await provider_store.resolve_model(db, model_key, modality)

    params = {}
    if ntype == "image":
        # 生成/编辑合一：有参考图=图生图（编辑），无=文生图
        params["size"] = data.get("size") or "1024x1024"
        params["n"] = int(data.get("n") or 1)
        if images:
            params["ref_asset_ids"] = [a.id for a in images]
    else:
        mode = data.get("mode") or "text2video"
        params["mode"] = mode
        params["duration"] = int(data.get("duration") or 5)
        params["ratio"] = data.get("ratio") or "16:9"
        params["resolution"] = data.get("resolution") or "720p"
        if mode == "text2video":
            pass  # 纯文本生成，上游/手动媒体一律忽略
        elif mode == "first_last":
            # 参考位约定：第 1 张=首帧，第 2 张=尾帧
            frames = images[:2]
            if not frames:
                raise ValueError("首尾帧模式需要至少一张首帧图片（连接上游图片节点或在图库选择）")
            params["first_frame_asset_id"] = frames[0].id
            if len(frames) >= 2:
                params["last_frame_asset_id"] = frames[1].id
        elif mode == "omni_ref":
            # 全能参考：图片+视频全部传入（视频编辑 / 多模态参考类模型）
            if images:
                params["ref_asset_ids"] = [a.id for a in images]
            if videos:
                params["video_ref_asset_ids"] = [v.id for v in videos]
        else:
            raise ValueError(f"未知的视频模式：{mode}")

    task = Task(
        kind=modality,
        status="pending",
        service_id=resolved.service.id,
        model=model_key,
        prompt=prompt,
        params_json=json.dumps(params, ensure_ascii=False),
        canvas_project_id=project_id,
        canvas_node_id=str(node["id"]),
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def _resolve_upstream(
    db: AsyncSession, project_id: int, doc: dict, node_id: str
) -> list[tuple[str, dict, list[Asset]]]:
    upstream: list[tuple[str, dict, list[Asset]]] = []
    nodes = {n["id"]: n for n in doc.get("nodes", [])}
    for e in doc.get("edges", []):
        if e.get("target") != node_id:
            continue
        src = nodes.get(e.get("source"))
        if src is None:
            continue
        if src["type"] == "text":
            upstream.append((src["id"], src, []))
            continue
        assets = await _latest_task_assets(db, project_id, src["id"])
        upstream.append((src["id"], src, assets))
    return upstream


def _missing_upstream_labels(upstream: list[tuple[str, dict, list[Asset]]]) -> list[str]:
    """上游媒体节点没有产物的标签列表（文本节点不算）。"""
    return [
        NODE_SCHEMAS[s["type"]]["label"]
        for _sid, s, assets in upstream
        if s["type"] != "text" and not assets
    ]


def _has_input(node: dict, prompt: str, upstream_text: str) -> bool:
    """节点是否有可执行输入。

    - 工作流节点：提示词可空（参数与工作流本身足够）
    - 文档节点：上游正文或本节点的「补充要求」有其一即可
    - 其余节点：必须有提示词
    """
    ntype = node["type"]
    if ntype == "workflow":
        return True
    if is_doc_kind(ntype):
        own = str((node.get("data") or {}).get("prompt") or "").strip()
        return bool(upstream_text.strip() or own)
    return bool(prompt.strip())


def _dispatch(task: Task) -> None:
    """按任务类型交给对应的执行通道。"""
    if task.kind == "video":
        runner.start_video(task.id)
    elif task.kind == "workflow":
        runner.start_comfy(task.id)
    elif task.kind == "text":
        runner.start_text(task.id)
    else:
        runner.start_image(task.id)


async def run_single_node(project_id: int, node_id: str) -> Task:
    """运行单个节点：上游取最近成功产物，缺产物直接报错。"""
    async with SessionLocal() as db:
        project = await db.get(Project, project_id)
        if project is None or not project.canvas_json:
            raise ValueError("画布不存在")
        doc = json.loads(project.canvas_json)
        nodes = {n["id"]: n for n in doc.get("nodes", [])}
        node = nodes.get(node_id)
        if node is None:
            raise ValueError("节点不存在")
        if not is_runnable(node["type"]):
            raise ValueError("文本节点无需运行，保存后直接供下游使用")

        upstream = await _resolve_upstream(db, project_id, doc, node_id)
        # 全能参考可消费上游视频，视频产物也算可用输入；但缺产物仍要求先运行
        missing = _missing_upstream_labels(upstream)
        if missing:
            raise ValueError(f"上游节点（{'、'.join(missing)}）还没有产物，请先运行它们")

        prompt, images, videos, upstream_text = await _node_inputs(db, doc, node, upstream)
        if not _has_input(node, prompt, upstream_text):
            raise ValueError("节点没有可用的输入内容")
        task = await _create_node_task(
            db, project_id, node, prompt, images, videos, upstream_text
        )

    _dispatch(task)
    return task


async def _run_full_graph(project_id: int) -> None:
    """整图执行：后台协程按拓扑序逐节点跑，上游失败则下游跳过。"""
    async with SessionLocal() as db:
        project = await db.get(Project, project_id)
        if project is None or not project.canvas_json:
            return
        doc = json.loads(project.canvas_json)
        nodes = {n["id"]: n for n in doc.get("nodes", [])}
        order = _topo_order(doc.get("nodes", []), doc.get("edges", []))

    failed: set[str] = set()
    for nid in order:
        node = nodes[nid]
        if not is_runnable(node["type"]):
            continue
        # 上游有失败/被跳过的节点则跳过本节点
        blocked = False
        async with SessionLocal() as db:
            for e in doc.get("edges", []):
                if e.get("target") == nid and e.get("source") in failed:
                    blocked = True
                    break
            if blocked:
                failed.add(nid)
                logger.info("画布 %s 节点 %s 因上游失败跳过", project_id, nid)
                continue
            try:
                upstream = await _resolve_upstream(db, project_id, doc, nid)
                prompt, images, videos, upstream_text = await _node_inputs(db, doc, node, upstream)
                if not _has_input(node, prompt, upstream_text):
                    raise ValueError("节点没有可用的输入内容")
                task = await _create_node_task(
                    db, project_id, node, prompt, images, videos, upstream_text
                )
            except Exception as e:  # noqa: BLE001 - 单节点失败不中断整图
                failed.add(nid)
                logger.warning("画布 %s 节点 %s 准备失败：%s", project_id, nid, e)
                continue

        _dispatch(task)

        # 轮询等待完成（runner 异步推进）
        deadline = asyncio.get_event_loop().time() + _NODE_WAIT_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            async with SessionLocal() as db:
                t = await db.get(Task, task.id)
                if t and t.status in ("completed", "failed", "cancelled"):
                    if t.status != "completed":
                        failed.add(nid)
                    break
        else:
            failed.add(nid)
            logger.warning("画布 %s 节点 %s 等待超时", project_id, nid)


def start_full_graph(project_id: int) -> None:
    """启动整图执行（不阻塞请求）。"""
    asyncio.get_event_loop().create_task(_run_full_graph(project_id))
