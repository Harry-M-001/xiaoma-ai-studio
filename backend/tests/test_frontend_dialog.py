"""弹窗行为统一的前端静态检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_frontend_dialog.py

为什么要这一组：**这个项目的弹窗皮肤早就统一了，行为没有**。
外观由 `.modal` / `.canvas-dialog` 两套类名管着，看着一致；但行为原来是三套——
`Modal` 能按 Esc 关、画布里 5 个自建弹窗与两个 Lightbox 不能，`role`/`aria-modal`
也只有 `Modal` 上有。键盘用户的体感就是「有的弹窗按 Esc 能关、有的关不掉」，
读屏软件也不知道跳出来的这层是个对话框。

现在所有遮罩都出自 `components/Dialog.tsx`。这里守三件事：
1. `Dialog` 必须真的做了那几件事（Esc / 点遮罩 / role / 焦点进与还 / Tab 循环），
   光抽个壳子把行为丢了，比不抽还糟；
2. 全项目不许再出现裸的遮罩类名（必须走 `Dialog`，或 `maskClassName` 传进去）；
3. `Modal` 不许自己再实现一套 Esc（两套实现迟早分叉）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
DIALOG = SRC / "components" / "Dialog.tsx"
COMMON = SRC / "components" / "common.tsx"

# 遮罩类名：出现在 className 上就是「又自己写了一个弹窗」
MASK_CLASSES = ("modal-mask", "canvas-dialog-mask", "lightbox")


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_dialog_handles_the_basics():
    """抽出来的壳子必须真管行为，否则等于把不一致藏进了新文件里。"""
    src = _text(DIALOG)
    assert 'e.key === "Escape"' in src, "Dialog 没有处理 Esc"
    assert "aria-modal" in src and 'role="dialog"' in src, "Dialog 没有声明自己是对话框"
    assert "aria-label" in src, "Dialog 没有无障碍名称（读屏只会念「对话框」）"
    assert "e.target === e.currentTarget" in src, "点遮罩关闭没有限定「点在遮罩本身上」"


def test_dialog_moves_focus_in_and_gives_it_back():
    """焦点进来、关掉还回去；键盘用户不该在关闭后掉回页面顶部。"""
    src = _text(DIALOG)
    assert "document.activeElement" in src, "Dialog 没有记录/判断焦点"
    assert re.search(r"return\s*\(\)\s*=>\s*\w+\?\.focus", src), (
        "Dialog 关闭时没有把焦点还给原来那个元素"
    )
    assert "box.contains(document.activeElement)" in src, (
        "Dialog 没有避开弹窗内部的 autoFocus（会抢输入框的焦点）"
    )


def test_dialog_keeps_tab_inside():
    """Tab 必须在弹窗内循环：不然键盘用户会 tab 到背后看不见的页面上。"""
    src = _text(DIALOG)
    assert 'e.key !== "Tab"' in src, "Dialog 没有拦截 Tab"
    assert "shiftKey" in src, "Dialog 没有处理 Shift+Tab（反向也一样要循环）"


def test_no_bare_mask_anywhere():
    """全项目不许再出现裸遮罩：要么走 Dialog，要么用 maskClassName 传进去。"""
    offenders: list[str] = []
    for path in SRC.rglob("*.tsx"):
        text = _text(path)
        for cls in MASK_CLASSES:
            # className="modal-mask" 这种才是自建遮罩；
            # maskClassName="modal-mask" 是喂给 Dialog 的，允许
            if f'className="{cls}"' in text:
                offenders.append(f"{path.relative_to(ROOT)}（{cls}）")
    assert not offenders, f"这些地方又自己写了遮罩：{offenders}——遮罩必须出自 Dialog"


def test_modal_reuses_dialog():
    """`Modal` 只能是 Dialog 的皮肤封装，不许自己再接一遍键盘事件。"""
    src = _text(COMMON)
    assert "from \"./Dialog\"" in src, "Modal 没有用 Dialog"
    assert "addEventListener(\"keydown\"" not in src, (
        "Modal 自己又接了一遍 keydown——两套实现迟早分叉"
    )


def _dialog_attrs(text: str) -> list[str]:
    """取出每个 `<Dialog ...>` 的属性块。

    不能简单地正则取到第一个 `>`：属性里到处是 `=>`（`onClose={() => ...}`），
    那会提前截断，让「有没有 label」判错。所以按行取，直到出现只含 `>`/`/>` 的收尾行。
    """
    out: list[str] = []
    for m in re.finditer(r"<Dialog\b", text):
        lines = text[text.rfind("\n", 0, m.start()) + 1 :].splitlines()
        buf = ""
        for idx, line in enumerate(lines):
            buf += line + "\n"
            s = line.strip()
            if s in (">", "/>"):
                break
            # 单行形式：<Dialog onClose={...} label={...}>
            if idx == 0 and s.endswith(">") and not s.endswith(("=>", "->")):
                break
        out.append(buf)
    return out


def test_every_dialog_passes_a_readable_name():
    """每个 <Dialog> 都要给 label：读屏念出来的就是它。"""
    offenders: list[str] = []
    for path in SRC.rglob("*.tsx"):
        for tag in _dialog_attrs(_text(path)):
            if "label=" not in tag:
                offenders.append(f"{path.relative_to(ROOT)}: {tag.splitlines()[0]}")
    assert not offenders, f"这些 <Dialog> 没有 label：{offenders}"


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
