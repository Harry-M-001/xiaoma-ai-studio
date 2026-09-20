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


def test_every_hint_renders_without_crashing():
    """每一个提示键都要能打出来，而且都要告诉用户怎么把报告发回来。

    `--hint <key>` 是「启动脚本失败时唯一能看到的解释」，某一条打不出来就等于
    那一类失败对用户完全不可读。
    """
    import contextlib
    import io

    for key in env.HINTS:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = env.main(["--hint", key])
        assert code == 0, key
        out = buf.getvalue()
        assert out.strip(), f"{key} 打出来是空的"
        assert "--report" in out, f"{key} 没告诉用户怎么把报告发回来"


def test_ffmpeg_hints_tell_how_to_install():
    """ffmpeg 的两条提示必须写清「怎么装」——只说「找不到 ffmpeg」等于没说。"""
    for key in ("ffmpeg-missing", "ffmpeg-nolibass"):
        assert key in env.HINTS, key
        text = "\n".join(env.HINTS[key])
        assert "winget" in text and "brew" in text, f"{key} 没给各平台的装法"
        assert "http" in text, f"{key} 没给下载地址"


LIBASS_FILTERS = " .. ass               V->V   Render ASS subtitles onto input video\n .. subtitles         V->V   Render text subtitles\n"
TRIMMED_FILTERS = " TS scale             V->V   Scale the input video size\n .. fps               V->V   ...\n"


def _fake_ffmpeg(candidates: list[Path], tables: dict[str, str]):
    """把 `ffmpeg_candidates` 与 `_run` 换掉，模拟机器上有哪几个 ffmpeg。

    `tables` 的键是路径字符串，值是 `-filters` 的输出（用来判有没有 libass）。
    **键要先过 `str(Path(...))` 归一化**：Windows 上 `Path("C:/x/f.exe")` 的 `str()`
    是反斜杠，直接拿正斜杠字符串当键会一条都命中不了——那样测试会「因为全都查不到
    libass」而走进另一个分支，看起来像代码错了。
    """
    normalized = {str(Path(k)): v for k, v in tables.items()}
    real_candidates = env.ffmpeg_candidates
    real_run = env._run

    def fake_candidates() -> list[Path]:
        return list(candidates)

    def fake_run(cmd, timeout=30, *, full=False):  # noqa: ANN001, ARG001
        path = str(cmd[0])
        if "-version" in cmd:
            return 0, f"ffmpeg version 9.9 fake at {path}"
        if "-filters" in cmd:
            return 0, normalized.get(path, "")
        return 0, ""

    env.ffmpeg_candidates = fake_candidates  # type: ignore[assignment]
    env._run = fake_run  # type: ignore[assignment]
    try:
        return env.check_ffmpeg()
    finally:
        env.ffmpeg_candidates = real_candidates  # type: ignore[assignment]
        env._run = real_run  # type: ignore[assignment]


def test_check_ffmpeg_reports_missing_as_a_warning():
    """没装 ffmpeg 只该是警告：它不影响启动，也不影响对话/出图/出视频。"""
    out = _fake_ffmpeg([], {})
    assert [f.level for f in out] == [env.WARN], out
    # 必须写清「哪些功能会受影响」，否则用户以为整个应用坏了
    assert "导演台" in out[0].detail, out[0].detail


def test_check_ffmpeg_prefers_the_build_that_has_libass():
    """**候选要全找一遍再挑**——只看 `which` 会误报。

    真实现场（本机就是这样）：PATH 里那个是某编辑器自带的裁剪版（没有 libass），
    而应用实际用的是 winget 装的完整版。体检若只报 PATH 里那个，用户会看到一个
    根本不存在的警告。
    """
    trimmed = "C:/some/app/ffmpeg.exe"
    full = "C:/winget/ffmpeg.exe"
    out = _fake_ffmpeg(
        [Path(trimmed), Path(full)], {trimmed: TRIMMED_FILTERS, full: LIBASS_FILTERS}
    )
    titles = {f.title: f.level for f in out}
    assert any("libass）可用" in t and v == env.OK for t, v in titles.items()), titles
    # 挑中的是带 libass 的那个（比路径要比归一化后的，Windows 上是反斜杠）
    picked = next(f.detail for f in out if f.title.startswith("ffmpeg 可用"))
    assert str(Path(full)) in picked, picked
    # 另一个不静默略过：要让用户知道应用不用它
    assert any("没有 libass" in t for t in titles), titles


