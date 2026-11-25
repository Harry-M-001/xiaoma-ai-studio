"""打包版一键更新：只换代码、失败可回滚、绝不碰数据。"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.services import update_service  # noqa: E402


def _make_package(path: Path, version: str, requirements: str | None = None) -> Path:
    """造一个当前安装包结构的 zip（桌面版是 app/ 包一层）。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("app/backend/app/__init__.py", f'__version__ = "{version}"\n')
        zf.writestr("app/backend/app/main.py", "# new main\n")
        zf.writestr("app/backend/app/newmod.py", "# 新增模块\n")
        zf.writestr("app/backend/alembic/env.py", "# new alembic\n")
        zf.writestr("app/frontend/webroot/index.html", "<html>new</html>")
        zf.writestr("app/frontend/webroot/assets/app-new.js", "console.log('new')")
        zf.writestr("app/CHANGELOG.md", "- new\n")
        if requirements is not None:
            zf.writestr("app/backend/requirements.txt", requirements)
        # 下面这些是用户/运行时数据，绝不能被写进安装目录
        zf.writestr("app/backend/data/secret.key", "FROM-PACKAGE")
        zf.writestr("app/runtime/python.exe", "FROM-PACKAGE")
        zf.writestr("app/.env", "APP_PASSWORD=from-package")
    return path


def _make_install(root: Path) -> None:
    (root / "backend" / "app").mkdir(parents=True)
    (root / "backend" / "app" / "__init__.py").write_text('__version__ = "1.0.0"\n')
    (root / "backend" / "app" / "main.py").write_text("# old main\n")
    (root / "backend" / "app" / "stale.py").write_text("# 新版删掉了这个文件\n")
    (root / "backend" / "alembic").mkdir(parents=True)
    (root / "backend" / "alembic" / "env.py").write_text("# old alembic\n")
    (root / "backend" / "requirements.txt").write_text("fastapi==0.115.6\n")
    (root / "frontend" / "webroot").mkdir(parents=True)
    (root / "frontend" / "webroot" / "index.html").write_text("<html>old</html>")
    (root / "frontend" / "webroot" / "assets").mkdir()
    (root / "frontend" / "webroot" / "assets" / "app-old.js").write_text("old")
    (root / "backend" / "data").mkdir()
    (root / "backend" / "data" / "secret.key").write_text("KEEP-ME")
    (root / "backend" / "data" / "xiaoma.db").write_text("DB")
    (root / ".env").write_text("APP_PASSWORD=keep")


def test_install_replaces_code_keeps_data_and_backs_up(tmp_path):
    root = tmp_path / "install"
    _make_install(root)
    pkg = _make_package(tmp_path / "v9.9.9-desktop.zip", "9.9.9")

    result = update_service.install_from_zip(pkg, "v9.9.9", root=root)

    assert result["updated"] is True
    assert result["toVersion"] == "9.9.9"
    assert (root / "backend" / "app" / "main.py").read_text() == "# new main\n"
    assert (root / "backend" / "app" / "newmod.py").is_file()
    assert not (root / "backend" / "app" / "stale.py").exists(), "旧版本残留文件没清掉"
    assert (root / "backend" / "data" / "secret.key").read_text() == "KEEP-ME"
    assert (root / "backend" / "data" / "xiaoma.db").read_text() == "DB"
    assert (root / ".env").read_text() == "APP_PASSWORD=keep"
    assert not (root / "runtime").exists(), "运行时不该被覆盖"
    assert (root / "backend" / "data" / "secret.key").read_text() != "FROM-PACKAGE"

    backup = Path(result["backup"])
    assert backup.is_file()
    with zipfile.ZipFile(backup) as zf:
        names = set(zf.namelist())
        assert "backend/app/stale.py" in names
        # Windows 写文件会把 \n 换成 \r\n，比较前统一一下
        assert zf.read("backend/app/main.py").replace(b"\r\n", b"\n") == b"# old main\n"


def test_version_mismatch_is_refused(tmp_path):
    root = tmp_path / "install"
    _make_install(root)
    pkg = _make_package(tmp_path / "wrong.zip", "9.9.8")

    with pytest.raises(update_service.UpdateError) as err:
        update_service.install_from_zip(pkg, "v9.9.9", root=root)
    assert "不一致" in str(err.value)
    assert (root / "backend" / "app" / "main.py").read_text() == "# old main\n"


def test_zip_slip_is_refused(tmp_path):
    root = tmp_path / "install"
    _make_install(root)
    pkg = tmp_path / "evil.zip"
    with zipfile.ZipFile(pkg, "w") as zf:
        zf.writestr("app/backend/app/__init__.py", '__version__ = "9.9.9"\n')
        zf.writestr("../evil.txt", "pwned")

    with pytest.raises(update_service.UpdateError) as err:
        update_service.install_from_zip(pkg, "v9.9.9", root=root)
    assert "路径" in str(err.value)
    assert not (tmp_path / "evil.txt").exists()


def test_dependency_change_is_refused(tmp_path):
    """依赖变了就不能只换代码：嵌入式运行时不会自己装包。"""
    root = tmp_path / "install"
    _make_install(root)
    pkg = _make_package(
        tmp_path / "deps.zip", "9.9.9", requirements="fastapi==0.115.6\nnewdep==1.0\n"
    )

    with pytest.raises(update_service.UpdateError) as err:
        update_service.install_from_zip(pkg, "v9.9.9", root=root)
    assert "依赖有变化" in str(err.value)
    assert (root / "backend" / "app" / "main.py").read_text() == "# old main\n"


def test_pick_asset_matches_package_kind():
    release = {
        "assets": [
            {"name": "xiaoma-ai-studio-v9-win64-desktop.zip"},
            {"name": "xiaoma-ai-studio-v9-win64-portable.zip"},
            {"name": "some-notes.txt"},
        ]
    }
    assert update_service.pick_asset(release, "desktop")["name"].endswith("-desktop.zip")
    assert update_service.pick_asset(release, "portable")["name"].endswith("-portable.zip")
    assert update_service.pick_asset({"assets": []}, "desktop") is None
