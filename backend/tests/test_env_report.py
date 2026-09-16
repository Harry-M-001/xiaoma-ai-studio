"""环境体检脚本的回归测试（直接 python 运行）。

运行：venv/Scripts/python tests/test_env_report.py
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

# backend/tools 不是包（只是给启动脚本调的单文件工具），按路径加载即可。
_spec = importlib.util.spec_from_file_location("env_report", BACKEND / "tools" / "env_report.py")
assert _spec and _spec.loader
env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(env)

ROOT = BACKEND.parent


def test_version_thresholds():
    """3.11 是下限，3.14+ 要给「依赖可能没有预编译包」的提醒。"""
    assert env.version_finding((3, 10, 11), "x").level == env.ERR
    assert env.version_finding((3, 9, 0), "x").level == env.ERR
    assert env.version_finding((3, 11, 0), "x").level == env.OK
    assert env.version_finding((3, 12, 8), "x").level == env.OK
    assert env.version_finding((3, 13, 1), "x").level == env.OK
    assert env.version_finding((3, 14, 0), "x").level == env.WARN


def test_version_finding_reports_the_interpreter():
    """出错时必须把「到底是哪个 python」写出来，否则用户无从下手。"""
    f = env.version_finding((3, 10, 11), r"C:\somewhere\python.exe")
    assert "3.10.11" in f.title
    assert r"C:\somewhere\python.exe" in f.detail
    assert f.hint  # 要给出下一步动作


def test_module_for_spec():
    assert env.module_for_spec("fastapi") == "fastapi"
    assert env.module_for_spec("uvicorn[standard]") == "uvicorn"
    assert env.module_for_spec("sqlalchemy[asyncio]") == "sqlalchemy"
    assert env.module_for_spec("pydantic-settings") == "pydantic_settings"
    # 包名与导入名不一致的例外
    assert env.module_for_spec("python-multipart") == "multipart"


def test_custom_nodes_location_is_rejected():
    """装在 ComfyUI 的 custom_nodes 里必须判错——这是用户实际踩到的坑。"""
    assert env.looks_like_custom_nodes(Path(r"D:\ComfyUI\custom_nodes\xiaoma-ai-studio"))
    assert env.looks_like_custom_nodes(Path(r"D:\ComfyUI\Custom_Nodes\xiaoma-ai-studio"))
    assert env.looks_like_custom_nodes(Path(r"E:\整合包\ComfyUI\custom_nodes\a\b"))
    assert not env.looks_like_custom_nodes(Path(r"D:\xiaoma-ai-studio"))
    assert not env.looks_like_custom_nodes(Path(r"D:\projects\my_custom_nodes_backup"))


def test_location_finding_level():
    bad = env.check_location(Path(r"D:\ComfyUI\custom_nodes\xiaoma-ai-studio"))
    assert [f.level for f in bad] == [env.ERR]
    # 提示里要告诉用户「挪到哪」，而不只是说错了
    assert "挪" in bad[0].hint

    good = env.check_location(Path(r"D:\xiaoma-ai-studio"))
    assert [f.level for f in good] == [env.OK]


def test_venv_python_path():
    p = env.venv_python(Path(r"D:\repo"))
    assert p.parent.parent.name == "venv"
    assert p.name == ("python.exe" if sys.platform == "win32" else "python")


def test_runtime_deps_cover_requirements():
    """requirements.txt 里加/减了依赖，体检清单必须跟着变。

    回归背景：这类「两处各写一份」的清单最容易悄悄漂移，
    漂移之后体检会漏报缺失的依赖，用户拿到的是「一切正常」。
    """
    text = (BACKEND / "requirements.txt").read_text(encoding="utf-8")
    specs = [
        line.split("==")[0].split(">=")[0].strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    checked = {env.module_for_spec(s) for s in env.RUNTIME_DEPS}
    missing = [s for s in specs if env.module_for_spec(s) not in checked]
    assert not missing, f"体检清单漏了这些依赖：{missing}"


def test_runtime_deps_are_importable_here():
    """测试是在 venv 里跑的，所以这些包必须真的能导入。"""
    import importlib.util

    missing = [s for s in env.RUNTIME_DEPS if importlib.util.find_spec(env.module_for_spec(s)) is None]
    assert not missing, f"当前解释器缺少：{missing}"


def test_deps_only_mode_on_a_healthy_interpreter():
    """测试是在装好依赖的 venv 里跑的，所以 --deps-only 必须返回 0。

    这个模式的作用是：外层脚本拿系统 Python 跑体检时，用它换成
    虚拟环境的解释器再复查一遍依赖，避免「系统 Python 没装依赖」被误报成故障。
    """
    assert env.main(["--deps-only"]) == 0


def test_deps_only_mode_reports_missing():
    """缺依赖时要返回 1 并在第一行列出包名（调用方只读第一行）。"""
    import contextlib
    import io

    orig = env.RUNTIME_DEPS
    env.RUNTIME_DEPS = [*orig, "definitely-not-installed-package"]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = env.main(["--deps-only"])
    finally:
        env.RUNTIME_DEPS = orig
    assert code == 1
    assert "definitely-not-installed-package" in buf.getvalue().splitlines()[0]


def test_start_bat_is_pure_ascii():
    """start.bat 里不许出现任何非 ASCII 字符。

    回归背景（真踩过）：cmd.exe 按字节偏移解析批处理，`chcp 65001` 遇到文件里的
    多字节字符会让偏移错位，cmd 开始把行片段当命令执行——实测症状是
    `'cho.' is not recognized`，意思是**失败提示本身先崩了**，用户反而什么提示都看不到。
    所以中文一律交给 Python 打印。
    """
    raw = (ROOT / "start.bat").read_bytes()
    bad = [(i, b) for i, b in enumerate(raw) if b > 127]
    assert not bad, f"start.bat 第 {bad[0][0]} 字节起出现非 ASCII（{len(bad)} 处）"


def test_hint_keys_used_by_launcher_all_exist():
    """start.bat 里调用的每个 --hint 键都必须真的有对应文案。

    这类「脚本调一个字符串 key、Python 侧另存一份表」的写法最容易漂移，
    漂移之后症状是失败时打不出任何解释。
    """
    text = (ROOT / "start.bat").read_text(encoding="utf-8")
    used = set(re.findall(r"--hint\s+([A-Za-z0-9_-]+)", text))
    assert used, "start.bat 没有调用 --hint"
    unknown = sorted(used - set(env.HINTS))
    assert not unknown, f"start.bat 调用了不存在的提示键：{unknown}"


def test_hint_renders():
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = env.main(["--hint", "pip-install"])
    assert code == 0
    out = buf.getvalue()
    assert "错误" in out
    assert "--report" in out  # 每条提示都要顺手告诉用户怎么把报告发回来


def test_launchers_reference_the_checker():
    """两个启动脚本都必须先跑体检。

    回归背景：用户报「start.bat 第二步一直失败」，但脚本当时对每一步都不检查
    返回码，失败了也继续往下跑，控制台里只有一堆无从下手的红字。
    """
    for name in ("start.bat", "start.sh"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "env_report.py" in text, f"{name} 没有调用环境体检"
        # 体检没过就要中止，不能继续往下装
        assert re.search(r"errorlevel 1|if ! python3", text), f"{name} 没有检查体检结果"


def test_launchers_never_depend_on_activate():
    """不许再靠 activate.bat / activate 来切换解释器。

    那一步在「虚拟环境其实没建成」时会静默失败，然后 pip 装到了系统 Python 上，
    看起来是「第二步一直失败」，实则是第一步就没成。
    """
    for name in ("start.bat", "start.sh"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "activate.bat" not in text, f"{name} 仍在使用 activate.bat"
        assert "venv/bin/activate" not in text, f"{name} 仍在使用 activate"


def test_launchers_use_the_venv_python_directly():
    for name, needle in (
        ("start.bat", r"backend\venv\Scripts\python.exe"),
        ("start.sh", "backend/venv/bin/python"),
    ):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert needle in text, f"{name} 没有用虚拟环境里的 python 绝对路径"


def test_launchers_check_each_step():
    """每一步都要有失败分支，且给出可执行的下一步。"""
    bat = (ROOT / "start.bat").read_text(encoding="utf-8")
    sh = (ROOT / "start.sh").read_text(encoding="utf-8")
    # 4 个编号步骤 + 环境体检，失败分支至少要有这么多
    assert bat.count("if errorlevel 1 (") >= 6, bat.count("if errorlevel 1 (")
    assert sh.count("die ") >= 6, sh.count("die ")


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
