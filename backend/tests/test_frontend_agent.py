"""画布页「一句话搭画布」入口与 Agent 的分工（前端静态检查）。

运行：venv/Scripts/python tests/test_frontend_agent.py

背景：画布页曾有一个「AI 搭画布」按钮，一度被整条删掉、只留 Agent 里的
`draft_canvas`。但用户站在画布上想「一句话铺一条链」时，还得跳去 Agent 再回来，
多一道门槛。现在的口径是：

1. **入口回到画布**：工具栏有「一句话搭画布」，出草稿、看预览、点落定；
2. **实现只有一份**：它调的就是 Agent `draft_canvas` 用的同一个后端 planner
   （`/api/canvas/agent/plan`），前端不许自己写第二套拓扑/布局；
3. **降级路径必须留着**：不支持工具调用的模型仍然要能干活——自动链、模板、
   手动添加节点、分享导入，这四条一个都不能少。

这里的断言全是静态的：有人把入口又删了、或者另起一套 planner，测试会立刻红。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
CANVAS = SRC / "pages" / "CanvasPage.tsx"
AGENT_PAGE = SRC / "pages" / "AgentPage.tsx"
API = SRC / "api.ts"
TYPES = SRC / "types.ts"
BACKEND_AGENT = ROOT / "backend" / "app" / "services" / "canvas_agent.py"
BACKEND_ROUTER = ROOT / "backend" / "app" / "routers" / "canvas.py"
BACKEND_TOOLS = ROOT / "backend" / "app" / "services" / "agent_tools.py"
SKILL_SHORT_VIDEO = ROOT / "backend" / "app" / "skills" / "short-video.md"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_the_canvas_has_a_one_sentence_draft_entry():
    """按钮、弹窗、草稿预览与落定回调都要在——只留个按钮等于没做。"""
    src = _text(CANVAS)
    for needed in ("一句话搭画布", "function AgentDialog", "DraftMiniMap", "applyAgentDraft"):
        assert needed in src, f"画布页缺 {needed}，入口不完整"


def test_the_frontend_plan_request_is_shared_with_the_agent():
    """草稿只经后端 planner 生成；前端请求函数与草稿类型缺一不可。"""
    assert "canvasAgentPlan" in _text(API), "api.ts 少了 canvasAgentPlan"
    assert "/api/canvas/agent/plan" in _text(API), "前端请求没有打到 Agent 那同一个 planner"
    assert "CanvasAgentDraft" in _text(TYPES), "types.ts 少了草稿类型"


def test_the_backend_planner_is_the_single_implementation():
    """planner 同时服务 Agent 的 draft_canvas 与画布页入口，不许删其中一边。"""
    assert "async def plan(" in _text(BACKEND_AGENT)
    assert '"/agent/plan"' in _text(BACKEND_ROUTER), "后端路由被误删了"
    tools = _text(BACKEND_TOOLS)
    assert '"draft_canvas"' in tools and "canvas_agent.plan(" in tools
    assert '"apply_canvas_draft"' in tools, "草稿落定的工具也不该消失"


def test_the_agent_is_told_to_use_the_draft_tools():
    """技能文档是模型的行为指南，入口搬进 Agent 后这里必须跟上。"""
    skill = _text(SKILL_SHORT_VIDEO)
    assert "draft_canvas" in skill, "短剧技能没有引导模型用 draft_canvas"


def test_manual_paths_stay_available_for_models_without_tool_calling():
    """非工具模型也得能把画布搭起来：自动链 / 添加节点 / 分享导入，缺一不可。"""
    src = _text(CANVAS)
    for needed in ("自动链", "添加节点", "分享"):
        assert needed in src, f"画布页少了「{needed}」这条手动路径"


def test_new_node_menu_skips_recipe_kinds():
    """「添加节点」不再列出内部原语（图片组）：新画布统一从分镜图进入。"""
    src = _text(CANVAS)
    menu = src[src.index("canvas-addmenu-title"):]
    menu = menu[: menu.index("</div>", menu.index("</button>")) + 6]
    assert "s.hidden" in menu and "s.menuHidden" in menu, "添加节点菜单没有按 hidden/menuHidden 过滤"
    assert "storyboardImage" not in menu, "添加节点菜单里还在硬编码分镜图"


def test_the_shared_landing_helper_is_still_used_by_import():
    """落定逻辑只剩分享导入一个调用方了，但错开/重盖章/补模型这些性质不能丢。"""
    src = _text(CANVAS)
    helper = src[src.index("const mergeCanvasContent"):]
    helper = helper[: helper.index("const applyImportedDoc")]
    for needed in ("Date.now().toString(36)", "modelKeyForNodeType(", "setDirty(true)"):
        assert needed in helper, f"落定逻辑里缺 {needed}"


def test_the_agent_timeline_shows_what_it_did_to_the_canvas():
    """Agent 读/改/跑画布时，对话里要看得见对象与结果，而不是一坨 JSON。

    守三件事：画布工具有一张人话卡片、页顶有条监视条、卡片能直接打开画布。
    """
    src = _text(AGENT_PAGE)
    for needed in (
        "function CanvasActivityCard",
        "function CanvasMonitor",
        '"agent-canvas-card"',
        "get_canvas",
        "apply_canvas_draft",
        "run_canvas",
        "canvas_status",
        "onOpenCanvas",
    ):
        assert needed in src, f"Agent 页缺「{needed}」，画布活动又只能看 JSON 了"


def test_the_agent_has_three_slash_commands_and_a_stop_button():
    """输入 / 弹三条命令；/goal 的持续监测要能看见、能停。"""
    src = _text(AGENT_PAGE)
    for needed in (
        "SLASH_ITEMS",
        "Spec",
        "Plan",
        "Goal",
        "agent-slash",
        "selectSlash",
        "持续监测中",
        "stopAgentGoal",
        "goalRunning",
    ):
        assert needed in src, f"Agent 页缺「{needed}」"
    assert "stopAgentGoal" in _text(API), "api.ts 少了停止持续监测的接口"


def test_the_agent_polls_only_while_a_turn_or_a_run_is_in_flight():
    """请求进行中要逐步回读会话；画布只在真有任务在跑时继续轮询。

    没有这条节制，打开一个老会话就会每 2 秒打两次接口，永远停不下来。
    """
    src = _text(AGENT_PAGE)
    assert "getAgentSession(sid)" in src, "请求进行中没有回读会话，思考与工具调用不会实时出现"
    assert "polls < 900" in src, "画布轮询没有停止条件"
    assert "RUNNING_STATUS" in src, "没有按任务状态判断「还在跑」"


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
