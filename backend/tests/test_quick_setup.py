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


def test_recognizes_real_ark_key():
    """方舟现在发的是 `ark-<uuid>-<尾码>`（形状照真实 Key 写，但用的是假值）。

    第一版只认裸 UUID，用户把真 Key 粘进来认不出，还会拿它去试 OpenAI/DeepSeek/Kimi 三家。
    这条用例就是那次事故的看门人。

    注意这里用的必须是**假 Key**：把真 Key 写进仓库会被 GitHub 的密钥扫描拦下来
    （实测被拦过一次），而且本来也不该把用户凭据提交上去。
    """
    assert _who("ark-1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d-abcd12") == ["ark"]


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


def _load_migration(name: str):
    spec = importlib.util.spec_from_file_location(
        name, BACKEND / "alembic" / "versions" / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_chain_matches_seed():
    """新装走种子、老库走迁移链：两条路的**最终结果**必须一致。

    不一致的症状很难查——同一串 Key，在新装的机器上认得出、在升级上来的机器上认不出，
    而两边代码是同一份。

    这里按顺序把迁移里对 key_pattern 的改动叠加起来（0012 全量 + 0013 覆盖 ark），
    再和种子逐条比对。以后再加一条覆盖式迁移，只需往 _CHAIN 里加个名字。
    """
    effective: dict[str, str] = dict(_load_migration("0012_preset_key_pattern").KEY_PATTERNS)
    _CHAIN: list[tuple[str, bool]] = [("0013_ark_key_pattern", True)]
    for name, overrides in _CHAIN:
        module = _load_migration(name)
        if overrides:
            effective.update(module.KEY_PATTERNS)
        else:
            for key, value in module.KEY_PATTERNS.items():
                effective.setdefault(key, value)

    for key, pattern in effective.items():
        assert SEEDS.get(key) == pattern, f"{key} 的识别规则在迁移与种子里不一致"


class _FakePreset:
    """够用的预设替身：只带 quick_setup 会读到的字段。"""

    def __init__(self, key: str, pattern: str) -> None:
        self.key = key
        self.name = f"{key} 假服务商"
        self.kind = "openai"
        self.base_url = f"https://{key}.invalid/v1"
        self.models_json = '[{"name": "m", "modality": "text", "label": "M"}]'
        self.hint = ""
        self.key_pattern = pattern
        self.sort_order = 1


class _PresetDB:
    """只返回几个预设的假库，用来验证「认不出时会不会瞎试」。"""

    def __init__(self, presets: list[_FakePreset]) -> None:
        self._presets = presets

    async def execute(self, *_args, **_kwargs):
        presets = self._presets

        class _R:
            def scalars(self):
                return self

            def all(self):
                return presets

        return _R()


def test_unrecognized_key_is_not_probed():
    """认不出归属时**一个都不试**，直接把候选给用户。

    两个理由：一是白等（串行三个真实请求）；二是**不能拿用户的凭据去敲不相干服务商的门**——
    那串 Key 可能属于任何一家，把它发给三家用户没在用（甚至没听说过）的公司是越界的。
    """
    db = _PresetDB([_FakePreset("openai", r"^sk-proj-"), _FakePreset("ark", r"^ark-")])
    result = asyncio.run(quick_setup.setup(db, api_key="something-else"))

    assert result["ok"] is False
    assert result["recognized"] is False
    assert result["tried"] == [], "认不出归属却发起了试探（等于把 Key 发给了猜测的服务商）"
    assert {c["key"] for c in result["candidates"]} == {"openai", "ark"}


def test_recognized_key_is_probed_and_saved():
    """认得出来就照旧：试探 → 通过 → 落库。这里用一个「必然连不上」的假地址来验证失败路径。"""
    db = _PresetDB([_FakePreset("openai", r"^sk-proj-")])
    result = asyncio.run(quick_setup.setup(db, api_key="sk-proj-" + "A" * 40))
    assert result["recognized"] is True
    assert result["ok"] is False
    assert [t["key"] for t in result["tried"]] == ["openai"], "认出来的候选应当被试探"
    assert result["tried"][0]["ok"] is False


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
