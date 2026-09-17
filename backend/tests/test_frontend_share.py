"""社区分享（#20）的前端静态检查。

运行：venv/Scripts/python tests/test_frontend_share.py

分享最容易出的两类事都不是「代码跑不起来」，而是**语义错了**：

1. **导出的不是我看到的**。传 project_id 就会分享「上次保存的版本」，
   而自动保存是 1 秒后的事，用户很可能刚刚改完就点了分享。
2. **导入的东西没经过落定那套处理**。与已有内容错开、id 重盖章、按类型补模型
   三件事少一件，用户看到的就是「画布被搞乱了」或者「一跑就报模型不存在」。

外加一条一致性：授权档位由服务端给，前端不该自己维护一份（两份必然漂）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
CANVAS = SRC / "pages" / "CanvasPage.tsx"
API = SRC / "api.ts"
BACKEND_ROUTER = ROOT / "backend" / "app" / "routers" / "share.py"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _share_dialog() -> str:
    src = _text(CANVAS)
    return src[src.index("function ShareDialog(") : src.index("/* ---------------- AI 搭画布")]


def _apply_imported() -> str:
    src = _text(CANVAS)
    body = src[src.index("const applyImportedDoc"):]
    return body[: body.index("const onPaneDoubleClick")]


def test_frontend_and_backend_agree_on_the_three_endpoints():
    api = _text(API)
    router = _text(BACKEND_ROUTER)
    # router 自己带 prefix="/api/share"，文件里只写后半截
    for name, path in (
        ("shareLicenses", "/licenses"),
        ("shareExport", "/export"),
        ("shareImport", "/import"),
    ):
        assert f"{name}:" in api, f"api.ts 里没有 {name}"
        assert f'"/api/share{path}"' in api, f"api.ts 里的 {name} 没打到 /api/share{path}"
        assert f'"{path}"' in router, f"后端没有 /api/share{path}"
        assert f"api.{name}(" in _text(CANVAS), f"没有页面在用 {name}"


def test_export_sends_the_canvas_on_screen_not_a_project_id():
    """传当前画布状态，而不是 project_id：用户想分享的是他屏幕上看到的东西。"""
    src = _text(CANVAS)
    call = src[src.index("api.shareExport(") - 200 : src.index("api.shareExport(") + 120]
    assert "doc," in call, "shareExport 没把当前画布传过去"
    assert "projectId" not in call, "shareExport 传了 projectId，那会分享上次保存的版本"

    # 传给对话框的 doc 必须由当前 nodes/edges 现算
    dialog_use = src[src.index("<ShareDialog"):]
    dialog_use = dialog_use[: dialog_use.index("/>")]
    assert "toCanvasDocNodes(nodes" in dialog_use
    assert "edges.map(" in dialog_use


def test_import_goes_through_the_shared_landing_helper():
    """导入必须复用落定那套（错开 / 重盖章 / 补模型），不能自己往画布上塞。"""
    body = _apply_imported()
    assert "mergeCanvasContent(" in body, "导入没有走共用的落定逻辑"
    for forbidden in ("setNodes(", "setEdges("):
        assert forbidden not in body, f"导入自己动了 {forbidden}"


def test_reading_a_share_does_not_touch_the_canvas():
    """「读一读内容」只读取：落不落由用户点确认决定。

    允许出现 onApply，但**只能有一次**，而且必须在「导入到画布」那个按钮上；
    多出来的那一处就说明有人顺手在读取之后直接落了。
    """
    dialog = _share_dialog()
    for forbidden in ("setNodes(", "setEdges(", "setDirty("):
        assert forbidden not in dialog, f"分享对话框里出现了 {forbidden}"

    assert dialog.count("onApply(") == 1, "onApply 出现了不止一次，读取之后就落库了？"
    # onApply 与「导入到画布」那个按钮的文案必须紧挨着（同一个按钮元素里）
    gap = dialog.index("导入到画布") - dialog.index("onApply(")
    assert 0 < gap < 220, f"onApply 不在「导入到画布」按钮里（相隔 {gap} 字符）"


def test_import_button_is_gated_on_ok():
    dialog = _share_dialog()
    idx = dialog.index("导入到画布")
    assert "incoming?.ok &&" in dialog[:idx], "「导入到画布」没有被 ok 判定拦住"


def test_licenses_come_from_the_server():
    """授权档位由后端给：前端自己再写一份，两边一定会漂。"""
    assert "shareLicenses" in _text(CANVAS)
    src = _text(CANVAS)
    for leaked in ("PolyForm", "CC-BY-NC", "AllRightsReserved"):
        assert leaked not in src, f"前端写死了授权档位 {leaked}，应当用后端给的 options"


def test_long_share_code_suggests_a_file_instead():
    """分享码太长就别让用户复制了：聊天窗口里必然被截断。"""
    dialog = _share_dialog()
    assert "codeTooLong" in dialog
    assert "截断" in dialog, "长码没有给出「改用文件」的说明"


def test_import_warns_are_shown_not_swallowed():
    """缺模型是提醒不是错误：得让用户看见，否则他跑的时候才发现。"""
    dialog = _share_dialog()
    assert "incoming.warnings" in dialog
    assert "incoming.errors" in dialog


def test_share_dialog_shows_license_and_author_before_importing():
    """别人分享的东西是什么、允许你怎么用，要先摆出来再让人点导入。"""
    dialog = _share_dialog()
    for needed in ("incoming.license", "incoming.author", "licenseText"):
        assert needed in dialog, f"导入前的信息里缺 {needed}"


def test_every_apply_path_stamps_ids_and_fills_models():
    """两个来源（AI 草稿、分享导入）都得走同一份落定逻辑，否则早晚只改一处。"""
    src = _text(CANVAS)
    body = src[src.index("const mergeCanvasContent"):]
    body = body[: body.index("const applyAgentDraft")]
    for needed in ("Date.now().toString(36)", "modelKeyForNodeType(", "setDirty(true)"):
        assert needed in body, f"落定逻辑里缺 {needed}"

    for caller in ("applyAgentDraft", "applyImportedDoc"):
        fn = src[src.index(f"const {caller}"):]
        fn = fn[: fn.index(");", fn.index("useCallback("))]
        assert "mergeCanvasContent(" in fn, f"{caller} 没有走共用的落定逻辑"


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
