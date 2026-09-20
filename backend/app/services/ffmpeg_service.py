"""FFmpeg 能力封装：二进制解析 / 检测 / 探测 / 截取 / 缩略图 / 合并 / 静图缓动样片。

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
import uuid
from pathlib import Path

from app.services import animatic, storage, transitions

logger = logging.getLogger(__name__)

_BIN_CACHE: tuple[str | None, str | None] | None = None  # (ffmpeg, ffprobe)
_TIMEOUT_PROBE = 30
_TIMEOUT_FRAME = 60
_TIMEOUT_CUT = 600
_TIMEOUT_MERGE = 1800
# 单镜缓动片段：一镜最长 12 秒，正常几秒就完；给 5 分钟是留给很慢的机器
_TIMEOUT_ANIMATIC_CLIP = 300
# 整条样片（含最后的合并）：上限 120 秒的画面，慢机器也够
_TIMEOUT_ANIMATIC_TOTAL = 2400

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
    global _BIN_CACHE, _SUBTITLE_FILTER
    _BIN_CACHE = None
    _SUBTITLE_FILTER = None


# 字幕滤镜探测结果。`None` = 还没探过；缓存是因为这个结论在一次进程生命周期里不会变
# （ffmpeg 是启动时解析出来的那个），而它每次都要起一个外部进程去问、约几十毫秒——
# 开一次界面就问一次是白等的。
_SUBTITLE_FILTER: bool | None = None


def subtitle_filter_available() -> bool:
    """ffmpeg 有没有字幕滤镜（`ass` / `subtitles`，即 libass）。

    **为什么必须探而不是假定有**：`ass` 与 `subtitles` 依赖 libass，而各种 ffmpeg 构建
    并不都带（本项目在另一台机器上就撞到过一个只有 `scale,fps` 的裁剪版）。
    不探的话，用户会在点下「导出」之后才拿到一句 `No such filter: 'ass'`——
    那时候片子已经渲染了一半。

    同步函数：它只读一个**已经缓存**的结论，不在请求线程里跑外部命令；结论由
    `warm_subtitle_filter()` 在应用启动时预热。
    """
    return bool(_SUBTITLE_FILTER)


async def warm_subtitle_filter() -> bool:
    """预热字幕滤镜探测（应用启动时调一次）。"""
    global _SUBTITLE_FILTER
    if _SUBTITLE_FILTER is not None:
        return _SUBTITLE_FILTER
    ffmpeg, _ = await _resolve_binaries()
    if not ffmpeg:
        _SUBTITLE_FILTER = False
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-filters",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except (FileNotFoundError, OSError, asyncio.TimeoutError):
        _SUBTITLE_FILTER = False
        return False
    text = (out or b"").decode("utf-8", "replace")
    # 只认滤镜名那一列，避免把构建说明里的 "libass" 字样算成有滤镜
    _SUBTITLE_FILTER = bool(re.search(r"^\s*\S*\s+(ass|subtitles)\s+V->V\b", text, re.M))
    logger.info("字幕滤镜探测：ass/subtitles %s", "可用" if _SUBTITLE_FILTER else "不可用")
    return _SUBTITLE_FILTER


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
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    duration = fmt.get("duration") or video.get("duration")
    # 帧率：转场（xfade）要求两条流帧率一致，混杂素材要先归一化，所以这里得报出来。
    # `avg_frame_rate` 是「分子/分母」形式的字符串，0/0 表示读不出来
    fps = None
    rate = str(video.get("avg_frame_rate") or "")
    num, _, den = rate.partition("/")
    try:
        if float(den):
            fps = round(float(num) / float(den), 3)
    except ValueError:
        fps = None
    return {
        "duration": float(duration) if duration else None,
        "width": video.get("width"),
        "height": video.get("height"),
        "codec": video.get("codec_name"),
        # 有没有音轨：合并时决定「要不要给它补一条静音」（见 _merge_with_transition）
        "has_audio": audio is not None,
        "has_video": bool(video),
        "fps": fps,
        # 音轨的编码参数：`_copy_concat_blocker` 靠它判断两段能不能直接接包
        "audio_codec": (audio or {}).get("codec_name") if audio else None,
        "audio_rate": _as_int((audio or {}).get("sample_rate")) if audio else None,
        "audio_channels": (audio or {}).get("channels") if audio else None,
    }


def _as_int(raw: object) -> int | None:
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


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


# ---------------------------------------------------------------- 转场与音效

_TIMEOUT_SFX = 120
# 统一帧率：xfade 要求两条流帧率一致。素材来自各家模型，帧率什么都有，
# 所以一律归一到 30（这是本项目所有生成视频的常见档位，重采样损失可以忽略）
_MERGE_FPS = 30


async def synth_sfx(kind: str, seconds: float) -> Path:
    """现场合成一小段音效，返回临时 wav 路径。

    **不下载任何素材**：配方就是几条 lavfi 表达式（见 `transitions.SFX_RECIPES`），
    所以没有授权问题、没有仓库体积问题、换台机器出来的是同一段声音，
    也不需要联网。想用真正的素材（比如自己收的 CC0 音效），走「选一条资产库音频」
    那条路——接口上是 `sfx_asset_id`，混音这条链路完全一样。
    """
    recipe = transitions.SFX_RECIPES.get(kind)
    if recipe is None:
        raise RuntimeError(f"没有这种音效：{kind}")
    source, extra = recipe
    ffmpeg, _ = await _resolve_binaries()
    out = _new_workdir("sfx") / f"sfx-{kind}.wav"

    # 尾巴一定要淡出：截断的正弦听起来是「咔」的一声，比没有音效更糟
    fade_out = f"afade=t=out:st={max(0.0, seconds - 0.18):.3f}:d=0.18"
    filters = ",".join(f for f in (extra, fade_out) if f)
    ok, err = await _run(
        [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", source,
            "-t", f"{seconds:.3f}",
            "-af", filters,
            "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
            str(out),
        ],
        _TIMEOUT_SFX,
    )
    if not ok or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"合成音效失败：{err}")
    return out


async def merge_videos(
    paths: list[Path],
    *,
    transition: str = "cut",
    transition_seconds: object = None,
    sfx_path: Path | None = None,
) -> Path:
    """按顺序合并多个视频。

    `transition` 留空 / `cut` 时是硬切，有两条路：**素材同构**时用 concat demuxer +
    流拷贝（最快、不重编码）；**不同构**时逐段归一化后重编码（见 `_merge_normalized`）。
    选了真正的转场则走 `_merge_with_transition`（也要重编码，慢一些）。

    `sfx_path` 是「转场处要贴的音效」——可以是我们现场合成的，也可以是用户从资产库里
    挑的一条音频；这里只认文件，谁来提供都一样。
    """
    key = transitions.sanitize_key(transition)
    if not transitions.is_cut(key):
        return await _merge_with_transition(
            paths,
            transition=key,
            seconds=transitions.sanitize_seconds(transition_seconds),
            sfx_path=sfx_path,
        )

    blocker = await _copy_concat_blocker(paths)
    if blocker is None:
        copied = await _merge_by_copy(paths)
        if copied is not None:
            return copied
        logger.info("流拷贝直拼失败，改用重编码合并")
    else:
        logger.info("素材不同构（%s），不走流拷贝直拼，改用重编码合并", blocker)
    return await _merge_normalized(paths)


async def _copy_concat_blocker(paths: list[Path]) -> str | None:
    """能不能用 concat demuxer + 流拷贝直拼。能则 `None`，不能则一句原因（进日志）。

    判据是**同构**：宽高 / 视频编码 / 帧率 / 有没有音轨 / 音轨编码参数全都要一致。

    为什么非要拦这一下：concat demuxer 是把各段的包**直接接起来**，不做任何转换。
    素材但凡不一致，接出来的片子时间戳就是错的——命令**照样成功返回**，于是
    「三段共 15 秒的素材，导出成 18.75 秒、音轨和画面对不上」这种结果是悄悄给出去的，
    不报错、也不会有任何提示。多编一遍画面，比这个强。

    读不出这些信息的（老文件、损坏文件）也一律不放行。
    """
    if len(paths) < 2:
        # 只有一段就没什么可「接」的，直拼必然是安全的（比如只有一镜的样片）
        return None
    infos = [await probe(p) for p in paths]
    first = infos[0]
    if not first.get("has_video") or not first.get("width") or not first.get("height"):
        return "第一段的视频信息读不出来"
    keys = (
        "width", "height", "codec", "fps",
        "has_audio", "audio_codec", "audio_rate", "audio_channels",
    )
    for i, info in enumerate(infos[1:], 2):
        if not info.get("has_video"):
            return f"第 {i} 段没有视频流"
        for k in keys:
            if info.get(k) != first.get(k):
                return f"第 {i} 段的 {k} 与第一段不同（{first.get(k)} vs {info.get(k)}）"
    return None


async def _merge_by_copy(paths: list[Path]) -> Path | None:
    """concat demuxer + 流拷贝直拼。**只在素材同构时可用**（调用方先问 `_copy_concat_blocker`）。

    失败回 `None`（交给重编码那条路），不在这里自己往下试别的写法。
    """
    ffmpeg, _ = await _resolve_binaries()
    out = _out_path("mp4")
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
        logger.info("流拷贝合并失败：%s", err)
        return None
    finally:
        list_file.unlink(missing_ok=True)


async def _merge_normalized(paths: list[Path]) -> Path:
    """不同构素材的硬切合并：逐段归一化再 `concat`。

    为什么不再沿用老的「concat 滤镜三级回退」：那一套在**有的段有音轨、有的段没有**
    时，第二级必然失败，然后退到第三级「整条丢掉音轨」——把本来有声音的段也一起弄哑，
    而且不报错。这里给缺音轨的段补一条等长静音，音轨就保住了。

    归一化本身与转场那条链**共用同一个 `_normalized_graph`**：两处各写一遍的话，
    迟早出现「转场那条链对得上、硬切那条链偏了」这种最难查的偏差。
    """
    ffmpeg, _ = await _resolve_binaries()
    out = _out_path("mp4")
    inputs, chains, vlabels, alabels, lens, _infos, _size = await _normalized_graph(paths)
    n = len(paths)
    chains.append(
        "".join(f"[{vlabels[i]}][{alabels[i]}]" for i in range(n))
        + f"concat=n={n}:v=1:a=1[vout][aout]"
    )
    ok, err = await _run(
        [
            ffmpeg, "-y", *inputs,
            "-filter_complex", ";".join(chains),
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(out),
        ],
        _TIMEOUT_MERGE,
    )
    if not ok or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"合并视频失败：{err}")
    logger.info("重编码合并完成：%s 段，成片约 %.2f 秒", n, sum(lens))
    return out


async def _normalized_graph(
    paths: list[Path],
) -> tuple[list[str], list[str], list[str], list[str], list[float], list[dict], tuple[int, int]]:
    """把 N 段素材归一化成「尺寸 / 帧率 / 像素格式一致，且每条都有一条等长音轨」的流。

    返回 `(ffmpeg 附加输入, 已建好的滤镜链, 视频标签, 音频标签, 各段时长, probe 结果, 目标尺寸)`。
    调用方接着往上叠自己的链（`concat` 或 `xfade`），最后 `";".join` 成 filter_complex。

    这里有四个「不这么做就会坏」的地方：

    1. **每段先归一化**（尺寸 / 帧率 / 像素格式 / SAR）：生成出来的素材什么尺寸什么帧率
       都有。`xfade` 要求两条流完全一致；`concat` 滤镜也要求参数相同，否则就是
       `Input link ... parameters do not match`，或者画面抖一下。
    2. **没有音轨的片段补一条等长静音**：不补的话 `acrossfade` 直接失败，`concat=v=1:a=1`
       也一样；退而求其次「整条丢掉音轨」则会把**有声音的片段也弄哑**，那是最坏的结果。
    3. **音轨裁到与画面等长再补静音**：素材的音轨常比画面短零点几秒，不补齐的话每接一次
       就累积一点偏移，越接越不对口。
    4. **`setpts` 重置时间戳**：`xfade` / `acrossfade` 都按 PTS 定位，素材自带的时间戳
       会让转场位置整体偏掉。

    目标尺寸以第一段为准；宽高取偶数（libx264 的 yuv420p 要求偶数，奇数会直接失败）。
    """
    infos = [await probe(p) for p in paths]
    durations = [i.get("duration") for i in infos]
    bad = [i for i, d in enumerate(durations) if not d or float(d) <= 0]
    if bad:
        i = bad[0]
        raise RuntimeError(
            f"读不出第 {i + 1} 段的时长，没法对齐各段。"
            "这一段可能是还没下载完或编码不完整——先确认它能正常播放再合并"
        )
    lens = [float(d) for d in durations]  # type: ignore[arg-type]

    width = int(infos[0].get("width") or 1280)
    height = int(infos[0].get("height") or 720)
    width -= width % 2
    height -= height % 2

    inputs: list[str] = []
    for p in paths:
        inputs += ["-i", str(p)]
    silent_idx: dict[int, int] = {}
    for i, info in enumerate(infos):
        if not info.get("has_audio"):
            silent_idx[i] = len(paths) + len(silent_idx)
            inputs += ["-f", "lavfi", "-t", f"{lens[i]:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]

    chains: list[str] = []
    for i, info in enumerate(infos):
        chains.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={_MERGE_FPS},"
            f"format=yuv420p,setpts=PTS-STARTPTS[v{i}]"
        )
        src = f"{i}:a" if info.get("has_audio") else f"{silent_idx[i]}:a"
        chains.append(
            f"[{src}]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"apad,atrim=0:{lens[i]:.3f},asetpts=N/SR/TB[a{i}]"
        )

    n = len(paths)
    return (
        inputs,
        chains,
        [f"v{i}" for i in range(n)],
        [f"a{i}" for i in range(n)],
        lens,
        infos,
        (width, height),
    )


async def _merge_with_transition(
    paths: list[Path],
    *,
    transition: str,
    seconds: float,
    sfx_path: Path | None = None,
) -> Path:
    """带转场地合并：`xfade` 接画面、`acrossfade` 接声音，可选在转场处贴一声音效。

    素材先过 `_normalized_graph`（归一化 / 补静音 / 对齐音轨长度 / 重置时间戳，
    四个坑都写在那里），这里只负责把转场叠上去。

    `transition` 收的是**预设 key**（`transitions.PRESETS` 里的那一列），不是 `xfade`
    的滤镜名——两者常常不一样（「过黑」的 key 是 `fade`、滤镜名是 `fadeblack`；
    `wipe` 压根没有同名的滤镜）。直接拿 key 当滤镜名会掉进两种坑：名字碰巧存在
    （`fade` 就是），于是「过黑」悄悄变成普通叠化，不报错；名字不存在（`wipe`），
    ffmpeg 才回一句看不懂的错。所以这里**必须**回过预设表取。
    """
    xfade_name = transitions.preset(transition)["xfade"]
    inputs, chains, vlabels, alabels, lens, _infos, _size = await _normalized_graph(paths)
    problem = transitions.check_clips(lens, transition, seconds)
    if problem:
        raise RuntimeError(problem)

    ffmpeg, _ = await _resolve_binaries()
    out = _out_path("mp4")

    # 音效在这条链上是**最后**一个输入（前面是各段素材 + 补的静音）
    sfx_idx = inputs.count("-i")
    if sfx_path is not None:
        inputs += ["-i", str(sfx_path)]

    # 画面：xfade 累进。offset = 「下一段从当前合成结果的第几秒开始叠进来」
    current_v = vlabels[0]
    running = lens[0]
    points: list[float] = []
    for i in range(1, len(paths)):
        offset = running - seconds
        points.append(offset)
        label = f"x{i}"
        chains.append(
            f"[{current_v}][{vlabels[i]}]xfade=transition={xfade_name}"
            f":duration={seconds}:offset={offset:.3f}[{label}]"
        )
        current_v = label
        running = running + lens[i] - seconds

    # 声音：acrossfade 链。它的总长同样等于 sum - (n-1)*d，所以与画面天然同步
    current_a = alabels[0]
    for i in range(1, len(paths)):
        label = f"ax{i}"
        chains.append(
            f"[{current_a}][{alabels[i]}]acrossfade=d={seconds}:c1=tri:c2=tri[{label}]"
        )
        current_a = label

    map_audio = current_a
    if sfx_path is not None and points:
        # 每个转场点贴一份：asplit 复制 n 份 → 各自 adelay 到转场起点 → amix 混回主音轨。
        # `duration=first` 让混音结果以主音轨为准（音效的尾巴不会把成片拖长）
        n = len(points)
        chains.append(
            f"[{sfx_idx}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"asplit={n}" + "".join(f"[s{k}]" for k in range(n))
        )
        for k, at in enumerate(points):
            ms = max(0, int(round(at * 1000)))
            chains.append(f"[s{k}]adelay={ms}|{ms}[d{k}]")
        chains.append(
            f"[{current_a}]" + "".join(f"[d{k}]" for k in range(n))
            + f"amix=inputs={n + 1}:duration=first:normalize=0[aout]"
        )
        map_audio = "aout"

    ok, err = await _run(
        [
            ffmpeg, "-y", *inputs,
            "-filter_complex", ";".join(chains),
            "-map", f"[{current_v}]", "-map", f"[{map_audio}]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(out),
        ],
        _TIMEOUT_MERGE,
    )
    if not ok or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"加转场合并失败：{err}")
    logger.info(
        "带转场合并完成：%s 段 / 转场 %s(%s) %.2fs，成片约 %.2f 秒（硬切会是 %.2f 秒）",
        len(paths), transition, xfade_name, seconds, running, sum(lens),
    )
    return out


# ---------------------------------------------------------------- 字幕烧录

_TIMEOUT_SUBS = 1800


async def burn_subtitles(
    src: Path,
    ass_text: str,
    *,
    font_keys: list[str],
    with_audio: bool = True,
) -> Path:
    """把一份 ASS 字幕烧进视频，返回成片路径。

    为什么走 ASS 而不是 `drawtext`：`drawtext` 要一条条手写时间轴与位置，而 ASS 本来
    就是为「多行、逐条时间轴、描边/底色/对齐」设计的，libass 一次渲染完——版式库那个
    模块产出的就是 ASS 文本，两边是同一件事的两半。

    三件在实现里必须做对的事（都有实测依据）：

    1. **`fontsdir=.` + 字体硬链接进工作目录**，不用绝对路径。filtergraph 有两层转义，
       Windows 绝对路径得写成 `C\\\\:/path`（二级反斜杠）才对，实测一级转义会失败。
       把 ass 与字体都放进工作目录、并把 ffmpeg 的 cwd 指过去，就完全绕开了这件事。
    2. **只挂这次用到的那几份字体**。libass 会把 `fontsdir` 里的字体全量解析：
       实测整库 80MB 要 0.16s、单份 0.08s。逐镜渲染时这个差价会累积。
    3. **`cwd` 必须设成工作目录**。`ass=sub.ass:fontsdir=.` 里的相对路径以进程 cwd 为基准，
       不设就会找不到文件——而且 ffmpeg 只会回一句 `fopen failed`，看不出是路径问题
       （这个坑在验收脚本里踩过两次）。
    """
    # 这里**必须自己预热一次**，不能读缓存：`subtitle_filter_available()` 是同步的、
    # 只读缓存，而缓存是应用启动时预热的。依赖「启动顺序」的话，任何在预热之前走到
    # 这条路的调用（单测、脚本、后台任务）都会把「还没探过」当成「没有滤镜」，
    # 报一句完全错误的理由。这个函数本身就是异步的，顺手探一次最省事。
    if not await warm_subtitle_filter():
        raise RuntimeError(
            "这台机器上的 ffmpeg 没有字幕滤镜（libass），烧不了字幕。"
            "换一个完整的 ffmpeg 构建再试"
        )
    ffmpeg, _ = await _resolve_binaries()
    if not ffmpeg:
        raise RuntimeError("未找到可用的 ffmpeg，烧不了字幕")

    # 延迟 import：`subtitle_fonts` 要用本模块的滤镜探测，模块级互相 import 会成环。
    # 字体只在真要烧字幕时才需要，放到这里没有额外代价。
    from app.services import subtitle_fonts

    # **字体缺了必须在这里就拦住。** 少了这一步，`stage_fonts` 只是「少挂几份」，
    # libass 会**静默回落系统字体**——最后出一部「有字幕、但不是你要的那个字体」的片子，
    # 而 ffmpeg 退出码是 0、没有任何异常。这类「不报错、悄悄给别的东西」正是最该拦的。
    problem = subtitle_fonts.check_ready(font_keys)
    if problem:
        raise RuntimeError(problem)

    work = _new_workdir("subs")
    _staged = subtitle_fonts.stage_fonts(work, font_keys)
    (work / "sub.ass").write_text(ass_text, encoding="utf-8")
    out = _out_path("mp4")

    cmd = [
        ffmpeg, "-y",
        "-i", str(src),
        "-vf", "ass=sub.ass:fontsdir=.",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
    ]
    cmd += ["-c:a", "copy"] if with_audio else ["-an"]
    cmd += ["-movflags", "+faststart", str(out)]

    ok, err = await _run_in(cmd, _TIMEOUT_SUBS, cwd=work)
    if not ok or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"烧字幕失败：{err}")
    logger.info("字幕烧录完成：挂载字体 %s，成片 %s", _staged, out.name)
    return out


async def render_subtitle_preview(
    ass_text: str,
    *,
    font_keys: list[str],
    width: int,
    height: int,
) -> bytes:
    """渲一张带字幕的**静帧预览图**，直接回字节（JPEG）。

    为什么是「真渲染一帧」而不是让前端拿 CSS 近似画一下：版式的效果取决于 libass 的
    字体选择、字形、描边、缩放、边距，前端再写一套必然对不上——「预览看着挺好、导出来
    不一样」是最伤信任的一种不一致。真渲一帧的成本只有几十毫秒。

    底色用深灰而不是纯黑：纯黑上看不清黑色描边，而描边恰恰是版式的一部分。

    **回字节而不是回路径**：预览不该进资产库（用一次就废，进了库就是一堆垃圾），
    而落了盘又不登记就会变成谁也管不到的孤儿文件。直接读进内存、把临时目录删掉最干净。
    """
    import shutil

    from app.services import subtitle_fonts

    problem = subtitle_fonts.check_ready(font_keys)
    if problem:
        raise RuntimeError(problem)

    ffmpeg, _ = await _resolve_binaries()
    if not ffmpeg:
        raise RuntimeError("未找到可用的 ffmpeg，渲不了预览")
    w = max(320, min(3840, int(width or 1280)))
    h = max(180, min(2160, int(height or 720)))
    work = _new_workdir("subprev")
    try:
        subtitle_fonts.stage_fonts(work, font_keys)
        (work / "sub.ass").write_text(ass_text, encoding="utf-8")
        out = work / "preview.jpg"
        ok, err = await _run_in(
            [ffmpeg, "-y",
             "-f", "lavfi", "-i", f"color=c=0x1E2430:s={w}x{h}:d=1",
             "-vf", "ass=sub.ass:fontsdir=.",
             "-frames:v", "1", "-q:v", "3", str(out)],
            120, cwd=work,
        )
        if not ok or not out.exists() or out.stat().st_size == 0:
            raise RuntimeError(f"渲染字幕预览失败：{err}")
        return out.read_bytes()
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def _run_in(cmd: list[str], timeout: int, *, cwd: Path) -> tuple[bool, str]:
    """在指定工作目录里跑命令。

    单开一个而不是给 `_run` 加参数：`_run` 被十几处调用，加一个只在字幕这条链上用到的
    参数会让每处都得想一下「我该不该传 cwd」，而字幕（还有它的预览）是唯一需要限定
    相对路径基准的场景。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
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
        tail = (stderr or b"")[-600:].decode("utf-8", "replace").strip()
        return False, tail or f"退出码 {proc.returncode}"
    return True, ""


