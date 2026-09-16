"""前后端状态值契约：任务状态必须两边一致。

这条测试是补出来的——曾经后端写 `completed`、前端只认 `succeeded`，
StatusBadge 查不到就兜底成了「排队中」，于是"生成完了还显示排队中"。
这里把两边钉在一起：后端新写一个状态值，前端没跟上就直接红。

运行：venv/Scripts/python tests/test_status_contract.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
FRONTEND = BACKEND.parent / "frontend" / "src"

# 后端 Task.status 的全部合法值（与 app/services/runner.py 的写入口径一致）
CANONICAL = {"pending", "processing", "completed", "failed", "cancelled"}

# 前端展示层额外兼容的历史别名（不得出现在后端写入路径里）
LEGACY_ALIASES = {"succeeded"}

# 检查这些文件里 `status = "xxx"` 的写入
_SCAN_FILES = [
    BACKEND / "app" / "services" / "runner.py",
    BACKEND / "app" / "services" / "canvas_runner.py",
    BACKEND / "app" / "routers" / "canvas.py",
]

_WRITE_RE = re.compile(r"""\bstatus\s*=\s*["']([a-z_]+)["']""")


def _backend_written_statuses() -> set[str]:
    found: set[str] = set()
    for path in _SCAN_FILES:
        text = path.read_text(encoding="utf-8")
        found.update(m.group(1) for m in _WRITE_RE.finditer(text))
    return found


def _frontend_status_keys() -> set[str]:
    """从 common.tsx 的 STATUS_MAP 里抠出所有键。"""
    text = (FRONTEND / "components" / "common.tsx").read_text(encoding="utf-8")
    start = text.index("const STATUS_MAP")
    block = text[start : text.index("\n};", start)]
    return set(re.findall(r"^\s*([a-z_]+):\s*(?:\{|DONE_META)", block, re.M))


def test_backend_never_writes_alias_status():
    """后端写入路径只准用规范值，别名只属于展示层。"""
    written = _backend_written_statuses()
    assert written, "没扫到任何状态写入，检查 _SCAN_FILES 是否过期"
    unknown = written - CANONICAL
    assert not unknown, f"后端写了非规范状态：{sorted(unknown)}"
    assert not (written & LEGACY_ALIASES), f"后端不该写历史别名：{sorted(written & LEGACY_ALIASES)}"


def test_frontend_covers_every_backend_status():
    """后端会写的每个状态，前端都必须有对应展示（否则会兜底成别的字）。"""
    keys = _frontend_status_keys()
    missing = CANONICAL - keys
    assert not missing, f"前端 STATUS_MAP 缺状态：{sorted(missing)}（现有 {sorted(keys)}）"


def test_frontend_keeps_legacy_alias():
    """历史别名仍要能正确显示成「已完成」，不能掉回兜底。"""
    keys = _frontend_status_keys()
    assert LEGACY_ALIASES <= keys, f"前端 STATUS_MAP 缺历史别名：{sorted(LEGACY_ALIASES - keys)}"


def test_no_pending_fallback_masks_unknown_status():
    """未知状态不许兜底成「排队中」——那正是这个 bug 的成因。"""
    text = (FRONTEND / "components" / "common.tsx").read_text(encoding="utf-8")
    assert "STATUS_MAP[status] ?? STATUS_MAP.pending" not in text, (
        "StatusBadge 又把未知状态兜底成 pending 了：未知状态应显示原始值"
    )


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failed else 0)
