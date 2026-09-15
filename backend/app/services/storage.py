"""本地文件存储：所有产物保存在数据目录 storage/ 下，按月份分目录。"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from app.config import settings

_EXT_BY_CT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
}


def _ext(content_type: str, fallback: str = "bin") -> str:
    return _EXT_BY_CT.get((content_type or "").lower(), fallback)


def save_bytes(data: bytes, content_type: str, preferred_ext: str | None = None) -> str:
    """写入文件，返回相对 storage 根目录的路径（POSIX 风格，用于 URL）。"""
    month = datetime.now().strftime("%Y-%m")
    folder = settings.storage_dir / month
    folder.mkdir(parents=True, exist_ok=True)
    ext = preferred_ext or _ext(content_type)
    name = f"{uuid.uuid4().hex}.{ext}"
    (folder / name).write_bytes(data)
    return f"{month}/{name}"


def abs_path(rel_path: str) -> Path:
    return settings.storage_dir / rel_path


def delete(rel_path: str) -> None:
    try:
        abs_path(rel_path).unlink(missing_ok=True)
    except OSError:
        pass


def guess_kind(content_type: str) -> str:
    ct = (content_type or "").lower()
    if ct.startswith("image/"):
        return "image"
    if ct.startswith("video/"):
        return "video"
    return "other"


def ext_for_video_url(url: str) -> str:
    tail = url.split("?")[0].lower()
    for ext in ("mp4", "webm", "mov"):
        if tail.endswith("." + ext):
            return ext
    return "mp4"