# ---------------------------------------------------------------- 音频

_TIMEOUT_MUX = 600


async def probe_audio_seconds(path: Path) -> float | None:
    """音频时长（秒）。读不出来返回 None。

    **不抛异常**：时长只用来在界面上显示、以及在报告里对帐（旁白比画面长多久）。
    为了一个装饰性数字把整个配音/出片流程判失败，代价完全不成比例。
    """
    _, ffprobe = await _resolve_binaries()
    if not ffprobe:
        return None
    cmd = [
        ffprobe, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_PROBE)
        data = json.loads(out.decode("utf-8", "replace"))
    except (FileNotFoundError, asyncio.TimeoutError, json.JSONDecodeError, OSError):
        return None
    fmt = data.get("format", {})
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    duration = fmt.get("duration") or audio.get("duration")
    try:
        value = float(duration)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


async def mux_audio(video: Path, audio: Path) -> Path:
    """把一条音轨封进无声成片，返回新文件路径。

    四个参数都是有意的：

    - `-c:v copy`：画面已经渲染好了，再编一遍只会掉质量、多花时间；
    - `-c:a aac`：mp4 容器里兼容性最好的一档（用户手上的音轨可能是 wav/mp3）；
    - `-af apad`：旁白比画面**短**时用静音补到画面结束。没有它，`-shortest`
      会在旁白读完那一刻把整个输出截断——用户要的是一条 9 秒的样片，
      配上一段 4 秒的旁白就只剩 4 秒画面，这个代价完全不成比例；
    - `-shortest`：旁白比画面**长**时在画面结束处收尾（报告里会说清楚截了多久）。
      **不做静默拉伸**：把旁白拉慢去凑时长，听起来会很怪。
    """
    ffmpeg, _ = await _resolve_binaries()
    if not ffmpeg:
        raise RuntimeError("未找到可用的 ffmpeg，无法把旁白合进样片")
    out = _out_path("mp4")
    ok, err = await _run(
        [
            ffmpeg, "-y",
            "-i", str(video),
            "-i", str(audio),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac",
            "-af", "apad",
            "-shortest",
            str(out),
        ],
        _TIMEOUT_MUX,
    )
    if not ok or not out.exists() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"把旁白合进样片失败：{err}")
    return out


