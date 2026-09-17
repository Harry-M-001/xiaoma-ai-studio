"""生成前软校验的前端静态检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_frontend_preflight.py

后端那侧已经在 `test_preflight.py` / `test_clock.py` 里钉住了「只告警不阻断」，
但**功能会不会真的变成阻断**，取决于前端怎么用它。这里守两条：

1. **预检调用必须包在 `runPreflight` 里**。它内部吞异常、降级成「没有告警」。
   裸调 `api.preflightXxx()` 的话，后端一抖就会把异常抛进生成路径——
   用户点了生成，界面弹一个报错，任务却没提交。辅助功能反客为主。
2. **有告警就必须弹窗**，即判定条件必须是「有告警 **或** 用户开了二次确认」，
   而不是「用户开了二次确认」。少了前半句，那些关掉二次确认的老用户
   就永远看不到提醒——他们关掉的是「多点一次」，不是「被蒙着走」。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"

# 用到预检的页面：每个都必须在提交前过一遍预检
PAGES = {
    "pages/ImagePage.tsx": "图片生成（单次 + 批量）",
    "pages/VideoPage.tsx": "视频生成",
}

NOTICE = SRC / "components" / "PreflightNotice.tsx"


def _text(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


def test_run_preflight_swallows_its_own_failures():
    """降级必须发生在 `runPreflight` 内部，而且是「返回空告警」而不是「抛出去」。"""
    src = NOTICE.read_text(encoding="utf-8")
    assert "export async function runPreflight" in src, "runPreflight 不见了"
    assert "catch" in src, "runPreflight 没有兜底，预检一挂就会连累生成"
    # 兜底时返回空数组，语义是「这次没拿到告警」，而不是「这次不许生成」
    assert re.search(r"catch\s*(\(\s*\w*\s*\))?\s*\{[^}]*return\s*\[\]", src), (
        "runPreflight 的 catch 分支应当返回空告警列表（静默降级），不能把错误抛出去"
    )


def test_pages_call_preflight_only_through_run_preflight():
    """页面里不许裸调预检接口。

    判据：`api.preflightXxx(` 必须出现在 `runPreflight(` 之后的 3 行内——
    也就是作为它的回调返回出去，而不是被直接 await。
    """
    offenders: list[str] = []
    for rel in PAGES:
        lines = _text(rel).splitlines()
        for i, line in enumerate(lines):
            if not re.search(r"api\.preflight\w*\(", line):
                continue
            window = "\n".join(lines[max(0, i - 3) : i + 1])
            if "runPreflight(" not in window:
                offenders.append(f"{rel}:{i + 1}")
    assert not offenders, (
        f"这些地方绕过了 runPreflight 直接调预检：{offenders}"
        "（预检失败会连带把生成也挡掉）"
    )


def test_warnings_always_force_the_dialog():
    """有告警必须弹窗，且判定条件是「有告警 or 开了二次确认」。"""
    for rel in PAGES:
        src = _text(rel)
        assert re.search(r"found\.length\s*>\s*0\s*\|\|\s*confirmGen", src), (
            f"{rel} 的提交判定不再是「有告警 or 二次确认」——"
            "关掉二次确认的用户会看不到任何提醒"
        )
        assert "<PreflightNotice warnings={warnings} />" in src, (
            f"{rel} 弹窗里没有渲染告警列表"
        )


def test_dialog_keeps_a_way_to_proceed():
    """告警弹窗里必须有「仍然生成」——没有出口的提醒就是阻断。"""
    for rel in PAGES:
        src = _text(rel)
        assert "仍然生成" in src, f"{rel} 的告警弹窗没有「仍然生成」，用户被卡住了"
        assert "返回修改" in src, f"{rel} 的告警弹窗没有「返回修改」，用户只能硬着头皮上"


def test_backend_endpoints_back_every_page_that_uses_them():
    """前端调了哪个预检端点，后端就必须有对应路由（防两处慢慢漂移）。"""
    router = (ROOT / "backend" / "app" / "routers" / "generation.py").read_text(encoding="utf-8")
    api_src = (SRC / "api.ts").read_text(encoding="utf-8")
    pages = "\n".join(_text(rel) for rel in PAGES)

    mapping = {
        "preflightImage": "/api/images/preflight",
        "preflightImageBatch": "/api/images/batch/preflight",
        "preflightVideo": "/api/videos/preflight",
    }
    for name, path in mapping.items():
        assert f"{name}:" in api_src, f"api.ts 里没有 {name}"
        assert f'"{path}"' in api_src, f"api.ts 里的 {name} 没打到 {path}"
        # router 自己带 prefix="/api"，所以文件里只写后半截
        assert f'"{path.removeprefix("/api")}"' in router, f"后端没有 {path} 这个路由"
        assert f"api.{name}(" in pages, f"没有任何页面在用 {name}（死代码）"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001  一个用例炸了别把剩下的都带下去
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
