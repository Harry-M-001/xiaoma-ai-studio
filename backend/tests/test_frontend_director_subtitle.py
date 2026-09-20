"""导演台「字幕」的前端静态检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_frontend_director_subtitle.py

这一项在界面上最容易做错的四件事：

1. **把版式表抄一份到前端**。抄了就可能出现「界面能选出一种后端不认识的版式」，
   而报错要到导出时才知道。所以选项必须来自 `GET /api/subtitles/options`。
2. **预览用前端 CSS 近似画**。版式取决于 libass 的字体选择、字形、描边、缩放、边距，
   前端再写一套必然对不上——「预览看着挺好、导出来不一样」是最伤信任的一种不一致。
   所以预览必须走后端真渲染那个接口。
3. **「版式跟画风」被写成强制**。版式是**用户直接选的**，画风只给默认值；
   用户选过之后不许被别的东西改掉（与产物回滚、候选定稿同一个口径）。
4. **字体缺了只报错、不给下载入口**。那用户就只能干看着。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "frontend" / "src" / "pages" / "DirectorPage.tsx"
API = ROOT / "frontend" / "src" / "api.ts"


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def _panel(src: str) -> str:
    start = src.index("function SubtitlePanel(")
    nxt = src.find("\n/**\n * 导演台（页面壳）", start)
    return src[start : nxt if nxt != -1 else len(src)]


def test_options_come_from_the_backend_not_a_local_copy():
    """版式与字体选项必须来自后端，前端不许自己抄一张表。"""
    src = _page()
    assert ".subtitleOptions()" in src, "没有去后端拿字幕选项"
    assert "options.styles.map" in src, "下拉不是由后端给的版式生成的"
    panel = _panel(src)
    for hardcoded in ('"doc_bottom"', "'doc_bottom'", '"variety_pop"', "'variety_pop'", '"guofeng_kai"'):
        assert hardcoded not in panel, (
            f"前端硬编码了版式 {hardcoded}——抄一份的话，界面能选出后端不认识的东西，"
            "报错要到导出时才知道"
        )


def test_preview_is_rendered_by_the_backend():
    """预览必须走后端真渲染那一帧，不许在前端用 CSS 画。"""
    panel = _panel(_page())
    assert "api.subtitlePreview(" in panel, "预览没走后端"
    assert "text: \"\"" in panel, "预览要用后端的内置示例文本（带繁体与生僻字，能暴露缺字）"
    # 不许出现「自己排版式」的痕迹
    for forbidden in ("textShadow", "fontFamily", "WebkitTextStroke"):
        assert forbidden not in panel, f"前端在自己画版式（{forbidden}）——那一定和导出结果对不上"


def test_font_can_be_downloaded_right_where_it_is_missing():
    """字体缺了要就地能下，只报一句错等于把用户卡住。"""
    panel = _panel(_page())
    assert "api.downloadSubtitleFont(" in panel, "没有下载字体的入口"
    assert "fontReady" in panel, "没有按版式检查它的字体在不在本机"
    assert "downloadBytes" in _page() or "fontLabel" in panel, "没说清要下的是哪一款"


def test_download_replaces_the_whole_options_object():
    """下完要**整份替换**选项，不能只换字体列表。

    踩过：接口只回字体列表，而版式里的 `fontReady` 还是页面加载时那一次的老值，
    于是界面显示「下完了但提示还在」。整份回传 + 整体替换，就不会再有「刷新了一半」。
    """
    src = _page()
    start = src.index("const download = async (")
    body = src[start : src.find("\n  };", start)]
    assert "onOptionsChange(r.options)" in body, "下载完没整份替换选项"
    assert "r.fonts" not in body and "usableFonts" not in body, "还在用「只换一半」的老写法"
    # 后端也要回整份
    api_src = API.read_text(encoding="utf-8")
    assert "options: SubtitleOptions" in api_src, "接口类型里下载回的不是整份选项"


def test_picking_a_style_without_its_font_does_not_render():
    """选了字体没下的版式时别去渲预览：后端会拒、弹一句错，而那时用户该做的是下载。"""
    panel = _panel(_page())
    start = panel.index("const pickStyle = (")
    body = panel[start : panel.find("\n  };", start)]
    assert "fontReady" in body, "没判断这款版式的字体在不在本机"


def test_subtitle_ui_sits_before_the_merge_button():
    """字幕区要在合并按钮**之前**——它是导出参数的一部分，不是导出之后的事。"""
    src = _page()
    assert src.index("<SubtitlePanel") < src.index("合并导出（"), "字幕区没排在合并按钮之前"


def test_merge_sends_the_subtitles_aligned_with_the_clips():
    """逐段字幕必须按下标与片段对齐发出去。

    直接传 `subtitles` 数组而不补齐的话，用户只填了前两段、删掉中间一段之类的操作会让
    字幕错位到别的片段上——那是「字幕配错画面」，比不出字幕更糟。
    """
    src = _page()
    start = src.index("const doMerge = ")
    body = src[start : src.find("\n  const totalDur", start)]
    assert "subtitleStyle," in body and "subtitleScale," in body, body
    assert "subtitles: clipIds.map(" in body, "字幕没按片段数对齐"
    assert "subtitles[i] ?? \"\"" in body, "对齐时没兜住缺位"


def test_no_libass_means_a_sentence_not_a_silent_failure():
    """本机 ffmpeg 没有 libass 时要说清是什么问题、怎么办，而不是让字幕区空着。"""
    panel = _panel(_page())
    assert "filterAvailable" in panel, "没检查本机能不能烧字幕"
    assert "libass" in panel, "没说清是缺字幕滤镜"
    assert "环境体检" in panel, "没告诉用户去哪看当前用的是哪个 ffmpeg"


def test_blank_lines_mean_no_subtitle_not_an_error():
    """留空 = 这一段不出字幕，不是「必填项没填」。措辞上要这么说。"""
    panel = _panel(_page())
    assert "留空" in panel, "没说「留空则这一段不出字幕」"
    assert "有字幕" in panel, "没给出「几段有字幕」的进度"


def test_api_only_sends_the_subtitle_fields_the_backend_accepts():
    """接口只发后端认的字段名，别自己起名。"""
    src = API.read_text(encoding="utf-8")
    start = src.index("mergeVideos: (args: MergeArgs)")
    end = src.index("// ---- 字幕（版式与字体） ----", start)
    body = src[start:end]
    for field in ("subtitle_style:", "subtitle_scale:", "subtitles:"):
        assert field in body, f"合并请求里少了 {field}"


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
