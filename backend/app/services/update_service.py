"""版本信息与在线更新。

三种安装方式，能力不同（不假装能做事）：

| 安装方式 | 判定依据 | 能做什么 |
|---|---|---|
| git 克隆 | 存在 `.git` 目录且 git 命令可用 | **一键更新**：git pull → 依赖变了才装依赖 → 前端变了才重建 |
| Docker | 容器内（`/.dockerenv` 或 `RUNNING_IN_DOCKER`） | 只提示新版本 + 给出 compose 命令（容器不该改自己） |
| 压缩包解压 | 以上都不是 | 只提示新版本，引导手动下载覆盖 |

更新源与仓库地址都来自配置表（`update.source` / `update.repo`），
所以换仓库、切 Gitee 都不需要改代码。

安全边界（更新是「改用户自己机器」的操作，宁可少做也不要做错）：
- 工作区有未提交改动 → 拒绝自动更新，提示先 commit / stash；
- 只走 `git pull --ff-only`，不做 merge、不做 force；
- 不自动重启进程（杀掉自己的父进程不可靠），改完让用户手动重启。
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app import __version__
from app.config import settings
from app.services.config_center_service import runtime_value

logger = logging.getLogger("xiaoma.update")

# 仓库根目录：services/ → app/ → backend/ → 仓库根
REPO_ROOT = Path(__file__).resolve().parents[3]

_CHECK_TTL = 600  # 检查结果缓存 10 分钟，避免频繁打上游 API
_CMD_TIMEOUT = 600  # 单条命令超时（npm build 可能几十秒到几分钟）

# 内置常见平台的 API 模板；update.source 也可以直接填完整 URL 前缀
_SOURCES: dict[str, str] = {
    "github": "https://api.github.com/repos/{repo}/releases/latest",
    "gitee": "https://gitee.com/api/v5/repos/{repo}/releases/latest",
}

# 人类可读的 Release 页面地址。API 里的 html_url 并非每个平台都给：
# 实测 Gitee 的 /releases/latest 返回的 html_url 恒为 null，只能自己拼，
# 否则界面上「查看 Release 说明」的链接在有新版本时会凭空消失。
_SOURCE_PAGES: dict[str, str] = {
    "github": "https://github.com/{repo}/releases/tag/{tag}",
    "gitee": "https://gitee.com/{repo}/releases/tag/{tag}",
}

_cache: dict[str, Any] = {}


# ============================================================
# 当前版本 / 安装方式
# ============================================================


def _read_git_head() -> tuple[str, str]:
    """直接读 .git 目录取分支与短 commit（不依赖 git 命令，压缩包用户也能跑）。"""
    git_dir = REPO_ROOT / ".git"
    head_file = git_dir / "HEAD"
    if not head_file.is_file():
        return "", ""
    try:
        head = head_file.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "", ""
    if head.startswith("ref:"):
        ref = head[4:].strip()
        branch = ref.rsplit("/", 1)[-1]
        ref_file = git_dir / ref
        if ref_file.is_file():
            try:
                return branch, ref_file.read_text(encoding="utf-8").strip()[:7]
            except OSError:
                return branch, ""
        # 打包过的仓库会把 refs 收进 packed-refs
        packed = git_dir / "packed-refs"
        if packed.is_file():
            try:
                for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.endswith(f" {ref}"):
                        return branch, line.split(" ", 1)[0][:7]
            except OSError:
                pass
        return branch, ""
    return "detached", head[:7]


def install_kind() -> str:
    """git | docker | archive"""
    if Path("/.dockerenv").exists() or runtime_value("update.in_docker", False):
        return "docker"
    if (REPO_ROOT / ".git").exists() and shutil.which("git"):
        return "git"
    return "archive"


def git_available() -> bool:
    return shutil.which("git") is not None


async def _run_cmd(args: list[str], cwd: Path, timeout: int = _CMD_TIMEOUT) -> tuple[int, str]:
    """跑一条命令，返回 (exit_code, 输出尾部)。

    注意：**不能**用 `stderr=STDOUT` —— 在 Windows 上若进程没有可用控制台
    （stdout 被重定向、以服务方式启动等），转发父进程 stdout 句柄会抛
    `OSError: Bad file descriptor`。统一用 PIPE 再在 Python 侧合并最稳。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, NotImplementedError, OSError) as e:
        return 127, f"无法执行 {' '.join(args)}：{e}"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        return 124, f"命令超时（{timeout}s）：{' '.join(args)}"
    text = b"\n".join(b for b in (out, err) if b)
    return proc.returncode or 0, text.decode("utf-8", "replace")[-4000:]


