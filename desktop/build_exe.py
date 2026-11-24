"""把桌面启动器打包成 Windows 桌面版（复用便携包的运行时与后端）。

前置：
    backend\\venv\\Scripts\\python.exe -m pip install pywebview pyinstaller

用法：
    backend\\venv\\Scripts\\python.exe desktop\\build_exe.py
    backend\\venv\\Scripts\\python.exe desktop\\build_exe.py --skip-portable   # 复用已打好的便携包

产物：
    dist\\desktop\\app\\XiaomaAIStudio.exe      可直接双击
    dist\\xiaoma-ai-studio-v<版本>-win64-desktop.zip

它不装安装器：装到 `%LOCALAPPDATA%` 的 NSIS/Inno 包是下一步。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
VENV_PY = BACKEND / "venv" / "Scripts" / "python.exe"
PORTABLE = ROOT / "dist" / "portable"
DESKTOP = ROOT / "dist" / "desktop"
APP = DESKTOP / "app"
STAGING = DESKTOP / "_build"
LAUNCHER = ROOT / "desktop" / "launcher.py"


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd or ROOT), check=True)


def read_version() -> str:
    text = (BACKEND / "app" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split('"')[1]
    raise SystemExit("读不到版本号")


def build_portable() -> None:
    run([str(VENV_PY), str(BACKEND / "tools" / "make_portable.py"), "--skip-deps"], cwd=BACKEND)


def copy_runtime() -> None:
    """把便携包里的运行时 / 后端 / 前端拷到桌面目录，排除用户数据。"""
    if APP.exists():
        shutil.rmtree(APP)
    APP.mkdir(parents=True)

    def ignore(_dir: str, names: list[str]) -> set[str]:
        skip = {"__pycache__"}
        return {n for n in names if n in skip or n.endswith((".pyc", ".pyo"))}

    for name in ("runtime", "backend", "frontend", "docs", "assets"):
        src = PORTABLE / name
        if not src.exists():
            continue
        dst = APP / name
        shutil.copytree(src, dst, ignore=ignore)
    # 用户数据不进桌面包：桌面版默认用 %APPDATA%，首次启动会自己建库
    data_dir = APP / "backend" / "data"
    if data_dir.exists():
        shutil.rmtree(data_dir)

    for name in ("README.md", "LICENSE", "CHANGELOG.md"):
        src = PORTABLE / name
        if src.is_file():
            shutil.copy2(src, APP / name)


def build_launcher(console: bool) -> Path:
    if STAGING.exists():
        shutil.rmtree(STAGING)
    STAGING.mkdir(parents=True)
    cmd = [
        str(VENV_PY), "-m", "PyInstaller", str(LAUNCHER),
        "--noconfirm", "--clean",
        "--name", "XiaomaAIStudio",
        "--onedir",
        "--contents-directory", "_internal",
        "--distpath", str(STAGING),
        "--workpath", str(STAGING / "work"),
        "--specpath", str(STAGING),
        # pywebview 的 EdgeChromium 后端是按需导入的，PyInstaller 扫不到
        "--collect-submodules", "webview",
        "--hidden-import", "webview.platforms.edgechromium",
        "--hidden-import", "webview.platforms.winforms",
        "--collect-all", "clr_loader",
        "--collect-all", "pythonnet",
        # 启动器只负责起服务 + 开窗口，后端那些重依赖由 runtime/ 里的解释器自己用。
        # 不排除的话 PyInstaller 会把 numpy / Pillow / cryptography 一起打进 _internal，
        # 桌面包凭空多出 20 MB（Gitee 单附件上限 100 MB，v2.2.5 就卡在这里）。
        "--exclude-module", "numpy",
        "--exclude-module", "PIL",
        "--exclude-module", "cryptography",
    ]
    cmd.append("--console" if console else "--windowed")
    run(cmd)
    out = STAGING / "XiaomaAIStudio"
    if not (out / "XiaomaAIStudio.exe").is_file():
        raise SystemExit(f"没找到打包产物：{out}")
    # exe 必须和 runtime/ 同级，启动器才认得出便携布局
    shutil.move(str(out / "XiaomaAIStudio.exe"), str(APP / "XiaomaAIStudio.exe"))
    internal = out / "_internal"
    if internal.exists():
        shutil.move(str(internal), str(APP / "_internal"))
    shutil.rmtree(STAGING, ignore_errors=True)
    return APP / "XiaomaAIStudio.exe"


def make_zip(version: str) -> Path:
    zip_path = ROOT / "dist" / f"xiaoma-ai-studio-v{version}-win64-desktop.zip"
    if zip_path.exists():
        zip_path.unlink()
    print(f"压缩 {APP} -> {zip_path.name}")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in APP.rglob("*"):
            if path.is_file():
                zf.write(path, Path(APP.name) / path.relative_to(APP))
    print(f"完成：{zip_path}（{zip_path.stat().st_size / 1048576:.1f} MB）")
    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser(description="打包桌面版")
    parser.add_argument("--skip-portable", action="store_true", help="复用 dist/portable")
    parser.add_argument("--console", action="store_true", help="保留控制台窗口（调试用）")
    parser.add_argument("--skip-zip", action="store_true")
    args = parser.parse_args()

    if not VENV_PY.is_file():
        raise SystemExit(f"找不到 venv 解释器：{VENV_PY}")
    if not args.skip_portable:
        build_portable()
    if not PORTABLE.exists():
        raise SystemExit("dist/portable 不存在，先跑 make_portable.py 或去掉 --skip-portable")

    copy_runtime()
    exe = build_launcher(args.console)
    print(f"桌面启动器：{exe}")
    version = read_version()
    if not args.skip_zip:
        make_zip(version)


if __name__ == "__main__":
    main()
