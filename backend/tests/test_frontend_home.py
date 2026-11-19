"""首页信息层级改造的前端静态检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_frontend_home.py

这一版首页与 dsh 的空状态对齐：**一个输入框 + 往下挪的创作入口**。

1. **输入框提交后交给 Agent**：按所选画布建任务、发送、跳过去；不允许只跳到一个空页面。
2. **首页不再有营销口号**（「探索未知之境」那类），也不再把某个功能写死成主按钮。
3. **点「最近画布」要直接进那个画布**；没有画布时要有空状态与出口。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
HOME = SRC / "pages" / "HomePage.tsx"
STYLES = SRC / "styles.css"


def _home() -> str:
    return HOME.read_text(encoding="utf-8")


def test_recent_project_opens_the_canvas():
    """最近画布卡片必须能一步进画布（拿不到回调时才退回画布列表）。"""
    src = _home()
    assert "onOpenCanvas(p)" in src, (
        "首页的最近画布又没有直接打开——用户得多点一次才能接着做"
    )
    assert "onOpenCanvas ? onOpenCanvas(p) : onNavigate(\"projects\")" in src, (
        "没有对「拿不到 onOpenCanvas」的降级：那种情况下会点了没反应"
    )


def test_home_submits_to_the_agent():
    """输入框不是装饰：提交要建 Agent 任务、发出去、再跳到那个任务。"""
    src = _home()
    assert "createAgentSession" in src, "首页没有建 Agent 任务"
    assert "agentSend" in src, "首页没有把这句话发给 Agent"
    assert "onOpenAgentSession" in src, "发送完没有跳转到那条任务"
    assert "agent_project_id: agentProjectId" in src, "没有把所选项目带进新任务"
    assert "project_id: canvasId" not in src, "首页还在直接绑画布；任务应该挂项目"
    # 没有可用模型时不许装作能发
    assert "meta?.ready" in src and "去模型服务" in src, "缺少「没接模型」的拦截与出口"


def test_home_offers_project_choice_and_creation():
    """首页选的是**项目**：可选已有，也可就地新建（不再直接绑画布）。"""
    src = _home()
    assert "createAgentProject" in src, "首页不能就地新建项目"
    assert "meta?.projects" in src, "项目下拉没有读 Agent 项目列表"
    assert "＋ 新建项目" in src, "项目下拉里没有「新建项目」入口"
    assert "home-new-project" in src, "新建项目的输入行没有样式钩子"


def test_home_has_manual_and_auto_approval():
    """审批方式要在首页就能选：手动 / 自动；旧提示语删掉。"""
    src = _home()
    assert "手动审批" in src and "自动审批" in src, "首页没有审批方式切换"
    assert "auto_approve: autoApprove" in src, "审批方式没有带进新任务"
    assert "花钱的操作会先停下来问你" not in src, "旧提示语还在"


def test_home_has_no_marketing_slogan():
    """首页不再用口号占位（用户明确要求去掉「探索未知之境」那类文案）。"""
    src = _home()
    for stale in ("探索未知之境", "探索未来之境", "一个人，一整个"):
        assert stale not in src, f"首页还留着口号：{stale}"


def test_empty_state_has_a_way_forward():
    """没有画布时要有引导与出口，不能什么都不显示。"""
    src = _home()
    assert "projects.length === 0" in src, "没有画布时首页缺少空状态"
    assert "home-empty" in src, "空状态没有样式钩子（会和普通卡片混在一起）"
    assert "铺一条示例" in src and "我要自己建" in src, "空状态没有给出下一步"


def test_enter_animation_respects_reduced_motion():
    """入场动效必须能被系统的「减少动态效果」关掉。"""
    css = STYLES.read_text(encoding="utf-8")
    assert ".home-enter" in css, "首页卡片没有入场动效的钩子"
    assert "prefers-reduced-motion" in css, "入场动效没有尊重 prefers-reduced-motion"
    # 关掉的那条必须在 reduce 查询里
    idx = css.index("prefers-reduced-motion")
    assert ".home-enter" in css[idx : idx + 200], (
        "prefers-reduced-motion 里没有关掉 .home-enter 的动画"
    )


def test_create_pages_seed_the_first_prompt():
    """创作页的空状态要给「点一下就能用」的示例。

    图片页与视频页最容易卡在「画面描述该写什么」——空白输入框 + 一排参数，
    新手不知道从哪下手。示例必须真的**能被点进输入框**，而不是只写一句引导。
    """
    css = STYLES.read_text(encoding="utf-8")
    assert ".prompt-example" in css, "示例提示词没有样式钩子"

    for rel in ("pages/ImagePage.tsx", "pages/VideoPage.tsx"):
        src = (SRC / rel).read_text(encoding="utf-8")
        assert "PROMPT_EXAMPLES" in src, f"{rel} 的空状态没有示例"
        assert "prompt-examples" in src, f"{rel} 没有渲染示例区"
        assert "setPrompt(ex)" in src, f"{rel} 的示例点了不会填进输入框（那只是个摆设）"


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
