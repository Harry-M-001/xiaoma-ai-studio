"""社区分享：把「一个东西」打包成能贴到任何地方的一段文本。

定位与边界（`路线图.md` #20）：

- **分享单元**是一份带 `schemaVersion` / `requires` / `license` 的 JSON 快照。
  三个字段各管一件事，缺一个都会在别人的机器上出问题：
  - `schemaVersion` 管**能不能读**：格式变了要能明说「你的版本太旧」，
    而不是解析到一半报一个看不懂的错。
  - `requires` 管**能不能跑**：对方缺图片模型、或版本里还没有这个节点类型，
    都要在导入前说清楚。这类问题不提前说，用户会在跑到一半时才发现。
  - `license` 管**能不能用**：作者声明授权范围。它只影响「别人可以拿它做什么」，
    不影响技术能不能导入，所以单独一栏展示。
- **零服务端**：分享只以文件或一段分享码的形式流转，服务端不存、不查、不分发。
  这里两个函数都是纯函数（导出 / 校验），没有任何落库动作。
- **分享的是「怎么做」，不是「做出来的东西」**：只带节点拓扑、节点要求与参数，
  不带产出的图片视频、不带任务记录。带上产物会让快照从几 KB 变成几十 MB，
  而且等于把作者生成的东西一并送出去了。

导入侧的**严格**是刻意的（与画布 Agent 的宽容相反）：Agent 面对的是模型胡写，
能救则救；这里面对的是一个人有意分享的东西，读不懂就该直说，而不是猜着改。
"""

from __future__ import annotations

import base64
import json
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.registry.canvas_nodes import (
    CANVAS_SCHEMA_VERSION,
    NODE_SCHEMAS,
    normalize_type,
    validate_document,
)

SHARE_FORMAT = "xiaoma-share"
SHARE_SCHEMA_VERSION = 1
# 分享码前缀：一串 base64 里认出「这是我们家的东西」，也顺便带上版本
CODE_PREFIX = "XMS1:"
# 超过这个长度的分享码就不建议用了（聊天窗口里贴不下，还容易被截断）
CODE_SOFT_LIMIT = 6000

# 作者可以声明的授权范围。给固定几档而不是自由填写：
# 自由填写的结果是每个人写的都不一样，读的人反而判断不了能做什么。
LICENSES = {
    "PolyForm-Noncommercial-1.0.0": "保留署名，禁止商用（与本项目一致）",
    "CC-BY-NC-4.0": "保留署名，禁止商用，允许修改",
    "CC0-1.0": "放弃权利，随便用",
    "AllRightsReserved": "保留所有权利，仅供查看与学习",
}
DEFAULT_LICENSE = "PolyForm-Noncommercial-1.0.0"

# 快照里带哪些节点字段。data 只带这些键，免得把运行时缓存（schema/status）
# 甚至本机的模型编号一起分享出去——模型编号在别人机器上指向的是另一个服务。
DATA_KEYS = (
    "prompt",
    "size",
    "n",
    "duration",
    "ratio",
    "mode",
    "chapterCount",
    "sceneCount",
    "shotCount",
    "assetScope",
    "shotVideo",
    "sceneRefs",
    "shotLimit",
    "styleKey",
    "mentionRefs",
)


@dataclass
class Imported:
    """导入侧读完一份快照后的结论。"""

    ok: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    title: str = ""
    description: str = ""
    license: str = ""
    author: str = ""
    createdAt: str = ""
    appVersion: str = ""
    requires: dict = field(default_factory=dict)
    missingNodeKinds: list[str] = field(default_factory=list)
    missingModalities: list[str] = field(default_factory=list)
    doc: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "title": self.title,
            "description": self.description,
            "license": self.license,
            "licenseText": LICENSES.get(self.license, ""),
            "author": self.author,
            "createdAt": self.createdAt,
            "appVersion": self.appVersion,
            "requires": self.requires,
            "missingNodeKinds": self.missingNodeKinds,
            "missingModalities": self.missingModalities,
            "doc": self.doc,
        }


# ============================================================
# 导出
# ============================================================


def required_node_kinds(nodes: list[dict]) -> list[str]:
    """这份画布用到了哪些节点类型（导入方据此判断自己版本够不够）。"""
    kinds: list[str] = []
    for n in nodes:
        kind = normalize_type(str(n.get("type") or ""))
        if kind and kind not in kinds:
            kinds.append(kind)
    return kinds


