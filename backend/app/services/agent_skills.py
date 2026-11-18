"""Agent 技能：`SKILL.md` 的**渐进披露**（2026 年主流做法）。

三层的取舍（照 Anthropic 那套，但只取我们付得起的两层）：

1. **第一层常驻**：只有 `name + description`，写进系统提示词——让模型**知道有这件事**，
   但不占多少 token；
2. **第二层按需**：正文由 `load_skill` 工具按下发（模型判断相关时才取）；
3. **第三层（附件/脚本）不做**：我们的技能没有可执行附件，硬造一层只会增加出错面。

文件放 `app/skills/*.md`，frontmatter 用最朴素的 `key: value`（不引 YAML 依赖）。
解析失败就**跳过并在日志里点名**——一个坏文件不该让整个 Agent 起不来。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("xiaoma.agent")

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

#: 单个技能正文进上下文的上限（正文很长时截断，并说清被截了）
MAX_BODY_CHARS = 6000


def _parse(text: str) -> tuple[dict[str, str], str]:
    """拆 frontmatter 与正文。只认 `---` 包起来的那一段 `key: value`。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip().lower()] = value.strip()
    return meta, parts[2].strip()


def load_all() -> list[dict]:
    """读全部技能。每次现读：文件只有几 KB，而热改技能是常用操作（不想重启）。"""
    items: list[dict] = []
    if not SKILLS_DIR.exists():
        return items
    for path in sorted(SKILLS_DIR.glob("*.md")):
        try:
            meta, body = _parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("技能 %s 读不出来：%s", path.name, e)
            continue
        name = (meta.get("name") or "").strip()
        desc = (meta.get("description") or "").strip()
        if not name or not desc:
            logger.warning("技能 %s 缺 name/description，已跳过", path.name)
            continue
        items.append({
            "key": path.stem,
            "name": name,
            "description": desc,
            "body": body,
        })
    return items


def metadata() -> list[dict]:
    """第一层：只给名字与描述（常驻系统提示词）。"""
    return [
        {"key": s["key"], "name": s["name"], "description": s["description"]}
        for s in load_all()
    ]


def get(key: str) -> dict | None:
    """第二层：按需取正文。"""
    wanted = str(key or "").strip().lower()
    for s in load_all():
        if s["key"].lower() == wanted or s["name"] == key:
            return s
    return None


def prompt_block() -> str:
    """写进系统提示词的那一小段。空列表时返回空串（别在提示词里留一个空标题）。"""
    items = metadata()
    if not items:
        return ""
    lines = ["可用技能（先用 load_skill 取正文，再照它做）："]
    lines += [f"- {s['key']}｜{s['name']}：{s['description']}" for s in items]
    return "\n".join(lines)
