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
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import Asset, ComfyWorkflow, Project, ProviderService, Task
from app.providers.base import AdapterError
from app.registry.canvas_nodes import (
    NODE_SCHEMAS,
    is_asset_image,
    is_batch_image,
    is_doc_kind,
    is_runnable,
    is_storyboard_image,
    is_storyboard_sheet,
    normalize_type,
)
from app.services import (
    animatic,
    asset_sheet,
    digital_human,
    ffmpeg_service,
    image_size,
    provider_store,
    run_budget,
    speech,
    speech_service,
    storage,
    storyboard_lint,
    storyboard_sheet,
    style_service,
)
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
        # 「这一张属于哪一组候选」：前端靠它把同一镜 / 同一资产的几张排成一组来挑定稿。
        # 分组规则只在后端有一份（candidate_key），前端不重写一遍——
        # 两处各判一次的话，会出现「界面上分在一组的，后端其实不是一组」。
        "candidateKey": candidate_key(data),
    }


def version_key_of(task: Task) -> str:
    """这个任务属于哪一版。

    「一版」= 一次运行派出的那批任务，它们带着同一个 `asset_batch`。所以版本的身份
    就直接用批次标记，不必再建一张表——**已有的批次标记本来就是版本**，
    只是以前没人把它当回事说出来。

    老数据里没有这个标记的任务（`asset_batch` 是 v1.1.22 才补到所有节点类型上的），
    每个任务自成一版：那正是它们本来的样子（一个任务 = 一次生成），
    回填反而会凭空编造出「哪些任务属于同一次运行」这个我们并不知道的事实。
    """
    batch = read_task_params(task).get("asset_batch")
    return str(batch) if batch else f"t{task.id}"


def group_versions(tasks: list[Task]) -> list[dict]:
    """把某个节点的历史任务按版本分组，**最新的在前**。

    入参要求按 `Task.id` 倒序（调用方查库时就是这个顺序）：这样「第一次出现的批次」
    就是最新那一版，字典的插入顺序天然给出「新 → 旧」，不用再排一次序。

    `index` 从 1 开始、**从旧往新数**（第 1 版是最早那次生成）：用户嘴里说的
    「第 2 版」就是这个号，倒过来数会让人对不上。
    """
    groups: dict[str, dict] = {}
    for t in tasks:
        key = version_key_of(t)
        g = groups.get(key)
        if g is None:
            g = {"key": key, "tasks": []}
            groups[key] = g
        g["tasks"].append(t)

    ordered = list(groups.values())
    total = len(ordered)
    for i, g in enumerate(ordered):
        g["index"] = total - i
        g["total"] = total
        g["latest"] = i == 0
        g["taskCount"] = len(g["tasks"])
    return ordered


def pick_version(groups: list[dict], version_key: object) -> dict | None:
    """挑出要交付的那一版；`version_key` 为空（没回滚过）或指不到任何一版时取最新。

    **指不到时退回最新而不是报错**：那多半是「分享码导入到别人机器上」或「历史任务被
    清理了」，为这个让整条链失败没有任何好处。但节点上会标出「当前交付的是第 N 版」，
    用户一眼能看出自己拿的是哪一版，不会以为拿到的是最新那版。
    """
    if not groups:
        return None
    want = str(version_key or "").strip()
    if want:
        for g in groups:
            if g["key"] == want:
                return g
    return groups[0]


def version_pin_of(node: dict) -> str:
    """节点上「回滚到的那一版」（`data.versionKey`）；没回滚过就是空串 = 跟最新。

    回滚是**显式**的：只有用户点了「设为当前」才会写这个字段。生成新产物**不会**动它，
    所以「回滚到第 2 版，然后又生成了第 4 版」之后，节点交付的仍然是第 2 版——
    这不是 bug，是「用户刚说过要用哪一版」这件事不该被一次生成悄悄推翻。
    节点上会把「当前交付的是第 N 版（不是最新）」标出来，并给一键切回。
    """
    return str((node.get("data") or {}).get("versionKey") or "").strip()


def candidate_key(params: dict) -> str:
    """把「一组候选」算出来：同一组的几张是同一个位置的备选，最多挑一张定稿。

    分组依据是「这组图是什么」里**最稳定的那个身份**，没有就整节点一组：

    - 分镜图节点：按**镜号**（一镜出 4 张 = 这一镜的四个备选）
    - 资产设定图节点：按**资产名**（一个角色出 4 张）
    - 普通图片节点：整节点一组（同一个提示词出的 4 张就是四个候选）

    **不用任务 id 分组**：任务 id 每生成一次就变，拿它当键会让「在上一版里定稿的那张」
    在生成新版之后变成指向不存在的键——而用户想表达的是「这个镜位用这张」，
    与它是第几次生成的无关。
    """
    shot = str(params.get("shot_no") or "").strip()
    if shot:
        return f"shot:{shot}"
    name = str(params.get("asset_name") or "").strip()
    if name:
        return f"asset:{name}"
    return ""


def picks_of(node: dict) -> dict[str, int]:
    """节点上「每组定稿了哪一张」（`data.picks`）；没定稿就是空字典 = 组内全部交付。

    定稿与回滚同一个口径：**只有用户显式点过才写，生成新产物不会动它**。
    落在节点 data 上而不是新开一列——「交付哪几张」和「交付哪一版」是同一个层级的决定，
    都该跟着图走（分享码导出导入时也能一起带走）。

    读的时候把认不出来的值丢掉（手改 JSON / 老数据都可能有脏值），
    一个脏值不该让整张图打不开。
    """
    raw = (node.get("data") or {}).get("picks")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        try:
            aid = int(value)
        except (TypeError, ValueError):
            continue
        if aid > 0:
            out[str(key)] = aid
    return out


def apply_picks(
    assets: list[Asset], keys: dict[int, str], picks: dict[str, int]
) -> list[Asset]:
    """按定稿过滤：**定了稿的那一组只留定稿那张**，没定稿的组原样交付。

    `keys` 是「资产 id → 候选组键」（由它所属任务的参数算出来，见 `candidate_key`）。

    定稿指不到任何一张时（那一版里没有它，比如回滚到了定稿之前的那一版），
    这一组**保持整组交付**——与「回滚指不到版本时退回最新」是同一个口径：
    不因为一次选择失效就让下游什么都读不到。节点面板会如实说明这件事。
    """
    if not picks:
        return assets
    groups: dict[str, list[Asset]] = {}
    for a in assets:
        groups.setdefault(keys.get(a.id, ""), []).append(a)

    keep: set[int] = set()
    for key, group in groups.items():
        picked = picks.get(key)
        if picked is not None and any(a.id == picked for a in group):
            keep.add(picked)
        else:
            keep.update(a.id for a in group)
    return [a for a in assets if a.id in keep]


