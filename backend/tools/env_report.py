"""环境体检 / 诊断报告 —— 装不上、起不来时先跑这个。

两种用法：

1. 启动脚本会用它做「开跑前的把关」：
       python backend/tools/env_report.py
   有任何「错误」项就退出码非 0，启动脚本据此中止并给出可读原因。

2. 别人用着出问题、你要远程定位时，让他跑这一条，把输出整段发回来：
       python backend/tools/env_report.py --report

关于隐私：本脚本**只读环境信息**（解释器版本、命令行工具版本、目录位置、
依赖是否装得上、端口是否被占），不读取、不打印任何 API Key / 令牌 /
数据库内容 / 提示词正文。`--report` 的输出可以放心直接贴出来。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

# 版本下限。低于它直接判错。
MIN_PY = (3, 11)
# 上游依赖大多只发预编译轮子。太新的解释器常常没有对应轮子，
# 于是 pip 会退化成源码编译（要 Rust / C 编译器）而失败——所以给个提醒。
WARN_NEWER_THAN = (3, 13)

# 运行期真正要能导入的包。这里写 requirements.txt 里的**包名**（含 extras），
# 导入名由 module_for_spec() 推导，免得两处各写一份、改了一处忘了另一处。
RUNTIME_DEPS = [
    "fastapi",
    "uvicorn[standard]",
    "sqlalchemy[asyncio]",
    "alembic",
    "aiosqlite",
    "pydantic",
    "pydantic-settings",
    "httpx",
    "python-multipart",
    "cryptography",
]

# 包名与导入名不一致的例外（大多数只是把 - 换成 _）
_IMPORT_NAME = {"python-multipart": "multipart"}

OK, WARN, ERR = "OK", "警告", "错误"

# 启动脚本失败时把「中文解释」交给这里打印。
#
# 为什么不直接写在 .bat 里：cmd.exe 解析批处理是按字节偏移顺序读的，一旦文件里
# 出现多字节字符（中文），配合 `chcp 65001` 会让偏移错位——实测会把 `echo` 拆成
# `cho.`、把整行中文当成命令去执行，于是「失败提示」本身先崩了，用户什么也看不到。
# 所以 .bat 里只留 ASCII，所有面向用户的中文都由 Python 输出（Python 走
# WriteConsoleW 写控制台，中文在任何代码页下都正常）。
HINTS: dict[str, list[str]] = {
    "no-python": [
        "[错误] 找不到可用的 python（命令不存在，或者存在但跑不起来）。",
        "       两种常见情况：",
        "       1. 还没装 → https://www.python.org/downloads/ 需要 Python 3.11+，",
        "          安装时务必勾选「Add python.exe to PATH」，装完重开一个终端窗口；",
        "       2. 装的是微软商店版本 → 它只是个「跳板」，本身不干活。请到",
        "          「设置 → 应用 → 高级应用设置 → 应用执行别名」关掉 python.exe /",
        "          python3.exe 这两个别名，再从 python.org 装官方版本。",
    ],
    "env-check": [
        "[错误] 环境体检没通过，已中止。",
        "       请按上面「→」的提示处理后重试；看不懂就整段发给作者。",
    ],
    "venv-failed": [
        "[错误] 创建虚拟环境失败。常见原因：",
        "       1. 当前 python 是精简版 / 嵌入式版（ComfyUI 整合包里带的那个就是），缺少 venv 模块；",
        "       2. 当前目录没有写入权限（比如装在 Program Files 里）。",
        "       两条出路，任选一条：",
        "       · 从 python.org 安装官方 Python 3.11 / 3.12 后重试（最稳）；",
        "       · 或者装 uv（https://docs.astral.sh/uv/）后再跑一次 start.bat，",
        "         脚本会自动改用它来建环境——uv 不依赖解释器自带的 venv 模块。",
    ],
    "venv-missing": [
        "[错误] 虚拟环境没有建成功（找不到 venv 里的 python）。",
        "       多半是上一步的 python 不对，请从 python.org 装官方 Python 3.11 / 3.12 后重试。",
    ],
    "pip-upgrade": [
        "[错误] 升级 pip 失败。",
        "       多半是网络问题（公司网络 / 需要代理）。可手动执行看看报错：",
        '       "backend\\venv\\Scripts\\python.exe" -m pip install --upgrade pip',
    ],
    "pip-install": [
        "[错误] 安装后端依赖失败。上面 pip 的报错就是原因，常见两种：",
        "       1. Python 版本太新，依赖还没有现成的安装包 → 换 Python 3.11 或 3.12 最稳；",
        "       2. 网络问题（公司网络 / 需要代理）→ 配置 pip 镜像源后重试。",
        "       想看完整报错，去掉 --quiet 手动跑一次：",
        '       "backend\\venv\\Scripts\\python.exe" -m pip install -r backend\\requirements.txt',
    ],
    "npm-missing": [
        "[错误] 未检测到 npm。",
        "       它通常随 Node.js 一起安装，请安装 Node.js 20+：https://nodejs.org/",
    ],
    "npm-install": [
        "[错误] 安装前端依赖失败。上面 npm 的报错就是原因。",
        "       国内网络可以试试换源： npm config set registry https://registry.npmmirror.com",
    ],
    "npm-build": [
        "[错误] 构建前端界面失败。上面 npm 的报错就是原因。",
        "       需要 Node.js 20+（版本太低会构建失败）： node --version",
    ],
    "serve-failed": [
        "[错误] 服务启动失败。",
        "       若提示 8787 端口被占用，请先关掉占用该端口的程序；",
        "       其它情况请把上面的报错整段发给作者。",
    ],
}


def module_for_spec(spec: str) -> str:
    """把 requirements.txt 里的包名换成 import 时用的模块名。"""
    base = spec.split("[")[0].strip().lower()
    return _IMPORT_NAME.get(base, base.replace("-", "_"))


class Finding:
    __slots__ = ("level", "title", "detail", "hint")

    def __init__(self, level: str, title: str, detail: str = "", hint: str = "") -> None:
        self.level = level
        self.title = title
        self.detail = detail
        self.hint = hint


def _reconfigure_stdout() -> None:
    """Windows 控制台默认不是 UTF-8，中文会炸；这里兜一下。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def _cmd_exe() -> str:
    """拿到 cmd.exe 的绝对路径。

    不能直接写 "cmd.exe"：PATH 被裁剪过的环境（比如某些 IDE 内置终端）里
    它可能找不到，于是 npm 这类 .cmd 工具会被误判成「不可用」。
    """
    comspec = os.environ.get("COMSPEC")
    if comspec and Path(comspec).exists():
        return comspec
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return str(Path(root) / "System32" / "cmd.exe")


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    """跑一条外部命令，返回 (退出码, 第一行输出)。找不到命令返回 (-1, "")。

    Windows 上 npm / npx 这类其实是 `.cmd` 批处理，CreateProcess 不能直接
    执行，必须经 cmd.exe 转一手——否则会误报「命令不可用」。
    """
    argv = list(cmd)
    if os.name == "nt" and argv and argv[0].lower().endswith((".cmd", ".bat")):
        argv = [_cmd_exe(), "/d", "/c", *argv]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            shell=False,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return -1, ""
    except subprocess.TimeoutExpired:
        return -2, ""
    except Exception:  # noqa: BLE001
        return -3, ""
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode or 0, out.splitlines()[0] if out else ""


