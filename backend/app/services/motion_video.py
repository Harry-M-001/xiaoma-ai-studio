"""Render local motion graphics from a bounded scene specification."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.services import ffmpeg_service


Color = Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")]
FORMATS = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (720, 720)}
FONT = Path(__file__).resolve().parents[2] / "assets" / "fonts" / "NotoSansCJKsc-Regular.otf"
RENDER_LOCK = asyncio.Lock()
Layout = Literal["title", "bars", "step", "offer", "countdown"]


class MotionBar(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=12, pattern=r"^[^\r\n\x00]+$")
    value: int = Field(ge=0, le=1_000_000)


class MotionScene(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=40, pattern=r"^[^\r\n\x00]+$")
    subtitle: str = Field(default="", max_length=70, pattern=r"^[^\r\n\x00]*$")
    seconds: int = Field(default=3, ge=1, le=6)
    background: Color = "#122234"
    accent: Color = "#72E0CA"
    motion: Literal["fade", "rise", "none"] = "rise"
    layout: Layout = "title"
    bars: list[MotionBar] | None = Field(default=None, min_length=2, max_length=4)
    step: int | None = Field(default=None, ge=1, le=99)
    price: str | None = Field(default=None, min_length=1, max_length=16, pattern=r"^[^\r\n\x00]+$")
    cta: str | None = Field(default=None, min_length=1, max_length=16, pattern=r"^[^\r\n\x00]+$")

    @model_validator(mode="after")
    def validate_layout(self) -> MotionScene:
        expected = {"bars": {"bars"}, "step": {"step"}, "offer": {"price", "cta"}}.get(self.layout, set())
        actual = {key for key in ("bars", "step", "price", "cta") if getattr(self, key) is not None}
        if actual != expected:
            fields = "、".join(sorted(expected)) or "无需额外字段"
            raise ValueError(f"{self.layout} 布局的额外字段：{fields}")
        if self.bars and not any(bar.value for bar in self.bars):
            raise ValueError("至少一个条形值须大于 0")
        return self


class MotionVideo(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(default="程序化短片", min_length=1, max_length=80)
    ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    scenes: list[MotionScene] = Field(min_length=1, max_length=6)


def _foreground(hex_color: str) -> str:
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4) for v in rgb]
    luminance = sum(v * w for v, w in zip(linear, (0.2126, 0.7152, 0.0722)))
    return "black" if luminance > 0.179 else "white"


def _text(file: str, size: int, color: str, x: str, y: str, extra: str = "") -> str:
    # Filter syntax never includes user text. All strings are read literally from local files.
    return (
        f"drawtext=fontfile=font.otf:textfile={file}.txt:expansion=none:"
        f"fontcolor={color}:fontsize={size}:x='{x}':y='{y}'{extra}"
    )


def scene_filters(scene: MotionScene, width: int, height: int, work: Path) -> str:
    accent = "0x" + scene.accent[1:]
    foreground = _foreground(scene.background)
    (work / "title.txt").write_text(scene.title, encoding="utf-8")
    rise = "+h*0.04*(1-min(t/0.45,1))" if scene.motion == "rise" else ""
    alpha = ":alpha='min(t/0.35,1)'" if scene.motion != "none" else ""
    title_y = {"bars": "0.12", "offer": "0.24", "countdown": "0.16"}.get(scene.layout, "0.43")
    title_size = min(width // (18 if scene.layout in ("bars", "offer", "countdown") else 13),
                     max(12, int(width * 0.74 / len(scene.title))))
    filters = [_text("title", title_size, foreground, "w*0.12", f"h*{title_y}{rise}", alpha)]
    prefix = "[0:v]"
    if scene.layout == "title":
        filters += [
            f"drawbox=x=iw*0.12:y=ih*0.36:w=iw*0.09:h=4:color={accent}:t=fill",
            f"drawbox=x=iw*0.12:y=ih*0.86:w=iw*0.76:h=3:color={accent}@0.35:t=fill",
        ]
    if scene.subtitle:
        (work / "subtitle.txt").write_text(scene.subtitle, encoding="utf-8")
        size = min(width // 26, max(10, int(width * 0.74 / len(scene.subtitle))))
        y = "0.78" if scene.layout in ("offer", "countdown") else "0.88" if scene.layout == "bars" else "0.60"
        filters.append(_text("subtitle", size, foreground, "w*0.12", f"h*{y}{rise}", alpha))

    if scene.bars:
        # Reveal each bar at the same fraction of its own width, then place labels on top.
        chart_x, chart_width = round(width * 0.32), round(width * 0.45)
        row_start, row_step, bar_height = round(height * .32), round(height * .13), round(height * .06)
        max_value = max(bar.value for bar in scene.bars)
        boxes = []
        masks = []
        overlays = []
        for i, bar in enumerate(scene.bars):
            y = row_start + i * row_step
            bar_width = max(1, round(chart_width * bar.value / max_value))
            if bar.value:
                boxes.append(f"drawbox=x={chart_x}:y={y}:w={bar_width}:h={bar_height}:color={accent}:t=fill")
                cover_width = max(2, (bar_width + 1) // 2 * 2)
                cover_height = (bar_height + 1) // 2 * 2
                masks.append(f"[mask{i}]crop={cover_width}:{cover_height}:0:0[cover{i}];")
                overlays.append((i, y, bar_width))
            for field, value, x in (("label", bar.label, "w*0.12"), ("value", str(bar.value), "w*0.80")):
                name = f"{field}{i}"
                (work / f"{name}.txt").write_text(value, encoding="utf-8")
                filters.append(_text(name, max(10, min(width // 39, round(width * .17 / len(value)))),
                                     foreground, x, f"{y}+({bar_height}-th)/2"))
        branches = "".join(f"[mask{i}]" for i, _, _ in overlays)
        graph = [f"[0:v]split={len(overlays)+1}[base]{branches};", *masks,
                 f"[base]{','.join(boxes)}[bars];"]
        previous = "bars"
        for i, y, bar_width in overlays:
            graph.append(f"[{previous}][cover{i}]overlay=x='{chart_x}+{bar_width}*min(t/0.75,1)':y={y}[row{i}];")
            previous = f"row{i}"
        prefix = "".join(graph) + f"[{previous}]"
    elif scene.step is not None:
        (work / "step.txt").write_text(f"{scene.step:02d}", encoding="utf-8")
        filters.append(_text("step", width // 11, foreground, "w*0.12", "h*0.22", alpha))
    elif scene.price is not None and scene.cta is not None:
        (work / "price.txt").write_text(scene.price, encoding="utf-8")
        (work / "cta.txt").write_text(scene.cta, encoding="utf-8")
        filters += [
            _text("price", min(width // 7, max(16, int(width * .74 / len(scene.price)))),
                  foreground, "w*0.12", "h*0.39", alpha),
            f"drawbox=x=iw*0.12:y=ih*0.64:w=iw*0.76:h=ih*0.10:color={accent}:t=fill",
            _text("cta", min(width // 17, max(12, int(width * .67 / len(scene.cta)))),
                  _foreground(scene.accent), "(w-tw)/2", "h*0.69-th/2"),
        ]
    elif scene.layout == "countdown":
        for offset in range(scene.seconds):
            name = f"tick{offset}"
            (work / f"{name}.txt").write_text(str(scene.seconds - offset), encoding="utf-8")
            filters.append(_text(name, min(width, height) // 3, foreground, "(w-tw)/2",
                                 "(h-th)/2", f":enable='gte(t,{offset})*lt(t,{offset + 1})'"))
    return prefix + ",".join(filters) + "[out]"


async def render(spec: MotionVideo, cancel: asyncio.Event | None = None) -> tuple[Path, dict]:
    width, height = FORMATS[spec.ratio]
    async with RENDER_LOCK:
        ffmpeg, ffprobe = await ffmpeg_service._resolve_binaries()
        if not ffmpeg or not ffprobe:
            raise RuntimeError("本地程序化渲染需要 FFmpeg 和 FFprobe")
        work = ffmpeg_service.new_workdir("motion")
        output: Path | None = None
        verified = False
        try:
            await asyncio.to_thread(shutil.copyfile, FONT, work / "font.otf")
            parts = []
            for i, scene in enumerate(spec.scenes, 1):
                if cancel is not None and cancel.is_set():
                    raise RuntimeError("已取消本地渲染")
                part = work / f"{i}.mp4"
                ok, error = await ffmpeg_service._run_in([
                    ffmpeg, "-nostdin", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"color=c=0x{scene.background[1:]}:s={width}x{height}:r=30",
                    "-t", str(scene.seconds), "-filter_complex", scene_filters(scene, width, height, work),
                    "-map", "[out]",
                    "-an", "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p",
                    "-preset", "veryfast", "-movflags", "+faststart", str(part.resolve()),
                ], timeout=30, cwd=work)
                if not ok:
                    raise RuntimeError(f"第 {i} 幕渲染失败：{error}")
                parts.append(part)
            if cancel is not None and cancel.is_set():
                raise RuntimeError("已取消本地渲染")
            output = await ffmpeg_service.merge_videos(parts)
            meta = await ffmpeg_service.probe(output)
            if cancel is not None and cancel.is_set():
                raise RuntimeError("已取消本地渲染")
            seconds = sum(scene.seconds for scene in spec.scenes)
            if (
                (meta.get("width"), meta.get("height")) != (width, height)
                or abs((meta.get("duration") or 0) - seconds) > 0.1
                or meta.get("fps") != 30 or meta.get("codec") != "h264"
            ):
                raise RuntimeError("成片规格校验失败：预期指定画幅与时长、30 fps、H.264")
            verified = True
            return output, meta
        finally:
            ffmpeg_service.drop_workdir(work)
            if output is not None and not verified:
                output.unlink(missing_ok=True)
