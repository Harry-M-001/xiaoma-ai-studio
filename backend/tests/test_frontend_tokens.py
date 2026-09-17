"""前端设计 token 的静态检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_frontend_tokens.py

为什么值得单独钉一条：`var(--typo)` 这种写法**不会报错**，CSS 只是把那条声明整条丢掉，
于是界面看起来「样式莫名不对」，而且构建、类型检查、单测全绿。
写这个测试的当天就真写错过一个（`--accent` 实际叫 `--primary`）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS = ROOT / "frontend" / "src" / "styles.css"

# 浏览器内置 / 无法在文件里定义的自定义属性白名单（真在用的就这些）
_KNOWN_EXTERNAL = {
    "--vscode-font-family",  # 万一将来嵌进 IDE 预览
}


def _defined(text: str) -> set[str]:
    return set(re.findall(r"(--[a-z0-9-]+)\s*:", text, re.I))


def _used(text: str) -> set[str]:
    return set(re.findall(r"var\(\s*(--[a-z0-9-]+)", text, re.I))


def test_every_var_is_defined():
    """用到的自定义属性必须在同一个文件里定义过（带降级值的 var() 除外）。

    带降级值（`var(--x, 12px)`）的调用按设计可以在未定义时退化，所以只查裸调用。
    """
    text = CSS.read_text(encoding="utf-8")
    defined = _defined(text)
    bare = set(re.findall(r"var\(\s*(--[a-z0-9-]+)\s*\)", text, re.I))
    missing = sorted(bare - defined - _KNOWN_EXTERNAL)
    assert not missing, f"styles.css 里用了没定义的 CSS 变量：{missing}"


def test_both_themes_define_the_same_tokens():
    """明暗两套主题要定义同一批**配色**变量。

    少一个的症状是「切到暗色后某一处颜色没变」——不影响功能，但很显眼，
    而且这种不一致只在手动点过主题切换之后才发现。

    字体、圆角、栏宽这些与主题无关的 token 只写在 `:root` 里，属于设计意图，
    所以按名单放行；名单之外的不一致一律判错。
    """
    text = CSS.read_text(encoding="utf-8")
    blocks = re.findall(r'(?::root|\[data-theme="dark"\])\s*\{(.*?)\}', text, re.S)
    assert len(blocks) >= 2, '没有找到两套主题的 token 定义（:root 与 [data-theme="dark"]）'
    light, dark = _defined(blocks[0]), _defined(blocks[1])

    theme_independent = {
        "--font-sans",
        "--font-mono",
        "--radius-sm",
        "--radius",
        "--radius-lg",
        "--sidebar-w",
    }
    missing_in_dark = sorted(light - dark - theme_independent)
    missing_in_light = sorted(dark - light - theme_independent)
    assert not missing_in_dark, f"暗色主题缺少这些变量：{missing_in_dark}"
    assert not missing_in_light, f"亮色主题缺少这些变量：{missing_in_light}"


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
