"""去字幕（前端接线）的静态检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_frontend_subtitle_removal.py

这一项的界面上有四件「看着对、其实是错的」很容易发生，所以逐条钉住：

1. **框的坐标必须是源视频像素。** 画面在界面上被 CSS 缩过，若把显示尺寸当坐标存起来，
   换个窗口大小就会「看着框住了、处理的是别处」——而且用户很难描述这个现象。
2. **最小边要和后端是同一个数。** 后端会把太小的框夹回来，但拖动时就该停住：
   不停的话用户以为拖动了、其实没动（后端悄悄改成了 8px）。
3. **用不了的手法也要列出来。** 灰掉 + 写清原因；静默消失会让用户以为程序坏了。
4. **预览是后端真渲的一帧**，改过框或换过帧之后必须说「这是旧预览」——
   对着旧结果下判断比没有结果更糟。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
DIRECTOR = SRC / "pages" / "DirectorPage.tsx"
API = SRC / "api.ts"
TYPES = SRC / "types.ts"
STYLES = SRC / "styles.css"
BACKEND_SERVICE = ROOT / "backend" / "app" / "services" / "subtitle_removal.py"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_api_and_types_match_the_backend():
    api = _text(API)
    for path in ("/api/director/subtitle-removal/frame",
                 "/api/director/subtitle-removal/preview",
                 "/api/director/subtitle-removal"):
        assert path in api, f"api 里没有 {path}"
    for name in ("removalFrame:", "removalPreview:", "removeSubtitles:"):
        assert name in api, f"api 里没有 {name}"
    # 后端收的是 snake_case，前端要转过去
    assert "asset_id: args.assetId" in api, "资产 id 没转成后端的 asset_id"

    types = _text(TYPES)
    for name in ("RemovalBox", "RemovalMethod", "RemovalFrame", "RemovalPreview"):
        assert f"interface {name}" in types, f"类型里没有 {name}"
    for field in ("available", "reason", "defaultMethod", "methods", "duration"):
        assert field in types, f"类型里没有 {field}"


def test_box_is_dragged_in_source_pixels():
    """拖动换算：屏幕位移 ÷ 容器宽度 × 源视频宽。**不能用显示尺寸当坐标。**"""
    src = _text(DIRECTOR)
    assert "function RemovalBoxPicker(" in src, "没有框选组件"
    # 拖动时取容器矩形，按比例换算成源视频像素
    assert "holder.current?.getBoundingClientRect()" in src, "拖动没取容器矩形"
    assert "((e.clientX - d.px) / d.rect.width) * natural.w" in src, "横轴换算不对"
    assert "((e.clientY - d.py) / d.rect.height) * natural.h" in src, "纵轴换算不对"
    # 框在 DOM 上用百分比定位：窗口缩放不会错位
    assert "left: pct(box.x, natural.w)" in src, "框没有按比例定位"
    assert "width: pct(box.w, natural.w)" in src, "框宽没有按比例定位"
    # 四个角都能拖
    for mode in ('mode: "nw"', 'mode: "ne"', 'mode: "sw"', 'mode: "se"'):
        assert mode in src, f"缺角上的拖拽点 {mode}"
    assert "onPointerDown={begin(" in src, "框/角上没有按下事件"


def test_pointer_capture_keeps_the_drag_alive():
    """拖出画面外再松手也要收得到——不然框会「粘」在鼠标上继续动。"""
    src = _text(DIRECTOR)
    assert "setPointerCapture?.(e.pointerId)" in src, "没有捕获指针"
    assert "onPointerUp={end}" in src and "onPointerCancel={end}" in src, "没有收尾处理"


def test_min_side_matches_the_backend():
    """前端的 BOX_MIN 必须等于后端的 MIN_SIDE。

    不一致的后果：前端允许拖出一个后端会悄悄改大的框——用户看到的是「我明明框了这么小」，
    实际处理的是另一块。这类「参数被悄悄改掉」的问题最难解释。
    """
    backend = _text(BACKEND_SERVICE)
    m = re.search(r"^MIN_SIDE\s*=\s*(\d+)", backend, re.M)
    assert m, "后端没找到 MIN_SIDE"
    front = re.search(r"const BOX_MIN\s*=\s*(\d+)", _text(DIRECTOR))
    assert front, "前端没找到 BOX_MIN"
    assert int(m.group(1)) == int(front.group(1)), (
        f"前后端最小边不一致：后端 {m.group(1)}、前端 {front.group(1)}"
    )


def test_unavailable_methods_are_listed_with_a_reason():
    """不可用的手法仍然列出来（灰掉 + 原因），不静默从列表里消失。"""
    src = _text(DIRECTOR)
    assert 'className={`subrm-method${m.key === method ? " on" : ""}' in src, "手法没有选中/未选中态"
    assert 'm.available ? "" : " off"' in src, "没有「用不了」的样式分支"
    assert "disabled={!m.available}" in src, "用不了的手法还能选"
    assert "m.available ? m.short : `用不了：${m.reason}`" in src, "用不了的手法没写原因"
    styles = _text(STYLES)
    assert ".subrm-method.off" in styles and ".subrm-method.on" in styles, "缺手法样式"


def test_preview_is_real_and_staleness_is_stated():
    """预览走后端真渲染，并且**改过框/换过帧要说明这是旧预览**。"""
    src = _text(DIRECTOR)
    assert "api.removalPreview(" in src, "没有调预览接口"
    # 预览图直接来自接口回的 data URI，而不是前端画一块
    assert '<img src={preview.image} alt="处理后" />' in src, "预览不是接口回的那一帧"
    assert "框或帧改过了，这是旧预览" in src, "改过框之后没说明预览是旧的"
    # 换帧、拖框、换预设、换手法都要把预览标成旧的
    assert src.count("setStale(true)") >= 4, "有改动路径没把预览标成旧的"
    assert "setStale(false)" in src, "跑完预览之后没有把「旧」清掉"
    # 手法的代价要跟着预览显示出来（用户看着效果、同时看到代价）
    assert "{preview.note}" in src, "没显示手法的代价"


def test_apply_replaces_the_clip_and_says_the_original_survives():
    """产物是**新资产**：流程里替换这一段，但要说清原片还在。"""
    src = _text(DIRECTOR)
    assert "api.removeSubtitles(" in src, "没有调处理接口"
    assert "setClips((prev) => prev.map((c) => (c.id === removing.id ? made : c)))" in src, (
        "没有在时间线上替换这一段"
    )
    assert "原片仍在资产库" in src, "没说明原片还在（用户会以为被覆盖了）"
    # 入口按钮在片段的操作区里，title 要说清这一步做什么
    assert "去字幕：框出画面里那一条字幕" in src, "片段行上没有去字幕入口"
    assert "onClick={() => setRemoving(c)}" in src, "入口没接上"
    # 中文说明「做不到无痕」必须在弹窗里（不能只写在文档里）
    assert "做不到无痕" in src, "没在弹窗里说清这件事做不到无痕"


def test_the_dialog_has_its_own_styles():
    styles = _text(STYLES)
    for cls in ("subrm-dialog", "subrm-canvas", "subrm-box", "subrm-handle",
                "subrm-methods", "subrm-compare", "subrm-placeholder", "subrm-foot"):
        assert f".{cls}" in styles, f"缺样式 .{cls}"


def test_scrollable_flex_boxes_never_collapse():
    """**同一个坑踩了第二次**，所以这条要变成规则来守。

    v1.1.26 的词表盒子：`overflow: hidden` 放在 flex 列里 → `min-height: auto` 失效
    → 被压成 1px，看着在、点不动。这一版 `.subrm-canvas` 一模一样：
    被压扁就等于「框选的那块画面看不见、也拖不动」，而框选正是这个弹窗的主要动作。

    规则：可滚动 flex 列里（`.subrm-body`、`.lint-dialog-body`）的盒子，
    写了 `overflow: hidden` 就必须配 `flex: 0 0 auto`。
    """
    styles = _text(STYLES)
    for name in (".subrm-canvas", ".lint-vocab"):
        start = styles.index(f"{name} {{")
        body = styles[start : styles.index("}", start)]
        if "overflow: hidden" in body:
            assert "flex: 0 0 auto" in body, (
                f"{name} 有 overflow: hidden 却没有 flex: 0 0 auto："
                "在可滚动 flex 列里会被压成 1px（踩过两次）"
            )


def test_method_availability_refreshes_when_the_box_moves():
    """可选项随框变，所以框一变就要重新问后端——否则「界面说能选、点了才报错」。

    这是浏览器走查里抓到的：把框从画面底部拖到中间之后，「裁掉」在界面上仍然是可选的，
    而它这时已经被后端禁止了（框不在边缘，裁掉会让上下画面错位）。
    """
    src = _text(DIRECTOR)
    assert "api.removalCheck(" in src, "没有调「这个框上哪些手法能用」的接口"
    assert "onCommit={() => void refreshMethods()}" in src, "松手后没刷新可选项"
    # 换预设也要刷新（它同样会改框）
    assert "void refreshMethods(nb)" in src, "换预设之后没刷新可选项"
    # 旧响应回来晚了要丢掉，否则会把新框的结论覆盖成旧的
    assert "seq !== checkSeq.current" in src, "没有防旧响应覆盖"
    # 选中的手法变得不可用时退回默认
    assert "row && row.available ? prev : data.defaultMethod" in src, "选中的手法不可用了没退回"
    api = _text(API)
    assert "/api/director/subtitle-removal/check" in api, "api 里没有 check 接口"


if __name__ == "__main__":
    import sys

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
