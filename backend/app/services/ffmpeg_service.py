"""FFmpeg 能力封装：二进制解析 / 检测 / 探测 / 截取 / 缩略图 / 合并。

所有命令走 asyncio 子进程，带超时；文件全部落在 storage 目录内。

二进制解析顺序：
1. 配置 limits.ffmpeg_path 显式指定
2. PATH 中的 ffmpeg（校验必需能力：libx264 / aac 编码器）
3. Windows 常见安装位置（winget / scoop / 手动安装），逐一校验能力
避免 PATH 指向精简构建（如某些 IDE 自带）导致导演台功能残缺。
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import re
import shutil
from pathlib import Path

from app.services import storage

logger = logging.getLogger(__name__)

_BIN_CACHE: tuple[str | None, str | None] | None = None  # (ffmpeg, ffprobe)
_TIMEOUT_PROBE = 30
_TIMEOUT_FRAME = 60
_TIMEOUT_CUT = 600
_TIMEOUT_MERGE = 1800

_WIN_CANDIDATES = [
    r"{LOCALAPPDATA}\Microsoft\WinGet\Links\ffmpeg.exe",
    r"{LOCALAPPDATA}\Microsoft\WinGet\Packages\*ffmpeg*\ffmpeg*\bin\ffmpeg.exe",
    r"{LOCALAPPDATA}\Microsoft\WinGet\Packages\*ffmpeg*\bin\ffmpeg.exe",
    r"{ProgramFiles}\ffmpeg\bin\ffmpeg.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"{USERPROFILE}\scoop\shims\ffmpeg.exe",
]


def _ffmpeg_path_from_config() -> str:
    from app.services.config_center_service import runtime_value

    try:
        v = (runtime_value("limits.ffmpeg_path", "") or "").strip()
    except Exception:
        v = ""
    return v


async def _probe_caps(binary: str) -> set[str] | None:
    """读取编码器清单；命令不可执行返回 None。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "-hide_banner", "-encoders",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except (FileNotFoundError, OSError, asyncio.TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    text = (out or b"").decode("utf-8", "replace")
    caps = set()
    if re.search(r"\blibx264\b", text):
        caps.add("libx264")
    if re.search(r"\baac\b", text):
        caps.add("aac")
    return caps


async def _resolve_binaries() -> tuple[str | None, str | None]:
    global _BIN_CACHE
    if _BIN_CACHE is not None:
        return _BIN_CACHE

    candidates: list[str] = []
    override = _ffmpeg_path_from_config()
    if override:
        candidates.append(override)
    which = shutil.which("ffmpeg")
    if which:
        candidates.append(which)
    import os

    for pattern in _WIN_CANDIDATES:
        tpl = pattern.format(**{k: os.environ.get(k, "") for k in ("LOCALAPPDATA", "ProgramFiles", "USERPROFILE")})
        for hit in sorted(glob.glob(tpl), reverse=True):
            candidates.append(hit)

    best: tuple[str, Path, set[str]] | None = None
    for c in candidates:
        caps = await _probe_caps(c)
        if caps is None:
            continue
        if best is None or len(caps) > len(best[2]):
            best = (c, Path(c), caps)
        # 能力齐全即停
        if caps >= {"libx264", "aac"}:
            break

    if best is None:
        logger.warning("未找到可用的 ffmpeg（已尝试：%s）", candidates[:5])
        _BIN_CACHE = (None, None)
        return _BIN_CACHE

    binary, path, caps = best
    if caps < {"libx264", "aac"}:
        logger.warning("ffmpeg 能力不全（%s）：%s，重编码功能可能不可用", caps, binary)

    ffprobe = str(path.with_name("ffprobe.exe")) if path.suffix == ".exe" else str(path.with_name("ffprobe"))
    if not Path(ffprobe).exists():
        ffprobe = shutil.which("ffprobe") or ""
    _BIN_CACHE = (binary, ffprobe or None)
    logger.info("导演台使用 ffmpeg：%s", binary)
    return _BIN_CACHE


def _reset_cache() -> None:
    """配置变更后调用，强制重新解析。"""
    global _BIN_CACHE
    _BIN_CACHE = None


async def _run(cmd: list[str], timeout: int) -> tuple[bool, str]:
    """运行外部命令，返回 (成功, stderr 尾部)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, "系统未安装 ffmpeg / ffprobe"
    except OSError as e:
        return False, f"命令启动失败：{e}"
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return False, "处理超时，已终止"
    if proc.returncode != 0:
        tail = (stderr or b"")[-500:].decode("utf-8", "replace").strip()
        return False, tail or f"退出码 {proc.returncode}"
    return True, ""


async def ffmpeg_available() -> tuple[bool, str | None]:
    """返回 (是否可用, 版本号)。二进制解析结果缓存。"""
    ffmpeg, _ = await _resolve_binaries()
    if not ffmpeg:
        return False, None
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return False, None
    if proc.returncode != 0:
        return False, None
    m = re.search(rb"ffmpeg version (\S+)", out or b"")
    return True, m.group(1).decode() if m else "unknown"


async def probe(path: Path) -> dict:
    """ffprobe 读取视频元信息：时长 / 宽高 / 编码。失败时返回空 dict。"""
    _, ffprobe = await _resolve_binaries()
    if not ffprobe:
        return {}
    cmd = [
        ffprobe, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_PROBE)
        data = json.loads(out.decode("utf-8", "replace"))
    except (FileNotFoundError, asyncio.TimeoutError, json.JSONDecodeError, OSError):
        return {}
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    fmt = data.get("format", {})
    duration = fmt.get("duration") or video.get("duration")
    return {
        "duration": float(duration) if duration else None,
        "width": video.get("width"),
        "height": video.get("height"),
        "codec": video.get("codec_name"),
    }


def _out_path(ext: str) -> Path:
    """生成 storage 内的绝对输出路径。"""
    rel = storage.save_bytes(b"", f"video/{ext}", preferred_ext=ext)
    p = storage.abs_path(rel)
    p.unlink(missing_ok=True)
    return p


def _save_asset_file(path: Path, ext: str) -> str:
    """把处理产物登记进 storage 目录（path 已在其中），返回相对路径。"""
    return path.relative_to(settings_storage_dir()).as_posix()


def settings_storage_dir() -> Path:
    from app.config import settings

    return settings.storage_dir


async def extract_clip(src: Path, start: float, end: float) -> Path:
    """截取 [start, end) 片段。优先流拷贝（快、无损），失败回退重编码。"""
    ffmpeg, _ = await _resolve_binaries()
    out = _out_path("mp4")
    ok, err = await _run(
        [
            ffmpeg, "-y",
            "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
            "-i", str(src),
            "-c", "copy",
            str(out),
        ],
        _TIMEOUT_CUT,
    )
    if ok and out.exists() and out.stat().st_size > 0:
        return out
    logger.info("流拷贝截取失败，回退重编码：%s", err)
    ok, err = await _run(
        [
            ffmpeg, "-y",
            "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
            "-i", str(src),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac",
            str(out),
        ],
        _TIMEOUT_CUT,
    )
    if not ok:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"截取片段失败：{err}")
    return out


async def extract_frame(src: Path, t: float) -> Path:
    """提取 t 秒处的一帧为 jpg。"""
    ffmpeg, _ = await _resolve_binaries()
    out = _out_path("jpg")
    ok, err = await _run(
        [ffmpeg, "-y", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1", "-q:v", "3", str(out)],
        _TIMEOUT_FRAME,
    )
    if not ok:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"提取画面失败：{err}")
    return out


async def merge_videos(paths: list[Path]) -> Path:
    """按顺序合并多个视频。优先 concat + 流拷贝；失败回退 concat 滤镜重编码。"""
    ffmpeg, _ = await _resolve_binaries()
    out = _out_path("mp4")

    # 方案一：concat demuxer + copy（同源片段最快）
    list_file = out.with_suffix(".txt")
    list_file.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in paths), encoding="utf-8"
    )
    try:
        ok, err = await _run(
            [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out)],
            _TIMEOUT_MERGE,
        )
        if ok and out.exists() and out.stat().st_size > 0:
            return out
        logger.info("流拷贝合并失败，回退重编码：%s", err)

        # 方案二：concat 滤镜（带音轨）
        inputs: list[str] = []
        for p in paths:
            inputs.extend(["-i", str(p)])
        n = len(paths)
        parts = "".join(f"[{i}:v][{i}:a]" for i in range(n))
        ok, err = await _run(
            [
                ffmpeg, "-y", *inputs,
                "-filter_complex", f"{parts}concat=n={n}:v=1:a=1",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac",
                str(out),
            ],
            _TIMEOUT_MERGE,
        )
        if ok and out.exists() and out.stat().st_size > 0:
            return out
        logger.info("带音轨合并失败，尝试丢弃音轨：%s", err)

        # 方案三：无音轨素材
        parts = "".join(f"[{i}:v]" for i in range(n))
        ok, err = await _run(
            [
                ffmpeg, "-y", *inputs,
                "-filter_complex", f"{parts}concat=n={n}:v=1:a=0",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-an",
                str(out),
            ],
            _TIMEOUT_MERGE,
        )
        if not ok:
            raise RuntimeError(f"合并视频失败：{err}")
        return out
    finally:
        list_file.unlink(missing_ok=True)
