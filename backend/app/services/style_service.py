"""导演风格库：把一张风格卡翻译成三个注入点的文本片段。

合规边界**写死在函数上**，不给调用方越界的机会：

- `agent_block()`    → 给 LLM（创作 Agent），中文，**可以出现导演名**（帮模型理解技法）
- `image_suffix()`   → 拼进生图提示词，英文，**只有技法片段与约束词**
- `video_suffix()`   → 拼进生视频提示词，英文，**只有技法片段与约束词**

所有函数对「风格不存在 / 已停用 / 片段为空」一律静默降级：
拿不到风格就等于没选风格，绝不让一个配置问题把整条链打断。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DirectorStyle


@dataclass(frozen=True)
class StyleCard:
    """一张风格卡（三个注入点的内容 + 展示名）。"""

    key: str
    name: str
    agent_prompt: str
    image_prompt: str
    video_prompt: str
    negative_prompt: str


def to_card(row: DirectorStyle) -> StyleCard:
    return StyleCard(
        key=row.key,
        name=row.name,
        agent_prompt=(row.agent_prompt or "").strip(),
        image_prompt=(row.image_prompt or "").strip().strip(","),
        video_prompt=(row.video_prompt or "").strip().strip(","),
        negative_prompt=(row.negative_prompt or "").strip().strip(","),
    )


async def load_card(db: AsyncSession, key: str) -> StyleCard | None:
    """按 key 取一张启用的风格卡；不存在或已停用返回 None。"""
    wanted = (key or "").strip()
    if not wanted:
        return None
    rows = await db.execute(
        select(DirectorStyle).where(DirectorStyle.key == wanted, DirectorStyle.enabled.is_(True))
    )
    row = rows.scalars().first()
    return to_card(row) if row is not None else None


async def load_cards(db: AsyncSession) -> list[StyleCard]:
    rows = await db.execute(
        select(DirectorStyle)
        .where(DirectorStyle.enabled.is_(True))
        .order_by(DirectorStyle.sort_order, DirectorStyle.id)
    )
    return [to_card(r) for r in rows.scalars().all()]


# ---- 三个注入点 ----


def agent_block(card: StyleCard | None) -> str:
    """给创作 Agent 的风格要求（追加到系统提示词末尾）。可含导演名。"""
    if card is None or not card.agent_prompt:
        return ""
    return (
        f"【本片风格：{card.name}】\n"
        f"{card.agent_prompt}\n"
        f"以上风格要求是硬性约束：景别偏好、运镜方式、光影与色彩走向都必须体现出来。"
    )


def _suffix(card: StyleCard, fragment: str) -> str:
    parts = [p for p in (fragment, card.negative_prompt) if p]
    return ", ".join(parts)


def image_suffix(card: StyleCard | None) -> str:
    """拼进生图提示词的片段（技法 + 约束，不含导演名）。"""
    if card is None:
        return ""
    return _suffix(card, card.image_prompt)


def video_suffix(card: StyleCard | None) -> str:
    """拼进生视频提示词的片段（技法 + 约束，不含导演名）。"""
    if card is None:
        return ""
    return _suffix(card, card.video_prompt)


def append_style(prompt: str, suffix: str) -> str:
    """把片段追加到提示词末尾；已包含过就不重复追加。"""
    base = (prompt or "").strip().strip(",")
    add = (suffix or "").strip().strip(",")
    if not add:
        return base
    if add in base:
        return base
    return f"{base}, {add}" if base else add