async def local_state() -> dict[str, Any]:
    """当前版本与本地仓库状态。"""
    branch, commit = _read_git_head()
    kind = install_kind()
    dirty: bool | None = None
    if kind == "git":
        # 取不到状态不算错，只是「未知」，不能让状态接口整体失败
        try:
            code, out = await _run_cmd(["git", "status", "--porcelain"], REPO_ROOT, timeout=30)
            dirty = None if code != 0 else bool(out.strip())
        except Exception:  # noqa: BLE001
            logger.warning("读取 git 状态失败，按未知处理", exc_info=True)
            dirty = None
    return {
        "version": __version__,
        "commit": commit,
        "branch": branch,
        "installKind": kind,
        "dirty": dirty,
        "repoRoot": str(REPO_ROOT),
        "gitAvailable": git_available(),
    }


# ============================================================
# 检查更新
# ============================================================


def _parse_version(text: str) -> tuple[int, ...]:
    m = re.match(r"v?(\d+(?:\.\d+)*)", (text or "").strip())
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


def _is_newer(latest: str, current: str) -> bool:
    a, b = _parse_version(latest), _parse_version(current)
    if not a:
        return False
    return a > b


def _release_url(source: str, repo: str) -> str:
    template = _SOURCES.get(source)
    if template:
        return template.format(repo=repo)
    # 自定义源：直接当成完整网址前缀用
    return f"{source.rstrip('/')}/{repo}/releases/latest"


def _release_page_url(source: str, repo: str, tag: str, given: str = "") -> str:
    """Release 页面地址：API 给就用 API 的，没给就按平台规则拼一个。"""
    if given:
        return given
    template = _SOURCE_PAGES.get(source)
    if not template or not repo or not tag:
        return ""
    return template.format(repo=repo, tag=tag)