async def _latest_task_assets(
    db: AsyncSession,
    project_id: int,
    node_id: int | str,
    version_key: object = None,
    picks: dict[str, int] | None = None,
) -> list[Asset]:
    """取某画布节点要交付给下游的那几张产物（按生成顺序）。

    这是「这个节点交付什么」的**唯一出口**，两道显式选择都在这里生效：

    - **版本**（`data.versionKey`）：默认给最新一版；回滚过就给回滚到的那一版。
    - **定稿**（`data.picks`）：一组候选里只留定稿那张；没定稿的组整组交付。

    「一处判断」是刻意的：下游取值、预览计数、分镜体检、静图样片都从这里取，
    所以只要这里认了，整条链读到的就是同一批产物。分几处各判一次的话，
    迟早出现「画布上显示的是这一张、下游用的是另一张」——而这正是这一项要修的问题
    （以前逐镜出视频遇到「一镜多张」时只能拿到最后生成的那张，用户挑不了）。

    一个版本里可能有多个任务（资产链 / 分镜图 / 逐镜视频一次运行派 N 个任务），
    它们带着同一个批次标记，按标记收齐——否则节点上只会显示最后一个任务的图。
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

    group = pick_version(group_versions(tasks), version_key)
    tasks = group["tasks"] if group else tasks[:1]

    arow = await db.execute(
        select(Asset)
        .where(Asset.task_id.in_([t.id for t in tasks]))
        .order_by(Asset.id.asc())
    )
    assets = list(arow.scalars().all())
    if not picks:
        return assets

    # 资产属于哪一组候选：看它所属任务的参数（镜号 / 资产名）
    keys = {t.id: candidate_key(read_task_params(t)) for t in tasks}
    return apply_picks(assets, {a.id: keys.get(a.task_id or 0, "") for a in assets}, picks)


# 版本列表里每版带几张产物缩略图：用户是靠「看一眼」认出版本的，标题里的时间帮不上忙
_VERSION_PREVIEW = 4


async def node_versions(
    db: AsyncSession, project_id: int, node_id: int | str, version_key: object = None
) -> dict:
    """某个节点的历史版本，**最新的在前**（含每版的产物缩略图与成败条数）。

    三处刻意的取舍：

    1. **不只列成功的那几条**。某一版可能一条都没成（全失败），而用户最想知道的恰恰是
       「这一版为什么不能用」——所以按任务全量分组，把失败条数一并给出。
    2. **与 `_latest_task_assets` 共用 `group_versions`**。两处各分一次组，迟早出现
       「列表说有 3 版、实际交付的是第 4 版」这种最难看的不一致。
    3. **`pin` 与 `activeKey` 分开回**。`pin` 是节点上写着的那个 key（可能因为历史任务
       被清理而指不到任何一版），`activeKey` 是**实际生效**的那一版。分开回，前端才能
       如实说「你选的那一版已经不在了，现在给的是最新这版」，而不是装作没事。
    """
    row = await db.execute(
        select(Task)
        .where(Task.canvas_project_id == project_id, Task.canvas_node_id == str(node_id))
        .order_by(Task.id.desc())
        .limit(_MAX_TASKS_PER_NODE)
    )
    tasks = list(row.scalars().all())
    groups = group_versions(tasks)

    by_task: dict[int, list[Asset]] = {}
    if tasks:
        arow = await db.execute(
            select(Asset)
            .where(Asset.task_id.in_([t.id for t in tasks]))
            .order_by(Asset.id.asc())
        )
        for a in arow.scalars().all():
            if a.task_id is not None:
                by_task.setdefault(a.task_id, []).append(a)

    items: list[dict] = []
    for g in groups:
        views: list[dict] = []
        for t in sorted(g["tasks"], key=lambda x: x.id):
            tparams = read_task_params(t)
            for a in by_task.get(t.id, []):
                views.append(asset_view(a, tparams))
        created = [t.created_at for t in g["tasks"] if t.created_at is not None]
        items.append(
            {
                "key": g["key"],
                "index": g["index"],
                "total": g["total"],
                "latest": g["latest"],
                "taskCount": g["taskCount"],
                "doneCount": sum(1 for t in g["tasks"] if t.status == "completed"),
                "failedCount": sum(1 for t in g["tasks"] if t.status == "failed"),
                "runningCount": sum(
                    1 for t in g["tasks"] if t.status in ("pending", "processing")
                ),
                "createdAt": min(created).isoformat() if created else "",
                # 只带前几张当缩略图，但要如实说一共几张（否则「这版只有 4 张」是假的）
                "assetCount": len(views),
                "assets": views[:_VERSION_PREVIEW],
            }
        )

    chosen = pick_version(groups, version_key)
    return {
        "nodeId": str(node_id),
        "pin": str(version_key or "").strip(),
        "activeKey": chosen["key"] if chosen else "",
        "latestKey": groups[0]["key"] if groups else "",
        "items": items,
    }


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


async def _audio_ref_asset(db: AsyncSession, data: dict) -> Asset | None:
    """节点上挂的对白音轨（`data.audioRefAssetId`），没有就返回 None。

    对白是**可选**的一件附加物：没挂就照旧出无声片子，挂错了要当场说清楚——
    所以这里对「id 指向的不是音频」直接报错，不静默当成没选。
    """
    raw = data.get("audioRefAssetId")
    if not raw:
        return None
    try:
        aid = int(raw)
    except (TypeError, ValueError):
        raise ValueError("节点上的「对白音轨」值不对（不是资产编号），请重新选一条") from None
    asset = await db.get(Asset, aid)
    if asset is None:
        raise ValueError("节点上选的「对白音轨」已经被删掉了，请重新选一条配音")
    if asset.kind != "audio":
        raise ValueError(
            f"「对白音轨」要选一条**音频**资产，而 #{aid} 是{asset.kind}——"
            "到「配音」页生成一段，或到资产库筛「音频」挑一条"
        )
    return asset


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

    同一镜有多张（多张采样）时，**用户在节点上定稿的那张说了算**——定稿在
    `_latest_task_assets` 里就已经生效了，所以传进来的通常每镜只剩一张。
    没定稿时退回「保留最新生成的那张」：这是老行为，可预期但确实不是用户挑的，
    所以节点面板会提示「这一镜还有别的候选，可以定稿一张」。
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


async def _shot_dialogue_audio(
    db: AsyncSession,
    *,
    plan: list[dict],
    model_key: str,
    shot_no: str,
) -> tuple[Asset, float | None]:
    """把这一镜的对白做成**一条**音频资产，回 `(资产, 秒数)`。

    三种情形走同一条出口，只是内部做法不同：

    - **整镜一个人**（最常见）：一次合成整镜的台词——与以前完全一样，不多花一次钱。
    - **一镜里有好几个人**：按句各合成一条，**再拼成这一镜的一条**。provider 那边只认
      一条参考音频，所以必须在本地拼好；拼接时段间补一小段静音（见 `join_audio`），
      否则两种音色会咬在一起，听着像一个人抢话。
    - 中间那几句**只落临时目录、不登记资产**：它们拼完就没用了，登进资产库只会多出
      一堆「配音 · 你」这种垃圾条目，而用户要的是这一镜的成品那一条。

    秒数返回的是**拼完之后**的时长：它比各段之和多出段间静音，而「这一镜读不读得完」
    用的就是它。
    """
    speakers = digital_human.plan_summary(plan)
    name = f"对白 · 镜头{shot_no}"
    if len(plan) == 1:
        item = plan[0]
        asset, meta = await speech_service.synthesize_to_asset(
            db,
            model_key=model_key,
            text=str(item["text"]),
            voice=str(item["voice"]),
            source="shot",
            name=name,
            note=f"逐镜对白（{speakers}）",
        )
        return asset, meta["seconds"]

    work = ffmpeg_service.new_workdir("shotvoice")
    try:
        parts: list[Path] = []
        for i, item in enumerate(plan):
            path, _meta = await speech_service.synthesize_to_temp(
                db,
                model_key=model_key,
                text=str(item["text"]),
                voice=str(item["voice"]),
                work=work,
                index=i,
            )
            parts.append(path)
        joined = await ffmpeg_service.join_audio(parts)
        asset, meta = await speech_service.register_audio_asset(
            db,
            path=joined,
            text="\n".join(str(i["text"]) for i in plan),
            name=name,
            note=f"逐镜对白（{speakers}）",
            source="shot",
        )
        return asset, meta["seconds"]
    finally:
        # 临时目录必须收走：里面是几句中间音频，一次一积（这条链上踩过同类漏）
        ffmpeg_service.drop_workdir(work)


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

    # 逐镜对白（数字人）：每一镜的「台词」先合成成配音，再作为参考音附给这一镜的视频。
    # 顺序不能反——「这一镜的台词读不读得完」只有拿到配音时长才知道，而配音便宜、视频贵。
    dialogue_on = bool(data.get("shotDialogue"))
    voice_map: dict[str, str] = {}
    default_voice = ""
    tts_key = ""
    if dialogue_on:
        voice_map, bad_lines = digital_human.parse_voice_map(data.get("shotVoices"))
        if bad_lines:
            raise ValueError(
                "「逐镜对白」的角色音色表里有认不出的行："
                + "、".join(f"「{x}」" for x in bad_lines)
                + "。每行写成「角色=音色」（用冒号也行），例如「小焰=nova」；"
                "角色名取自台词前的标签（「小焰：…」）"
            )
        default_voice = speech.sanitize_voice(data.get("shotVoice"))

    # 先把「哪几镜要说话、每一镜要几句」挑出来：一句台词都没有时**立刻报错**，一个字的钱都别花
    plans: dict[str, list[dict]] = {}
    if dialogue_on:
        for shot in shots:
            plan = digital_human.voice_plan(
                speech.dialogue_segments(getattr(shot, "dialogue", "")),
                voice_map,
                default_voice,
            )
            if plan:
                plans[str(shot.no)] = plan
        if not plans:
            raise ValueError(
                "「逐镜对白」开着，但这份分镜表里一句台词都没有。"
                "可以先去「分镜」节点给每镜补一行「台词：…」，或者把这个开关关掉"
            )
        try:
            tts_key = await speech_service.default_model_key(db)
        except AdapterError as e:
            # 配置不对要在开跑之前说（预览也会走到这里，于是确认弹窗里就能看到）
            raise ValueError(f"「逐镜对白」要先把语音模型配好：{e}") from e

    tasks: list[Task] = []
    missing: list[str] = []
    dialogue_skipped: list[str] = []
    voiced = 0
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

        # 逐镜对白：这一镜有台词就配出来挂上；配不了就**不派这一镜的视频**
        if dialogue_on and str(shot.no) in plans:
            plan = plans[str(shot.no)]
            if dry_run:
                # 预览绝不花钱：只标出「这一镜要合成几次」。**一镜多人时是每句一次**，
                # 所以这里记的是**次数**而不是一个 True——记 True 的话账会少算。
                params["shot_dialogue"] = digital_human.utterance_count(plan)
            else:
                try:
                    audio, seconds = await _shot_dialogue_audio(
                        db, plan=plan, model_key=tts_key, shot_no=str(shot.no)
                    )
                except AdapterError as e:
                    dialogue_skipped.append(f"镜头{shot.no} 的配音没做成：{e}")
                    continue
                problem = digital_human.check_duration(
                    requested=int(params.get("duration") or 0),
                    audio_seconds=seconds,
                    name=audio.name or "",
                    duration_hint="分镜表里这一镜的时长",
                )
                if problem:
                    # 时长来自分镜表，我们**不替用户改**（改时长就是改钱）；
                    # 这一镜不派任务，并在日志里逐条说明为什么
                    dialogue_skipped.append(
                        digital_human.skip_reason(
                            shot_no=str(shot.no),
                            audio_seconds=seconds,
                            shot_seconds=int(params.get("duration") or 0),
                        )
                    )
                    continue
                params["audio_ref_asset_id"] = audio.id
                params["dialogue_note"] = digital_human.attach_note(
                    name=audio.name or "", seconds=seconds
                )
                voiced += 1

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
        tasks.append(task)

    if not tasks:
        # 「一条都没派」有两种完全不同的原因，必须分开说。混在一起报
        # 「分镜图与镜头对不上」的话，用户会去反复核对镜号（那儿其实没错），
        # 而真正要改的是台词长度或这一镜的时长——**报错的指向错，比不报错更费时间**。
        if missing:
            also = ("另外，" + "；".join(dialogue_skipped) + "。") if dialogue_skipped else ""
            raise ValueError(
                "分镜图与镜头对不上：上游给了 "
                f"{len(by_shot)} 张带镜号的图（镜号 {'、'.join(sorted(by_shot))}），"
                f"而分镜表要的是镜号 {'、'.join(missing)}。"
                "常见原因是两边「生成镜数」填得不一样——请让「分镜图」节点覆盖到这些镜号"
                + also
            )
        raise ValueError(
            "「逐镜对白」一镜都没配上，这一跑没有派任何视频任务："
            + "；".join(dialogue_skipped)
            + "。台词已经试配过了（配音资产在资产库里），改完分镜表再跑一次即可"
        )

    if dry_run:
        return tasks

    if dialogue_on and voiced == 0:
        # 一镜都没配上：派出去就是拿视频钱换一串没声音的片子。宁可一条都不派。
        raise ValueError(
            "「逐镜对白」一镜都没配上，这一跑没有派任何视频任务："
            + "；".join(dialogue_skipped)
            + "。台词已经试配过了（配音资产在资产库里），改完分镜表再跑一次即可"
        )

    for t in tasks:
        db.add(t)
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
    if dialogue_on and not dry_run:
        # 没配上的那几镜逐条列出来（用户看到成片少了一段，最想知道的就是为什么）
        logger.warning("节点「%s」%s", label, digital_human.dialogue_summary(
            voiced=voiced, skipped=dialogue_skipped
        ))
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
    """任务 params 里引用到的资产 id（参考图 / 首帧 / 尾帧 / 参考视频 / 对白音轨）。

    整图预览靠它统计「同一张素材被这一跑引用了多少次」。
    """
    ids: list[int] = []
    for key in ("ref_asset_ids", "video_ref_asset_ids"):
        for x in params.get(key) or []:
            if isinstance(x, int):
                ids.append(x)
    for key in ("first_frame_asset_id", "last_frame_asset_id", "audio_ref_asset_id"):
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
    # 这一跑派出的任务都盖同一个批次标记 —— 它就是「一版」的身份。
    # 单任务节点（文本 / 图片 / 视频 / 工作流）也照盖：不盖的话「重新生成」出来的
    # 那一条会自成一版，而它其实只是把同一版里失败的那条补上。
    batch = _new_batch_id(node)

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
                    "asset_batch": batch,
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
            "asset_batch": batch,
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

    params = {"asset_batch": batch}
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
            # 参考位约定：第 1 张=首帧，第 2 张=尾帧。
            # 上游「一镜多张候选」时上游已按定稿过滤过，所以这里拿到的就是用户挑的那张；
            # 没定稿时会按生成顺序取前两张（老行为），节点面板会提示可以定稿。
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

        # 对白 / 口播（数字人那条路）：挂一条配音当参考音，出来的片子自带声音。
        # 时长不够读完整句时**在这里拦掉**：改时长就是改钱，必须由用户自己点那一下，
        # 我们不替他悄悄把 5 秒改成 10 秒（见 services/digital_human.py 的第 1 条口径）。
        audio_asset = await _audio_ref_asset(db, data)
        if audio_asset is not None:
            audio_name = audio_asset.name or audio_asset.original_name
            problem = digital_human.check_duration(
                requested=int(params.get("duration") or 0),
                audio_seconds=audio_asset.duration,
                name=audio_name,
            )
            if problem:
                raise ValueError(problem)
            params["audio_ref_asset_id"] = audio_asset.id
            # 任务说明里留下「这一段带了哪条配音、多长」——事后才查得清时长为什么是这么多
            params["dialogue_note"] = digital_human.attach_note(
                name=audio_name, seconds=audio_asset.duration
            )

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
    db: AsyncSession, project_id: int, doc: dict, node_id: str, use_pins: bool = True
) -> list[tuple[str, dict, list[Asset]]]:
    """取上游节点要交给本节点的产物。

    `use_pins` 决定**上游节点上回滚过的版本算不算数**，两个入口的口径不同：

    - **单节点运行**（`use_pins=True`，也是默认）：算数。这正是回滚的用处——
      「这一版的分镜图更好，拿它去出视频」。
    - **整图运行**（`use_pins=False`）：不算数，一律按最新那版。因为整图会把每个可执行
      节点都重跑一遍、它们各自都会产出新的一版；这时候还按旧版给下游，用户会看到
      「我刚跑的图没被用上」——那是比「回滚没生效」更难解释的事。
    """
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
        pin = version_pin_of(src) if use_pins else ""
        picks = picks_of(src) if use_pins else {}
        assets = await _latest_task_assets(db, project_id, src["id"], pin, picks)
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


async def run_single_node(project_id: int, node_id: str, run_id: str = "") -> list[Task]:
    """运行单个节点：上游取最近成功产物，缺产物直接报错。

    返回本次派发出的全部任务（资产设定图节点会有多个）。
    `run_id` 由调用方给（API 层在受理请求时生成），用于事后把「这一跑」的任务串起来。
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
        await _stamp_run(db, tasks, run_id)

    for task in tasks:
        # 队列满时由 runner 把这条任务标失败并写明原因（提示口径统一在 runner 里）
        await runner.start_or_fail(task.kind, task.id)
    return tasks