def looks_like_custom_nodes(path: Path) -> bool:
    """项目是不是被放进了 ComfyUI 的 custom_nodes 目录。

    这个位置是错的，而且错得很隐蔽：ComfyUI 会把这个仓库当成一个插件去
    import，而它自己又要起一个 8787 的 Web 服务——两边互相干扰，
    更常见的是把 ComfyUI 那套「嵌入式 Python」误当成系统 Python 来用，
    于是 `python -m venv` 根本没法工作。
    """
    return any(part.lower() == "custom_nodes" for part in path.resolve().parts)


def venv_dir(root: Path) -> Path:
    return root / "backend" / "venv"


def venv_python(root: Path) -> Path:
    if os.name == "nt":
        return venv_dir(root) / "Scripts" / "python.exe"
    return venv_dir(root) / "bin" / "python"


def in_venv() -> bool:
    return sys.prefix != sys.base_prefix or bool(os.environ.get("VIRTUAL_ENV"))


def version_finding(version: tuple[int, int, int], exe: str) -> Finding:
    """按版本号给出判断。抽成纯函数，方便单测把 3.10 / 3.11 / 3.14 都过一遍。"""
    ver_s = ".".join(str(x) for x in version)
    if version[:2] < MIN_PY:
        return Finding(
            ERR,
            f"Python 版本过低：{ver_s}",
            f"当前解释器：{exe}",
            "本项目需要 Python 3.11 或更高版本。装了新 Python 之后，"
            "请确认它的路径排在 PATH 最前面（`where python` 看第一个是不是你刚装的）。",
        )
    if version[:2] > WARN_NEWER_THAN:
        return Finding(
            WARN,
            f"Python 版本较新：{ver_s}",
            f"当前解释器：{exe}",
            "部分后端依赖可能还没发布对应版本的预编译包，安装时会尝试源码编译而失败。"
            "如果卡在「安装后端依赖」这一步，换成 Python 3.11 或 3.12 最稳。",
        )
    return Finding(OK, f"Python 版本：{ver_s}", exe)


