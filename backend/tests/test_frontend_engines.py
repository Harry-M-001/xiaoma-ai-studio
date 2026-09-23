"""本机引擎页（前端接线）的静态检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_frontend_engines.py

这一页的「安装程序」那一档（就是去字幕高质量档，`#44`）最容易错在**下完之后**：

用户下的是一个 731MB 的独立安装程序，装与跑都在它自己的界面里，**我们不代装也不代跑**。
所以下完之后唯一有用的结论是「文件在哪、接下来做什么」——原来下完整了什么都不说，
只多出一个「校验」按钮，用户找不到那份安装包。这类「做了事却不说结果」比报错更难查：
报错至少知道错了。

另外按钮文字要与状态对得上：已经下过的用户看到的若是「下载安装包」，他点下去
才会发现是重下 731MB。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
PAGE = SRC / "pages" / "EnginesPage.tsx"
STYLES = SRC / "styles.css"
TYPES = SRC / "types.ts"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_the_installer_tier_says_where_the_file_landed():
    """下完之后必须把安装包的路径写出来，并说清接下来做什么。

    这一档我们不代装：用户下完 731MB 之后要自己去双击它。没有路径，他就只剩
    「在硬盘里翻」这一条路。
    """
    src = _text(PAGE)
    assert 'item.archive === "installer"' in src, "没区分「安装程序」这一档"
    assert "item.archiveComplete" in src, "没看安装包下完整了没有"
    assert "item.archivePath" in src, "下完了没说安装包在哪（用户会找不到那 731MB）"
    assert "拖回导演台" in src, "没说装完之后怎么接回流程"
    # 路径是长串，要能断行，不能把这一条撑开
    assert ".eng-path" in _text(STYLES), "缺长路径的断行样式"


def test_the_download_button_says_what_it_will_do():
    """按钮文字要跟当前状态对得上，四个分支对应四种真实状态。"""
    src = _text(PAGE)
    assert "downloadLabel" in src, "按钮文字没有按状态算"
    for label in ("接着下载", "下载并安装", "下载安装包", "重新下载安装包"):
        assert label in src, f"少了「{label}」这一档"


def test_the_page_never_invents_an_installed_verdict_for_the_installer_tier():
    """「已装好」只按 `installed`（标志文件命中）说。

    安装程序那一档的 `marker` 是空的——它的目录不归我们管，**判不出来**。
    界面不许在没判据的地方编一个结论出来。
    """
    src = _text(PAGE)
    assert "item.installed" in src, "没按 installed 判断装没装"
    assert "已装好" in src, "没给出「已装好」这个状态"
    # 「已装好」那句必须在 installed 为真的分支里，且带上命中的文件
    assert "已装好：{item.installedMarker}" in src, (
        "「已装好」没带上标志文件——那样它与「安装程序下好了」就分不出来了"
    )


def test_the_engine_row_type_matches_the_backend():
    """后端回的字段名不许在前端被改名（改名不会报错，只会静默变成 undefined）。"""
    types = _text(TYPES)
    for field in ("archiveBytes", "archiveComplete", "archivePath", "installedMarker", "downloading"):
        assert field in types, f"类型里没有 {field}"


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
