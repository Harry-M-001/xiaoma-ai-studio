"""粘贴 Key 快速接入的规则测试（直接 python 运行）。

运行：venv/Scripts/python tests/test_quick_setup.py

这里刻意不去连真上游：真正要钉死的是**判定规则**——
「这串 Key 会被认成谁」以及「认不出时会不会乱猜」。
探测本身的失败路径由 test_provider_errors.py 覆盖。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.providers.base import AdapterError  # noqa: E402
from app.registry.schema_registry import SCHEMA_REGISTRY  # noqa: E402
from app.services import quick_setup  # noqa: E402


def _seed_patterns() -> dict[str, str]:
    spec = SCHEMA_REGISTRY["provider_presets"]
    return {row["key"]: row.get("key_pattern", "") for row in spec.seed}


SEEDS = _seed_patterns()


def _who(key: str) -> list[str]:
    """这串 Key 会被哪些预设认出来（按预设顺序）。"""
    return [k for k, pattern in SEEDS.items() if quick_setup._matches(pattern, key)]


def test_recognizes_openai_project_key():
    assert _who("sk-proj-" + "A" * 40) == ["openai"]


def test_ambiguous_sk32_hits_both_candidates():
    """DeepSeek 与通义千问的 Key 形状完全相同。

    这是现实：机器分不开。所以要求**两个都进候选**，由真实请求去分辨，
    而不是按 sort_order 偷偷选一个——选错的表现是「Key 明明没问题却报 401」。
    """
    assert _who("sk-" + "0123456789abcdef" * 2) == ["deepseek", "qwen"]


def test_recognizes_kimi_long_key():
    assert _who("sk-" + "a" * 48) == ["kimi"]


def test_recognizes_openrouter_key():
    assert _who("sk-or-v1-" + "b" * 32) == ["openrouter"]


def test_recognizes_zhipu_two_part_key():
    assert _who("0" * 32 + "." + "c" * 16) == ["zhipu"]


def test_recognizes_ark_uuid_key():
    assert _who("3f2c1b0a-1d2e-4f5a-8b9c-0d1e2f3a4b5c") == ["ark"]


def test_unknown_shape_hits_nothing():
    """认不出来时必须一个都不命中，由上层退化成「列全部候选给用户挑」。"""
    assert _who("totally-not-a-key") == []
    assert _who("hf_" + "d" * 40) == []


def test_broken_pattern_is_ignored_not_fatal():
    """配置表里写错正则：跳过它，不要让整个接入流程崩掉。"""
    assert quick_setup._matches("([unclosed", "sk-whatever") is False
    assert quick_setup._matches("", "sk-whatever") is False
    # 一条坏的 + 一条好的，好的仍然生效
    assert quick_setup._matches("([unclosed|^sk-", "sk-1") is True


def test_empty_key_is_rejected_before_touching_db():
    """空 Key 要在碰数据库之前就拦掉（顺带保证「粘了个空格」也有明确提示）。"""
    for bad in ("", "   ", "\n"):
        try:
            asyncio.run(quick_setup.setup(None, api_key=bad))  # type: ignore[arg-type]
        except AdapterError as e:
            assert "Key" in str(e)
        else:
            raise AssertionError(f"空 Key 应该被拒绝：{bad!r}")


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _EmptyDB:
    """预设表为空的假库：用来验证「不存在的服务商」这类纯逻辑分支，不碰真数据库。"""

    async def execute(self, *_args, **_kwargs):
        return _EmptyResult()


def test_unknown_provider_key_names_the_problem():
    """显式指定一个不存在的服务商时要说清是哪个，而不是静默变成自动识别。"""
    try:
        asyncio.run(
            quick_setup.setup(
                _EmptyDB(), api_key="sk-" + "a" * 48, provider_key="not-exist"
            )
        )
    except AdapterError as e:
        assert "not-exist" in str(e)
    else:
        raise AssertionError("不存在的服务商应该报错")


def test_no_presets_yields_empty_candidates_not_crash():
    """预设表为空（配置被清空）时，接入接口要给出「没得选」而不是抛异常。"""
    result = asyncio.run(quick_setup.setup(_EmptyDB(), api_key="sk-" + "a" * 48))
    assert result["ok"] is False
    assert result["candidates"] == []
    assert result["tried"] == []


def test_ollama_preset_is_not_a_key_candidate():
    """Ollama 不需要 Key，不该出现在「粘贴 Key」的候选里（它走另一条接入路径）。"""
    assert SEEDS["ollama"] == ""
    # 反过来确认：所有能靠 Key 形状认出来的预设里都没有 ollama
    assert "ollama" not in _who("sk-proj-" + "A" * 40)
    assert "ollama" not in _who("sk-or-v1-" + "b" * 32)


def test_migration_patterns_match_seed():
    """新装走种子、老库走迁移：两份规则必须一致。

    不一致的症状很难查——同一串 Key，在新装的机器上认得出、在升级上来的机器上认不出，
    而两边代码是同一份。
    """
    spec = importlib.util.spec_from_file_location(
        "mig_0012", BACKEND / "alembic" / "versions" / "0012_preset_key_pattern.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, pattern in module.KEY_PATTERNS.items():
        assert SEEDS.get(key) == pattern, f"{key} 的识别规则在迁移与种子里不一致"


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