def check_interpreter(root: Path) -> list[Finding]:
    out: list[Finding] = [version_finding(tuple(sys.version_info[:3]), sys.executable)]  # type: ignore[arg-type]

    if not in_venv():
        # 精简体 / 嵌入式 Python（ComfyUI 整合包里那种）没有 venv 与 ensurepip，
        # `python -m venv` 会直接失败——这是「第一步看着过去了、第二步一直失败」的头号原因。
        missing = [m for m in ("venv", "ensurepip") if importlib.util.find_spec(m) is None]
        if missing:
            out.append(
                Finding(
                    ERR,
                    f"当前 Python 缺少标准库模块：{'、'.join(missing)}",
                    f"当前解释器：{sys.executable}",
                    "这通常说明你用的是「精简版 / 嵌入式」Python（ComfyUI 整合包里带的那个就是）。"
                    "它没法创建虚拟环境。请到 python.org 装一个官方 Python 3.11 或 3.12，"
                    "并确保 `where python` 的第一个是它。",
                )
            )
        else:
            out.append(Finding(OK, "当前是系统 Python，可创建虚拟环境"))
    else:
        out.append(Finding(OK, "当前已在虚拟环境内", sys.prefix))

    return out


def check_location(root: Path) -> list[Finding]:
    out: list[Finding] = []
    if looks_like_custom_nodes(root):
        out.append(
            Finding(
                ERR,
                "项目被放在了 ComfyUI 的 custom_nodes 目录里",
                str(root),
                "这个位置不能用。ComfyUI 会把这个仓库当成插件去加载，而本项目自己是一个"
                "独立的 Web 服务（8787 端口），两者会互相干扰；而且 ComfyUI 整合包带的"
                "是嵌入式 Python，创建虚拟环境会失败。\n"
                "     请挪到普通目录再启动，例如： D:\\xiaoma-ai-studio",
            )
        )
    else:
        out.append(Finding(OK, "项目位置正常", str(root)))
    return out


def check_tooling(root: Path, *, need_node: bool) -> list[Finding]:
    out: list[Finding] = []

    git = shutil.which("git")
    if git:
        code, ver = _run([git, "--version"])
        out.append(Finding(OK, f"git 可用：{ver}", git) if code == 0 else Finding(WARN, "git 存在但调不动", git))
    else:
        out.append(
            Finding(
                WARN,
                "未检测到 git",
                "",
                "不影响本次启动，但「一键更新」需要 git clone 安装才能用。",
            )
        )

    if not need_node:
        return out

    node = shutil.which("node")
    if node:
        code, ver = _run([node, "--version"])
        out.append(Finding(OK, f"Node.js：{ver}", node) if code == 0 else Finding(WARN, "node 存在但调不动", node))
    else:
        out.append(
            Finding(
                ERR,
                "未检测到 Node.js",
                "",
                "构建前端界面需要 Node.js 20+：https://nodejs.org/  装完记得重开一个终端。",
            )
        )

    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm:
        code, ver = _run([npm, "--version"])
        out.append(Finding(OK, f"npm：{ver}", npm) if code == 0 else Finding(WARN, "npm 存在但调不动", npm))
    else:
        out.append(
            Finding(
                ERR,
                "未检测到 npm",
                "",
                "npm 通常随 Node.js 一起安装。若 node 可用而 npm 不可用，"
                "多半是安装时没勾选 npm，重装 Node.js 即可。",
            )
        )
    return out


