"""创作参数校验：参数档位一律以配置表为准，代码里不再写死清单。

校验通过返回规范化后的值；不在允许集合内则给出可读错误，
错误里会带出当前允许的档位，方便用户直接改配置或换选项。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ParamOption
from app.providers.base import AdapterError


async def allowed_values(db: AsyncSession, kind: str) -> list[str]:
    rows = await db.execute(
        select(ParamOption)
        .where(ParamOption.kind == kind, ParamOption.enabled.is_(True))
        .order_by(ParamOption.sort_order, ParamOption.id)
    )
    return [r.value for r in rows.scalars().all()]


async def validate_option(db: AsyncSession, kind: str, value: object, label: str) -> str:
    """校验参数档位；该参数类型未配置任何选项时放行（交由上游判断）。"""
    options = await allowed_values(db, kind)
    if not options:
        return str(value)
    text = str(value)
    if text not in options:
        raise AdapterError(
            f"{label}「{text}」不在允许的档位内，可选：{'、'.join(options)}"
            "（可在「系统设置 → 参数选项」中调整）"
        )
    return text


async def validate_int_option(db: AsyncSession, kind: str, value: int, label: str) -> int:
    text = await validate_option(db, kind, value, label)
    try:
        return int(text)
    except ValueError as e:
        raise AdapterError(f"{label}需要是整数") from e


def validate_duration(value: object, low: int, high: int, label: str = "视频时长") -> int:
    """视频时长是**连续值**，按模型能接受的区间校验（不再走参数档位表）。

    只认 5/10 会让 Seedance 2.5 / Wan 3.0 的 30 秒形同虚设；而完全不校验
    又会把「3 秒」这种低于所有模型下限的值发上去，白等一轮 400。
    """
    try:
        seconds = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise AdapterError(f"{label}需要是整数") from e
    if seconds < low or seconds > high:
        raise AdapterError(f"{label}需在 {low}–{high} 秒之间（当前模型的上限）")
    return seconds
