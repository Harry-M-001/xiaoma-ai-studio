"""桌面版启动器：复用现有便携包布局，用 WebView2 开一个原生窗口。

它不重写任何业务代码：找到 `backend/` 与 Python 运行时 → 起 uvicorn（127.0.0.1）→
等 `/api/health` → 把窗口指到本地地址。关窗即停后端。

用法（源码运行）：
    backend\\venv\\Scripts\\python.exe desktop\\launcher.py

参数：
    --port 8787        固定端口（默认自动挑空闲端口）
    --data-dir PATH    数据目录（默认 %APPDATA%\\XiaomaAIStudio\\data）
    --portable         数据放在程序目录的 backend\\data（便携包行为）
    --no-window        只起后端，不开窗口（调试 / 冒烟）
    --debug            打开 WebView 调试与后端日志窗口

打包成桌面版时，把本文件用 PyInstaller 打成 exe 放进便携包根目录即可：
exe 旁边有 `runtime\\python.exe` 与 `backend\\` 就按便携布局运行，否则按源码布局。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_TITLE = "小马AI工坊"
MUTEX_NAME = "XiaomaAIStudio.Desktop"
HEALTH_PATH = "/api/health"
START_TIMEOUT = 60.0

SPLASH_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{height:100%;margin:0;background:#0f1115;color:#e6e6ef;
font-family:"Segoe UI","Microsoft YaHei",sans-serif;display:flex;
align-items:center;justify-content:center;flex-direction:column;gap:18px}
.logo{width:56px;height:56px;border-radius:16px;background:linear-gradient(135deg,#7c3aed,#4f46e5);
display:flex;align-items:center;justify-content:center;font-size:26px;font-weight:700}
.dot{width:8px;height:8px;border-radius:50%;background:#7c3aed;animation:p 1s infinite alternate}
.row{display:flex;gap:8px}.row .dot:nth-child(2){animation-delay:.2s}.row .dot:nth-child(3){animation-delay:.4s}
@keyframes p{from{opacity:.25;transform:translateY(2px)}to{opacity:1;transform:translateY(-2px)}}
small{color:#8b8b9e}
</style></head><body>
<div class="logo">马</div><div style="font-size:16px">正在启动小马AI工坊…</div>
<div class="row"><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>
<small>首次启动会初始化本地数据库，通常几秒钟</small>
</body></html>"""


# --------------------------------------------------------------------------
# 纯函数（便于单测）
# --------------------------------------------------------------------------


def find_free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def resolve_layout() -> tuple[Path, Path | None]:
    """返回 (仓库根或程序目录, Python 解释器路径或 None=用当前解释器)。

    - 打包后（PyInstaller）：exe 旁边的 `runtime/python.exe` + `backend/`
    - 源码运行：仓库根的 `backend/`，解释器用当前 venv
    """
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
        runtime_py = base / "runtime" / "python.exe"
        return base, (runtime_py if runtime_py.is_file() else None)
    base = Path(__file__).resolve().parents[1]
    # 源码运行时优先用项目 venv：它既有后端依赖，也有桌面窗口依赖 pywebview
    venv_py = base / "backend" / "venv" / "Scripts" / "python.exe"
    return base, (venv_py if venv_py.is_file() else None)


def resolve_data_dir(base: Path, *, portable: bool, override: str = "") -> Path:
    if override:
        return Path(override).expanduser().resolve()
    if portable:
        return base / "backend" / "data"
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "XiaomaAIStudio" / "data"


def health_url(port: int) -> str:
    return f"http://127.0.0.1:{port}{HEALTH_PATH}"


def wait_ready(port: int, proc: subprocess.Popen, timeout: float = START_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    url = health_url(port)
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.25)
    return False


# --------------------------------------------------------------------------
# 单实例
# --------------------------------------------------------------------------


