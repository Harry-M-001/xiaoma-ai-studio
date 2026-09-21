"""分镜静态体检的用例（直接 python 运行）。

运行：venv/Scripts/python tests/test_storyboard_lint.py

这批用例的重点**不是「规则能不能报警」，是「会不会误报」**。
一份只会喊「狼来了」的体检报告，用户看两次就不看了，比没有更糟。
所以下面专门有几条「正常分镜不该被点名」的反向用例：

- 六镜里三个「固定」镜头 → 不许报运镜雷同（一场对话戏这么拍是常规，不是毛病）；
- 只有三个镜头时 → 不做占比统计（3 镜里 2 个就是 67%，说了没意义）；
- 套话词只出现一次 → 不报 AI 腔（那是文风，不是缺陷）。

另外钉住两条契约：
1. 解析必须复用 `storyboard_sheet.parse_storyboard`——体检说 12 镜、开拍说 15 镜，
   这种对不上的报告会直接让人不信它；
2. 输出形状与生成前的 preflight 告警一致，前端才能复用同一个组件画。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import storyboard_lint as lint  # noqa: E402
from app.services.storyboard_sheet import Shot  # noqa: E402


def S(
    no: str,
    size: str = "",
    move: str = "",
    duration: str = "",
    scene: str = "",
    first_frame: str = "",
    emotion: str = "",
    dialogue: str = "",
    scene_title: str = "",
) -> Shot:
    """造一个镜头。默认全空，用例里只写它关心的那几项。"""
    return Shot(
        no=no,
        heading=f"镜头{no}",
        size=size,
        move=move,
        duration=duration,
        scene=scene,
        first_frame=first_frame,
        emotion=emotion,
        dialogue=dialogue,
        scene_title=scene_title,
    )


def codes(findings) -> set[str]:
    return {f.code for f in findings}


def warns(findings) -> set[str]:
    return {f.code for f in findings if f.level == "warn"}


# 一份「没毛病的分镜」：六镜，景别与运镜都有变化，字段齐全，没有套话
GOOD_TEXT = """## 场景1 | 清晨的码头

### 镜头1 | 远景 | 固定 | 6s
- 画面：雾气未散的码头，吊车停在半空
- 首帧提示词：wide establishing shot of a foggy harbor at dawn, cranes silhouetted, cold blue palette

### 镜头2 | 中景 | 缓慢推近 | 4s
- 画面：老陈蹲在缆绳旁，把一封信折了两折
- 首帧提示词：medium shot of an old fisherman folding a letter beside a mooring rope, soft light

### 镜头3 | 近景 | 固定 | 3s
- 画面：他捏信的手指抖了一下
- 首帧提示词：close up of trembling fingers holding a folded letter, shallow depth of field

### 镜头4 | 全景 | 横移 | 5s
- 画面：码头尽头，一艘小艇正在靠岸
- 首帧提示词：full shot of a small boat approaching the pier, side tracking camera

### 镜头5 | 特写 | 跟拍 | 3s
- 画面：信封上被水浸开的一角
- 首帧提示词：extreme close up of a water stained envelope corner, handheld follow

