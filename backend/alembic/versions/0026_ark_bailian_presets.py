"""方舟 / 百炼换成 2026-11 的最新模型。

服务商预设的种子是「不存在才插入」，所以已经在用的人不会自动看到新模型：
本地库里 `ark` / `qwen` 两行还是 2024 年的旧清单。这里把这两行更新到最新，
并给**已经建好的**方舟 / 通义千问服务补上缺失的模型——只追加、不删除，
用户自己加的模型和已经勾过的旧模型都保留。

百炼的图片/视频服务是新增预设（`bailian`），由启动时的种子插入，这里不重复造。

Revision ID: 0026_ark_bailian_presets
Revises: 0025_kie_label_dedupe
Create Date: 2026-11-21
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa

revision = "0026_ark_bailian_presets"
down_revision = "0025_kie_label_dedupe"
branch_labels = None
depends_on = None

ARK_HINT = (
    "豆包 Seed 2.1 文本 / Seedream 5.5 图片 / Seedance 2.5 视频；"
    "模型名填具体版本 ID，版本过期后到方舟控制台复制新的。"
)
ARK_MODELS = [
    {"name": "doubao-seed-2-1-pro-260915", "modality": "text", "label": "豆包 Seed 2.1 Pro"},
    {"name": "doubao-seed-2-1-lite-260915", "modality": "text", "label": "豆包 Seed 2.1 Lite"},
    {"name": "doubao-seedream-5-5-261028", "modality": "image", "label": "豆包 Seedream 5.5"},
    {"name": "doubao-seedream-5-0-pro-260628", "modality": "image", "label": "豆包 Seedream 5.0 Pro"},
    {"name": "doubao-seedream-5-0-lite-260128", "modality": "image", "label": "豆包 Seedream 5.0 Lite"},
    {"name": "doubao-seedance-2-5-260628", "modality": "video", "label": "豆包 Seedance 2.5"},
    {"name": "doubao-seedance-2-5-flash-261115", "modality": "video", "label": "豆包 Seedance 2.5 Flash"},
    {"name": "doubao-seedance-2-0-260128", "modality": "video", "label": "豆包 Seedance 2.0"},
    {"name": "doubao-seedance-1-0-pro-250528", "modality": "video", "label": "豆包 Seedance 1.0 Pro（旧版）"},
]

QWEN_HINT = "阿里云百炼的 OpenAI 兼容模式；最新文本模型 qwen4-max / qwen3.8-max / qwen3.8-flash"
QWEN_MODELS = [
    {"name": "qwen4-max", "modality": "text", "label": "Qwen4 Max"},
    {"name": "qwen3.8-max", "modality": "text", "label": "Qwen3.8 Max"},
    {"name": "qwen3.8-flash", "modality": "text", "label": "Qwen3.8 Flash"},
    {"name": "qwen3.7-plus", "modality": "text", "label": "Qwen3.7 Plus"},
]

#: downgrade 用：回到改之前的那两行
OLD_ARK = [
    {"name": "doubao-seedream-4-0-250828", "modality": "image", "label": "豆包 Seedream 4.0"},
    {"name": "doubao-seedance-1-0-pro-250528", "modality": "video", "label": "豆包 Seedance 1.0 Pro"},
]
OLD_ARK_HINT = "豆包 Seedream 图片 / Seedance 视频，模型名填具体版本 ID"
OLD_QWEN = [
    {"name": "qwen-plus", "modality": "text", "label": "Qwen Plus"},
    {"name": "qwen-turbo", "modality": "text", "label": "Qwen Turbo"},
    {"name": "qwen-max", "modality": "text", "label": "Qwen Max"},
]
OLD_QWEN_HINT = "阿里云百炼的 OpenAI 兼容模式"


def _load(raw: str) -> list[dict]:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict)]


def _dump(items: list[dict]) -> str:
    return json.dumps(items, ensure_ascii=False)


def _merge(existing: list[dict], wanted: list[dict]) -> tuple[list[dict], bool]:
    """按 name 追加缺失项；已有的一律不动（用户可能改过 label / 删过模型）。"""
    seen = {str(m.get("name") or "") for m in existing}
    added = [m for m in wanted if m["name"] not in seen]
    return existing + added, bool(added)


def _set_preset(conn, key: str, models: list[dict], hint: str) -> None:
    conn.execute(
        sa.text(
            "UPDATE provider_presets SET models_json = :m, hint = :h WHERE key = :k"
        ),
        {"m": _dump(models), "h": hint, "k": key},
    )


def _add_missing_service_models(conn, models: list[dict], *, kind: str, url_like: str) -> None:
    rows = conn.execute(
        sa.text(
            "SELECT id, base_url, models_json FROM provider_services WHERE kind = :kind"
        ),
        {"kind": kind},
    ).fetchall()
    for row in rows:
        if url_like not in (row.base_url or "").lower():
            continue
        merged, changed = _merge(_load(row.models_json), models)
        if changed:
            conn.execute(
                sa.text("UPDATE provider_services SET models_json = :m WHERE id = :i"),
                {"m": _dump(merged), "i": row.id},
            )


def upgrade() -> None:
    conn = op.get_bind()
    _set_preset(conn, "ark", ARK_MODELS, ARK_HINT)
    _set_preset(conn, "qwen", QWEN_MODELS, QWEN_HINT)
    _add_missing_service_models(conn, ARK_MODELS, kind="ark", url_like="volces.com")
    _add_missing_service_models(conn, QWEN_MODELS, kind="openai", url_like="dashscope.aliyuncs.com")


def downgrade() -> None:
    conn = op.get_bind()
    _set_preset(conn, "ark", OLD_ARK, OLD_ARK_HINT)
    _set_preset(conn, "qwen", OLD_QWEN, OLD_QWEN_HINT)
