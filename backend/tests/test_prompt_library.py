"""提示词库种子：结构有效、键唯一、占位符成对。"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.registry.schema_registry import SCHEMA_REGISTRY  # noqa: E402


def _seeds() -> list[dict]:
    spec = SCHEMA_REGISTRY["prompts"]
    return list(spec.seed or [])


def test_seed_keys_are_unique_and_required_fields_present():
    rows = _seeds()
    keys = [r["key"] for r in rows]
    assert len(keys) == len(set(keys)), "提示词 key 重复，导入导出会互相覆盖"
    for row in rows:
        assert row["key"] and row["title"] and row["content"]
        assert row["modality"] in ("text", "image", "video")
        assert row["content"].count("{") == row["content"].count("}"), (
            f"{row['key']} 的占位符花括号不成对"
        )


def test_each_modality_has_enough_templates():
    rows = _seeds()
    assert len(rows) >= 30, "提示词库太薄，起不到「拿来就用」的作用"
    for modality, minimum in (("image", 10), ("video", 8), ("text", 10)):
        got = [r for r in rows if r["modality"] == modality]
        assert len(got) >= minimum, f"{modality} 模板只有 {len(got)} 条"
