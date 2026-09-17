"""#21 整图运行二次确认的前端静态检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_frontend_preview.py

后端那侧在 `test_canvas_preview.py` 里钉住了「预估是纯读、且与真跑同源」，
但**这份预估到底有没有拦住用户**取决于前端怎么接。这里守四件事：

1. **「运行整图」必须先预估、再弹确认**。若 runAll 里还留着直接 `runCanvas`，
   就等于扣费确认被绕过——按钮上写着预估，点下去却已经跑起来了。
2. **确认框必须有出口**（先不跑），且真正启动只发生在确认回调里。
3. **条数要区分「0 条」与「现在算不出来」**。把 null 显示成 0 会让用户以为不用花钱。
4. **已有产物会被重做**这条必须出现在弹窗里——整图最贵的一笔就是它。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
PAGE = SRC / "pages" / "CanvasPage.tsx"


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def _body(src: str, name: str) -> str:
    """切出组件内某个 `const name = ...` 的正文（到下一个顶层声明之前）。"""
    start = src.index(f"const {name} = ")
    ends = [
        p
        for p in (
            src.find("\n  const ", start + 1),
            src.find("\n  function ", start + 1),
            src.find("\n  useEffect(", start + 1),
        )
        if p != -1
    ]
    return src[start : min(ends) if ends else len(src)]


def test_run_all_previews_before_starting_anything():
    """点「运行整图」只能拿到预估，不能顺手把整图跑起来。"""
    src = _page()
    run_all = _body(src, "runAll")
    assert "api.previewCanvas(" in run_all, "runAll 没有预估就跑了，二次确认被绕过"
    assert "api.runCanvas(" not in run_all, (
        "runAll 里直接调了 runCanvas —— 用户还没确认，任务已经派出去了"
    )
    assert "setRunPreview(" in run_all, "预估结果没有交给弹窗，用户看不到任何东西"


def test_only_the_confirm_callback_actually_starts_the_graph():
    """真正启动整图只发生在确认回调里，而且启动前不重复预估。"""
    confirm = _body(_page(), "confirmRunAll")
    assert "api.runCanvas(" in confirm, "确认回调没有启动整图"
    assert "setRunPreview(null)" in confirm, "启动后没关掉确认框"
    assert "api.previewCanvas(" not in confirm, "确认时又估了一遍：估的数与跑的不是同一时刻"


def test_confirm_dialog_keeps_a_way_out():
    """有确认就必须有退出：没有出口的确认框就是阻断。"""
    src = _page()
    assert "function RunConfirmDialog(" in src, "确认弹窗组件不见了"
    dialog = src[src.index("function RunConfirmDialog(") : src.index("/** 节点属性浮框")]
    assert "先不跑" in dialog, "确认框没有「先不跑」"
    assert "开始运行" in dialog, "确认框没有「开始运行」"
    assert "onConfirm" in dialog and "onClose" in dialog, "确认框没有接回调"


def test_unknown_count_is_not_shown_as_zero():
    """条数待定要照实说，不能渲染成 0 条（那会让人误以为这一跑不花钱）。"""
    src = _page()
    dialog = src[src.index("function RunConfirmDialog(") : src.index("/** 节点属性浮框")]
    assert "n.count === null" in dialog, "没有把「条数待定」和「0 条」分开"
    assert "条数待定" in dialog, "待定的节点没有可读文案"
    # 后端到底能不能算出条数，也要显示出来，别让用户以为预估是准的
    assert "等上游跑完才知道" in dialog or "pendingNodes" in dialog, (
        "弹窗没有交代「有些节点现在算不出条数」"
    )


def test_dialog_surfaces_the_cost_and_the_redo():
    """弹窗必须把「要花几次调用」和「哪些产物会被重做」摆出来。"""
    src = _page()
    dialog = src[src.index("function RunConfirmDialog(") : src.index("/** 节点属性浮框")]
    assert "totals.tasks" in dialog, "弹窗没有显示这一跑会派多少任务"
    assert "totals.steps" in dialog, "弹窗没有显示会执行几个节点"
    assert "hasOutput" in dialog and "会重做" in dialog, (
        "弹窗没有提示「已有产物会被重做」——整图最贵的一笔就是它"
    )
    assert "preview.notes.map" in dialog, "后端给的说明被丢掉了（前端只该渲染，不该另写一套口径）"


def test_pages_do_not_call_legacy_blocking_run():
    """`runCanvas(projectId)` 不带 nodeId 的位置只应有确认回调一处。"""
    src = _page()
    hits = re.findall(r"api\.runCanvas\(\s*projectId\s*\)", src)
    assert len(hits) == 1, f"整图启动入口有 {len(hits)} 处，二次确认会被绕过"


def test_api_and_route_agree():
    """前端调的端点，后端必须真有（防两处慢慢漂移）。"""
    api_src = (SRC / "api.ts").read_text(encoding="utf-8")
    router = (ROOT / "backend" / "app" / "routers" / "canvas.py").read_text(encoding="utf-8")
    page = _page()

    assert "previewCanvas:" in api_src, "api.ts 里没有 previewCanvas"
    assert "`/api/canvas/${projectId}/preview`" in api_src, "previewCanvas 没打到预览端点"
    # router 自己带 prefix="/api/canvas"，所以文件里只写后半截
    assert '@router.get("/{project_id}/preview")' in router, "后端没有预览路由"
    assert "api.previewCanvas(" in page, "没有任何页面在用 previewCanvas（死代码）"


def test_backend_preview_is_read_only_by_contract():
    """预览走的是 dry_run 的建任务代码——前端这一整套提醒才有依据。"""
    runner = (ROOT / "backend" / "app" / "services" / "canvas_runner.py").read_text(
        encoding="utf-8"
    )
    assert "async def preview_graph(" in runner, "后端没有 preview_graph"
    preview = runner[runner.index("async def preview_graph(") :]
    assert "dry_run=True" in preview, "预览没走 dry_run，会一边估一边落库"


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