def test_check_ffmpeg_warns_when_only_a_trimmed_build_exists():
    """只有裁剪版时要说清「烧不了字幕」并指向换构建的提示。"""
    trimmed = "C:/some/app/ffmpeg.exe"
    out = _fake_ffmpeg([Path(trimmed)], {trimmed: TRIMMED_FILTERS})
    warn = next(f for f in out if "libass" in f.title)
    assert warn.level == env.WARN, warn.title
    assert "ffmpeg-nolibass" in warn.hint, warn.hint
    # 也要说清「不影响什么」，免得用户以为整条链都废了
    assert "不受影响" in warn.detail or "不影响" in warn.detail, warn.detail


def test_check_ffmpeg_reads_the_full_filter_list():
    """判 libass 必须读**完整**的 `-filters` 输出。

    `_run` 默认只回第一行；只看第一行必然得出「没有 libass」，而应用里那份
    `subtitle_filter_available()` 用的是全文——两处结论不一致就是误报。
    """
    real_run = env._run
    real_candidates = env.ffmpeg_candidates
    seen: list[bool] = []

    def fake_run(cmd, timeout=30, *, full=False):  # noqa: ANN001, ARG001
        if "-filters" in cmd:
            seen.append(full)
            # 只有整段里才看得到 ass：第一行是 scale
            return 0, TRIMMED_FILTERS + LIBASS_FILTERS
        return 0, "ffmpeg version 9.9"

    env._run = fake_run  # type: ignore[assignment]
    env.ffmpeg_candidates = lambda: [Path("C:/x/ffmpeg.exe")]  # type: ignore[assignment]
    try:
        out = env.check_ffmpeg()
    finally:
        env._run = real_run  # type: ignore[assignment]
        env.ffmpeg_candidates = real_candidates  # type: ignore[assignment]
    assert seen and all(seen), "查滤镜清单时没要完整输出"
    assert any("libass）可用" in f.title for f in out), [f.title for f in out]


def test_readme_documents_the_required_tools():
    """README 必须写清「要装什么、去哪下、怎么核对」。

    这是新人第一眼要看的东西；漏了 ffmpeg（尤其是它的 libass 要求）会直接表现成
    「别的都能用、就是烧字幕失败」，而那时用户已经剪完片子了。
    """
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for need in (
        "## 环境要求",
        "https://www.python.org/downloads/",
        "https://nodejs.org/",
        "winget install Gyan.FFmpeg",
        "brew install ffmpeg",
        "libass",
        "FFMPEG_PATH",
        "backend/tools/env_report.py",
        "backend/assets/fonts/SOURCES.md",
    ):
        assert need in text, f"README 里少了：{need}"
    # 三种安装方式各自要装什么，必须在同一张表里说清
    assert "Windows 便携包" in text and "只有 ffmpeg" in text, "没说清便携包只需要 ffmpeg"
    # 字体手动下载地址（想离线装或自己校验的人要用）
    for url in (
        "fonts-v1/NotoSerifCJKsc-Regular.otf",
        "fonts-v1/LXGWWenKai-Regular.ttf",
        "fonts-v1/SmileySans-Oblique.ttf",
    ):
        assert url in text, f"README 里少了字体地址：{url}"


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


def test_issue_mode_renders_template():
    """`--issue` 要直接吐出可粘贴提交的正文，且不能夹带别的东西。

    这条路径就是「让用户手动把日志发给我」的主干：用户复制、粘贴、提交三步完事。
    只要混进了别的内容（比如体检结论那两行），用户就得自己先删干净——
    一麻烦他就不发了，所以宁可在这里钉死。
    """
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = env.main(["--issue", "--root", str(ROOT), "--skip-node"])
    assert code == 0, "体检有错项时 --issue 也要正常输出（错误项本身就是要发的内容）"
    out = buf.getvalue()
    for section in ("### 我做了什么", "### 出了什么问题", "### 我期望的结果", "### 环境信息"):
        assert section in out, f"Issue 内容缺少小节：{section}"
    assert "```text" in out, "环境信息要放在代码块里，否则粘贴后排版散掉"
    # 命令行输出不能带「结论：N 个错误」这类体检收尾语
    assert "结论：" not in out, "--issue 混进了体检结论，用户还得手动删"


def test_issue_text_folds_error_lines_into_details():
    """带错误日志时用 <details> 折叠，避免正文被几十行日志冲垮。

    折叠块存在的意义：用户只用看前三个小节，日志是给作者看的。
    """
    with_details = env.build_issue_text([], extra_lines=["ERROR 上游返回 502"])
    assert "<details>" in with_details
    assert "ERROR 上游返回 502" in with_details
    assert "</details>" in with_details

    without = env.build_issue_text([])
    assert "<details>" not in without, "没有日志时不该留一个空的折叠块"


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
