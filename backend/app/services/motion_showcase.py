"""Fixed local audiovisual compositions available to the Agent."""

from __future__ import annotations

import asyncio
import math
import random
import subprocess
import wave
from functools import lru_cache
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict, Field

from app.services import ffmpeg_service, motion_video


FONTS = motion_video.FONT.parent
W, H, FPS, LENGTH, RATE = 1280, 720, 24, 10, 48000


class ShowcaseVideo(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    template: Literal["water_park", "gel_pen", "rural_town"]
    name: str = Field(default="有声场景短片", min_length=1, max_length=80)
    title: str | None = Field(default=None, min_length=1, max_length=16, pattern=r"^[^\r\n\x00]+$")
    closing_title: str | None = Field(default=None, min_length=1, max_length=16, pattern=r"^[^\r\n\x00]+$")


@lru_cache
def font(size, bold=False):
    weight = "Bold" if bold else "Regular"
    return ImageFont.truetype(str(FONTS / f"NotoSansCJKsc-{weight}.otf"), size)


def txt(draw, xy, value, size, color, bold=False, anchor=None):
    draw.text(xy, value, fill=color, font=font(size, bold), anchor=anchor)


def headline(draw, xy, text, size, color):
    width = draw.textlength(text, font=font(size, True))
    txt(draw, xy, text, min(size, max(24, int(size * 510 / max(width, 1)))), color, True)


def clamp(x):
    return min(1, max(0, x))


def ease(x):
    q = clamp(x)
    return q * q * (3 - 2 * q)


def gradient(top, bottom):
    strip = Image.new("RGB", (1, H))
    draw = ImageDraw.Draw(strip)
    for y in range(H):
        q = y / (H - 1)
        draw.point((0, y), tuple(round(a + (b - a) * q) for a, b in zip(top, bottom)))
    return strip.resize((W, H))


WATER_BG = gradient((15, 110, 197), (103, 227, 235))
PEN_BG = gradient((231, 224, 208), (250, 247, 237))
TOWN_BG = gradient((41, 70, 105), (247, 202, 147))
SLIDE = [(654 + 610*q, 235 + 296*q + 73*math.sin(2.15*math.pi*q)) for q in np.linspace(0, 1, 180)]
SEED = random.Random(110)
DROPS = [(SEED.random(), SEED.uniform(-1, 1), SEED.uniform(.3, 1)) for _ in range(34)]
TREES = [(SEED.uniform(45, 1240), SEED.uniform(382, 428), SEED.uniform(.6, 1.3)) for _ in range(39)]


def line(draw, points, color, size):
    draw.line(points, fill=color, width=size, joint="curve")
    r = size / 2
    for x, y in (points[0], points[-1]):
        draw.ellipse((x-r, y-r, x+r, y+r), fill=color)


def water(t, spec):
    im = WATER_BG.copy()
    d = ImageDraw.Draw(im)
    d.ellipse((940, 70, 1086, 216), fill="#ffdc75")
    for n in range(5):
        x = (110 + n * 275 + t * (9 + n)) % (W + 210) - 90
        y = 108 + (n % 3) * 45
        d.rounded_rectangle((x, y, x+145, y+13), radius=7, fill="#9fe8f4")
    d.polygon([(0, 480), (232, 352), (400, 441), (600, 314), (895, 446), (W, 341), (W,H), (0,H)], fill="#228eae")
    for i, color in enumerate(("#20acd0", "#158bbb", "#127cae", "#1171a3")):
        base = 470 + i * 58
        wave_points = [(x, base + 13 * math.sin(x / 105 + t * (1.9 + i * .18) + i)) for x in range(0, W + 9, 8)]
        d.polygon(wave_points + [(W, H), (0, H)], fill=color)
    for n in range(18):
        x = 565 + (n * 131 + 34 * t) % 760
        y = 548 + (n * 43) % 141
        d.rounded_rectangle((x,y,x+18+n%4*8,y+3), radius=2, fill="#62d4d8")
    d.line((697, 282, 697, 522), "#eafaea", width=9)
    d.line((915, 339, 915, 544), "#d6f2eb", width=9)
    active_slide = SLIDE[:-5]
    line(d, active_slide, "#086f9e", 76)
    line(d, [(x, y-10) for x, y in active_slide], "#fff1d5", 62)
    line(d, [(x, y-10) for x, y in active_slide], "#ff9359", 43)
    line(d, [(x, y-22) for x, y in active_slide], "#ffb379", 6)
    if 1.25 <= t <= 6.0:
        q = clamp((t-1.25)/4.25)
        x,y = active_slide[int((len(active_slide)-1)*q**1.47)]
        d.ellipse((x-29,y-28,x+29,y+2), fill="#1950a1", outline="#fff5e2", width=4)
        d.ellipse((x-13,y-49,x+12,y-23), fill="#ffd59a")
        d.arc((x-15,y-51,x+14,y-21), 178, 350, "#16324d", 6)
    splash = t-5.45
    if 0 <= splash <= 1.2:
        for p, direction, scale in DROPS:
            x = 1196 + direction*175*splash*scale
            y = 503 - (245+p*150)*splash*(1-splash*.8)*scale
            r = max(2, round(10*scale*(1-splash*.62)))
            d.ellipse((x-r,y-r,x+r,y+r), fill="#d5fff3")
    d.rounded_rectangle((68, 74, 261, 114), 18, fill="#fff3d6")
    txt(d, (165,94), "SUMMER  /  01", 20, "#134572", True, "mm")
    if t < 6.8:
        x = 70 + 30 * (1-ease(t/.65))
        headline(d,(x,164),spec.title or "夏日水上乐园",63,"#fff9e9")
        txt(d,(x,255),"一路滑进夏天",40,"#dbfffb",True)
        txt(d,(x,323),"滑道 · 水花 · 清凉时光",24,"#f0ffff")
    else:
        y = 163 + 27 * (1-ease((t-6.8)/.52))
        headline(d,(69,y),spec.closing_title or "今天，去玩水！",63,"#fff9e9")
        d.rounded_rectangle((70,y+113,290,y+169),26,fill="#ffdb76")
        txt(d,(180,y+140),"约上朋友出发",24,"#12436b",True,"mm")
    d.line((70,663,1210,663), "#b2edf0", width=1)
    txt(d,(70,674),"WATER PARK  /  THE SPLASH CUT",15,"#ebfffa")
    return im


def pen(t, spec):
    im = PEN_BG.copy()
    d = ImageDraw.Draw(im)
    for n in range(15):
        y = 63+n*47
        d.line((42,y,1234,y), "#e6e0d5", width=1)
    d.line((53,57,53,661),"#d8c9b1",1)
    txt(d,(80,69),"STATIONERY STUDIO     /     02",17,"#6e6151",True)
    d.line((78,111,1202,111),"#bdb0a0",1)
    intro = t < 3.1
    middle = 3.1 <= t < 7.2
    heading, sub = (
        (spec.title or "中性笔", "让灵感落在纸上") if intro else
        ("落笔，有迹", "随手写下今天的灵感") if middle else
        (spec.closing_title or "写下下一笔", "笔记、涂写与日常记录")
    )
    beat_start = 0 if intro else 3.1 if middle else 7.2
    y = 171 + 22*(1-ease((t-beat_start)/.45))
    headline(d,(80,y),heading,70,"#242331")
    txt(d,(85,y+111),sub,27,"#665a50")
    d.rounded_rectangle((83,y+172,246,y+212),20,fill="#29273a")
    txt(d,(164,y+191),"GEL INK PEN",16,"#fff4df",True,"mm")
    shift = 45 * (1-ease(t/.8))
    cx,cy = 951 + shift, 360 + 12*math.sin(t*1.18)
    tilt = 28+5*math.cos(t*1.9) if intro else -153 if middle else 32+3*math.sin(t)
    if t > 3.25:
        strokes = [(515+570*q,566+37*math.sin(q*math.tau*2.7)*(1-.55*q)-32*q) for q in np.linspace(0,1,190)]
        length = int(ease((t-3.28)/3.1)*(len(strokes)-1))
        if length > 1:
            line(d,strokes[:length+1],"#293247",5)
        if middle:
            angle = math.radians(tilt)
            x0,y0 = strokes[length]
            cx = x0 - (282*math.cos(angle) - 2*math.sin(angle))
            cy = y0 - (-282*math.sin(angle) - 2*math.cos(angle))
    elif middle:
        cx,cy = 775,429
    pen_item = Image.new("RGBA",(660,176))
    p = ImageDraw.Draw(pen_item)
    p.rounded_rectangle((24,75,606,137),30,fill=(97,72,62,36))
    p.rounded_rectangle((42,66,555,108),19,fill="#232338")
    p.rounded_rectangle((82,71,502,88),8,fill="#50526a")
    p.line((95,72,492,72),fill="#9694a9",width=2)
    p.rounded_rectangle((447,66,564,108),16,fill="#171725")
    for j in range(8): p.line((464+j*10,72,464+j*10,102),fill="#4b4c63",width=2)
    p.rounded_rectangle((112,58,224,66),4,fill="#d3bb8b")
    p.polygon([(555,69),(589,78),(589,96),(555,105)],fill="#b0adb0")
    p.polygon([(589,78),(612,86),(589,95)],fill="#423c4c")
    p.rounded_rectangle((23,73,55,102),10,fill="#323349")
    txt(p,(297,80),"G E L",11,"#ded4b9")
    pen_item = pen_item.rotate(tilt, resample=Image.Resampling.BICUBIC, expand=True)
    im.paste(pen_item,(round(cx-pen_item.width/2),round(cy-pen_item.height/2)),pen_item)
    d = ImageDraw.Draw(im)
    d.line((78,647,1202,647),"#bdb0a0",1)
    txt(d,(79,657),"PAPER  /  INK  /  IDEAS",16,"#5a5047")
    return im


def ridge(draw, t, level, amp, rate, color, seed):
    offset = t*rate
    points = [(x, level+amp*math.sin(x/179+offset+seed)+amp*.41*math.sin(x/73+seed*2.3)) for x in range(0,W+12,12)]
    draw.polygon(points+[(W,H),(0,H)],fill=color)


def town(t, spec):
    im = TOWN_BG.copy()
    d = ImageDraw.Draw(im)
    sun_y = 193 - 48*ease(t/9)
    d.ellipse((934,sun_y-64,1062,sun_y+64),fill="#ffda93")
    for i in range(6):
        x = (225 + i*209 + t*(8+i*.6))%(W+140)-50
        y = 192+i%3*38
        d.rounded_rectangle((x,y,x+130+i%2*48,y+9),5,fill="#c4afa2")
    ridge(d,t,332,67,.025,"#71828b",3.1)
    ridge(d,t,388,69,.045,"#566f77",1.0)
    ridge(d,t,449,56,.075,"#3e6065",1.8)
    ridge(d,t,494,36,.080,"#315852",3.7)
    for x,y,s in TREES:
        x = (x+14*t)%(W+60)-30
        d.polygon([(x,y-43*s),(x-11*s,y),(x+12*s,y)],fill="#335855")
        d.line((x,y,x,y+12*s),fill="#2e4b49",width=2)
    d.polygon([(0,548),(320,519),(480,501),(710,513),(944,532),(1280,503),(1280,720),(0,720)],fill="#496347")
    for k in range(8):
        d.line([(x,553+k*19+12*math.sin(x/156+k*.3+t*.15)) for x in range(0,W+6,6)],fill=("#6d7d4e" if k%2==0 else "#566e49"),width=3)
    river = [(820+123*math.cos(q*5.8+t*.05)+88*q,499+225*q) for q in np.linspace(0,1,100)]
    widths = np.linspace(6,88,len(river))
    d.polygon([(x-r,y) for (x,y),r in zip(river,widths)]+[(x+r,y) for (x,y),r in reversed(list(zip(river,widths)))],fill="#779a9a")
    for n in range(8):
        q = (n/8+t*.105)%1
        x,y = river[int(q*(len(river)-1))]
        d.line((x-q*42,y,x+q*42,y),fill="#a8b9ac",width=2)
    for i, (hx,hy,s) in enumerate(((565,456,.83),(640,477,1.05),(748,461,.91),(827,481,1.0),(941,450,.72),(1004,468,.81))):
        x = hx + 6*t
        d.rectangle((x-28*s,hy-27*s,x+28*s,hy+21*s),fill="#ddc09d")
        d.polygon([(x-37*s,hy-26*s),(x,hy-57*s),(x+37*s,hy-26*s)],fill=("#703d3d" if i%2 else "#854f41"))
        d.rectangle((x+7*s,hy-13*s,x+17*s,hy-2*s),fill="#ffdc91")
        d.rectangle((x-8*s,hy+1*s,x+1*s,hy+20*s),fill="#584b4b")
    mist = Image.new("RGBA",(W,H))
    m = ImageDraw.Draw(mist)
    for k in range(7):
        x = (k*333+28*t)%(W+330)-200
        y = 394+k%4*26
        m.ellipse((x,y,x+420,y+17+k%2*5),fill=(251,219,184,28-k*2))
    im = Image.alpha_composite(im.convert("RGBA"),mist).convert("RGB")
    d = ImageDraw.Draw(im)
    for i in range(5):
        x = 837-i*46-t*(13+i)
        y = 239+(i%2*16)+8*math.sin(t*.94+i)
        d.arc((x-14,y-6,x,y+7),195,344,"#324b61",2)
        d.arc((x,y-6,x+14,y+7),195,344,"#324b61",2)
    txt(d,(72,67),"RURAL STUDIES    /    03",17,"#fff3d9",True)
    title = (spec.title or "小镇醒来") if t < 6.65 else (spec.closing_title or "沿河，慢一点")
    subtitle = "晨光穿过山脊" if t < 6.65 else "乡野的一天，由此开始"
    transition = t if t<6.65 else t-6.65
    x = 72 + 23*(1-ease(transition/.62))
    headline(d,(x,139),title,66,"#fff5df")
    txt(d,(x,236),subtitle,26,"#fff1d8")
    d.line((72,646,1205,646),"#a9b99e",1)
    txt(d,(73,660),"MORNING LIGHT  /  A RIVERSIDE VILLAGE",15,"#fff1d8")
    return im


def soundtrack(kind, path):
    rng = np.random.default_rng(71)
    track = np.zeros((RATE * LENGTH, 2), dtype=np.float64)

    def add(start, values, gain, pan=0):
        i = round(RATE*start)
        n = min(len(values), len(track)-i)
        if n > 0:
            track[i:i+n] += values[:n,None]*gain*np.array([1-pan*.48,1+pan*.48])

    def tone(start, length, midi, gain, pan=0, softness=4):
        a = np.arange(round(RATE*length))/RATE
        hz = 440*2**((midi-69)/12)
        sound = np.sin(math.tau*hz*a) + .25*np.sin(math.tau*2*hz*a) + .06*np.sin(math.tau*3*hz*a)
        envelope = np.minimum(1,a/.026)*np.exp(-softness*a)*np.minimum(1,(length-a)/.08)
        add(start,sound*envelope,gain,pan)

    def noise(start,length,gain,decay,pan=0,grain=7):
        a=np.arange(round(RATE*length))/RATE
        raw=rng.standard_normal(len(a)+grain-1)
        smooth=np.convolve(raw,np.ones(grain)/grain,mode="valid")
        add(start,smooth*np.minimum(1,a/.052)*np.exp(-decay*a)*np.minimum(1,(length-a)/.10),gain,pan)

    if kind == "water":
        for i, note in enumerate((72,76,79,81,79,76,74,77,81,84,81,77,76,79,83,86,83,79,72,76)):
            tone(.25+i*.43,.78,note,.13,(-1)**i*.29)
            if i%2==0: tone(.24+i*.43,.42,note-24,.2,softness=8)
            noise(.35+i*.43,.11,.10,25,grain=2)
        for i in range(7):
            noise(5.42+i*.047,1.1,.19,4.6,pan=.65-i*.15,grain=5)
        noise(1.2,4.6,.11,.30,.4,grain=10)
        tone(7.14,1.12,84,.13,.18)
    elif kind == "pen":
        for i,note in enumerate((55,62,67,71,66,64,60,62,67,74,71,67)):
            tone(.18+i*.72,1.18,note,.15,(-1)**i*.19,softness=2.9)
        noise(.86,.21,.12,13,grain=2)
        for i in range(16):
            noise(3.28+i*.185,.22,.10,.85,pan=.24*math.sin(i),grain=3)
        tone(7.22,.8,79,.09,.25)
    else:
        for i, notes in enumerate(((55,62,69),(53,60,67),(50,57,65),(55,62,67))):
            for j, n in enumerate(notes):
                tone(i*2.3+j*.28,2.7,n,.077,(-1)**j*.38,softness=1.15)
        noise(0,10,.16,.035,grain=31)
        noise(.45,9.48,.07,.024,pan=-.5,grain=13)
        for i,start in enumerate((1.25,2.9,4.23,6.15,8.1)):
            for j in range(3):
                a=np.arange(6600)/RATE
                f=1350+i*76+j*130
                add(start+j*.115,np.sin(math.tau*(f*a+1800*a*a))*np.sin(np.pi*a/(len(a)/RATE))**2,.055,.53-i*.23)
    t=np.arange(len(track))/RATE
    track *= (np.minimum(1,t/.12)*np.minimum(1,(LENGTH-t)/.32))[:,None]
    peak = max(.25, float(np.max(np.abs(track))))
    pcm=np.asarray(track/peak*.78*32767,dtype="<i2")
    with wave.open(str(path),"wb") as wav:
        wav.setparams((2,2,RATE,0,"NONE","not compressed"))
        wav.writeframes(pcm.tobytes())


RENDERERS = {
    "water_park": (water, "water", ["轻快配乐", "滑行与水花"]),
    "gel_pen": (pen, "pen", ["轻音乐", "笔尖摩擦"]),
    "rural_town": (town, "town", ["舒缓配乐", "水声", "鸟鸣"]),
}


def _encode(ffmpeg, spec, work, output, cancel):
    draw_frame, audio_kind, _ = RENDERERS[spec.template]
    audio = work / "soundtrack.wav"
    soundtrack(audio_kind, audio)
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
        "-pixel_format", "rgb24", "-video_size", f"{W}x{H}", "-framerate", str(FPS), "-i", "pipe:0",
        "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-threads", "2", "-crf", "18",
        "-preset", "fast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", "-t", str(LENGTH), str(output),
    ]
    with (work / "ffmpeg.log").open("w+b") as log:
        with subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log) as proc:
            try:
                for index in range(FPS * LENGTH):
                    if cancel is not None and cancel.is_set():
                        raise RuntimeError("已取消有声短片渲染")
                    proc.stdin.write(draw_frame(index / FPS, spec).tobytes())
                proc.stdin.close()
                if proc.wait(timeout=20):
                    raise OSError("FFmpeg 编码失败")
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.seek(0)
                detail = log.read()[-500:].decode("utf-8", "replace").strip()
                raise RuntimeError(f"有声短片渲染失败：{detail or exc}") from exc
            finally:
                if proc.poll() is None:
                    proc.kill()
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass


