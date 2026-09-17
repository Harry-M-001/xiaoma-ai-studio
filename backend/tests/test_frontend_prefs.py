"""前端界面偏好的静态检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_frontend_prefs.py

为什么用静态检查而不是跑起来：这里要钉的是一条**结构约定**——
「前端所有 localStorage 读写都必须经过 prefs.ts」。这条约定靠人记是记不住的，
而破坏它的后果很隐蔽：某个页面自己读了一个脏值、原样用出去，界面上只是「有点怪」。
所以用一条能自动跑的规则守它（和 test_frontend_tokens.py 守 CSS 变量一个思路）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
PREFS = SRC / "prefs.ts"

# 允许直接碰 localStorage 的文件（白名单要写清理由，不然它就变成一句空话）
ALLOWED = {
    "prefs.ts": "偏好读写的唯一入口：合法化都在这里做",
    "api.ts": "访问口令（token）是登录状态而不是界面偏好，且它必须能在 prefs 之外被清掉",
}


def _ts_files() -> list[Path]:
    return [p for p in SRC.rglob("*.ts")] + [p for p in SRC.rglob("*.tsx")]


def test_no_raw_localstorage_outside_the_whitelist():
    offenders: list[str] = []
    for path in _ts_files():
        if path.name in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"localStorage\s*\.\s*(getItem|setItem|removeItem|clear)", text):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, (
        "这些文件绕过了 prefs 直接读写 localStorage：" + "、".join(offenders) +
        "（界面偏好一律走 frontend/src/prefs.ts，好让脏数据只在一处降级）"
    )


def test_prefs_drops_invalid_values_instead_of_using_them():
    """非法值要就地丢掉，而不是留着——留着就会每次进页面重复失败一次。"""
    text = PREFS.read_text(encoding="utf-8")
    assert "dropRaw(" in text, "prefs.ts 里没有丢弃非法值的逻辑"
    read_block = text[text.index("function read<T>") : text.index("const FLAG")]
    assert "dropRaw(key)" in read_block, "校验失败的分支应当把那个键删掉"


def test_every_pref_is_named_in_one_place():
    """键名集中在一张表里：散落各处的话，改键名必然会漏。"""
    text = PREFS.read_text(encoding="utf-8")
    keys_block = text[text.index("const K = {") : text.index("} as const;")]
    for key in ("xm_theme", "xm_sidebar_collapsed", "xm_canvas_palette", "xm_canvas_chain_level", "xm_canvas_thumb"):
        assert key in keys_block, f"{key} 没有登记在 prefs 的键名表里"
    # 模型键按能力拼，避免写死三份
    assert "xm_model_" in keys_block, "模型键应当由能力名拼出来"


def test_preferences_that_reference_local_state_are_validated_against_it():
    """模型键与自动链档位是**指向本机状态**的引用，必须校验目标还在不在。

    模型：服务被删/改名后旧 key 悬空 → 选择框空白、一生成就报「模型服务不存在」。
    自动链档位：老版本写过的 key 在新版本里可能已经没了 → 下拉框显示空白。
    """
    text = PREFS.read_text(encoding="utf-8")
    assert "available.includes(" in text, "模型键没有对着「当前可用的模型」校验"
    assert "known.includes(" in text, "自动链档位没有对着「当前存在的档位」校验"


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
