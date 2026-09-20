"""导演台「转场与转场音效」的前端静态检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_frontend_director_transition.py

这一项最容易做错的地方**不在后端**，而在界面上：

1. **成片会比硬切短**（转场是把相邻两段交叠，不是插一段新的）。这句话如果只写在文档里，
   用户就是拿到片子发现短了才知道。所以前端必须把「成片约 N 秒、比硬切短 M 秒」摆在
   合并按钮旁边，而且要在**点下去之前**摆出来。
2. **放不下就不该让他点**。转到 ffmpeg 那边才报 `Invalid duration`，用户看不懂，
   也不知道该改哪一段。按钮该灰着，并说明是第几段多长。
3. **转场表不能在前端抄一份**。抄了就可能出现「界面能选出一种后端不认识的转场」，
   而报错要到合并时才知道。所以选项必须来自 `GET /api/director/transitions`。
4. **合并请求要真的带上那几个参数**。只传 asset_ids 的话，用户选的转场与音效会被
   默默忽略——界面看着生效了，成片却是硬切。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "frontend" / "src" / "pages" / "DirectorPage.tsx"
API = ROOT / "frontend" / "src" / "api.ts"


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def _merge_fn(src: str) -> str:
    start = src.index("const doMerge = ")
    nxt = src.find("\n  const totalDur", start)
    return src[start : nxt if nxt != -1 else len(src)]


def test_merge_sends_the_transition_and_sfx():
    """合并请求必须带上转场/时长/音效——只传 asset_ids 会让用户的选择被默默忽略。"""
    body = _merge_fn(_page())
    assert "api.mergeVideos({" in body, "合并还是只传了片段 id，用户选的转场会被丢掉"
    for key in ("assetIds", "transition", "transitionSeconds", "sfxArgs"):
        assert key in body, f"合并请求里少了 {key}"


def test_merge_is_blocked_when_the_transition_does_not_fit():
    """放不下时不该让他点下去——ffmpeg 的 Invalid duration 用户看不懂。"""
    body = _merge_fn(_page())
    assert "preview?.problem" in body, "放不下时 doMerge 没拦"
    src = _page()
    assert "disabled={merging || Boolean(preview?.problem)}" in src, "按钮在放不下时没变灰"


def test_the_shortfall_is_shown_before_merging():
    """「成片会比硬切短」必须在点合并之前说出来。"""
    src = _page()
    assert "shortfallSeconds" in src, "页面上没有「比硬切短多少」"
    assert "比硬切短" in src, "没有那句话本身"
    assert "totalSeconds" in src, "没有把成片时长摆出来"


def test_the_problem_from_the_backend_is_shown_verbatim():
    """后端给的那句理由（「第 2 段只有 0.8 秒」）要原样显示，别在前端另写一套判断。"""
    src = _page()
    assert "preview.problem" in src and "director-merge-warn" in src, "放不下的理由没摆出来"


def test_options_come_from_the_backend_not_a_local_copy():
    """转场选项必须来自 `GET /api/director/transitions`，前端不许自己抄一张表。"""
    src = _page()
    assert ".transitionOptions()" in src, "没有去后端拿转场预设"
    assert "trOpts?.presets" in src, "下拉不是由后端预设生成的"
    for hardcoded in ('"dissolve"', "'dissolve'", '"fadeblack"', "'fadeblack'", '"wipeleft"'):
        assert hardcoded not in src, (
            f"前端硬编码了转场名 {hardcoded}——抄一份的话，界面能选出后端不认识的东西，"
            "报错要到合并时才知道"
        )


def test_the_preview_is_recomputed_when_the_choices_change():
    """改片段/改转场/改时长都要重算一次，否则提示会停在上一次的数字上。"""
    src = _page()
    start = src.index("api\n      .mergePreview(") if "api\n      .mergePreview(" in src else src.index("mergePreview(")
    tail = src[start:]
    end = tail.index("}, [")
    deps = tail[end : tail.index("]);", end)]
    for dep in ("clipIds.join", "transition", "trSeconds", "sfxPick"):
        assert dep in deps, f"预览没有跟着 {dep} 重算，提示会停在旧数字上：{deps}"


def test_sfx_can_come_from_the_users_own_audio():
    """音效下拉里要有「资产库里我自己的音频」这条——不然那条接口能力在界面上没法用。"""
    src = _page()
    assert "ASSET_PREFIX" in src and "audioAssets" in src, "下拉里没有「我自己的音频」"
    assert 'kind: "audio"' in src, "没有去读音频资产"
    assert "sfxAssetId" in src, "选了自己的音频却没把它的 id 发给后端"


def test_api_sends_the_same_fields_to_preview_and_merge():
    """预览与合并必须发同一组字段：两处不一致的话，「弹窗写 13 秒、导出来 12.5 秒」。"""
    src = API.read_text(encoding="utf-8")
    start = src.index("mergePreview: (args: MergeArgs)")
    end = src.index("// ---- 项目 ----", start)
    both = src[start:end]
    assert "mergeVideos: (args: MergeArgs)" in both, "mergeVideos 的入参不是 MergeArgs"
    # 带冒号一起数：`transition:` 与 `transition_seconds:` 是两回事
    for field in ("asset_ids:", "transition:", "transition_seconds:", "sfx:", "sfx_asset_id:"):
        assert both.count(field) == 2, f"{field} 在预览与合并里发的次数不一样（应当各一次）"


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
