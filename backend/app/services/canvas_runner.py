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
    is_batch_image,
    is_doc_kind,
    is_runnable,
    is_storyboard_image,
)
from app.services import asset_sheet, provider_store, storage, storyboard_sheet, style_service
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


def _new_batch_id(node: dict) -> str:
    """一批任务的批次标记。

    批量节点（资产设定图 / 分镜图）一次运行会派发 N 个任务，它们共享这个标记，
    节点状态因此能把整批产物一次收齐（见 `_latest_task_assets` 与画布 /status）。
    """
    return f"{int(time.time() * 1000):x}-{node['id']}"


def asset_view(asset: Asset, params: dict | None = None) -> dict:
    """产物在画布上的展示视图：短标签 + 悬停说明。

    批量节点一次出很多张图，用户得一眼看出"哪张是哪一镜 / 哪个资产"：
    - 分镜图：标签用镜号（`镜头3`），说明里补景别运镜与自动挂的参考图；
    - 资产设定图：标签用资产名，说明里补类型；
    - 其余产物（文档 / 视频 / 单图）：退回文件名。
    """
    data = params or {}
    shot = str(data.get("shot_no") or "").strip()
    if shot:
        label = f"镜头{shot}"
        title = str(data.get("shot_label") or label).strip() or label
        injected = data.get("injected_names") or []
        if injected:
            title = f"{title} · 参考：{'、'.join(str(n) for n in injected)}"
    else:
        name = (asset.name or "").strip()
        label = name
        title = name or asset.original_name
        if name and asset.category:
            title = f"{name} · {asset.category}"
    return {
        "id": asset.id,
        "url": f"/media/{asset.filename}",
        "kind": asset.kind,
        "name": asset.name or asset.original_name,
        "category": asset.category,
        "label": label,
        "title": title,
    }


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


