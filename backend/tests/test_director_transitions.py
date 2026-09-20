"""#38 转场与转场音效（直接 python 运行）。

运行：venv/Scripts/python tests/test_director_transitions.py

为什么这一项要单测：转场有两个**不报错但结果是错的**陷阱，用户从界面上看不出来：

1. **成片会变短**。`xfade` 是让相邻两段交叠，不是插一段新的：三段各 5 秒、转场 1 秒，
   成片 13 秒而不是 15 秒。这条必须在合并**之前**就说出来，所以口径（`total_seconds` /
   `shortfall`）得能被直接断言，而不是靠跑一次编码去量。
2. **硬切那条「流拷贝直拼」的快路只对同构素材成立**。素材尺寸 / 帧率 / 有没有音轨
   但凡不一致，concat demuxer 接出来的片子时间戳就是错的，而命令**照样成功返回**——
   「三段共 15 秒导出成 18.75 秒」这种结果是悄悄给出去的。所以这里钉住：
   不同构时必须走重编码，同构时老快路不能被牺牲（否则所有老流程平白变慢）。

还有一处：素材里有的段没音轨时，老的三级回退会退到「整条丢掉音轨」——把本来有声音的
段也一起弄哑。现在改成给缺音轨的段补一条等长静音，所以「补静音」这件事也要钉住。

真跑 ffmpeg 的验收另有一份（量成片时长、取帧看转场、量音量看音效），这里只测口径与命令。
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Iterator

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import Asset  # noqa: E402
from app.routers import director  # noqa: E402
from app.schemas import DirectorMergeIn  # noqa: E402
from app.services import ffmpeg_service, storage, transitions  # noqa: E402


def _info(**over) -> dict:
    """一条完整的 probe 结果。默认是「640x360@30 有音轨的 h264/aac」。"""
    base = {
        "duration": 5.0,
        "width": 640,
        "height": 360,
        "codec": "h264",
        "has_audio": True,
        "has_video": True,
        "fps": 30.0,
        "audio_codec": "aac",
        "audio_rate": 44100,
        "audio_channels": 2,
    }
    base.update(over)
    return base


@contextlib.contextmanager
def _stub_ffmpeg(probes: dict[str, dict], cmds: list[list[str]]) -> Iterator[None]:
    """把 ffmpeg 换掉：`probe` 按**文件名**回表里的元信息，`_run` 只记录命令并产出占位文件。

    `probes` 的键是文件名（如 `"a.mp4"`），这样测试里造几条假素材就够了，不必真编码。
    """
    real_probe = ffmpeg_service.probe
    real_run = ffmpeg_service._run
    real_bins = ffmpeg_service._resolve_binaries

    async def fake_probe(path):  # noqa: ANN001
        return dict(probes[str(Path(path).name)], **{})

    async def fake_run(cmd, timeout):  # noqa: ANN001, ARG001
        cmds.append(list(cmd))
        out = Path(cmd[-1])
        if out.suffix:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(32))
        return True, ""

    async def fake_bins():
        return "ffmpeg", "ffprobe"

    ffmpeg_service.probe = fake_probe
    ffmpeg_service._run = fake_run
    ffmpeg_service._resolve_binaries = fake_bins
    try:
        yield
    finally:
        ffmpeg_service.probe = real_probe
        ffmpeg_service._run = real_run
        ffmpeg_service._resolve_binaries = real_bins


def _run(fn):
    """开一个内存库跑 `fn(db)`，并把它的返回值交回来（有些用例要拿 `_resolve_sfx` 的结果）。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            async with maker() as db:
                return await fn(db)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