async def _probe(url: str, *, direct: bool) -> tuple[int, dict | None, str]:
    """发一次 GET，返回 (状态码, JSON 或 None, 错误说明)。

    `direct=True` 表示**忽略系统代理**。这一点必须显式控制：httpx 默认
    `trust_env=True`，而它在 Windows 上是走 `urllib.request.getproxies()`，
    **会读注册表里的 IE/WinINET 代理设置**。用户一开 VPN，请求就会被静默
    导向代理出口；共享的机场 IP 常被 GitHub 限流（403），于是本机直连明明
    能通、应用内却报错。所以两条路都要试，而不是听天由命。
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "xiaoma-ai-studio",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(
            timeout=20.0, follow_redirects=True, trust_env=not direct
        ) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        return 0, None, f"{e.__class__.__name__}：{e}"[:200]
    if resp.status_code != 200:
        return resp.status_code, None, ""
    try:
        return 200, resp.json() or {}, ""
    except ValueError:
        return 200, None, "响应不是合法 JSON"


async def _fetch_release(source: str, repo: str) -> tuple[dict | None, str]:
    """取最新 Release。返回 (数据, 错误消息)；成功时错误为空串。

    先直连、失败再走系统代理：
    - 直连优先是为了避开「机场共享 IP 被 GitHub 限流」；
    - 代理兜底是为了照顾 GitHub API 本身被墙的网络环境。
    """
    url = _release_url(source, repo)
    errors: list[str] = []
    for label, direct in (("直连", True), ("系统代理", False)):
        code, data, err = await _probe(url, direct=direct)
        if code == 200 and data is not None:
            return data, ""
        if code == 404:
            return None, f"{source} 上没有仓库「{repo}」或它还没发布 Release"
        if code == 0:
            errors.append(f"{source} {label}连不上（{err}）")
            continue
        if code in (403, 429):
            errors.append(f"{source} {label}被限流（HTTP {code}）")
            continue
        errors.append(f"{source} {label}返回 HTTP {code}")
    return None, "；".join(errors) or f"{source} 检查失败"


async def check_update(force: bool = False) -> dict[str, Any]:
    """检查更新。未配置仓库地址时返回可读提示，不报错。

    GitHub 与 Gitee 的**账号名往往不同**（GitHub: 用户名，Gitee: 另一个用户名），
    所以两个源各支持一个仓库路径：`update.repo` 给 GitHub，`update.repo_gitee`
    给 Gitee（留空则复用前者）。配置的源不通时自动退到另一个源。
    """
    state = await local_state()
    source = str(runtime_value("update.source", "github") or "github").strip()
    github_repo = str(runtime_value("update.repo", "") or "").strip()
    gitee_repo = str(runtime_value("update.repo_gitee", "") or "").strip()
    repos: dict[str, str] = {"github": github_repo, "gitee": gitee_repo or github_repo}
    primary_repo = repos.get(source) or github_repo or gitee_repo
    state["source"] = source
    state["repo"] = primary_repo
    state["repoGitee"] = repos.get("gitee", "")

    if not primary_repo:
        state.update(
            {
                "hasUpdate": False,
                "error": "尚未配置更新源仓库，请到「系统设置 → 系统配置」填写 update.repo"
                "（GitHub 用 用户名/仓库名；Gitee 账号名不同时另填 update.repo_gitee）",
            }
        )
        return state

    now = time.time()
    cache_key = f"{source}:{primary_repo}"
    if not force and _cache.get("key") == cache_key and now - float(_cache.get("at", 0)) < _CHECK_TTL:
        # 缓存里只有 Release 信息，本地状态（版本 / commit / 安装方式 / 仓库）每次都要取最新
        merged = dict(state)
        merged.update(_cache["data"])
        return merged

    result: dict[str, Any] = {
        "hasUpdate": False,
        "latest": "",
        "notes": "",
        "url": "",
        "publishedAt": "",
        "checkedAt": "",
        "usedSource": "",
        "usedRepo": "",
    }

    data: dict | None = None
    errors: list[str] = []
    order = [source, *[s for s in _SOURCES if s != source]]
    for candidate in order:
        cand_repo = repos.get(candidate, "")
        if not cand_repo:
            continue
        data, err = await _fetch_release(candidate, cand_repo)
        if data is not None:
            result["usedSource"] = candidate
            result["usedRepo"] = cand_repo
            break
        errors.append(err)

    if data is None:
        result["error"] = "；".join(errors) or "检查更新失败"
    else:
        latest = str(data.get("tag_name") or data.get("name") or "").strip()
        result.update(
            {
                "hasUpdate": _is_newer(latest, __version__),
                "latest": latest,
                "notes": str(data.get("body") or "")[:4000],
                "url": _release_page_url(
                    str(result.get("usedSource") or ""),
                    str(result.get("usedRepo") or ""),
                    latest,
                    str(data.get("html_url") or ""),
                ),
                "publishedAt": str(data.get("published_at") or data.get("created_at") or ""),
            }
        )

    result["checkedAt"] = datetime.datetime.now().isoformat(timespec="seconds")
    state.update(result)
    if not result.get("error"):
        _cache.update({"key": cache_key, "at": now, "data": dict(result)})
    state["canSelfUpdate"] = can_self_update()
    state["packageKind"] = package_kind()
    return state


# ============================================================
# 一键更新
# ============================================================


def _changed_files(diff_output: str) -> list[str]:
    return [ln.strip() for ln in diff_output.splitlines() if ln.strip()]


async def run_update() -> dict[str, Any]:
    """执行更新。仅 git 安装且工作区干净时才允许。"""
    state = await local_state()
    kind = state["installKind"]

    if kind == "docker":
        return {
            "ok": False,
            "needsRestart": False,
            "steps": [],
            "message": "当前运行在 Docker 容器里，容器内不适合自我更新。请在宿主机执行："
            "docker compose pull && docker compose up -d",
        }
    if kind == "archive":
        return {
            "ok": False,
            "needsRestart": False,
            "steps": [],
            "message": "当前是压缩包安装：可用「一键更新」只替换程序代码（保留数据），"
            "也可以到 Releases 下载新版本解压覆盖。",
        }
    if state.get("dirty"):
        return {
            "ok": False,
            "needsRestart": False,
            "steps": [],
            "message": "本地有未提交的改动，为避免覆盖你的修改已停止自动更新。"
            "请先 git commit 或 git stash 后再更新",
        }
    if not git_available():
        return {
            "ok": False,
            "needsRestart": False,
            "steps": [],
            "message": "未检测到 git 命令，请安装 git 后重试，或手动下载新版本覆盖",
        }

    steps: list[dict[str, Any]] = []
    before = state.get("commit") or ""

    # 升级前自动做一份轻备份：更新换的是代码，但用户的数据库与作品不该跟着冒险。
    # 备份失败（例如磁盘满）不阻断更新，但要在步骤里如实写出来，不能悄悄跳过。
    from app.services import backup_service

    try:
        info = await asyncio.to_thread(backup_service.create_light_backup, "pre-update")
        steps.append(
            {
                "name": "升级前轻备份",
                "ok": True,
                "output": f"{info['name']}（{info['size']} 字节，素材清单 {info['assets']} 条）",
            }
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("升级前轻备份失败：%s", e)
        steps.append({"name": "升级前轻备份", "ok": False, "output": str(e)})

    async def step(name: str, args: list[str], cwd: Path) -> tuple[bool, str]:
        code, out = await _run_cmd(args, cwd)
        ok = code == 0
        steps.append({"name": name, "ok": ok, "output": out.strip()})
        return ok, out

    ok, _ = await step("拉取最新代码", ["git", "pull", "--ff-only"], REPO_ROOT)
    if not ok:
        return {
            "ok": False,
            "needsRestart": False,
            "steps": steps,
            "message": "git pull 失败，可能是本地有分叉提交或网络问题，请按上面的输出手动处理",
        }

    after_branch, after = _read_git_head()
    changed: list[str] = []
    if before and after and before != after:
        code, diff = await _run_cmd(
            ["git", "diff", "--name-only", before, after], REPO_ROOT, timeout=60
        )
        if code == 0:
            changed = _changed_files(diff)

    versions = {"before": before, "after": after, "branch": after_branch}

    # 依赖只在清单文件真变了才装，避免每次更新都等 pip
    if not changed or "backend/requirements.txt" in changed:
        import sys

        ok, _ = await step(
            "安装后端依赖",
            [sys.executable, "-m", "pip", "install", "-q", "-r", "backend/requirements.txt"],
            REPO_ROOT,
        )
        if not ok:
            return {
                "ok": False,
                "needsRestart": True,
                "steps": steps,
                "versions": versions,
                "message": "代码已更新，但依赖安装失败，请手动执行 "
                "backend\\venv\\Scripts\\python -m pip install -r backend/requirements.txt",
            }

    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not changed or any(p.startswith("frontend/") for p in changed):
        if not npm:
            steps.append({"name": "重建前端界面", "ok": False, "output": "未检测到 npm"})
        else:
            frontend_dir = REPO_ROOT / "frontend"
            if any(p.startswith("frontend/package") for p in changed):
                ok, _ = await step("安装前端依赖", [npm, "install"], frontend_dir)
                if not ok:
                    steps.append({"name": "跳过重建", "ok": False, "output": "依赖安装失败"})
                    return {
                        "ok": False,
                        "needsRestart": True,
                        "steps": steps,
                        "versions": versions,
                        "message": "代码已更新，但前端依赖安装失败，请手动在 frontend 目录执行 npm install",
                    }
            await step("重建前端界面", [npm, "run", "build"], frontend_dir)

    return {
        "ok": True,
        "needsRestart": True,
        "steps": steps,
        "versions": versions,
        "changed": changed,
        "message": "更新完成。请重启服务（关闭窗口后重新运行 start.bat / start.sh）以加载新版本",
    }


# ============================================================
# 打包版（桌面版 / 便携版）一键更新
# ============================================================

# 更新时只覆盖「纯代码与界面」。runtime / _internal / 启动器 exe 正在被当前进程使用，
# Windows 上根本覆盖不了；data / .env / secret.key 属于用户，任何情况下都不碰。
_CODE_DIRS = ("backend/app", "backend/alembic", "backend/assets", "frontend", "docs", "assets")
_CODE_FILES = ("CHANGELOG.md", "README.md", "LICENSE", "README-PORTABLE.txt")

_MAX_PACKAGE_BYTES = 600 * 1024 * 1024
_TRUSTED_HOSTS = (
    "github.com",
    "gitee.com",
    "githubusercontent.com",
    "release-assets.githubusercontent.com",
)

_apply_lock = asyncio.Lock()


class UpdateError(Exception):
    """一键更新失败：消息直接展示给用户，不需要再翻译一层。"""


def package_kind() -> str:
    """desktop | portable。

    桌面版的包根有启动器 `XiaomaAIStudio.exe` 与 `_internal`；便携版只有 `start.bat`。
    """
    if (REPO_ROOT / "XiaomaAIStudio.exe").is_file() or (REPO_ROOT / "_internal").is_dir():
        return "desktop"
    return "portable"


def can_self_update() -> bool:
    """只有 Windows 的压缩包安装才支持「一键更新」。

    git 克隆有自己的更新路径（run_update）；Docker / Linux 不在这里做——
    替换正在运行的进程文件在不同平台上风险差别太大，不做比做错好。
    """
    return (
        os.name == "nt"
        and install_kind() == "archive"
        and (REPO_ROOT / "backend" / "app" / "main.py").is_file()
    )


def pick_asset(release: dict[str, Any], kind: str) -> dict[str, Any] | None:
    """从 Release 资产里挑出当前安装方式对应的包（`…-desktop.zip` / `…-portable.zip`）。"""
    assets = release.get("assets")
    if not isinstance(assets, list):
        return None
    suffix = f"-{kind}.zip"
    for item in assets:
        if isinstance(item, dict) and str(item.get("name") or "").endswith(suffix):
            return item
    return None


async def _release_with_assets() -> tuple[str, str, dict[str, Any]]:
    """按配置的源顺序取最新 Release（保留 assets，check_update 只留下展示字段）。"""
    source = str(runtime_value("update.source", "github") or "github").strip()
    github_repo = str(runtime_value("update.repo", "") or "").strip()
    gitee_repo = str(runtime_value("update.repo_gitee", "") or "").strip()
    repos = {"github": github_repo, "gitee": gitee_repo or github_repo}

    errors: list[str] = []
    order = [source, *[s for s in _SOURCES if s != source]]
    for candidate in order:
        repo = repos.get(candidate, "")
        if not repo:
            continue
        data, err = await _fetch_release(candidate, repo)
        if data is not None:
            return candidate, repo, data
        errors.append(err)
    raise UpdateError("检查更新失败：" + ("；".join(errors) or "没有可用的更新源"))


async def _download_asset(url: str, dest: Path) -> int:
    """下载安装包到本机（先直连、失败再走系统代理），返回字节数。

    与检查更新一样区分直连 / 代理：机场共享 IP 被限流时直连往往更稳，
    而 GitHub 本身被墙时又只能走代理。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    headers = {"User-Agent": "xiaoma-ai-studio"}
    last = ""
    for direct in (True, False):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, read=300.0),
                follow_redirects=True,
                trust_env=not direct,
            ) as client:
                async with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code != 200:
                        last = f"HTTP {resp.status_code}"
                        continue
                    total = int(resp.headers.get("content-length") or 0)
                    if total > _MAX_PACKAGE_BYTES:
                        raise UpdateError("安装包异常大，已拒绝下载")
                    size = 0
                    with tmp.open("wb") as fh:
                        async for chunk in resp.aiter_bytes(1024 * 1024):
                            if not chunk:
                                continue
                            size += len(chunk)
                            if size > _MAX_PACKAGE_BYTES:
                                raise UpdateError("安装包异常大，已拒绝下载")
                            fh.write(chunk)
            if size == 0:
                raise UpdateError("下载到的安装包是空的")
            os.replace(tmp, dest)
            return size
        except UpdateError:
            tmp.unlink(missing_ok=True)
            raise
        except (httpx.HTTPError, OSError) as e:
            tmp.unlink(missing_ok=True)
            last = f"{e.__class__.__name__}：{e}"[:200]
    raise UpdateError(f"下载安装包失败：{last}")