async def render(spec: ShowcaseVideo, cancel: asyncio.Event | None = None) -> tuple[Path, dict]:
    async with motion_video.RENDER_LOCK:
        ffmpeg, ffprobe = await ffmpeg_service._resolve_binaries()
        if not ffmpeg or not ffprobe:
            raise RuntimeError("本地有声短片需要 FFmpeg 和 FFprobe")
        if cancel is not None and cancel.is_set():
            raise RuntimeError("已取消有声短片渲染")
        work = ffmpeg_service.new_workdir("showcase")
        output: Path | None = None
        draft = work / "video.mp4"
        try:
            worker = asyncio.create_task(asyncio.to_thread(_encode, ffmpeg, spec, work, draft, cancel))
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Wait for the encoder to release files before the cleanup below.
                await asyncio.gather(worker, return_exceptions=True)
                raise
            meta = await ffmpeg_service.probe(draft)
            if cancel is not None and cancel.is_set():
                raise RuntimeError("已取消有声短片渲染")
            if (
                (meta.get("width"), meta.get("height"), meta.get("fps")) != (W, H, FPS)
                or abs((meta.get("duration") or 0) - LENGTH) > 0.05
                or meta.get("codec") != "h264" or meta.get("audio_codec") != "aac"
                or meta.get("audio_channels") != 2
            ):
                raise RuntimeError("有声成片校验失败，需要 10 秒横屏 H.264 和双声道 AAC")
            output = ffmpeg_service._out_path("mp4")
            draft.replace(output)
            return output, meta
        finally:
            ffmpeg_service.drop_workdir(work)