async def _add(db, kind: str = "video", name: str = "a.mp4", **over) -> Asset:
    a = Asset(
        kind=kind,
        filename=name,
        original_name=name,
        content_type="video/mp4" if kind == "video" else "audio/mpeg",
        size=1024,
        **over,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


# ------------------------------------------------------------------ 纯口径


def test_unknown_transition_falls_back_to_hard_cut():
    """认不出的转场当硬切，不报错：老画布 / 分享码里可能压根没有这个字段。"""
    assert transitions.sanitize_key(None) == "cut"
    assert transitions.sanitize_key("") == "cut"
    assert transitions.sanitize_key("不存在的转场") == "cut"
    assert transitions.sanitize_key("dissolve") == "dissolve"
    assert transitions.is_cut("cut") and not transitions.is_cut("dissolve")


def test_every_preset_is_self_consistent():
    """预设表内部的 key 必须唯一、label/hint 都不为空——前端下拉直接用这张表。"""
    keys = [p["key"] for p in transitions.PRESETS]
    assert len(keys) == len(set(keys)), f"转场预设 key 重复：{keys}"
    assert keys[0] == "cut", "第一项必须是硬切（默认值）"
    for p in transitions.PRESETS:
        assert p["label"] and p["hint"], p
        assert transitions.sanitize_key(p["key"]) == p["key"], f"{p['key']} 自己都认不出来"
    # xfade 只认 ASCII 名字；中文 label 不进命令行
    for p in transitions.PRESETS:
        assert p["xfade"].isascii(), p


def test_sfx_presets_use_ascii_keys():
    """音效预设的 key 会进命令行与 lavfi 表达式，必须是 ASCII；中文只放在 label。"""
    assert transitions.SFX_PRESETS[0]["key"] == "", "第一项必须是「不加音效」"
    keys = [p["key"] for p in transitions.SFX_PRESETS]
    assert len(keys) == len(set(keys))
    for p in transitions.SFX_PRESETS:
        assert p["key"].isascii(), p
        assert p["label"] and p["hint"], p
    for k in transitions.SFX_RECIPES:
        assert k.isascii() and k in keys, f"配方 {k} 没在下拉里"


def test_transition_seconds_is_clamped_not_rejected():
    """转场时长超范围就夹到边界（改一下就好，不该报错）；读不出来才用默认。"""
    assert transitions.sanitize_seconds(None) == transitions.DEFAULT_SECONDS
    assert transitions.sanitize_seconds("abc") == transitions.DEFAULT_SECONDS
    assert transitions.sanitize_seconds(float("nan")) == transitions.DEFAULT_SECONDS
    assert transitions.sanitize_seconds(0.01) == transitions.MIN_SECONDS
    assert transitions.sanitize_seconds(99) == transitions.MAX_SECONDS
    assert transitions.sanitize_seconds("0.5") == 0.5


def test_total_seconds_accounts_for_the_overlap():
    """成片时长 = 各段之和 - 转场 x (n-1)；硬切就是各段之和。"""
    lens = [5.0, 4.0, 6.0]
    assert transitions.total_seconds(lens, "cut", 0.5) == 15.0
    assert abs(transitions.total_seconds(lens, "dissolve", 0.5) - 14.0) < 1e-9
    assert abs(transitions.shortfall(lens, "dissolve", 0.5) - 1.0) < 1e-9
    # 一段、两段、空
    assert transitions.total_seconds([5.0], "dissolve", 1.0) == 5.0
    assert transitions.total_seconds([], "dissolve", 1.0) == 0.0
    assert abs(transitions.total_seconds([2.0, 2.0], "dissolve", 1.0) - 3.0) < 1e-9


def test_shortfall_is_zero_for_hard_cut():
    """硬切时不能说「会变短」——那会平白吓用户一跳，而片子一秒没短。"""
    lens = [5.0, 4.0, 6.0]
    assert transitions.shortfall(lens, "cut", 0.5) == 0.0
    assert transitions.shortfall([5.0], "dissolve", 0.5) == 0.0
    assert transitions.shortfall(lens, "none", 0.5) == 0.0  # 认不出的也算硬切


def test_check_clips_blocks_a_transition_that_does_not_fit():
    """转场放不下要拦住，并说清是**第几段**、多少秒——别让 ffmpeg 回一句 Invalid duration。"""
    msg = transitions.check_clips([5.0, 0.8, 6.0], "dissolve", 1.0)
    assert msg, "转场 1 秒放进 0.8 秒的段里竟然放行了"
    assert "0.8" in msg and "1" in msg, f"理由里没说清是哪一段/多长：{msg}"
    assert "第 2 段" in msg, f"没指明第几段：{msg}"


def test_check_clips_blocks_unreadable_duration():
    """读不出时长必须拦：转场位置是按它算的，算不出来还硬接，画面会错位而不一定报错。"""
    msg = transitions.check_clips([5.0, None, 6.0], "dissolve", 0.5)
    assert msg and "第 2 段" in msg, msg


def test_check_clips_never_blocks_hard_cut():
    """硬切是老行为，不能被新加的校验挡下来：老画布、老分享码都得照旧能用。"""
    assert transitions.check_clips([5.0, None, 0.1], "cut", 1.0) == ""
    assert transitions.check_clips([5.0, None], "不存在的转场", 1.0) == ""
    # 放得下就放行
    assert transitions.check_clips([5.0, 1.2, 6.0], "dissolve", 1.0) == ""


def test_sfx_is_a_touch_longer_than_the_transition():
    """音效要比转场长一个尾巴，否则「呼」的一声会跟着转场一起被切掉。"""
    assert abs(transitions.sfx_seconds(0.5) - (0.5 + transitions.SFX_TAIL)) < 1e-9
    assert transitions.sfx_seconds(0.5) > 0.5
    # 上限：不能因为有人把转场拉到 2 秒就合出半分钟的音效
    assert transitions.sfx_seconds(transitions.MAX_SECONDS) <= transitions.MAX_SECONDS + transitions.SFX_TAIL


def test_sfx_key_unknown_means_no_sfx():
    assert transitions.sanitize_sfx(None) == ""
    assert transitions.sanitize_sfx("whoosh") == "whoosh"
    assert transitions.sanitize_sfx("../../evil") == ""
    assert transitions.sanitize_sfx("不存在") == ""


# ------------------------------------------------------------------ 硬切的两条路


def test_same_shape_clips_still_take_the_copy_fast_path():
    """同构素材（同一段源视频切出来的）必须继续走流拷贝：老流程不能平白变慢。"""
    with tempfile.TemporaryDirectory() as tmp:
        settings.DATA_DIR = tmp
        a, b = Path(tmp) / "a.mp4", Path(tmp) / "b.mp4"
        cmds: list[list[str]] = []
        with _stub_ffmpeg({"a.mp4": _info(), "b.mp4": _info()}, cmds):
            asyncio.run(ffmpeg_service.merge_videos([a, b], transition="cut"))
        assert any("-c" in c and "copy" in c for c in cmds), f"没走流拷贝：{cmds}"
        assert not any("-filter_complex" in c for c in cmds), "同构素材不该重编码"


def test_mismatched_clips_skip_the_copy_fast_path():
    """不同构素材**不能**直拼：concat demuxer 会把时间戳接错，而且命令照样成功。

    这是本条的实际缺陷：三段共 15 秒的素材导出成 18.75 秒、音轨和画面对不上，
    不报错、没有提示。
    """
    with tempfile.TemporaryDirectory() as tmp:
        settings.DATA_DIR = tmp
        paths = [Path(tmp) / n for n in ("a.mp4", "b.mp4", "c.mp4")]
        probes = {
            "a.mp4": _info(),
            "b.mp4": _info(width=480, height=480, fps=30.0),   # 尺寸不同
            "c.mp4": _info(width=1280, height=720, fps=60.0),  # 尺寸帧率都不同
        }
        cmds: list[list[str]] = []
        with _stub_ffmpeg(probes, cmds):
            asyncio.run(ffmpeg_service.merge_videos(paths, transition="cut"))
        assert cmds, "没有跑任何 ffmpeg 命令"
        assert not any("-f" in c and "concat" in c and "copy" in c for c in cmds), (
            "不同构素材仍然走了流拷贝直拼——这正是那个静默出错的地方"
        )
        last = cmds[-1]
        assert "-filter_complex" in last, f"没走重编码：{last}"
        graph = last[last.index("-filter_complex") + 1]
        assert "concat=n=3:v=1:a=1" in graph, graph


def test_blocker_names_the_difference():
    """拦下来的时候要说清是哪一段的哪一项不同——不然日志里只有一句「不同构」，查不动。"""

    async def scenario():
        paths = [Path("a.mp4"), Path("b.mp4")]

        async def fake_probe(p):
            return _info() if Path(p).name == "a.mp4" else _info(has_audio=False, audio_codec=None,
                                                                audio_rate=None, audio_channels=None)

        real = ffmpeg_service.probe
        ffmpeg_service.probe = fake_probe
        try:
            return await ffmpeg_service._copy_concat_blocker(paths)
        finally:
            ffmpeg_service.probe = real

    reason = asyncio.run(scenario())
    assert reason and "has_audio" in reason, f"没指明不同之处：{reason}"
    assert "第 2 段" in reason, reason


def test_one_clip_is_never_blocked():
    """只有一段就没什么可「接」的（比如只有一镜的样片），不该白白重编码一遍。"""

    async def scenario():
        real = ffmpeg_service.probe

        async def fake_probe(p):  # noqa: ANN001, ARG001
            raise AssertionError("只有一段时不该去问元信息")

        ffmpeg_service.probe = fake_probe
        try:
            return await ffmpeg_service._copy_concat_blocker([Path("only.mp4")])
        finally:
            ffmpeg_service.probe = real

    assert asyncio.run(scenario()) is None


# ------------------------------------------------------------------ 归一化（补静音）


def test_normalized_graph_fills_in_a_silent_track():
    """有的段没音轨时要**补一条等长静音**，而不是把整条音轨丢掉。

    丢掉的话，本来有声音的段也会被弄哑，而且不报错——这是老三级回退最坏的结果。
    """
    with tempfile.TemporaryDirectory() as tmp:
        settings.DATA_DIR = tmp
        paths = [Path(tmp) / n for n in ("a.mp4", "b.mp4")]
        probes = {"a.mp4": _info(duration=5.0), "b.mp4": _info(duration=4.0, has_audio=False,
                                                             audio_codec=None, audio_rate=None,
                                                             audio_channels=None)}
        cmds: list[list[str]] = []
        with _stub_ffmpeg(probes, cmds):
            inputs, chains, vlabels, alabels, lens, infos, size = asyncio.run(
                ffmpeg_service._normalized_graph(paths)
            )
        assert lens == [5.0, 4.0], lens
        assert vlabels == ["v0", "v1"] and alabels == ["a0", "a1"]
        # 缺音轨的第二段补了 anullsrc，且长度与它自己一致（不是与第一段一致）
        assert inputs.count("-i") == 3, inputs
        assert any("anullsrc" in x for x in inputs), inputs
        assert inputs[6:10] == ["-t", "4.000", "-i", "anullsrc=r=48000:cl=stereo"], inputs
        joined = ";".join(chains)
        assert "[2:a]aformat" in joined, f"第二段的音轨没接到补的静音上：{joined}"
        assert "[1:a]aformat" not in joined, "第二段没有音轨，不该去引用 1:a"
        # 音频要裁到与画面等长，否则每接一次就累积一点偏移
        assert "atrim=0:4.000" in joined and "atrim=0:5.000" in joined, joined
        assert (size[0] % 2, size[1] % 2) == (0, 0), f"宽高必须是偶数：{size}"


def test_normalized_graph_hard_cut_carries_the_audio_through():
    """硬切走重编码时也要把音轨带上（`concat=v=1:a=1`），不能默认无声。"""
    with tempfile.TemporaryDirectory() as tmp:
        settings.DATA_DIR = tmp
        paths = [Path(tmp) / n for n in ("a.mp4", "b.mp4")]
        cmds: list[list[str]] = []
        with _stub_ffmpeg({"a.mp4": _info(), "b.mp4": _info()}, cmds):
            asyncio.run(ffmpeg_service._merge_normalized(paths))
        last = cmds[-1]
        assert "-map" in last and "[aout]" in last, f"没带音轨：{last}"


def test_normalized_graph_refuses_unreadable_duration():
    """时长读不出来要拦住并指明第几段——按它算位置的东西会错位。"""
    with tempfile.TemporaryDirectory() as tmp:
        settings.DATA_DIR = tmp
        paths = [Path(tmp) / n for n in ("a.mp4", "b.mp4")]
        cmds: list[list[str]] = []
        with _stub_ffmpeg({"a.mp4": _info(duration=None), "b.mp4": _info()}, cmds):
            try:
                asyncio.run(ffmpeg_service._normalized_graph(paths))
                raise AssertionError("时长读不出来竟然没拦")
            except RuntimeError as e:
                assert "第 1 段" in str(e), str(e)


# ------------------------------------------------------------------ 转场链


def test_transition_takes_the_sfx_as_the_last_input():
    """音效是 chain 上最后一个输入，索引不能算错（算错就混进别的段的声音了）。"""
    with tempfile.TemporaryDirectory() as tmp:
        settings.DATA_DIR = tmp
        paths = [Path(tmp) / n for n in ("a.mp4", "b.mp4")]
        sfx = Path(tmp) / "sfx.wav"
        sfx.write_bytes(b"RIFF0000WAVE")
        # 第二段没音轨 -> 会补一个输入，音效索引就该是 3
        probes = {"a.mp4": _info(duration=5.0), "b.mp4": _info(duration=4.0, has_audio=False,
                                                             audio_codec=None, audio_rate=None,
                                                             audio_channels=None)}
        cmds: list[list[str]] = []
        with _stub_ffmpeg(probes, cmds):
            asyncio.run(ffmpeg_service.merge_videos(
                paths, transition="dissolve", transition_seconds=0.5, sfx_path=sfx
            ))
        last = cmds[-1]
        graph = last[last.index("-filter_complex") + 1]
        assert "[3:a]aformat" in graph, f"音效的输入索引算错了：{graph}"
        assert "asplit=1" in graph, graph
        # offset = 5 - 0.5 = 4.5 -> adelay 4500ms
        assert "adelay=4500|4500" in graph, f"音效没贴到转场点上：{graph}"
        assert "amix=inputs=2:duration=first:normalize=0" in graph, graph
        assert "xfade=transition=dissolve:duration=0.5:offset=4.500" in graph, graph
        assert "acrossfade=d=0.5" in graph, graph


def test_every_preset_uses_its_own_xfade_name():
    """预设 key 与 `xfade` 滤镜名**不是一回事**，逐个钉住。

    「过黑」的 key 是 `fade`、滤镜名却是 `fadeblack`；`wipe` 压根没有同名的滤镜。
    拿 key 当滤镜名会掉进两个坑：名字碰巧存在（`fade` 就是），于是「过黑」悄悄变成
    普通叠化、不报错；名字不存在（`wipe`）才报错。两种都不该出现。
    """
    probes = {"a.mp4": _info(duration=5.0), "b.mp4": _info(duration=4.0)}
    for preset in transitions.PRESETS:
        if not preset["xfade"]:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            settings.DATA_DIR = tmp
            paths = [Path(tmp) / n for n in ("a.mp4", "b.mp4")]
            cmds: list[list[str]] = []
            with _stub_ffmpeg(probes, cmds):
                asyncio.run(ffmpeg_service.merge_videos(
                    paths, transition=preset["key"], transition_seconds=0.5
                ))
            graph = cmds[-1][cmds[-1].index("-filter_complex") + 1]
            assert f"transition={preset['xfade']}:duration" in graph, (
                f"{preset['key']} 用的是滤镜名 {preset['xfade']}，实际发出去的是：{graph}"
            )


def test_sfx_is_placed_at_every_transition():
    """三段两处转场，音效就该贴两份（不是只在第一处响）。"""
    with tempfile.TemporaryDirectory() as tmp:
        settings.DATA_DIR = tmp
        paths = [Path(tmp) / n for n in ("a.mp4", "b.mp4", "c.mp4")]
        sfx = Path(tmp) / "sfx.wav"
        sfx.write_bytes(b"RIFF0000WAVE")
        probes = {"a.mp4": _info(duration=5.0), "b.mp4": _info(duration=4.0), "c.mp4": _info(duration=6.0)}
        cmds: list[list[str]] = []
        with _stub_ffmpeg(probes, cmds):
            asyncio.run(ffmpeg_service.merge_videos(
                paths, transition="fade", transition_seconds=0.5, sfx_path=sfx
            ))
        graph = cmds[-1][cmds[-1].index("-filter_complex") + 1]
        assert "asplit=2" in graph, graph
        # 第二处：running = 5+4-0.5 = 8.5 -> offset = 8.0
        assert "adelay=4500|4500" in graph and "adelay=8000|8000" in graph, graph
        assert "xfade=transition=fadeblack" in graph, graph


def test_hard_cut_never_touches_the_sfx():
    """硬切时不合成、不混音效：快路不该因为多了一个参数就变慢。"""
    with tempfile.TemporaryDirectory() as tmp:
        settings.DATA_DIR = tmp
        paths = [Path(tmp) / n for n in ("a.mp4", "b.mp4")]
        sfx = Path(tmp) / "sfx.wav"
        sfx.write_bytes(b"RIFF0000WAVE")
        cmds: list[list[str]] = []
        with _stub_ffmpeg({"a.mp4": _info(), "b.mp4": _info()}, cmds):
            asyncio.run(ffmpeg_service.merge_videos(
                paths, transition="cut", transition_seconds=0.5, sfx_path=sfx
            ))
        assert not any(str(sfx) in arg for c in cmds for arg in c), "硬切竟然用上了音效文件"


def test_synth_sfx_rejects_unknown_kind():
    """认不出的音效要报错而不是合出一段莫名声音。"""

    async def scenario():
        try:
            await ffmpeg_service.synth_sfx("../../evil", 0.5)
            return None
        except RuntimeError as e:
            return str(e)

    msg = asyncio.run(scenario())
    assert msg and "evil" in msg, msg


# ------------------------------------------------------------------ 接口


def test_transitions_endpoint_matches_the_presets():
    """`GET /transitions` 回的三块就是前端下拉的全部来源，不另抄一份。"""
    data = asyncio.run(director.list_transitions())
    assert [p["key"] for p in data["presets"]] == [p["key"] for p in transitions.PRESETS]
    assert [p["key"] for p in data["sfx"]] == [p["key"] for p in transitions.SFX_PRESETS]
    assert data["seconds"] == {
        "min": transitions.MIN_SECONDS,
        "max": transitions.MAX_SECONDS,
        "default": transitions.DEFAULT_SECONDS,
    }


def test_merge_preview_does_the_same_math_as_the_merge():
    """预览与合并必须同一套口径：两处各算一次，「弹窗写 13 秒、导出来 12.5 秒」最难解释。"""
    with tempfile.TemporaryDirectory() as tmp:
        settings.DATA_DIR = tmp

        async def scenario(db):
            ids = []
            for i, d in enumerate([5.0, 4.0, 6.0]):
                a = await _add(db, name=f"{i}.mp4", duration=int(d))
                ids.append(a.id)
            cmds: list[list[str]] = []
            with _stub_ffmpeg({"0.mp4": _info(duration=5.0), "1.mp4": _info(duration=4.0),
                               "2.mp4": _info(duration=6.0)}, cmds):
                out = await director.merge_preview(
                    DirectorMergeIn(asset_ids=ids, transition="dissolve", transition_seconds=0.5), db
                )
            assert out["totalSeconds"] == 14.0, out
            assert out["hardCutSeconds"] == 15.0, out
            assert out["shortfallSeconds"] == 1.0, out
            assert out["problem"] == "", out
            assert out["clipSeconds"] == [5.0, 4.0, 6.0], out
            assert cmds == [], f"预览是纯读，不该写盘或编码：{cmds}"

        _run(scenario)


def test_merge_preview_reports_a_transition_that_does_not_fit():
    with tempfile.TemporaryDirectory() as tmp:
        settings.DATA_DIR = tmp

        async def scenario(db):
            ids = []
            for i in range(2):
                a = await _add(db, name=f"{i}.mp4", duration=3)
                ids.append(a.id)
            cmds: list[list[str]] = []
            with _stub_ffmpeg({"0.mp4": _info(duration=0.4), "1.mp4": _info(duration=3.0)}, cmds):
                out = await director.merge_preview(
                    DirectorMergeIn(asset_ids=ids, transition="dissolve", transition_seconds=0.5), db
                )
            assert out["problem"], out
            assert "第 1 段" in out["problem"], out

        _run(scenario)


def test_merge_returns_400_when_the_transition_does_not_fit():
    """放不下是**输入问题**，回 400 并说清；不能扔给 ffmpeg 报一句 Invalid duration。"""
    with tempfile.TemporaryDirectory() as tmp:
        settings.DATA_DIR = tmp

        async def scenario(db):
            ids = []
            for i in range(2):
                a = await _add(db, name=f"{i}.mp4", duration=3)
                ids.append(a.id)
            cmds: list[list[str]] = []
            with _stub_ffmpeg({"0.mp4": _info(duration=0.4), "1.mp4": _info(duration=3.0)}, cmds):
                try:
                    await director.merge_videos(
                        DirectorMergeIn(asset_ids=ids, transition="dissolve", transition_seconds=0.5), db
                    )
                    raise AssertionError("放不下的转场竟然合并下去了")
                except HTTPException as e:
                    assert e.status_code == 400, e.status_code
                    assert "第 1 段" in str(e.detail), e.detail
            assert cmds == [], f"拦下来了却已经跑了 ffmpeg：{cmds}"

        _run(scenario)


def test_merge_refuses_a_missing_sfx_asset():
    """音效资产选错了要报错，不能静默降级成「没有音效」——用户会以为生效了。"""
    with tempfile.TemporaryDirectory() as tmp:
        settings.DATA_DIR = tmp

        async def scenario(db):
            ids = []
            for i in range(2):
                a = await _add(db, name=f"{i}.mp4", duration=3)
                ids.append(a.id)
            cmds: list[list[str]] = []
            with _stub_ffmpeg({"0.mp4": _info(), "1.mp4": _info()}, cmds):
                try:
                    await director.merge_videos(
                        DirectorMergeIn(asset_ids=ids, transition="dissolve", sfx_asset_id=9999), db
                    )
                    raise AssertionError("不存在的音效资产竟然通过了")
                except HTTPException as e:
                    assert e.status_code == 400, e.status_code
                    # 「不在了」与「不是音频」要分开说：两种情况用户要改的地方不一样
                    assert "不在" in str(e.detail), e.detail

        _run(scenario)


def test_merge_refuses_a_non_audio_sfx_asset():
    with tempfile.TemporaryDirectory() as tmp:
        settings.DATA_DIR = tmp

        async def scenario(db):
            ids = []
            for i in range(2):
                a = await _add(db, name=f"{i}.mp4", duration=3)
                ids.append(a.id)
            wrong = await _add(db, kind="image", name="pic.png")
            cmds: list[list[str]] = []
            with _stub_ffmpeg({"0.mp4": _info(), "1.mp4": _info()}, cmds):
                try:
                    await director.merge_videos(
                        DirectorMergeIn(asset_ids=ids, transition="dissolve", sfx_asset_id=wrong.id), db
                    )
                    raise AssertionError("拿一张图片当音效竟然通过了")
                except HTTPException as e:
                    assert e.status_code == 400 and "音频" in str(e.detail), e.detail

        _run(scenario)


def test_resolve_sfx_is_a_noop_for_hard_cut():
    """硬切时不去合成音效：`_resolve_sfx` 直接回空，别多做一次编码。"""
    calls = {"synth": 0}
    real = ffmpeg_service.synth_sfx

    async def fake_synth(kind, seconds):  # noqa: ANN001, ARG001
        calls["synth"] += 1
        return Path("never.wav")

    ffmpeg_service.synth_sfx = fake_synth

    async def scenario(db):
        payload = DirectorMergeIn(asset_ids=[1, 2], transition="cut", sfx="whoosh")
        return await director._resolve_sfx(payload, "cut", 0.5, db)

    try:
        path, label, tmp_dir = _run(scenario)
    finally:
        ffmpeg_service.synth_sfx = real
    assert path is None and label == "" and tmp_dir is None
    assert calls["synth"] == 0, "硬切时不该去合成音效"


def test_resolve_sfx_labels_the_builtin_recipe():
    """内置配方要落一个中文标签，资产库里才看得出这条片子用了什么音效。"""
    real = ffmpeg_service.synth_sfx
    made: dict[str, Path] = {}

    async def fake_synth(kind, seconds):  # noqa: ANN001
        p = Path(tempfile.mkdtemp()) / f"sfx-{kind}.wav"
        p.write_bytes(b"RIFF0000WAVE")
        made["path"] = p
        made["seconds"] = seconds
        return p

    ffmpeg_service.synth_sfx = fake_synth

    async def scenario(db):
        payload = DirectorMergeIn(asset_ids=[1, 2], transition="dissolve", sfx="whoosh")
        return await director._resolve_sfx(payload, "dissolve", 0.5, db)

    try:
        path, label, tmp_dir = _run(scenario)
    finally:
        ffmpeg_service.synth_sfx = real
        if "path" in made:
            shutil.rmtree(made["path"].parent, ignore_errors=True)
    assert path is not None and label and "whoosh" in label, (path, label)
    # 合成时长要比转场长一个尾巴
    assert made["seconds"] == transitions.sfx_seconds(0.5), made
    # 临时目录要交回给调用方删（不然会堆在 storage/tmp 里）
    assert tmp_dir == made["path"].parent, (tmp_dir, made["path"].parent)


def test_resolve_sfx_prefers_the_users_own_audio():
    """给了资产库音频就以它为准（用户自己的素材优先于内置配方）。"""
    with tempfile.TemporaryDirectory() as tmp:
        settings.DATA_DIR = tmp

        async def scenario(db):
            audio_path = storage.abs_path("own.wav")
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            audio_path.write_bytes(b"RIFF0000WAVE")
            own = await _add(db, kind="audio", name="own.wav")
            own.name = "我的音效"
            await db.commit()
            payload = DirectorMergeIn(
                asset_ids=[1, 2], transition="dissolve", sfx="whoosh", sfx_asset_id=own.id
            )
            return await director._resolve_sfx(payload, "dissolve", 0.5, db)

        path, label, tmp_dir = _run(scenario)
        assert path is not None and path.name == "own.wav", path
        assert label == "我的音效", label
        assert tmp_dir is None, "用的是用户自己的文件，没有临时目录要删"

        # 合成音效没被调用过（有人把它忘了就会多合成一份白干活）
        assert not (Path(tmp) / "storage" / "tmp").exists()


def test_merge_note_records_the_transition_in_the_asset():
    """合并产物的来路要写进 prompt：资产库里一眼看得出这条片子是怎么接的。"""
    with tempfile.TemporaryDirectory() as tmp:
        settings.DATA_DIR = tmp

        async def scenario(db):
            ids = []
            for i in range(3):
                a = await _add(db, name=f"{i}.mp4", duration=4)
                ids.append(a.id)
            probes = {f"{i}.mp4": _info(duration=5.0 if i == 0 else 4.0) for i in range(3)}
            probes["merged.mp4"] = _info(duration=13.0)  # 成片时长以量出来的为准
            cmds: list[list[str]] = []
            real_save = ffmpeg_service._save_asset_file
            real_abs = storage.abs_path
            with _stub_ffmpeg(probes, cmds):
                ffmpeg_service._save_asset_file = lambda p, ext: "2026-09/merged.mp4"  # noqa: ARG005
                storage.abs_path = lambda name: Path(tmp) / "merged.mp4"  # noqa: ARG005
                try:
                    out = await director.merge_videos(
                        DirectorMergeIn(asset_ids=ids, transition="dissolve", transition_seconds=0.5), db
                    )
                finally:
                    ffmpeg_service._save_asset_file = real_save
                    storage.abs_path = real_abs
            assert "3 段" in (out.prompt or ""), out.prompt
            assert "叠化" in (out.prompt or ""), out.prompt
            assert "0.5" in (out.prompt or ""), out.prompt
            # 成片时长以**量出来的**为准（这里量到 13 秒，按各段算是 12 秒）：
            # 按估算落库的话，导演台列表和实际片子会一直差着
            assert out.duration == 13, out.duration

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
