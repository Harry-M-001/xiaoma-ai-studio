"""Windows「解压即用」便携包打包脚本（纯标准库）。

产出：
    dist/portable/                                    解包目录（可直接双击里面的 start.bat 试跑）
    dist/xiaoma-ai-studio-v<版本>-win64-portable.zip   最终分发包

用法：
    python backend/tools/make_portable.py
    python backend/tools/make_portable.py --skip-deps        # 复用已装好的 runtime，二次打包快很多
    python backend/tools/make_portable.py --out dist/portable --keep-zip
    python backend/tools/make_portable.py --python-url <embed zip 地址>

为什么要「嵌入式 Python」而不是打成 exe：
- 官方 embed 包只有十来兆，解压即用，用户机器上不用装任何东西；
- 它默认既不导入 site、也不带 site-packages，所以**必须改写 python311._pth**，
  否则 `import fastapi` 直接 ModuleNotFoundError —— 这是本脚本最关键的一步；
- 依赖用「构建侧解释器的 pip --target」灌进 runtime\\Lib\\site-packages，
  于是包里不含 pip，也不需要用户联网。

打包机要求：
- 构建侧解释器（跑本脚本的这个）必须与下载的 embed 包**同为 3.11 win_amd64**，
  否则 pip 会去取源码包，在没有编译器的机器上必然失败。

关于包根 start.bat 的纯 ASCII 约束（项目里真实踩过的坑）：
- cmd.exe 是按**字节偏移**顺序解析批处理文件的。文件里一旦出现多字节字符（中文），
  再叠加 `chcp 65001`，偏移就会错位，cmd 开始把半行当命令执行——真实症状是
  `'cho.' is not recognized`，于是「失败提示」本身先崩了，用户什么也看不到。
- 所以包里的 start.bat 只允许 ASCII；所有面向用户的中文说明都放在
  README-PORTABLE.txt 里（UTF-8），依赖缺失时由 start.bat 打印英文提示并指过去。
- 本脚本在写盘后**断言**字节全 ASCII（非 ASCII 直接报错终止），避免哪天手改错了还发出去。

不做什么：
- 不重新构建前端（frontend/webroot/index.html 不存在就直接报错退出）；
- 不创建虚拟环境、不联网装依赖、不碰 8787 端口上的任何东西。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse

# --------------------------------------------------------------------------- 常量

ROOT = Path(__file__).resolve().parents[2]  # backend/tools/make_portable.py -> 仓库根
DEFAULT_OUT = ROOT / "dist" / "portable"
DEFAULT_PYTHON_URL = (
    "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"
)

VERSION_FILE = Path("backend") / "app" / "__init__.py"
VERSION_RE = re.compile(r"""__version__\s*=\s*['"]([^'"]+)['"]""")

# 进包的目录树（相对仓库根）以及各自额外要跳过的文件名（只作用于树根）
COPY_TREES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("backend/app", ()),
    ("backend/alembic", ()),
    ("backend/tools", ("make_portable.py",)),  # 打包脚本自己不进包
    ("frontend/webroot", ()),
    # 随包字体（v1.1.25 起）：烧字幕要用，约 82 MB，是包里最大的一块非运行时内容。
    # 不进包的话「选字体」这个功能在便携版里直接不可用，而源码版却是好的——那种
    # 「同一个功能两种安装方式表现不一样」的坑最难查，所以宁可让包大一点。
    ("backend/assets", ()),
)

# 进包的单个文件
COPY_FILES: tuple[str, ...] = (
    "backend/alembic.ini",
    "backend/requirements.txt",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
)

DATA_DIR = "backend/data"  # 建一个空目录，用户数据都落这里

# 必须能被「运行时解释器」真的 import 的包（逐个真起 python.exe 去试）
REQUIRED_IMPORTS: tuple[str, ...] = (
    "fastapi",
    "uvicorn",
    "sqlalchemy",
    "alembic",
    "httpx",
    "pydantic",
    "cryptography",
    "aiosqlite",
)

# 软检查：uvicorn[standard] 的 C 扩展 / 纯 py 依赖。缺了服务仍能跑，但把它们的
# 导入一起试一遍，能顺带验证「site-packages 里的编译扩展能不能在 embedded
# Python 里加载」——这正是改写 _pth 之后最容易出问题的地方。
OPTIONAL_IMPORTS: tuple[str, ...] = (
    "httptools",
    "websockets",
    "watchfiles",
    "yaml",
    "dotenv",
)

SKIP_DIR_NAMES = {"__pycache__"}
SKIP_SUFFIXES = (".pyc", ".pyo")

TOTAL_STEPS = 6

# --------------------------------------------------------------------------- start.bat

# 注意：这里是**纯 ASCII**。改这个字符串时千万别写中文。
START_BAT = r"""@echo off
REM ===========================================================================
REM  XiaoMa AI Studio - portable edition launcher
REM
REM  KEEP THIS FILE PURE ASCII.
REM  cmd.exe parses batch files by byte offset. With `chcp 65001` plus multi-byte
REM  (e.g. Chinese) text inside the file those offsets desynchronise and cmd
REM  starts executing fragments of lines - the real symptom is
REM  `'cho.' is not recognized`, so the failure message itself blew up and the
REM  user saw nothing useful. Chinese instructions live in README-PORTABLE.txt.
REM ===========================================================================
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PY=%ROOT%runtime\python.exe"
set "CHECK=%ROOT%backend\tools\env_report.py"

