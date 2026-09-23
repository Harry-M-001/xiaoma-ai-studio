"""去字幕的纯口径 + 真跑 ffmpeg（直接 python 运行）。

运行：venv/Scripts/python tests/test_subtitle_removal.py

这一项的核心不是「函数算得对」，而是**三个只有真跑才知道的约束**（三段探针量出来的，
写在 `services/subtitle_removal.py` 的模块注释里）：

1. **抹平的框不能碰画面四边**——碰了 ffmpeg 直接 `Conversion failed`。字幕贴着画面底边
   是最常见的情形，所以这里必须自动内缩 1px，并把这件事说给用户。
2. **零宽 / 零高的框不报错**，退出码 0 却什么都没做。所以最小边得自己拦。
3. **裁掉只有「框贴着上下边缘」时才是一刀切干净**；框在画面中间时不让用——
   把中间挖掉再对接会让上下两条画面错位，比留一条糊痕更糟。

另外把「四种手法在什么情况下不能用、为什么」也钉住：`available=False` 时必须带 `reason`，
因为不可用的手法仍然会列在界面上，静默消失会让人以为程序坏了。

最后一段是这一版（`#44`）补的：**要真无痕的那一档**。它不是第五种手法——那四种都是拿
画面盖掉像素，而这一档是 AI 补全，靠的是我们**不代装、也不代跑**的一个独立安装程序。
所以与它有关的不是什么算法，而是口径：只回安装包的事实（下没下、在哪儿），
**不许编一个「装好了」的结论**（它的目录不归我们管，没有判据）。
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.services import (  # noqa: E402
    engine_install,
    ffmpeg_service,
    image_size,
    local_engines,
    subtitle_removal as sr,
)

W, H = 1280, 720


def _bottom() -> dict[str, int]:
    return {"x": 0, "y": H - 110, "w": W, "h": 110}


def _middle() -> dict[str, int]:
    return {"x": 100, "y": 300, "w": 900, "h": 100}


def _parse_delogo(expr: str) -> dict[str, int]:
    """把 `delogo=x=1:y=611:w=1278:h=108` 拆回一个 dict。"""
    out: dict[str, int] = {}
    for part in expr.split("delogo=")[1].split(":"):
        key, _, value = part.partition("=")
        out[key] = int(value)
    return out


# ------------------------------------------------------------------ 纯口径


def test_the_table_is_self_consistent():
    sr.assert_consistent()
    assert len(sr.as_dicts()) == 4, "四种手法"
    assert sr.labels() == ["抹平", "遮住", "模糊", "裁掉"], sr.labels()
    assert sr.by_key("delogo") is not None and sr.by_key("不存在") is None
    for row in sr.as_dicts():
        assert row["label"] and row["short"] and row["hint"], f"{row['key']} 缺文案"


def test_default_box_is_a_bottom_band():
    """推荐框是「铺满宽的底部一条」——模型烧的字幕九成在那儿。"""
    box = sr.default_box(W, H)
    assert box["x"] == 0 and box["w"] == W, box
    assert box["y"] + box["h"] <= H, box
    assert box["h"] >= 24, box
    # 小画面也不许出负数或越界
    small = sr.default_box(64, 48)
    assert small["y"] >= 0 and small["y"] + small["h"] <= 48, small


def test_box_is_clamped_into_the_frame_and_made_even():
    """越界的框夹回来、宽高取偶数——**故意宽容**：用户拖到边界差一两像素不该报错。"""
    got = sr.sanitize_box({"x": -50, "y": 9999, "w": 99999, "h": 7}, W, H)
    assert got == {"x": 0, "y": H - 8, "w": W, "h": 8}, got
    assert sr.sanitize_box({"x": 1.9, "y": 3.7, "w": 101, "h": 51}, W, H) == {
        "x": 0, "y": 2, "w": 100, "h": 50}
    # 少了 / 写坏了字段：不炸，退回默认
    assert sr.sanitize_box({}, W, H)["w"] == W
    assert sr.sanitize_box({"x": "abc", "w": None}, W, H)["h"] == H
    assert sr.sanitize_box(None, W, H)["h"] == H


def test_a_degenerate_box_is_caught_here_not_by_ffmpeg():
    """零宽 / 零高的框 ffmpeg 不报错（实测退出码 0），所以最小边必须自己拦。"""
    assert sr.MIN_SIDE >= 8
    box = sr.sanitize_box({"x": 10, "y": 10, "w": 0, "h": 0}, W, H)
    assert box["w"] == sr.MIN_SIDE and box["h"] == sr.MIN_SIDE, box
    assert sr.box_problem(box, W, H) == ""
    # 画面本身比最小边还小时，直接说做不了
    assert sr.box_problem({"x": 0, "y": 0, "w": 4, "h": 4}, 4, 4) != ""
    # 越界的框（理论上 sanitize 之后不会出现）也要拦
    assert sr.box_problem({"x": 0, "y": 0, "w": W, "h": H + 2}, W, H) != ""


def test_edges_are_detected_with_a_tolerance():
    assert sr.edges_of(_bottom(), W, H) == ["left", "right", "bottom"]
    assert sr.edges_of(_middle(), W, H) == []
    top = {"x": 0, "y": 0, "w": W, "h": 100}
    assert sr.edges_of(top, W, H) == ["left", "top", "right"]
    # 差一两像素也算贴边：字幕带与边缘之间常常只剩一条细缝
    near = {"x": 0, "y": H - 108, "w": W, "h": 106}
    assert "bottom" in sr.edges_of(near, W, H)
    inner = {"x": 4, "y": 100, "w": W - 8, "h": 100}
    assert sr.edges_of(inner, W, H) == []


def test_delogo_is_inset_when_the_box_touches_an_edge():
    """抹平的框不能碰画面边缘，贴边时**自动内缩 1px**（实测 1px 就够）。

    断的是「框完全落在画面内」这个不变量，而不是几个硬编码数字——ffmpeg 要的就是
    这个条件，数字随内缩量变，写死了改一次常量就要改一次断言。
    """
    bottom = _bottom()
    got = _parse_delogo(sr.filter_args("delogo", bottom, W, H)[1])
    assert got["x"] > 0 and got["y"] > 0, got
    assert got["x"] + got["w"] < W and got["y"] + got["h"] < H, f"抹平的框必须完全在画面内：{got}"
    assert abs(got["y"] - bottom["y"]) <= 2 and abs(got["h"] - bottom["h"]) <= 2, got
    # 完全在画面内部的框：一个像素都不内缩，别白丢画面
    inner = {"x": 100, "y": 200, "w": 600, "h": 80}
    assert _parse_delogo(sr.filter_args("delogo", inner, W, H)[1]) == inner
    # 四条边都贴（整幅）：内缩之后仍然完全在画面内
    full = _parse_delogo(sr.filter_args("delogo", {"x": 0, "y": 0, "w": W, "h": H}, W, H)[1])
    assert full["x"] > 0 and full["y"] > 0 and full["x"] + full["w"] < W and full["y"] + full["h"] < H, full


def test_crop_only_works_flush_against_an_edge():
    """框贴底 → 保留框上方那部分；贴顶 → 保留框下方那部分；在中间 → 不让用。"""
    bottom = sr.filter_args("crop", _bottom(), W, H)
    assert bottom == ["-vf", f"crop={W}:{H - 110}:0:0"], bottom
    top = sr.filter_args("crop", {"x": 0, "y": 0, "w": W, "h": 100}, W, H)
    assert top == ["-vf", f"crop={W}:{H - 100}:0:100"], top
    try:
        sr.filter_args("crop", _middle(), W, H)
    except ValueError as e:
        assert "错位" in str(e), e
    else:
        raise AssertionError("中间的框用裁掉应当被拒绝")


def test_cover_borrows_from_above_then_below():
    args = sr.filter_args("cover", _middle(), W, H)
    assert args[0] == "-filter_complex", args
    # 上方够 → 从框上方借
    assert f"crop=900:{sr.COVER_STRIP}:100:{300 - sr.COVER_GAP - sr.COVER_STRIP}" in args[1], args[1]
    # 上方不够（框贴着画面顶部）→ 从框下方借
    near_top = {"x": 0, "y": 2, "w": W, "h": 60}
    args2 = sr.filter_args("cover", near_top, W, H)
    assert f"crop={W}:{sr.COVER_STRIP}:0:{2 + 60 + sr.COVER_GAP}" in args2[1], args2[1]
    # 上下都没有（框占满整个高度）→ 不让用
    try:
        sr.filter_args("cover", {"x": 0, "y": 0, "w": W, "h": H}, W, H)
    except ValueError as e:
        assert "相邻" in str(e), e
    else:
        raise AssertionError("框占满高度时遮住应当被拒绝")


def test_blur_radius_is_clamped_for_tiny_boxes():
    """半径按框高定，但必须夹住：框只有 8px 高时还按比例算，boxblur 会直接失败。"""
    for h in (6, 8, 10, 24, 60, 200):
        box = {"x": 0, "y": 100, "w": 400, "h": h}
        expr = sr.filter_args("blur", box, W, H)[1]
        radius = int(expr.split("boxblur=")[1].split(":")[0])
        assert radius >= 1, (h, expr)
        assert radius <= 40, (h, expr)
        assert radius <= max(1, min(box["w"], box["h"]) // 2), (h, expr)


def test_unknown_method_is_rejected():
    for bad in ("", "不认识", "DELOGO "):
        try:
            sr.filter_args(bad or "x", _bottom(), W, H)
        except ValueError:
            continue
        raise AssertionError(f"「{bad}」应当被拒绝")


def test_every_method_row_explains_why_it_is_unavailable():
    """不可用的手法**仍然列出来**并写清原因——静默消失会让用户以为程序坏了。"""
    rows = {r["key"]: r for r in sr.method_rows(_bottom(), W, H)}
    assert len(rows) == 4
    assert rows["crop"]["available"] is True, "贴底的框上裁掉可用"
    assert "内缩 1px" in rows["delogo"]["hint"], "贴边时要说清会内缩"
    rows_mid = {r["key"]: r for r in sr.method_rows(_middle(), W, H)}
    assert rows_mid["crop"]["available"] is False and rows_mid["crop"]["reason"], rows_mid["crop"]
    rows_full = {r["key"]: r for r in sr.method_rows({"x": 0, "y": 0, "w": W, "h": H}, W, H)}
    assert rows_full["cover"]["available"] is False and rows_full["cover"]["reason"]
    for row in list(rows.values()) + list(rows_mid.values()):
        assert row["available"] or row["reason"], row


def test_default_method_is_delogo():
    """默认抹平：两种底图上实测都最高（平整 41.6 dB / 纹理 20.3 dB）。"""
    assert sr.default_method(_bottom(), W, H) == "delogo"
    assert sr.default_method(_middle(), W, H) == "delogo"
    assert sr.default_method({"x": 0, "y": 0, "w": W, "h": H}, W, H) == "delogo"


# ------------------------------------------------------------------ 真跑 ffmpeg


def _run(coro):
    return asyncio.run(coro)


def test_every_available_method_really_renders_a_frame():
    """四种手法**在有真 ffmpeg 的机器上都要真渲出来**，而不是只把参数拼对。

    `delogo` 贴边报错、`boxblur` 半径过大报错、`crop` 取到奇数高报错——这三种错
    都只在真跑时才暴露，拼字符串是看不出来的。所以这里连**细到 8px 的框**也跑一遍。
    """

    async def case():
        ff, _ = await ffmpeg_service._resolve_binaries()
        if not ff:
            return "skip"
        work = Path(tempfile.mkdtemp(prefix="subrm_test_"))
        src = work / "src.mp4"
        ok, err = await ffmpeg_service._run(
            [ff, "-y", "-f", "lavfi", "-i", f"testsrc2=size={W}x{H}:rate=25:duration=1",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", str(src)], 300)
        assert ok, err
        try:
            # 贴底 4 种（含裁掉）+ 中间 3 种 + 细条 3 种（后两种都没有贴边，裁掉按设计不可用）
            combos = [
                ("贴底", _bottom()),
                ("中间", _middle()),
                ("细条", {"x": 0, "y": 100, "w": W, "h": 8}),
            ]
            rendered: dict[str, tuple[int, int] | None] = {}
            for name, box in combos:
                for row in sr.method_rows(box, W, H):
                    if not row["available"]:
                        continue
                    raw = await ffmpeg_service.render_removal_preview(
                        src, 0.2, box=box, method=str(row["key"]), width=W, height=H)
                    assert raw[:3] == b"\xff\xd8\xff", f"{name}/{row['key']} 不是 JPEG"
                    tmp = work / "prev.jpg"
                    tmp.write_bytes(raw)
                    rendered[f"{name}/{row['key']}"] = image_size.read_image_size_from_file(tmp)
            expected = {
                "贴底/delogo", "贴底/cover", "贴底/blur", "贴底/crop",
                "中间/delogo", "中间/cover", "中间/blur",
                "细条/delogo", "细条/cover", "细条/blur",
            }
            assert set(rendered) == expected, f"跑到的组合不对：{sorted(rendered)}"

            # 只有裁掉会改尺寸，其余三种尺寸必须不变
            for key, size in rendered.items():
                assert size is not None, f"{key} 读不出尺寸"
                if key.endswith("crop"):
                    assert size == (W, _bottom()["y"]), f"{key} 应当变矮：{size}"
                else:
                    assert size == (W, H), f"{key} 不该改尺寸：{size}"

            # 整段跑一遍：抹平尺寸不变、音轨照旧带过去
            out = await ffmpeg_service.remove_subtitles(src, box=_bottom(), method="delogo",
                                                       width=W, height=H)
            info = await ffmpeg_service.probe(out)
            assert (info.get("width"), info.get("height")) == (W, H), info
            assert info.get("has_audio") is True, "音轨被弄丢了（这一步只该改画面）"
            out.unlink(missing_ok=True)

            out2 = await ffmpeg_service.remove_subtitles(src, box=_bottom(), method="crop",
                                                         width=W, height=H)
            info2 = await ffmpeg_service.probe(out2)
            assert info2.get("height") == _bottom()["y"], info2
            out2.unlink(missing_ok=True)
            return "ok"
        finally:
            shutil.rmtree(work, ignore_errors=True)

    assert _run(case()) in ("ok", "skip")


def test_removal_frame_reports_source_size_not_the_scaled_one():
    """取帧回的是**源视频**尺寸（框的坐标系），显示用的图缩到 1280 以内。

    回成显示尺寸的话，框一换算就错位——而且是「用户眼睛看着对、处理的是别处」那种错位。
    """

    async def case():
        ff, _ = await ffmpeg_service._resolve_binaries()
        if not ff:
            return "skip"
        work = Path(tempfile.mkdtemp(prefix="subrm_frame_"))
        big = work / "big.mp4"
        ok, err = await ffmpeg_service._run(
            [ff, "-y", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=25:duration=1",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p", str(big)],
            300)
        assert ok, err
        try:
            raw, w, h = await ffmpeg_service.removal_frame(big, 0.2)
            assert (w, h) == (1920, 1080), (w, h)
            tmp = work / "frame.jpg"
            tmp.write_bytes(raw)
            shown = image_size.read_image_size_from_file(tmp)
            assert shown is not None and max(shown) <= 1280, shown
            assert raw[:3] == b"\xff\xd8\xff"
            return "ok"
        finally:
            shutil.rmtree(work, ignore_errors=True)

    assert _run(case()) in ("ok", "skip")


# ------------------------------------------------- 高质量档（#44：只给地址、不代装）


@contextlib.contextmanager
def _data_dir(root: Path):
    """把数据目录指到临时目录（安装包就落在它下面的 `engines/_downloads` 里）。"""
    real = engine_install.settings

    class _S:
        data_dir = root

    engine_install.settings = _S  # type: ignore[assignment]
    try:
        yield
    finally:
        engine_install.settings = real


def test_the_high_quality_tier_is_an_engine_we_do_not_install():
    """这一档指向的必须是清单里**我们不代装**那一项，而且不许混进四种手法。

    它是 AI 补全（字幕那一带按周围画面重画出来），与上面四种「盖像素」不是一回事：
    自带一整套运行环境、装完是它自己的界面。口径一旦被改成「我们代装」，
    用户就会在下完 731MB 之后发现还要自己装一遍——这条把口径钉在清单的形状上。
    """
    engine = local_engines.by_key(sr.LOCAL_TIER_KEY)
    assert engine is not None, f"清单里没有 {sr.LOCAL_TIER_KEY} 这一项"
    assert engine.archive == "installer", engine.archive
    assert engine.marker == "", "安装程序那一档的目录不归我们管，不该有判据"
    assert sr.LOCAL_TIER_KEY not in sr._BY_KEY, "高质量档不是我们能执行的手法，不该混进 METHODS"


def test_the_tier_reports_the_installer_never_a_verdict_of_its_own():
    """只回安装包的事实：下没下、在哪儿。**不许编一个「装好了」的结论。**"""
    with tempfile.TemporaryDirectory() as tmp:
        with _data_dir(Path(tmp)):
            fresh = engine_install.external_tier(sr.LOCAL_TIER_KEY)
            assert fresh["key"] == sr.LOCAL_TIER_KEY
            assert fresh["downloaded"] is False
            assert fresh["partialBytes"] == 0 and fresh["partialText"] == ""
            assert fresh["downloading"] is False
            # 路径也照样回：界面能照实说「它会下到这儿」，比留一句「还没下」有用
            assert "engines" in fresh["installerPath"], fresh["installerPath"]
            assert "installed" not in fresh, "不许回「装没装」这种我们判不出来的结论"
            assert fresh["sizeText"] and fresh["url"].startswith("https://")

            # 下了一半：照实说是半个，并且能接着下
            half = Path(fresh["installerPath"])
            half.parent.mkdir(parents=True, exist_ok=True)
            half.write_bytes(b"x" * 1024)
            part = engine_install.external_tier(sr.LOCAL_TIER_KEY)
            assert part["downloaded"] is False
            assert part["partialBytes"] == 1024 and part["partialText"] == "1 KB", part["partialText"]

            # 下完整了：改成说它在磁盘上的哪条路径
            engine = local_engines.by_key(sr.LOCAL_TIER_KEY)
            assert engine is not None
            half.write_bytes(b"x" * engine.size)
            done = engine_install.external_tier(sr.LOCAL_TIER_KEY)
            assert done["downloaded"] is True
            assert done["partialBytes"] == 0 and done["partialText"] == ""
            assert done["installerPath"] == str(half)


def test_an_unknown_tier_key_is_empty_not_an_empty_shell():
    """清单里没有这一项时回空字典：界面据此不显示那一块，而不是显示一堆 undefined。"""
    assert engine_install.external_tier("清单里没有这一项") == {}


def test_the_frame_response_carries_the_tier_state():
    """高质量档的状态要跟「帧 + 尺寸 + 推荐框 + 四种手法」**同一批**回来。

    为什么另开一个接口不行：这个弹窗本来就是一次请求把那一批拿齐的（分几次会出现
    「框按旧尺寸画、手法按新尺寸判」这种对不上的中间状态），高质量档的状态与它们同级——
    界面要能当场答上「要真无痕的另一档在哪儿、下了没有」。
    """
    src = (BACKEND / "app" / "routers" / "director.py").read_text(encoding="utf-8")
    start = src.index('@router.post("/subtitle-removal/frame")')
    end = src.index('@router.post("/subtitle-removal/check")', start)
    body = src[start:end]
    assert '"localTier"' in body, "取帧那一份响应里没带高质量档的状态"
    assert "subtitle_removal.LOCAL_TIER_KEY" in body, (
        "路由里把高质量档的 key 写死了：这个 key 属于去字幕这一块的口径"
    )


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