def _safe_extract(zip_path: Path, dest: Path) -> None:
    """解压并挡住 zip slip（绝对路径 / `..`），不依赖 zipfile 的默认行为。"""
    dest.mkdir(parents=True, exist_ok=True)
    base = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or re.match(r"^[a-zA-Z]:", name):
                raise UpdateError("安装包里有绝对路径，已拒绝解压")
            if ".." in Path(name).parts:
                raise UpdateError("安装包里有 ../ 路径，已拒绝解压")
            target = (dest / name).resolve()
            if not target.is_relative_to(base):
                raise UpdateError("安装包试图写到目标目录之外，已拒绝解压")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


def _package_root(staging: Path) -> Path:
    """压缩包的根可能是包目录本身，也可能多套一层（GitHub 桌面版是 `app/`）。"""
    if (staging / "backend" / "app" / "main.py").is_file():
        return staging
    for child in staging.iterdir():
        if child.is_dir() and (child / "backend" / "app" / "main.py").is_file():
            return child
    raise UpdateError("安装包里没有 backend/app/main.py，不是小马的安装包")


def _package_version(root: Path) -> str:
    text = (root / "backend" / "app" / "__init__.py").read_text(encoding="utf-8", errors="replace")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not m:
        raise UpdateError("安装包里读不到版本号")
    return m.group(1)