def required_modalities(nodes: list[dict]) -> list[str]:
    """这份画布需要哪些能力（text/image/video），导入方据此判断缺不缺模型。"""
    need: list[str] = []
    for n in nodes:
        schema = NODE_SCHEMAS.get(normalize_type(str(n.get("type") or ""))) or {}
        category = schema.get("category", "")
        modality = {"document": "text", "image": "image", "video": "video"}.get(category)
        if modality and modality not in need:
            need.append(modality)
    return need


def _slim_node(node: dict) -> dict:
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    kept = {k: data[k] for k in DATA_KEYS if k in data and data[k] not in ("", None, [], {})}
    return {
        "id": str(node.get("id") or ""),
        "type": normalize_type(str(node.get("type") or "")),
        "position": node.get("position") or {"x": 0, "y": 0},
        "data": kept,
    }


def _slim_edges(edges: list[dict]) -> list[dict]:
    out = []
    for e in edges:
        out.append(
            {
                "id": str(e.get("id") or f"e{len(out)}"),
                "source": str(e.get("source") or ""),
                "target": str(e.get("target") or ""),
                "sourceHandle": e.get("sourceHandle"),
                "targetHandle": e.get("targetHandle"),
            }
        )
    return out


def export_canvas(
    doc: dict,
    *,
    title: str,
    description: str = "",
    author: str = "",
    license_key: str = DEFAULT_LICENSE,
    app_version: str = "",
) -> dict:
    """把一张画布打包成分享快照。

    `doc` 可以是前端当前状态（不必先保存）——「我想分享的」与「我屏幕上看到的」
    是同一个东西，不该逼用户先点一次保存。
    """
    nodes_raw = doc.get("nodes") or []
    edges_raw = doc.get("edges") or []
    nodes = [_slim_node(n) for n in nodes_raw if isinstance(n, dict)]
    edges = _slim_edges([e for e in edges_raw if isinstance(e, dict)])

    # 导出前先按契约自检一遍：分享出一份自己都打不开的东西没有意义
    validate_document({"schemaVersion": CANVAS_SCHEMA_VERSION, "nodes": nodes_raw, "edges": edges_raw})

    return {
        "format": SHARE_FORMAT,
        "schemaVersion": SHARE_SCHEMA_VERSION,
        "kind": "canvas",
        "title": (title or "").strip()[:100] or "未命名画布",
        "description": (description or "").strip()[:500],
        "author": (author or "").strip()[:60],
        "license": license_key if license_key in LICENSES else DEFAULT_LICENSE,
        "createdAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "appVersion": app_version,
        "canvasSchemaVersion": CANVAS_SCHEMA_VERSION,
        "requires": {
            "nodeKinds": required_node_kinds(nodes),
            "modalities": required_modalities(nodes),
            "canvasSchemaVersion": CANVAS_SCHEMA_VERSION,
        },
        "payload": {"nodes": nodes, "edges": edges},
        "notes": ["不含任何产出文件与密钥，只带节点拓扑、节点要求与参数。"],
    }


# ============================================================
# 分享码：让「贴一段文本」成为可能
# ============================================================


def encode_code(snapshot: dict) -> str:
    """快照 → 一段可粘贴的分享码（压缩 + base64url）。

    不做加密也不会做：分享内容本来就是给人看的，加密只会让「贴之前先看看是什么」
    这件事做不到。压缩是为了塞进聊天窗口。
    """
    raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    packed = zlib.compress(raw, 9)
    return CODE_PREFIX + base64.urlsafe_b64encode(packed).decode("ascii").rstrip("=")


def decode_code(code: str) -> dict:
    """分享码 → 快照。只认前缀，不做「猜它可能是分享码」这种事。"""
    text = (code or "").strip()
    if not text.startswith(CODE_PREFIX):
        raise ValueError(f"这不是分享码（应当以 {CODE_PREFIX} 开头）")
    body = text[len(CODE_PREFIX) :]
    body += "=" * (-len(body) % 4)
    try:
        packed = base64.urlsafe_b64decode(body.encode("ascii"))
        raw = zlib.decompress(packed)
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ValueError("分享码不完整或已损坏，请让对方重新复制一份") from e
    if not isinstance(data, dict):
        raise ValueError("分享码里不是一个对象")
    return data


