"""字幕的纯口径（直接 python 运行）。

运行：venv/Scripts/python tests/test_subtitles.py

为什么这一项要单测：烧字幕有四个**不报错但结果是错的**陷阱，用户从成片上不一定马上看出来：

1. **ASS 的颜色是 BGR 不是 RGB。** `#FF8800` 要写成 `&H000088FF`。写反了画面照样有颜色，
   只是红蓝互换——很容易被当成「风格问题」一路带下去。
2. **文本里的真换行会把 ASS 文件按行拆坏。** text 字段里出现换行，后面的内容会被当成
   新字段，而 libass 只是**静默丢掉**、不报错。必须转成 `\\N`。
3. **花括号会开启覆盖标签块。** 正文里写「{旁白}」会被 libass 当成样式指令，那一行直接变形。
4. **时间轴反了 / 零长的字幕会被静默跳过。** 少一句字幕比报错更难查，所以在导出前拦住。

另外两条设计口径也要钉住：**版式是用户直接选的、画风只给默认值**（用户选过之后换画风
不许动它），以及**认不出的 key 一律退回默认**（老画布 / 分享码里可能没有这个字段）。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.services import subtitle_fonts, subtitles  # noqa: E402


def _cues() -> list[dict[str, object]]:
    return [
        {"start": 0.0, "end": 2.5, "text": "第一句"},
        {"start": 2.5, "end": 5.0, "text": "第二句"},
    ]


# ------------------------------------------------------------------ 颜色与时间


def test_ass_color_is_bgr_not_rgb():
    """ASS 的颜色是 `&HAABBGGRR`——写反了红蓝互换，画面照样"有颜色"。"""
    assert subtitles.ass_color("#FF8800") == "&H000088FF"
    assert subtitles.ass_color("#000000") == "&H00000000"
    assert subtitles.ass_color("#FFFFFF") == "&H00FFFFFF"
    # 带 # 与不带都要认
    assert subtitles.ass_color("FF8800") == subtitles.ass_color("#FF8800")
    # AA 是「透明度」：00 才是不透明
    assert subtitles.ass_color("#FF8800", alpha=0xB3).startswith("&HB3")
    # 允许直接写 #RRGGBBAA
    assert subtitles.ass_color("#FF8800B3").startswith("&HB3")


def test_ass_color_rejects_a_bad_value():
    """颜色写错要报错，不能静默给一个乱七八糟的值。"""
    for bad in ("", "red", "#FFF", "#GGGGGG"):
        try:
            subtitles.ass_color(bad)
            raise AssertionError(f"{bad!r} 竟然通过了")
        except ValueError:
            pass


def test_ass_time_format_and_negative_clamp():
    assert subtitles.ass_time(0) == "0:00:00.00"
    assert subtitles.ass_time(2.5) == "0:00:02.50"
    assert subtitles.ass_time(61.25) == "0:01:01.25"
    assert subtitles.ass_time(3661.99) == "1:01:01.99"
    # 负值夹到 0：ffprobe 偶尔给出 -0.01 这类起点，直接格式化会变成 -1:59:59.99
    # 而 libass 不报错、只是那一条永远不显示
    assert subtitles.ass_time(-0.01) == "0:00:00.00"
    # 四舍五入到 100 要进位，不能出现 .100
    assert subtitles.ass_time(1.999) == "0:00:02.00"


# ------------------------------------------------------------------ 文本清理


def test_ass_text_turns_newlines_into_hard_breaks():
    """真换行会把 ASS 文件按行拆坏，libass 只是静默丢掉——必须转成 `\\N`。"""
    assert subtitles.ass_text("第一行\n第二行") == r"第一行\N第二行"
    assert subtitles.ass_text("第一行\r\n第二行") == r"第一行\N第二行"
    # 连续空行也各自变成一个 \N，不会留真换行
    assert "\n" not in subtitles.ass_text("甲\n\n乙")


def test_ass_text_escapes_braces():
    """`{` 会开启覆盖标签块：正文里写「{旁白}」会让那一行直接变形。"""
    out = subtitles.ass_text("他说{笑}了一声")
    assert "{" not in out.replace(r"\{", "") and "}" not in out.replace(r"\}", ""), out
    assert r"\{" in out and r"\}" in out, out


def test_ass_text_strips_blank_lines_and_edges():
    assert subtitles.ass_text("   两边有空格   ") == "两边有空格"
    assert subtitles.ass_text(None) == ""
    assert subtitles.ass_text("  \n  ") == ""


# ------------------------------------------------------------------ 版式与画风（用户说了算）


def test_every_style_is_self_consistent():
    """版式表自身要自洽：key 唯一、颜色合法、字体在清册里、对齐值合法。"""
    keys = [str(s["key"]) for s in subtitles.STYLES]
    assert len(keys) == len(set(keys)), keys
    assert subtitles.DEFAULT_STYLE in keys, "默认版式必须真的存在"
    for s in subtitles.STYLES:
        assert s["label"] and s["hint"], s
        subtitles.ass_color(str(s["primary"]))  # 非法颜色会抛
        subtitles.ass_color(str(s["outline"]))
        if s["box"]:
            subtitles.ass_color(str(s["box"]))
        assert subtitle_fonts.font(s["font"]) is not None, f"{s['key']} 引用了不存在的字体 {s['font']}"
        # ASS 的 numpad 对齐：1..9
        assert 1 <= int(s["align"]) <= 9, s
        assert 0 < float(s["size"]) < 0.2, s
        assert float(s["outline_w"]) >= 0 and float(s["shadow"]) >= 0, s


def test_every_recommendation_points_at_a_real_style():
    """画风推荐表里的每一项都必须指向真实存在的版式——写错就等于悄悄用了默认。"""
    keys = {str(s["key"]) for s in subtitles.STYLES}
    for genre, target in subtitles.RECOMMENDED.items():
        assert target in keys, f"{genre} 推荐的版式 {target} 不存在"


def test_the_users_choice_beats_the_genre():
    """**这是这一项的核心口径**：用户选过就用用户的，换画风不许动它。

    与 #32 回滚、#33 定稿同一个口径——用户显式说过的话不被别的东西推翻。
    """
    assert subtitles.resolve_style("variety_pop", "ink_wash") == "variety_pop"
    # 用户没选（空）才看画风
    assert subtitles.resolve_style("", "ink_wash") == "guofeng_kai"
    assert subtitles.resolve_style(None, "handheld_doc") == "doc_bottom"
    # 认不出的值当「没选」，退回画风推荐
    assert subtitles.resolve_style("不存在的版式", "cyberpunk") == "variety_pop"


def test_following_genre_flag():
    assert subtitles.following_genre("") is True
    assert subtitles.following_genre(None) is True
    assert subtitles.following_genre("不存在的版式") is True
    assert subtitles.following_genre("doc_bottom") is False


def test_unknown_genre_falls_back_to_default():
    assert subtitles.recommend("没这个画风") == subtitles.DEFAULT_STYLE
    assert subtitles.recommend(None) == subtitles.DEFAULT_STYLE


def test_scale_is_clamped_not_rejected():
    assert subtitles.sanitize_scale(None) == 1.0
    assert subtitles.sanitize_scale("abc") == 1.0
    assert subtitles.sanitize_scale(float("nan")) == 1.0
    assert subtitles.sanitize_scale(0.01) == subtitles.MIN_SCALE
    assert subtitles.sanitize_scale(99) == subtitles.MAX_SCALE
    assert subtitles.sanitize_scale("1.2") == 1.2


# ------------------------------------------------------------------ 时间轴


def test_cues_are_laid_out_by_shot_durations():
    """时间轴按逐镜时长累加——镜头长度本来就已经定了，不让用户再填一遍时间码。"""
    cues = subtitles.cues_from_shots([(2.0, "甲"), (3.5, "乙"), (1.5, "丙")])
    assert [(c["start"], c["end"]) for c in cues] == [(0.0, 2.0), (2.0, 5.5), (5.5, 7.0)]
    assert [c["text"] for c in cues] == ["甲", "乙", "丙"]


def test_shots_without_dialogue_or_duration_are_skipped_but_time_still_advances():
    """没有台词的镜头不该产生空字幕，但**时间要继续走**——否则后面所有字幕都会提前。"""
    cues = subtitles.cues_from_shots([(2.0, "甲"), (3.0, ""), (1.0, "丙")])
    assert [c["text"] for c in cues] == ["甲", "丙"]
    assert [(c["start"], c["end"]) for c in cues] == [(0.0, 2.0), (5.0, 6.0)]


def test_cue_total_seconds():
    assert subtitles.cue_total_seconds(_cues()) == 5.0
    assert subtitles.cue_total_seconds([]) == 0.0


# ------------------------------------------------------------------ 与转场配合（时间轴不许漂）


def test_transition_shifts_later_clips_earlier():
    """**配了转场之后成片会变短，字幕时间轴必须跟着变。**

    按「各段时长累加」算的话，第 i 段会提前 `seconds * i`——三段 0.5 秒转场、第三段
    就偏了 1 秒，用户看到的是「字幕比画面早出来一截」，而且越往后越明显。
    """
    from app.services import transitions

    lens = [5.0, 4.0, 6.0]
    # 硬切：累加
    assert transitions.clip_starts(lens, "cut", 0.5) == [0.0, 5.0, 9.0]
    # 叠化 0.5 秒：第 2 段从 4.5 起、第 3 段从 8.0 起
    assert transitions.clip_starts(lens, "dissolve", 0.5) == [0.0, 4.5, 8.0]


def test_clip_spans_end_at_the_next_start_so_cues_never_overlap():
    """每段的止 = 下一段的起点。

    按「起点 + 本段时长」算的话，相邻两条字幕会重叠 `seconds` 秒——屏幕上两行字叠在一起。
    """
    from app.services import transitions

    spans = transitions.clip_spans([5.0, 4.0, 6.0], "dissolve", 0.5)
    assert spans == [(0.0, 4.5), (4.5, 8.0), (8.0, 14.0)]
    for (_s1, e1), (s2, _e2) in zip(spans, spans[1:]):
        assert e1 <= s2, spans
    # 最后一段的止 = 成片总长
    assert spans[-1][1] == transitions.total_seconds([5.0, 4.0, 6.0], "dissolve", 0.5)


def test_cues_from_spans_follows_the_merged_timeline():
    from app.services import transitions

    lens = [5.0, 4.0, 6.0]
    spans = transitions.clip_spans(lens, "dissolve", 0.5)
    cues = subtitles.cues_from_spans(spans, ["甲", "乙", "丙"])
    assert [(c["start"], c["end"]) for c in cues] == [(0.0, 4.5), (4.5, 8.0), (8.0, 14.0)]
    assert [c["text"] for c in cues] == ["甲", "乙", "丙"]


def test_cues_from_spans_skips_blank_segments_without_shifting_the_rest():
    """某一段没字幕只是这一段不出字，**后面几段的时间不许被挪**。"""
    from app.services import transitions

    spans = transitions.clip_spans([4.0, 3.0, 5.0], "cut", 0.5)
    cues = subtitles.cues_from_spans(spans, ["甲", "", "丙"])
    assert [c["text"] for c in cues] == ["甲", "丙"]
    assert [(c["start"], c["end"]) for c in cues] == [(0.0, 4.0), (7.0, 12.0)]


def test_cues_from_spans_tolerates_short_text_list():
    """字幕条数比片段少（用户只填了前两段）不该炸。"""
    cues = subtitles.cues_from_spans([(0.0, 2.0), (2.0, 5.0)], ["只有一条"])
    assert len(cues) == 1


def test_check_cues_blocks_empty_and_reversed():
    assert subtitles.check_cues(_cues()) == ""
    msg = subtitles.check_cues([])
    assert msg and "没有字幕内容" in msg
    bad = [{"start": 5.0, "end": 5.0, "text": "零长"}]
    assert "第 1 条" in subtitles.check_cues(bad)
    rev = _cues() + [{"start": 9.0, "end": 8.0, "text": "反了"}]
    assert "第 3 条" in subtitles.check_cues(rev)


# ------------------------------------------------------------------ ASS 生成


def test_build_ass_has_the_three_required_sections():
    ass = subtitles.build_ass(_cues(), style_key="doc_bottom", width=1920, height=1080)
    assert "[Script Info]" in ass and "[V4+ Styles]" in ass and "[Events]" in ass
    assert "PlayResX: 1920" in ass and "PlayResY: 1080" in ass
    assert "ScaledBorderAndShadow: yes" in ass, "不写这条，4K 上描边会细到看不见"
    assert ass.count("Dialogue: 0,") == 2
    assert ass.endswith("\n")


def test_build_ass_uses_the_family_name_not_the_file_name():
    """ASS 里写的必须是**族名**。写成文件名会让 libass 静默回落系统字体（中文变豆腐块）。"""
    ass = subtitles.build_ass(_cues(), style_key="guofeng_kai", width=1280, height=720)
    assert "LXGW WenKai" in ass, ass
    assert ".ttf" not in ass and ".otf" not in ass, "ASS 里不许出现字体文件名"


def test_sizes_scale_with_the_frame_height():
    """尺寸按画面高度换算，所以同一套版式在 720p 与 4K 上表现一致。"""
    small = subtitles.build_ass(_cues(), style_key="doc_bottom", width=1280, height=720)
    large = subtitles.build_ass(_cues(), style_key="doc_bottom", width=3840, height=2160)

    def style_line(ass: str) -> str:
        return next(ln for ln in ass.splitlines() if ln.startswith("Style: Default,"))

    small_size = int(style_line(small).split(",")[2])
    large_size = int(style_line(large).split(",")[2])
    # 三倍关系，允许 1 像素的四舍五入差（720*0.048=34.56、2160*0.048=103.68）
    assert abs(large_size - small_size * 3) <= 1, (small_size, large_size)


def test_scale_multiplies_the_font_size():
    one = subtitles.build_ass(_cues(), style_key="doc_bottom", width=1920, height=1080)
    two = subtitles.build_ass(_cues(), style_key="doc_bottom", width=1920, height=1080, size_scale=1.5)

    def size_of(ass: str) -> int:
        return int(next(ln for ln in ass.splitlines() if ln.startswith("Style: Default,")).split(",")[2])

    assert size_of(two) > size_of(one)


def test_box_style_switches_border_style():
    """带底板的版式要用 BorderStyle=3 配 BackColour，否则底板根本不出现。"""
    bar = subtitles.build_ass(_cues(), style_key="news_bar", width=1920, height=1080)
    line = next(ln for ln in bar.splitlines() if ln.startswith("Style: Default,"))
    fields = line[len("Style: Default,"):].split(",")
    # Format: ... Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, ...
    assert fields[14] == "3", fields  # BorderStyle
    assert fields[6] != "&H00000000", "底板颜色没设上"


def test_outline_style_has_no_box():
    plain = subtitles.build_ass(_cues(), style_key="doc_bottom", width=1920, height=1080)
    fields = next(ln for ln in plain.splitlines() if ln.startswith("Style: Default,"))[len("Style: Default,"):].split(",")
    assert fields[14] == "1"


def test_dialogue_lines_carry_escaped_text_and_time():
    ass = subtitles.build_ass(
        [{"start": 1.0, "end": 2.0, "text": "甲\n乙{注}"}], style_key="doc_bottom", width=1280, height=720
    )
    line = next(ln for ln in ass.splitlines() if ln.startswith("Dialogue:"))
    assert line.endswith(r"甲\N乙\{注\}"), line
    assert ",0:00:01.00,0:00:02.00," in line, line


def test_blank_cues_are_dropped_from_the_events():
    cues = _cues() + [{"start": 5.0, "end": 6.0, "text": "   "}]
    ass = subtitles.build_ass(cues, style_key="doc_bottom", width=1280, height=720)
    assert ass.count("Dialogue: 0,") == 2


def test_build_ass_falls_back_when_the_style_key_is_unknown():
    """老画布 / 分享码里可能没有这个字段，认不出的要用默认版式而不是崩。"""
    ass = subtitles.build_ass(_cues(), style_key="没这个版式", width=1280, height=720)
    assert "Style: Default," in ass
    assert ass.count("Dialogue: 0,") == 2


def test_fonts_used_reports_the_one_font_the_style_needs():
    """只挂用到的那一份：libass 会把 fontsdir 里的字体全量解析，整库挂要慢一倍。"""
    assert subtitles.fonts_used("guofeng_kai") == ["wenkai"]
    assert subtitles.fonts_used("poster_smiley") == ["smiley"]
    assert subtitles.fonts_used("") == ["noto_sans"]


def test_style_options_marks_font_readiness():
    rows = subtitles.style_options()
    by_key = {str(r["key"]): r for r in rows}
    # 内置那款一定可用——「开箱就能烧中文字幕」这条要成立
    assert by_key["doc_bottom"]["fontReady"] is True
    assert by_key["doc_bottom"]["fontLabel"] == "思源黑体"
    assert all(isinstance(r["fontReady"], bool) for r in rows)


# ------------------------------------------------------------------ 字体清册


def test_manifest_is_consistent():
    """清册自检：内置那款必须真的在磁盘上，且内置的**有且只有一款**。

    「内置的字体却在磁盘上找不到」是最坏的：界面看着能用、一点却报错。
    内置多了则是往每个用户包里塞体积——那正是「大文件不进仓库」要避免的。
    """
    subtitle_fonts.assert_manifest_consistent()


def test_builtin_font_is_the_default_everywhere():
    """默认版式用的必须是内置字体，否则「打开就能用」不成立。"""
    default_style = subtitles.style(subtitles.DEFAULT_STYLE)
    assert default_style is not None
    assert default_style["font"] == subtitle_fonts.BUILTIN_KEY
    # 一个画风的推荐也不该把默认版式推到需要下载的字体上
    assert subtitles.fonts_used(subtitles.DEFAULT_STYLE) == [subtitle_fonts.BUILTIN_KEY]


def test_unknown_font_key_falls_back_to_builtin():
    assert subtitle_fonts.resolve_key("没这个字体") == subtitle_fonts.BUILTIN_KEY
    assert subtitle_fonts.resolve_key("") == subtitle_fonts.BUILTIN_KEY
    assert subtitle_fonts.resolve_key("wenkai") == "wenkai"
    # 认不出也要给得出族名，不然 ASS 里会写出一个空 Fontname
    assert subtitle_fonts.family_of("没这个字体") == "Noto Sans CJK SC"


def test_font_manifest_families_are_exact_and_ascii():
    """族名是 ASS 里逐字匹配的，全是实测值；顺手挡住「不小心写成文件名」。"""
    for f in subtitle_fonts.FONTS:
        fam = str(f["family"])
        assert fam and fam.isascii(), f
        assert not fam.endswith((".ttf", ".otf")), f"族名不可能是文件名：{fam}"
        for name in f["files"]:  # type: ignore[union-attr]
            assert str(name).endswith((".ttf", ".otf")), name


def test_stage_fonts_links_only_what_is_asked_for(tmp_path=None):
    """`stage_fonts` 只把点名的字体放进工作目录，而且不复制大文件（同卷硬链接）。"""
    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as tmp:
        work = _P(tmp) / "shot"
        staged = subtitle_fonts.stage_fonts(work, [subtitle_fonts.BUILTIN_KEY])
        assert staged, "内置字体应当能挂上"
        assert all((work / n).exists() for n in staged)
        # 只挂点名的那一款
        assert "LXGWWenKai-Regular.ttf" not in staged
        # 再挂一次是幂等的（同一个镜头重跑不该堆文件）
        again = subtitle_fonts.stage_fonts(work, [subtitle_fonts.BUILTIN_KEY])
        assert again == staged


def test_missing_font_reports_which_file():
    """缺文件要能说出缺哪一个——不然界面上只能说「字体不可用」，用户无从下手。"""
    assert subtitle_fonts.missing_files(subtitle_fonts.BUILTIN_KEY) == []
    assert subtitle_fonts.font("noto_serif") is not None


def test_verify_installed_checks_the_content_not_just_existence(tmp_path=None):
    """**「文件在」不等于「能用」**：一个半截文件在 `missing_files()` 眼里也是「在」。

    这是真跑验收里逮到的洞：把字体截成 1KB，`install_font` 照样回「不用再下」，
    然后拿着坏文件去渲染——libass 会静默回落系统字体，出一部字体不对的片子。
    """
    import asyncio
    import tempfile
    from pathlib import Path as _P

    from app.services import ffmpeg_service

    # `check_ready` 里的滤镜判据读的是缓存，而缓存是应用启动时预热的；
    # 单测里要自己预热一次，否则会把「还没探过」当成「没有滤镜」，断言跑偏。
    asyncio.run(ffmpeg_service.warm_subtitle_filter())

    real = subtitle_fonts.user_dir
    with tempfile.TemporaryDirectory() as tmp:
        fake = _P(tmp)
        subtitle_fonts.user_dir = lambda: fake  # type: ignore[assignment]
        try:
            # 内置那款是好的
            assert subtitle_fonts.verify_installed(subtitle_fonts.BUILTIN_KEY) == []
            assert subtitle_fonts.font_status(subtitle_fonts.BUILTIN_KEY) == "ok"

            # 没下过的：报 missing，而不是 corrupt
            assert subtitle_fonts.font_status("wenkai") == "missing"

            # 把一个「在、但内容不对」的文件放进下载目录 → 必须判成 corrupt
            name = str((subtitle_fonts.font("wenkai") or {})["files"][0])  # type: ignore[index]
            (fake / name).write_bytes(b"\x00" * 4096)
            assert subtitle_fonts.missing_files("wenkai") == [], "存在性检查看不到内容不对"
            assert subtitle_fonts.verify_installed("wenkai") == [name]
            assert subtitle_fonts.font_status("wenkai") == "corrupt"
            # corrupt 时导出体检要说「重新下载」，而不是「没下载过」
            msg = subtitle_fonts.check_ready(["wenkai"])
            assert "霞鹜文楷" in msg, msg
            assert "损坏" in msg or "重新下载" in msg, msg
        finally:
            subtitle_fonts.user_dir = real  # type: ignore[assignment]


def test_every_downloadable_font_has_a_checksum():
    """非内置的每一份都必须有 sha256：没有它就没法比对，等于「下到什么算什么」。"""
    for f in subtitle_fonts.FONTS:
        if f["bundled"]:
            continue
        sums = dict(f.get("sha256") or {})  # type: ignore[arg-type]
        for name in f["files"]:  # type: ignore[union-attr]
            assert sums.get(str(name)), f"{f['key']} 的 {name} 没有校验和"


def test_download_urls_point_at_our_own_mirrors():
    """地址必须是**我们自己的 Release 附件**，不是上游直链。

    上游 `raw.githubusercontent.com` 国内基本不通，而用户在国内；而且地址写在代码里，
    必须长期不变（所以挂在固定 tag 的 Release 上，不挂版本 Release）。
    """
    for f in subtitle_fonts.FONTS:
        for name in f["files"]:  # type: ignore[union-attr]
            url = subtitle_fonts.download_url(str(name))
            assert url.startswith("https://"), url
            assert "releases/download/fonts-v1/" in url, url
            assert url.endswith(str(name)), url
    # 第一个来源优先，且两个来源不同
    assert subtitle_fonts.download_url("x.otf", 0) != subtitle_fonts.download_url("x.otf", 1)
    # 越界夹取，不炸
    assert subtitle_fonts.download_url("x.otf", 99) == subtitle_fonts.download_url("x.otf", 1)


def test_download_size_is_known_for_every_font():
    """界面要显示「要下多大」，不能等用户下完才发现很占地方。"""
    for f in subtitle_fonts.FONTS:
        assert subtitle_fonts.download_size(f["key"]) > 0, f
    assert subtitle_fonts.download_size("wenkai") == 25_575_676


def test_bundled_font_lives_in_the_readonly_dir_and_downloads_go_to_the_data_dir():
    """内置的在仓库目录、下载的在数据目录——两者不能混。

    混了的话「大文件不进仓库」这条就形同虚设，而且便携包可能落在只读位置。
    """
    for f in subtitle_fonts.FONTS:
        if not f["bundled"]:
            continue
        for name in f["files"]:  # type: ignore[union-attr]
            found = subtitle_fonts.find_file(str(name))
            assert found is not None and found.parent == subtitle_fonts.BUILTIN_DIR, found
    assert subtitle_fonts.user_dir() != subtitle_fonts.BUILTIN_DIR
    assert str(subtitle_fonts.user_dir()).endswith("fonts")


def test_check_ready_when_everything_is_present():
    """内置字体齐了就不该报错（这一条会真去问 ffmpeg 有没有 ass 滤镜）。"""
    import asyncio

    from app.services import ffmpeg_service

    asyncio.run(ffmpeg_service.warm_subtitle_filter())
    if not ffmpeg_service.subtitle_filter_available():  # pragma: no cover
        # 这台机器上的 ffmpeg 没有 libass：那也该说清是滤镜的问题，而不是怪字体
        msg = subtitle_fonts.check_ready([subtitle_fonts.BUILTIN_KEY])
        assert "libass" in msg, msg
        return
    # 内置那款是随包分发的，必须真的可用——「打开就能烧中文字幕」这条要成立
    assert subtitle_fonts.check_ready([subtitle_fonts.BUILTIN_KEY]) == ""
    assert subtitle_fonts.BUILTIN_KEY in subtitle_fonts.usable_keys()
    assert subtitle_fonts.available()


# ------------------------------------------------------------------ 接口


def test_options_endpoint_gives_everything_the_ui_needs():
    """选项接口要一次给全，前端不另抄任何一份表。

    抄一份就会出现「界面能选出后端不认识的东西」，而报错要到导出时才知道。
    """
    import asyncio

    from app.routers import subtitles as subs_router

    data = asyncio.run(subs_router.list_options())
    assert len(data["styles"]) == len(subtitles.STYLES)
    assert len(data["fonts"]) == len(subtitle_fonts.FONTS)
    assert data["defaultStyle"] == subtitles.DEFAULT_STYLE
    assert data["recommended"] == subtitles.RECOMMENDED
    assert set(data["scale"]) == {"min", "max", "default"}
    assert isinstance(data["filterAvailable"], bool)
    # **每一款版式引用的字体都在字体清单里**——不然界面会出现一个选了就报错的版式
    font_keys = {str(f["key"]) for f in data["fonts"]}
    for st in data["styles"]:
        assert str(st["font"]) in font_keys, st
    # 清单里不带 sha256 明细（那是校验用的，摆到前端没意义）
    for f in data["fonts"]:
        assert "sha256" not in f, f
        assert f["downloadBytes"] > 0, f
    # 示例文本要能暴露缺字与字体回退：带繁体 + 生僻字 + 英文数字
    text = str(data["previewText"])
    assert "漢" in text and any(ord(c) > 0x3400 for c in text), text


def test_preview_endpoint_returns_a_real_jpeg():
    """预览是**真渲染一帧**，不是前端近似画——所以这里断言它真出了一张 JPEG。

    版式效果取决于 libass 的字体选择/字形/描边/缩放/边距，前端再写一套必然对不上，
    而「预览看着挺好、导出来不一样」是最伤信任的一种不一致。
    """
    import asyncio
    import base64

    from app.routers import subtitles as subs_router
    from app.schemas import SubtitlePreviewIn

    async def scenario():
        from app.services import ffmpeg_service

        await ffmpeg_service.warm_subtitle_filter()
        if not ffmpeg_service.subtitle_filter_available():  # pragma: no cover
            return None
        return await subs_router.preview(
            SubtitlePreviewIn(style="doc_bottom", text="预览测试", width=640, height=360)
        )

    out = asyncio.run(scenario())
    if out is None:  # pragma: no cover
        return
    assert out["style"] == "doc_bottom"
    assert out["font"] == subtitle_fonts.BUILTIN_KEY
    assert out["image"].startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(out["image"].split(",", 1)[1])
    assert raw[:3] == b"\xff\xd8\xff", "不是 JPEG"
    assert len(raw) > 1000, len(raw)


def test_download_refuses_a_font_that_does_not_exist():
    import asyncio

    from fastapi import HTTPException

    from app.routers import subtitles as subs_router

    try:
        asyncio.run(subs_router.download_font("没这个字体"))
        raise AssertionError("不存在的字体竟然能下载")
    except HTTPException as e:
        assert e.status_code == 404, e.status_code


def test_download_returns_the_whole_options_object():
    """下载接口要回**整份选项**，不只回字体列表。

    踩过：只回字体列表时，版式里的 `fontReady` 还是页面加载时那一次的老值，
    界面就会显示「下完了但提示还在」。整份回传 + 前端整体替换，就没有「刷新了一半」。
    内置那款已经在本机，所以这条走「不用再下」的早退分支、**不联网**。
    """
    import asyncio

    from app.routers import subtitles as subs_router

    out = asyncio.run(subs_router.download_font(subtitle_fonts.BUILTIN_KEY))
    assert out["ok"] is True
    opts = out["options"]
    assert "styles" in opts and "fonts" in opts and "usableFonts" in opts, list(opts)
    # 版式里的 fontReady 必须与字体状态一致——这两处一旦不一致，
    # 就会出现「字体明明好了、版式还说缺」（或者反过来）
    status = {str(f["key"]): str(f["status"]) for f in opts["fonts"]}
    for st in opts["styles"]:
        assert st["fontReady"] == (status[str(st["font"])] == "ok"), st


def test_all_test_functions_have_unique_names():
    """元测试：**不许有两个同名的测试函数**。

    踩过：改测试时不小心把两个函数改成同一个名字，Python 的 globals 会只留最后一个，
    另一个**静默不跑**——测试数看着没少，实际断言已经不在执行了。所以在这里钉一条。
    """
    import re
    from collections import Counter
    from pathlib import Path as _P

    here = _P(__file__).resolve().parent
    for path in sorted(here.glob("test_*.py")):
        names = re.findall(r"^def (test_\w+)\(", path.read_text(encoding="utf-8"), re.M)
        dup = [n for n, c in Counter(names).items() if c > 1]
        assert not dup, f"{path.name} 里有重名的测试函数（后者会静默覆盖前者）：{dup}"


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
