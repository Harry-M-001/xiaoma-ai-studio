"""资产链：解析资产表（Markdown 表格）+ 组装设定图提示词。

资产链只做两件事，都不碰「内部 JSON 结构」这个雷区：

1. `assetSheet` 节点让 LLM 把「谁 / 在哪 / 用什么」写成一张 Markdown 表格
   （内容层，用户可直接手改，改完不用重新生成）；
2. `assetImage` 节点读这张表，逐行生成设定图，并把「资产名 → 图片」落进资产库，
   下游节点只要在提示词里提到资产名，就能自动挂上对应参考图（角色提及注入）。

表格约定（由 agent_prompts 里的 assetSheet 提示词约束）：

    | 中文名 | 类型 | 英文视觉描述 | 出现场次 |
    | --- | --- | --- | --- |
    | 暮光闪闪 | 角色 | Twilight Sparkle, purple unicorn, ... | S01,S03 |

解析刻意做得宽容：表头可有可无、列数 3-4 列都收、坏行跳过不报错，
只有「一行都没解出来」才让上层报错——模型偶尔不听话不应该让整条链崩掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 允许的资产类型；其余写法一律归一到这三类
ASSET_CATEGORIES = ("角色", "场景", "道具")

_CATEGORY_ALIASES = {
    "角色": "角色", "人物": "角色", "主角": "角色", "配角": "角色", "character": "角色",
    "场景": "场景", "地点": "场景", "环境": "场景", "location": "场景", "scene": "场景",
    "道具": "道具", "物品": "道具", "物件": "道具", "prop": "道具", "object": "道具",
}

# 类型 → 生成范围开关（前端浮框上的档位）
CATEGORY_SCOPES = {"角色": "character", "场景": "scene", "道具": "prop"}

# 单次最多生成多少行资产的设定图：防止模型写了一百行把额度烧光
MAX_ASSET_ROWS = 24

# 表头关键字：整行命中就跳过（不同模型写的中文表头五花八门）
_HEADER_WORDS = {"中文名", "名称", "资产名", "名字", "角色名", "name", "asset", "资产"}

# `- 暮光闪闪（角色）：purple unicorn ...` 这类非表格兜底
_BULLET_RE = re.compile(r"^\s*[-*+]\s*(?:\*\*)?(.+?)(?:\*\*)?\s*[（(]([^）)]{1,10})[）)]\s*[:：]?\s*(.+)$")

# 英文视觉描述里不该出现的脏东西（有些模型会带上 markdown 粗体或反引号）
_TRIM_CHARS = " \t`*_\"'“”"


@dataclass(frozen=True)
class AssetRow:
    """资产表的一行。"""

    name: str
    category: str
    visual: str
    scenes: str = ""

    @property
    def scope(self) -> str:
        return CATEGORY_SCOPES.get(self.category, "")


def _clean(cell: str) -> str:
    text = (cell or "").strip()
    if len(text) >= 4 and text.startswith("**") and text.endswith("**"):
        text = text[2:-2]
    text = text.replace("\\|", "|")
    return text.strip(_TRIM_CHARS).strip()


def normalize_category(raw: str) -> str:
    """把五花八门的类型写法归一；认不出来就返回原词（不丢信息）。"""
    key = _clean(raw).lower()
    if key in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[key]
    for alias, cat in _CATEGORY_ALIASES.items():
        if alias in key:
            return cat
    return _clean(raw)


def looks_like_separator(cells: list[str]) -> bool:
    """`| --- | :--: |` 这类分隔行。"""
    if not cells:
        return False
    for c in cells:
        body = c.replace("-", "").replace(":", "").strip()
        if body:
            return False
    return any("-" in c for c in cells)


def _row_from_cells(cells: list[str]) -> AssetRow | None:
    if looks_like_separator(cells):
        return None
    name = _clean(cells[0])
    if not name or name.lower() in _HEADER_WORDS or name in _HEADER_WORDS:
        return None
    category = normalize_category(cells[1]) if len(cells) > 1 else ""
    visual = _clean(cells[2]) if len(cells) > 2 else ""
    scenes = _clean(cells[3]) if len(cells) > 3 else ""
    if not visual:
        return None
    return AssetRow(name=name[:80], category=category, visual=visual[:800], scenes=scenes[:120])


def _iter_rows(text: str):
    """先走表格行；表格行一条都没有时，再退到「- 名称（类型）：描述」列表行。"""
    table_hits = 0
    bullets: list[AssetRow] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("|"):
            cells = [_clean(c) for c in line.strip("|").split("|")]
            row = _row_from_cells(cells)
            if row:
                table_hits += 1
                yield row
            continue
        m = _BULLET_RE.match(line)
        if m:
            visual = _clean(m.group(3))
            if visual:
                bullets.append(
                    AssetRow(
                        name=_clean(m.group(1))[:80],
                        category=normalize_category(m.group(2)),
                        visual=visual[:800],
                    )
                )
    if table_hits == 0:
        yield from bullets


def parse_asset_table(text: str) -> list[AssetRow]:
    """解析资产表，按出现顺序返回、同名去重，最多 MAX_ASSET_ROWS 行。"""
    rows: list[AssetRow] = []
    seen: set[str] = set()
    for row in _iter_rows(text):
        if row.name in seen:
            continue
        seen.add(row.name)
        rows.append(row)
        if len(rows) >= MAX_ASSET_ROWS:
            break
    return rows


def filter_rows(rows: list[AssetRow], scope: str) -> list[AssetRow]:
    """按生成范围过滤（"" = 全部）。"""
    key = (scope or "").strip()
    if not key or key == "all":
        return list(rows)
    return [r for r in rows if r.scope == key]


# ---- 设定图提示词 ----

_CHARACTER_TEMPLATE = (
    "character reference sheet of {visual}, three views in one image "
    "(front view, side view, back view) plus a face close-up, full body, "
    "standing straight, plain flat light grey background, even studio lighting, "
    "consistent costume and hairstyle across all views, high detail"
)
_SCENE_TEMPLATE = (
    "establishing wide shot of {visual}, empty scene with no people, "
    "cinematic composition, natural lighting, rich environmental detail, high detail"
)
_PROP_TEMPLATE = (
    "product reference photo of {visual}, isolated on a plain neutral background, "
    "single object centered with several angles shown in one image, even studio lighting, high detail"
)
_GENERIC_TEMPLATE = "{visual}, reference sheet, plain clean background, consistent design, high detail"

_TEMPLATES = {"角色": _CHARACTER_TEMPLATE, "场景": _SCENE_TEMPLATE, "道具": _PROP_TEMPLATE}

# 统一追加的负面约束（各家用词不同，写全一点最省事）
NEGATIVE_SUFFIX = "no text, no caption, no watermark, no logo, no extra characters"


def build_reference_prompt(row: AssetRow, extra: str = "", keep_negative: bool = True) -> str:
    """把一行资产拼成生图提示词；extra 是节点上的「补充要求」（统一画风词等）。"""
    template = _TEMPLATES.get(row.category, _GENERIC_TEMPLATE)
    head = template.format(visual=row.visual.strip())
    parts = [head]
    if extra.strip():
        parts.append(extra.strip())
    if keep_negative:
        parts.append(NEGATIVE_SUFFIX)
    return ", ".join(p.strip().strip(",") for p in parts if p.strip())
