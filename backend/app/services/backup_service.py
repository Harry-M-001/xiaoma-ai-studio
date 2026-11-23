"""备份与恢复：轻备份（升级前自动）与全量备份（手动，含生成产物）。

为什么分两档：
- **轻备份**只有「数据库快照 + 素材清单 + 校验和 + 迁移版本」，几 MB、秒级，
  所以可以在每次一键更新之前自动做一份——升级失败时至少「谁是谁、有哪些作品」还在；
- **全量备份**要把 `storage/` 里的图/视频一起打包，动辄几十上百 GB，只能由用户显式触发，
  并在动手之前先检查磁盘余量，宁可拒绝也不要写到一半把盘塞满。

两条硬规矩：
1. **API Key 不进备份包**。数据库快照落地后立刻把 `provider_services.api_key_enc` 清空，
   恢复回来的机器上重新填 Key 即可；`secret.key` 从来不进包。
2. **恢复前校验迁移版本**。备份可能来自更老的版本：只要它的 revision 在当前迁移链上，
   就允许恢复（启动时自动 `upgrade head`）；来自更新的版本则拒绝，避免降级把表结构弄坏。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory

from app import __version__
from app.config import settings

logger = logging.getLogger("xiaoma.backup")

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
_BACKUP_FORMAT = "xiaoma-backup"
# 打包时额外留的余量：zip 本身、数据库临时副本、文件系统碎片
_DISK_MARGIN = 64 * 1024 * 1024
_DISK_RATIO = 1.2
# 生成产物（可重下/可再生）不进备份：引擎是几十 GB 的可下载文件，缩略图能随时重做
_EXCLUDED_DIRS = {"engines", "thumbnails", "logs", "backups"}


# ---------------------------------------------------------------- 基础信息


def backup_dir() -> Path:
    p = settings.data_dir / "backups"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _alembic_info() -> tuple[str, set[str]]:
    """当前迁移链的 head 与全部 revision（用于判断备份是否来自更新的版本）。"""
    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    return script.get_current_head() or "", {r.revision for r in script.walk_revisions()}


def _db_revision(path: Path) -> str:
    """读某个 SQLite 文件里的 alembic 版本；读不出来返回空串。"""
    try:
        con = sqlite3.connect(str(path))
        try:
            row = con.execute("select version_num from alembic_version").fetchone()
            return str(row[0]) if row else ""
        finally:
            con.close()
    except sqlite3.Error:
        return ""


def _snapshot_db(dest: Path) -> None:
    """用 SQLite Online Backup API 拷一份一致的数据库，并清掉其中的 API Key。

    直接 `copy` 正在写的 WAL 数据库可能拿到半截状态；backup API 拿到的是一致快照。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_path = settings.database_path
    if not src_path.exists():
        raise RuntimeError("还没有数据库文件，先启动一次应用再备份")
    src = sqlite3.connect(str(src_path))
    dst = sqlite3.connect(str(dest))
    try:
        with dst:
            src.backup(dst)
        # 密钥不进备份包：密文与 secret.key 一起才解得出，但「不带回去」是明确要求。
        try:
            with dst:
                dst.execute("UPDATE provider_services SET api_key_enc = ''")
        except sqlite3.Error:
            pass  # 很老的库可能还没有这张表
    finally:
        src.close()
        dst.close()