def acquire_single_instance() -> bool:
    """拿不到互斥量说明已经有一个实例；顺手把它带到前台。"""
    if sys.platform != "win32":
        return True
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return True
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, APP_TITLE)
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
        else:
            user32.MessageBoxW(None, "小马AI工坊已经在运行。", APP_TITLE, 0x40)
        return False
    # 句柄故意不关闭：进程活着期间保持互斥量
    return True


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def start_backend(base: Path, python: str, data_dir: Path, port: int,
                  debug: bool) -> tuple[subprocess.Popen, Path]:
    backend_dir = base / "backend"
    if not (backend_dir / "app" / "main.py").is_file():
        raise SystemExit(f"找不到后端目录：{backend_dir}")
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "desktop.log"

    env = os.environ.copy()
    env["DATA_DIR"] = str(data_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    env["XIAOMA_DESKTOP"] = "1"

    cmd = [
        python, "-m", "uvicorn", "app.main:app",
        "--host", "127.0.0.1", "--port", str(port),
    ]
    creation = 0
    if sys.platform == "win32" and not debug:
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log = log_path.open("a", encoding="utf-8")
    log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} desktop start ---\n")
    log.flush()
    proc = subprocess.Popen(
        cmd, cwd=str(backend_dir), env=env,
        stdout=log, stderr=subprocess.STDOUT,
        creationflags=creation,
    )
    return proc, log_path


def stop_backend(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_window(url: str, splash_ok: bool, error_text: str = "") -> None:
    try:
        import webview  # 延迟导入：单测与 --no-window 都不需要窗口组件
    except ModuleNotFoundError as e:
        raise SystemExit(
            "缺少桌面窗口依赖 pywebview。请用项目 venv 运行："
            r"backend\venv\Scripts\python.exe desktop\launcher.py"
        ) from e

    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    window = webview.create_window(
        APP_TITLE,
        html=SPLASH_HTML,
        width=1440,
        height=900,
        min_size=(1100, 700),
        background_color="#0f1115",
    )

    def boot() -> None:
        if splash_ok:
            window.load_url(url)
        else:
            window.load_html(
                "<html><body style='background:#0f1115;color:#e6e6ef;"
                "font-family:Segoe UI,Microsoft YaHei;padding:40px'>"
                f"<h3>后端启动失败</h3><pre>{error_text}</pre>"
                "<p>日志：数据目录下的 logs/desktop.log</p></body></html>"
            )

    webview.start(boot)


def main() -> None:
    parser = argparse.ArgumentParser(description="小马AI工坊桌面版")
    parser.add_argument("--port", type=int, default=0, help="固定端口（默认自动）")
    parser.add_argument("--data-dir", default="", help="数据目录")
    parser.add_argument("--portable", action="store_true", help="数据放在程序目录（便携行为）")
    parser.add_argument("--no-window", action="store_true", help="只起后端，不开窗口")
    parser.add_argument("--debug", action="store_true", help="调试模式")
    args = parser.parse_args()

    if not args.no_window and not acquire_single_instance():
        return

    base, runtime_py = resolve_layout()
    python = str(runtime_py or sys.executable)
    data_dir = resolve_data_dir(base, portable=args.portable, override=args.data_dir)
    port = args.port or find_free_port()
    url = f"http://127.0.0.1:{port}/"

    proc, log_path = start_backend(base, python, data_dir, port, args.debug)
    ok = wait_ready(port, proc)
    try:
        if args.no_window:
            if not ok:
                raise SystemExit(f"后端没起来，日志：{log_path}")
            print(f"后端已就绪：{url}")
            print("按 Ctrl+C 停止")
            proc.wait()
            return
        if ok:
            print(f"{APP_TITLE} 已启动：{url}（数据目录 {data_dir}）")
        run_window(url, ok, f"60 秒内没有等到 {health_url(port)}\n日志：{log_path}")
    finally:
        stop_backend(proc)


if __name__ == "__main__":
    main()
