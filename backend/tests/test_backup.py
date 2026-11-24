"""备份与恢复：升级前能自动做、全量只手动、密钥不出门、旧库恢复后能迁移。

这几条都是「出事时才知道」的能力，所以在这里用**真实的小数据目录**跑一遍：
真建库、真放一个素材文件、真打 zip、真恢复，而不是只测几个字符串。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.services import backup_service  # noqa: E402

HEAD, KNOWN = backup_service._alembic_info()


def _make_data_dir(root: Path, revision: str = HEAD, key: str = "SECRET-KEY-123") -> None:
    """造一个「能备份」的最小数据目录：数据库 + 一条素材 + 一个素材文件。"""
    data = root / "data"
    storage = data / "storage" / "2026-11"
    storage.mkdir(parents=True)
    (storage / "a.png").write_bytes(b"PNG")
    (data / "secret.key").write_bytes(b"fernet-key-should-never-be-packed")

    con = sqlite3.connect(str(data / "xiaoma.db"))
    con.executescript(
        """
        create table alembic_version (version_num varchar(32) primary key);
        create table provider_services (id integer primary key, name text, api_key_enc text);
        create table assets (
            id integer primary key, kind text, filename text, original_name text,
            size integer, name text, task_id integer
        );
        """
    )
    con.execute("insert into alembic_version values (?)", (revision,))
    con.execute("insert into provider_services (id, name, api_key_enc) values (1, 'x', ?)", (key,))
    con.execute(
        "insert into assets values (1, 'image', '2026-11/a.png', 'a.png', 3, '', null)"
    )
    con.commit()
    con.close()


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    _make_data_dir(tmp_path)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def test_light_backup_has_no_secrets_and_keeps_the_manifest(data_dir):
    info = backup_service.create_light_backup("manual")
    path = data_dir / "backups" / info["name"]
    assert path.is_file()

    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        manifest = json.loads(zf.read("manifest.json"))
        assert "xiaoma.db" in names
        assert "secret.key" not in names, "密钥文件被打进备份包了"
        assert manifest["containsSecrets"] is False

        # 数据库快照里的密钥必须已经清空
        snap = path.parent / ".peek.db"
        snap.write_bytes(zf.read("xiaoma.db"))
        try:
            con = sqlite3.connect(str(snap))
            key = con.execute("select api_key_enc from provider_services").fetchone()[0]
            con.close()
        finally:
            snap.unlink(missing_ok=True)
        assert key == "", "数据库快照里还留着 API Key"

    assert b"SECRET-KEY-123" not in path.read_bytes(), "备份包里能搜到明文密钥"
    assert manifest["totals"]["assets"] == 1
    assert manifest["alembicRevision"] == HEAD


def test_light_backup_is_listed(data_dir):
    made = backup_service.create_light_backup("manual")
    rows = backup_service.list_backups()
    assert [r["name"] for r in rows] == [made["name"]]
    assert rows[0]["kind"] == "light" and rows[0]["assets"] == 1


def test_full_backup_packs_storage_and_records_checksums(data_dir):
    info = backup_service.create_full_backup()
    path = data_dir / "backups" / info["name"]
    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        assert "storage/2026-11/a.png" in zf.namelist()
        assert "secret.key" not in zf.namelist()
        entry = next(f for f in manifest["files"] if f["path"] == "2026-11/a.png")
        assert entry["size"] == 3 and len(entry["sha256"]) == 64


def test_full_backup_refuses_when_disk_is_too_full(data_dir, monkeypatch):
    monkeypatch.setattr(backup_service, "_free_bytes", lambda: 1024)
    with pytest.raises(RuntimeError) as e:
        backup_service.create_full_backup()
    assert "磁盘余量不足" in str(e.value)
    # 拒绝要在动手之前：既不产出半截 zip，也不在 backups 目录留下垃圾
    backups = data_dir / "backups"
    assert not backups.exists() or not list(backups.glob("full-*.zip"))


def test_inspect_accepts_a_backup_from_an_older_revision(tmp_path, monkeypatch):
    old = KNOWN - {HEAD}
    assert old, "迁移链里应当有更早的版本"
    rev = sorted(old)[0]
    _make_data_dir(tmp_path, revision=rev)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))

    info = backup_service.create_light_backup("manual")
    checked = backup_service.inspect_backup(tmp_path / "data" / "backups" / info["name"])
    assert checked["ok"] is True
    assert checked["alembicRevision"] == rev
    assert "自动迁移" in checked["hint"]


def test_inspect_refuses_a_backup_from_a_future_revision(tmp_path, monkeypatch):
    _make_data_dir(tmp_path, revision="9999_from_the_future")
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    info = backup_service.create_light_backup("manual")
    with pytest.raises(ValueError) as e:
        backup_service.inspect_backup(tmp_path / "data" / "backups" / info["name"])
    assert "迁移版本" in str(e.value)


def test_inspect_refuses_a_corrupted_package(tmp_path, monkeypatch):
    _make_data_dir(tmp_path)
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    info = backup_service.create_light_backup("manual")
    path = tmp_path / "data" / "backups" / info["name"]

    # 把数据库快照换掉一个字节，校验和应当立刻发现
    with zipfile.ZipFile(path) as zf:
        entries = {n: zf.read(n) for n in zf.namelist()}
    entries["xiaoma.db"] = entries["xiaoma.db"] + b"tampered"
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in entries.items():
            zf.writestr(name, body)

    with pytest.raises(ValueError) as e:
        backup_service.inspect_backup(path)
    assert "校验和" in str(e.value)


def test_restore_puts_the_database_and_assets_back(data_dir):
    info = backup_service.create_full_backup()
    path = data_dir / "backups" / info["name"]

    # 模拟「用坏了一阵子」：数据库被改、素材被删
    con = sqlite3.connect(str(settings.database_path))
    con.execute("update assets set name = 'changed'")
    con.commit()
    con.close()
    (settings.storage_dir / "2026-11" / "a.png").unlink()

    result = asyncio.run(backup_service.restore_backup(path))
    assert result["restored"] is True and result["needsRestart"] is True

    con = sqlite3.connect(str(settings.database_path))
    name = con.execute("select name from assets where id = 1").fetchone()[0]
    key = con.execute("select api_key_enc from provider_services").fetchone()[0]
    con.close()
    assert name == "", "数据库没有被恢复"
    assert key == "", "恢复后不该带回 API Key"
    assert (settings.storage_dir / "2026-11" / "a.png").read_bytes() == b"PNG"
    # 恢复前的现场要留一份，误操作还能回去
    assert list((data_dir / "backups").glob("pre-restore-*.db"))


def test_backup_download_path_rejects_traversal(data_dir):
    backup_service.create_light_backup("manual")
    with pytest.raises(ValueError):
        backup_service.backup_path("../../../etc/passwd")


def test_update_runs_a_light_backup_before_pulling():
    """升级前自动轻备份必须在 git pull **之前**发生，否则代码先变了、库没保住。"""
    src = (BACKEND / "app" / "services" / "update_service.py").read_text(encoding="utf-8")
    body = src[src.index("async def run_update") :]
    assert 'create_light_backup' in body
    assert body.index("create_light_backup") < body.index('"git", "pull"')