REM Service port. If 8787 is taken, change the number below (see README-PORTABLE.txt).
set "PORT=8787"

echo ================================================
echo   XiaoMa AI Studio - portable edition
echo ================================================
echo.

if not exist "%PY%" (
  echo [ERROR] runtime\python.exe is missing, the package is incomplete.
  echo         Delete this folder, extract the zip again, and do not run it
  echo         from inside a zip viewer.
  echo         Details: README-PORTABLE.txt
  pause
  exit /b 1
)

echo [1/2] checking bundled dependencies ...
"%PY%" "%CHECK%" --deps-only
if errorlevel 1 (
  echo.
  echo [ERROR] Some bundled dependencies are missing, or cannot be imported by
  echo         runtime\python.exe.
  echo         Details: README-PORTABLE.txt
  pause
  exit /b 1
)

echo [2/2] starting the service ...
echo.
echo ================================================
echo   Open in your browser:  http://127.0.0.1:%PORT%
echo   Press Ctrl+C to stop the service.
echo ================================================
echo.

"%PY%" -m uvicorn app.main:app --app-dir "%ROOT%backend" --host 127.0.0.1 --port %PORT%
if errorlevel 1 (
  echo.
  echo [ERROR] The service failed to start. Common causes:
  echo         - port %PORT% is already in use: close the other program, or edit
  echo           the PORT line in this file and run start.bat again;
  echo         - antivirus / SmartScreen blocked runtime\python.exe: add this
  echo           folder to the allow list and retry;
  echo         - the package is incomplete: extract the zip again.
  echo         Details: backend\data\logs\app.log    Help: README-PORTABLE.txt
  pause
  exit /b 1
)

endlocal
"""

# --------------------------------------------------------------------------- README-PORTABLE.txt

README_PORTABLE = r"""小马AI工坊 · Windows 便携版
================================

这是什么
--------
一个「解压即用」的绿色包：不需要安装 Python，不需要联网装依赖，不需要跑 npm。
解压后双击 start.bat 就能用。

怎么用
------
1. 把 zip 完整解压到任意目录（路径里有中文也没关系；但建议不要放在
   C:\Program Files 这类需要管理员权限的位置）。
2. 双击包根的 start.bat。
3. 控制台出现  "Open in your browser:  http://127.0.0.1:8787"  之后，
   用浏览器打开 http://127.0.0.1:8787 。
4. 停止服务：在那个控制台窗口按 Ctrl+C，或者直接关掉窗口。

注意：start.bat 起的是一个前台服务，那个黑色控制台窗口必须一直开着；
      关掉窗口 = 停掉服务。不要在压缩软件的预览窗口里直接双击 start.bat，
      一定要先解压。

数据放在哪
----------
数据库、密钥、生成的图片/视频等全部在 backend\data\ 下面：

    backend\data\xiaoma.db        数据库
    backend\data\storage\         生成的图片 / 视频等产物
    backend\data\logs\app.log     运行日志（出问题先看它）

