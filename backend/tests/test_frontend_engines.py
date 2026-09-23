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


# ------------------------------------------------- 解锁门槛（#45）


def test_the_undownloadable_tier_gets_a_link_instead_of_a_download_button():
    """「不给下载」那一档（`archive === "external"`）不能出现下载按钮。

    后端也会拦（`start()` 直接拒绝），但界面是第一道：给一个点不出东西的按钮，
    用户只会以为程序坏了。这里换成官方安装说明的外链——那一档的用法在它自己的文档里。
    """
    src = _text(PAGE)
    assert 'item.archive === "external" ?' in src, "没有按「不给下载」分支出不同的动作"
    assert "官方安装说明" in src, "没给出官方安装说明的入口"
    assert "item.homepage" in src, "外链没指向项目主页"
    # 下载按钮必须在这个分支**之外**：
    start = src.index('item.archive === "external" ?')
    link_branch = src[start : src.index(") : item.downloading ?", start)]
    assert "onDownload" not in link_branch, "「不给下载」那一档还挂着下载动作"


def test_the_page_never_claims_a_hash_we_do_not_have():
    """没有包就没有摘要：这时不许写「sha256：上游摘要」。

    那一档 `proof` 是空串，而原来的写法是「不是 local-download 就写上游摘要」——
    等于给一个不存在的摘要编了个来源。
    """
    src = _text(PAGE)
    assert "{item.proof && (" in src, "没按「有没有摘要」判断（空 proof 会渲染成上游摘要）"


def test_the_hardware_row_shows_the_vram_and_the_gate():
    """显存要摆出来：它既是硬件事实，也是这一档解锁门槛的依据。"""
    src = _text(PAGE)
    assert "hw.vramText" in src, "硬件那一行没有显存"
    assert "item.minVramGb" in src, "卡片上没有把门槛写出来"


def test_a_hidden_tier_is_still_accounted_for():
    """没显示的档要在页尾交代一句。

    不够门槛的档是整行不下发的（后端 `locked_rows` 与 `rows` 互补）。不说的话，
    用户会看到「别人的界面里有一档、我这台没有」——**沉默最难解释**。
    """
    src = _text(PAGE)
    assert "data.lockedTiers.map(" in src, "没渲染「被门槛拦下的那几档」"
    assert "还有一档按硬件门槛显示" in src, "没说清「这档没显示」是怎么回事"
    assert "t.reason" in src, "没带上「为什么没显示」"
    types = _text(TYPES)
    assert "EngineLockedTier" in types and "lockedTiers" in types, "类型里没有 lockedTiers"
    for field in ("minVramGb", "vramText", "vramGb"):
        assert field in types, f"类型里没有 {field}"
    assert ".eng-locked" in _text(STYLES), "缺「没显示」那一条的样式"


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
