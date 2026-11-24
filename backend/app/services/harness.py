"""DeepSeek Harness（dsh）sidecar 的可选组件：下载 / 校验 / 安装 / 启动 / 停止。

边界与设计（见 `docs/harness-sidecar.md`）：

- 它是**可选组件**：没装、没启动时，原 Agent 与画布完全不受影响；
- 不把 dsh 写进主包：Node 运行时与 dsh 都装在数据目录，随用随下；
- 凭据只进子进程环境（`DEEPSEEK_API_KEY`），不落 dsh 的 profile 文件；
- dsh 只通过 HTTP MCP（`/api/mcp`）访问画布，不碰 `backend/data`。

安装是一次性的慢操作（下载 Node 几十 MB + npm 安装几十个包），所以用后台任务 +
状态轮询，不在请求里阻塞。状态只在内存里，进程重启后会重新探测。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import security
from app.config import settings
from app.models import ProviderService

DSH_VERSION = "0.2.2-rc.1"
DSH_PACKAGE = f"@deepseek-ai/dsh@{DSH_VERSION}"
NODE_VERSION = "v24.18.1"
NODE_MIN = (24, 9)
DEFAULT_PORT = 3081

NODE_ZIP = f"https://nodejs.org/dist/{NODE_VERSION}/node-{NODE_VERSION}-win-x64.zip"
SHASUMS = f"https://nodejs.org/dist/{NODE_VERSION}/SHASUMS256.txt"


def root() -> Path:
    p = settings.data_dir / "harness"
    p.mkdir(parents=True, exist_ok=True)
    return p


def home() -> Path:
    p = root() / "home"
    p.mkdir(parents=True, exist_ok=True)
    return p


def node_dir() -> Path:
    return root() / "node"


def dsh_dir() -> Path:
    return root() / "dsh"


def dsh_bin() -> Path:
    return dsh_dir() / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"


def log_path() -> Path:
    return root() / "dsh.log"


def pid_path() -> Path:
    return root() / "dsh.pid"


def settings_path() -> Path:
    return root() / "settings.json"


def repo_root() -> Path:
    """本仓库根目录：工作区绝不能选它（dsh 能写文件，写到这里等于改产品自己）。"""
    return Path(__file__).resolve().parents[3]


def get_workspace() -> str:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        return str(data.get("workspace") or "")
    except (OSError, ValueError):
        return ""


def set_workspace(path: str) -> str:
    target = validate_workspace(path)
    data = {}
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    data["workspace"] = str(target)
    settings_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


def validate_workspace(path: str) -> Path:
    """工作区必须是用户显式选的、真实存在的普通目录，且不能是仓库/数据目录。

    这不是「防君子」的路径白名单：dsh 的文件与 Shell 能力是真的能写盘的，
    工作区就是它唯一的写入边界，所以边界本身必须是干净的目标目录。
    """
    raw = (path or "").strip().strip('"')
    if not raw:
        raise ValueError("还没有选择工作区。")
    target = Path(raw).expanduser()
    if not target.is_absolute():
        raise ValueError("工作区要填绝对路径。")
    try:
        target = target.resolve(strict=True)
    except OSError:
        raise ValueError("这个目录不存在。") from None
    if not target.is_dir():
        raise ValueError("工作区必须是一个目录。")
    if target.parent == target:
        raise ValueError("不能把整个磁盘当工作区。")

    data = settings.data_dir.resolve()
    repo = repo_root().resolve()
    if target == repo or repo.is_relative_to(target):
        raise ValueError("工作区不能是仓库根目录，也不能是包含它的上级目录。")
    if target == data or data.is_relative_to(target):
        raise ValueError("工作区不能是数据目录，也不能是包含它的上级目录。")
    return target


def list_dirs(path: str) -> dict:
    """给工作区选择器用的目录浏览：只列子目录，不列文件、不读内容。

    空路径 = 从头开始（Windows 列盘符，其它平台列 `/`）。
    这是只读操作；能不能选来当工作区仍由 `validate_workspace` 说了算。
    """
    raw = (path or "").strip().strip('"')
    roots_requested = not raw
    if roots_requested:
        if os.name == "nt":
            entries = [
                {"name": f"{ch}:", "path": f"{ch}:\\"}
                for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                if Path(f"{ch}:\\").exists()
            ]
            return {"path": "", "parent": "", "entries": entries, "roots": True}
        raw = "/"

    target = Path(raw).expanduser()
    if not target.is_absolute():
        raise ValueError("请填绝对路径。")
    try:
        target = target.resolve(strict=True)
    except OSError:
        raise ValueError("这个目录不存在。") from None
    if not target.is_dir():
        raise ValueError("这不是一个目录。")

    entries: list[dict] = []
    try:
        with os.scandir(target) as it:
            for item in it:
                if len(entries) >= 500:
                    break
                try:
                    if item.is_dir(follow_symlinks=False) and not item.name.startswith("."):
                        entries.append({"name": item.name, "path": str(Path(item.path))})
                except OSError:
                    continue
    except PermissionError:
        raise ValueError("没有权限读取这个目录。") from None
    entries.sort(key=lambda e: e["name"].lower())
    parent = str(target.parent) if target.parent != target else ""
    return {
        "path": str(target),
        "parent": parent,
        "entries": entries,
        "roots": roots_requested,
    }


# 后台安装任务的状态（单进程；UI 轮询它）
_job: dict[str, str] = {"state": "idle", "detail": "", "error": ""}
_proc: asyncio.subprocess.Process | None = None
_log_file = None


def _set(state: str, detail: str = "", error: str = "") -> None:
    _job.update({"state": state, "detail": detail, "error": error})


def _parse_version(text: str) -> tuple[int, ...]:
    digits = "".join(ch if ch.isdigit() else "." for ch in (text or ""))
    return tuple(int(x) for x in digits.split(".") if x.isdigit())


async def _node_version(exe: Path) -> tuple[int, ...]:
    try:
        proc = await asyncio.create_subprocess_exec(
            str(exe), "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return _parse_version(out.decode(errors="ignore").strip())
    except (OSError, asyncio.TimeoutError):
        return ()


async def find_node() -> tuple[Path | None, str]:
    """优先用随组件下载的 Node；没有再看系统 PATH。返回 (可执行文件, 版本文本)。"""
    candidates: list[tuple[Path, str]] = []
    local = node_dir() / ("node.exe" if os.name == "nt" else "bin/node")
    if local.exists():
        candidates.append((local, "组件内置"))
    system = shutil.which("node")
    if system:
        candidates.append((Path(system), "系统安装"))
    for exe, source in candidates:
        version = await _node_version(exe)
        if version and version >= NODE_MIN:
            return exe, f"{source} · v{'.'.join(map(str, version))}"
    return None, ""


def _npm_for(node: Path) -> str:
    """npm 跟着 Node 走：内置 Node 用同目录的 npm.cmd，系统 Node 用 PATH 里的 npm。"""
    local = node.parent / ("npm.cmd" if os.name == "nt" else "npm")
    if Path(local).exists():
        return str(local)
    return shutil.which("npm") or "npm"


def dsh_version() -> str:
    pkg = dsh_dir() / "node_modules" / "@deepseek-ai" / "dsh" / "package.json"
    try:
        return str(json.loads(pkg.read_text(encoding="utf-8")).get("version") or "")
    except (OSError, ValueError):
        return ""


def _tail(lines: int = 40) -> str:
    try:
        text = log_path().read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def profile_patch() -> str:
    """写到 profile 的用户覆盖层里：MCP 必须写在 `- insert:` 下，顶层覆盖会静默失效。"""
    url = f"http://127.0.0.1:{settings.PORT}/api/mcp"
    return (
        "- insert:\n"
        "    - id: xms\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config:\n"
        "        serverName: xms\n"
        "        transport: streamable-http\n"
        f"        url: {url}\n"
        "        toolCallTimeoutMs: 120000\n"
    )


def _write_profile_patch() -> None:
    patch = home() / "profiles" / "web" / "cordis.patch.yml"
    patch.parent.mkdir(parents=True, exist_ok=True)
    current = patch.read_text(encoding="utf-8") if patch.exists() else ""
    if "serverName: xms" in current:
        return
    patch.write_text((current.rstrip() + "\n" if current.strip() else "") + profile_patch(),
                     encoding="utf-8")


async def _run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None,
               timeout: float = 900) -> tuple[int, str]:
    # Windows 上 npm 是 .cmd，subprocess 直接 exec 会报 WinError 193，必须过 cmd /c
    if os.name == "nt" and cmd and cmd[0].lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c", *cmd]
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd) if cwd else None, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "命令超时"
    return int(proc.returncode or 0), out.decode(errors="ignore")


async def _download_node() -> None:
    if os.name != "nt":
        raise RuntimeError("自动下载 Node 目前只支持 Windows；其它平台请先安装 Node 24.9+。")
    _set("downloading", f"下载 Node {NODE_VERSION}…")
    async with httpx.AsyncClient(follow_redirects=True, timeout=60 * 10) as client:
        sums = (await client.get(SHASUMS)).text
        digest = ""
        name = Path(NODE_ZIP).name
        for line in sums.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == name:
                digest = parts[0].lower()
                break
        if not digest:
            raise RuntimeError("校验清单里没有这个 Node 包，已停止安装。")

        tmp = root() / "node.zip"
        hasher = hashlib.sha256()
        with tmp.open("wb") as fh:
            async with client.stream("GET", NODE_ZIP) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes(1024 * 256):
                    fh.write(chunk)
                    hasher.update(chunk)
        if hasher.hexdigest() != digest:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("Node 包校验失败（sha256 不一致），已删除下载文件。")

        _set("installing", "解压 Node…")
        target = node_dir()
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        with zipfile.ZipFile(tmp) as zf:
            for member in zf.infolist():
                # 去掉 zip 里的顶层目录（node-vXX-win-x64/）
                rel = Path(member.filename).parts[1:]
                if not rel:
                    continue
                dest = target.joinpath(*rel)
                if member.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, dest.open("wb") as out:
                        shutil.copyfileobj(src, out)
        tmp.unlink(missing_ok=True)


async def _install_dsh(node: Path) -> None:
    npm = _npm_for(node)
    env = dict(os.environ)
    env["PATH"] = f"{node.parent}{os.pathsep}{env.get('PATH', '')}"
    _set("installing", f"安装 {DSH_PACKAGE}（第一次比较慢）…")
    code, out = await _run(
        [npm, "install", "--prefix", str(dsh_dir()), DSH_PACKAGE,
         "--no-audit", "--no-fund", "--loglevel=error"],
        env=env, timeout=1800,
    )
    if code != 0 or not dsh_bin().exists():
        raise RuntimeError(f"安装 dsh 失败：{out.strip()[-600:] or code}")
    _set("ready", f"已安装 dsh {dsh_version()}")


async def _install() -> None:
    try:
        if dsh_bin().exists():
            _write_profile_patch()
            _set("ready", f"已安装 dsh {dsh_version()}，可以直接启动。")
            return
        node, _ = await find_node()
        if node is None:
            await _download_node()
            node, _ = await find_node()
            if node is None:
                raise RuntimeError("Node 解压后仍然不可用。")
        await _install_dsh(node)
        _write_profile_patch()
    except Exception as e:  # noqa: BLE001  失败原因要给到界面
        _set("error", "安装失败", f"{type(e).__name__}: {e}")


def start_install() -> dict:
    if _job["state"] in ("downloading", "installing"):
        return dict(_job)
    _set("checking", "检查 Node 与 dsh…")
    asyncio.create_task(_install())
    return dict(_job)


# ============================================================ 启动 / 停止


async def _probe(url: str, timeout: float = 1.5) -> bool:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, follow_redirects=True)
            return r.status_code < 500
    except Exception:  # noqa: BLE001
        return False


def _url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def _web_url(port: int) -> str:
    """dsh 启动后会在日志里打印带 token 的地址；没有 token 的根路径会 401。"""
    pattern = re.compile(rf"https?://127\.0\.0\.1:{port}/\?token=[^\s]+")
    for line in reversed(_tail(200).splitlines()):
        m = pattern.search(line)
        if m:
            return m.group(0)
    return _url(port)


async def deepseek_key(db: AsyncSession) -> str:
    """挑一个已启用的 DeepSeek 官方服务；没有就如实报错，不猜别的 Key。"""
    rows = (await db.execute(
        select(ProviderService).where(ProviderService.enabled.is_(True))
    )).scalars().all()
    for row in rows:
        if "api.deepseek.com" in (row.base_url or ""):
            key = security.decrypt(row.api_key_enc)
            if key:
                return key
    return ""


async def _init_profile(node: Path) -> None:
    profile = home() / "profiles" / "web"
    if profile.exists():
        return
    env = dict(os.environ)
    env["DSH_HOME"] = str(home())
    await _run([str(node), str(dsh_bin()), "--profile", "web", "--dump-config"],
               env=env, timeout=180)


async def start(db: AsyncSession, port: int = DEFAULT_PORT) -> dict:
    global _proc, _log_file
    node, _ = await find_node()
    if node is None:
        return {"ok": False, "error": "还没有可用的 Node 24.9+，先在下面点「下载并安装」。"}
    if not dsh_bin().exists():
        return {"ok": False, "error": "dsh 还没安装，先在下面点「下载并安装」。"}
    key = await deepseek_key(db)
    if not key:
        return {
            "ok": False,
            "error": "没有找到可用的 DeepSeek 官方 Key。",
            "hint": "到「模型服务」接入 DeepSeek 官方服务（Base URL 含 api.deepseek.com）后再启动。",
        }
    if await _probe(_url(port)):
        return {"ok": True, "url": _web_url(port), "detail": "已经在跑。"}

    try:
        workspace = validate_workspace(get_workspace())
    except ValueError as e:
        return {
            "ok": False,
            "error": f"工作区不可用：{e}",
            "hint": "先在上面选一个普通文件夹（不能是仓库根或数据目录）。",
        }

    await _init_profile(node)
    _write_profile_patch()

    env = dict(os.environ)
    env["DSH_HOME"] = str(home())
    env["DEEPSEEK_API_KEY"] = key
    env.setdefault("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    # 强制最保守的一档：只允许写工作区，Shell 命令逐条问人。
    # 不读用户环境里的设置，避免「上次试过全权限」在这次被悄悄继承。
    env["DSH_PERMISSION_MODE"] = "workspace-write"
    env["PATH"] = f"{node.parent}{os.pathsep}{env.get('PATH', '')}"

    log = log_path().open("ab")
    try:
        _proc = await asyncio.create_subprocess_exec(
            str(node), str(dsh_bin()), "--profile", "web",
            "--host", "127.0.0.1", "--port", str(port), "--no-open",
            cwd=str(workspace), env=env,
            stdout=log, stderr=asyncio.subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except OSError as e:
        log.close()
        return {"ok": False, "error": f"启动失败：{e}"}
    _log_file = log
    pid_path().write_text(str(_proc.pid), encoding="utf-8")

    for _ in range(60):
        if _proc.returncode is not None:
            break
        if await _probe(_url(port)):
            return {"ok": True, "url": _web_url(port), "detail": "已启动。"}
        await asyncio.sleep(1)
    detail = _tail(30)
    return {
        "ok": False,
        "error": "dsh 没有在 60 秒内就绪。",
        "hint": detail[-800:] or "看看数据目录下 harness/dsh.log。",
    }


async def stop() -> dict:
    global _proc, _log_file
    pid = None
    if _proc is not None:
        pid = _proc.pid
    elif pid_path().exists():
        try:
            pid = int(pid_path().read_text(encoding="utf-8").strip())
        except ValueError:
            pid = None
    if pid:
        if os.name == "nt":
            await _run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=20)
        else:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
    _proc = None
    if _log_file is not None:
        try:
            _log_file.close()
        except OSError:
            pass
        _log_file = None
    pid_path().unlink(missing_ok=True)
    return {"ok": True}


async def status(db: AsyncSession, port: int = DEFAULT_PORT) -> dict:
    node, node_text = await find_node()
    running = await _probe(_url(port))
    saved = get_workspace()
    try:
        validate_workspace(saved)
        ws_ready, ws_reason = True, ""
    except ValueError as e:
        ws_ready, ws_reason = False, (str(e) if saved else "还没有选择工作区")
    return {
        "job": dict(_job),
        "node": {"ready": node is not None, "text": node_text},
        "dsh": {"installed": dsh_bin().exists(), "version": dsh_version(),
                "pinned": DSH_VERSION},
        "workspace": {"path": saved, "ready": ws_ready, "reason": ws_reason},
        "permission": "只写工作区 · Shell 命令逐条确认",
        "running": running,
        # 端口上可能是别处起的 dsh（比如手动装的）：只有我们写过 pid 才认为可停
        "owned": running and pid_path().exists(),
        "url": _web_url(port) if running else "",
        "modelReady": bool(await deepseek_key(db)),
        "log": _tail(30),
        "home": str(home()),
    }
