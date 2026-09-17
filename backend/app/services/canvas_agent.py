"""E 期：自然语言搭画布。

一次结构化输出，不做工具循环（架构方案 §4）。LLM 只负责**拓扑与参数**，不写创作
内容——内容留给下游各节点的 Agent。这样 JSON 才可能稳：让模型同时产出「结构 + 800 字
小说」必然截断，而截断的 JSON 是解析不出来的。

三个难点，也是这个模块的全部内容：

**1. 契约下发，不写第二份清单。**
节点类型、每种节点吃什么吐什么、能配哪些参数、参数的合法取值，全部从 `NODE_SCHEMAS`
与 `param_options` 现取。加一个节点类型或加一个尺寸档位，Agent 立刻就会用，不用改这里。

**2. 宽容修复。**
LLM 的输出一定会有错：不认识新节点、把连线连成环、引用不存在的 id、把 `size` 写成
`1920x1080`（不在档位里）。这里**不抛异常**，而是丢掉坏的那一小块、留下能用的，
并把每次丢掉的**理由**讲清楚。一个字段拼错就让用户拿到空白画布，等于这个功能没做。
（对照：`canvas_nodes.validate_document` 是保存画布用的，那里必须严格——两者职责不同，
所以这里没有复用它的抛错逻辑，而是复用同一份 `NODE_SCHEMAS` 事实源。）

**3. 不问 LLM 要坐标。**
模型给的 x/y 十有八九重叠成一坨。拓扑本身是确定的：按层（最长路径）算列、同层往下排，
出来永远干净、可读，而且是**确定的**——同样的拓扑每次落在同一个位置，测试也写得动。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ParamOption
from app.providers.base import AdapterError
from app.registry.canvas_nodes import NODE_SCHEMAS, VIDEO_MODES, normalize_type
from app.services import provider_store, style_service

logger = logging.getLogger("xiaoma.canvas_agent")

# ---- Agent 结果协议（#22） ----
#
# 把「为什么没成」做成**机器可读**的状态，前端据此给不同的下一步，
# 而不是去匹配报错文案（文案一改，判断就悄悄失效）。
STATUS_OK = "ok"                      # 出了可用的草稿
STATUS_NEED_INPUT = "need_input"      # 需求太单薄，改一句话就行（不花额度）
STATUS_NEED_CREDENTIALS = "need_credentials"  # 本机没有可用的文本模型
STATUS_UNPARSABLE = "unparsable"      # 模型没给出能解析的 JSON（可换模型/重试）
STATUS_UPSTREAM_ERROR = "upstream_error"  # 上游调用失败（带可读原因）

# 单次最多接受多少个节点：再多基本是模型在发散，用户拿到的是一团乱麻
MAX_NODES = 14
# 需求短于这个字数就先问清楚，别拿额度去猜
MIN_BRIEF_CHARS = 6
# 布局步长（与前端「一键铺链」保持一致，两种方式铺出来的画布看起来才是一套）
STEP_X = 300
STEP_Y = 240
ORIGIN = (80.0, 140.0)

# 「特征」→ 节点 data 里的参数名。契约用 features 声明能力，落库用这些键。
FEATURE_TO_PARAM = {
    "imageSize": "size",
    "sampleCount": "n",
    "duration": "duration",
    "ratio": "ratio",
    "videoMode": "mode",
    "chapterCount": "chapterCount",
    "sceneCount": "sceneCount",
    "shotCount": "shotCount",
    "assetScope": "assetScope",
    "shotVideo": "shotVideo",
    "sceneRefs": "sceneRefs",
    "shotLimit": "shotLimit",
    "styleSelect": "styleKey",
}

# 参数名 → 给用户看的中文标签。提示里说「画幅比例不在可选档位」，
# 比说「ratio 不在可选档位」有用得多——用户界面上根本没有 "ratio" 这个词。
PARAM_LABEL = {
    "size": "图片尺寸",
    "n": "生成张数",
    "duration": "视频时长",
    "ratio": "画幅比例",
    "mode": "视频模式",
    "styleKey": "风格",
    "chapterCount": "章数",
    "sceneCount": "场数",
    "shotCount": "镜头数",
    "shotLimit": "镜头上限",
    "assetScope": "资产范围",
    "shotVideo": "逐镜出片",
    "sceneRefs": "同场景串联",
}

# 参数的「档位类型」（对应 param_options.kind）：从档位表里取合法值来校验
PARAM_OPTION_KIND = {
    "size": "image_size",
    "n": "image_count",
    "duration": "video_duration",
    "ratio": "video_ratio",
}

# 只能由前端填、Agent 不许碰的键（模型需按当前可用模型解析，工作流要用户上传）
AGENT_FORBIDDEN_PARAMS = ("model_key", "refImages", "workflowId", "docText", "schema", "status")

# 节点类别 → 需要哪种能力（用于「这条链要什么模型」的前置提示）
CATEGORY_MODALITY = {"document": "text", "image": "image", "video": "video"}


@dataclass
class Draft:
    """Agent 产出的一份画布草稿（未落库）。"""

    status: str
    summary: str = ""
    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    next_action: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "summary": self.summary,
            "nodes": self.nodes,
            "edges": self.edges,
            "notes": self.notes,
            "warnings": self.warnings,
            "requires": self.requires,
            "missing": self.missing,
            "nextAction": self.next_action,
        }


# ============================================================
# 一、契约下发
# ============================================================


async def _option_values(db: AsyncSession) -> dict[str, list[str]]:
    """各档位的合法值（只取启用的），用于把合法取值一并告诉模型。

    不告诉模型「尺寸只有这几个」的话，它就会编一个 1920x1080 出来——
    而这个值到不了上游，只会被后端丢掉，用户看到的是「我明明说了尺寸却没生效」。
    """
    rows = await db.execute(
        select(ParamOption).where(ParamOption.enabled.is_(True)).order_by(ParamOption.sort_order)
    )
    out: dict[str, list[str]] = {}
    for r in rows.scalars().all():
        out.setdefault(r.kind, []).append(str(r.value))
    return out


def _value_hint(param: str, options: dict[str, list[str]]) -> str:
    kind = PARAM_OPTION_KIND.get(param)
    if kind and options.get(kind):
        return "，可选：" + " / ".join(options[kind])
    if param == "mode":
        return "，可选：" + " / ".join(f"{k}（{v}）" for k, v in VIDEO_MODES.items())
    if param == "assetScope":
        return "，可选：留空（全部）/ character / scene / prop"
    if param == "shotVideo":
        return "，可选：off / each / chain"
    return ""


def build_system_prompt(
    options: dict[str, list[str]], styles: list[style_service.StyleCard]
) -> str:
    """把「有哪些节点、能配什么、合法值是什么」讲给模型。"""
    lines: list[str] = [
        "你是「小马AI工坊」的搭画布助手。用户说一句话需求，你把它翻译成一张**节点拓扑图**。",
        "",
        "【绝对要求】",
        "1. 只输出一个 JSON 对象。不要解释、不要前后加说明文字、不要用 ``` 代码块包起来。",
        "2. 不要写创作内容。节点里的 prompt 是「给这个节点的工作要求」，不是小说/剧本正文。",
        "3. 只用下面列出的节点类型，不要发明新类型。",
        "4. 连线的两端必须是你自己定义过的 id，且不能连成环（数据是一条链往下走的）。",
        "5. 节点不超过 " + str(MAX_NODES) + " 个。够用就好，堆节点不会让片子更好。",
        "",
        "【可用节点类型】",
    ]
    for kind, schema in NODE_SCHEMAS.items():
        params = _agent_params(kind)
        in_types = "/".join(h["type"] for h in schema["handles"]["targets"]) or "无"
        out_type = "/".join(h["type"] for h in schema["handles"]["sources"]) or "无"
        line = f"- {kind}（{schema['label']}）：{schema['description']}；输入 {in_types}，输出 {out_type}"
        if params:
            line += "；可配参数：" + "、".join(
                f"{p}{_value_hint(p, options)}" for p in params
            )
        lines.append(line)

    lines += [
        "",
        "【输出格式】",
        "{",
        '  "summary": "一句话说明你搭了什么（给用户看的，不要写创作内容）",',
        '  "nodes": [',
        '    {"id": "n1", "kind": "idea", "prompt": "对这个节点的要求", "params": {}}',
        "  ],",
        '  "edges": [{"from": "n1", "to": "n2"}]',
        "}",
        "",
        "【常见需求的搭法】",
        "- 只要文字产出：idea → novel → script → storyboard",
        "- 还要看到画面：再接 assetSheet → assetImage（角色/场景设定图），",
        "  或 storyboard → storyboardImage（逐镜出图）",
        "- 还要出片子：storyboardImage 之后接 video（params 里给 shotVideo: chain，逐镜出片并首尾相连）",
    ]
    if styles:
        lines += ["", "【可选风格卡】用户明确提到风格时才填 params.styleKey（多个节点都填同一个）："]
        for s in styles:
            lines.append(f"- {s.key}：{s.name}")
    lines += ["", "用户没提风格就不要填 styleKey。", "现在，请只回 JSON。"]
    return "\n".join(lines)


def _agent_params(kind: str) -> list[str]:
    """该节点允许 Agent 填的参数（从 features 推出来，不另写清单）。"""
    schema = NODE_SCHEMAS.get(kind) or {}
    out: list[str] = []
    for feat in schema.get("features", []):
        param = FEATURE_TO_PARAM.get(feat)
        if param and param not in out:
            out.append(param)
    return out


def build_user_message(brief: str, style_asked: bool) -> str:
    text = f"需求：{brief.strip()}"
    if style_asked:
        text += "\n（用户提到了风格，请从可选风格卡里挑一张填进相关节点。）"
    return text


def style_mentioned(brief: str, styles: list[style_service.StyleCard]) -> bool:
    """需求里是不是真的提到了某张风格卡。

    只在提到时才催模型填 styleKey：无条件催会让它随手塞一张风格进去，
    而用户根本没要那个味道，还很难发现是哪儿带进来的。
    """
    text = brief or ""
    return any(s.name in text or s.key in text for s in styles)


# ============================================================
# 二、解析（模型很爱加解释和代码块）
# ============================================================

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def extract_json(text: str) -> dict | None:
    """从模型的回复里挖出那个 JSON 对象。

    三种常见形态都要认：干净的 JSON、被 ``` 包起来的、前后带解释的。
    用「括号配对」而不是「第一个 { 到最后一个 }」：后者在模型多写了一段带花括号的
    示例时会吞掉中间所有东西，解析出来是个四不像。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    candidates: list[str] = []
    fenced = _FENCE.findall(raw)
    candidates.extend(f.strip() for f in fenced)
    candidates.append(raw)

    for candidate in candidates:
        parsed = _first_balanced_object(candidate)
        if parsed is not None:
            return parsed
    return None


def _first_balanced_object(text: str) -> dict | None:
    start = text.find("{")
    while start >= 0:
        depth = 0
        in_str = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
        start = text.find("{", start + 1)
    return None


# ============================================================
# 三、宽容修复
# ============================================================


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _validate_param(
    param: str, value: Any, options: dict[str, list[str]], style_keys: set[str]
) -> tuple[Any, str]:
    """校验单个参数。返回 (可用值, 拒绝理由)；理由为空表示通过。

    理由都写成一句完整的话（「图片尺寸 1920x1080 不在可选档位（…）」），
    调用方直接接在「节点「图片生成」：」后面就是人话。
    """
    label = PARAM_LABEL.get(param, param)
    kind = PARAM_OPTION_KIND.get(param)
    if kind:
        allowed = options.get(kind) or []
        text = str(value)
        if param in ("n", "duration"):
            num = _coerce_int(value)
            if num is None:
                return None, f"{label}需要一个整数，收到的是 {value!r}"
            text = str(num)
        # 档位表为空时放行（该参数类型没配任何档位，交由上游判断，与 option_service 同口径）
        if allowed and text not in allowed:
            return None, f"{label} {text} 不在可选档位（{' / '.join(allowed)}）"
        return (_coerce_int(text) if param in ("n", "duration") else text), ""

    if param == "mode":
        if value in VIDEO_MODES:
            return value, ""
        return None, f"{label} {value} 不是支持的取值"

    if param == "styleKey":
        if not value:
            return None, ""
        if str(value) in style_keys:
            return str(value), ""
        return None, f"{label}「{value}」不存在"

    if param == "assetScope":
        if value in ("", None, "character", "scene", "prop"):
            return (str(value) if value else None), ""
        return None, f"{label} {value} 不是支持的取值"

    if param == "shotVideo":
        if value in ("off", "each", "chain"):
            return value, ""
        return None, f"{label} {value} 不是支持的取值"

    if param == "sceneRefs":
        if isinstance(value, bool):
            return value, ""
        return None, f"{label}需要是 true/false"

    if param in ("chapterCount", "sceneCount", "shotCount", "shotLimit"):
        num = _coerce_int(value)
        if num is None or num < 0:
            return None, f"{label}需要是 0 或正整数，收到的是 {value!r}"
        return min(num, 30), ""
    return None, f"不认识的参数 {param}"


def repair(
    raw: dict, options: dict[str, list[str]], styles: list[style_service.StyleCard]
) -> tuple[str, list[dict], list[dict], list[str]]:
    """把模型输出修成一份可用的拓扑。返回 (summary, nodes, edges, notes)。

    每一步修复都留一条 note：用户看到的不该是「凭空少了一个节点」，
    而是「你说的是 A，但契约里没有 A，所以我按 B 处理了」。
    """
    notes: list[str] = []
    style_keys = {s.key for s in styles}

    summary = str(raw.get("summary") or "").strip()

    raw_nodes = raw.get("nodes")
    if not isinstance(raw_nodes, list):
        raw_nodes = []
    if len(raw_nodes) > MAX_NODES:
        notes.append(f"模型给了 {len(raw_nodes)} 个节点，超过 {MAX_NODES} 个的部分已丢弃（太多了反而看不清）。")
        raw_nodes = raw_nodes[:MAX_NODES]

    nodes: list[dict] = []
    dropped_kinds: dict[str, int] = {}
    seen_ids: set[str] = set()
    for item in raw_nodes:
        if not isinstance(item, dict):
            continue
        nid = str(item.get("id") or "").strip()
        if not nid or nid in seen_ids:
            nid = f"n{len(nodes) + 1}"
        kind = normalize_type(str(item.get("kind") or item.get("type") or "").strip())
        if kind not in NODE_SCHEMAS:
            dropped_kinds[kind or "(空)"] = dropped_kinds.get(kind or "(空)", 0) + 1
            continue

        params: dict[str, Any] = {}
        allowed = _agent_params(kind)
        raw_params = item.get("params") if isinstance(item.get("params"), dict) else {}
        for key, value in raw_params.items():
            if key in AGENT_FORBIDDEN_PARAMS:
                continue
            if key not in allowed:
                notes.append(f"节点「{NODE_SCHEMAS[kind]['label']}」不支持参数 {key}，已忽略。")
                continue
            cleaned, reason = _validate_param(key, value, options, style_keys)
            if reason:
                notes.append(f"节点「{NODE_SCHEMAS[kind]['label']}」：{reason}，已改用默认值。")
                continue
            if cleaned is not None and cleaned != "":
                params[key] = cleaned

        data: dict[str, Any] = {"prompt": str(item.get("prompt") or "").strip()}
        data.update(params)
        nodes.append({"id": nid, "kind": kind, "data": data})
        seen_ids.add(nid)

    if dropped_kinds:
        detail = "、".join(f"{k} × {v}" for k, v in dropped_kinds.items())
        notes.append(f"模型用了契约里没有的节点类型（{detail}），这些节点已丢弃。")

    raw_edges = raw.get("edges")
    if not isinstance(raw_edges, list):
        raw_edges = []
    ids = {n["id"] for n in nodes}
    edges: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    dangling = 0
    for e in raw_edges:
        if not isinstance(e, dict):
            continue
        src = str(e.get("from") or e.get("source") or "").strip()
        dst = str(e.get("to") or e.get("target") or "").strip()
        if src not in ids or dst not in ids:
            dangling += 1
            continue
        if src == dst or (src, dst) in seen_pairs:
            continue
        seen_pairs.add((src, dst))
        edges.append({"from": src, "to": dst})
    if dangling:
        notes.append(f"有 {dangling} 条连线指向了不存在的节点，已丢弃。")

    edges, cyclic = _break_cycles(edges, [n["id"] for n in nodes])
    if cyclic:
        notes.append(f"有 {cyclic} 条连线会让流程成环（数据没法往下走），已断开。")

    return summary, nodes, edges, notes


def _break_cycles(edges: list[dict], order: list[str]) -> tuple[list[dict], int]:
    """按节点出现顺序做 DFS，遇到回边就丢掉那条边。

    丢边而不是丢节点：用户看得见的损失更小，而且拓扑还是通的。
    """
    rank = {nid: i for i, nid in enumerate(order)}
    adj: dict[str, list[dict]] = {}
    for e in edges:
        adj.setdefault(e["from"], []).append(e)

    WHITE, GREY, BLACK = 0, 1, 2
    state = {nid: WHITE for nid in order}
    kept: list[dict] = []
    dropped = 0

    def dfs(u: str) -> None:
        nonlocal dropped
        state[u] = GREY
        for e in sorted(adj.get(u, []), key=lambda x: rank.get(x["to"], 0)):
            v = e["to"]
            if state.get(v) == GREY:
                dropped += 1
                continue
            kept.append(e)
            if state.get(v) == WHITE:
                dfs(v)
        state[u] = BLACK

    for nid in order:
        if state[nid] == WHITE:
            dfs(nid)
    return kept, dropped


# ============================================================
# 四、自动布局
# ============================================================


def layout(nodes: list[dict], edges: list[dict]) -> list[dict]:
    """按层落位：列 = 最长路径层号，同层按出现顺序往下排。

    不问模型要坐标的理由写在模块开头。这里保证两件事：
    同样的拓扑永远落在同样的位置；同一层的节点不会重叠。
    """
    if not nodes:
        return nodes
    order = [n["id"] for n in nodes]
    preds: dict[str, list[str]] = {nid: [] for nid in order}
    for e in edges:
        preds.setdefault(e["to"], []).append(e["from"])

    level: dict[str, int] = {}

    def depth(nid: str, guard: set[str]) -> int:
        if nid in level:
            return level[nid]
        if nid in guard:  # 理论上不会有环（上面已经断了），兜底防止递归打转
            return 0
        guard.add(nid)
        ps = [p for p in preds.get(nid, []) if p in preds]
        value = 0 if not ps else max(depth(p, guard) for p in ps) + 1
        level[nid] = value
        return value

    for nid in order:
        depth(nid, set())

    columns: dict[int, list[str]] = {}
    for nid in order:
        columns.setdefault(level.get(nid, 0), []).append(nid)

    pos: dict[str, dict[str, float]] = {}
    ox, oy = ORIGIN
    tallest = max((len(v) for v in columns.values()), default=1)
    for col, ids_in_col in columns.items():
        # 竖向居中：短列不会一律贴顶，看上去更整齐
        offset = (tallest - len(ids_in_col)) * STEP_Y / 2
        for row, nid in enumerate(ids_in_col):
            pos[nid] = {"x": ox + col * STEP_X, "y": oy + offset + row * STEP_Y}

    for n in nodes:
        n["position"] = pos.get(n["id"], {"x": ox, "y": oy})
    return nodes


def required_modalities(nodes: list[dict]) -> list[str]:
    """这份草稿需要哪些能力（text/image/video），用于跑之前先提醒缺什么。"""
    need: list[str] = []
    for n in nodes:
        schema = NODE_SCHEMAS.get(n["kind"]) or {}
        modality = CATEGORY_MODALITY.get(schema.get("category", ""))
        if modality and modality not in need:
            need.append(modality)
    return need


# ============================================================
# 五、跑一次（含一次自我修复）
# ============================================================

# 模型把 JSON 写坏时的补救话术。给一次机会，但只给一次：
# 再失败基本是模型能力问题，继续重试只是烧额度。
_REPAIR_PROMPT = (
    "上一次的回复不是合法 JSON（无法解析）。请**只**输出那个 JSON 对象本身："
    "不要解释、不要代码块、不要注释。字段与格式见我上一条要求。"
)

_STATUS_NEXT_ACTION = {
    STATUS_NEED_INPUT: "把需求再说具体一点（想要什么题材、多长、要不要出片子），然后重试。",
    STATUS_NEED_CREDENTIALS: "到「模型服务」接入一个文本模型（也可以直接接入本机 Ollama），再回来试。",
    STATUS_UNPARSABLE: "换个更强的文本模型再试一次；本次没有改动画布。",
    STATUS_UPSTREAM_ERROR: "按提示排除上游问题后重试；本次没有改动画布。",
}


async def _collect(adapter: Any, model_name: str, messages: list[dict],
                   temperature: float, max_tokens: int) -> str:
    parts: list[str] = []
    async for piece in adapter.chat_stream(
        model_name, messages, temperature=temperature, max_tokens=max_tokens
    ):
        parts.append(piece)
    return "".join(parts).strip()


async def _available_modalities(db: AsyncSession) -> list[str]:
    rows = await provider_store.list_services(db)
    return list({o.modality for o in provider_store.list_model_options(rows)})


async def plan(
    db: AsyncSession,
    brief: str,
    *,
    model_key: str = "",
    max_tokens: int = 2000,
) -> Draft:
    """把一句话需求变成一份画布草稿。**只读**：不改画布、不建任务。"""
    text = (brief or "").strip()
    if len(text) < MIN_BRIEF_CHARS:
        return Draft(
            status=STATUS_NEED_INPUT,
            summary="",
            next_action=_STATUS_NEXT_ACTION[STATUS_NEED_INPUT],
            warnings=[
                "需求太短了，我猜不出你想拍什么。",
                "可以试试：「做一个 60 秒竖屏短剧，三幕结构，从一句话创意开始」",
                "或者：「小说到分镜，只要文字产出，不要出图」。",
            ],
        )

    options = await _option_values(db)
    styles = await style_service.load_cards(db)
    system = build_system_prompt(options, styles)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": build_user_message(text, style_mentioned(text, styles))},
    ]

    try:
        resolved = await provider_store.resolve_model(db, model_key, "text")
    except AdapterError:
        # 指定的模型不可用就退回「随便挑一个能用的文本模型」：
        # 用户点的是「AI 搭画布」，不是「用某个特定模型搭画布」。
        rows = await provider_store.list_services(db)
        first = next(iter(provider_store.list_model_options(rows, "text")), None)
        if first is None:
            return Draft(
                status=STATUS_NEED_CREDENTIALS,
                next_action=_STATUS_NEXT_ACTION[STATUS_NEED_CREDENTIALS],
                warnings=["还没有可用的文本模型，搭画布要先有一个能对话的模型。"],
            )
        try:
            resolved = await provider_store.resolve_model(db, first.key, "text")
        except AdapterError as e:
            return Draft(status=STATUS_NEED_CREDENTIALS, warnings=[str(e)],
                         next_action=_STATUS_NEXT_ACTION[STATUS_NEED_CREDENTIALS])

    raw: dict | None = None
    reply = ""
    try:
        reply = await _collect(resolved.adapter, resolved.model_name, messages, 0.2, max_tokens)
        raw = extract_json(reply)
        if raw is None:
            logger.info("画布 Agent 首次输出不是 JSON，重试一次")
            messages = messages + [
                {"role": "assistant", "content": reply[:2000]},
                {"role": "user", "content": _REPAIR_PROMPT},
            ]
            reply = await _collect(resolved.adapter, resolved.model_name, messages, 0.1, max_tokens)
            raw = extract_json(reply)
    except Exception as e:  # noqa: BLE001
        logger.warning("画布 Agent 调用失败：%s", e)
        return Draft(status=STATUS_UPSTREAM_ERROR, warnings=[str(e) or type(e).__name__],
                     next_action=_STATUS_NEXT_ACTION[STATUS_UPSTREAM_ERROR])
    finally:
        await resolved.adapter.close()

    if raw is None:
        return Draft(
            status=STATUS_UNPARSABLE,
            warnings=[
                "模型没有按要求给出 JSON，无法生成草稿。",
                f"它实际回的开头是：{(reply or '（空）')[:120]}",
            ],
            next_action=_STATUS_NEXT_ACTION[STATUS_UNPARSABLE],
        )

    summary, nodes, edges, notes = repair(raw, options, styles)
    if not nodes:
        return Draft(
            status=STATUS_UNPARSABLE,
            summary=summary,
            notes=notes,
            warnings=["模型给的节点类型一个都不认识，没有可用草稿。"],
            next_action=_STATUS_NEXT_ACTION[STATUS_UNPARSABLE],
        )

    nodes = layout(nodes, edges)
    requires = required_modalities(nodes)
    available = await _available_modalities(db)
    missing = [m for m in requires if m not in available]

    warnings: list[str] = []
    if missing:
        label = {"text": "文本", "image": "图片", "video": "视频"}
        names = "与".join(label.get(m, m) for m in missing)
        warnings.append(f"这份草稿需要{names}模型，你还没接入；可以先把前面的节点跑起来，缺的那一步等接入后再跑。")

    batch_nodes = sum(1 for n in nodes if n["kind"] in ("assetImage", "storyboardImage"))
    if batch_nodes:
        warnings.append(
            f"草稿里有 {batch_nodes} 个节点是「一行/一镜一个任务」的批量出图节点，"
            "运行整图会一次派出多个任务，先确认张数与额度。"
        )
    if "video" in requires:
        warnings.append("草稿包含视频节点：出片慢且贵，建议先跑通前面的文字与图片节点。")

    return Draft(
        status=STATUS_OK,
        summary=summary,
        nodes=nodes,
        edges=edges,
        notes=notes,
        warnings=warnings,
        requires=requires,
        missing=missing,
    )