def _sync_tree(src: Path, dst: Path) -> None:
    """把 src 覆盖到 dst：新文件写过去，旧版本留下的文件删掉。

    只对代码目录调用，删除范围因此是可控的；`backend/data`、`.env` 都不在这里面。
    """
    dst.mkdir(parents=True, exist_ok=True)
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)

    extras = [p for p in dst.rglob("*") if not (src / p.relative_to(dst)).exists()]
    for path in sorted(extras, key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()


def _zip_tree(zip_path: Path, root: Path, rels: tuple[str, ...]) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in rels:
            path = root / rel
            if path.is_file():
                zf.write(path, rel)
                continue
            if not path.is_dir():
                continue
            for item in path.rglob("*"):
                if item.is_file():
                    zf.write(item, item.relative_to(root).as_posix())


def _restore(backup: Path, root: Path) -> None:
    tmp = backup.parent / "restore-tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    _safe_extract(backup, tmp)
    for rel in _CODE_DIRS:
        if (tmp / rel).is_dir():
            _sync_tree(tmp / rel, root / rel)
    for rel in _CODE_FILES:
        if (tmp / rel).is_file():
            shutil.copy2(tmp / rel, root / rel)
    shutil.rmtree(tmp, ignore_errors=True)


def install_from_zip(zip_path: Path, latest: str, root: Path | None = None) -> dict[str, Any]:
    """把安装包里的代码覆盖到当前安装（同步函数，调用方放线程里跑）。

    顺序是「先备份、再覆盖、失败回滚」：
    - 备份放在数据目录 `updates/backups/`，出问题可手工解压恢复；
    - 包内版本必须与 Release 标签一致，防止拿错包；
    - 覆盖失败会立刻从备份还原，避免留下半新半旧的程序目录。
    """
    target = root or REPO_ROOT
    staging = zip_path.parent / f"staging-{zip_path.stem}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    _safe_extract(zip_path, staging)
    try:
        pkg = _package_root(staging)
        pkg_version = _package_version(pkg)
        if _parse_version(pkg_version) != _parse_version(latest):
            raise UpdateError(
                f"包内版本 {pkg_version} 与 Release 标签 {latest} 不一致，已拒绝安装"
            )

        # 依赖变了就不能只换代码：嵌入式运行时不会自己装新包，硬换会得到一个起不来的程序。
        req_new = pkg / "backend" / "requirements.txt"
        req_old = target / "backend" / "requirements.txt"
        if req_new.is_file() and req_old.is_file():

            def _deps(path: Path) -> set[str]:
                return {
                    ln.strip()
                    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
                    if ln.strip() and not ln.strip().startswith("#")
                }

            if _deps(req_new) != _deps(req_old):
                raise UpdateError(
                    "新版本的依赖有变化，一键更新只换代码会启动失败。"
                    "请下载完整安装包解压覆盖（data/ 会保留），或先在设置里导出配置再重装"
                )

        backups = zip_path.parent / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = backups / f"backup-{__version__}-{stamp}.zip"
        _zip_tree(backup, target, _CODE_DIRS + _CODE_FILES)

        applied: list[str] = []
        try:
            for rel in _CODE_DIRS:
                src = pkg / rel
                if src.is_dir():
                    _sync_tree(src, target / rel)
                    applied.append(rel)
            for rel in _CODE_FILES:
                src = pkg / rel
                if src.is_file():
                    shutil.copy2(src, target / rel)
        except OSError as e:
            try:
                _restore(backup, target)
            except OSError as e2:
                raise UpdateError(
                    f"覆盖文件失败（{e}），自动回滚也失败（{e2}）。备份在 {backup}"
                ) from e2
            raise UpdateError(f"覆盖文件失败（{e}），已回滚到更新前") from e
        return {
            "updated": True,
            "fromVersion": __version__,
            "toVersion": pkg_version,
            "backup": str(backup),
            "applied": applied,
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)


async def apply_packaged_update() -> dict[str, Any]:
    """打包版一键更新：下载 → 校验 → 备份 → 覆盖代码（不碰数据与运行时）。"""
    if not can_self_update():
        raise UpdateError(
            "一键更新只支持 Windows 的桌面版 / 便携版压缩包安装；"
            "git 安装请用普通更新，Docker 请在宿主机更新镜像"
        )

    async with _apply_lock:
        _, _, release = await _release_with_assets()
        latest = str(release.get("tag_name") or release.get("name") or "").strip()
        if not latest:
            raise UpdateError("最新 Release 没有版本标签")
        if not _is_newer(latest, __version__):
            return {
                "updated": False,
                "version": __version__,
                "latest": latest,
                "message": f"当前已是最新版本（{__version__}）",
            }

        kind = package_kind()
        asset = pick_asset(release, kind)
        if asset is None:
            names = "、".join(
                str(a.get("name")) for a in release.get("assets") or [] if isinstance(a, dict)
            )
            raise UpdateError(
                f"最新版本里没有「{kind}」安装包（Release 现有资产：{names or '空'}）"
            )

        url = str(asset.get("browser_download_url") or "").strip()
        if not url:
            raise UpdateError("安装包没有下载地址")
        host = urlparse(url).hostname or ""
        if not any(host == h or host.endswith("." + h) for h in _TRUSTED_HOSTS):
            raise UpdateError(f"安装包地址不在信任的发布源里：{host}")

        dest = settings.data_dir / "updates" / f"{latest}-{kind}.zip"
        size = await _download_asset(url, dest)
        result = await asyncio.to_thread(install_from_zip, dest, latest)
        result.update({"size": size, "restartRequired": True})
        return result