def parse_share(text: str) -> dict:
    """把用户粘贴的东西解析成快照：分享码或 JSON 都认。"""
    body = (text or "").strip()
    if not body:
        raise ValueError("请粘贴分享码，或选择一个 .json 文件")
    if body.startswith(CODE_PREFIX):
        return decode_code(body)
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise ValueError(f"这既不是分享码也不是 JSON：{e.msg}（第 {e.lineno} 行）") from e
    if not isinstance(data, dict):
        raise ValueError("文件里不是一个 JSON 对象")
    return data


# ============================================================
# 导入
# ============================================================


def import_share(
    text: str, *, local_node_kinds: set[str] | None = None, local_modalities: set[str] | None = None
) -> Imported:
    """读一份分享快照，给出「能不能用、缺什么」的结论。

    只读不写：把结论交给调用方，由它在界面上让用户确认之后再落到画布上。
    导入失败时返回 `ok=False` 与**具体原因**，而不是抛异常让前端只能显示「导入失败」。
    """
    result = Imported()
    try:
        snapshot = parse_share(text)
    except ValueError as e:
        result.errors.append(str(e))
        return result

    fmt = snapshot.get("format")
    if fmt != SHARE_FORMAT:
        result.errors.append(
            f"这不是小马AI工坊的分享文件（format={fmt!r}，应当是 {SHARE_FORMAT!r}）"
        )
        return result

    version = snapshot.get("schemaVersion")
    if version != SHARE_SCHEMA_VERSION:
        result.errors.append(
            f"这份分享用的格式版本是 {version}，本版本只能读 {SHARE_SCHEMA_VERSION}。"
            "请升级本程序后再导入。"
        )
        return result

    kind = snapshot.get("kind")
    if kind != "canvas":
        result.errors.append(f"暂不支持导入这种分享单元（kind={kind!r}），目前只支持画布。")
        return result

    result.title = str(snapshot.get("title") or "")
    result.description = str(snapshot.get("description") or "")
    result.license = str(snapshot.get("license") or "")
    result.author = str(snapshot.get("author") or "")
    result.createdAt = str(snapshot.get("createdAt") or "")
    result.appVersion = str(snapshot.get("appVersion") or "")
    requires = snapshot.get("requires") if isinstance(snapshot.get("requires"), dict) else {}
    result.requires = requires

    payload = snapshot.get("payload")
    if not isinstance(payload, dict):
        result.errors.append("分享内容里没有 payload")
        return result
    nodes = payload.get("nodes") or []
    edges = payload.get("edges") or []
    if not isinstance(nodes, list) or not isinstance(edges, list):
        result.errors.append("payload 里的 nodes / edges 必须是数组")
        return result

    try:
        clean_nodes, clean_edges = validate_document(
            {
                "schemaVersion": snapshot.get("canvasSchemaVersion", CANVAS_SCHEMA_VERSION),
                "nodes": nodes,
                "edges": edges,
            }
        )
    except ValueError as e:
        result.errors.append(f"分享的画布不合法：{e}")
        return result

    # 「缺什么」要按**本机**的实情说，而不是照抄对方声明的 requires：
    # 对方的声明可能不准（比如他本人也没那个模型），以我们这边看到的为准。
    local_kinds = local_node_kinds if local_node_kinds is not None else set(NODE_SCHEMAS)
    used_kinds = required_node_kinds(clean_nodes)
    result.missingNodeKinds = [k for k in used_kinds if k not in local_kinds]

    needs = required_modalities(clean_nodes)
    if local_modalities is not None:
        result.missingModalities = [m for m in needs if m not in local_modalities]

    if result.missingNodeKinds:
        result.errors.append(
            "这份画布用到了本版本还没有的节点类型："
            + "、".join(result.missingNodeKinds)
            + "。请升级本程序后再导入。"
        )
    if result.missingModalities:
        label = {"text": "文本", "image": "图片", "video": "视频"}
        names = "、".join(label.get(m, m) for m in result.missingModalities)
        # 只是提醒不是错误：先导入、之后接入模型再跑，完全合理
        result.warnings.append(f"这份画布需要{names}模型，你目前还没接入；可以先导入，跑之前再接入。")

    if not result.license:
        result.warnings.append("这份分享没有声明授权范围，用之前最好先问一下作者。")
    elif result.license not in LICENSES:
        result.warnings.append(f"作者声明的授权范围是「{result.license}」，本程序不认识这一档。")

    result.doc = {
        "schemaVersion": CANVAS_SCHEMA_VERSION,
        "nodes": clean_nodes,
        "edges": clean_edges,
        "viewport": {},
    }
    result.ok = not result.errors
    return result
