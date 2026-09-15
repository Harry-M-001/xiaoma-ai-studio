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
import re
import shutil
import time
from pathlib import Path
from typing import Any

import httpx

from app import __version__
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


async def _fetch_release(source: str, repo: str) -> tuple[dict | None, str]:
    """取最新 Release。返回 (数据, 错误消息)；成功时错误为空串。"""
    url = _release_url(source, repo)
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"Accept": "application/vnd.github+json"})
    except httpx.HTTPError as e:
        return None, f"{source} 连不上（{e.__class__.__name__}）"
    if resp.status_code == 200:
        return resp.json() or {}, ""
    if resp.status_code == 404:
        return None, f"{source} 上没有仓库「{repo}」或它还没发布 Release"
    return None, f"{source} 返回 HTTP {resp.status_code}"


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
                "url": str(data.get("html_url") or ""),
                "publishedAt": str(data.get("published_at") or data.get("created_at") or ""),
            }
        )

    result["checkedAt"] = datetime.datetime.now().isoformat(timespec="seconds")
    state.update(result)
    if not result.get("error"):
        _cache.update({"key": cache_key, "at": now, "data": dict(result)})
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
            "message": "当前不是 git 克隆安装（未检测到 .git 目录），无法自动更新。"
            "请到 Releases 下载新版本解压覆盖；你的 data/ 目录可直接保留。",
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
