"""导演台截帧（`POST /api/director/thumbnail`）的宽高来源（直接 python 运行）。

运行：venv/Scripts/python tests/test_director_frame.py

为什么这件小事值得单测：导演台的「AI 改造」在把片段交给视频生成页之前要先截一帧，
而**这一帧就是图生视频的首帧**。它的宽高如果照抄源视频资产，那么 v1.1.7 之前入库的
视频（宽高一律为空）截出来的图也就没有宽高，于是
「首帧与画幅不符」「首帧会被放大」两条告警会**静默失效**——
跑起来不报错，只是永远不提醒。

所以这里钉住：尺寸以截出来的那个文件为准，源视频记没记都不影响；
文件读不出来时才退回源视频的宽高（总比字段空着好）。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import tempfile
from pathlib import Path
from typing import Iterator

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import Asset  # noqa: E402
from app.routers import director  # noqa: E402
from app.schemas import DirectorExtractIn, DirectorThumbnailIn  # noqa: E402
from app.services import ffmpeg_service, storage  # noqa: E402


def _png(width: int, height: int) -> bytes:
    """最小 PNG 头：只需签名 + IHDR，`read_image_size` 就够读了。"""
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
    )


def _run(fn):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with maker() as db:
            await fn(db)
        await engine.dispose()

    asyncio.run(scenario())


async def _add_video(db, width: int | None = None, height: int | None = None) -> Asset:
    """建一条视频资产；不传宽高就是「老素材」那种状态（v1.1.7 之前全是）。"""
    a = Asset(
        kind="video",
        filename="clip.mp4",
        original_name="clip.mp4",
        content_type="video/mp4",
        size=1024,
        width=width,
        height=height,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


@contextlib.contextmanager
def _stub_frame(frame_file: Path) -> Iterator[None]:
    """把「调 ffmpeg 截一帧」换成「产出 frame_file 这个文件」。

    `storage.abs_path` 一并指过去，免得测试往真实数据目录里写东西。
    给一个内容不是图片的文件，就等价于「尺寸读不出来」——实现应当退回源视频的宽高。
    """
    real_extract = ffmpeg_service.extract_frame
    real_save = ffmpeg_service._save_asset_file
    real_abs = storage.abs_path

    async def fake_extract(path, t):  # noqa: ANN001, ARG001
        return frame_file

    ffmpeg_service.extract_frame = fake_extract
    ffmpeg_service._save_asset_file = lambda p, ext: frame_file.name  # noqa: ARG005
    storage.abs_path = lambda name: frame_file  # noqa: ARG005
    try:
        yield
    finally:
        ffmpeg_service.extract_frame = real_extract
        ffmpeg_service._save_asset_file = real_save
        storage.abs_path = real_abs


@contextlib.contextmanager
def _stub_clip(clip_file: Path, probed: dict | None = None) -> Iterator[dict]:
    """把「截取片段」换成「产出 clip_file」；`probed` 为 None 时禁止调用 ffprobe。

    返回一个计数器，用来断言「源视频已经有宽高时不该多跑一次 ffprobe」。
    """
    calls = {"probe": 0}
    real_extract = ffmpeg_service.extract_clip
    real_save = ffmpeg_service._save_asset_file
    real_abs = storage.abs_path
    real_probe = ffmpeg_service.probe

    async def fake_extract(path, start, end):  # noqa: ANN001, ARG001
        return clip_file

    async def fake_probe(path):  # noqa: ANN001, ARG001
        calls["probe"] += 1
        if probed is None:
            raise AssertionError("源视频已经有宽高，不该再多跑一次 ffprobe")
        return probed

    ffmpeg_service.extract_clip = fake_extract
    ffmpeg_service._save_asset_file = lambda p, ext: clip_file.name  # noqa: ARG005
    ffmpeg_service.probe = fake_probe
    storage.abs_path = lambda name: clip_file  # noqa: ARG005
    try:
        yield calls
    finally:
        ffmpeg_service.extract_clip = real_extract
        ffmpeg_service._save_asset_file = real_save
        ffmpeg_service.probe = real_probe
        storage.abs_path = real_abs


def test_frame_size_comes_from_the_file_not_the_video():
    """源视频没记宽高（老素材）时，截出来的帧必须自己带上真实尺寸。"""
    with tempfile.TemporaryDirectory() as tmp:
        frame = Path(tmp) / "frame.png"
        frame.write_bytes(_png(1280, 720))

        async def scenario(db):
            video = await _add_video(db)  # 宽高为空
            with _stub_frame(frame):
                out = await director.extract_thumbnail(
                    DirectorThumbnailIn(asset_id=video.id, t=0), db
                )
            assert (out.width, out.height) == (1280, 720), (
                f"截帧的宽高来自源视频而不是文件：{(out.width, out.height)}"
                "——老视频没记宽高，这条链下游的首帧告警就全哑了"
            )

        _run(scenario)


def test_frame_of_a_720p_clip_needs_no_guessing():
    """分辨率不同（竖屏）时同样以文件为准，而不是照抄视频的旧值。"""
    with tempfile.TemporaryDirectory() as tmp:
        frame = Path(tmp) / "frame.png"
        frame.write_bytes(_png(720, 1280))

        async def scenario(db):
            video = await _add_video(db, width=1920, height=1080)  # 记错了也不影响
            with _stub_frame(frame):
                out = await director.extract_thumbnail(
                    DirectorThumbnailIn(asset_id=video.id, t=1.5), db
                )
            assert (out.width, out.height) == (720, 1280)

        _run(scenario)


def test_falls_back_to_the_video_when_the_file_cannot_be_read():
    """文件读不出来时退回源视频的宽高：字段空着的代价是下游静默失效。"""
    with tempfile.TemporaryDirectory() as tmp:
        broken = Path(tmp) / "frame.bin"
        broken.write_bytes(b"\x00\x01\x02")  # 文件在，但不是能识别的图片

        async def scenario(db):
            video = await _add_video(db, width=1920, height=1080)
            with _stub_frame(broken):
                out = await director.extract_thumbnail(
                    DirectorThumbnailIn(asset_id=video.id, t=0), db
                )
            assert (out.width, out.height) == (1920, 1080)

        _run(scenario)


def test_no_size_anywhere_is_not_an_error():
    """两头都没有尺寸也不能抛异常：这只是少了一条提醒，不该让截帧失败。"""
    with tempfile.TemporaryDirectory() as tmp:
        broken = Path(tmp) / "frame.bin"
        broken.write_bytes(b"\x00\x01\x02")

        async def scenario(db):
            video = await _add_video(db)
            with _stub_frame(broken):
                out = await director.extract_thumbnail(
                    DirectorThumbnailIn(asset_id=video.id, t=0), db
                )
            assert out.width is None and out.height is None

        _run(scenario)


def test_extracted_frame_is_a_real_image_asset():
    """截帧必须落成 `kind=image` 的资产——视频生成页的首帧只认图片。"""
    with tempfile.TemporaryDirectory() as tmp:
        frame = Path(tmp) / "frame.png"
        frame.write_bytes(_png(1024, 1024))

        async def scenario(db):
            video = await _add_video(db)
            with _stub_frame(frame):
                out = await director.extract_thumbnail(
                    DirectorThumbnailIn(asset_id=video.id, t=0), db
                )
            assert out.kind == "image"
            assert out.source == "frame"
            assert out.url.startswith("/media/")

        _run(scenario)


def test_clip_asks_the_product_when_the_source_has_no_size():
    """同一处口径也用在「截取片段」上：源没记宽高就去问产物，别一路空下去。"""
    with tempfile.TemporaryDirectory() as tmp:
        clip = Path(tmp) / "clip.mp4"
        clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(64))

        async def scenario(db):
            src = await _add_video(db)  # 老素材：宽高为空
            with _stub_clip(clip, probed={"width": 320, "height": 176, "duration": 9.98}) as calls:
                out = await director.extract_clip(
                    DirectorExtractIn(asset_id=src.id, start=0, end=10), db
                )
            assert calls["probe"] == 1
            assert (out.width, out.height) == (320, 176)
            # 时长按用户点的区间算：probe 的 9.98 取整会变成「截了 10 秒显示 9 秒」
            assert out.duration == 10

        _run(scenario)


def test_clip_does_not_probe_when_the_source_already_knows_its_size():
    """源里已经有宽高时不要多跑一次 ffprobe（每个片段一次，攒起来是白等的钱）。"""
    with tempfile.TemporaryDirectory() as tmp:
        clip = Path(tmp) / "clip.mp4"
        clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(64))

        async def scenario(db):
            src = await _add_video(db, width=1280, height=720)
            # probed=None：真调了 probe 就会抛 AssertionError
            with _stub_clip(clip) as calls:
                out = await director.extract_clip(
                    DirectorExtractIn(asset_id=src.id, start=1, end=4), db
                )
            assert calls["probe"] == 0
            assert (out.width, out.height) == (1280, 720)
            assert out.duration == 3

        _run(scenario)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
