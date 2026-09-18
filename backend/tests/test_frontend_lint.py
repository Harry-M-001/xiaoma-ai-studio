"""#28 分镜体检（前端接线）的静态检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_frontend_lint.py

后端把镜头语言查得再准，前端接错一处就白做。这里守三类最容易错的地方：

1. **先存再查**：体检读的是库里的画布，不是屏幕上的。用户刚改完分镜表就点「体检」，
   如果没先保存，报告描述的是旧正文——「结果和我眼前看到的不一样」是最容易失去信任的失败。
2. **没查出问题也要说一句**：`PreflightNotice` 在 warnings 为空时直接 `return null`，
   所以「没查出问题」这句话**不能**塞进它的 `headline`，否则永远显示不出来（写的时候真踩过）。
3. **跳过要如实说**：还没跑过的分镜节点会被后端记进 `skipped`，前端不显示的话，
   用户会以为体检把它漏了。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
CANVAS = SRC / "pages" / "CanvasPage.tsx"
NOTICE = SRC / "components" / "PreflightNotice.tsx"
API = SRC / "api.ts"
TYPES = SRC / "types.ts"
STYLES = SRC / "styles.css"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_lint_saves_before_asking():
    """体检前必须先保存：否则查的是库里的旧正文，和眼前的内容对不上。"""
    src = _text(CANVAS)
    start = src.index("const runLint =")
    body = src[start : src.index("\n  };", start)]
    assert "await saveDoc()" in body, "体检没先保存画布，查到的会是旧正文"
    assert "if (dirty && !(await saveDoc())) return;" in body, (
        "保存失败还继续查（会拿一份和屏幕上不一致的结果给人看）"
    )
    assert "api.lintCanvas(projectId)" in body, "体检没有真的调接口"


def test_toolbar_button_states_its_cost():
    """体检按钮要把「不花钱」写在 title 里——不然没人敢按。"""
    src = _text(CANVAS)
    assert "onClick={() => void runLint()}" in src, "工具栏没有体检按钮"
    start = src.index("onClick={() => void runLint()}")
    block = src[start - 200 : start + 400]
    assert "不调模型" in block and "不花钱" in block, "按钮没说明这是零成本操作"
    assert "disabled={linting || !loaded}" in block, "体检按钮没有防重入/未加载保护"
    assert "体检" in block, "按钮文案丢了"


def test_clean_result_is_not_silent():
    """没查出问题也要说一句，而且这句话不能靠 `headline` 传。

    这是真踩过的坑：`PreflightNotice` 在 warnings 为空时 `return null`，
    于是塞给它的「没查出问题」永远不会渲染——界面上什么都不显示，
    用户分不清「没问题」和「按钮没生效」。
    """
    notice = _text(NOTICE)
    assert "if (warnings.length === 0) return null;" in notice, (
        "PreflightNotice 的空告警行为变了——这条守卫的前提要重新看一遍"
    )

    src = _text(CANVAS)
    # 用渲染出来的那句文案来定位（文件里还有解释这条坑的注释，别被注释骗过去）
    copy = "没查出问题：镜头语言和字段都是齐的"
    assert copy in src, "干净的分镜表没有任何正面反馈"
    idx = src.index(copy)
    near = src[idx - 400 : idx + 200]
    assert "lint-dialog-ok" in near, "「没查出问题」没有自己的展示块"
    assert "report.warnings.length === 0 && report.nodes.length > 0" in near, (
        "「没查出问题」没有限定条件：节点全被跳过时也会说没问题"
    )
    # 不许再走回 headline 那条死路
    head_start = src.index("const head =")
    head_block = src[head_start : src.index(";", head_start)]
    assert "没查出问题" not in head_block, "「没查出问题」又被塞进 PreflightNotice 的 headline 了"

    assert ".lint-dialog-ok" in _text(STYLES), "「没查出问题」的样式没写"


def test_warning_list_reuses_the_shared_notice():
    """告警列表复用 PreflightNotice：弹窗皮肤统一了，告警样式也不该另起一套。"""
    src = _text(CANVAS)
    assert "PreflightNotice" in src, "没有用共享的告警组件"
    assert "<PreflightNotice warnings={report.warnings} headline={head} />" in src, (
        "体检弹窗没有复用 PreflightNotice"
    )


def test_skipped_nodes_are_stated():
    """被跳过的节点要如实列出来（还没跑过 ≠ 没问题）。"""
    src = _text(CANVAS)
    assert "report.skipped.length > 0" in src, "跳过的节点没有展示条件"
    assert "report.skipped.map" in src, "跳过的节点没有渲染"
    assert "{s.reason}" in src, "跳过的原因没显示出来——只说「跳过」等于没说"


def test_empty_canvas_gets_an_explanation():
    """画布上什么都没有时要解释清楚，而不是给一个空弹窗。"""
    src = _text(CANVAS)
    assert "report.nodes.length === 0 && report.skipped.length === 0" in src, (
        "没有「画布上还没内容」的分支"
    )
    assert "lint-dialog-empty" in src, "空状态没有样式挂点"
    assert ".lint-dialog-empty" in _text(STYLES), "空状态样式没写"


def test_api_and_types_agree_with_backend():
    """接口路径与字段名要和后端对上（后端返回的是 camelCase）。"""
    api = _text(API)
    assert "`/api/canvas/${projectId}/lint`" in api, "接口路径不是 /lint"
    assert "lintCanvas: (projectId: number) =>" in api, "api 里没有 lintCanvas"

    types = _text(TYPES)
    for field in ("shotTotal", "skipped", "warnings", "nodes", "blocking"):
        assert field in types, f"前端类型里没有 {field}（后端返回它，前端要读）"
    assert "CanvasLint" in types and "CanvasLintNode" in types and "CanvasLintFinding" in types


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