def _asset_manifest() -> list[dict[str, Any]]:
    """素材清单：不读文件内容，只列「有哪些、多大、在哪儿」。"""
    try:
        con = sqlite3.connect(str(settings.database_path))
        try:
            rows = con.execute(
                "select id, kind, filename, original_name, size, name, task_id from assets order by id"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return [
        {
            "id": r[0],
            "kind": r[1],
            "filename": r[2],
            "originalName": r[3],
            "size": r[4],
            "name": r[5],
            "taskId": r[6],
        }
        for r in rows
    ]


def _storage_files() -> list[Path]:
    root = settings.storage_dir
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in _EXCLUDED_DIRS:
            continue
        files.append(path)
    return files


# ---------------------------------------------------------------- 生成备份


def _write_manifest(
    staging: Path,
    *,
    kind: str,
    reason: str,
    db_file: Path,
    assets: list[dict[str, Any]],
    storage_files: list[tuple[Path, str]] | None = None,
) -> dict[str, Any]:
    head, _known = _alembic_info()
    manifest: dict[str, Any] = {
        "format": _BACKUP_FORMAT,
        "kind": kind,
        "appVersion": __version__,
        "alembicRevision": _db_revision(db_file) or head,
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "reason": reason,
        "containsSecrets": False,
        "database": {
            "name": "xiaoma.db",
            "size": db_file.stat().st_size,
            "sha256": _sha256(db_file),
        },
        "assets": assets,
        "totals": {"assets": len(assets), "assetBytes": sum(int(a.get("size") or 0) for a in assets)},
    }
    if storage_files is not None:
        files = []
        for path, rel in storage_files:
            files.append({"path": rel, "size": path.stat().st_size, "sha256": _sha256(path)})
        manifest["files"] = files
        manifest["totals"]["files"] = len(files)
        manifest["totals"]["fileBytes"] = sum(f["size"] for f in files)
    (staging / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def _zip_staging(staging: Path, dest: Path) -> None:
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for path in sorted(staging.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(staging).as_posix()
            # 媒体文件本来就不压缩，直接存省 CPU（几十 GB 时差别很大）
            compress = zipfile.ZIP_STORED if rel.startswith("storage/") else zipfile.ZIP_DEFLATED
            zf.write(path, rel, compress_type=compress)


def create_light_backup(reason: str = "manual") -> dict[str, Any]:
    """轻备份：数据库快照 + 素材清单 + 校验和 + 迁移版本。"""
    with tempfile.TemporaryDirectory(prefix="xiaoma-light-") as tmp:
        staging = Path(tmp)
        db_file = staging / "xiaoma.db"
        _snapshot_db(db_file)
        manifest = _write_manifest(
            staging, kind="light", reason=reason, db_file=db_file, assets=_asset_manifest()
        )
        dest = backup_dir() / f"light-{_now_tag()}-{reason}.zip"
        _zip_staging(staging, dest)
    logger.info("轻备份完成：%s", dest.name)
    return _describe(dest, manifest)


def _free_bytes() -> int:
    return shutil.disk_usage(settings.data_dir).free


def create_full_backup() -> dict[str, Any]:
    """全量备份：数据库 + 生成产物。先检查磁盘余量，不够就明确拒绝。"""
    files = _storage_files()
    need = settings.database_path.stat().st_size if settings.database_path.exists() else 0
    need += sum(p.stat().st_size for p in files)
    free = _free_bytes()
    if free < int(need * _DISK_RATIO) + _DISK_MARGIN:
        raise RuntimeError(
            f"磁盘余量不足：这次全量备份约需 {need / 1024 / 1024:.0f} MB"
            f"（含余量 {int(need * _DISK_RATIO + _DISK_MARGIN) / 1024 / 1024:.0f} MB），"
            f"当前只剩 {free / 1024 / 1024:.0f} MB。请先清理磁盘或改用轻备份。"
        )

    with tempfile.TemporaryDirectory(prefix="xiaoma-full-") as tmp:
        staging = Path(tmp)
        db_file = staging / "xiaoma.db"
        _snapshot_db(db_file)
        root = settings.storage_dir
        pairs: list[tuple[Path, str]] = []
        for path in files:
            rel = path.relative_to(root)
            target = staging / "storage" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            pairs.append((target, rel.as_posix()))
        manifest = _write_manifest(
            staging,
            kind="full",
            reason="manual",
            db_file=db_file,
            assets=_asset_manifest(),
            storage_files=pairs,
        )
        dest = backup_dir() / f"full-{_now_tag()}.zip"
        _zip_staging(staging, dest)
    logger.info("全量备份完成：%s（%s 个文件）", dest.name, manifest["totals"].get("files", 0))
    return _describe(dest, manifest)


def _describe(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": path.name,
        "size": path.stat().st_size,
        "createdAt": manifest.get("createdAt"),
        "kind": manifest.get("kind"),
        "reason": manifest.get("reason"),
        "alembicRevision": manifest.get("alembicRevision"),
        "assets": manifest.get("totals", {}).get("assets", 0),
    }


# ---------------------------------------------------------------- 列表 / 下载


def list_backups() -> list[dict[str, Any]]:
    """列出已有备份（新→旧）。读不出 manifest 的文件也列出来，只是没有详情。"""
    out: list[dict[str, Any]] = []
    for path in sorted(backup_dir().glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        item: dict[str, Any] = {"name": path.name, "size": path.stat().st_size, "kind": ""}
        try:
            with zipfile.ZipFile(path) as zf:
                manifest = json.loads(zf.read("manifest.json"))
            item.update(
                {
                    "kind": manifest.get("kind") or "",
                    "reason": manifest.get("reason") or "",
                    "createdAt": manifest.get("createdAt") or "",
                    "alembicRevision": manifest.get("alembicRevision") or "",
                    "assets": manifest.get("totals", {}).get("assets", 0),
                }
            )
        except (KeyError, zipfile.BadZipFile, json.JSONDecodeError):
            item["broken"] = True
        out.append(item)
    return out


def backup_path(name: str) -> Path:
    """把「备份名」解析成实际路径，拒绝目录穿越。"""
    root = backup_dir().resolve()
    path = (root / name).resolve()
    if path.parent != root or not path.is_file():
        raise ValueError("备份文件不存在")
    return path


# ---------------------------------------------------------------- 恢复


def _read_manifest(zf: zipfile.ZipFile) -> dict[str, Any]:
    try:
        raw = zf.read("manifest.json")
    except KeyError as e:
        raise ValueError("这不是本应用的备份包（缺少 manifest.json）") from e
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError("备份包的 manifest.json 已损坏") from e
    if manifest.get("format") != _BACKUP_FORMAT:
        raise ValueError("备份包格式不认识，可能来自其它工具")
    return manifest


def _check_revision(manifest: dict[str, Any]) -> tuple[str, str]:
    """返回 (备份里的 revision, 提示)。来自更新版本的备份直接拒绝。"""
    head, known = _alembic_info()
    rev = str(manifest.get("alembicRevision") or "")
    if not rev:
        return "", "备份里没有记录迁移版本，恢复后请留意首次启动的迁移输出"
    if rev == head:
        return rev, ""
    if rev in known:
        return rev, "备份来自更早的版本，恢复后首次启动会自动迁移到当前版本"
    raise ValueError(
        f"备份的迁移版本 {rev} 不在当前版本（{head}）的迁移链上，可能来自更新的应用版本，"
        "已拒绝恢复。请先升级应用，或使用对应版本的备份。"
    )


def inspect_backup(path: Path) -> dict[str, Any]:
    """只校验不落盘：恢复前的「能不能用、会迁移吗」。"""
    with zipfile.ZipFile(path) as zf:
        manifest = _read_manifest(zf)
        db_info = manifest.get("database") or {}
        if db_info.get("name") not in zf.namelist():
            raise ValueError("备份包里没有数据库快照")
        digest = hashlib.sha256(zf.read(db_info["name"])).hexdigest()
        if db_info.get("sha256") and digest != db_info["sha256"]:
            raise ValueError("数据库快照校验和不一致，备份包可能已损坏")
        rev, hint = _check_revision(manifest)
    return {
        "ok": True,
        "kind": manifest.get("kind"),
        "createdAt": manifest.get("createdAt"),
        "appVersion": manifest.get("appVersion"),
        "alembicRevision": rev,
        "currentRevision": _alembic_info()[0],
        "hint": hint,
        "containsSecrets": bool(manifest.get("containsSecrets")),
        "totals": manifest.get("totals") or {},
        "needsRestart": True,
    }


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    root = dest.resolve()
    for info in zf.infolist():
        target = (dest / info.filename).resolve()
        if root != target and root not in target.parents:
            raise ValueError(f"备份包里含有越界路径：{info.filename}")
    zf.extractall(dest)


async def restore_backup(path: Path) -> dict[str, Any]:
    """恢复备份：校验 → 把当前库另存一份 → 换库 → 覆盖素材。

    恢复完必须重启应用：进程里还握着旧库的连接，运行中热替换只会得到
    「一半旧一半新」的状态。所以这里只做文件层的事，返回 `needsRestart`。
    """
    info = inspect_backup(path)

    with tempfile.TemporaryDirectory(prefix="xiaoma-restore-") as tmp:
        staging = Path(tmp)
        with zipfile.ZipFile(path) as zf:
            _safe_extract(zf, staging)

        # 换库之前先把当前这一份存下来：恢复本身也可能是误操作
        from app.database import engine

        await engine.dispose()
        if settings.database_path.exists():
            keep = backup_dir() / f"pre-restore-{_now_tag()}.db"
            shutil.copy2(settings.database_path, keep)
        shutil.copy2(staging / "xiaoma.db", settings.database_path)

        restored_files = 0
        src_storage = staging / "storage"
        if src_storage.is_dir():
            for src in src_storage.rglob("*"):
                if not src.is_file():
                    continue
                rel = src.relative_to(src_storage)
                target = settings.storage_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
                restored_files += 1

    logger.info("已恢复备份 %s（素材 %s 个），需要重启应用", path.name, restored_files)
    return {
        **info,
        "restored": True,
        "restoredFiles": restored_files,
        "message": "恢复完成，请重启应用。" + (info.get("hint") or ""),
    }
