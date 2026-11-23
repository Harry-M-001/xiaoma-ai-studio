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