def check_venv_and_deps(root: Path) -> list[Finding]:
    out: list[Finding] = []
    vpy = venv_python(root)
    if not vpy.exists():
        out.append(
            Finding(
                WARN,
                "虚拟环境尚未创建",
                f"预期位置：{vpy}",
                "首次运行 start.bat / start.sh 会自动创建；若反复创建失败，见上方「Python 缺少标准库模块」那条。",
            )
        )
        return out

    out.append(Finding(OK, "虚拟环境已存在", str(vpy)))

    missing = [s for s in RUNTIME_DEPS if importlib.util.find_spec(module_for_spec(s)) is None]

    # 体检通常是拿「系统 Python」跑的，而依赖装在虚拟环境里。
    # 这种情况下「缺依赖」是正常的，不能报成问题，否则会把人带偏。
    try:
        running_under_venv = Path(sys.executable).resolve() == vpy.resolve()
    except OSError:
        running_under_venv = False

    if not running_under_venv:
        # 依赖装在虚拟环境里，而体检是拿系统 Python 跑的。
        # 不能就此放过——用虚拟环境自己的解释器再问一次，才能真的验证依赖。
        return out + check_venv_deps_via_subprocess(vpy)

    if missing:
        out.append(
            Finding(
                WARN,
                f"虚拟环境里缺少 {len(missing)} 个后端依赖",
                "、".join(missing),
                "依赖没装全，重跑一次 start.bat / start.sh 让脚本补装。",
            )
        )
    else:
        out.append(Finding(OK, f"后端依赖齐全（{len(RUNTIME_DEPS)} 项）"))
    return out


def check_venv_deps_via_subprocess(vpy: Path) -> list[Finding]:
    """让虚拟环境的解释器自己报一遍缺哪些依赖。"""
    try:
        script = str(Path(__file__).resolve())
    except NameError:  # pragma: no cover - 只在极端环境下发生
        return [Finding(WARN, "无法验证虚拟环境里的依赖", "定位不到体检脚本自身")]
    code, out = _run([str(vpy), script, "--deps-only"], timeout=90)
    if code == 0:
        return [Finding(OK, f"后端依赖齐全（{len(RUNTIME_DEPS)} 项）")]
    if code == 1 and out:
        return [
            Finding(
                WARN,
                "虚拟环境里缺少后端依赖",
                out,
                "依赖没装全，重跑一次 start.bat / start.sh 让脚本补装。",
            )
        ]
    return [Finding(WARN, "无法验证虚拟环境里的依赖", f"退出码 {code} {out}".strip())]


def check_port_free(port: int) -> list[Finding]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        busy = s.connect_ex(("127.0.0.1", port)) == 0
    if busy:
        return [
            Finding(
                WARN,
                f"端口 {port} 已被占用",
                "",
                "如果本机已经开着一个「小马AI工坊」，那这就是正常的（直接开浏览器访问即可）；"
                "否则请先关掉占用该端口的程序再启动。",
            )
        ]
    return [Finding(OK, f"端口 {port} 空闲")]


def check_data_dir(root: Path) -> list[Finding]:
    """数据落在 backend/data（Docker 里是 /app/data，由 compose 的卷挂到 ./data）。"""
    data = root / "backend" / "data"
    if not data.exists():
        return [Finding(OK, "backend/data 目录待首次启动时创建")]
    db = data / "xiaoma.db"
    media = data / "storage"
    size = ""
    if media.exists():
        try:
            total = sum(f.stat().st_size for f in media.rglob("*") if f.is_file())
            size = f"，媒体文件约 {total / 1024 / 1024:.1f} MB"
        except Exception:  # noqa: BLE001
            size = ""
    return [
        Finding(
            OK,
            "backend/data 已存在（历史数据会保留）",
            f"数据库：{'有' if db.exists() else '无'}{size}",
        )
    ]


SYMBOL = {OK: "[OK]  ", WARN: "[警告]", ERR: "[错误]"}


def collect_findings(
    root: Path, *, need_node: bool = True, check_port: bool = True, port: int = 8787
) -> list[Finding]:
    """跑一遍全部检查并返回结论。

    `check_port=False` 用于「应用已经在跑、从应用内部导出报告」的场景——
    那时端口必然被自己占着，报出来只是噪音。
    """
    findings: list[Finding] = []
    findings += check_interpreter(root)
    findings += check_location(root)
    findings += check_tooling(root, need_node=need_node)
    findings += check_venv_and_deps(root)
    if check_port:
        findings += check_port_free(port)
    findings += check_data_dir(root)
    return findings


def build_report_text(
    findings: list[Finding], *, heading: bool = True, footer: bool = True
) -> str:
    """把结论渲染成文本。拆出来是为了让应用侧的「导出日志」能直接复用同一份输出，
    避免命令行与界面两处各写一套、慢慢跑偏。"""
    lines: list[str] = []
    if heading:
        lines += [
            "=" * 60,
            "小马AI工坊 · 环境诊断报告",
            "（本报告不含任何 API Key / 密钥 / 数据库内容，可直接贴出发送）",
            "=" * 60,
        ]
    for f in findings:
        lines.append(f"{SYMBOL[f.level]} {f.title}")
        if f.detail:
            lines.append(f"        {f.detail}")
        if f.hint and f.level != OK:
            lines.append(f"      → {f.hint}")
    if footer:
        lines += [
            "-" * 60,
            f"系统：{platform.system()} {platform.release()} ({platform.machine()})",
            f"脚本解释器：{sys.executable}",
            f"工作目录：{Path.cwd()}",
        ]
        if heading:
            lines.append("=" * 60)
    return "\n".join(lines)


