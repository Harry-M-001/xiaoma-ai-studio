"""E 期画布 Agent（直接 python 运行）。

运行：venv/Scripts/python tests/test_canvas_agent.py

这个功能的成败不在「能不能调通 LLM」，而在**模型输出出错时用户的体验**。
所以用例集中在四件事：

1. **宽容修复**：模型认不出新节点、连成环、引用不存在的 id、把尺寸写成档位外的
   值——每一种都要「丢掉坏的那一小块、留下能用的、并说明理由」，而不是整份作废。
   一个字段拼错就让用户拿到空白画布，等于这个功能没做。
2. **不问模型要坐标**：布局必须由拓扑算出来，且**确定**（同样的拓扑落在同样的位置）。
3. **结果协议**：`need_input` 不该花额度、`need_credentials` 要能自己发现、
   `unparsable` 只重试一次。前端靠这些状态给下一步，不能靠匹配文案。
4. **只读**：`plan()` 不改画布、不建任务。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import Project, Task  # noqa: E402
from app.registry.canvas_nodes import NODE_SCHEMAS  # noqa: E402
from app.services import canvas_agent  # noqa: E402
from app.services import config_center_service as cc  # noqa: E402
from app.services import provider_store  # noqa: E402


def _run(fn):
    async def main():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as db:
                await cc.ensure_seed(db)
                return await fn(db)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _node(node_id: str, kind: str, prompt: str = "做点什么", **params) -> dict:
    item = {"id": node_id, "kind": kind, "prompt": prompt}
    if params:
        item["params"] = params
    return item


# ============================================================
# 一、解析：模型很爱加解释和代码块
# ============================================================


def test_extract_json_handles_the_three_shapes_models_actually_emit():
    plain = '{"summary":"a","nodes":[],"edges":[]}'
    assert canvas_agent.extract_json(plain) == {"summary": "a", "nodes": [], "edges": []}

    fenced = f"好的，这是结果：\n```json\n{plain}\n```\n希望有帮助！"
    assert canvas_agent.extract_json(fenced) == {"summary": "a", "nodes": [], "edges": []}

    bare_fence = f"```\n{plain}\n```"
    assert canvas_agent.extract_json(bare_fence) == {"summary": "a", "nodes": [], "edges": []}


def test_extract_json_takes_the_first_complete_object():
    """后面还跟了一段带花括号的说明时，不能把两段吞成一个四不像。"""
    text = '{"nodes":[{"id":"n1"}],"edges":[]}\n\n说明：{"这是个例子":true}'
    assert canvas_agent.extract_json(text) == {"nodes": [{"id": "n1"}], "edges": []}


def test_extract_json_ignores_braces_inside_strings():
    text = '{"summary":"他说「{你好}」","nodes":[],"edges":[]}'
    assert canvas_agent.extract_json(text)["summary"] == "他说「{你好}」"


def test_extract_json_returns_none_for_non_json():
    for bad in ("", "我做不到", "{不是 json", "[1,2,3]", '{"a": '):
        assert canvas_agent.extract_json(bad) is None, bad


# ============================================================
# 二、宽容修复
# ============================================================


def _repair(raw, db_options=None, styles=None):
    return canvas_agent.repair(raw, db_options or {}, styles or [])


def test_unknown_node_kind_is_dropped_with_a_note():
    _, nodes, _, notes = _repair({
        "nodes": [_node("n1", "idea"), _node("n2", "teleporter")],
        "edges": [],
    })
    assert [n["kind"] for n in nodes] == ["idea"]
    assert any("teleporter" in n for n in notes), notes


def test_dangling_edges_are_dropped_with_a_note():
    _, _, edges, notes = _repair({
        "nodes": [_node("n1", "idea"), _node("n2", "novel")],
        "edges": [{"from": "n1", "to": "n2"}, {"from": "n1", "to": "n9"}],
    })
    assert edges == [{"from": "n1", "to": "n2"}]
    assert any("不存在" in n and "1 条" in n for n in notes), notes


def test_cycle_is_broken_by_dropping_an_edge_not_a_node():
    _, nodes, edges, notes = _repair({
        "nodes": [_node("n1", "idea"), _node("n2", "novel"), _node("n3", "script")],
        "edges": [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"},
                  {"from": "n3", "to": "n1"}],
    })
    assert len(nodes) == 3, "断环不该丢节点"
    assert len(edges) == 2
    assert any("成环" in n for n in notes), notes


def test_self_loop_is_dropped_silently():
    """自连接不是「成环」那么严重，直接丢弃即可，不必给用户一条提示。"""
    _, _, edges, notes = _repair({
        "nodes": [_node("n1", "idea")],
        "edges": [{"from": "n1", "to": "n1"}],
    })
    assert edges == []


def test_duplicate_edges_are_collapsed():
    _, _, edges, _ = _repair({
        "nodes": [_node("n1", "idea"), _node("n2", "novel")],
        "edges": [{"from": "n1", "to": "n2"}, {"from": "n1", "to": "n2"}],
    })
    assert len(edges) == 1


def test_out_of_range_param_is_rejected_and_default_kept():
    """尺寸写成 1920x1080（不在档位里）时要退回默认并解释，而不是带着它往下走。"""
    options = {"image_size": ["1024x1024", "1280x720"]}
    _, nodes, _, notes = _repair({
        "nodes": [_node("n1", "image", size="1920x1080")],
        "edges": [],
    }, options)
    assert "size" not in nodes[0]["data"], "档位外的值不该落到节点上"
    assert any("1920x1080" in n and "默认" in n for n in notes), notes


def test_in_range_param_is_kept():
    options = {"image_size": ["1024x1024", "1280x720"]}
    _, nodes, _, notes = _repair({
        "nodes": [_node("n1", "image", size="1280x720")],
        "edges": [],
    }, options)
    assert nodes[0]["data"]["size"] == "1280x720"
    assert not [n for n in notes if "size" in n], notes


def test_param_not_supported_by_the_node_kind_is_ignored():
    """idea 节点没有 size 这个参数，模型塞进来要忽略（而不是污染节点 data）。"""
    _, nodes, _, notes = _repair({
        "nodes": [_node("n1", "idea", size="1024x1024")],
        "edges": [],
    })
    assert "size" not in nodes[0]["data"]
    assert any("不支持参数 size" in n for n in notes), notes


def test_agent_cannot_set_forbidden_params():
    """model_key / refImages 这类必须由前端按真实可用的东西填，Agent 不许碰。"""
    _, nodes, _, _ = _repair({
        "nodes": [{"id": "n1", "kind": "image", "prompt": "x",
                   "params": {"model_key": "9:whatever", "refImages": [{"id": 1}]}}],
        "edges": [],
    })
    assert "model_key" not in nodes[0]["data"]
    assert "refImages" not in nodes[0]["data"]


def test_video_mode_and_style_are_validated():
    _, nodes, _, notes = _repair({
        "nodes": [
            _node("n1", "video", mode="warp_drive"),
            _node("n2", "novel", styleKey="不存在的风格"),
        ],
        "edges": [],
    })
    assert "mode" not in nodes[0]["data"]
    assert "styleKey" not in nodes[1]["data"]
    assert len([n for n in notes if "默认" in n]) == 2, notes
    # 提示里要用界面上的说法，不是 data 里的英文键名
    assert any("视频模式" in n for n in notes), notes
    assert any("风格「不存在的风格」不存在" in n for n in notes), notes


def test_existing_style_key_is_kept():
    from app.services.style_service import StyleCard

    styles = [StyleCard(key="wkw", name="王家卫", agent_prompt="", image_prompt="",
                        video_prompt="", negative_prompt="")]
    _, nodes, _, notes = _repair({
        "nodes": [_node("n1", "novel", styleKey="wkw")],
        "edges": [],
    }, {}, styles)
    assert nodes[0]["data"]["styleKey"] == "wkw"


def test_broken_ids_and_missing_ids_are_repaired():
    """模型偶尔会漏 id 或两个节点用同一个 id —— 修好它，别让整份草稿作废。"""
    _, nodes, edges, _ = _repair({
        "nodes": [{"kind": "idea", "prompt": "a"}, _node("dup", "novel"), _node("dup", "script")],
        "edges": [{"from": "dup", "to": "dup"}],
    })
    ids = [n["id"] for n in nodes]
    assert len(ids) == 3 and len(set(ids)) == 3, ids
    assert all(e["from"] != e["to"] for e in edges)


def test_too_many_nodes_are_capped_with_a_note():
    raw = {"nodes": [_node(f"n{i}", "text", "x") for i in range(40)], "edges": []}
    _, nodes, _, notes = _repair(raw)
    assert len(nodes) == canvas_agent.MAX_NODES
    assert any("超过" in n for n in notes), notes


def test_non_dict_and_missing_fields_do_not_crash():
    for bad in ({"nodes": "不是数组"}, {"nodes": [1, "x", None]}, {"nodes": [{}]}, {}):
        _, nodes, edges, _ = _repair(bad)
        assert isinstance(nodes, list) and isinstance(edges, list)
    # 空 kind 的节点会被丢掉，理由里说明是「契约里没有」
    _, nodes, _, notes = _repair({"nodes": [{"id": "n1", "kind": "", "prompt": "x"}], "edges": []})
    assert nodes == [] and notes


def test_legacy_kind_is_normalized_not_dropped():
    """存量画布里还有 imageEdit，Agent 万一学会了这个旧名字也该照收。"""
    _, nodes, _, _ = _repair({"nodes": [_node("n1", "imageEdit")], "edges": []})
    assert nodes[0]["kind"] == "image"


# ============================================================
# 三、布局：不问模型要坐标
# ============================================================


def _chain_raw():
    return {
        "summary": "s",
        "nodes": [_node("a", "idea"), _node("b", "novel"), _node("c", "script")],
        "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}],
    }


def test_layout_puts_a_chain_in_a_row_left_to_right():
    _, nodes, edges, _ = _repair(_chain_raw())
    nodes = canvas_agent.layout(nodes, edges)
    xs = [n["position"]["x"] for n in nodes]
    assert xs == sorted(xs) and len(set(xs)) == 3
    assert len({n["position"]["y"] for n in nodes}) == 1


def test_layout_is_deterministic():
    """同样的拓扑两次必须落在同样的位置：否则用户每点一次布局就变一次。"""
    a = canvas_agent.layout(*_repair(_chain_raw())[1:3])
    b = canvas_agent.layout(*_repair(_chain_raw())[1:3])
    assert [n["position"] for n in a] == [n["position"] for n in b]


def test_layout_stacks_a_branch_without_overlap():
    _, nodes, edges, _ = _repair({
        "nodes": [_node("a", "storyboard"), _node("b", "assetImage"), _node("c", "video")],
        "edges": [{"from": "a", "to": "b"}, {"from": "a", "to": "c"}],
    })
    nodes = canvas_agent.layout(nodes, edges)
    left = [n for n in nodes if n["id"] != "a"]
    assert {n["position"]["x"] for n in left} == {left[0]["position"]["x"]}, "同层应当同列"
    assert len({n["position"]["y"] for n in left}) == 2, "同层不能重叠"


def test_layout_respects_longest_path_not_shortest():
    """a→b→d 与 a→d 并存时，d 必须在第 3 列，否则连线会往回指。"""
    _, nodes, edges, _ = _repair({
        "nodes": [_node("a", "idea"), _node("b", "novel"), _node("d", "script")],
        "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "d"}, {"from": "a", "to": "d"}],
    })
    nodes = canvas_agent.layout(nodes, edges)
    col = {n["id"]: n["position"]["x"] for n in nodes}
    assert col["a"] < col["b"] < col["d"]


def test_layout_survives_an_empty_graph():
    assert canvas_agent.layout([], []) == []


def test_required_modalities_follows_the_node_catalogue():
    _, nodes, _, _ = _repair({
        "nodes": [_node("a", "idea"), _node("b", "storyboardImage"), _node("c", "video")],
        "edges": [],
    })
    assert canvas_agent.required_modalities(nodes) == ["text", "image", "video"]


# ============================================================
# 四、契约下发
# ============================================================


def test_system_prompt_covers_every_node_kind_in_the_registry():
    """契约是唯一事实源：注册表里加一个节点类型，提示词里必须自动出现。"""
    prompt = canvas_agent.build_system_prompt({}, [])
    for kind, schema in NODE_SCHEMAS.items():
        assert f"- {kind}（{schema['label']}）" in prompt, f"提示词里没有 {kind}"
    assert "只输出一个 JSON 对象" in prompt


def test_system_prompt_lists_the_real_option_values():
    """把合法档位告诉模型，否则它会编一个用不了的尺寸出来。"""
    prompt = canvas_agent.build_system_prompt(
        {"image_size": ["1024x1024", "1280x720"], "video_ratio": ["16:9", "9:16"]}, []
    )
    assert "1024x1024 / 1280x720" in prompt
    assert "16:9 / 9:16" in prompt


def test_system_prompt_only_mentions_styles_when_they_exist():
    assert "可选风格卡" not in canvas_agent.build_system_prompt({}, [])
    from app.services.style_service import StyleCard

    card = StyleCard(key="wkw", name="王家卫", agent_prompt="", image_prompt="",
                     video_prompt="", negative_prompt="")
    assert "wkw：王家卫" in canvas_agent.build_system_prompt({}, [card])


# ============================================================
# 五、结果协议 + 只读
# ============================================================


def test_short_brief_asks_for_input_without_spending_a_call():
    async def case(db):
        called = _install_fake(db, ['{"nodes":[]}'])
        draft = await canvas_agent.plan(db, "做个视频")
        assert draft.status == canvas_agent.STATUS_NEED_INPUT
        assert not called(), "需求太短时不该花额度去猜"
        assert draft.next_action
        return True

    assert _run(case) is True


def test_missing_text_model_reports_need_credentials():
    async def case(db):
        draft = await canvas_agent.plan(db, "做一个 60 秒竖屏短剧，三幕结构")
        assert draft.status == canvas_agent.STATUS_NEED_CREDENTIALS
        assert any("文本模型" in w for w in draft.warnings)
        assert "模型服务" in draft.next_action
        return True

    assert _run(case) is True


def test_unparsable_retries_once_then_gives_up():
    async def case(db):
        calls = _install_fake(db, ["我做不到", "还是不给你"])
        draft = await canvas_agent.plan(db, "做一个 60 秒竖屏短剧，三幕结构")
        assert draft.status == canvas_agent.STATUS_UNPARSABLE
        assert len(calls()) == 2, "应当只给一次自我修复的机会（再多就是烧额度）"
        assert draft.warnings and "JSON" in draft.warnings[0]
        return True

    assert _run(case) is True


def test_second_attempt_can_succeed():
    """第一次没给 JSON、被追问后给了 —— 这正是那次重试存在的意义。"""
    async def case(db):
        _install_fake(db, ["我这就写……", '```json\n{"summary":"ok","nodes":[{"id":"n1","kind":"idea","prompt":"x"}],"edges":[]}\n```'])
        draft = await canvas_agent.plan(db, "做一个 60 秒竖屏短剧，三幕结构")
        assert draft.status == canvas_agent.STATUS_OK
        assert [n["kind"] for n in draft.nodes] == ["idea"]
        return True

    assert _run(case) is True


def test_successful_plan_is_laid_out_and_reports_requirements():
    async def case(db):
        reply = (
            '{"summary":"文字链","nodes":['
            '{"id":"a","kind":"idea","prompt":"写一个创意"},'
            '{"id":"b","kind":"storyboard","prompt":"做分镜","params":{"shotCount":8}},'
            '{"id":"c","kind":"video","prompt":"出片","params":{"shotVideo":"chain"}}],'
            '"edges":[{"from":"a","to":"b"},{"from":"b","to":"c"}]}'
        )
        _install_fake(db, [reply])
        draft = await canvas_agent.plan(db, "做一个 60 秒竖屏短剧，三幕结构，逐镜出片")
        assert draft.status == canvas_agent.STATUS_OK
        assert draft.summary == "文字链"
        assert all("position" in n for n in draft.nodes), "草稿必须自带落位"
        assert draft.nodes[1]["data"]["shotCount"] == 8
        assert draft.nodes[2]["data"]["shotVideo"] == "chain"
        assert draft.requires == ["text", "video"]
        # 缺的能力要提前说清，而不是等用户跑了才发现
        assert draft.missing == ["text", "video"]
        assert any("视频" in w for w in draft.warnings)
        return True

    assert _run(case) is True


def test_upstream_failure_is_reported_with_a_reason():
    async def case(db):
        _install_fake(db, [], error="上游返回 503，请稍后重试")
        draft = await canvas_agent.plan(db, "做一个 60 秒竖屏短剧，三幕结构")
        assert draft.status == canvas_agent.STATUS_UPSTREAM_ERROR
        assert "503" in draft.warnings[0]
        assert draft.next_action
        return True

    assert _run(case) is True


def test_plan_never_writes_anything():
    """搭画布是**只看不动**：不建任务、不改项目。"""
    async def case(db):
        _install_fake(db, ['{"summary":"s","nodes":[{"id":"n1","kind":"idea","prompt":"x"}],"edges":[]}'])
        await canvas_agent.plan(db, "做一个 60 秒竖屏短剧，三幕结构")
        tasks = (await db.execute(select(func.count()).select_from(Task))).scalar()
        projects = (await db.execute(select(func.count()).select_from(Project))).scalar()
        assert tasks == 0 and projects == 0
        return True

    assert _run(case) is True


# ============================================================
# 测试脚手架：假适配器（不打真实上游）
# ============================================================


class _FakeAdapter:
    def __init__(self, replies: list[str], error: str = ""):
        self.replies = list(replies)
        self.error = error
        self.calls: list[list[dict]] = []

    async def chat_stream(self, model, messages, temperature=None, max_tokens=None):
        self.calls.append(messages)
        if self.error:
            raise RuntimeError(self.error)
        text = self.replies.pop(0) if self.replies else ""
        yield text

    async def close(self):
        return None


class _Resolved:
    def __init__(self, adapter: _FakeAdapter):
        self.adapter = adapter
        self.model_name = "fake-model"
        self.modality = "text"
        self.service = None
        self.label = "fake"


def _install_fake(db, replies: list[str], error: str = ""):
    """把 provider_store 的画面换成假的，两个都被打桩，互相不干扰。"""
    adapter = _FakeAdapter(replies, error)
    resolved = _Resolved(adapter)

    async def fake_resolve(_db, _key, _modality):
        return resolved

    async def fake_list(_db):
        return []

    orig_resolve = provider_store.resolve_model
    orig_list = provider_store.list_services
    provider_store.resolve_model = fake_resolve  # type: ignore[assignment]
    provider_store.list_services = fake_list  # type: ignore[assignment]

    import atexit

    def restore():
        provider_store.resolve_model = orig_resolve  # type: ignore[assignment]
        provider_store.list_services = orig_list  # type: ignore[assignment]

    atexit.register(restore)
    return lambda: adapter.calls


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001  一个用例炸了别把剩下的都带下去
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