def new_run_id() -> str:
    """生成一次运行的标识。

    不用自增序号：任务表里没有「运行」这张表，标识要能由任意一处生成而不需要先查库；
    uuid 足够短、不撞，也能直接放进 URL query。
    """
    return uuid.uuid4().hex[:12]


# 在跑的整图：run_id → {projectId, expected, done}。
#
# 为什么非要有这块内存状态：整图是**边跑边派**任务的（第 2 个节点要等第 1 个跑完
# 才知道派几条），所以「已有任务都结束了」并不等于「这一跑结束了」——只看任务表
# 会在第一个节点跑完时误判成跑完，把「实际 1 次（预估 2 次）」这种半截数字当结论弹给用户。
# 用内存而不是建表：它只回答「这一跑还在不在进行」，进程重启后本来也就没有这一跑了。
_ACTIVE_RUNS: dict[str, dict] = {}
_MAX_ACTIVE_RUNS = 50


def _register_run(run_id: str, project_id: int, expected: int) -> None:
    _ACTIVE_RUNS[run_id] = {
        "projectId": project_id,
        "expected": max(0, int(expected or 0)),
        "done": False,
    }
    # 只留最近这些：run_id 是查账用的，跑过几十次之后没人会再去翻最老的那一跑
    while len(_ACTIVE_RUNS) > _MAX_ACTIVE_RUNS:
        _ACTIVE_RUNS.pop(next(iter(_ACTIVE_RUNS)))


