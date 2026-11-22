"""KIE 旧模型名保留为隐藏别名。

`0021_model_catalog` 把 KIE 的模型清单整体换成了新一代（Seedream 5.x / Seedance 2.x），
但**老画布节点与老任务的 model_key 还指着旧名**（`bytedance/seedream-v4-*`、
`kling/v2-5-*`）。清单里没有这个名字，`resolve_model` 就会报「模型已从服务配置中移除」，
重跑旧节点直接失败——目录升级不该把已经做好的作品弄坏。

做法与 DeepSeek 那次一致：**旧名留成 `hidden` 的兼容别名**，下拉里看不到，
解析仍然认。不重写历史数据（任务快照是当时的记录，改了就失真）。

Revision ID: 0024_kie_legacy_aliases
Revises: 0023_deepseek_v41_flash
Create Date: 2026-11-20
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa

revision = "0024_kie_legacy_aliases"
down_revision = "0023_deepseek_v41_flash"
branch_labels = None
depends_on = None

LEGACY = [
    {"name": "bytedance/seedream-v4-text-to-image", "modality": "image",
     "label": "Seedream 4.0 文生图（兼容别名）"},
    {"name": "bytedance/seedream-v4-edit", "modality": "image",
     "label": "Seedream 4.0 参考图编辑（兼容别名）"},
    {"name": "kling/v2-5-turbo-image-to-video-pro", "modality": "video",
     "label": "Kling 2.5 Turbo 首帧生视频（兼容别名）"},
    {"name": "kling/v2-5-turbo-text-to-video-pro", "modality": "video",
     "label": "Kling 2.5 Turbo 文生视频（兼容别名）"},
]


def _load(raw: str) -> list[dict]:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict)]


def _dump(items: list[dict]) -> str:
    return json.dumps(items, ensure_ascii=False)


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, kind, base_url, models_json FROM provider_services")
    ).fetchall()
    for row in rows:
        # 只认异步的 KIE（kind=kie）；KIE 的 Gemini 聊天服务是 openai，不能沾这批别名
        if row.kind != "kie" or "api.kie.ai" not in (row.base_url or "").lower():
            continue
        data = _load(row.models_json)
        have = {str(d.get("name") or "") for d in data}
        changed = False
        for item in LEGACY:
            if item["name"] not in have:
                data.append({**item, "hidden": True})
                changed = True
        if changed:
            conn.execute(
                sa.text("UPDATE provider_services SET models_json = :m WHERE id = :i"),
                {"m": _dump(data), "i": row.id},
            )


def downgrade() -> None:
    conn = op.get_bind()
    names = {d["name"] for d in LEGACY}
    rows = conn.execute(
        sa.text("SELECT id, kind, base_url, models_json FROM provider_services")
    ).fetchall()
    for row in rows:
        if row.kind != "kie" or "api.kie.ai" not in (row.base_url or "").lower():
            continue
        data = [d for d in _load(row.models_json) if str(d.get("name") or "") not in names]
        conn.execute(
            sa.text("UPDATE provider_services SET models_json = :m WHERE id = :i"),
            {"m": _dump(data), "i": row.id},
        )