### 镜头6 | 大远景 | 升降 | 8s
- 画面：整片海湾，小艇在雾里靠近
- 首帧提示词：aerial rise over the whole bay, a small boat approaching through fog
"""


def test_clean_storyboard_has_no_warning():
    """正常分镜不该出现任何 warn 级结论（info 允许）。"""
    shots, findings, _ = lint.lint_text(GOOD_TEXT)
    assert len(shots) == 6, f"应该解析出 6 个镜头，实际 {len(shots)}"
    assert not warns(findings), f"正常分镜被点名了：{sorted(warns(findings))}"


def test_lint_reuses_the_generation_parser():
    """体检与开拍必须是同一个解析器：数字对不上就没人信了。"""
    from app.services import storyboard_sheet

    shots, _, summary = lint.lint_text(GOOD_TEXT)
    same = storyboard_sheet.parse_storyboard(GOOD_TEXT)
    assert [s.no for s in shots] == [s.no for s in same], "体检用的解析结果与开拍不一致"
    assert summary["shots"] == len(same)


def test_summary_counts():
    _, _, summary = lint.lint_text(GOOD_TEXT)
    assert summary["shots"] == 6
    assert summary["totalSeconds"] == 6 + 4 + 3 + 5 + 3 + 8
    assert summary["scenes"] == 1
    assert summary["sizes"]["中景"] == 1


def test_three_consecutive_static_shots_is_not_a_defect():
    """反向用例：一场对话戏连着三个固定镜头，不许报运镜雷同。"""
    shots = [
        S("1", "中景", "固定", "4s", "甲抬头", "a man looks up in a dim room"),
        S("2", "近景", "固定", "3s", "乙低头", "a woman looks down in a dim room"),
        S("3", "中景", "固定", "4s", "甲开口", "a man starts to speak in a dim room"),
        S("4", "特写", "固定", "2s", "手搭在桌上", "a hand resting on a wooden table"),
        S("5", "全景", "缓慢推近", "5s", "房间全貌", "wide shot of a dim living room"),
    ]
    found = lint.lint_shots(shots)
    assert "move_run" not in codes(found), "固定镜头连排被误报成运镜雷同"
    assert "move_dominant" not in warns(found) or True  # 固定占多数最多只给 info
    dom = [f for f in found if f.code == "move_dominant"]
    if dom:
        assert dom[0].level == "info", "固定镜头占多数不该是 warn 级"


def test_three_consecutive_push_shots_is_reported():
    """对照用例：连着三个「缓慢推近」是要报的。"""
    shots = [
        S("1", "中景", "缓慢推近", "4s", "甲抬头", "a man looks up in a dim room"),
        S("2", "中景", "缓慢推近", "4s", "甲走近", "a man walks closer in a dim room"),
        S("3", "近景", "缓慢推近", "3s", "甲的脸", "close up of a man face in a dim room"),
        S("4", "全景", "固定", "5s", "房间", "wide shot of a dim living room"),
    ]
    found = lint.lint_shots(shots)
    run = [f for f in found if f.code == "move_run"]
    assert run, "连续三个推镜没有被报出来"
    assert run[0].level == "warn"
    assert run[0].shots == ("1", "2", "3"), f"镜号不对：{run[0].shots}"


def test_small_storyboard_gets_no_distribution_noise():
    """反向用例：只有三个镜头时不做占比统计。"""
    shots = [S(str(i), "中景", "固定", "3s", f"画面{i}", "a shot of something on a table") for i in (1, 2, 3)]
    found = lint.lint_shots(shots)
    assert "size_single_kind" not in codes(found), "三镜全中景不该报景别单调（样本太小）"
    assert "size_dominant" not in codes(found)


def test_size_single_kind_and_dominant_are_mutually_exclusive():
    """六镜全中景 → 只报「只有一种」；四中景两其他 → 报「占比过高」。"""
    all_same = [S(str(i), "中景", "固定", "3s", f"画面{i}", "a medium shot of a person at a desk") for i in range(1, 7)]
    found = lint.lint_shots(all_same)
    assert "size_single_kind" in codes(found)
    assert "size_dominant" not in codes(found), "只有一种景别时不该再叠加占比结论"

    mixed = [
        S("1", "中景", "固定", "3s", "画面1", "a medium shot of a person at a desk"),
        S("2", "中景", "固定", "3s", "画面2", "a medium shot of a person by the door"),
        S("3", "中景", "固定", "3s", "画面3", "a medium shot of a person on a chair"),
        S("4", "中景", "固定", "3s", "画面4", "a medium shot of a person at the window"),
        S("5", "近景", "固定", "3s", "画面5", "close up of a person eyes"),
        S("6", "远景", "固定", "3s", "画面6", "wide shot of the whole street"),
    ]
    found = lint.lint_shots(mixed)
    assert "size_dominant" in codes(found), "四中景/六镜（67%）应该报占比过高"
    assert "size_single_kind" not in codes(found)
    dom = [f for f in found if f.code == "size_dominant"][0]
    assert dom.shots == ("1", "2", "3", "4"), f"镜号不对：{dom.shots}"
    assert "67%" in dom.message


def test_missing_fields_all_reported():
    """只写了画面的一镜：四个缺字段结论都该出来。"""
    found = lint.lint_shots([S("1", scene="一个人站在雨里")])
    assert {"missing_size", "missing_move", "missing_duration", "first_frame_missing"} <= codes(found)


def test_duplicate_adjacent_scene_text():
    """相邻两镜画面开头一样 → 疑似复制粘贴。"""
    same = "老陈站在码头上，手里捏着一封信，海风把他的衣角吹起来"
    shots = [
        S("1", "中景", "固定", "3s", same, "medium shot of an old man holding a letter"),
        S("2", "近景", "固定", "3s", same, "close up of an old man holding a letter"),
    ]
    found = lint.lint_shots(shots)
    dup = [f for f in found if f.code == "scene_duplicate_adjacent"]
    assert dup, "两镜画面完全相同却没报"
    assert dup[0].shots == ("2",), f"应该只点后一镜：{dup[0].shots}"


def test_ai_slop_needs_density():
    """套话词要成规模才算 AI 腔；偶尔一个不报。"""
    once = [
        S(str(i), "中景", "固定", "3s", f"画面{i}", "a shot of a person at a desk") for i in range(1, 9)
    ]
    once[0] = S("1", "中景", "固定", "3s", "他仿佛在等谁", "a shot of a person waiting at a desk")
    assert "ai_slop" not in codes(lint.lint_shots(once)), "只出现一次套话词不该报"

    heavy = list(once)
    for i, a in enumerate(("仿佛", "宛如", "顿时")):
        heavy[i] = S(str(i + 1), "中景", "固定", "3s", f"他{a}在等谁", "a shot of a person waiting at a desk")
    found = [f for f in lint.lint_shots(heavy) if f.code == "ai_slop"]
    assert found, "三镜套话没被报出来"
    assert found[0].level == "warn"


def test_short_first_frame():
    shots = [
        S("1", "中景", "固定", "3s", "一个人在等", "短提示"),
        S("2", "近景", "固定", "3s", "另一个人在写", "a close up of a hand writing a long letter"),
    ]
    found = [f for f in lint.lint_shots(shots) if f.code == "first_frame_too_short"]
    assert found and found[0].shots == ("1",)
    assert found[0].level == "info"


def test_single_scene_hint():
    """镜头不少却只有一个场景 → info 提示一句。"""
    shots = [
        S(str(i), "中景", "固定", "3s", f"画面{i}", "a medium shot of a person at a desk", scene_title="场景1")
        for i in range(1, 10)
    ]
    found = [f for f in lint.lint_shots(shots) if f.code == "single_scene"]
    assert found and found[0].level == "info"


def test_findings_are_warn_first_and_unique():
    shots = [S(str(i), "中景", "固定", "3s", f"画面{i}") for i in range(1, 8)]
    found = lint.lint_shots(shots)
    assert len({f.code for f in found}) == len(found), "同一份报告里出现了重复的 code"
    levels = [f.level for f in found]
    assert levels == sorted(levels, key=lambda x: x != "warn"), "warn 应该排在 info 前面"
    for f in found:
        assert f.code and f.message and f.suggestion, f"结论缺字段：{f}"
        assert f.message.endswith("。") or f.message.endswith("）"), f"话没说完：{f.message}"


def test_warnings_have_preflight_shape():
    """输出形状要和生成前的 preflight 告警一致，前端才能复用同一个组件。"""
    found = lint.lint_shots([S("1", scene="一个人站在雨里")])
    w = found[0].as_warning("分镜")
    assert set(w) == {"code", "level", "message", "suggestion"}
    assert w["message"].startswith("「分镜」"), w["message"]
    assert w["level"] in ("warn", "info")


def test_every_rule_is_exercised_by_this_file():
    """元用例：每条规则都要有触发用例，且源码里不许出现「没人测过的 code」。

    没有这条，加一条新规则忘写用例，规则里的 bug 会静默藏住——而体检这类
    「只在特定数据形状下才说话」的功能，静默失效是最容易发生的失败方式。
    """
    import re

    source = (Path(__file__).resolve().parents[1] / "app" / "services" / "storyboard_lint.py").read_text(
        encoding="utf-8"
    )
    declared = set(re.findall(r'code="([a-z_]+)"', source))
    expected = {
        "missing_size",
        "missing_move",
        "missing_duration",
        "first_frame_missing",
        "size_single_kind",
        "size_dominant",
        "size_span_narrow",
        "move_run",
        "move_dominant",
        "move_kinds_few",
        "move_unknown",
        "move_not_a_move",
        "scene_duplicate_adjacent",
        "ai_slop",
        "first_frame_too_short",
        "single_scene",
    }
    assert declared == expected, f"规则表变了，用例表要同步：新增 {declared - expected}，消失 {expected - declared}"

    dup_text = "老陈站在码头上，手里捏着一封信，海风把他的衣角吹起来"
    cases = [
        # 缺字段
        lint.lint_shots([S("1", scene="一个人站在雨里")]),
        # 景别：只有一种
        lint.lint_shots([S(str(i), "中景", "固定", "3s", f"画面{i}") for i in range(1, 7)]),
        # 景别：一种占 67%（4/6）
        lint.lint_shots(
            [S(str(i), "中景", "固定", "3s", f"画面{i}") for i in range(1, 5)]
            + [
                S("5", "近景", "固定", "3s", "画面5"),
                S("6", "远景", "固定", "3s", "画面6"),
            ]
        ),
        # 景别：只用到两种且没有一种占多数（3/3）→ 偏窄
        lint.lint_shots(
            [S(str(i), "中景", "固定", "3s", f"画面{i}") for i in range(1, 4)]
            + [S(str(i), "近景", "摇镜", "3s", f"画面{i}") for i in range(4, 7)]
        ),
        # 运镜：连续三个推近
        lint.lint_shots(
            [S(str(i), "中景", "缓慢推近", "3s", f"画面{i}") for i in range(1, 4)]
            + [S("4", "全景", "固定", "5s", "画面4")]
        ),
        # 运镜：占 67% 但不连续
        lint.lint_shots(
            [
                S("1", "中景", "摇镜", "3s", "画面1"),
                S("2", "近景", "固定", "3s", "画面2"),
                S("3", "中景", "摇镜", "3s", "画面3"),
                S("4", "近景", "固定", "3s", "画面4"),
                S("5", "中景", "摇镜", "3s", "画面5"),
                S("6", "远景", "缓慢推近", "3s", "画面6"),
            ]
        ),
        # 运镜：只用到两种、各占一半 → 种类偏少
        lint.lint_shots(
            [S(str(i), "中景", "固定" if i % 2 else "摇镜", "3s", f"画面{i}") for i in range(1, 9)]
        ),
        # 运镜：这一栏写的是镜头类型 / 机位（「主观镜头」不是运动方式）
        lint.lint_shots(
            [S(str(i), "中景", "主观镜头", "3s", f"画面{i}") for i in range(1, 7)]
        ),
        # 运镜：自造说法，词表里没有
        lint.lint_shots(
            [S(str(i), "中景", "镜头缓缓飘过", "3s", f"画面{i}") for i in range(1, 7)]
        ),
        # 画面复制粘贴（开头 ≥12 字相同）
        lint.lint_shots(
            [
                S("1", "", "", "", dup_text),
                S("2", "", "", "", dup_text),
            ]
        ),
        # AI 腔
        lint.lint_shots([S(str(i), "中景", "固定", "3s", "他仿佛在等谁") for i in range(1, 7)]),
        # 首帧提示词过短
        lint.lint_shots([S("1", "中景", "固定", "3s", "画面", "短")]),
        # 镜头不少但只有一个场景
        lint.lint_shots(
            [S(str(i), "中景", "固定", "3s", f"画面{i}", scene_title="场景1") for i in range(1, 10)]
        ),
    ]
    seen: set[str] = set()
    for c in cases:
        seen |= {f.code for f in c}
    missing = expected - seen
    assert not missing, f"这些规则没有任何用例触发过：{sorted(missing)}"


def test_one_broken_rule_does_not_kill_the_report():
    """某条规则自己写崩了，报告仍要出得来（体检是辅助，不能变成 500）。"""
    original = lint._RULES

    def boom(_shots):
        raise RuntimeError("这条规则坏了")

    try:
        lint._RULES = (boom, lint._rule_missing_fields)
        found = lint.lint_shots([S("1", scene="一个人站在雨里")])
    finally:
        lint._RULES = original
    assert "missing_size" in codes(found), "坏规则连累了后面的规则"


def test_empty_text_returns_nothing():
    shots, findings, summary = lint.lint_text("")
    assert shots == [] and findings == []
    assert summary["shots"] == 0


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
