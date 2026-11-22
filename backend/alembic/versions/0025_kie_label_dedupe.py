"""模型下拉里的服务名不再重复。

`0021_model_catalog` 给 KIE 的模型 label 都带了一个「（KIE）」的厂商尾缀，
而前端渲染下拉时本来就会追加服务名（`{label}（{service_name}）`），
于是变成「Seedance 2.5（KIE）（KIE（图片/视频·异步））」——同一件事说了两遍。

厂商归属这件事**只在服务名上写一次**就够了：模型 label 保持「Seedance 2.5」，
落到界面上自然就是「Seedance 2.5（KIE（图片/视频·异步））」。

只去掉 label **结尾**的「（KIE）」；用户自己起的名字（不以它结尾）不动。

Revision ID: 0025_kie_label_dedupe
Revises: 0024_kie_legacy_aliases
Create Date: 2026-11-20
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa

revision = "0025_kie_label_dedupe"
down_revision = "0024_kie_legacy_aliases"
branch_labels = None
depends_on = None

SUFFIX = "（KIE）"

#: 当初带尾缀的那几条（downgrade 时按名字加回去，避免给用户自建模型乱加）
KIE_LABELS = {
    "gemini-3-8-flash-openai": "Gemini 3.8 Flash",
    "seedream/5-flash-text-to-image": "Seedream 5.0 Flash 文生图",
    "seedream/5-flash-image-to-image": "Seedream 5.0 Flash 参考图",
    "seedream/5-pro-text-to-image": "Seedream 5.0 Pro 文生图",
    "seedream/5-lite-text-to-image": "Seedream 5.0 Lite 文生图",
    "seedream/4.5-text-to-image": "Seedream 4.5 文生图",
    "nano-banana-2": "Nano Banana 2",
    "gpt-image-2-text-to-image": "GPT Image 2 文生图",
    "gpt-image-2-image-to-image": "GPT Image 2 参考图",
    "bytedance/seedance-2-5": "Seedance 2.5",
    "bytedance/seedance-2": "Seedance 2.0",
    "kling-4.0/keyframe-to-video": "Kling 4.0 首帧生视频",
    "wan/3-0-video": "Wan 3.0",
}


def _load(raw: str) -> list[dict]:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict)]


def _dump(items: list[dict]) -> str:
    return json.dumps(items, ensure_ascii=False)


def _strip(data: list[dict]) -> tuple[list[dict], bool]:
    changed = False
    out = []
    for item in data:
        label = str(item.get("label") or "")
        if label.endswith(SUFFIX):
            out.append({**item, "label": label[: -len(SUFFIX)]})
            changed = True
        else:
            out.append(item)
    return out, changed


def _restore(data: list[dict]) -> tuple[list[dict], bool]:
    changed = False
    out = []
    for item in data:
        name = str(item.get("name") or "")
        label = str(item.get("label") or "")
        want = KIE_LABELS.get(name)
        if want and label == want:
            out.append({**item, "label": want + SUFFIX})
            changed = True
        else:
            out.append(item)
    return out, changed


def _run(transform):
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT key, models_json FROM provider_presets WHERE key IN ('kie', 'kie_image')")
    ).fetchall()
    for row in rows:
        data, changed = transform(_load(row.models_json))
        if changed:
            conn.execute(
                sa.text("UPDATE provider_presets SET models_json = :m WHERE key = :k"),
                {"m": _dump(data), "k": row.key},
            )

    rows = conn.execute(
        sa.text("SELECT id, kind, base_url, models_json FROM provider_services")
    ).fetchall()
    for row in rows:
        is_kie_async = row.kind == "kie" and "api.kie.ai" in (row.base_url or "").lower()
        is_kie_chat = row.kind == "openai" and "api.kie.ai" in (row.base_url or "").lower()
        if not (is_kie_async or is_kie_chat):
            continue
        data, changed = transform(_load(row.models_json))
        if changed:
            conn.execute(
                sa.text("UPDATE provider_services SET models_json = :m WHERE id = :i"),
                {"m": _dump(data), "i": row.id},
            )


def upgrade() -> None:
    _run(_strip)


def downgrade() -> None:
    _run(_restore)
