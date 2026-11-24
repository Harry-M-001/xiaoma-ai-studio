"""发版脚本的纯逻辑：预检读取与「该清理哪些旧 Release」。不联网。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
TOOL = BACKEND / "tools" / "release.py"


def _load():
    spec = importlib.util.spec_from_file_location("xiaoma_release", TOOL)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["xiaoma_release"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_versions_match_and_changelog_has_release_notes():
    rel = _load()
    back, front = rel.read_version()
    assert back == front, (back, front)
    body = rel.changelog_section(f"v{back}")
    assert body.strip(), "发版说明不能为空"


def test_prune_candidates_are_oldest_zip_bearing_and_skip_current():
    rel = _load()
    releases = [
        {"id": 30, "tag_name": "v2.2.3", "assets": [{"name": "x-win64-portable.zip"}]},
        {"id": 10, "tag_name": "v1.0.0", "assets": [{"name": "x-win64-portable.zip"}]},
        {"id": 5, "tag_name": "v0.9.0", "assets": [{"name": "v0.9.0.zip"}]},  # 只有源码包
        {"id": 20, "tag_name": "v1.1.0", "assets": [
            {"name": "x-win64-portable.zip"}, {"name": "x-win64-desktop.zip"},
        ]},
        {"id": 40, "tag_name": "fonts-v1", "assets": [{"name": "font.otf"}]},
    ]
    picked = rel.pick_prune_candidates(releases, keep_tag="v2.2.3")
    assert [r["tag_name"] for r in picked] == ["v1.0.0", "v1.1.0"], picked


def test_package_asset_matches_only_our_builds():
    """只认我们自己打的包；Gitee 自动附的源码 zip 与字体等附件不算。"""
    rel = _load()
    assert rel.is_package_asset("a-win64-portable.zip")
    assert rel.is_package_asset("a-win64-desktop.zip")
    assert not rel.is_package_asset("v1.0.0.zip")
    assert not rel.is_package_asset("font.otf")
    assert not rel.is_package_asset("")
    assert rel.asset_named({"assets": [{"name": "a.zip", "id": 1}]}, "a.zip")
    assert rel.asset_named({"assets": [{"name": "b.zip", "id": 1}]}, "a.zip") is None


def test_gitee_attachment_limit_is_checked_before_publishing():
    """100 MB 上限要在推 tag / 建 Release 之前就查出来，否则一次发版会卡在半路。"""
    rel = _load()
    limit = rel.GITEE_MAX_ATTACHMENT
    assert not rel.attachment_too_big(limit), "刚好等于上限应当放行"
    assert rel.attachment_too_big(limit + 1)


def test_prune_only_when_every_package_is_backed_up_on_github():
    """清理 Gitee 旧包前，同 tag 的每个包都必须在 GitHub 上还有一份。"""
    rel = _load()
    both = {
        "assets": [
            {"name": "a-win64-portable.zip"},
            {"name": "a-win64-desktop.zip"},
        ]
    }
    assert rel.all_packages_backed_up(
        both, {"a-win64-portable.zip", "a-win64-desktop.zip"}
    )
    assert not rel.all_packages_backed_up(both, {"a-win64-portable.zip"}), "少一个备份也不能删"
    assert not rel.all_packages_backed_up({"assets": [{"name": "v1.0.0.zip"}]}, set())


def test_network_errors_are_retried_but_not_forever():
    rel = _load()
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("write timed out")
        return "ok"

    assert rel.retry_call(flaky, what="测试上传", delay=0) == "ok"
    assert calls["n"] == 3

    def always_fail():
        raise TimeoutError("still down")

    try:
        rel.retry_call(always_fail, what="测试上传", attempts=2, delay=0)
    except SystemExit as e:
        assert "连续 2 次失败" in str(e)
    else:
        raise AssertionError("连续失败后应当退出，而不是无限重试")
