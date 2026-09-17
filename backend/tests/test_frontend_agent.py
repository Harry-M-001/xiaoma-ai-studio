"""AI 搭画布（#19）与 Agent 结果协议（#22）的前端静态检查。

运行：venv/Scripts/python tests/test_frontend_agent.py

后端那侧已经钉住了「宽容修复」和「结果协议」本身（见 test_canvas_agent.py）。
这里守的是**前端有没有按协议用**，因为协议一旦被绕过，功能会以最难查的方式坏掉：

1. **草稿必须经确认才落库**。「AI 搭画布」的整条价值就在这个确认动作上：
   如果生成即落库，用户会得到一张自己没同意的画布，而自动保存会立刻把它写进库里。
2. **落定必须标脏**。不标脏 = 不触发自动保存 = 用户看到节点在画布上、刷新后没了。
3. **与已有内容错开**。草稿坐标从固定原点算起，直接落上去会正好压在旧节点上。
4. **每个后端状态都要有对应的说法**。少一个，用户就会看到一块空白区域。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
CANVAS = SRC / "pages" / "CanvasPage.tsx"
API = SRC / "api.ts"
BACKEND_AGENT = ROOT / "backend" / "app" / "services" / "canvas_agent.py"
BACKEND_ROUTER = ROOT / "backend" / "app" / "routers" / "canvas.py"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_frontend_and_backend_agree_on_the_endpoint():
    api = _text(API)
    router = _text(BACKEND_ROUTER)
    assert '"/api/canvas/agent/plan"' in api, "api.ts 里的地址变了"
    assert '"/agent/plan"' in router, "后端没有这个路由"
    assert "canvasAgentPlan:" in api and "api.canvasAgentPlan(" in _text(CANVAS)


def test_the_apply_button_is_gated_on_status_ok():
    """只有 ok 才给「放到画布上」。其余状态一律不给入口。"""
    src = _text(CANVAS)
    apply_idx = src.index("放到画布上")
    gate_idx = src.rindex("{ok && (", 0, apply_idx)
    assert gate_idx > 0, "「放到画布上」没有被 ok 判定包住"

    dialog = src[src.index("function AgentDialog(") :]
    # 生成本身绝不改动画布：onApply 只能在那个按钮里出现一次
    assert dialog.count("onApply(") == 1, "onApply 在别处被调用了，草稿可能未经确认就落库"


def test_generating_a_draft_does_not_touch_the_canvas():
    """生成草稿这一步只能是「请求 + 展示」，不能顺手往画布上写东西。"""
    src = _text(CANVAS)
    body = src[src.index("function AgentDialog(") : src.index("/** 节点属性浮框")]
    for forbidden in ("setNodes(", "setEdges(", "setDirty(", "api.saveCanvas("):
        assert forbidden not in body, f"搭画布对话框里出现了 {forbidden}，生成不该动画布"


def _agent_apply() -> str:
    src = _text(CANVAS)
    body = src[src.index("const applyAgentDraft"):]
    return body[: body.index("const applyImportedDoc")]


def test_applying_delegates_to_the_shared_landing_helper():
    """落定的三件事（错开 / 重盖章 / 补模型）现在由共用函数负责，两处不再各写一份。

    这里只钉「确实走的是那一份」；那些性质本身由 test_frontend_share.py 钉住，
    免得同一条断言在两个文件里各写一遍、以后只改一处。
    """
    body = _agent_apply()
    assert "mergeCanvasContent(" in body, "AI 草稿没有走共用的落定逻辑"
    for forbidden in ("setNodes(", "setEdges("):
        assert forbidden not in body, f"落定细节漏回了 applyAgentDraft（{forbidden}）"


def test_applying_marks_the_canvas_dirty_and_closes_the_dialog():
    """不标脏就不会自动保存；不关弹窗用户会以为没生效。"""
    src = _text(CANVAS)
    helper = src[src.index("const mergeCanvasContent"):]
    helper = helper[: helper.index("const applyAgentDraft")]
    assert "setDirty(true)" in helper
    assert "setAgentOpen(false)" in _agent_apply()


def test_applying_offsets_away_from_existing_content():
    """已有节点时外来内容必须平移，否则会正好压在旧内容上。"""
    src = _text(CANVAS)
    helper = src[src.index("const mergeCanvasContent"):]
    helper = helper[: helper.index("const applyAgentDraft")]
    assert "existing.length > 0" in helper, "没有判断画布是否已有内容"
    assert "shift" in helper and "position: { x: n.position.x + shift.x" in helper
    assert "Math.max(...existing.map((n) => n.position.x))" in helper, "没有按现有内容的最右侧平移"


def test_applying_ids_are_stamped_so_repeat_runs_do_not_collide():
    src = _text(CANVAS)
    helper = src[src.index("const mergeCanvasContent"):]
    helper = helper[: helper.index("const applyAgentDraft")]
    assert "Date.now().toString(36)" in helper and "im_${raw}_${stamp}" in helper


def test_every_backend_status_has_a_frontend_saying():
    """后端的结果协议加了状态而前端没跟上时，用户会看到一块空白的失败区。"""
    backend = _text(BACKEND_AGENT)
    statuses = re.findall(r"^STATUS_\w+ = \"(\w+)\"", backend, re.M)
    assert len(statuses) >= 4, f"后端状态没取到：{statuses}"

    src = _text(CANVAS)
    table = src[src.index("const AGENT_FAIL_TITLE"):]
    table = table[: table.index("};")]
    for status in statuses:
        assert f"{status}:" in table, f"前端没有处理状态 {status}"


def test_status_is_not_rendered_by_string_matching():
    """状态必须按 code 判断，不能去匹配报错文案——文案一改，判断就悄悄失效。"""
    src = _text(CANVAS)
    body = src[src.index("function AgentDialog(") : src.index("/** 节点属性浮框")]
    assert 'draft.status === "ok"' in body or "draft?.status === \"ok\"" in body
    assert not re.search(r"status\.(includes|startsWith)\(", body), "在按文案猜状态"


def test_model_keys_are_filled_by_the_shared_helper():
    """落内容时按类型填模型，且用的是与自动链同一份 helper（两处各写一遍必漂）。"""
    src = _text(CANVAS)
    helper = src[src.index("const mergeCanvasContent"):]
    helper = helper[: helper.index("const applyAgentDraft")]
    assert "modelKeyForNodeType(n.type, models)" in helper

    chain = _text(SRC / "canvasChain.ts")
    assert "export function modelKeyForNodeType" in chain
    # 自动链也用它，不再自带一份
    assert "modelKeyForNodeType(type, models)" in chain


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
