"""首页信息层级改造的前端静态检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_frontend_home.py

首页这一版改的三件事，都容易被后来的改动悄悄退回去，所以各钉一条：

1. **点「最近项目」要直接进那个项目的画布**。以前它只跳到项目列表页——想接着做的人
   还得多点一次；而组件本来就拿到了 `onOpenCanvas`，只是没用在这一处。
2. **主行动按钮要随状态变化**（有作品 → 继续；没作品没模型 → 先接入；其余 → 从示例开始）。
   固定成某个功能的入口（原来是"开始创作"直跳图片生成）对新手与老用户都不合适。
3. **没有项目时要有空状态**，而不是整块区域消失（页面会缺一截，且没有任何引导）。
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
    """最近项目卡片必须能一步进画布（拿不到回调时才退回项目列表）。"""
    src = _home()
    assert "onOpenCanvas(p)" in src, (
        "首页的最近项目又没有直接打开画布——用户得多点一次才能接着做"
    )
    assert "onOpenCanvas ? onOpenCanvas(p) : onNavigate(\"projects\")" in src, (
        "没有对「拿不到 onOpenCanvas」的降级：那种情况下会点了没反应"
    )


def test_primary_action_follows_state():
    """主行动按钮必须按状态分三种，而不是写死一个入口。"""
    src = _home()
    assert "继续「" in src, "没有「继续上一件作品」这一态"
    assert "先接入一个模型" in src, "没有「先去接入模型」这一态（新手会点什么都撞同一个错）"
    assert "从示例开始" in src, "没有「从示例开始」这一态"
    assert "setup.ready" in src, "主行动没有依据「有没有可用模型」来判断"


def test_empty_state_has_a_way_forward():
    """没有项目时要有引导与出口，不能什么都不显示。"""
    src = _home()
    assert "projects.length === 0" in src, "没有项目时首页缺少空状态"
    assert "home-empty" in src, "空状态没有样式钩子（会和普通卡片混在一起）"
    # 空状态里必须至少有一个能往前走的按钮
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
