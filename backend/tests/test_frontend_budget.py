"""#30 预算闸与运行对账（前端接线）的静态检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_frontend_budget.py

后端把闸门算得再准，前端接错一处就白做。这里守四类最容易错的地方：

1. **确认必须是一个动作，不是点两次同一个按钮**：超限时按钮要锁住，勾选之后才解锁；
2. **确认值要真的传回去**：只在前端拦住，绕过界面直接调接口就没人管了；
3. **轮询必须会停**：查不到、跑完了、超时了都要收手，卸载页面也要清掉，
   否则一个卡住的任务会让它一直问下去；
4. **预估与实际要放在一起看**：只显示「实际 12 次」看不出差在哪，必须带预估。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
CANVAS = SRC / "pages" / "CanvasPage.tsx"
API = SRC / "api.ts"
TYPES = SRC / "types.ts"
STYLES = SRC / "styles.css"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: E402
from app.services import run_budget  # noqa: E402


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _section(src: str, start: str, end: str) -> str:
    return src[src.index(start) : src.index(end)]


def test_exceeding_the_budget_needs_a_checkbox_not_a_second_click():
    """超限时要锁住「开始运行」，勾选后才解锁——点两次同一个按钮不叫确认。"""
    dialog = _section(_text(CANVAS), "function RunConfirmDialog", "function RunReportDialog")
    assert "needAck" in dialog and "gate?.exceeds" in dialog
    assert "type=\"checkbox\"" in dialog, "没有勾选框"
    assert "disabled={busy || (needAck && !acked)}" in dialog, "超限时按钮没锁住"
    assert "我知道，仍然要跑" in dialog


def test_the_ack_value_is_sent_back_to_the_api():
    """只在前端拦住等于没拦：确认值必须真的回传，服务端会再核一遍。"""
    dialog = _section(_text(CANVAS), "function RunConfirmDialog", "function RunReportDialog")
    assert "onConfirm(needAck ? gate.ack : 0)" in dialog, "确认值没有回传"

    src = _text(CANVAS)
    assert "api.runCanvas(projectId, undefined, ackCalls)" in src, "运行接口没有带上确认值"
    api = _text(API)
    assert "ack_calls: ackCalls" in api, "请求体里没有 ack_calls"
    assert "runCanvas: (projectId: number, nodeId?: string, ackCalls = 0)" in api


def test_budget_gate_shows_the_numbers_and_where_to_change_it():
    """说超限就得同时给出「调用几次」「上限几次」「去哪儿改」。"""
    dialog = _section(_text(CANVAS), "function RunConfirmDialog", "function RunReportDialog")
    assert "gate.calls" in dialog and "gate.limit" in dialog
    assert "系统设置" in dialog, "没告诉用户去哪儿调上限"
    assert "整图运行调用上限" in dialog


def test_run_estimate_comes_from_the_server_response():
    """预估值用接口返回的那个：它就是服务端据以放行的同一个数。"""
    src = _text(CANVAS)
    assert "setRunEstimate(r.estimate?.calls ?? 0)" in src, "预估值没跟着这一跑记下来"
    # 刷新过页面就没有响应里那个数了，要回落到这一跑自己记着的预估
    assert "estimate={runEstimate || runReport.expected}" in src, "没有回落用的预估"
    api = _text(API)
    assert "estimate?: { calls: number }" in api


def test_polling_stops_in_every_case():
    """跑完了、查不到、超时了都要收手；卸载页面也要清掉。"""
    src = _text(CANVAS)
    body = _section(src, "const watchRun =", "const deleteSelected")
    assert "s.finished" in body, "没判断是否跑完"
    assert "catch {" in body and "stop = true" in body, "查不到时没有收手"
    assert "tries > 240" in body, "没有轮询上限"
    assert "window.clearInterval(runWatcher.current)" in body, "轮询没有清掉"
    assert "runWatcher = useRef<number | null>(null)" in src
    # 卸载时清理
    cleanup = src[src.index("离开画布时把"):]
    assert "clearInterval(runWatcher.current)" in cleanup[:400], "卸载时没停掉轮询"


def test_report_compares_estimate_with_actual_and_names_failures():
    """只显示「实际 12 次」看不出差在哪，必须把预估和失败数一起摆出来。"""
    report = _section(_text(CANVAS), "function RunReportDialog", "function formatDuration")
    assert "estimate" in report and "（预估 ${estimate} 次）" in report
    assert "summary.failed" in report and "失败" in report
    assert "run-report-bad" in report
    assert "formatDuration(summary.elapsedSec)" in report, "耗时没说人话"


def test_report_is_honest_about_what_is_counted():
    """账只算我们发出去的调用，不能让人以为这是供应商的用量账单。"""
    report = _section(_text(CANVAS), "function RunReportDialog", "function formatDuration")
    assert "不含供应商侧的用量上报" in report
    assert "失败的任务不会因为失败就不计费" in report


def test_types_match_the_backend_shape():
    """字段名是前后端契约：后端怎么给，前端怎么读。"""
    types = _text(TYPES)
    gate_keys = set(run_budget.gate(3, 1, limit=2))
    for key in gate_keys:
        assert key in types, f"前端类型里没有 gate.{key}"
    for key in ("CanvasRunGate", "CanvasRunSummary", "calls", "finished", "active",
                "expected", "products", "videoSeconds"):
        assert key in types, f"前端类型里没有 {key}"

    # 后端 run_summary 返回的键要都在前端类型里（少一个就是界面上少一句话）
    runner = _text(ROOT / "backend" / "app" / "services" / "canvas_runner.py")
    body = runner[runner.index("async def run_summary") :]
    # run_summary 是目前文件里的最后一个函数，后面没有空三段，取到文件尾即可
    cut = body.find("\n\n\n")
    body = body[:cut] if cut != -1 else body
    keys = set(re.findall(r'"(\w+)":', body))
    expected = {
        "runId", "calls", "byKind", "status", "completed", "failed", "running",
        "finished", "active", "expected", "products", "nodes", "elapsedSec",
    }
    assert expected <= keys, f"后端返回里少了：{expected - keys}"
    for key in sorted(expected):
        assert key in types, f"后端给了 {key}，前端类型里没有"


def test_run_id_column_exists_on_tasks():
    """对账靠的是「这一跑」的标识；没有这一列，账就分不出是哪一次。"""
    assert hasattr(models.Task, "run_id")
    assert "run_id" in _text(ROOT / "backend" / "alembic" / "versions" / "0014_task_run_id.py")


def test_styles_exist():
    css = _text(STYLES)
    for cls in (".run-confirm-gate", ".run-report-headline", ".run-report-headline.ok",
                ".run-report-headline.warn", ".run-report-bad"):
        assert cls in css, f"styles.css 里没有 {cls}"


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
