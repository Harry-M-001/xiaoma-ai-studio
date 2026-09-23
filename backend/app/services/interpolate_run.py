"""补帧的执行层：三段式（拆帧 → 目录批量补帧 → 按新帧率合帧）。

与 `interpolate.py`（纯口径）分工，与 `upscale_run.py` 的关系是**共用脚手架**：
子进程怎么跑、心跳怎么问、取消怎么杀、产物帧怎么重编号、错误怎么滤掉设备清单——
这些一个都不重写，直接用 `upscale_run` 里那几件。两份各写的部分只有一件：
**合帧那一步的帧率**。

视频为什么也要三段式：`rife-ncnn-vulkan` 的 `-i` 明确写的是
`input image directory (jpg/png/webp)`，**不认视频**——与超分那两档一样。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.services import ffmpeg_service
from app.services import interpolate as ip
from app.services import upscale_run as urun

logger = logging.getLogger(__name__)

_MIN_TIMEOUT = 1800.0


async def run_video(
    *,
    src: Path,
    model: str,
    src_fps: float,
    target: int,
    gpu: int = -1,
    on_progress: Callable[[int], Awaitable[bool]] | None = None,
) -> tuple[Path, int, int, int, int]:
    """把一段视频补帧。返回 `(产物路径, 宽, 高, 产物帧数, 秒数)`。

    `on_progress` 返回 False 表示用户不要了：立刻停手并抛 `CancelledError`
    （与超分同一条：任务中心那个「取消」只改数据库状态，不取消协程，
    所以引擎这一层必须自己有心跳）。
    """
    ffmpeg, _ = await ffmpeg_service._resolve_binaries()
    if not ffmpeg:
        raise RuntimeError("未找到可用的 ffmpeg，做不了补帧")

    info = await ffmpeg_service.probe(src)
    width, height = int(info.get("width") or 0), int(info.get("height") or 0)
    if width <= 0 or height <= 0:
        raise RuntimeError("读不出这段视频的尺寸，没法补帧")
    fps = float(info.get("fps") or 0) or src_fps
    duration = float(info.get("duration") or 0)
    has_audio = bool(info.get("has_audio"))

    work = urun._new_workdir("interp")
    frames_in = work / "in"
    frames_out = work / "out"
    frames_in.mkdir(parents=True, exist_ok=True)
    # 输出目录**必须预先建出来**：那个引擎按输出名的后缀判格式，目录名没有后缀
    frames_out.mkdir(parents=True, exist_ok=True)
    final = urun._out_path("mp4")

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

        # 用**数出来的**帧数把上限与目标复核一次（预检那次是按时长估的）
        spec = ip.find_model(model) or ip.Model(key=model, label=model, note="", custom=False)
        problem = ip.target_problem(total, fps, target, model=spec)
        if problem:
            raise ValueError(problem)
        want = ip.expected_out_frames(model, total, fps, target)
        await _must(5)

        args = ip.plan_dir_args(in_dir=frames_in, out_dir=frames_out, model=model,
                                src_frames=total, src_fps=fps, target=target, gpu=gpu)
        timeout = max(_MIN_TIMEOUT,
                      4 * ip.estimate_seconds(model, width, height, total))

        async def _beat() -> bool:
            done = len(list(frames_out.iterdir()))
            # 补帧会**多出**帧来，所以进度按「目标帧数」而不是「输入帧数」看
            return await _report(5 + int(88 * done / max(1, want)))

        log = await urun._run_exe(args, cwd=Path(args[0]).parent, timeout=timeout,
                                  beat=_beat, tick_every=3.0)

        produced = len(list(frames_out.iterdir()))
        if produced == 0:
            raise RuntimeError(f"一帧都没补出来。引擎说：{urun._error_tail(log)}")
        if produced != want:
            # 帧数不对就一定串了：少了会变速，多了会拖长。**如实报，不假装成功**
            raise RuntimeError(
                f"补出来 {produced} 帧，应该是 {want} 帧。帧数不对拼出来会变速，已经中止。"
                f"引擎说：{urun._error_tail(log)}"
            )
        pattern = urun._number_frames(frames_out)
        await _must(94)

        # **帧率按实际产出回推**（产物帧数 ÷ 原时长），不硬写目标帧率：
        # 只做 2 倍的模型实际给的是 2N 帧，硬写目标帧率会让片子变速。
        seconds_in = total / float(fps)
        fps_out = produced / seconds_in if seconds_in else float(fps)
        out_size = (width, height)
        # **必须给 `-framerate`**：那串 PNG 本身不带帧率，image2 默认按 25fps 读。
        # 不给它的话，60 帧会被当成 2.4 秒，再按 `-r 60` 重采样成 144 帧——
        # 这一版第一次真跑就是这么错的（超分那边同一个坑，见 upscale_run 里的注释）。
        cmd = [ffmpeg, "-y", "-framerate", f"{fps_out:g}", "-i", pattern,
               "-i", str(src), "-map", "0:v:0", "-map", "1:a:0?"]
        cmd += ["-c:a", "copy",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-r", f"{fps_out:g}", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(final)]
        ok, err = await ffmpeg_service._run_in(cmd, 3600, cwd=work)
        if not ok or not final.exists() or final.stat().st_size == 0:
            final.unlink(missing_ok=True)
            raise RuntimeError(f"合帧失败：{err}")
        await _must(98)

        got = await ffmpeg_service.probe(final)
        got_w, got_h = int(got.get("width") or 0), int(got.get("height") or 0)
        if (got_w, got_h) != out_size:
            # 补帧**不该改画幅**：尺寸变了说明我们哪一步串了，宁可判失败
            final.unlink(missing_ok=True)
            raise RuntimeError(
                f"产物尺寸是 {got_w}×{got_h}，应该是 {out_size[0]}×{out_size[1]}——"
                "补帧只该提高帧的密度，不该改画幅，所以判为失败"
            )
        got_duration = float(got.get("duration") or 0)
        # 帧数对帐：这是**唯一**能证明「补出来的帧真的都进了片子」的维度
        got_frames = await ffmpeg_service.probe_video_frames(final)
        if got_frames is not None and abs(got_frames - produced) > 1:
            final.unlink(missing_ok=True)
            raise RuntimeError(
                f"产物里有 {got_frames} 帧，我们补出来了 {produced} 帧——"
                "差这么多说明合帧那一步的帧率串了，已判为失败"
            )
        if duration and abs(got_duration - duration) > max(0.5, duration * 0.1):
            logger.warning("补帧后时长从 %.2fs 变成 %.2fs（%s）", duration, got_duration, src)
        if has_audio and not got.get("has_audio"):
            logger.warning("原片有音轨但产物没有：%s", src)
        await _report(100)
        return final, got_w, got_h, produced, int(round(got_duration or duration))
    except asyncio.CancelledError:
        final.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
