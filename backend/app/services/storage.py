"""本地文件存储：所有产物保存在数据目录 storage/ 下，按月份分目录。"""

from __future__ import annotations

import subprocess
import sys
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
    # 音频这几个扩展名不是装饰：`/media/` 是 StaticFiles 按扩展名猜 MIME 的，
    # 存成 .bin 会让 `<audio>` 拿不到 audio/*，进度条与时长全废。
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
}


def _ext(content_type: str, fallback: str = "bin") -> str:
    return _EXT_BY_CT.get((content_type or "").lower(), fallback)


def image_size_kwargs(data: bytes, content_type: str = "") -> dict[str, int]:
    """给 `Asset(...)` 用的宽高参数；不是图片或读不出来就返回空字典。

    宽高不是装饰性字段：图生视频前要拿首帧的比例与分辨率去跟所选画幅对帐
    （见 `preflight`），读不到就只能闭嘴不提醒。所以**每个存图片的地方都带上它**——
    放在这里是为了别让五六个调用点各写一遍再慢慢漂移。
    视频的宽高归 ffprobe 管（见 `director`），这里不碰。
    """
    from app.services.image_size import read_image_size

    size = read_image_size(data, content_type)
    if size is None:
        return {}
    return {"width": size[0], "height": size[1]}


def reveal_in_file_manager(path: Path) -> bool:
    """在系统文件管理器里定位到该文件，返回是否成功唤起。

    自托管应用的产物就在本机，「产物到底存哪了」是个真问题——这个动作省掉用户
    自己去找路径。**只负责唤起，不碰任何文件**；调用方必须先确认路径在数据目录内
    （这类用系统命令打开路径的能力，一旦能指到任意路径就成了信息暴露口子）。

    容器里通常没有文件管理器，那时返回 False，界面提示用户手动去数据目录看。
    """
    try:
        if sys.platform.startswith("win"):
            # explorer 的 /select 是「打开所在目录并选中该文件」
            subprocess.Popen(["explorer", "/select,", str(path)])  # noqa: S603,S607
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])  # noqa: S603,S607
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])  # noqa: S603,S607
    except Exception:  # noqa: BLE001
        return False
    return True


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
    if ct.startswith("audio/"):
        return "audio"
    return "other"


def ext_for_video_url(url: str) -> str:
    tail = url.split("?")[0].lower()
    for ext in ("mp4", "webm", "mov"):
        if tail.endswith("." + ext):
            return ext
    return "mp4"


def audio_ext(content_type: str) -> str:
    """音频扩展名。认不出来的类型按 mp3 处理。

    宁可猜错也不能落成 `.bin`：`/media/` 按扩展名给 MIME，`.bin` 会让浏览器拿到
    `application/octet-stream`，`<audio>` 直接不认（后端一声不吭，用户只看到「放不了」）。
    """
    return _ext(content_type, "mp3")
