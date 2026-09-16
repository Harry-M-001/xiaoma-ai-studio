"""分镜表解析：把 storyboard 节点产出的 Markdown 镜头表拆成一个个镜头。

分镜 agent 的输出格式（由 agent_prompts 里的 storyboard 提示词约束）：

    ### 镜头3 | 中景 | 缓慢推近 | 4s
    - 画面：暮光闪闪站在花园中央，缓缓抬头（动作幅度小、连续）
    - 台词：（无）
    - 情绪外化：手指无意识绞紧衣角
    - 首帧提示词：Twilight Sparkle standing in a crystal garden, slow tilt up, ...
    - 约束：无字幕、无 Logo、无人物变形

解析刻意做得宽容（模型很少每次都写得分毫不差）：
- 标题行支持 `### 镜头N`、`## 镜头 N`、整行加粗 `**镜头N | ...**`；
- 字段行支持 `- 画面：`、`* 画面:`、`**画面**：`、`画面：`；
- 字段名做同义词归一；缺字段不报错，缺「首帧提示词」时退回用「画面」生成；
- 「镜头清单 / 大纲 / 目录」这类汇总标题会被跳过，不会被当成镜头。

只有「一行镜头都解不出来」才返回空，交上层报错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 一次最多出多少个镜头的分镜图（防止一个 60 镜的分镜表把额度烧光）
MAX_SHOTS = 24

# 汇总性标题：出现在 plan 与正文里，不能当成镜头
_SKIP_TITLE_WORDS = ("清单", "大纲", "目录", "表格", "总览", "summary", "资产")

# 标题行：`## 镜头3 | ...` / `**镜头3 | ...**`
_ATX_HEADING = re.compile(r"^\s{0,3}(#{2,4})\s*(.+?)\s*#*\s*$")
_BOLD_HEADING = re.compile(r"^\s*\*\*(.+?)\*\*\s*[:：]?\s*$")

# 字段行：`- 画面：xxx` / `* 画面: xxx` / `**画面**：xxx` / `画面：xxx`
_FIELD_LINE = re.compile(r"^\s*(?:[-*+]\s*)?\*{0,2}([^：:*]{1,12})\*{0,2}\s*[:：]\s*(.*)$")

# 镜号：`镜头3` / `镜3` / `Shot 3` / `SH03`
_SHOT_NO = re.compile(r"(?:镜头|镜|shot)\s*([0-9]{1,3}[A-Za-z\-]*)", re.I)

# 「标题里有没有镜头/镜号」的判定
_SHOT_TITLE = re.compile(r"(镜头|镜号|\bshot\b)", re.I)

# 字段名归一：五花八门的写法 → 内部键
_FIELD_ALIASES = {
    "画面": "scene", "画面描述": "scene", "内容": "scene", "镜头内容": "scene",
    "描述": "scene", "scene": "scene", "visual": "scene",
    "首帧提示词": "first_frame", "首帧": "first_frame", "英文提示词": "first_frame",
    "生图提示词": "first_frame", "image prompt": "first_frame", "prompt": "first_frame",
    "台词": "dialogue", "对白": "dialogue", "dialogue": "dialogue",
    "情绪外化": "emotion", "情绪": "emotion", "emotion": "emotion",
    "约束": "constraints", "限制": "constraints", "注意事项": "constraints",
}

# 备选字段名（按优先级取第一个非空的）
_FIRST_FRAME_KEYS = ("first_frame",)

_TRIM = " \t`*_\"'“”"


@dataclass(frozen=True)
class Shot:
    """一个镜头。"""

    no: str
    heading: str
    size: str = ""
    move: str = ""
    duration: str = ""
    scene: str = ""
    dialogue: str = ""
    emotion: str = ""
    first_frame: str = ""
    constraints: str = ""

    @property
    def label(self) -> str:
        """日志/任务里的显示名。"""
        bits = [b for b in (self.size, self.move, f"{self.duration}") if b]
        return f"镜头{self.no}" + (f"（{' · '.join(bits)}）" if bits else "")

    @property
    def scan_text(self) -> str:
        """角色提及注入的扫描文本：整块镜头内容都算，中文角色名通常在「画面」里。"""
        return " ".join(
            p for p in (self.heading, self.scene, self.dialogue, self.emotion, self.first_frame) if p
        )

    @property
    def image_prompt(self) -> str:
        """生图提示词：优先英文首帧提示词，没有就退回画面描述。"""
        return self.first_frame or self.scene


def _clean(text: str) -> str:
    return (text or "").strip().strip(_TRIM).strip()


def _split_heading(heading: str) -> tuple[str, str, str, str]:
    """`镜头3 | 中景 | 缓慢推近 | 4s` → (镜号, 景别, 运镜, 时长)。"""
    parts = [_clean(p) for p in heading.split("|")]
    title = parts[0] if parts else ""
    m = _SHOT_NO.search(title)
    no = m.group(1) if m else ""
    size = parts[1] if len(parts) > 1 else ""
    move = parts[2] if len(parts) > 2 else ""
    duration = parts[3] if len(parts) > 3 else ""
    # 时长里常见「约 4 秒」「4s」，只留数字+单位
    duration = re.sub(r"^约\s*", "", duration).strip()
    return no, size, move, duration


def _heading_of(line: str) -> str | None:
    m = _ATX_HEADING.match(line)
    if m:
        return _clean(m.group(2))
    m = _BOLD_HEADING.match(line)
    if m:
        return _clean(m.group(1)) or None
    return None


def _blocks(text: str) -> list[tuple[str, list[str]]]:
    """按标题切块；返回 [(标题, 块内行)]。"""
    out: list[tuple[str, list[str]]] = []
    current: str | None = None
    body: list[str] = []
    for raw in (text or "").splitlines():
        heading = _heading_of(raw)
        if heading is not None:
            if current is not None:
                out.append((current, body))
            current = heading
            body = []
            continue
        if current is not None:
            body.append(raw)
    if current is not None:
        out.append((current, body))
    return out


def _fields(lines: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            continue
        m = _FIELD_LINE.match(line)
        if not m:
            continue
        name = _clean(m.group(1)).lower()
        value = _clean(m.group(2))
        key = _FIELD_ALIASES.get(name) or _FIELD_ALIASES.get(_clean(m.group(1)))
        if key and value and key not in found:
            found[key] = value
    return found


def parse_storyboard(text: str) -> list[Shot]:
    """解析分镜表；按出现顺序返回，最多 MAX_SHOTS 个。"""
    blocks = _blocks(text)
    if not blocks:
        return []

    shot_blocks = [(h, b) for h, b in blocks if _SHOT_TITLE.search(h)]
    # 一个「镜头」标题都没有时退一步：把每个标题块都当一个镜头（模型换了别的写法）
    chosen = shot_blocks or [(h, b) for h, b in blocks]

    shots: list[Shot] = []
    for heading, body in chosen:
        if any(w in heading for w in _SKIP_TITLE_WORDS):
            continue
        fields = _fields(body)
        scene = fields.get("scene", "")
        first_frame = next((fields[k] for k in _FIRST_FRAME_KEYS if fields.get(k)), "")
        if not scene and not first_frame:
            continue
        no, size, move, duration = _split_heading(heading)
        if not no:
            no = str(len(shots) + 1)
        shots.append(
            Shot(
                no=no,
                heading=heading,
                size=size,
                move=move,
                duration=duration,
                scene=scene,
                dialogue=fields.get("dialogue", ""),
                emotion=fields.get("emotion", ""),
                first_frame=first_frame,
                constraints=fields.get("constraints", ""),
            )
        )
        if len(shots) >= MAX_SHOTS:
            break
    return shots


def limit_shots(shots: list[Shot], limit: int) -> list[Shot]:
    """按节点上的「生成镜数」截断；limit <= 0 表示全部。"""
    if limit <= 0:
        return list(shots)
    return list(shots[:limit])
