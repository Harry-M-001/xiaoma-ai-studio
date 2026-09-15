"""在线更新：版本比较、更新源地址、一键更新的安全边界（直接 python 运行）。

运行：venv/Scripts/python tests/test_update.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import update_service


def test_parse_version():
    assert update_service._parse_version("v1.2.3") == (1, 2, 3)
    assert update_service._parse_version("1.0.0") == (1, 0, 0)
    assert update_service._parse_version("v2.0") == (2, 0)
    assert update_service._parse_version("v1.0.0-beta.1") == (1, 0, 0)
    assert update_service._parse_version("") == ()
    assert update_service._parse_version("nightly") == ()


def test_is_newer():
    assert update_service._is_newer("v1.0.1", "1.0.0") is True
    assert update_service._is_newer("v1.1.0", "1.0.9") is True
    assert update_service._is_newer("v2.0.0", "1.99.99") is True
    assert update_service._is_newer("v1.0.0", "1.0.0") is False
    assert update_service._is_newer("v0.9.9", "1.0.0") is False
    # 上游 tag 不是版本号时必须判定为「没有新版」，不能瞎提示
    assert update_service._is_newer("nightly", "1.0.0") is False
    assert update_service._is_newer("", "1.0.0") is False


def test_release_url():
    assert (
        update_service._release_url("github", "me/repo")
        == "https://api.github.com/repos/me/repo/releases/latest"
    )
    assert (
        update_service._release_url("gitee", "me/repo")
        == "https://gitee.com/api/v5/repos/me/repo/releases/latest"
    )
    # 自定义源当成完整网址前缀，方便自建 Gitea / 内网镜像
    assert (
        update_service._release_url("https://git.example.com/api/v1", "me/repo")
        == "https://git.example.com/api/v1/me/repo/releases/latest"
    )


def test_changed_files_ignores_blank_lines():
    diff = "backend/app/main.py\n\nfrontend/src/api.ts\n  \nREADME.md\n"
    assert update_service._changed_files(diff) == [
        "backend/app/main.py",
        "frontend/src/api.ts",
        "README.md",
    ]


def test_install_kind_is_known_value():
    assert update_service.install_kind() in ("git", "docker", "archive")


def test_local_state_shape():
    state = asyncio.run(update_service.local_state())
    for key in ("version", "commit", "branch", "installKind", "dirty", "repoRoot"):
        assert key in state, f"缺少字段 {key}"
    assert isinstance(state["version"], str) and state["version"]


def _refuse_with(kind: str) -> dict:
    """把安装方式临时改成非 git，验证一键更新的拒绝路径（不会真的跑命令）。"""
    orig = update_service.install_kind
    update_service.install_kind = lambda: kind  # type: ignore[assignment]
    try:
        return asyncio.run(update_service.run_update())
    finally:
        update_service.install_kind = orig  # type: ignore[assignment]


def test_run_update_refuses_docker():
    out = _refuse_with("docker")
    assert out["ok"] is False
    assert out["needsRestart"] is False
    assert out["steps"] == []
    # 必须给出宿主机的替代做法，不能让用户干瞪眼
    assert "docker compose pull" in out["message"]


def test_run_update_refuses_archive():
    out = _refuse_with("archive")
    assert out["ok"] is False
    assert out["steps"] == []
    assert "git" in out["message"]
    # 压缩包用户必须被告知 data/ 目录可以保留
    assert "data/" in out["message"]


def test_check_update_without_repo_returns_hint():
    """未配置 update.repo 时返回可读提示，而不是抛异常或假报有新版本。"""
    from app.services import config_center_service

    orig = config_center_service.runtime_value
    config_center_service.runtime_value = lambda key, default=None: (  # type: ignore[assignment]
        "" if key == "update.repo" else default
    )
    # update_service 内部按名引用 runtime_value，需同时替换模块内引用
    orig_in_mod = update_service.runtime_value
    update_service.runtime_value = config_center_service.runtime_value  # type: ignore[assignment]
    try:
        out = asyncio.run(update_service.check_update(force=True))
    finally:
        config_center_service.runtime_value = orig  # type: ignore[assignment]
        update_service.runtime_value = orig_in_mod  # type: ignore[assignment]
    assert out["hasUpdate"] is False
    assert "update.repo" in out.get("error", "")


def _with_repo(repo: str):
    """把 update 配置临时改成指定仓库。"""

    def fake(key, default=None):
        return repo if key == "update.repo" else ("gitee" if key == "update.source" else default)

    return fake


def _with_cfg(values: dict):
    def fake(key, default=None):
        return values.get(key, default)

    return fake


def _patched(values: dict, fetch):  # noqa: ANN001
    """装好配置与假的 Release 抓取，返回 (恢复函数, 抓取记录)。"""
    from app.services import config_center_service

    calls: list[tuple[str, str]] = []

    async def fake_fetch(source: str, repo: str):  # noqa: ANN202
        calls.append((source, repo))
        return fetch(source, repo)

    orig_mod_rv = update_service.runtime_value
    orig_cc_rv = config_center_service.runtime_value
    orig_fetch = update_service._fetch_release
    update_service.runtime_value = _with_cfg(values)  # type: ignore[assignment]
    update_service._fetch_release = fake_fetch  # type: ignore[assignment]
    update_service._cache.clear()

    def restore() -> None:
        update_service.runtime_value = orig_mod_rv  # type: ignore[assignment]
        config_center_service.runtime_value = orig_cc_rv  # type: ignore[assignment]
        update_service._fetch_release = orig_fetch  # type: ignore[assignment]
        update_service._cache.clear()

    return restore, calls


def test_per_source_repo_each_uses_own_path():
    """GitHub 与 Gitee 账号名不同：两个源必须各查自己的仓库路径。"""
    # gitee 先失败，回退到 github
    restore, calls = _patched(
        {
            "update.source": "gitee",
            "update.repo": "Harry-M-001/xiaoma-ai-studio",
            "update.repo_gitee": "haoruiM/xiaoma-ai-studio",
        },
        lambda source, repo: (None, "no") if source == "gitee" else ({"tag_name": "v9.9.9"}, ""),
    )
    try:
        out = asyncio.run(update_service.check_update(force=True))
    finally:
        restore()
    assert calls[0] == ("gitee", "haoruiM/xiaoma-ai-studio"), calls
    assert calls[1] == ("github", "Harry-M-001/xiaoma-ai-studio"), calls
    assert out["usedSource"] == "github"
    assert out["usedRepo"] == "Harry-M-001/xiaoma-ai-studio"
    assert out["hasUpdate"] is True


def test_gitee_repo_falls_back_to_github_path_when_unset():
    """没单独填 Gitee 仓库时，两个源复用同一个路径（同名仓库的常见情况）。"""
    restore, calls = _patched(
        {"update.source": "gitee", "update.repo": "me/repo", "update.repo_gitee": ""},
        lambda source, repo: ({"tag_name": "v9.9.9"}, ""),
    )
    try:
        asyncio.run(update_service.check_update(force=True))
    finally:
        restore()
    assert calls == [("gitee", "me/repo")], calls


def test_missing_source_repo_is_skipped_not_fatal():
    """只配了 Gitee 仓库、源却是 github 时，应直接查 Gitee 而不是报「未配置」。"""
    restore, calls = _patched(
        {"update.source": "github", "update.repo": "", "update.repo_gitee": "haoruiM/repo"},
        lambda source, repo: ({"tag_name": "v9.9.9"}, ""),
    )
    try:
        out = asyncio.run(update_service.check_update(force=True))
    finally:
        restore()
    assert calls == [("gitee", "haoruiM/repo")], calls
    assert out.get("error") in (None, ""), out


def test_cached_result_keeps_local_state():
    """走缓存路径时必须仍带上 source/repo/version 等本地字段。

    回归背景：缓存里只存 Release 信息，早期实现直接返回缓存，
    导致前端拿到 hasUpdate=true 却同时显示「未配置仓库」。
    """
    import time as _time

    from app.services import config_center_service

    orig_cc, orig_mod = config_center_service.runtime_value, update_service.runtime_value
    update_service.runtime_value = _with_repo("me/repo")  # type: ignore[assignment]
    try:
        # 人工塞入一份「刚查过」的缓存
        update_service._cache.clear()
        update_service._cache.update(
            {
                "key": "gitee:me/repo",
                "at": _time.time(),
                "data": {
                    "hasUpdate": True,
                    "latest": "v9.9.9",
                    "notes": "缓存里的更新说明",
                    "url": "https://example.com/rel",
                    "publishedAt": "2026-09-01T00:00:00Z",
                    "checkedAt": "2026-09-15T00:00:00",
                    "usedSource": "github",
                },
            }
        )
        out = asyncio.run(update_service.check_update(force=False))
    finally:
        update_service._cache.clear()
        config_center_service.runtime_value = orig_cc  # type: ignore[assignment]
        update_service.runtime_value = orig_mod  # type: ignore[assignment]

    # 缓存命中：Release 信息来自缓存
    assert out["hasUpdate"] is True
    assert out["latest"] == "v9.9.9"
    assert out["notes"] == "缓存里的更新说明"
    # 本地字段必须补齐，不能凭空消失
    assert out["repo"] == "me/repo", out
    assert out["source"] == "gitee", out
    assert out["version"], out
    assert "installKind" in out


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
