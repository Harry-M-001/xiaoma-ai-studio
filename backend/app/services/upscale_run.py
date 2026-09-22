"""超分的执行层：真跑那两个 ncnn-vulkan 程序，视频还要自己走三段式。

与 `upscale.py`（纯口径）分工：这边才有子进程、临时目录、ffmpeg 与 storage。

## 三段式（视频那条，上游 README_windows.md 写的就是这三步）

    ffmpeg 拆帧 → realesrgan/waifu2x 对**目录**批量放大 → ffmpeg 合帧并把原音轨拷回来

四个只有真跑才知道的细节：

1. **输出目录要预先建出来。** 实测输出目录不存在时会直接报
   `invalid outputpath extension type`（它按输出名的后缀判格式，而目录名没有后缀）；
   目录存在、并且显式给 `-f png`，就稳了。
2. **帧率必须按 ffprobe 读到的真实值回填。** 上游 README 的示例里写死 `-r 23.98`，
   那是它自己 demo 的帧率——照抄就是把用户的视频改成另一个速度。
3. **音轨用 `-map 1:a:0?` 拷回来**，那个问号是必需的：没有音轨的片子（静图样片、
   无声成片）走 `-map 1:a:0` 会直接失败。
4. **进程的输出里一半是设备清单**（每台设备四行 `[0 NVIDIA...]`）。直接把它当 stderr
   贴给用户，用户看到的就是一屏设备信息而真正的错在最后一行。所以要先把那些行滤掉。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.config import settings
from app.services import ffmpeg_service, image_size, storage
from app.services import upscale as up

logger = logging.getLogger(__name__)

# 单张图片的超时：估值的 4 倍，至少 5 分钟（第一张要加载模型，会慢一些）
_MIN_IMAGE_TIMEOUT = 300.0
# 视频：估值 × 4，至少 20 分钟
_MIN_VIDEO_TIMEOUT = 1200.0

# 设备清单缓存：跑一次 exe 就能拿到，但它要起一次显卡上下文（约 1 秒），
# 而弹窗一打开就要用。同一台机器上设备不会变，缓存久一点没关系。
_DEVICE_TTL = 900.0
_devices: dict[str, tuple[float, list[up.Device]]] = {}

# 设备清单的行：`[0 NVIDIA GeForce RTX 5060 Laptop GPU]  queueC=2[8]  queueT=1[2]`
_DEVICE_LINE = re.compile(r"^\[\d+\s")


def _out_path(ext: str) -> Path:
    """在 storage 内开一个空文件并返回绝对路径（与 `ffmpeg_service._out_path` 同一条约定）。"""
    rel = storage.save_bytes(b"", f"image/{ext}", preferred_ext=ext)
    path = storage.abs_path(rel)
    path.unlink(missing_ok=True)
    return path


def _save_asset_file(path: Path) -> str:
    return path.relative_to(settings.storage_dir).as_posix()


def _new_workdir(prefix: str = "upscale") -> Path:
    work = settings.storage_dir / "tmp" / f"{prefix}-{uuid.uuid4().hex[:12]}"
    work.mkdir(parents=True, exist_ok=True)
    return work


def _error_tail(text: str, limit: int = 400) -> str:
    """把子进程输出收成一句能看的错。

    **先把设备清单滤掉**：那是每台设备四行、一次十几行的固定输出，不滤掉的话
    真正的错会被淹在最前面，用户拿到的「尾部」全是显卡参数。
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    keep = [ln for ln in lines if not _DEVICE_LINE.match(ln)]
    # 它也会打一堆 `0.00%` 的进度行，同样不是错
    keep = [ln for ln in keep if not re.fullmatch(r"[\d.]+%", ln)]
    tail = " / ".join(keep[-4:]) if keep else "（它没有输出任何信息）"
    return tail[:limit]


def _kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    """杀掉子进程并回收。

    只 kill 不 wait 会留下僵尸进程和没关的管道（表现为 asyncio 报一堆
    `unclosed transport`，以及进程句柄挂着不放）——与 v1.1.30 本机配音同一条。
    """
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        return
    with contextlib.suppress(Exception):
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()


async def _reap(proc: asyncio.subprocess.Process) -> None:
    try:
        await asyncio.wait_for(proc.wait(), timeout=15)
    except (asyncio.TimeoutError, ProcessLookupError, OSError):
        pass