def _finish_run(run_id: str) -> None:
    if run_id in _ACTIVE_RUNS:
        _ACTIVE_RUNS[run_id]["done"] = True


def run_progress(run_id: str) -> dict:
    """这一跑还在进行吗、受理时估了多少次（不是这一跑就当作早已结束）。"""
    state = _ACTIVE_RUNS.get(run_id)
    if state is None:
        return {"active": False, "expected": 0}
    return {"active": not state["done"], "expected": state["expected"]}


async def _stamp_run(db: AsyncSession, tasks: list[Task], run_id: str) -> None:
    """把这一批任务标上「属于哪一跑」。

    放在建完任务之后统一盖戳（而不是在建任务的每一处都传 run_id）：
    建任务的代码路径有六条（文档/图片/视频/资产图/分镜图/逐镜出片/工作流），
    逐条改容易漏；这里只有一个入口，漏不掉。
    """
    if not run_id or not tasks:
        return
    for t in tasks:
        t.run_id = run_id
    await db.commit()


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
        pinned: list[str] = []  # 回滚到旧版本的节点（整图会重跑它们，按最新算）
        billable = False  # 是否会真实调用外部模型（workflow 走本机 ComfyUI，不扣额度）
        dialogue_calls = 0  # 逐镜对白会额外做几次语音合成（它们不出现在任务数里）

        for nid in order:
            node = nodes[nid]
            if not is_runnable(node["type"]):
                continue
            step_nodes += 1
            if node["type"] != "workflow":
                # 非 workflow 节点一律要真调模型（workflow 走本机 ComfyUI，不扣额度）
                billable = True
            label = NODE_SCHEMAS[node["type"]]["label"]
            if version_pin_of(node):
                pinned.append(label)
            # 预览描述的是「整图跑一遍」会发生什么，所以这里按整图的口径取上游（忽略回滚）
            upstream = await _resolve_upstream(db, project_id, doc, nid, use_pins=False)
            # 上游里本次才会产出东西的节点：它们跑完之前，本节点会派几条无从得知
            waiting = [
                NODE_SCHEMAS[s["type"]]["label"]
                for _sid, s, assets in upstream
                if is_runnable(s["type"]) and not assets and not _doc_override_text(s)
            ]
            existing = await _latest_task_assets(
                db, project_id, nid, version_pin_of(node), picks_of(node)
            )

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
                    tp = read_task_params(t)
                    # 逐镜对白会**额外**做语音合成：它不是任务，所以不在「调用次数」里，
                    # 但它是真花钱的一次调用——不摆出来的话，这一跑的账就少算了。
                    # 一镜多人时这一镜是**每句一次**，所以这里加的是次数（不是 1）。
                    dialogue_calls += int(tp.get("shot_dialogue") or 0)
                    for aid in _referenced_asset_ids(tp):
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
    # 预算闸：整图运行是「一次点击派出几十次调用」的入口，超限时要在确认弹窗里
    # 明确说出来（前端据此要求再确认一次，后端在受理时会再核一遍）
    state = run_budget.gate(total_tasks, len(pending), limit=run_budget.call_budget())
    if state["exceeds"] or state["uncertain"]:
        notes.append({"level": "warning", "text": run_budget.gate_message(state)})
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
    if pinned:
        # 回滚只在「单跑某个节点」时生效。整图会把这些节点重跑一遍，那时还按旧版给
        # 下游，用户会看到「我刚跑的图没被用上」——必须在点下去之前就说清楚
        notes.append(
            {
                "level": "warning",
                "text": "、".join(f"「{x}」" for x in pinned)
                + " 当前交付的是回滚后的旧版本。整图会把每个节点都重跑一遍，"
                "所以这一跑它们一律按最新那一版算；想让回滚生效，请只单跑下游那个节点。",
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
    if dialogue_calls:
        # 语音合成是**额外**的一次付费调用（不占任务名额），必须单独说出来：
        # 不说的话，用户看着「这一跑 5 次调用」的账，实际还会再花 5 次配音的钱
        notes.append(
            {
                "level": "warning",
                "text": f"逐镜对白：这一跑还会额外做 {dialogue_calls} 次语音合成"
                "（按所选语音服务计费，不计在上面那个次数里），"
                "再把每段配音分别附给对应镜头的视频。"
                "一镜里有几个人说话时，每一句各合成一次、再拼成这一镜的一条配音"
                "（段与段之间补一点点静音，所以拼完比各句之和略长）。",
            }
        )
    if reused:
        top = "、".join(f"「{r['name']}」{r['count']} 次" for r in reused[:5])
        notes.append({"level": "info", "text": f"本次会复用已有素材：{top}。"})

    return {
        "nodes": items,
        "totals": {
            "tasks": total_tasks,
            # 「调用次数」= 任务数：文档节点一次调用产出一个任务（分块则一块一个），
            # 图片节点的 n 张是同一次请求（算一次），视频一段一次。所以这两个数同源，
            # 但语义不同——前端文案要说「次」而不是「个任务」。
            "calls": total_tasks,
            "steps": step_nodes,
            "byKind": by_kind,
            "pendingNodes": len(pending),
            "rerunNodes": len(rerun),
            "blockedNodes": len(blocked),
            # 逐镜对白的额外语音合成次数（不属于「调用次数」那一栏，也还没排进任务）
            "dialogueCalls": dialogue_calls,
        },
        "gate": state,
        "reused": reused,
        "notes": notes,
        "billable": billable,
    }


async def lint_graph(project_id: int) -> dict:
    """分镜静态体检：把画布上每份镜头表拉出来，零成本检查镜头语言。

    与 `preview_graph` 的分工：那个答「这一跑要花多少钱」，这个答「拍出来会不会难看」。
    两者都是**纯读**：不建任务、不写库、不调模型（连模型解析都不做），可以随手点。

    只体检「能解析出镜头表」的内容：
    - 分镜节点（storyboard）即使解析不出来也要如实说出来——格式写歪了正是最该被点出来的；
    - 其它文档节点（小说/剧本/资产表）解析不出镜头表是正常的，**静默跳过**，不当成问题报。
    - 内容只看两处：手改正文（docText）与已生成的正文。节点上的 `prompt` 是「生成要求」，
      不是内容，不参与体检（否则还没跑过的节点会因为一段指令文本被误报）。
    """
    async with SessionLocal() as db:
        project = await db.get(Project, project_id)
        if project is None or not project.canvas_json:
            raise ValueError("画布不存在")
        doc = json.loads(project.canvas_json)

        nodes_out: list[dict] = []
        warnings: list[dict] = []
        skipped: list[dict] = []
        shot_total = 0

        for node in doc.get("nodes", []):
            nid = node.get("id")
            ntype = normalize_type(node.get("type") or "")
            if not ntype or not nid:
                continue
            label = NODE_SCHEMAS.get(ntype, {}).get("label") or ntype
            assets = await _latest_task_assets(
                db, project_id, nid, version_pin_of(node), picks_of(node)
            )
            # 手改正文 > 已生成的正文。
            # 注意**不能**再兜底到 `node.data.prompt`：对文档节点来说 prompt 是「生成要求」
            # （比如「请按三幕结构改写，每镜不超过 4 秒」），不是内容。把它当内容会对着
            # 一堆指令文本报「解析不出镜头表」，而那个节点其实只是还没跑过。
            text = _doc_override_text(node) or _doc_asset_text(assets)

            is_sheet_node = is_storyboard_sheet(ntype)
            if not text:
                if is_sheet_node:
                    skipped.append({"id": nid, "label": label, "reason": "这个节点还没有内容，先跑一次"})
                continue

            shots, findings, summary = storyboard_lint.lint_text(text)
            if not shots:
                if is_sheet_node:
                    # 分镜节点解析不出镜头表 = 下游逐镜出图会直接报错，必须现在就说
                    warnings.append(
                        storyboard_lint.Finding(
                            code="unparsable_storyboard",
                            level="warn",
                            message="没能从内容里解析出镜头表，下游的逐镜出图/出片会直接失败。",
                            suggestion="标题按 `### 镜头1 | 中景 | 缓慢推近 | 4s` 写，"
                            "字段用 `- 画面：`、`- 首帧提示词：`。",
                        ).as_warning(label)
                    )
                continue

            shot_total += summary["shots"]
            nodes_out.append(
                {
                    "id": nid,
                    "type": ntype,
                    "label": label,
                    "summary": summary,
                    "findings": [
                        {
                            "code": f.code,
                            "level": f.level,
                            "message": f.message,
                            "suggestion": f.suggestion,
                            "shots": list(f.shots),
                        }
                        for f in findings
                    ],
                }
            )
            warnings.extend(f.as_warning(label) for f in findings)

    level_rank = {"warn": 0, "info": 1}
    warnings.sort(key=lambda w: (level_rank.get(w["level"], 2), w["code"]))

    return {
        "blocking": False,
        "warnings": warnings,
        "nodes": nodes_out,
        "skipped": skipped,
        "shotTotal": shot_total,
    }


class AnimaticBusy(RuntimeError):
    """已有一条样片在渲染。

    渲染是纯 CPU 的（zoompan 逐帧算），两条同时跑只会互相拖慢、双双超时，
    所以这里直接拒绝后来的那条，让用户等一会儿再点——比默默排队两分钟好。
    """


class AnimaticInputError(ValueError):
    """出样片的输入不成立（分镜表没接、还没有分镜图、图与镜号对不上…）。

    单独一个类型是为了把「用户改一下就能跑」与「画布/节点根本不存在」分开：
    前者回 400 并把怎么改说清楚，后者回 404。
    """


# 进程内只允许一条样片在渲染。
# 模块级而不是塞进某个对象：渲染是 GPU/CPU 密集的，两处各持一把锁等于没锁。
# 顺带说明为什么它不会把测试搞坏：无竞争的 acquire 走的是不碰事件循环的快路径，
# 所以每个用例各自 `asyncio.run`（新循环）也不会撞上「绑定了另一个事件循环」。
_ANIMATIC_LOCK = asyncio.Lock()


async def _animatic_narration(
    db: AsyncSession,
    *,
    data: dict,
    shots: list,
    clips: list,
    video_seconds: int,
) -> tuple[Path | None, dict]:
    """按需要在出片前把旁白做好，返回 (音轨路径, 写进报告的那一段)。

    三条口径：

    - **只念台词，不念画面描述。** 画面描述是给生图模型的提示词（「中景，角色站在
      花园中央，缓缓抬头」），念出来是制作说明而不是旁白。一句台词都没有时直接报错
      并告诉用户去哪儿补，而不是拿画面描述凑一段——那样他会听到一版莫名其妙的解说词。
    - **先做旁白再渲染。** 语音合成是一次付费调用：配置不对、台词为空这些情况要在
      几分钟的渲染之前就报出来。
    - **旁白只是一条整轨，不与镜头逐一对齐。** 逐镜对齐要按每镜时长做静音填充，
      留给按角色分配音色那一步（数字人）；这一版如实说明「旁白比画面长多少秒」。
    """
    if not data.get("sampleNarration"):
        return None, {"enabled": False}

    by_no = {str(getattr(s, "no", "")): s for s in shots}
    used = [by_no[str(c.shot_no)] for c in clips if str(c.shot_no) in by_no]
    text, lines = speech.narration_from_shots(used)
    if not lines:
        raise AnimaticInputError(
            "这份分镜表里一句台词都没有，配不了旁白。"
            "可以先去「分镜」节点给每镜补一行「台词：…」，或者把旁白关掉"
        )

    model_key = await speech_service.default_model_key(db)
    try:
        asset, meta = await speech_service.synthesize_to_asset(
            db,
            model_key=model_key,
            text=text,
            voice=speech.sanitize_voice(data.get("sampleVoice")),
            source="animatic",
            name=f"样片旁白 · {lines} 句",
            note="来自分镜表的台词，整段一条（未按镜头逐句对齐）",
        )
    except AdapterError as e:
        # 上游/配置类问题按「输入不对」上报（400），别让它变成一句 500
        raise AnimaticInputError(f"配旁白失败：{e}") from e

    note = speech.truncation_note(audio_seconds=meta["seconds"], video_seconds=video_seconds)
    logger.info(
        "样片旁白已生成：%s 句 / %s 字 / %s 秒（音色 %s）",
        lines, meta["chars"], f"{meta['seconds']:.1f}" if meta["seconds"] else "?", meta["voice"] or "默认",
    )
    return storage.abs_path(asset.filename), {
        "enabled": True,
        "assetId": asset.id,
        "url": f"/media/{asset.filename}",
        "chars": meta["chars"],
        "lines": lines,
        "seconds": meta["seconds"],
        "voice": meta["voice"] or "服务默认音色",
        "note": note,
    }


async def render_animatic(project_id: int, node_id: str) -> tuple[Asset, dict]:
    """从「分镜图」节点出一版静图缓动样片：零生成成本，先审节奏。

    与生成链的关系：它**不是**一个节点、不建任务、不进自动链，也不花一分钱；
    只是拿这个节点已经出好的图，配上上游分镜表写的时长与运镜，本地渲染一条片子。
    所以设定、机位有变化时，随手再出一版就是了。

    内容口径与体检、出图三处完全一致（同一个 `parse_storyboard`、同一套上游取值），
    否则会出现「体检说 6 镜、样片只认 5 镜」这种最难解释的不一致。
    """
    if _ANIMATIC_LOCK.locked():
        raise AnimaticBusy("正在渲染上一条样片，请等它出完再点")

    async with _ANIMATIC_LOCK:
        async with SessionLocal() as db:
            project = await db.get(Project, project_id)
            if project is None or not project.canvas_json:
                raise ValueError("画布不存在")
            doc = json.loads(project.canvas_json)
            node = next((n for n in doc.get("nodes", []) if n.get("id") == node_id), None)
            if node is None:
                raise ValueError("节点不存在")
            ntype = normalize_type(node.get("type") or "")
            if not is_storyboard_image(ntype):
                raise AnimaticInputError("只有「分镜图」节点能出样片——它才有一镜一张图")

            upstream = await _resolve_upstream(db, project_id, doc, node_id)
            _prompt, _images, _videos, upstream_text = await _node_inputs(db, doc, node, upstream)
            shots = storyboard_sheet.parse_storyboard(upstream_text)
            if not shots:
                raise AnimaticInputError(
                    "没能从上游内容里解析出分镜表：出样片要知道每镜几秒、怎么运镜，"
                    "请把上游「分镜」节点接进来"
                )

            assets = await _latest_task_assets(
                db, project_id, node_id, version_pin_of(node), picks_of(node)
            )
            by_shot = await _assets_by_shot(db, assets)
            if not by_shot:
                raise AnimaticInputError("这个节点还没有出过分镜图，先运行一次再出样片")

            data = node.get("data") or {}
            plan = animatic.plan(
                shots,
                by_shot,
                default_seconds=animatic.sanitize_seconds(data.get("sampleShotSeconds")),
                default_move=animatic.sanitize_default_move(str(data.get("sampleDefaultMove") or "")),
            )
            if not plan.clips:
                detail = "；".join(f"镜头{s['shot']}（{s['reason']}）" for s in plan.skipped[:6])
                raise AnimaticInputError(
                    f"没有任何一镜能进样片：{detail or '分镜表与已出的图对不上镜号'}"
                )

            # 先按文件实际在不在把镜头筛一遍。
            # 库里有一行 Asset 不代表文件还在（数据目录被清理过、手动搬过、同步工具删过），
            # 这种图如果交给 ffmpeg，报出来的是「Invalid data found」这类跟用户无关的话；
            # 按「这一镜跳过」处理，才能在后来的报告里说清是缺了哪几镜、为什么缺。
            items: list[tuple[object, animatic.AnimaticClip, tuple[int, int]]] = []
            for clip in plan.clips:
                asset = by_shot[clip.shot_no]
                path = storage.abs_path(asset.filename)
                if not path.exists():
                    plan.skipped.append({"shot": clip.shot_no, "reason": "图片文件不在了"})
                    continue
                size = image_size.read_image_size_from_file(path)
                if not size:
                    size = (int(asset.width or 0), int(asset.height or 0))
                if not size[0] or not size[1]:
                    plan.skipped.append({"shot": clip.shot_no, "reason": "读不出图片尺寸"})
                    continue
                items.append((path, clip, (max(2, size[0]), max(2, size[1]))))

            if not items:
                raise AnimaticInputError(
                    "样片用到的分镜图都读不到了（文件被清理或搬走过）。"
                    "可以先重跑一次「分镜图」节点，再出样片"
                )
            if len(items) < len(plan.clips):
                # 时长与镜数都以真正进片的那几个算，报告才对得上画面
                kept = {clip.shot_no for _p, clip, _s in items}
                plan.clips = [c for c in plan.clips if c.shot_no in kept]

            # 成片尺寸以**第一镜的图**为准：同一个节点用同一个尺寸设置出图，
            # 拿首镜定基准即可；万一混进了别的尺寸，后面每镜仍按自己的原图算工作画布，
            # 由 ffmpeg 按「填满」裁切对齐，不会因为一张异形图整条样片失败。
            base = image_size.read_image_size_from_file(items[0][0]) or items[0][2]
            out_size = animatic.output_size(
                base[0],
                base[1],
                ratio=animatic.sanitize_ratio(str(data.get("sampleRatio") or "")),
                long_side=animatic.sanitize_long_side(data.get("sampleRes")),
            )

            # 旁白要在渲染之前做完：语音合成是一次付费调用，先做就能在配置不对、
            # 台词为空这些情况下立刻报错，而不是让人先等几分钟渲染再失败。
            audio_path, narration = await _animatic_narration(
                db, data=data, shots=shots, clips=plan.clips, video_seconds=plan.seconds
            )

            out_path = await ffmpeg_service.render_animatic(
                items, out_size=out_size, audio=audio_path
            )

            rel = ffmpeg_service._save_asset_file(out_path, "mp4")
            record = Asset(
                kind="video",
                filename=rel,
                original_name=f"样片_{len(plan.clips)}镜_{out_size[0]}x{out_size[1]}.mp4",
                content_type="video/mp4",
                size=out_path.stat().st_size,
                source="animatic",
                width=out_size[0],
                height=out_size[1],
                duration=plan.seconds,
                # 来路写进 prompt：资产库里一眼能看出这条片子是怎么来的、跳过了哪几镜
                prompt=animatic.summarize(plan),
            )
            db.add(record)
            await db.commit()
            await db.refresh(record)

            report = plan.to_report()
            report["size"] = [out_size[0], out_size[1]]
            report["fps"] = animatic.FPS
            report["bytes"] = record.size
            report["narration"] = narration
            logger.info(
                "画布 %s 节点 %s 出样片：%s 镜 / %s 秒 / %sx%s（跳过 %s 镜%s）",
                project_id, node_id, len(plan.clips), plan.seconds,
                out_size[0], out_size[1], len(plan.skipped),
                "，带旁白" if narration.get("enabled") else "",
            )
            return record, report


async def _run_full_graph(project_id: int, run_id: str = "") -> None:
    """整图执行：后台协程按拓扑序逐节点跑，上游失败则下游跳过。"""
    try:
        await _walk_graph(project_id, run_id)
    finally:
        # 不管中间炸成什么样，都要把这一跑标成「结束了」——
        # 漏了这一步，前端会一直以为还在跑，永远不弹那一跑的实际账。
        _finish_run(run_id)


async def _walk_graph(project_id: int, run_id: str) -> None:
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
                # 整图会把每个可执行节点都重跑一遍，所以忽略各节点上回滚的版本：
                # 这一跑它们自己就会产出新版，下游该用新的
                upstream = await _resolve_upstream(db, project_id, doc, nid, use_pins=False)
                tasks = await _build_node_tasks(db, project_id, doc, node, upstream)
                await _stamp_run(db, tasks, run_id)
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


def start_full_graph(project_id: int, expected: int = 0) -> str:
    """启动整图执行（不阻塞请求），返回这一跑的 run_id。

    `expected` 是受理时算出来的预估调用次数。要在这里记下来，是因为整图**边跑边派**：
    只按已存在的任务判断「跑完了没有」会在第一个节点结束时误判（见 `_ACTIVE_RUNS`）。
    """
    run_id = new_run_id()
    _register_run(run_id, project_id, expected)
    asyncio.get_event_loop().create_task(_run_full_graph(project_id, run_id))
    return run_id


async def run_summary(project_id: int, run_id: str) -> dict:
    """这一跑（run_id）的实际账：派了多少次、成了几条、失败几条、出了多少产物。

    「实际」的口径是**我们自己发出去的调用**（一个任务一次调用），不是供应商回的用量：
    我们发了几次是可数的、也是我们要为之后果负责的那部分；上游报不报 token 由它决定，
    报了我们也不用（没有价格表，换不成钱）。

    全跑完之前也能调：`finished` 为 False 时前端会继续轮询，而不是拿半截数字当结论。
    """
    if not run_id:
        raise ValueError("缺少 run_id")
    progress = run_progress(run_id)
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Task)
                .where(Task.run_id == run_id, Task.canvas_project_id == project_id)
                .order_by(Task.id.asc())
            )
        ).scalars().all()
        if not rows:
            raise ValueError("这一跑没有对应的任务记录（run_id 不对，或记录已被清理）")

        task_ids = [t.id for t in rows]
        assets = (
            await db.execute(select(Asset).where(Asset.task_id.in_(task_ids)))
        ).scalars().all()

        project = await db.get(Project, project_id)
        labels: dict[str, str] = {}
        if project and project.canvas_json:
            for n in json.loads(project.canvas_json).get("nodes", []):
                labels[str(n.get("id"))] = NODE_SCHEMAS.get(
                    normalize_type(n.get("type") or ""), {}
                ).get("label") or str(n.get("type") or "")

    by_kind: dict[str, int] = {}
    status_count: dict[str, int] = {}
    per_node: dict[str, dict] = {}
    for t in rows:
        by_kind[t.kind] = by_kind.get(t.kind, 0) + 1
        status_count[t.status] = status_count.get(t.status, 0) + 1
        nid = str(t.canvas_node_id or "")
        slot = per_node.setdefault(nid, {"id": nid, "label": labels.get(nid, "未归属"), "calls": 0, "failed": 0})
        slot["calls"] += 1
        if t.status in ("failed", "cancelled"):
            slot["failed"] += 1

    done = sum(status_count.get(s, 0) for s in ("completed", "failed", "cancelled"))
    running = len(rows) - done
    video_seconds = sum(int(a.duration or 0) for a in assets if a.kind == "video")
    started = min((t.created_at for t in rows if t.created_at), default=None)
    ended = max((t.completed_at for t in rows if t.completed_at), default=None)
    elapsed = int((ended - started).total_seconds()) if (started and ended) else None

    return {
        "runId": run_id,
        "calls": len(rows),
        "byKind": by_kind,
        "status": status_count,
        "completed": status_count.get("completed", 0),
        "failed": sum(status_count.get(s, 0) for s in ("failed", "cancelled")),
        "running": running,
        # 两个条件都要满足：已有的任务都结束了，**并且**这一跑本身已经走完整图。
        # 少了后半句，整图跑到第一个节点结束时就会被当成跑完（它是边跑边派的）。
        "finished": running == 0 and not progress["active"],
        "active": progress["active"],
        "expected": progress["expected"],
        "products": {
            "images": sum(1 for a in assets if a.kind == "image"),
            "videos": sum(1 for a in assets if a.kind == "video"),
            "documents": sum(1 for a in assets if a.kind == "document"),
            "videoSeconds": video_seconds,
        },
        "nodes": sorted(per_node.values(), key=lambda x: (-x["calls"], x["id"])),
        "startedAt": started.isoformat() if started else None,
        "endedAt": ended.isoformat() if ended else None,
        "elapsedSec": elapsed,
    }