升级新版时：只替换程序目录（runtime\、backend\app\、backend\alembic\、
backend\tools\、frontend\webroot\、start.bat 等），
**不要删除 backend\data\**，历史数据和密钥都会保留。

端口 8787 被占用怎么办
----------------------
1. 如果本机已经有一个「小马AI工坊」在跑，那直接开浏览器访问
   http://127.0.0.1:8787 即可，不用再启动第二个。
2. 如果是别的程序占着 8787：用记事本打开包根的 start.bat，找到这一行

       set "PORT=8787"

   把 8787 改成别的端口（例如 8790），保存后重新双击 start.bat，
   然后浏览器访问 http://127.0.0.1:8790 。
   （start.bat 是纯英文文本，用记事本改不会乱码。）

启动失败怎么办
--------------
1. 看控制台里的英文报错。常见三类：
   - 端口被占用（改 PORT，见上一条）；
   - 杀毒软件 / SmartScreen 拦截了 runtime\python.exe（把整个文件夹加白名单）；
   - 包没解压完整（重新完整解压一遍 zip）。
2. 看 backend\data\logs\app.log 的最后几十行。
3. 想要一份详细的中文环境诊断报告，在包根目录打开命令行执行：

       runtime\python.exe backend\tools\env_report.py --report

   它只读环境信息（解释器版本、依赖是否齐全、端口占用、目录位置），
   不打印任何 API Key / 密钥 / 数据库内容，可以整段复制发给作者。

日志在哪
--------
    backend\data\logs\app.log                应用日志（滚动，出问题先看它）
    start.bat 那个控制台窗口里的输出          实时日志（进程日志也在这儿）

已知限制
--------
- 这是便携包，不是安装包：没有快捷方式、不写开始菜单、不写注册表，
  删掉整个文件夹就等于卸载（记得先备份 backend\data\ 里的数据）。
- 只带 Windows 64 位的官方嵌入式 Python 运行时（3.11 系列），
  32 位 Windows 或 macOS / Linux 用不了。
- 视频合成等功能需要系统里已有 FFmpeg（程序会去 PATH 和常见安装位置找），
  便携包里不包含它。
- 首次在一个新目录下启动，会被 SmartScreen 提示一次，选「仍要运行」即可。
"""

# --------------------------------------------------------------------------- 小工具


def _reconfigure_stdout() -> None:
    """Windows 控制台默认不是 UTF-8，中文输出可能炸；这里兜一下。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def log(msg: str = "") -> None:
    print(msg, flush=True)


def step(index: int, title: str) -> None:
    log(f"[{index}/{TOTAL_STEPS}] {title}")


def die(msg: str, code: int = 1) -> None:
    log("")
    log(f"[错误] {msg}")
    raise SystemExit(code)


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def dir_size(path: Path) -> tuple[int, int]:
    """返回 (字节数, 文件数)。"""
    total = 0
    count = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
                count += 1
        except OSError:
            continue
    return total, count


def _rmtree(path: Path) -> None:
    """删除目录；遇到只读文件时先改权限再删（wheel 里可能有只读文件）。"""

    def _onexc(func, p, _exc):  # type: ignore[no-untyped-def]
        try:
            os.chmod(p, 0o777)
            func(p)
        except Exception:  # noqa: BLE001
            pass

    shutil.rmtree(path, onerror=_onexc)  # type: ignore[arg-type]


def _skip(rel: Path) -> bool:
    return any(part in SKIP_DIR_NAMES for part in rel.parts) or rel.name.endswith(
        SKIP_SUFFIXES
    )


def _run(cmd: list[str], *, env: dict[str, str] | None = None, timeout: int = 900):
    """跑一条命令，实时回显输出，返回 (退出码, 最后若干行, 合并输出)。"""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )
    lines: list[str] = []
    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            lines.append(line)
            log(f"    | {line}")
            if len(lines) > 4000:  # 极端情况下别把内存吃满
                del lines[:2000]
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = -9
        lines.append(f"(超时 {timeout}s，已强制结束)")
    return code, lines[-40:], "\n".join(lines)