def _number_frames(frames: Path) -> str:
    """把目录里的产物帧改名成 `frame00000001.png` 这样的连续序号，返回序号模板。

    为什么要做：合帧那一步用的是 `frame%08d.png` 这个模式，而**引擎给产物起什么名
    我们不掌握**（目录模式下它通常沿用输入的文件名，但那是它的实现细节，不是承诺）。
    沿用得上就什么都不做（下面会直接返回）；沿用不上就按排序后的顺序重编一遍。

    重命名是原地改目录项、不搬数据，所以再长的片子也只是一个 O(帧数) 的目录操作。
    **分两趟做**是为了避免改名撞车（`a.png` → `b.png` 时 `b.png` 可能还在）。

    返回的是**带真实后缀**的模板：我们给 `-f png`，但它给什么后缀就以什么为准，
    否则合帧那一步会一个文件都匹配不到。
    """
    files = sorted(p for p in frames.iterdir() if p.is_file())
    suffix = files[0].suffix if files else ".png"
    pattern = frames / f"frame%08d{suffix}"
    want = [f"frame{i:08d}{suffix}" for i in range(1, len(files) + 1)]
    if [p.name for p in files] == want:
        return str(pattern)
    staging: list[tuple[Path, Path]] = []
    for i, path in enumerate(files):
        tmp = frames / f".renumber-{i:08d}"
        path.rename(tmp)
        staging.append((tmp, frames / want[i]))
    for tmp, final in staging:
        tmp.rename(final)
    return str(pattern)


# ------------------------------------------------------------------ 设备


#: 问设备清单用的参数。
#:
#: **不能用「不带参数运行」**——实测那样它只打用法表（857 字符里一行设备都没有），
#: 因为设备清单是在它真正开始初始化 Vulkan 时才打的。这里的组合会走到那一步、
#: 然后被无效的设备号挡回来，是最便宜的一种问法（实测 0.2 秒，比丢一个不存在的
#: 输入文件快 4 倍），而且**不会碰 GPU 也不会加载模型**。
_DEVICE_PROBE_ARGS = ["-g", "99", "-i", "nope.png", "-o", "nope.png"]


async def devices(engine_key: str, *, force: bool = False) -> list[up.Device]:
    """问一次 exe「这台机器上有哪些计算设备」。

    **从它的输出里读，不问 WMI。** 理由：WMI 报的是系统里的显卡，
    而这个 exe 报的是它**真能用的** Vulkan 设备——两者不一定一样（实测这台机器上
    它报 3 个，其中两个是同一块集显的两个入口），而用户要选的是后者。

    拿不到就回空列表（界面只显示「自动」）：**没有清单也不要编一个出来**。
    """
    cached = _devices.get(engine_key)
    if cached and not force and time.monotonic() - cached[0] < _DEVICE_TTL:
        return cached[1]

    exe = up.exe_path(engine_key)
    if exe is None:
        return []
    text = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(exe), *_DEVICE_PROBE_ARGS, cwd=str(exe.parent),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            text = out.decode("utf-8", "replace")
        except asyncio.TimeoutError:
            _kill_and_reap(proc)
            await _reap(proc)
    except (FileNotFoundError, OSError) as e:
        logger.warning("问设备清单失败（%s）：%s", engine_key, e)
        return []

    found = up.parse_devices(text)
    _devices[engine_key] = (time.monotonic(), found)
    if not found:
        logger.info("%s 没有报出任何计算设备（输出 %d 字符）", engine_key, len(text))
    return found


# ------------------------------------------------------------------ 跑一次


