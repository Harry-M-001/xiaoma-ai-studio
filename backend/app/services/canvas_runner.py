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
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import Asset, ComfyWorkflow, Project, ProviderService, Task
from app.registry.canvas_nodes import (
    NODE_SCHEMAS,
    is_asset_image,
    is_doc_kind,
    is_runnable,
)
from app.services import asset_sheet, provider_store, storage
from app.services.config_center_service import runtime_value
from app.services.runner import runner

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 2.0
_NODE_WAIT_TIMEOUT = 900  # 单节点等待上限（秒），视频任务轮询由 runner 自己推进
_PER_TASK_WAIT_EXTRA = 120  # 批量节点每多一个任务追加的等待预算（秒）
_MAX_TASKS_PER_NODE = 200  # 单节点最多回溯多少个历史任务（资产链批量运行后产物很多）
# 角色提及注入一次最多挂几张参考图：挂太多会稀释主体，也容易触发上游模型的参考图数量上限
MAX_MENTION_REFS = 3


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


def read_task_params(task: Task) -> dict:
    """容错读 params_json（历史任务可能有脏数据）。"""
    try:
        data = json.loads(task.params_json or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


async def _latest_task_assets(db: AsyncSession, project_id: int, node_id: int | str) -> list[Asset]:
    """取某画布节点最近一次成功任务的全部产物（按生成顺序）。

    资产链节点一次运行会产生 N 个任务（一行资产一个任务），这批任务的
    params_json 里带同一个 asset_batch 标记；这里按标记把整批产物收齐，
    否则节点上只会显示最后一个任务的图。普通节点没有该标记，行为不变（只取最新一个）。
    """
    row = await db.execute(
        select(Task)
        .where(
            Task.canvas_project_id == project_id,
            Task.canvas_node_id == str(node_id),
            Task.status == "completed",
        )
        .order_by(Task.id.desc())
        .limit(_MAX_TASKS_PER_NODE)
    )
    tasks = list(row.scalars().all())
    if not tasks:
        return []

    batch = read_task_params(tasks[0]).get("asset_batch")
    if batch:
        tasks = [t for t in tasks if read_task_params(t).get("asset_batch") == batch]
    else:
        tasks = tasks[:1]

    arow = await db.execute(
        select(Asset)
        .where(Asset.task_id.in_([t.id for t in tasks]))
        .order_by(Asset.id.asc())
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


async def _inject_asset_refs(
    db: AsyncSession, project_id: int, text: str, images: list[Asset]
) -> tuple[list[Asset], list[str]]:
    """角色提及注入：提示词里出现资产名 → 自动挂上该资产的设定图当参考图。

    口径（故意保守，宁可少挂也不乱挂）：
    - 只在同一画布项目内匹配，不同项目的同名角色不串味；
    - 精确子串匹配，不做模糊近义；
    - 按在文本里出现的位置排序（长名优先，避免「小马」抢「小马宝莉」的命中）；
    - 一次最多挂 MAX_MENTION_REFS 张，且不与上游/手动参考重复。

    返回 (新的参考图列表, 实际注入的资产名列表)。
    """
    if not text.strip():
        return images, []

    rows = await db.execute(
        select(Asset)
        .join(Task, Asset.task_id == Task.id)
        .where(
            Task.canvas_project_id == project_id,
            Asset.kind == "image",
            Asset.name != "",
        )
        .order_by(Asset.id.asc())
    )
    # 同名可能有多张（同一角色多次生成）：候选按生成顺序，优先用最早的那张
    by_name: dict[str, list[Asset]] = {}
    for a in rows.scalars().all():
        by_name.setdefault(a.name, []).append(a)

    hits = [(text.find(name), name) for name in by_name]
    hits = [h for h in hits if h[0] >= 0]
    if not hits:
        return images, []
    hits.sort(key=lambda h: (h[0], -len(h[1])))

    existing = {a.id for a in images}
    picked: list[Asset] = []
    names: list[str] = []
    for _idx, name in hits:
        if len(picked) >= MAX_MENTION_REFS:
            break
        for asset in by_name[name]:
            if asset.id in existing:
                continue
            existing.add(asset.id)
            picked.append(asset)
            names.append(name)
            break

    if picked:
        logger.info("画布 %s 提及注入参考图：%s", project_id, "、".join(names))
    return [*images, *picked], names


async def _create_asset_image_tasks(
    db: AsyncSession, project_id: int, node: dict, upstream_text: str, images: list[Asset]
) -> list[Task]:
    """资产设定图节点：解析上游资产表 → 逐行建一个图片任务。

    一行资产 = 一个任务，好处是单行失败不影响其它行，任务中心里也能逐个重试。
    这批任务带同一个 asset_batch 标记，节点上因此能一次看到整批产物。
    """
    data = node.get("data") or {}
    label = NODE_SCHEMAS["assetImage"]["label"]
    scope = str(data.get("assetScope") or "")
    rows = asset_sheet.filter_rows(asset_sheet.parse_asset_table(upstream_text), scope)
    if not rows:
        if scope and scope != "all":
            raise ValueError(f"「{label}」当前生成范围内没有资产行，请把范围改回「全部」")
        raise ValueError(
            f"「{label}」没能从上游内容里解析出资产表："
            "请先运行上游「资产表」节点并确认它产出的是 Markdown 表格"
        )

    model_key = str(data.get("model_key") or "")
    if not model_key:
        raise ValueError(f"节点「{label}」未选择模型")
    resolved = await provider_store.resolve_model(db, model_key, "image")

    size = str(data.get("size") or runtime_value("defaults.image_size", "1024x1024") or "1024x1024")
    per_row = max(1, min(4, int(data.get("n") or 1)))
    style = str(data.get("prompt") or "").strip()
    batch = f"{int(time.time() * 1000):x}-{node['id']}"
    ref_ids = [a.id for a in images]

    tasks: list[Task] = []
    for row in rows:
        params: dict = {
            "size": size,
            "n": per_row,
            # 资产身份：落库后下游就能按名字自动引用（角色提及注入）
            "asset_name": row.name,
            "asset_category": row.category,
            "asset_batch": batch,
        }
        if ref_ids:
            params["ref_asset_ids"] = ref_ids
        task = Task(
            kind="image",
            status="pending",
            service_id=resolved.service.id,
            model=model_key,
            prompt=asset_sheet.build_reference_prompt(row, style),
            params_json=json.dumps(params, ensure_ascii=False),
            canvas_project_id=project_id,
            canvas_node_id=str(node["id"]),
        )
        db.add(task)
        tasks.append(task)

    await db.commit()
    for t in tasks:
        await db.refresh(t)
    logger.info("画布 %s 资产设定图：%s 行 → %s 个任务", project_id, len(rows), len(tasks))
    return tasks


async def _apply_manual_refs(db: AsyncSession, data: dict, images: list[Asset]) -> list[Asset]:
    """手动选择的参考（图库/上传）优先于上游产物 —— 与 dola-v2 同口径。

    注意按 refImages 数组顺序组装（首尾帧模式 [0]=首帧 [1]=尾帧，不能按资产 id 排序）。
    """
    manual_ids = _manual_ref_ids(data)
    if not manual_ids:
        return images
    arow = await db.execute(select(Asset).where(Asset.id.in_(manual_ids)))
    by_id = {a.id: a for a in arow.scalars().all() if a.kind == "image"}
    picked = [by_id[i] for i in manual_ids if i in by_id]
    return picked or images


async def _create_node_task(
    db: AsyncSession,
    project_id: int,
    node: dict,
    prompt: str,
    images: list[Asset],
    videos: list[Asset],
    upstream_text: str = "",
    injected_names: list[str] | None = None,
) -> Task:
    ntype = node["type"]
    data = node.get("data") or {}

    # 手动选择的参考（图库/上传）优先于上游产物
    images = await _apply_manual_refs(db, data, images)

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

    # 记下自动挂了哪些资产参考图，方便在任务详情里回溯"这张图为什么长这样"
    if injected_names:
        params["injected_names"] = injected_names

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
    - 资产设定图：输入完全来自上游资产表，节点自己的「统一风格」可留空
    - 其余节点：必须有提示词
    """
    ntype = node["type"]
    if ntype == "workflow":
        return True
    if is_asset_image(ntype):
        return bool(upstream_text.strip())
    if is_doc_kind(ntype):
        own = str((node.get("data") or {}).get("prompt") or "").strip()
        return bool(upstream_text.strip() or own)
    return bool(prompt.strip())


# 会消费参考图的节点类型（角色提及注入只对它们有意义）
_IMAGE_CONSUMERS = ("image", "video", "workflow")


async def _build_node_tasks(
    db: AsyncSession,
    project_id: int,
    doc: dict,
    node: dict,
    upstream: list[tuple[str, dict, list[Asset]]],
) -> list[Task]:
    """把一个画布节点翻译成待执行的任务列表。

    普通节点 = 1 个任务；资产设定图节点 = 每行资产 1 个任务。
    """
    prompt, images, videos, upstream_text = await _node_inputs(db, doc, node, upstream)
    if not _has_input(node, prompt, upstream_text):
        raise ValueError("节点没有可用的输入内容")

    data = node.get("data") or {}
    ntype = node["type"]

    if is_asset_image(ntype):
        return await _create_asset_image_tasks(
            db, project_id, node, upstream_text, await _apply_manual_refs(db, data, images)
        )

    injected: list[str] = []
    # 角色提及注入：提示词里提到资产名就自动挂设定图（可在节点上关掉）
    if ntype in _IMAGE_CONSUMERS and data.get("mentionRefs") is not False:
        images, injected = await _inject_asset_refs(db, project_id, prompt, images)

    return [
        await _create_node_task(
            db, project_id, node, prompt, images, videos, upstream_text, injected
        )
    ]


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


async def run_single_node(project_id: int, node_id: str) -> list[Task]:
    """运行单个节点：上游取最近成功产物，缺产物直接报错。

    返回本次派发出的全部任务（资产设定图节点会有多个）。
    """
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

        tasks = await _build_node_tasks(db, project_id, doc, node, upstream)

    for task in tasks:
        _dispatch(task)
    return tasks


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
                tasks = await _build_node_tasks(db, project_id, doc, node, upstream)
            except Exception as e:  # noqa: BLE001 - 单节点失败不中断整图
                failed.add(nid)
                logger.warning("画布 %s 节点 %s 准备失败：%s", project_id, nid, e)
                continue

        for task in tasks:
            _dispatch(task)

        # 轮询等待整批完成（runner 异步推进）
        # 资产设定图一批可能有二十几个任务，等待预算要按任务数放宽，
        # 否则一个慢模型就能把整图执行误判成超时失败。
        task_ids = [t.id for t in tasks]
        budget = _NODE_WAIT_TIMEOUT + max(0, len(task_ids) - 1) * _PER_TASK_WAIT_EXTRA
        deadline = asyncio.get_event_loop().time() + budget
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            async with SessionLocal() as db:
                finished, broken = 0, False
                for tid in task_ids:
                    t = await db.get(Task, tid)
                    if t is None or t.status in ("failed", "cancelled"):
                        broken = True
                        finished += 1
                    elif t.status == "completed":
                        finished += 1
                if finished >= len(task_ids):
                    if broken:
                        failed.add(nid)
                    break
        else:
            failed.add(nid)
            logger.warning("画布 %s 节点 %s 等待超时", project_id, nid)


def start_full_graph(project_id: int) -> None:
    """启动整图执行（不阻塞请求）。"""
    asyncio.get_event_loop().create_task(_run_full_graph(project_id))
