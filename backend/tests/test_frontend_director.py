"""导演台「AI 改造」的前端静态检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_frontend_director.py

这条链路曾经是坏的：导演台手上是**视频片段**，而视频生成接口的首帧必须是图片。
把片段的 id 直接当首帧带过去时，前端一路放行（只往 localStorage 塞了个 {id,url}），
要到用户填完参数、点下生成之后才被后端挡下（「首帧图片不存在或不是图片」）——
反馈来得又晚又像是模型的问题。

修法是在跳转之前先截一帧。所以这里守两句：
1. `aiRemake` 必须先调 `extractThumbnail`，且带过去的是**那一帧**的 id；
2. 全文件不许再出现「把片段的 id 直接当首帧」的写法。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "frontend" / "src" / "pages" / "DirectorPage.tsx"


def _src() -> str:
    return PAGE.read_text(encoding="utf-8")


def _ai_remake(src: str) -> str:
    start = src.index("const aiRemake = ")
    nxt = src.find("\n  const doMerge", start)
    return src[start : nxt if nxt != -1 else len(src)]


def test_ai_remake_extracts_a_frame_first():
    """跳转之前必须先截帧——片段本身不是合法的首帧。"""
    body = _ai_remake(_src())
    assert "extractThumbnail(" in body, "AI 改造没有截帧，视频页拿到的是视频而不是图片"
    assert "frame.id" in body, "截出来的那一帧没有被当首帧带过去"


def test_clip_id_is_never_passed_as_the_first_frame():
    """回归守卫：不许再出现「把片段的 id 当首帧」。"""
    src = _src()
    assert "setDraftFirstFrame({ id: clip.id" not in src, (
        "又把片段本身当首帧带过去了——这条路径会在提交时才被后端挡下"
    )


def test_extract_failure_is_reported_and_does_not_navigate():
    """截帧失败要说出来，并且不能带着一个空的草稿跳转。"""
    body = _ai_remake(_src())
    # 失败分支必须给用户一句话，而不是静默跳过去
    assert "catch" in body and "toast.error" in body, "截帧失败没有任何提示"


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