async def _run_exe(
    args: list[str],
    *,
    cwd: Path,
    timeout: float,
    beat: Callable[[], Awaitable[bool]] | None = None,
    tick_every: float = 3.0,
) -> str:
    """跑一次引擎，返回它的输出。**不看退出码**（口径 5），产物由调用方验。

    `beat` 是心跳：每 `tick_every` 秒问一次「还要不要继续」，并且顺便把进度报出去。
    **进度与取消合成一个回调**是有意的：两者都要碰一次数据库，分成两个回调就是两次。

    为什么需要「问」而不是靠 `asyncio` 的取消：任务中心那个「取消」只把数据库里的
    状态改成 `cancelled`（见 `routers/generation.cancel_task`），**它不会取消协程**。
    没有这个心跳的话，用户点了取消之后进程照跑——一段视频能白烧几十分钟显卡。
    """
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    chunks: list[bytes] = []

    async def _drain() -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            chunks.append(line)
            if len(chunks) > 4000:
                del chunks[:2000]

    drain = asyncio.create_task(_drain())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while proc.returncode is None:
            if loop.time() > deadline:
                raise TimeoutError(
                    f"跑了 {int(timeout)} 秒还没结束，已中止。"
                    "如果这段素材特别大，先剪短一点或降一档倍数"
                )
            if beat is not None and not await beat():
                proc.terminate()
                raise asyncio.CancelledError()
            await asyncio.sleep(tick_every)
    finally:
        if proc.returncode is None:
            _kill_and_reap(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(drain, timeout=10)
        await _reap(proc)

    text = b"".join(chunks).decode("utf-8", "replace")
    if proc.returncode not in (0, None):
        # 退出码非 0 不一定是错（不带参数那次就是 -1），是不是错由产物决定
        logger.info("引擎退出码 %s：%s", proc.returncode, _error_tail(text))
    return text



async def run_image(
    engine_key: str,
    *,
    src: Path,
    model: str,
    scale: int,
    gpu: int = -1,
    tta: bool = False,
    beat: Callable[[], Awaitable[bool]] | None = None,
) -> tuple[Path, int, int]:
    """放大一张图。返回 `(storage 内的产物路径, 宽, 高)`。

    **必须验产物**：退出码 0 也可能是失败（口径 5），而「失败但界面显示成功」
    是最坏的一种结果。所以这里既查文件在不在，也查尺寸是不是正好整数倍。
    """
    out = _out_path("png")
    args = up.plan_image_args(engine_key, src=src, out=out,
                              model=model, scale=scale, gpu=gpu, tta=tta)
    timeout = max(_MIN_IMAGE_TIMEOUT,
                  4 * up.estimate_for(engine_key, model, scale, _pixels_of(src)))
    try:
        log = await _run_exe(args, cwd=Path(args[0]).parent, timeout=timeout, beat=beat)
    except asyncio.CancelledError:
        # 用户点了取消：把刚开的那个空文件收掉。留着它就成了 storage 里的孤儿文件——
        # 没登记成资产、也没人知道它是谁的，只能靠事后清理脚本兜底。
        out.unlink(missing_ok=True)
        raise
    if not out.exists() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"放大没有产出文件（{up.human_seconds(int(timeout))} 的上限内）。"
                           f"引擎说：{_error_tail(log)}")
    got = image_size.read_image_size_from_file(out)
    if got is None:
        out.unlink(missing_ok=True)
        raise RuntimeError("放大出来了文件，但读不出它的尺寸（产物可能是坏的）。"
                           f"引擎说：{_error_tail(log)}")
    return out, got[0], got[1]


def _pixels_of(src: Path) -> int:
    got = image_size.read_image_size_from_file(src)
    return got[0] * got[1] if got else 0


# ------------------------------------------------------------------ 视频


async def frames_of(src: Path) -> int:
    """这一段有多少帧。**估的**（时长 × 帧率），只用来做上限预检。

    真正跑的时候以**拆出来的文件数**为准——那是数出来的，不是算出来的。
    """
    info = await ffmpeg_service.probe(src)
    duration = info.get("duration") or 0
    fps = info.get("fps") or 0
    try:
        return max(0, int(round(float(duration) * float(fps))))
    except (TypeError, ValueError):
        return 0