def _doc_override_text(node: dict) -> str:
    """用户在画布上直接编辑过的正文（存在节点 data 的 `docText`）。

    这条覆盖链的用意：文档节点生成完常常要手改几个字（改个名字、删一句），
    为此重跑整条链既慢又费钱。所以手改的正文优先给下游用，
    用户也不用担心重跑之后自己的修改被悄悄丢掉。
    """
    return str((node.get("data") or {}).get("docText") or "").strip()


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
                # 文档节点：手改正文 > 已生成的正文 > 节点上的输入
                t = _doc_override_text(src_node) or _doc_asset_text(assets) or t
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
    db: AsyncSession,
    project_id: int,
    node: dict,
    upstream_text: str,
    images: list[Asset],
    dry_run: bool = False,
) -> list[Task]:
    """资产设定图节点：解析上游资产表 → 逐行建一个图片任务。

    一行资产 = 一个任务，好处是单行失败不影响其它行，任务中心里也能逐个重试。
    这批任务带同一个 asset_batch 标记，节点上因此能一次看到整批产物。

    `dry_run=True` 时只把任务对象造出来、**不落库**，用来回答「这一跑会派多少个任务」。
    预估与真跑共用这一段，是为了让「弹窗上写的数」与「实际派发的数」不可能对不上。
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
    batch = _new_batch_id(node)
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
        if not dry_run:
            db.add(task)
        tasks.append(task)

    if dry_run:
        return tasks
    await db.commit()
    for t in tasks:
        await db.refresh(t)
    logger.info("画布 %s 资产设定图：%s 行 → %s 个任务", project_id, len(rows), len(tasks))
    return tasks


async def _create_storyboard_image_tasks(
    db: AsyncSession,
    project_id: int,
    node: dict,
    upstream_text: str,
    manual: list[Asset],
    card: style_service.StyleCard | None,
    dry_run: bool = False,
) -> list[Task]:
    """分镜图节点：解析上游分镜表 → 逐镜建一个图片任务。

    与资产设定图的关键差别：**每一镜的参考图是单独挑的**。
    分镜的「画面」行里通常写着角色中文名，所以逐镜做一次角色提及注入，
    就能把"这一镜该长什么样"的设定图挂上去，而不是把整批设定图全塞进去。
    """
    data = node.get("data") or {}
    label = NODE_SCHEMAS["storyboardImage"]["label"]

    shots = storyboard_sheet.parse_storyboard(upstream_text)
    if not shots:
        raise ValueError(
            f"「{label}」没能从上游内容里解析出分镜表："
            "请先运行上游「分镜」节点，并确认它产出的是镜头表"
            "（`### 镜头N | 景别 | 运镜 | 时长s` + `- 画面：` / `- 首帧提示词：`）"
        )
    total_found = len(shots)
    shots = _dedupe_shots(
        storyboard_sheet.limit_shots(shots, int(data.get("shotLimit") or 0)), label
    )

    model_key = str(data.get("model_key") or "")
    if not model_key:
        raise ValueError(f"节点「{label}」未选择模型")
    resolved = await provider_store.resolve_model(db, model_key, "image")

    size = str(data.get("size") or runtime_value("defaults.image_size", "1024x1024") or "1024x1024")
    per_shot = max(1, min(4, int(data.get("n") or 1)))
    extra = str(data.get("prompt") or "").strip()
    style_suffix = style_service.image_suffix(card)
    mention_on = data.get("mentionRefs") is not False
    batch = _new_batch_id(node)

    tasks: list[Task] = []
    for shot in shots:
        refs = list(manual)
        injected: list[str] = []
        if mention_on:
            refs, injected = await _inject_asset_refs(db, project_id, shot.scan_text, refs)
        prompt = style_service.append_style(shot.image_prompt, extra)
        prompt = style_service.append_style(prompt, style_suffix)
        params: dict = {
            "size": size,
            "n": per_shot,
            "shot_no": shot.no,
            "shot_label": shot.label,
            "asset_batch": batch,
        }
        if refs:
            params["ref_asset_ids"] = [a.id for a in refs]
        if injected:
            params["injected_names"] = injected
        tasks.append(
            Task(
                kind="image",
                status="pending",
                service_id=resolved.service.id,
                model=model_key,
                prompt=prompt,
                params_json=json.dumps(params, ensure_ascii=False),
                canvas_project_id=project_id,
                canvas_node_id=str(node["id"]),
            )
        )
        if not dry_run:
            db.add(tasks[-1])

    if dry_run:
        return tasks
    await db.commit()
    for t in tasks:
        await db.refresh(t)
    logger.info(
        "画布 %s 分镜图：解析出 %s 镜，本次生成 %s 镜 → %s 个任务",
        project_id, total_found, len(shots), len(tasks),
    )
    return tasks


def _dedupe_shots(shots: list[storyboard_sheet.Shot], label: str) -> list[storyboard_sheet.Shot]:
    """同一镜号只保留首次出现。

    上游正文有可能是多份分镜表拼起来的（重跑过上游节点、或者用户手工把两份表粘在一起）。
    不去重就会按镜号重复出片 / 重复出图——**费用直接翻倍**，而且 9 个任务看起来和 3 个
    一样「正常」，只有账单上能看出来。所以宁可在这里挡一道，并把去重数量写进日志。
    """
    seen: set[str] = set()
    unique: list[storyboard_sheet.Shot] = []
    for shot in shots:
        if shot.no in seen:
            continue
        seen.add(shot.no)
        unique.append(shot)
    dropped = len(shots) - len(unique)
    if dropped:
        logger.warning(
            "节点「%s」的上游分镜表里有 %s 个重复镜号，已按首次出现去重（避免重复出片）",
            label,
            dropped,
        )
    return unique


async def _assets_by_shot(db: AsyncSession, assets: list[Asset]) -> dict[str, Asset]:
    """按镜号归拢上游图片。

    分镜图节点在建任务时把镜号写进了 `params_json`，这里顺着 `Asset.task_id`
    读回来，就知道每张图是哪一镜的。**靠镜号配对而不是靠顺序**：上游可能被
    「生成镜数」截断过，两边的顺序不一定对得齐，按顺序配会张冠李戴。
    同一镜有多张（多张采样）时保留最新的一张。
    """
    task_ids = {a.task_id for a in assets if a.task_id}
    if not task_ids:
        return {}
    rows = (await db.execute(select(Task).where(Task.id.in_(task_ids)))).scalars().all()
    shot_of: dict[int, str] = {}
    for t in rows:
        no = str(read_task_params(t).get("shot_no") or "").strip()
        if no:
            shot_of[t.id] = no

    out: dict[str, Asset] = {}
    # 按 id 升序覆盖，最终留下的是每镜最新的那张
    for a in sorted(assets, key=lambda x: x.id or 0):
        no = shot_of.get(a.task_id or 0)
        if no:
            out[no] = a
    return out


def _prev_shot_image(
    shots: list[storyboard_sheet.Shot], idx: int, by_shot: dict[str, Asset]
) -> Asset | None:
    """同一场景里、当前镜之前最近的一镜的分镜图。

    分镜表没写场景标题时，把整张表当成一个场景——「串联」这件事本身仍然有意义。
    往前找而不是只看前一镜：中间那镜可能没出图（被截断或失败），
    那就顺延到再前一镜，而不是直接放弃串联。
    """
    if idx <= 0:
        return None
    current_scene = shots[idx].scene_no
    if not current_scene:
        for j in range(idx - 1, -1, -1):
            img = by_shot.get(str(shots[j].no))
            if img is not None:
                return img
        return None
    for j in range(idx - 1, -1, -1):
        if shots[j].scene_no != current_scene:
            return None
        img = by_shot.get(str(shots[j].no))
        if img is not None:
            return img
    return None


async def _create_shot_video_tasks(
    db: AsyncSession,
    project_id: int,
    node: dict,
    upstream_text: str,
    images: list[Asset],
    dry_run: bool = False,
) -> list[Task]:
    """视频节点逐镜出片：每一镜用它自己的分镜图当首帧，可选与下一镜首尾相连。

    这是自动链「分镜图 → 视频」这一段。分镜图节点已经把图逐镜出好了，
    这里一镜一段地送进生视频，不必再手工一张张选。

    `data.shotVideo`：
    - `each`  每镜一段，该镜分镜图当首帧
    - `chain` 每镜一段，第 N 镜分镜图当首帧、**第 N+1 镜分镜图当尾帧**——相邻两镜
      的接缝处首尾同图，拼起来就是连贯的

    `dry_run=True` 时只把任务对象造出来、**不落库**：用来回答「这一跑会派多少段视频」。
    校验（镜号对不上、缺模型、模式不对）照旧照跑，所以预览也能提前把「这一跑必挂」说出来。
    """
    data = node.get("data") or {}
    label = NODE_SCHEMAS["video"]["label"]

    shots = storyboard_sheet.parse_storyboard(upstream_text)
    if not shots:
        raise ValueError(
            f"「{label}」没能从上游内容里解析出分镜表：请把上游「分镜」节点接进来，"
            "并确认它产出的是镜头表（`### 镜头N | 景别 | 运镜 | 时长s` + `- 画面：`）"
        )
    total_found = len(shots)
    shots = _dedupe_shots(
        storyboard_sheet.limit_shots(shots, int(data.get("shotLimit") or 0)), label
    )

    model_key = str(data.get("model_key") or "")
    if not model_key:
        raise ValueError(f"节点「{label}」未选择模型")
    resolved = await provider_store.resolve_model(db, model_key, "video")

    mode = str(data.get("mode") or "first_last")
    if mode == "text2video":
        raise ValueError(
            "逐镜出片要靠分镜图当首帧：请把模式切到「首尾帧」或「全能参考」，"
            "或者把「逐镜出视频」关掉"
        )
    chain = str(data.get("shotVideo") or "each") == "chain"
    scene_refs = data.get("sceneRefs") is not False

    by_shot = await _assets_by_shot(db, images)
    if not by_shot:
        raise ValueError(
            f"「{label}」没有拿到带镜号的分镜图：请把上游「分镜图」节点接进来；"
            "想用图库里手动选的图，就把「逐镜出视频」关掉、改用普通模式"
        )

    duration_default = int(data.get("duration") or 5)
    ratio = str(data.get("ratio") or "16:9")
    resolution = str(data.get("resolution") or "720p")
    extra = str(data.get("prompt") or "").strip()
    card = await style_service.load_card(db, str(data.get("styleKey") or ""))
    style_suffix = style_service.video_suffix(card)
    mention_on = data.get("mentionRefs") is not False
    manual = await _apply_manual_refs(db, data, [])
    batch = _new_batch_id(node)

    tasks: list[Task] = []
    missing: list[str] = []
    for idx, shot in enumerate(shots):
        first = by_shot.get(str(shot.no))
        if first is None:
            missing.append(shot.no)
            continue

        prompt = shot.video_prompt
        prompt = style_service.append_style(prompt, extra)
        prompt = style_service.append_style(prompt, style_suffix)

        refs: list[Asset] = list(manual)
        injected: list[str] = []
        if mention_on:
            refs, injected = await _inject_asset_refs(db, project_id, shot.scan_text, refs)

        params: dict = {
            "mode": mode,
            # 分镜表里逐镜写了时长就用它（4s 的镜头不该被拉成 5s），否则用节点上的设置
            "duration": shot.duration_seconds or duration_default,
            "ratio": ratio,
            "resolution": resolution,
            "shot_no": shot.no,
            "shot_label": shot.label,
            "asset_batch": batch,
        }
        if shot.scene_no:
            params["scene_no"] = shot.scene_no

        if mode == "first_last":
            params["first_frame_asset_id"] = first.id
            if chain and idx + 1 < len(shots):
                nxt = by_shot.get(str(shots[idx + 1].no))
                if nxt is not None:
                    params["last_frame_asset_id"] = nxt.id
        else:  # omni_ref：首帧塞进参考图，再按需串上同场景上一镜
            ref_ids = [first.id]
            if scene_refs:
                prev = _prev_shot_image(shots, idx, by_shot)
                if prev is not None:
                    ref_ids.append(prev.id)
                    injected = [*injected, prev.name or prev.original_name]
            for a in refs:
                if a.id not in ref_ids:
                    ref_ids.append(a.id)
            params["ref_asset_ids"] = ref_ids

        if injected:
            params["injected_names"] = injected

        task = Task(
            kind="video",
            status="pending",
            service_id=resolved.service.id,
            model=model_key,
            prompt=prompt,
            params_json=json.dumps(params, ensure_ascii=False),
            canvas_project_id=project_id,
            canvas_node_id=str(node["id"]),
        )
        if not dry_run:
            db.add(task)
        tasks.append(task)

    if not tasks:
        raise ValueError(
            "分镜图与镜头对不上：上游给了 "
            f"{len(by_shot)} 张带镜号的图（镜号 {'、'.join(sorted(by_shot))}），"
            f"而分镜表要的是镜号 {'、'.join(missing)}。"
            "常见原因是两边「生成镜数」填得不一样——请让「分镜图」节点覆盖到这些镜号"
        )

    if dry_run:
        return tasks

    await db.commit()
    for t in tasks:
        await db.refresh(t)

    if missing:
        # 放到日志里而不是直接报错：出一部分总比一镜都不出好，
        # 而且这条会在任务中心的任务日志里看得到（用户不必来问）
        logger.warning(
            "节点「%s」有 %s 个镜头没有对应分镜图，已跳过：镜号 %s（共解析出 %s 镜，本次出片 %s 段）",
            label,
            len(missing),
            "、".join(missing),
            total_found,
            len(tasks),
        )
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


def _referenced_asset_ids(params: dict) -> list[int]:
    """任务 params 里引用到的资产 id（参考图 / 首帧 / 尾帧 / 参考视频）。

    整图预览靠它统计「同一张素材被这一跑引用了多少次」。
    """
    ids: list[int] = []
    for key in ("ref_asset_ids", "video_ref_asset_ids"):
        for x in params.get(key) or []:
            if isinstance(x, int):
                ids.append(x)
    for key in ("first_frame_asset_id", "last_frame_asset_id"):
        x = params.get(key)
        if isinstance(x, int):
            ids.append(x)
    return ids


async def _persist_task(db: AsyncSession, task: Task, dry_run: bool) -> Task:
    """落库单个任务；`dry_run=True` 时**完全不碰 session**。

    预览（预估要派多少任务）和真跑共用同一段建任务代码，只有「落库」这一步分开。
    这样预览在物理上不可能写库——靠的不是调用方自觉，而是这里根本不执行写。
    """
    if dry_run:
        return task
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def _create_node_task(
    db: AsyncSession,
    project_id: int,
    node: dict,
    prompt: str,
    images: list[Asset],
    videos: list[Asset],
    upstream_text: str = "",
    injected_names: list[str] | None = None,
    dry_run: bool = False,
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
                {
                    "agent_key": ntype,
                    "extra": own_prompt,
                    "node_params": node_params,
                    # 风格卡：由 runner._run_text 追加到系统提示词（这里可以出现导演名）
                    "style_key": str(data.get("styleKey") or ""),
                },
                ensure_ascii=False,
            ),
            canvas_project_id=project_id,
            canvas_node_id=str(node["id"]),
        )
        return await _persist_task(db, task, dry_run)

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
        return await _persist_task(db, task, dry_run)

    modality = "video" if ntype == "video" else "image"
    model_key = data.get("model_key") or ""
    if not model_key:
        raise ValueError(f"节点「{NODE_SCHEMAS[ntype]['label']}」未选择模型")
    resolved = await provider_store.resolve_model(db, model_key, modality)

    # 风格注入：给模型的只有技法片段与约束词，导演名不出现在这里（合规）
    card = await style_service.load_card(db, str(data.get("styleKey") or ""))
    if card is not None:
        suffix = (
            style_service.video_suffix(card) if modality == "video"
            else style_service.image_suffix(card)
        )
        prompt = style_service.append_style(prompt, suffix)

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
    return await _persist_task(db, task, dry_run)


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


def _is_shot_video(node: dict) -> bool:
    """视频节点是否开了「逐镜出片」。

    开了之后提示词与首帧都来自上游（分镜表逐镜给画面、分镜图逐镜给首帧），
    节点自己写不写提示词都成立 —— 这个口径必须只有一处定义，
    否则「能不能跑」和「走哪条分支」会各判各的。
    """
    return (
        node["type"] == "video"
        and str((node.get("data") or {}).get("shotVideo") or "off") != "off"
    )


def _has_input(node: dict, prompt: str, upstream_text: str) -> bool:
    """节点是否有可执行输入。

    - 工作流节点：提示词可空（参数与工作流本身足够）
    - 批量图片节点（资产设定图 / 分镜图）：输入完全来自上游文档，节点自己的输入框可选
    - 逐镜出片的视频节点：提示词由上游分镜表逐镜给出，节点自己的输入框可选
    - 文档节点：上游正文或本节点的「补充要求」有其一即可
    - 其余节点：必须有提示词
    """
    ntype = node["type"]
    if ntype == "workflow":
        return True
    if is_batch_image(ntype):
        return bool(upstream_text.strip())
    if _is_shot_video(node):
        # 每一镜的画面与首帧都由上游逐镜给出，节点自己不写提示词也成立
        return True
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
    dry_run: bool = False,
) -> list[Task]:
    """把一个画布节点翻译成待执行的任务列表。

    普通节点 = 1 个任务；资产设定图节点 = 每行资产 1 个任务。

    `dry_run=True` 只算数不落库：整图确认弹窗（见 `preview_graph`）走的也是这一段，
    保证「弹窗上写的数」和「真跑派出的数」来自同一份计算。
    """
    prompt, images, videos, upstream_text = await _node_inputs(db, doc, node, upstream)
    if not _has_input(node, prompt, upstream_text):
        raise ValueError("节点没有可用的输入内容")

    data = node.get("data") or {}
    ntype = node["type"]

    if is_asset_image(ntype):
        return await _create_asset_image_tasks(
            db,
            project_id,
            node,
            upstream_text,
            await _apply_manual_refs(db, data, images),
            dry_run=dry_run,
        )

    if is_storyboard_image(ntype):
        # 上游图片一律不带进来：每一镜要的是"这一镜提到的那几个角色"，
        # 把整批设定图全塞进去只会稀释主体（改由逐镜的提及注入来挑）。
        manual = await _apply_manual_refs(db, data, [])
        card = await style_service.load_card(db, str(data.get("styleKey") or ""))
        return await _create_storyboard_image_tasks(
            db, project_id, node, upstream_text, manual, card, dry_run=dry_run
        )

    # 视频节点逐镜出片（D 期）：分镜图节点已经逐镜出好图，这里一镜一段送进生视频。
    # 放在普通分支之前，因为它同样要一次建出多个任务。
    if _is_shot_video(node):
        return await _create_shot_video_tasks(
            db, project_id, node, upstream_text, images, dry_run=dry_run
        )

    injected: list[str] = []
    # 角色提及注入：提示词里提到资产名就自动挂设定图（可在节点上关掉）
    if ntype in _IMAGE_CONSUMERS and data.get("mentionRefs") is not False:
        images, injected = await _inject_asset_refs(db, project_id, prompt, images)

    return [
        await _create_node_task(
            db,
            project_id,
            node,
            prompt,
            images,
            videos,
            upstream_text,
            injected,
            dry_run=dry_run,
        )
    ]


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
        # 队列满时由 runner 把这条任务标失败并写明原因（提示口径统一在 runner 里）
        await runner.start_or_fail(task.kind, task.id)
    return tasks


# 一张素材被这一跑引用这么多次以上，才值得在确认弹窗里点名（更低的次数属正常搭配）
_REUSE_MIN_HITS = 2


async def preview_graph(project_id: int) -> dict:
    """整图执行前的预估：这一跑会派多少任务、花多少次调用、谁已经有产物。

    **纯读**：不建任务、不写库、不调模型（模型解析只查本机配置），可以随便点。
    任务数用的是和真跑同一段代码（`_build_node_tasks(dry_run=True)`），
    所以弹窗上的数字不可能和实际派发的对不上；算不出来的节点如实标成待定，不猜。
    """
    async with SessionLocal() as db:
        project = await db.get(Project, project_id)
        if project is None or not project.canvas_json:
            raise ValueError("画布不存在")
        doc = json.loads(project.canvas_json)
        nodes = {n["id"]: n for n in doc.get("nodes", [])}
        order = _topo_order(doc.get("nodes", []), doc.get("edges", []))

        items: list[dict] = []
        hits: dict[int, int] = {}  # 资产 id → 本次被引用次数
        by_kind: dict[str, int] = {}
        total_tasks = 0
        step_nodes = 0  # 本次会执行的节点数
        pending: list[str] = []  # 条数待定的节点
        rerun: list[str] = []  # 已经有产物、这一跑会重做的节点
        blocked: list[str] = []  # 已经能判定必挂的节点
        billable = False  # 是否会真实调用外部模型（workflow 走本机 ComfyUI，不扣额度）

        for nid in order:
            node = nodes[nid]
            if not is_runnable(node["type"]):
                continue
            step_nodes += 1
            if node["type"] != "workflow":
                # 非 workflow 节点一律要真调模型（workflow 走本机 ComfyUI，不扣额度）
                billable = True
            label = NODE_SCHEMAS[node["type"]]["label"]
            upstream = await _resolve_upstream(db, project_id, doc, nid)
            # 上游里本次才会产出东西的节点：它们跑完之前，本节点会派几条无从得知
            waiting = [
                NODE_SCHEMAS[s["type"]]["label"]
                for _sid, s, assets in upstream
                if is_runnable(s["type"]) and not assets and not _doc_override_text(s)
            ]
            existing = await _latest_task_assets(db, project_id, nid)

            item: dict = {
                "id": nid,
                "type": node["type"],
                "label": label,
                "count": None,
                "kinds": {},
                "hasOutput": bool(existing),
                "outputCount": len(existing),
                "waiting": waiting,
                "error": "",
            }

            try:
                tasks = await _build_node_tasks(
                    db, project_id, doc, node, upstream, dry_run=True
                )
            except Exception as e:  # noqa: BLE001 - 预览要把「这跑必挂」摆出来，而不是自己中断
                if waiting:
                    item["waiting"] = waiting
                else:
                    item["error"] = str(e)
                    blocked.append(label)
            else:
                item["count"] = len(tasks)
                total_tasks += len(tasks)
                by: dict[str, int] = {}
                for t in tasks:
                    by[t.kind] = by.get(t.kind, 0) + 1
                    by_kind[t.kind] = by_kind.get(t.kind, 0) + 1
                    for aid in _referenced_asset_ids(read_task_params(t)):
                        hits[aid] = hits.get(aid, 0) + 1
                item["kinds"] = by

            if item["count"] is None and not item["error"]:
                pending.append(label)
            if existing:
                rerun.append(label)
            items.append(item)

        reused: list[dict] = []
        if hits:
            arow = await db.execute(select(Asset).where(Asset.id.in_(list(hits))))
            names = {a.id: (a.name or a.original_name) for a in arow.scalars().all()}
            reused = [
                {"assetId": aid, "name": names.get(aid, f"#{aid}"), "count": n}
                for aid, n in sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))
                if n >= _REUSE_MIN_HITS
            ]

    notes: list[dict] = []
    if blocked:
        notes.append(
            {
                "level": "blocker",
                "text": "这一跑会直接失败："
                + "、".join(f"「{x}」" for x in blocked)
                + "。请先按节点上的提示补齐配置，否则整图会在这里断掉。",
            }
        )
    if billable and (total_tasks or pending or blocked):
        seg: list[str] = []
        if total_tasks:
            seg.append(f"至少 {total_tasks} 次模型调用")
        if pending:
            seg.append(f"{len(pending)} 个节点的次数要等上游跑完才知道")
        notes.append(
            {
                "level": "warning",
                "text": f"这一跑会执行 {step_nodes} 个节点"
                + (f"，{'、'.join(seg)}" if seg else "")
                + "。每次调用都会真实消耗对应服务的额度（本机服务则占本机算力），确认后再跑。",
            }
        )
    if rerun:
        notes.append(
            {
                "level": "warning",
                "text": "、".join(f"「{x}」" for x in rerun)
                + " 已经有产物了，整图会把它们重做一遍（再花一次钱）。"
                "只想补跑某一步的话，用那个节点上的单独运行。",
            }
        )
    if reused:
        top = "、".join(f"「{r['name']}」{r['count']} 次" for r in reused[:5])
        notes.append({"level": "info", "text": f"本次会复用已有素材：{top}。"})

    return {
        "nodes": items,
        "totals": {
            "tasks": total_tasks,
            "steps": step_nodes,
            "byKind": by_kind,
            "pendingNodes": len(pending),
            "rerunNodes": len(rerun),
            "blockedNodes": len(blocked),
        },
        "reused": reused,
        "notes": notes,
        "billable": billable,
    }


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
            await runner.start_or_fail(task.kind, task.id)

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