# ---------------------------------------------------------------- 静图缓动样片


def _new_workdir(prefix: str = "animatic") -> Path:
    """给一次本地渲染开一个临时目录（在 storage 内，结束时整体删掉）。

    放在 storage 内是为了守住本模块「文件全落在 storage 目录内」的约定；
    目录名带 uuid，两次渲染不会互相踩。`prefix` 只是给排查时看的东西起个名
    （样片是 animatic、转场音效是 sfx）。
    """
    work = settings_storage_dir() / "tmp" / f"{prefix}-{uuid.uuid4().hex[:12]}"
    work.mkdir(parents=True, exist_ok=True)
    return work


async def render_animatic(
    items: list[tuple[Path, animatic.AnimaticClip, tuple[int, int]]],
    *,
    out_size: tuple[int, int],
    audio: Path | None = None,
) -> Path:
    """把「图片 + 镜头参数」逐镜渲染成缓动片段，再合成一条样片。

    `items` 是 `(图片路径, 镜头, 源图尺寸)`。**逐镜渲染再合并**而不是一条巨型
    filter_complex：哪个镜头的表达式写错了就单独报哪个镜头，而不是整条命令一句
    「Invalid argument」；代价是中间多落几个临时文件，收尾统一删掉。

    合成的活交给 `merge_videos`（流拷贝 → 带音轨重编码 → 无音轨重编码三级回退），
    本模块不再写第四套合并逻辑。

    `audio` 给的是旁白音轨。**单镜片段保持 `-an`、只在最后封一次音轨**：
    给每镜都塞一条空音轨，会让合并走到「带音轨重编码」那一档，白白多编一遍画面。
    """
    if not items:
        raise RuntimeError("没有可渲染的镜头")

    ffmpeg, _ = await _resolve_binaries()
    if not ffmpeg:
        raise RuntimeError("未找到可用的 ffmpeg，无法出样片")

    work = _new_workdir()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _TIMEOUT_ANIMATIC_TOTAL
    try:
        parts: list[Path] = []
        for idx, (image, clip, src_size) in enumerate(items, 1):
            left = deadline - loop.time()
            if left <= 1:
                raise RuntimeError(
                    f"渲染超时（超过 {_TIMEOUT_ANIMATIC_TOTAL // 60} 分钟）："
                    "请减少镜数或调短每镜时长后重试"
                )
            out = work / f"{idx:02d}.mp4"
            cmd = animatic.clip_command(
                ffmpeg, image, out, clip, src_size=src_size, out_size=out_size
            )
            ok, err = await _run(cmd, int(min(_TIMEOUT_ANIMATIC_CLIP, left)))
            if not ok or not out.exists() or out.stat().st_size == 0:
                out.unlink(missing_ok=True)
                raise RuntimeError(f"镜头 {clip.shot_no} 渲染失败：{err}")
            parts.append(out)

        merged = await merge_videos(parts)
        if audio is not None:
            voiced = await mux_audio(merged, audio)
            # 无声那版是中间产物，封完就删：它已经没用了，留着只会让用户困惑
            merged.unlink(missing_ok=True)
            merged = voiced
        logger.info(
            "静图样片渲染完成：%s 镜 → %s%s",
            len(parts), merged.name, "（带旁白）" if audio is not None else "",
        )
        return merged
    finally:
        shutil.rmtree(work, ignore_errors=True)

