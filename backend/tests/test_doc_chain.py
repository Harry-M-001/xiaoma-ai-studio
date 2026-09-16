"""自动链文档生成：模板拼装与分块编排验证（无需 pytest：直接 python 运行）。

运行：venv/Scripts/python tests/test_doc_chain.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import doc_service
from app.services.doc_service import AgentSpec


def _spec(**kw) -> AgentSpec:
    base = dict(
        key="novel",
        label="小说",
        system_prompt="你是小说家。",
        user_template="【创意简报】\n{content}\n\n【补充要求】\n{params}",
        plan_prompt="列出 {total} 章大纲。\n{content}",
        chunk_prompt="写第 {index} 章（共 {total} 章）。\n【大纲】\n{plan}",
        chunk_param="chapterCount",
        model_key="",
        temperature=0.8,
        max_tokens=2000,
    )
    base.update(kw)
    return AgentSpec(**base)


def test_render_replaces_known_placeholders():
    out = doc_service.render("A{content}B{params}C", content="正文", params="补充")
    assert out == "A正文B补充C", out


def test_render_keeps_unknown_placeholder():
    # 用户自改模板可能写出没见过的占位符，不能因此 500
    out = doc_service.render("A{x}B{content}C", content="正文")
    assert out == "A{x}B正文C", out


def test_render_value_with_braces_is_safe():
    # 上游正文里带花括号（提示词/JSON 片段很常见）不能被当成占位符解析
    out = doc_service.render("前缀{content}后缀", content='{"role": "主角"}')
    assert out == '前缀{"role": "主角"}后缀', out


def test_resolve_total_clamps():
    s = _spec()
    assert doc_service.resolve_total(s, {"chapterCount": 3}) == 3
    assert doc_service.resolve_total(s, {"chapterCount": "5"}) == 5
    # 填 0 / 负数 / 垃圾值 → 退回 1
    assert doc_service.resolve_total(s, {"chapterCount": 0}) == 1
    assert doc_service.resolve_total(s, {"chapterCount": -2}) == 1
    assert doc_service.resolve_total(s, {"chapterCount": "abc"}) == 1
    assert doc_service.resolve_total(s, {}) == 1
    # 上限保护：填 999 不会真的调 999 次
    assert doc_service.resolve_total(s, {"chapterCount": 999}) == doc_service.MAX_CHUNKS
    # 没配 chunk_param 的阶段永远单块
    assert doc_service.resolve_total(_spec(chunk_param=""), {"chapterCount": 9}) == 1


def test_needs_chunking():
    s = _spec()
    assert doc_service.needs_chunking(s, 3) is True
    assert doc_service.needs_chunking(s, 1) is True  # 有大纲提示词，仍需走分块流程
    # 没配 chunk_prompt 的阶段退化为一次成文
    assert doc_service.needs_chunking(_spec(chunk_prompt=""), 3) is False


def test_message_composition():
    s = _spec()
    msgs = doc_service.chunk_messages(s, "上游正文", "补充", 3, 2, "大纲内容")
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == "你是小说家。"
    assert msgs[1]["role"] == "user"
    assert "写第 2 章（共 3 章）" in msgs[1]["content"]
    assert "大纲内容" in msgs[1]["content"]


def test_compose_document_orders_plan_first():
    out = doc_service.compose_document("## 第1章 甲\n梗概", ["## 第1章 甲\n正文", "## 第2章 乙\n正文"])
    assert out.startswith("## 第1章 甲\n梗概")
    assert "## 第2章 乙" in out
    # 不放大纲时只拼正文
    out2 = doc_service.compose_document("大纲", ["正文一"], with_plan=False)
    assert out2 == "正文一", out2


def test_progress_monotonic_within_range():
    assert doc_service.progress_for(0, 4, 10, 85) == 10
    assert doc_service.progress_for(2, 4, 10, 85) == 52
    assert doc_service.progress_for(4, 4, 10, 85) == 95
    assert doc_service.progress_for(9, 4, 10, 85) == 95  # 不越界


def test_seed_agents_are_complete():
    """各阶段种子必须齐备，且模板占位符与 chunk_param 自洽。"""
    from app.registry.schema_registry import SCHEMA_REGISTRY

    spec = SCHEMA_REGISTRY.get("agent_prompts")
    assert spec is not None, "agent_prompts 未注册到配置注册表"
    keys = [row["key"] for row in spec.seed]
    assert keys == ["idea", "novel", "script", "storyboard", "assetSheet"], keys

    by_key = {row["key"]: row for row in spec.seed}
    # idea 与 assetSheet 单次成文，不该有分块配置
    # （资产表被拆成两半既难读也难解析）
    assert not by_key["idea"]["chunk_prompt"]
    assert not by_key["idea"]["chunk_param"]
    assert not by_key["assetSheet"]["chunk_prompt"]
    assert not by_key["assetSheet"]["chunk_param"]
    # 其余三个阶段必须配齐「大纲 + 分块 + 块数参数」
    for key in ("novel", "script", "storyboard"):
        row = by_key[key]
        assert row["plan_prompt"].strip(), f"{key} 缺大纲提示词"
        assert row["chunk_prompt"].strip(), f"{key} 缺分块模板"
        assert row["chunk_param"].strip(), f"{key} 缺块数参数名"
        for ph in ("{index}", "{total}"):
            assert ph in row["chunk_prompt"], f"{key} 分块模板缺 {ph}"
        assert "{total}" in row["plan_prompt"], f"{key} 大纲提示词缺 {{total}}"
        assert row["system_prompt"].strip() and row["user_template"].strip()


def test_placeholder_defaults_cover_seed_params():
    """节点上会写进 node_params 的参数名，必须在 agent 种子里用得上。"""
    from app.registry.canvas_nodes import NODE_SCHEMAS
    from app.registry.schema_registry import SCHEMA_REGISTRY

    agents = {row["key"]: row for row in SCHEMA_REGISTRY["agent_prompts"].seed}
    for key in ("novel", "script", "storyboard"):
        param = agents[key]["chunk_param"]
        features = NODE_SCHEMAS[key]["features"]
        assert param in features, f"节点「{key}」缺少参数控件 {param}（features={features}）"


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
