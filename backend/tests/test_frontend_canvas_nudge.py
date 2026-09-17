"""画布「浮框自动让位」的前端静态检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_frontend_canvas_nudge.py

为什么要这一组：用户报的「拖动节点偶尔画布乱窜」根子在这里。

画布有个贴心的设计：节点浮框超出可视区时自动把画布平移一点，让整块浮框都看得见
（`CanvasPage` 里的 `nudgeViewport`）。问题是它**没有闸门**，而触发它重新测量的
时机里混进了「用户正在拖节点」：

- 拖一个还没选中的节点 → React Flow 在拖起手时就把它选中 → 浮框随之挂载，
  并在挂载后 420 / 950 / 1500ms 各测一次。这三次测量正好落在拖动过程中；
- 任务状态轮询（有节点在跑时每 2.5s 一次）也会让浮框重新测量。

于是指针还按在节点上，画布先自己平移走了。浏览器实测（项目 002，同一节点同一浮框）：

| 场景 | 浮框溢出 | 画布视口 |
|---|---|---|
| 普通点一下选中 | 51px | 平移 50.76px 去对齐（功能正常） |
| 按住拖，t=1.0s（覆盖 420/950ms 两次测量） | 51px | 完全不动 |
| 按住拖，t=2.0s（覆盖 1500ms 第三次） | 66px | 完全不动 |
| 松手之后 | 66px | 完全不动 |

这里守四件事，都是「写法一漂移、bug 就回来」的地方：
1. `nudgeViewport` 里必须有拖动闸门，且闸门用 `useRef`（用 state 会读到过期闭包）；
2. 两个拖动回调必须成对设置/清除闸门（少一个，要么拖动期间照旧乱窜，
   要么这个功能从此整个失效——用户以为画布坏了）；
3. 松手时**不许**再补一次对齐。试过，松手那一刻会把刚放下的节点连同画布一起拽走
   （实测偏了 3105px，节点直接飞出屏幕），所以这里刻意没有这样的触发点；
4. 浮框只在「尺寸变了」时才重新对齐：位置变了而尺寸没变，说明动的是节点或画布本身
   （都是用户自己拖的），这时候再把画布拽回去又是一次乱走。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
CANVAS = SRC / "pages" / "CanvasPage.tsx"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _block(text: str, start: str, end: str) -> str:
    """取 text 里从 start 到其后第一个 end 的那一段（含首尾）。"""
    i = text.find(start)
    assert i >= 0, f"源码里找不到 `{start}`（位置改了就要同步改这条检查）"
    j = text.find(end, i + len(start))
    assert j >= 0, f"从 `{start}` 往后找不到 `{end}`"
    return text[i : j + len(end)]


def test_nudge_has_a_drag_gate():
    """闸门必须在 `nudgeViewport` 自己身上：调用方散在浮框里，挡不住。"""
    fn = _block(_text(CANVAS), "const nudgeViewport = useCallback(", "[getViewport, setViewport]")
    assert "if (draggingRef.current) return;" in fn, (
        "nudgeViewport 没有拖动闸门——拖节点时画布又会被自动平移"
    )


def test_drag_gate_is_a_ref():
    """必须是 ref：state 在 useCallback 的闭包里会一直是初始值，闸门形同虚设。"""
    src = _text(CANVAS)
    assert "const draggingRef = useRef(false)" in src, (
        "draggingRef 不是 useRef(false)——改成 state 后闭包读到过期值，闸门会失效"
    )


def test_drag_callbacks_set_and_clear_the_gate():
    """少写一半都不行：只置位会永久卡住（自动让位功能全废），只清位等于没闸门。"""
    src = _text(CANVAS)
    started = _block(src, "onNodeDragStart={() => {", "}}")
    assert "draggingRef.current = true;" in started, "拖动开始时没有落下闸门"
    stopped = _block(src, "onNodeDragStop={() => {", "}}")
    assert "draggingRef.current = false;" in stopped, "拖动结束后没有抬起闸门"


def test_drag_stop_does_not_realign():
    """松手那一刻不许再补一次对齐（实测会把刚放下的节点和画布一起拽走 3105px）。"""
    src = _text(CANVAS)
    stopped = _block(src, "onNodeDragStop={() => {", "}}")
    assert "setViewport(" not in stopped, "拖动结束回调里又去动视口了"
    for counter in ("alignTick", "dragTick", "nudgeTick"):
        assert counter not in src, (
            f"又出现了 `{counter}` 这种「拖完再对齐一次」的触发器："
            "松手时画布会跟着动，正是用户说的乱窜"
        )


def test_panel_only_realigns_when_its_size_changed():
    """浮框的位置会跟着节点/画布走，只有尺寸变化才是「内容真的不一样了」。"""
    eff = _block(_text(CANVAS), "const lastAlignHeight = useRef(0);", "}, [id, statusKey]);")
    assert "const resized =" in eff, "浮框没有判断「尺寸变没变」"
    assert "if (!resized) return;" in eff, "尺寸没变也继续往下对齐了"
    gate = eff.index("if (!resized) return;")
    nudge = eff.index("nudge(dx, dy)")
    assert gate < nudge, "闸门写在了 nudge 调用之后，等于没拦"


def test_panel_remeasures_only_on_node_switch_or_status():
    """打字不该反复平移画布，所以重测时机就这三处固定下来。"""
    src = _text(CANVAS)
    assert "}, [id, statusKey]);" in src, "浮框的重测依赖改了，需要重新确认「什么情况下会动画布」"


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
