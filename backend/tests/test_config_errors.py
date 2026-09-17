"""配置保存失败时的提示文案（直接 python 运行）。

运行：venv/Scripts/python tests/test_config_errors.py

为什么值一条：用户报的「保存失败」就是这一类。原来的文案只回一句
「保存失败：存在重复的唯一标识，请修改后重试」——不说是哪个字段、哪个值、跟谁撞了，
用户只能干瞪眼。这里把「说清是哪一项」钉住，防止以后又退回笼统说法。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.registry.schema_registry import SCHEMA_REGISTRY  # noqa: E402
from app.services import config_center_service as cc  # noqa: E402


class _FakeOrig(Exception):
    """冒充 sqlalchemy 的 DBAPI 异常（真实异常的原始信息挂在 .orig 上）。"""


class _FakeDBError(Exception):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.orig = _FakeOrig(text)


class _Row:
    def __init__(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


def test_unique_violation_names_the_field_and_the_value():
    spec = SCHEMA_REGISTRY["prompts"]
    msg = cc._save_error(spec, _Row(key="translate-polish"), _FakeDBError("UNIQUE constraint failed: prompts.key"))
    assert "标识" in msg, msg  # 字段的中文名（FieldSpec.label）
    assert "translate-polish" in msg, msg  # 用户刚填的那个值
    assert spec.label in msg, msg  # 哪张表
    assert "换一个" in msg, msg  # 下一步怎么做


def test_falls_back_to_column_name_for_unknown_field():
    spec = SCHEMA_REGISTRY["prompts"]
    msg = cc._save_error(spec, _Row(slug="x"), _FakeDBError("UNIQUE constraint failed: prompts.slug"))
    assert "slug" in msg, msg


def test_value_is_quoted_only_when_present():
    spec = SCHEMA_REGISTRY["prompts"]
    msg = cc._save_error(spec, _Row(key=""), _FakeDBError("UNIQUE constraint failed: prompts.key"))
    assert "「」" not in msg, msg
    assert "标识" in msg


def test_other_db_errors_keep_the_original_reason():
    """不是唯一约束失败时，别把原始原因吃掉——那才是排查用的线索。"""
    spec = SCHEMA_REGISTRY["prompts"]
    msg = cc._save_error(spec, _Row(key="a"), _FakeDBError("database is locked"))
    assert msg.startswith("保存失败："), msg
    assert "database is locked" in msg, msg


def test_unparsable_unique_error_still_says_something_useful():
    """认不出是哪一列时，也要回一句人能看懂的话，而不是抛原始 SQL 报错。"""
    spec = SCHEMA_REGISTRY["prompts"]
    msg = cc._save_error(spec, _Row(key="a"), _FakeDBError("UNIQUE constraint failed"))
    assert "重复" in msg, msg
    assert "UNIQUE" not in msg, msg


def test_every_table_with_a_key_field_has_a_readable_label():
    """带唯一「标识」的表都得有中文名，否则报错里会出现「provider_presets」这种机器名。"""
    for name, spec in SCHEMA_REGISTRY.items():
        if any(f.name == "key" for f in spec.fields):
            msg = cc._save_error(spec, _Row(key="dup"), _FakeDBError(f"UNIQUE constraint failed: {name}.key"))
            assert spec.label in msg, f"{name} 缺少可读的表名：{msg}"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failed else 0)