def build_issue_text(findings: list[Finding], *, extra_lines: list[str] | None = None) -> str:
    """按仓库的 Issue 模板排好版，用户改两行就能直接粘贴提交。

    为什么单独做这个：自动上报暂时不做，那「让用户手动发过来」这条路就必须好走。
    裸丢一份体检清单过去，用户还得自己组织语言；把模板骨架先填好，
    他只补「我做了什么 → 出了什么 → 期望是什么」三句即可。

    `extra_lines` 由应用侧传入（最近若干条错误日志），命令行调用时为空。
    """
    lines: list[str] = []
    lines.append("### 我做了什么")
    lines.append("")
    lines.append("（一句话描述操作步骤，例如：铺了 L2 自动链，点了运行）")
    lines.append("")
    lines.append("### 出了什么问题")
    lines.append("")
    lines.append("（贴报错原文，或描述现象。任务中心里那张卡片的「日志」按钮能看到细节）")
    lines.append("")
    lines.append("### 我期望的结果")
    lines.append("")
    lines.append("（可选）")
    lines.append("")
    lines.append("### 环境信息")
    lines.append("")
    lines.append("```text")
    lines.append(build_report_text(findings, heading=False, footer=True))
    lines.append("```")
    if extra_lines:
        lines.append("")
        lines.append("<details><summary>最近的错误与警告（已脱敏，点开查看）</summary>")
        lines.append("")
        lines.append("```text")
        lines.extend(extra_lines)
        lines.append("```")
        lines.append("")
        lines.append("</details>")
    lines.append("")
    lines.append("<!-- 由「导出日志」自动生成：只含环境信息与检查结论，")
    lines.append("     不含提示词、作品正文、图片或接口密钥。 -->")
    return "\n".join(lines)


def render(findings: list[Finding], *, report: bool) -> None:
    print(build_report_text(findings, heading=report, footer=report))


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdout()
    parser = argparse.ArgumentParser(description="小马AI工坊环境体检")
    parser.add_argument("--report", action="store_true", help="输出可整段复制发送的诊断报告")
    parser.add_argument(
        "--issue",
        action="store_true",
        help="输出按 Issue 模板排好版的内容（改两句即可粘贴提交）",
    )
    parser.add_argument("--root", default="", help="仓库根目录（默认按脚本位置推断）")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--skip-node", action="store_true", help="跳过 Node.js / npm 检查")
    parser.add_argument(
        "--deps-only",
        action="store_true",
        help="只报当前解释器缺哪些后端依赖，供外层脚本换解释器复查",
    )
    parser.add_argument(
        "--hint",
        default="",
        choices=["", *HINTS],
        help="打印某个失败场景的中文解释（供启动脚本在失败时调用）",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[2]

    if args.hint:
        for line in HINTS[args.hint]:
            print(line)
        print()
        print("完整诊断（可整段发给作者）：")
        print(f"    python \"{Path(__file__).resolve()}\" --report")
        return 0

    if args.deps_only:
        missing = [s for s in RUNTIME_DEPS if importlib.util.find_spec(module_for_spec(s)) is None]
        if missing:
            # 单行输出：调用方只取第一行
            print("、".join(missing))
            return 1
        return 0

    findings: list[Finding] = collect_findings(
        root, need_node=not args.skip_node, port=args.port
    )

    if args.issue:
        # 只输出那段可直接粘贴的正文，不要夹带其它内容——
        # 这段是给用户整段复制的，多一行都得他自己删
        print(build_issue_text(findings))
        return 0

    render(findings, report=args.report)

    errors = [f for f in findings if f.level == ERR]
    warns = [f for f in findings if f.level == WARN]
    print()
    print(f"结论：{len(errors)} 个错误，{len(warns)} 个提醒。")
    if errors:
        print("请先按上面的「→」提示处理错误项，再重新运行启动脚本。")
        if not args.report:
            print("如果看不懂，请运行下面这条，把输出整段发回给作者：")
            print(f"    {sys.executable} backend/tools/env_report.py --report")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
