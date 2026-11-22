"""模型目录：DeepSeek V4 与 KIE 新一代图片/视频。

2026-11 按官方文档核对后的两处变化：

- **DeepSeek**：`deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 弃用，
  新模型是 `deepseek-v4-flash` / `deepseek-v4-pro`（1M 上下文，支持工具调用）。
- **KIE**：图片与视频都换了一代入参形状（按家族分派见 `providers/kie.py`），
  预设清单同步到 Seedream 5.x / 4.5、Nano Banana 2、GPT Image 2、
  Seedance 2.x、Kling 4.0、Wan 3.0。

**老服务只补不改**：出厂清单原样保留的话整体换新；用户改过的只在末尾追加缺的
模型，不删他手工加的那几条，也不动 Key。

Revision ID: 0021_model_catalog
Revises: 0020_agent_auto_approve
Create Date: 2026-11-19
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa

revision = "0021_model_catalog"
down_revision = "0020_agent_auto_approve"
branch_labels = None
depends_on = None


OLD_DEEPSEEK = [
    {"name": "deepseek-chat", "modality": "text", "label": "DeepSeek Chat"},
    {"name": "deepseek-reasoner", "modality": "text", "label": "DeepSeek R1"},
]
NEW_DEEPSEEK = [
    {"name": "deepseek-v4-flash", "modality": "text", "label": "DeepSeek V4 Flash"},
    {"name": "deepseek-v4-pro", "modality": "text", "label": "DeepSeek V4 Pro"},
]
DEEPSEEK_HINT = (
    "V4 全系 1M 上下文；v4-flash 便宜、v4-pro 更强。"
    "旧名 deepseek-chat / deepseek-reasoner 已于 2026-07-24 弃用。"
)

OLD_KIE = [
    {"name": "bytedance/seedream-v4-text-to-image", "modality": "image",
     "label": "Seedream 4.0 文生图（KIE）"},
    {"name": "bytedance/seedream-v4-edit", "modality": "image",
     "label": "Seedream 4.0 参考图编辑（KIE）"},
    {"name": "kling/v2-5-turbo-image-to-video-pro", "modality": "video",
     "label": "Kling 2.5 Turbo 首帧生视频（KIE）"},
    {"name": "kling/v2-5-turbo-text-to-video-pro", "modality": "video",
     "label": "Kling 2.5 Turbo 文生视频（KIE）"},
]
NEW_KIE = [
    {"name": "seedream/5-flash-text-to-image", "modality": "image",
     "label": "Seedream 5.0 Flash 文生图（KIE）"},
    {"name": "seedream/5-flash-image-to-image", "modality": "image",
     "label": "Seedream 5.0 Flash 参考图（KIE）"},
    {"name": "seedream/5-pro-text-to-image", "modality": "image",
     "label": "Seedream 5.0 Pro 文生图（KIE）"},
    {"name": "seedream/5-lite-text-to-image", "modality": "image",
     "label": "Seedream 5.0 Lite 文生图（KIE）"},
    {"name": "seedream/4.5-text-to-image", "modality": "image",
     "label": "Seedream 4.5 文生图（KIE）"},
    {"name": "nano-banana-2", "modality": "image", "label": "Nano Banana 2（KIE）"},
    {"name": "gpt-image-2-text-to-image", "modality": "image",
     "label": "GPT Image 2 文生图（KIE）"},
    {"name": "gpt-image-2-image-to-image", "modality": "image",
     "label": "GPT Image 2 参考图（KIE）"},
    {"name": "bytedance/seedance-2-5", "modality": "video", "label": "Seedance 2.5（KIE）"},
    {"name": "bytedance/seedance-2", "modality": "video", "label": "Seedance 2.0（KIE）"},
    {"name": "kling-4.0/keyframe-to-video", "modality": "video",
     "label": "Kling 4.0 首帧生视频（KIE）"},
    {"name": "wan/3-0-video", "modality": "video", "label": "Wan 3.0（KIE）"},
]
KIE_HINT = (
    "图片/视频走统一异步任务接口（createTask → recordInfo），参考图与首帧会先上传成 URL。"
    "图片：seedream/5-flash-text-to-image、seedream/5-flash-image-to-image、"
    "nano-banana-2、gpt-image-2-text-to-image；"
    "视频：bytedance/seedance-2-5（最长 30 秒）、bytedance/seedance-2、"
    "kling-4.0/keyframe-to-video（必须给首帧）、wan/3-0-video。"
)


def _dump(items: list[dict]) -> str:
    return json.dumps(items, ensure_ascii=False)


def _load(raw: str) -> list[dict]:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict)]


def _names(items: list[dict]) -> list[str]:
    return [str(d.get("name") or "") for d in items]


def _forward(raw: str, old: list[dict], new: list[dict]) -> str:
    """出厂清单 → 整体换新；用户改过 → 只补缺的，不删不改。"""
    data = _load(raw)
    if _names(data) == _names(old):
        return _dump(new)
    have = set(_names(data))
    merged = list(data)
    for item in new:
        if item["name"] not in have:
            merged.append(item)
    return _dump(merged)


def _backward(raw: str, old: list[dict], new: list[dict]) -> str:
    data = _load(raw)
    if _names(data) == _names(new):
        return _dump(old)
    old_names = set(_names(old))
    new_names = set(_names(new))
    kept = [
        d for d in data
        if str(d.get("name") or "") in old_names or str(d.get("name") or "") not in new_names
    ]
    return _dump(kept)


def _is_kie_async(url: str) -> bool:
    # 聊天服务的 Base URL 带模型段（/gemini-…/v1），只有异步服务是裸域
    return (url or "").strip().rstrip("/").lower().endswith("api.kie.ai")


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE provider_presets SET models_json = :m, hint = :h WHERE key = 'deepseek'"
        ),
        {"m": _dump(NEW_DEEPSEEK), "h": DEEPSEEK_HINT},
    )
    conn.execute(
        sa.text(
            "UPDATE provider_presets SET models_json = :m, hint = :h WHERE key = 'kie_image'"
        ),
        {"m": _dump(NEW_KIE), "h": KIE_HINT},
    )
    # 这一行现在同时承载图片与视频，名字要跟着改（老库里叫「KIE（图片·异步）」）
    conn.execute(
        sa.text("UPDATE provider_presets SET name = :n WHERE key = 'kie_image'"),
        {"n": "KIE（图片/视频·异步）"},
    )

    rows = conn.execute(
        sa.text("SELECT id, base_url, models_json FROM provider_services")
    ).fetchall()
    for row in rows:
        url = (row.base_url or "").lower()
        if "api.deepseek.com" in url:
            merged = _forward(row.models_json, OLD_DEEPSEEK, NEW_DEEPSEEK)
        elif _is_kie_async(row.base_url):
            merged = _forward(row.models_json, OLD_KIE, NEW_KIE)
        else:
            continue
        conn.execute(
            sa.text("UPDATE provider_services SET models_json = :m WHERE id = :i"),
            {"m": merged, "i": row.id},
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("UPDATE provider_presets SET models_json = :m WHERE key = 'deepseek'"),
        {"m": _dump(OLD_DEEPSEEK)},
    )
    conn.execute(
        sa.text("UPDATE provider_presets SET models_json = :m WHERE key = 'kie_image'"),
        {"m": _dump(OLD_KIE)},
    )
    conn.execute(
        sa.text("UPDATE provider_presets SET hint = :h WHERE key = 'deepseek'"),
        {"h": "性价比高的中文文本模型"},
    )
    conn.execute(
        sa.text("UPDATE provider_presets SET hint = :h WHERE key = 'kie_image'"),
        {"h": "图片/视频走统一异步任务接口（createTask → recordInfo），参考图与首帧会先上传成 URL。"},
    )

    rows = conn.execute(
        sa.text("SELECT id, base_url, models_json FROM provider_services")
    ).fetchall()
    for row in rows:
        url = (row.base_url or "").lower()
        if "api.deepseek.com" in url:
            reverted = _backward(row.models_json, OLD_DEEPSEEK, NEW_DEEPSEEK)
        elif _is_kie_async(row.base_url):
            reverted = _backward(row.models_json, OLD_KIE, NEW_KIE)
        else:
            continue
        conn.execute(
            sa.text("UPDATE provider_services SET models_json = :m WHERE id = :i"),
            {"m": reverted, "i": row.id},
        )
