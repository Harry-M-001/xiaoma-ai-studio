"""Agent 工作台（2.0）的行为测试：工具清单、历史重建、草稿落定的文档形状、接线。

运行：venv/Scripts/python tests/test_agent.py

不跑真模型：这条链上最贵、最不稳定的一段恰好也是「最难在单测里断言」的一段。
这里守的是**确定性**的三块——工具契约、上下文重建、写库形状；真机走查单独做。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.models import AgentSession, AgentStep  # noqa: E402
from app.registry.canvas_nodes import NODE_SCHEMAS, validate_document  # noqa: E402
from app.services import agent_runner, agent_tools  # noqa: E402


# ================================================================ 1. 工具契约


def test_tool_names_and_tiers_are_well_formed():
    """工具名唯一、权限级别合法、参数是对象、不可逆的必须有预览。"""
    names = [t.name for t in agent_tools.TOOLS]
    assert len(names) == len(set(names)), names
    assert 8 <= len(names) <= 28, f"工具数量不在合理区间：{len(names)}"
    for t in agent_tools.TOOLS:
        assert t.tier in (agent_tools.TIER_READ, agent_tools.TIER_WRITE,
                          agent_tools.TIER_IRREVERSIBLE), t.name
        assert t.parameters.get("type") == "object", t.name
        assert t.description and len(t.description) > 8, t.name
        if t.tier == agent_tools.TIER_IRREVERSIBLE:
            assert t.preview is not None, f"{t.name} 不可逆却没有代价预览"


def test_the_tool_set_covers_the_canvas_loop():
    """画布这条线上，读 / 建 / 草稿 / 落定 / 改 / 连线 / 执行 一个都不能少。"""
    names = {t.name for t in agent_tools.TOOLS}
    for required in (
        "list_agent_projects", "create_agent_project",
        "list_projects", "get_canvas", "summarize_canvas", "preview_canvas", "lint_canvas",
        "create_project", "draft_canvas", "apply_canvas_draft", "update_node", "connect_nodes",
        "run_canvas", "run_node", "canvas_status",
        "list_skills", "load_skill", "get_task", "get_asset", "list_engines",
        "list_prompt_templates",
    ):
        assert required in names, f"缺少工具 {required}"


# ================================================================ 1b. 技能（渐进披露）


def test_skills_have_metadata_and_body():
    """一个技能至少要有 name/description（第一层）与正文（第二层），否则等于没有。"""
    from app.services import agent_skills

    items = agent_skills.load_all()
    assert len(items) >= 3, [s["key"] for s in items]
    keys = [s["key"] for s in items]
    assert len(keys) == len(set(keys))
    for s in items:
        assert s["name"] and s["description"], s["key"]
        assert len(s["body"]) > 200, f"{s['key']} 正文太短，等于没写"
        # 描述要写清「什么时候用」，否则模型判断不了相关性
        assert "时" in s["description"] or "先加载" in s["description"], s["description"]


def test_skill_lookup_and_prompt_block():
    from app.services import agent_skills

    first = agent_skills.metadata()[0]
    got = agent_skills.get(first["key"])
    assert got and got["body"]
    assert agent_skills.get("no_such_skill") is None

    block = agent_skills.prompt_block()
    for item in agent_skills.metadata():
        assert item["key"] in block and item["name"] in block
    # 正文不进提示词（那是第二层），否则就不是渐进披露了
    assert agent_skills.metadata()[0]["description"] in block


def test_load_skill_is_safe_and_actionable():
    """未知技能要报错并**列出可选项**；存在时返回正文（超长截断）。"""
    import asyncio

    ok = asyncio.run(agent_tools.execute(None, "load_skill", {"key": "short-video"}))  # type: ignore[arg-type]
    assert ok["ok"] is True
    assert ok["data"]["body"] and "短视频" in ok["data"]["body"]

    bad = asyncio.run(agent_tools.execute(None, "load_skill", {"key": "nope"}))  # type: ignore[arg-type]
    assert bad["ok"] is False
    assert "short-video" in bad["hint"], bad["hint"]


def test_openai_schema_shape():
    """下发给模型的就是标准的 function schema：名称、描述、JSON Schema 三件。"""
    for item in agent_tools.openai_tools():
        assert item["type"] == "function"
        fn = item["function"]
        assert fn["name"] and fn["description"]
        assert fn["parameters"]["type"] == "object"


def test_the_system_prompt_says_the_hard_rules():
    """提示词里必须写清「不可逆要人点」「事实只信工具」「只调一个工具」——这三条是护栏。"""
    text = agent_runner.build_system_prompt(
        AgentSession(id=1, title="t", model_key="", canvas_project_id=7)
    )
    assert "run_canvas" in text and "确认" in text
    assert "只信工具返回" in text
    assert "每次只调一个工具" in text
    assert "id=7" in text, "绑定画布必须写在提示词里，否则模型每次都要先猜"
    # 技能的第一层（名字 + 描述）必须常驻，否则模型不知道有 load_skill 可调
    assert "可用技能" in text and "load_skill" in text


def test_the_system_prompt_scopes_the_context_to_the_agent_project():
    """项目要写进提示词：不同项目的会话不能互相看见对方的进展，模型也不该假装知道。"""
    from app.models import AgentProject

    session = AgentSession(id=1, title="t", model_key="", canvas_project_id=None, agent_project_id=5)
    project = AgentProject(id=5, name="短剧 A · 前期", description="只做选角与设定", canvas_project_id=9)
    text = agent_runner.build_system_prompt(session, project)
    assert "短剧 A · 前期" in text and "id=5" in text
    assert "只做选角与设定" in text
    assert "id=9" in text, "画布应从项目继承"
    assert "别的项目" in text

    # 未归档会话要说清自己是未归档，而不是假装有项目
    loose = agent_runner.build_system_prompt(AgentSession(id=2, title="t", model_key=""))
    assert "未归档" in loose


# ================================================================ 2. 上下文重建


def _step(**kw) -> AgentStep:
    base = dict(id=1, session_id=1, seq=1, role="assistant", content="", tool_name="",
                tool_args="", tool_result="", tier="", status="ok", duration_ms=0, tokens=0)
    base.update(kw)
    return AgentStep(**base)


def test_history_rebuilds_tool_pairs_from_steps():
    """一次工具调用要还原成 `assistant(tool_calls)` + `tool(result)` 两条，id 稳定可复现。"""
    session = AgentSession(id=1, title="t", model_key="", canvas_project_id=None)
    rows = [
        _step(id=1, seq=1, role="user", content="看看画布"),
        _step(id=2, seq=2, role="tool", tool_name="list_projects", tool_args="{}",
              tool_result=json.dumps({"ok": True, "data": {"projects": []}}), tier="read"),
    ]
    msgs = agent_runner.messages_from_steps(session, rows)
    assert msgs[0]["role"] == "system"
    assert msgs[1] == {"role": "user", "content": "看看画布"}
    assert msgs[2]["role"] == "assistant"
    call = msgs[2]["tool_calls"][0]
    assert call["id"] == "call_2" and call["function"]["name"] == "list_projects"
    # 带 tool_calls 的 assistant 消息**不能是空 content**：KIE 这类网关会直接拒绝
    # （Message content is blank），而那是 HTTP 200 里的业务错误，很难看出来
    assert str(msgs[2].get("content") or "").strip(), msgs[2]
    assert msgs[3]["role"] == "tool" and msgs[3]["tool_call_id"] == "call_2"
    assert json.loads(msgs[3]["content"])["ok"] is True


def test_pending_steps_are_not_fed_back_and_rejected_ones_are():
    """等待确认的那步不进上下文；被拒绝的要变成一条「用户拒绝」的结果（让模型换做法）。"""
    session = AgentSession(id=1, title="t", model_key="", canvas_project_id=None)
    pending = _step(id=3, seq=3, role="tool", tool_name="run_canvas", tool_args="{}",
                    tool_result="{}", tier="irreversible", status="pending")
    msgs = agent_runner.messages_from_steps(session, [pending])
    assert len(msgs) == 1, "pending 不该出现在上下文里"

    rejected = _step(id=4, seq=4, role="tool", tool_name="run_canvas", tool_args="{}",
                     tool_result="{}", tier="irreversible", status="rejected")
    msgs = agent_runner.messages_from_steps(session, [rejected])
    tool_msg = next(m for m in msgs if m["role"] == "tool")
    assert "用户拒绝" in tool_msg["content"]


def test_long_tool_results_are_clipped_in_context():
    """大结果只把前 4000 字给模型（指针代替载荷），但库里仍然留着全文。"""
    session = AgentSession(id=1, title="t", model_key="", canvas_project_id=None)
    big = "x" * 9000
    step = _step(id=5, seq=5, role="tool", tool_name="get_canvas", tool_args="{}",
                 tool_result=big, tier="read")
    msgs = agent_runner.messages_from_steps(session, [step])
    tool_msg = next(m for m in msgs if m["role"] == "tool")
    assert len(tool_msg["content"]) < len(big)
    assert "截断" in tool_msg["content"]


# ================================================================ 3. 草稿落定的文档形状


def _draft() -> dict:
    return {
        "nodes": [
            {"id": "n1", "kind": "idea", "data": {"prompt": "外卖侠", "shotCount": 8},
             "position": {"x": 0, "y": 0}},
            {"id": "n2", "kind": "storyboard", "data": {"prompt": ""},
             "position": {"x": 320, "y": 0}},
        ],
        "edges": [{"from": "n1", "to": "n2"}],
    }


class _Project:
    canvas_json = None
    name = "测试"


def test_draft_becomes_a_valid_canvas_document():
    """落库形状必须与前端保存的一致：`type` 是真实节点类型，`schema` 不落库，边缘带 handle。"""
    doc = agent_tools._doc_from_draft(
        _draft(), _Project(), {"idea": "2:deepseek-chat", "storyboard": "2:deepseek-chat"}
    )
    assert {n["type"] for n in doc["nodes"]} == {"idea", "storyboard"}
    for n in doc["nodes"]:
        assert n["data"]["nodeType"] == n["type"]
        assert "schema" not in n["data"], "运行时缓存不该进库"
        assert n["data"]["model_key"] == "2:deepseek-chat"
    assert doc["edges"][0]["sourceHandle"] == NODE_SCHEMAS["idea"]["handles"]["sources"][0]["id"]
    assert doc["edges"][0]["targetHandle"] == NODE_SCHEMAS["storyboard"]["handles"]["targets"][0]["id"]
    nodes, edges = validate_document(doc)
    assert len(nodes) == 2 and len(edges) == 1


def test_appending_a_draft_shifts_it_right_of_existing_content():
    """第二次落草稿不能压在旧节点上（用户看到的是「画布被搞乱了」）。"""
    class _Busy:
        canvas_json = json.dumps({
            "schemaVersion": 1,
            "nodes": [{"id": "old", "type": "text", "position": {"x": 1200, "y": 0}, "data": {}}],
            "edges": [],
            "viewport": {},
        })
        name = "测试"

    doc = agent_tools._doc_from_draft(
        _draft(), _Busy(), {"idea": "", "storyboard": ""}
    )
    new_nodes = [n for n in doc["nodes"] if n["id"] != "old"]
    assert all(n["position"]["x"] > 1200 for n in new_nodes), [n["position"] for n in new_nodes]


def test_a_draft_with_unknown_kinds_is_dropped_not_crashing():
    draft = {
        "nodes": [{"id": "n1", "kind": "no_such_kind", "data": {}, "position": {"x": 0, "y": 0}}],
        "edges": [],
    }
    doc = agent_tools._doc_from_draft(draft, _Project(), {})
    assert doc["nodes"] == []


# ================================================================ 4. 接线


def test_the_router_and_page_are_wired():
    main = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    api = (BACKEND.parent / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
    page = (BACKEND.parent / "frontend" / "src" / "pages" / "AgentPage.tsx").read_text(
        encoding="utf-8"
    )
    assert "agent.router" in main, "路由没注册"
    assert '"/api/agent/meta"' in api and "/api/agent/sessions" in api
    assert "/api/agent/projects" in api, "项目两个接口没接"
    assert "agentSend" in api and "agentConfirm" in api
    assert "还没做" not in page, "占位页文案还在，说明页面没换掉"
    assert "确认执行" in page, "缺少不可逆操作的确认入口"
    assert "新任务" in page, "缺任务入口"
    assert "技能（按需加载" in page, "能力面板里没有技能分组"

    migration = BACKEND / "alembic" / "versions" / "0018_agent_tables.py"
    assert migration.exists(), "缺少会话/步骤两张表的迁移"
    text = migration.read_text(encoding="utf-8")
    assert "agent_sessions" in text and "agent_steps" in text

    projects = BACKEND / "alembic" / "versions" / "0019_agent_projects.py"
    assert projects.exists(), "缺少 Agent 项目表的迁移"
    ptext = projects.read_text(encoding="utf-8")
    assert "agent_projects" in ptext and "agent_project_id" in ptext


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