def _capture(cmd: list[str], *, timeout: int = 300) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return -1, repr(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# --------------------------------------------------------------------------- 步骤 1


def find_version() -> str:
    path = ROOT / VERSION_FILE
    if not path.exists():
        die(f"找不到版本号来源文件：{path}")
    match = VERSION_RE.search(path.read_text(encoding="utf-8"))
    if not match:
        die(f"{path} 里没有解析到 __version__")
    return match.group(1)


def check_sources() -> int:
    """进包的文件都得在；前端产物不在就直接报错，绝不偷偷跑 npm。"""
    missing: list[str] = []
    for rel, _ in COPY_TREES:
        if not (ROOT / rel).is_dir():
            missing.append(rel + "/")
    for rel in COPY_FILES:
        if not (ROOT / rel).is_file():
            missing.append(rel)

    index = ROOT / "frontend" / "webroot" / "index.html"
    if not index.is_file():
        die(
            "前端构建产物不存在：frontend/webroot/index.html\n"
            "       本脚本不负责构建前端（不会执行 npm）。请先在 frontend 目录跑一次\n"
            "       `npm install && npm run build`，把产物生成到 frontend/webroot 之后再来打包。"
        )
    if missing:
        die("以下文件/目录缺失，无法打包：\n       - " + "\n       - ".join(missing))

    source_files = 0
    for rel, _ in COPY_TREES:
        for item in (ROOT / rel).rglob("*"):
            if item.is_file() and not _skip(item.relative_to(ROOT / rel)):
                source_files += 1
    source_files += len(COPY_FILES)

    log(f"    版本号：{find_version()}")
    log(f"    仓库根：{ROOT}")
    log(f"    前端产物：{index}（{human(index.stat().st_size)}）")
    log(f"    待拷贝：{source_files} 个文件")
    return source_files


# --------------------------------------------------------------------------- 步骤 2


def cache_dir_from(args: argparse.Namespace) -> Path:
    if args.cache_dir:
        return Path(args.cache_dir).expanduser().resolve()
    env = os.environ.get("XIAOMA_PORTABLE_CACHE", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(tempfile.gettempdir()) / "xiaoma-portable-cache"


def _zip_intact(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            if zf.testzip() is not None:
                return False
            return any(name.endswith("python.exe") for name in zf.namelist())
    except Exception:  # noqa: BLE001
        return False


def ensure_embed_zip(url: str, cache: Path) -> Path:
    """下载官方 embed 包到缓存目录；命中缓存就不再联网（离线也能二次构建）。"""
    cache.mkdir(parents=True, exist_ok=True)
    name = Path(urlparse(url).path).name or "python-embed-amd64.zip"
    target = cache / name

    if target.exists():
        if _zip_intact(target):
            log(f"    命中缓存：{target}（{human(target.stat().st_size)}，不联网）")
            return target
        log(f"    缓存文件损坏，删除后重新下载：{target}")
        target.unlink()

    log(f"    下载：{url}")
    tmp = target.with_name(target.name + ".part")
    req = urllib.request.Request(
        url, headers={"User-Agent": "xiaoma-make-portable"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            decile = 0
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if total:
                    now = got * 10 // total
                    if now > decile:
                        decile = now
                        log(f"      {decile * 10}%  {human(got)} / {human(total)}")
    except urllib.error.URLError as exc:
        tmp.unlink(missing_ok=True)
        die(
            f"下载嵌入式 Python 失败：{exc}\n"
            "       请检查网络或代理，也可以手动下载后用 --python-url 指到本地文件路径，\n"
            f"       或者把 zip 放进缓存目录：{cache}"
        )
    tmp.replace(target)

    if not _zip_intact(target):
        die(f"下载到的文件不是可用的嵌入包（校验失败）：{target}")
    log(f"    下载完成：{target}（{human(target.stat().st_size)}）")
    return target


def patch_pth(runtime: Path) -> Path:
    """改写 python311._pth：保留原内容，末尾补 site-packages 与 `import site`。

    嵌入式 Python 一旦存在 `._pth` 文件，就进入隔离模式（等价 -I）：sys.path 完全
    由这个文件决定，site-packages 不会被自动加入。而用 pip --target 装出来的依赖
    恰恰在 Lib\\site-packages 里 —— 不改这一行，import fastapi 必然失败。

    `import site` 是给 site.py 一个执行机会（处理 site-packages 里的 .pth），
    顺便让 `-m uvicorn` 这类执行方式的行为更接近普通 Python。
    """
    candidates = sorted(runtime.glob("python*._pth"))
    if not candidates:
        die(f"runtime 里找不到 python*._pth，嵌入包可能不完整：{runtime}")
    pth = candidates[0]

    # utf-8-sig：万一文件带了 BOM 也读得对；BOM 在读之后就没了，不会再写回去。
    text = pth.read_bytes().decode("utf-8-sig")
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()

    wanted = ["Lib\\site-packages", "import site"]
    added = [w for w in wanted if w not in lines]
    lines.extend(added)

    # 必须是 ASCII 无 BOM：解释器逐行读这个文件，多一个字节都不行。
    pth.write_bytes(("\r\n".join(lines) + "\r\n").encode("ascii"))
    log(f"    已改写 {pth.name}：{pth}")
    for line in lines:
        log(f"      {line}")
    if added:
        log(f"    新增行：{'、'.join(added)}")
    return pth


def extract_runtime(embed_zip: Path, runtime: Path) -> None:
    if runtime.exists():
        log(f"    清理旧的 runtime：{runtime}")
        _rmtree(runtime)
    runtime.mkdir(parents=True)
    with zipfile.ZipFile(embed_zip) as zf:
        zf.extractall(runtime)

    exe = runtime / "python.exe"
    if not exe.exists():
        die(f"解压后没找到 runtime\\python.exe，嵌入包内容不对：{embed_zip}")
    code, out = _capture([str(exe), "-c", "import sys; print(sys.version)"])
    if code != 0:
        die(f"runtime\\python.exe 跑不起来（退出码 {code}）：\n{out.strip()}")
    log(f"    运行时解释器可用：{out.strip().splitlines()[0]}")
    patch_pth(runtime)


# --------------------------------------------------------------------------- 步骤 3


def install_deps(build_python: Path, req: Path, site_packages: Path, cache: Path) -> None:
    site_packages.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(build_python),
        "-m",
        "pip",
        "install",
        "--target",
        str(site_packages),
        "-r",
        str(req),
        "--prefer-binary",
        "--upgrade",
        "--no-warn-script-location",
    ]
    log("    " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    env = dict(os.environ)
    env["PIP_CACHE_DIR"] = str(cache / "pip")
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env.pop("PYTHONPATH", None)  # 别把构建机上的包混进目标目录

    code, tail, _ = _run(cmd, env=env)
    if code != 0:
        die(
            "pip 安装依赖失败（退出码 "
            f"{code}）。上面的 pip 输出就是原因。\n"
            "       常见两种：目标平台没有对应的 win_amd64 轮子（看包名/版本）、网络或代理问题。\n"
            "       提示：依赖要装给 Python 3.11 win_amd64 用，构建侧解释器也必须是 3.11。"
        )

    # pip --target 会把控制台入口脚本放到 <target>/Scripts（Windows）或 bin，
    # 这些 .exe 里嵌的是**构建机解释器**的绝对路径，到用户机器上必然跑不起来，
    # 顺手还会把构建机的路径泄露进包里。我们只用 `-m uvicorn`，直接删掉。
    for junk in ("Scripts", "bin"):
        p = site_packages / junk
        if p.exists():
            log(f"    删除 pip 生成的入口脚本目录（含构建机绝对路径）：{junk}")
            _rmtree(p)


# --------------------------------------------------------------------------- 步骤 4


def _probe_script(names: list[str]) -> str:
    return "\n".join(
        [
            "import importlib, json, sys",
            f"names = {names!r}",
            "ok = {}",
            "bad = []",
            "for n in names:",
            "    try:",
            "        m = importlib.import_module(n)",
            "        ok[n] = str(getattr(m, '__version__', '') or '')",
            "    except Exception as exc:",
            "        bad.append(n + ' -> ' + type(exc).__name__ + ': ' + str(exc).splitlines()[0])",
            "print('PROBE ' + json.dumps({'ok': ok, 'bad': bad, 'version': sys.version, 'exe': sys.executable}, ensure_ascii=False))",
        ]
    )


def verify_imports(runtime_py: Path) -> None:
    """真起一次运行时解释器去 import，而不是查文件是否存在。"""
    names = list(REQUIRED_IMPORTS) + list(OPTIONAL_IMPORTS)
    code, out = _capture([str(runtime_py), "-c", _probe_script(names)], timeout=300)
    if code != 0:
        die(f"运行时解释器导入自检失败（退出码 {code}）：\n{out.strip()}")

    payload = None
    for line in out.splitlines():
        if line.startswith("PROBE "):
            payload = json.loads(line[len("PROBE ") :])
    if payload is None:
        die(f"自检没有返回可解析的结果：\n{out.strip()}")

    log(f"    解释器：{payload['version']}")
    log(f"    可执行：{payload['exe']}")
    ok: dict[str, str] = payload["ok"]
    bad: list[str] = payload["bad"]
    for name in REQUIRED_IMPORTS:
        if name in ok:
            log(f"      [OK]   {name} {ok[name]}".rstrip())
    for name in OPTIONAL_IMPORTS:
        if name in ok:
            log(f"      [OK]   {name} {ok[name]}（uvicorn[standard] 附带的）".rstrip())

    missing_required = [b for b in bad if b.split(" -> ")[0] in REQUIRED_IMPORTS]
    if missing_required:
        die(
            "运行时解释器导入不了这些必需依赖：\n       - "
            + "\n       - ".join(missing_required)
            + "\n       多半是 _pth 没改对，或 site-packages 装到了别的位置。"
        )
    missing_optional = [b for b in bad if b.split(" -> ")[0] in OPTIONAL_IMPORTS]
    if missing_optional:
        log("      [警告] 以下附带的依赖导入失败（不影响启动）：")
        for item in missing_optional:
            log(f"        - {item}")


# --------------------------------------------------------------------------- 步骤 5


def clean_stage(out: Path) -> None:
    """清理上一次打包留下的应用文件。

    `runtime/` 故意保留：`--skip-deps` 就是靠复用它的；`backend/data/` 也保留，
    免得哪次误操作把别人放进去的数据删了。
    """
    stale = [
        out / "backend" / "app",
        out / "backend" / "alembic",
        out / "backend" / "tools",
        out / "backend" / "alembic.ini",
        out / "backend" / "requirements.txt",
        out / "backend" / "__pycache__",
        out / "frontend",
        out / "start.bat",
        out / "README-PORTABLE.txt",
    ]
    removed = 0
    for path in stale:
        if path.is_dir():
            _rmtree(path)
            removed += 1
        elif path.exists():
            path.unlink()
            removed += 1
    if removed:
        log(f"    清理了 {removed} 项上一次的产物（保留 runtime/ 与 backend/data/）")


def copy_app(out: Path) -> tuple[int, int]:
    copied = 0
    copied_bytes = 0

    for rel, skip_names in COPY_TREES:
        src = ROOT / rel
        dst = out / rel
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.rglob("*"):
            inner = item.relative_to(src)
            if _skip(inner):
                continue
            if inner.parent == Path(".") and inner.name in skip_names:
                log(f"    跳过：{rel}/{inner.name}")
                continue
            target = dst / inner
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
                copied += 1
                copied_bytes += item.stat().st_size
        log(f"    {rel} -> 完成")

    for rel in COPY_FILES:
        src = ROOT / rel
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
        copied_bytes += src.stat().st_size
    log(f"    单文件 {len(COPY_FILES)} 个 -> 完成")

    data = out / DATA_DIR
    if not data.exists():
        data.mkdir(parents=True, exist_ok=True)
        log(f"    新建空目录：{DATA_DIR}/")
    else:
        log(f"    已存在（保留用户数据）：{DATA_DIR}/")

    # 拷完之后再扫一遍，把上一步可能带进来的缓存清掉（前端产物里不会，但保险）
    for item in list(out.rglob("__pycache__")):
        if item.is_dir():
            _rmtree(item)
    return copied, copied_bytes


# --------------------------------------------------------------------------- 步骤 6


def write_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def write_start_bat(out: Path) -> Path:
    path = out / "start.bat"
    text = START_BAT.replace("\r\n", "\n").replace("\n", "\r\n")
    try:
        raw = text.encode("ascii")
    except UnicodeEncodeError as exc:  # pragma: no cover - 只有写错字才会发生
        die(
            "start.bat 出现了非 ASCII 字符，这会让 cmd.exe 的字节偏移解析错位"
            f"（典型报错 'cho.' is not recognized）：{exc}\n"
            "       请把中文说明挪到 README-PORTABLE.txt，start.bat 里只留英文。"
        )

    bad = [i for i, b in enumerate(raw) if b > 127]
    if bad:  # pragma: no cover - 与上面的 encode 断言互为兜底
        die(f"start.bat 第 {bad[0]} 字节起出现非 ASCII（共 {len(bad)} 处），已拒绝出包")
    lone_lf = raw.count(b"\n") - raw.count(b"\r\n")
    if lone_lf:
        log(f"    [警告] start.bat 有 {lone_lf} 处裸 LF 换行，已按 CRLF 写入（批处理更稳）")

    write_bytes(path, raw)
    log(f"    已写出 {path}（{human(len(raw))}，纯 ASCII 已断言：{len(raw)} 字节）")
    return path


def write_readme(out: Path) -> Path:
    path = out / "README-PORTABLE.txt"
    raw = README_PORTABLE.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
    write_bytes(path, raw)
    log(f"    已写出 {path}（UTF-8，{human(len(raw))}）")
    return path


def make_zip(out: Path, zip_path: Path, pkg_root: str) -> tuple[int, int]:
    files: list[Path] = []
    empty_dirs: list[Path] = []
    for item in out.rglob("*"):
        rel = item.relative_to(out)
        if _skip(rel):
            continue
        if item.is_dir():
            try:
                if not any(item.iterdir()):
                    empty_dirs.append(rel)
            except OSError:
                pass
        elif item.is_file():
            files.append(item)
    files.sort()

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for rel in empty_dirs:  # 空目录（backend/data）也要在包里留个位
            zf.writestr(f"{pkg_root}/{rel.as_posix().rstrip('/')}/", "")
        for path in files:
            rel = path.relative_to(out).as_posix()
            zf.write(path, arcname=f"{pkg_root}/{rel}")
            written += 1
            if written % 400 == 0:
                log(f"      已压缩 {written} 个文件 ...")
    log(f"    压缩完成：{written} 个文件 + {len(empty_dirs)} 个空目录")
    return written, len(empty_dirs)


# --------------------------------------------------------------------------- main


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="打包 Windows 解压即用便携包（嵌入式 Python 3.11 + 依赖 + 应用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="解包目录（默认 dist/portable；相对路径按当前工作目录解析）",
    )
    parser.add_argument(
        "--skip-deps",
        action="store_true",
        help="复用已装好的 runtime（跳过下载/解压/装依赖），二次打包快很多",
    )
    parser.add_argument(
        "--python-url",
        default=DEFAULT_PYTHON_URL,
        help=f"嵌入式 Python 的 zip 地址（默认 {DEFAULT_PYTHON_URL}；可指向本地文件）",
    )
    parser.add_argument(
        "--keep-zip",
        action="store_true",
        help="若目标 zip 已存在就保留它、跳过压缩（只想刷新解包目录时用）",
    )
    parser.add_argument(
        "--cache-dir",
        default="",
        help="下载缓存目录（默认取环境变量 XIAOMA_PORTABLE_CACHE，否则用系统临时目录）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdout()
    args = parse_args(argv)

    started = time.perf_counter()
    out = Path(args.out).expanduser()
    if not out.is_absolute():
        out = (Path.cwd() / out).resolve()
    else:
        out = out.resolve()

    version = find_version()
    zip_name = f"xiaoma-ai-studio-v{version}-win64-portable.zip"
    zip_path = out.parent / zip_name
    pkg_root = zip_name[: -len(".zip")]  # zip 里的顶层目录，解压出来就是一个文件夹
    cache = cache_dir_from(args)

    log("=" * 62)
    log("  小马AI工坊 · Windows 便携包打包")
    log("=" * 62)
    log(f"  仓库根  ：{ROOT}")
    log(f"  解包目录：{out}")
    log(f"  目标压缩包：{zip_path}")
    log(f"  缓存目录：{cache}")
    log("")

    # ---------- 1 源检查
    step(1, "检查源文件与版本号")
    check_sources()
    log("")

    out.mkdir(parents=True, exist_ok=True)
    runtime = out / "runtime"
    runtime_py = runtime / "python.exe"
    site_packages = runtime / "Lib" / "site-packages"
    req = ROOT / "backend" / "requirements.txt"
    build_python = Path(sys.executable).resolve()

    # ---------- 2 运行时
    step(2, "准备 Windows 嵌入式 Python 运行时")
    major_minor = f"{sys.version_info[0]}.{sys.version_info[1]}"
    if major_minor != "3.11":
        log(
            f"    [警告] 当前构建侧解释器是 {major_minor}（{build_python}），"
            "而默认下载的是 3.11 的 embed 包。\n"
            "           版本不一致时 pip 可能取到源码包而失败，请用 3.11 的解释器打包。"
        )
    if args.skip_deps and runtime_py.exists():
        log(f"    复用已有运行时（--skip-deps）：{runtime_py}")
        patch_pth(runtime)  # 幂等；顺手保证 _pth 一定是对的
    else:
        url = args.python_url
        if url and Path(url).exists():
            embed_zip = Path(url).resolve()
            log(f"    使用本地嵌入包：{embed_zip}")
        else:
            embed_zip = ensure_embed_zip(url, cache)
        extract_runtime(embed_zip, runtime)
    log("")

    # ---------- 3 依赖
    step(3, "安装后端依赖到 runtime/Lib/site-packages")
    deps_ready = site_packages.is_dir() and any(site_packages.iterdir())
    if args.skip_deps and deps_ready:
        log(f"    跳过（--skip-deps，复用已装好的依赖）：{site_packages}")
    elif args.skip_deps and not deps_ready:
        die(
            "--skip-deps 要求 runtime 里已经有装好的依赖，但没找到：\n"
            f"       {site_packages}\n"
            "       先不带 --skip-deps 跑一次完整打包。"
        )
    else:
        if site_packages.exists() and any(site_packages.iterdir()):
            log(f"    清空旧的 site-packages：{site_packages}")
            _rmtree(site_packages)
        install_deps(build_python, req, site_packages, cache)
    deps_bytes, deps_files = dir_size(site_packages)
    log(f"    site-packages：{deps_files} 个文件，{human(deps_bytes)}")
    log("")

    # ---------- 4 抽查导入
    step(4, "用运行时解释器抽查关键依赖")
    verify_imports(runtime_py)
    log("")

    # ---------- 5 拷贝应用
    step(5, "拷贝应用代码、前端产物与文档")
    clean_stage(out)
    copied, copied_bytes = copy_app(out)
    log(f"    共拷贝 {copied} 个文件，{human(copied_bytes)}")
    log("")

    # ---------- 6 启动脚本 + 压缩
    step(6, "生成启动脚本/说明，并压缩")
    write_start_bat(out)
    write_readme(out)

    zip_kept = False
    if args.keep_zip and zip_path.exists():
        size_mb = human(zip_path.stat().st_size)
        log(f"    --keep-zip：目标压缩包已存在，保留不重建（{size_mb}）")
        zip_kept = True
    else:
        if zip_path.exists():
            log(f"    覆盖旧的压缩包：{zip_path.name}")
            zip_path.unlink()
        make_zip(out, zip_path, pkg_root)

    # ---------- 收尾统计
    out_bytes, out_files = dir_size(out)
    zip_bytes = zip_path.stat().st_size
    elapsed = time.perf_counter() - started

    log("")
    log("=" * 62)
    log("  便携包已就绪")
    log("=" * 62)
    log(f"  版本      ：v{version}")
    log(f"  解包目录  ：{out}")
    log(f"               {out_files} 个文件，{human(out_bytes)}")
    log(f"  压缩包    ：{zip_path}")
    log(f"               {human(zip_bytes)}" + ("（沿用已有包）" if zip_kept else ""))
    log(f"  耗时      ：{elapsed:.1f} 秒")
    log("")
    log("  解压 zip 后双击包根的 start.bat 即可运行（默认端口 8787）。")
    log("  不想动 8787 时，可直接用包内运行时手工验证（换端口）：")
    log(
        f'    "{out}\\runtime\\python.exe" -m uvicorn app.main:app '
        f'--app-dir "{out}\\backend" --host 127.0.0.1 --port 8788'
    )
    log("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
