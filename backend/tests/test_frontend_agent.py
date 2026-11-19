"""「AI 搭画布」入口收进 Agent 后的前端静态检查。

运行：venv/Scripts/python tests/test_frontend_agent.py

背景：画布页原先有一个「AI 搭画布」按钮，用一句话生成节点草稿。Agent 上线后
它和 Agent 里的 `draft_canvas` 是同一件事的两套入口——用户不知道该点哪个，
维护也要改两处。现在口径是：

1. **画布页不再有这条入口**：按钮、弹窗、草稿类型与请求函数都删掉；
2. **后端 planner 还在**：它仍是 Agent 工具 `draft_canvas` 的实现，
   只是不再由画布页直接调用；
3. **降级路径必须留着**：不支持工具调用的模型仍然要能干活——自动链、模板、
   手动添加节点、分享导入，这四条一个都不能少。

这里的断言全是静态的：删干净之后如果有人又把它加回来，测试会立刻红。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
CANVAS = SRC / "pages" / "CanvasPage.tsx"
API = SRC / "api.ts"
TYPES = SRC / "types.ts"
BACKEND_AGENT = ROOT / "backend" / "app" / "services" / "canvas_agent.py"
BACKEND_ROUTER = ROOT / "backend" / "app" / "routers" / "canvas.py"
BACKEND_TOOLS = ROOT / "backend" / "app" / "services" / "agent_tools.py"
SKILL_SHORT_VIDEO = ROOT / "backend" / "app" / "skills" / "short-video.md"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_the_canvas_toolbar_no_longer_has_its_own_agent_entry():
    """按钮、弹窗、草稿预览一并消失——半留半删比不删更糟。"""
    src = _text(CANVAS)
    for gone in ("AI 搭画布", "function AgentDialog", "DraftMiniMap", "AGENT_FAIL_TITLE"):
        assert gone not in src, f"画布页里还有 {gone}，入口没收干净"
    for gone in ("setAgentOpen", "applyAgentDraft", "canvasAgentPlan"):
        assert gone not in src, f"画布页里还在调用 {gone}"


def test_the_frontend_no_longer_requests_plans_directly():
    """草稿只经 Agent 的 draft_canvas 生成，前端不再单独发这个请求。"""
    assert "canvasAgentPlan" not in _text(API), "api.ts 里还留着 canvasAgentPlan"
    assert "CanvasAgentDraft" not in _text(TYPES), "types.ts 里还留着草稿类型"


def test_the_backend_planner_is_still_there_for_the_agent_tool():
    """planner 没被误删：Agent 的 draft_canvas 还要调它。"""
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
    """「添加节点」不再列出配方节点（旧分镜图）：新画布统一用图片组。"""
    src = _text(CANVAS)
    menu = src[src.index("canvas-addmenu-title"):]
    menu = menu[: menu.index("</div>", menu.index("</button>")) + 6]
    assert "s.hidden" in menu, "添加节点菜单没有按 hidden 过滤"
    assert "storyboardImage" not in menu, "添加节点菜单里还在硬编码分镜图"


def test_the_shared_landing_helper_is_still_used_by_import():
    """落定逻辑只剩分享导入一个调用方了，但错开/重盖章/补模型这些性质不能丢。"""
    src = _text(CANVAS)
    helper = src[src.index("const mergeCanvasContent"):]
    helper = helper[: helper.index("const applyImportedDoc")]
    for needed in ("Date.now().toString(36)", "modelKeyForNodeType(", "setDirty(true)"):
        assert needed in helper, f"落定逻辑里缺 {needed}"


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