async def run_video(
    engine_key: str,
    *,
    src: Path,
    model: str,
    scale: int,
    gpu: int = -1,
    on_progress: Callable[[int], Awaitable[bool]] | None = None,
) -> tuple[Path, int, int, int, int]:
    """把一段视频逐帧放大。返回 `(产物路径, 宽, 高, 帧数, 秒数)`。

    三段式的中间一步是**目录批量放大**：模型只加载一次，比一帧起一次进程快得多。
    进度用「输出目录里已经有多少帧」来报——实测它的百分比输出一直是 `0.00%`，
    靠那个报进度会永远停在 0。

    `on_progress` 返回 False 表示**用户不要了**：立刻停手并抛 `CancelledError`。
    """
    ffmpeg, _ = await ffmpeg_service._resolve_binaries()
    if not ffmpeg:
        raise RuntimeError("未找到可用的 ffmpeg，做不了视频超分")

    info = await ffmpeg_service.probe(src)
    width, height = int(info.get("width") or 0), int(info.get("height") or 0)
    if width <= 0 or height <= 0:
        raise RuntimeError("读不出这段视频的尺寸，没法放大")
    fps = float(info.get("fps") or 0) or 25.0
    has_audio = bool(info.get("has_audio"))

    problem = up.size_problem(width, height, scale)
    if problem:
        raise ValueError(problem)

    work = _new_workdir("upscale")
    frames_in = work / "in"
    frames_out = work / "out"
    frames_in.mkdir(parents=True, exist_ok=True)
    # 输出目录**必须预先建出来**（见文件头细节 1）
    frames_out.mkdir(parents=True, exist_ok=True)
    final = _out_path("mp4")

    async def _report(pct: int) -> bool:
        if on_progress is None:
            return True
        return await on_progress(max(0, min(100, int(pct))))

    async def _must(pct: int) -> None:
        if not await _report(pct):
            raise asyncio.CancelledError()

    try:
        await _must(2)
        ok, err = await ffmpeg_service._run_in(
            [ffmpeg, "-y", "-i", str(src), "-vsync", "0",
             str(frames_in / "frame%08d.png")],
            1800, cwd=work,
        )
        if not ok:
            raise RuntimeError(f"拆帧失败：{err}")
        total = len(list(frames_in.iterdir()))
        if total == 0:
            raise RuntimeError("拆帧之后一帧都没有，这段视频可能读不出来")

        # 上限用**数出来的**帧数复核一次（预检那次是估的，可能差几帧）
        problem = up.frames_problem(total)
        if problem:
            raise ValueError(problem)
        await _must(5)

        args = up.plan_dir_args(engine_key, in_dir=frames_in, out_dir=frames_out,
                                model=model, scale=scale, gpu=gpu)
        timeout = max(_MIN_VIDEO_TIMEOUT,
                      4 * up.estimate_for(engine_key, model, scale,
                                          width * height, total))

        async def _beat() -> bool:
            done = len(list(frames_out.iterdir()))
            return await _report(5 + int(88 * done / max(1, total)))

        log = await _run_exe(args, cwd=Path(args[0]).parent, timeout=timeout, beat=_beat)

        produced = len(list(frames_out.iterdir()))
        if produced == 0:
            raise RuntimeError(f"一帧都没放大出来。引擎说：{_error_tail(log)}")
        if produced != total:
            # 少帧就是少画面。**如实报**，不假装成功——拼出来的片子会缺帧或变速
            raise RuntimeError(
                f"只放大了 {produced} / {total} 帧，缺帧拼出来会变速，已经中止。"
                f"引擎说：{_error_tail(log)}"
            )
        pattern = _number_frames(frames_out)
        await _must(94)

        out_size = up.target_size(width, height, scale)
        cmd = [ffmpeg, "-y", "-i", pattern]
        # 音轨：有就拷回来（`?` 让没有音轨的片子也能走这条命令），这与上游 README 同一条
        cmd += ["-i", str(src),
                "-map", "0:v:0", "-map", "1:a:0?",
                "-c:a", "copy",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                # 帧率按**真实值**回填，不抄上游示例里的 23.98
                "-r", f"{fps:g}", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(final)]
        ok, err = await ffmpeg_service._run_in(cmd, 3600, cwd=work)
        if not ok or not final.exists() or final.stat().st_size == 0:
            final.unlink(missing_ok=True)
            raise RuntimeError(f"合帧失败：{err}")
        await _must(98)

        # 产物对帐：尺寸不对就是没做对，不许当成功
        got_info = await ffmpeg_service.probe(final)
        got_w, got_h = int(got_info.get("width") or 0), int(got_info.get("height") or 0)
        if (got_w, got_h) != out_size:
            final.unlink(missing_ok=True)
            raise RuntimeError(
                f"产物尺寸是 {got_w}×{got_h}，应该是 {out_size[0]}×{out_size[1]}，"
                "对不上，已判为失败（宁可让你重跑，也不要给你一段看着好了的错片子）"
            )
        if has_audio and not got_info.get("has_audio"):
            logger.warning("原片有音轨但产物没有：%s", src)
        duration = int(round(float(got_info.get("duration") or 0)))
        await _report(100)
        return final, got_w, got_h, total, duration
    except asyncio.CancelledError:
        # 取消时把落盘的那个 mp4 收掉（没拼完的半成品不该留在 storage 里）
        final.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def saved_rel(path: Path) -> str:
    """把 storage 内的绝对路径转成相对路径（登记 Asset 用）。"""
    return _save_asset_file(path)
