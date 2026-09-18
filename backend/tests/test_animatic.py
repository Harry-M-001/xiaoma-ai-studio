"""#29 静图缓动样片（animatic）的回归测试（直接 python 运行）。

运行：venv/Scripts/python tests/test_animatic.py

分三层，越往下越"真"：

1. **纯逻辑**：运镜文字 → 缓动参数、尺寸计算、滤镜表达式。不需要 ffmpeg。
2. **接线**（`canvas_runner.render_animatic`）：分镜表与已出的图怎么配对、
   对不上时说什么、锁是否生效。ffmpeg 与 storage 都被替身接管，
   断言的是「交出去的镜头参数对不对」。
3. **真跑一遍 ffmpeg**（没有 ffmpeg 就跳过）：最重要的是**方向**——
   只验「画面变了没有」不够，推和拉、左摇和右摇在数值上都会让画面变。
   所以这里用满屏细节的 testsrc 测试图，把手算出来的取景框当标准答案逐像素比。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterator

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import Asset, Project, Task  # noqa: E402
from app.services import animatic, canvas_runner, ffmpeg_service, storage, storyboard_lint  # noqa: E402
from app.services import storyboard_sheet  # noqa: E402

SHEET = """## 场景1 | 黄昏的站台

### 镜头1 | 大远景 | 缓慢推近 | 4s
- 画面：站台全景，列车驶入
- 首帧提示词：Wide shot of a platform at dusk

### 镜头2 | 中景 | 向右横移 | 3s
- 画面：他抬手看表
- 首帧提示词：Medium shot of a man checking his watch

### 镜头3 | 特写 | 固定 | 2s
- 画面：指节敲了两下
- 首帧提示词：Close up of fingers tapping
"""


def _png(width: int, height: int) -> bytes:
    """最小 PNG 头：签名 + IHDR，`read_image_size_from_file` 就够读了。"""
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
    )


def _run(fn) -> None:  # noqa: ANN001
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    real = canvas_runner.SessionLocal

    async def scenario() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        canvas_runner.SessionLocal = maker
        try:
            await fn(maker)
        finally:
            canvas_runner.SessionLocal = real
            await engine.dispose()

    asyncio.run(scenario())


# ================================================================ 1. 纯逻辑


def test_move_words_map_to_the_right_motion():
    """分镜表怎么写，样片就怎么动——这是这个功能有没有意义的底线。"""
    table = {
        "缓慢推近": animatic.PUSH,
        "推": animatic.PUSH,
        "zoom in": animatic.PUSH,
        "拉远": animatic.PULL,
        "后拉": animatic.PULL,
        "向右横移": animatic.PAN_RIGHT,
        "平移": animatic.PAN_RIGHT,
        "跟拍": animatic.PAN_RIGHT,
        "向左移动": animatic.PAN_LEFT,
        "上摇": animatic.TILT_UP,
        "上升": animatic.TILT_UP,
        "下摇": animatic.TILT_DOWN,
        "下降": animatic.TILT_DOWN,
        "升降": animatic.TILT_UP,
        "环绕": animatic.ORBIT,
        "固定": animatic.STATIC,
        "静止不动": animatic.STATIC,
        "static": animatic.STATIC,
    }
    for text, want in table.items():
        got = animatic.move_spec(text).kind
        assert got == want, f"「{text}」判成了 {got}，期望 {want}"


def test_earliest_move_wins_when_several_are_written():
    """一栏里写了两个运镜时，**先说**的那个说了算。

    「横移跟拍」是移动，「推进跟拍」是推近——两者都含「跟」，
    靠一张固定优先级表必然写错一个。
    """
    assert animatic.move_spec("横移跟拍").kind == animatic.PAN_RIGHT
    assert animatic.move_spec("推进跟拍").kind == animatic.PUSH
    assert animatic.move_spec("推近后右移").kind == animatic.PUSH
    assert animatic.move_spec("右移后推近").kind == animatic.PAN_RIGHT


def test_unknown_move_falls_back_and_can_be_told_to_hold_still():
    """认不出来时默认轻微推近；节点设成「固定」时就必须真的不动。"""
    for text in ("", "   ", "莫名其妙", "随便写了点什么"):
        assert animatic.move_spec(text).kind == animatic.PUSH, f"「{text}」没有兜底"
        assert animatic.move_spec(text, default=animatic.STATIC).kind == animatic.STATIC


def test_static_judgement_agrees_with_the_lint():
    """「固定镜头」这件事体检与样片必须同一个答案。

    两处各写一份词表迟早会漂移，然后出现「体检说这不是固定镜头、
    样片却一动不动」——同一份分镜表给出两个说法，用户只会谁都不信。
    """
    words = ("固定", "静止", "不动", "static", "lock", "固定机位", "静止镜头")
    for w in words:
        assert animatic.is_static_move(w) is True, f"「{w}」样片没当成固定"
        assert storyboard_lint._is_static_move(w) is True, f"「{w}」体检没当成固定"
    for w in ("缓慢推近", "横移", "跟拍", "环绕", ""):
        assert animatic.is_static_move(w) is False, f"「{w}」样片误判成固定"
        assert storyboard_lint._is_static_move(w) is False, f"「{w}」体检误判成固定"


def test_only_moves_that_need_room_are_overscanned():
    """推/拉/固定都不该被超采样（白掉一次清晰度），摇移升降才需要余量。"""
    assert animatic.move_spec("推近").overscan == 1.0
    assert animatic.move_spec("拉远").overscan == 1.0
    assert animatic.move_spec("固定").overscan == 1.0
    for text in ("右移", "左移", "上摇", "下摇", "环绕"):
        assert animatic.move_spec(text).overscan == animatic.OVERSCAN, f"「{text}」没有余量"


def test_plan_skips_shots_without_an_image_and_says_why():
    """镜号对不上就跳过并记下来，不按顺序硬凑——凑出来的样片会张冠李戴。"""
    shots = storyboard_sheet.parse_storyboard(SHEET)
    plan = animatic.plan(shots, {"1": object(), "3": object()})
    assert plan.shot_nos == ["1", "3"]
    assert len(plan.skipped) == 1 and plan.skipped[0]["shot"] == "2"
    assert "还没有分镜图" in plan.skipped[0]["reason"]
    assert plan.seconds == 6, f"4s + 2s 应该是 6 秒，实际 {plan.seconds}"


def test_plan_clamps_durations():
    """时长写错不该毁掉整条样片。

    三种写法各有各的兜底，都要钉住：
    - 没写 → 用节点上的兜底时长；
    - 写到 20 秒这种「太长了但不算错」→ 夹到上限（样片没人有耐心看完）；
    - 99 秒这种明显写错 → `Shot.duration_seconds` 本来就返回 0，于是也走兜底。
    """
    shots = [
        storyboard_sheet.Shot(no="1", heading="镜头1 | 中景 | 推 | "),
        storyboard_sheet.Shot(no="2", heading="镜头2 | 中景 | 推 | 20s", duration="20s"),
        storyboard_sheet.Shot(no="3", heading="镜头3 | 中景 | 推 | 0s", duration="0s"),
        storyboard_sheet.Shot(no="4", heading="镜头4 | 中景 | 推 | 99s", duration="99s"),
    ]
    imgs = {s.no: object() for s in shots}
    plan = animatic.plan(shots, imgs, default_seconds=5)
    got = {c.shot_no: c.seconds for c in plan.clips}
    assert got["1"] == 5, "没写时长的应该用兜底值"
    assert got["2"] == animatic.MAX_SHOT_SECONDS, "超长时长没有夹到上限"
    assert got["3"] == 5, "写了 0s 应该退回兜底值而不是 0"
    assert got["4"] == 5, "离谱时长应该退回兜底值（解析器本来就挡掉了它）"


def test_plan_truncates_over_the_total_cap():
    """总时长超上限时截断，并且**如实报告**截掉了哪几镜。"""
    shots = [
        storyboard_sheet.Shot(no=str(i), heading=f"镜头{i} | 中景 | 推 | 12s", duration="12s")
        for i in range(1, 21)
    ]
    imgs = {s.no: object() for s in shots}
    plan = animatic.plan(shots, imgs, max_total_seconds=60)
    assert plan.seconds <= 60
    assert len(plan.clips) == 5 and plan.truncated == [str(i) for i in range(6, 21)]
    report = plan.to_report()
    assert report["shots"] == 5 and len(report["truncated"]) == 15


def test_output_size_never_upscales_a_small_image():
    """小图不能假装成 1080p：放出来的细节是假的，还不如老实出小样片。"""
    assert animatic.output_size(512, 288) == (512, 288)
    assert animatic.output_size(512, 288, long_side=1920) == (512, 288)
    assert animatic.output_size(1024, 1024) == (1024, 1024)
    assert animatic.output_size(1920, 1080, long_side=1280) == (1280, 720)


def test_output_size_fixed_ratios_are_even():
    """固定画幅的边长必须是偶数，否则 yuv420p 编码直接失败。"""
    for ratio, want in (("16:9", (1280, 720)), ("9:16", (720, 1280)), ("1:1", (1280, 1280)), ("4:3", (1280, 960))):
        got = animatic.output_size(1024, 1024, ratio=ratio)
        assert got == want, f"{ratio} 算出了 {got}，期望 {want}"
        assert got[0] % 2 == 0 and got[1] % 2 == 0


def test_filter_is_wellformed_for_every_move():
    """每条运镜都要给出「能跑且会动」的表达式：带 on 的进度、正确的帧数与输出尺寸。"""
    shots = storyboard_sheet.parse_storyboard(SHEET)
    plan = animatic.plan(shots, {s.no: object() for s in shots})
    for clip in plan.clips:
        f = animatic.kenburns_filter(clip, src_size=(1920, 1080), out_size=(1280, 720))
        assert f"d={clip.frames}" in f and "fps=30" in f and "s=1280x720" in f, f
        assert "format=yuv420p" in f, "没有统一像素格式，合并时会失败"
        assert "zoompan=" in f
        for key in ("z='", "x='", "y='"):
            assert key in f, f"缺 {key}"
        if clip.spec.zoom_from != clip.spec.zoom_to:
            assert "on/" in f, "缩放是变化的，表达式里却没有帧进度"
    # 固定镜头不该有超采样，也不该有进度项
    static_clip = next(c for c in plan.clips if c.spec.kind == animatic.STATIC)
    f = animatic.kenburns_filter(static_clip, src_size=(1920, 1080), out_size=(1280, 720))
    assert "scale=1920:1080:" in f, f"固定镜头被超采样了：{f}"
    assert "on/" not in f, f"固定镜头不该有运动：{f}"


def test_clip_command_has_no_audio_and_caps_frames():
    """片段必须无音轨（合并时不会因为声道数不一致失败），且帧数写死。"""
    shots = storyboard_sheet.parse_storyboard(SHEET)
    plan = animatic.plan(shots, {s.no: object() for s in shots})
    clip = plan.clips[0]
    cmd = animatic.clip_command("ffmpeg", Path("a.png"), Path("o.mp4"), clip,
                                src_size=(1920, 1080), out_size=(1280, 720))
    assert "-an" in cmd
    assert cmd[cmd.index("-frames:v") + 1] == str(clip.frames)
    assert "libx264" in cmd


def test_sanitizers_reject_junk_from_the_canvas_doc():
    """节点上的设置来自画布 JSON（可能被手改成任意值），全都要能兜住。"""
    assert animatic.sanitize_ratio("16:9") == "16:9"
    assert animatic.sanitize_ratio("21:9") == animatic.DEFAULT_RATIO
    assert animatic.sanitize_long_side(1920) == 1920
    assert animatic.sanitize_long_side("abc") == animatic.DEFAULT_LONG_SIDE
    assert animatic.sanitize_long_side(4096) == animatic.DEFAULT_LONG_SIDE
    assert animatic.sanitize_seconds(6) == 6
    assert animatic.sanitize_seconds(999) == animatic.MAX_SHOT_SECONDS
    assert animatic.sanitize_seconds(None) == animatic.DEFAULT_SHOT_SECONDS
    assert animatic.sanitize_default_move("static") == animatic.STATIC
    assert animatic.sanitize_default_move("乱写") == animatic.PUSH


def test_summary_records_what_was_skipped():
    """产物说明里必须留下「跳过了哪几镜」——不然事后没人知道样片少了什么。"""
    shots = storyboard_sheet.parse_storyboard(SHEET)
    plan = animatic.plan(shots, {"1": object(), "2": object()})
    text = animatic.summarize(plan)
    assert "2 镜" in text and "跳过" in text and "3" in text
    empty = animatic.plan(shots, {})
    assert "没有可用的镜头" in animatic.summarize(empty)


# ================================================================ 2. 接线


def _doc(nodes: list[dict]) -> dict:
    return {
        "schemaVersion": 1,
        "nodes": nodes,
        "edges": [{"id": "e1", "source": "sb", "target": "simg"}],
        "viewport": {},
    }


def _sb_node() -> dict:
    return {"id": "sb", "type": "storyboard", "data": {"docText": SHEET, "prompt": ""}}


def _simg_node(**data) -> dict:
    return {"id": "simg", "type": "storyboardImage", "data": {"model_key": "m1", **data}}


@contextlib.contextmanager
def _stub_storage(by_name: dict[str, Path], root: Path) -> Iterator[None]:
    """把 `storage.abs_path` 指到临时目录里的文件，免得测试去读真实数据目录。

    没在表里的名字映射到 `root` 下一个**不存在**的路径——「文件被清理掉了」
    这种情况要靠它来造（直接 KeyError 是测试自己写崩，不是被测代码的行为）。
    """
    real = storage.abs_path
    storage.abs_path = lambda name: by_name.get(Path(name).name, root / Path(name).name)  # noqa: ARG005
    try:
        yield
    finally:
        storage.abs_path = real


@contextlib.contextmanager
def _stub_ffmpeg(fake_out: Path, captured: dict) -> Iterator[None]:
    """接管「渲染」这一步：只记录收到什么，产出 fake_out 这个文件。"""
    real_render = ffmpeg_service.render_animatic
    real_save = ffmpeg_service._save_asset_file

    async def fake_render(items, *, out_size):  # noqa: ANN001
        captured["items"] = items
        captured["out_size"] = out_size
        return fake_out

    ffmpeg_service.render_animatic = fake_render
    ffmpeg_service._save_asset_file = lambda p, ext: f"2026-09/{fake_out.name}"  # noqa: ARG005
    try:
        yield
    finally:
        ffmpeg_service.render_animatic = real_render
        ffmpeg_service._save_asset_file = real_save


async def _seed(
    maker,  # noqa: ANN001
    node: dict,
    shots: list[str],
    files: dict[str, Path],
    upstream_text: str = SHEET,
) -> int:
    """建一个「分镜 → 分镜图」的小图，并给指定镜号造出已完成的图任务。

    `asset_batch` 必须写上：`_latest_task_assets` 靠它把「一次运行产出的整批图」
    收齐，没有这个标记时只会取最新那一个任务的产物——真实的出图节点每次都写。
    """
    doc = _doc([_sb_node(), node])
    if upstream_text != SHEET:
        doc["nodes"][0]["data"]["docText"] = upstream_text
    async with maker() as db:
        p = Project(name="样片测试", canvas_json=json.dumps(doc, ensure_ascii=False))
        db.add(p)
        await db.commit()
        await db.refresh(p)
        for no in shots:
            t = Task(
                kind="image", status="completed", service_id=1, model="m1", prompt="",
                params_json=json.dumps(
                    {"shot_no": no, "shot_label": f"镜头{no}", "asset_batch": "b1"}
                ),
                canvas_project_id=p.id, canvas_node_id="simg",
            )
            db.add(t)
            await db.commit()
            await db.refresh(t)
            db.add(Asset(
                kind="image", filename=f"shot{no}.png", original_name=f"shot{no}.png",
                content_type="image/png", size=100, task_id=t.id,
            ))
        await db.commit()
        return p.id


def test_renders_from_the_storyboard_plan_and_registers_a_video_asset():
    """一次成功的出片：镜头参数来自分镜表、产物落成 video 资产、说明写进来路。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        files = {}
        for no, size in (("1", (1920, 1080)), ("2", (1280, 720))):
            p = root / f"shot{no}.png"
            p.write_bytes(_png(*size))
            files[no] = p
        out = root / "sample.mp4"
        out.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(64))

        async def scenario(maker):  # noqa: ANN001
            pid = await _seed(maker, _simg_node(), ["1", "2"], files)
            captured: dict = {}
            with _stub_storage({f"shot{no}.png": p for no, p in files.items()}, root):
                with _stub_ffmpeg(out, captured):
                    asset, report = await canvas_runner.render_animatic(pid, "simg")

            assert asset.kind == "video" and asset.source == "animatic"
            assert asset.duration == 7, f"4s + 3s 应该是 7 秒，实际 {asset.duration}"
            assert (asset.width, asset.height) == (1280, 720), "成片尺寸没按第一镜的图算"
            assert "跳过" in (asset.prompt or "") and "3" in (asset.prompt or "")

            # 交给 ffmpeg 的镜头：顺序按分镜表、运镜按分镜表、时长按分镜表
            items = captured["items"]
            assert [c.shot_no for _p, c, _s in items] == ["1", "2"]
            assert items[0][1].spec.kind == animatic.PUSH
            assert items[1][1].spec.kind == animatic.PAN_RIGHT
            assert [c.seconds for _p, c, _s in items] == [4, 3]
            assert captured["out_size"] == (1280, 720)
            assert report["shots"] == 2 and report["seconds"] == 7

            async with maker() as db:
                rows = (await db.execute(select(Asset).where(Asset.kind == "video"))).scalars().all()
            assert len(rows) == 1, "样片没落成资产"

        _run(scenario)


def test_node_settings_actually_reach_the_plan():
    """节点上的画幅/分辨率/默认运镜/兜底时长都要真的用上。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        p1 = root / "shot1.png"
        p1.write_bytes(_png(1024, 1024))
        out = root / "sample.mp4"
        out.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(64))

        async def scenario(maker):  # noqa: ANN001
            node = _simg_node(sampleRatio="16:9", sampleRes=1920, sampleShotSeconds=6,
                              sampleDefaultMove="static")
            # 分镜表里这一镜没写运镜和时长 → 走节点上的默认值
            sheet = "### 镜头1 | 中景 |  | \n- 画面：他抬手看表\n"
            pid = await _seed(maker, node, ["1"], {"1": p1}, upstream_text=sheet)
            captured: dict = {}
            with _stub_storage({"shot1.png": p1}, root):
                with _stub_ffmpeg(out, captured):
                    _asset, report = await canvas_runner.render_animatic(pid, "simg")
            assert captured["out_size"] == (1920, 1080), f"画幅/分辨率没生效：{captured['out_size']}"
            clip = captured["items"][0][1]
            assert clip.spec.kind == animatic.STATIC, "节点选的「认不出就固定」没生效"
            assert clip.seconds == 6, "兜底时长没生效"
            assert report["size"] == [1920, 1080]

        _run(scenario)


def test_no_image_at_all_is_a_clear_error():
    """一镜都没出图时要说人话，而不是交一条空片子出去。"""
    async def scenario(maker):  # noqa: ANN001
        pid = await _seed(maker, _simg_node(), [], {})
        try:
            await canvas_runner.render_animatic(pid, "simg")
        except canvas_runner.AnimaticInputError as e:
            assert "还没有出过分镜图" in str(e)
            return
        raise AssertionError("没有分镜图却出片成功了")

    _run(scenario)


def test_no_storyboard_upstream_is_a_clear_error():
    """没有上游分镜表就没法知道每镜几秒、怎么运镜——必须说清楚缺什么。"""
    async def scenario(maker):  # noqa: ANN001
        doc = _doc([_simg_node()])
        doc["edges"] = []  # 断开上游
        async with maker() as db:
            p = Project(name="样片测试", canvas_json=json.dumps(doc, ensure_ascii=False))
            db.add(p)
            await db.commit()
            await db.refresh(p)
        try:
            await canvas_runner.render_animatic(p.id, "simg")
        except canvas_runner.AnimaticInputError as e:
            assert "分镜表" in str(e) and "分镜" in str(e)
            return
        raise AssertionError("没有分镜表却出片成功了")

    _run(scenario)


def test_other_node_types_cannot_render():
    """只有分镜图节点能出样片：别的节点没有「一镜一张图」这个结构。"""
    async def scenario(maker):  # noqa: ANN001
        doc = _doc([{"id": "sb", "type": "storyboard", "data": {"docText": SHEET}}])
        async with maker() as db:
            p = Project(name="样片测试", canvas_json=json.dumps(doc, ensure_ascii=False))
            db.add(p)
            await db.commit()
            await db.refresh(p)
        try:
            await canvas_runner.render_animatic(p.id, "sb")
        except canvas_runner.AnimaticInputError as e:
            assert "分镜图" in str(e)
            return
        raise AssertionError("分镜节点竟然出了样片")

    _run(scenario)


def test_missing_canvas_is_a_plain_valueerror():
    """画布不存在要走 404 那条路，不能和「输入不对」混成一个状态码。"""
    async def scenario(maker):  # noqa: ANN001
        try:
            await canvas_runner.render_animatic(999999, "simg")
        except canvas_runner.AnimaticInputError:
            raise AssertionError("不存在的画布被当成了输入错误（应该回 404）")
        except ValueError as e:
            assert "画布不存在" in str(e)
            return
        raise AssertionError("不存在的画布竟然出片成功了")

    _run(scenario)


def test_a_missing_image_file_is_reported_not_crashed():
    """库里有一行 Asset 不代表文件还在：缺文件的镜头按「跳过」报，其余照常渲染。

    这条是从真实环境里踩出来的——数据目录被清理过后，`Asset` 行还在、文件没了，
    交给 ffmpeg 只会得到一句「Invalid data found」，用户完全不知道发生了什么。
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        p1 = root / "shot1.png"
        p1.write_bytes(_png(1280, 720))
        # shot2.png 故意不建：模拟文件被清理掉
        out = root / "sample.mp4"
        out.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(64))

        async def scenario(maker):  # noqa: ANN001
            pid = await _seed(maker, _simg_node(), ["1", "2"], {"1": p1})
            captured: dict = {}
            with _stub_storage({"shot1.png": p1}, root):
                with _stub_ffmpeg(out, captured):
                    asset, report = await canvas_runner.render_animatic(pid, "simg")

            assert [c.shot_no for _p, c, _s in captured["items"]] == ["1"], "缺文件的镜头不该交给 ffmpeg"
            assert asset.duration == 4, f"时长只该算真正进片的那一镜，实际 {asset.duration}"
            reasons = {s["shot"]: s["reason"] for s in report["skipped"]}
            assert reasons.get("2") == "图片文件不在了", report["skipped"]
            assert "文件不在了" in (asset.prompt or ""), "产物说明里没留下「为什么少了一镜」"

        _run(scenario)


def test_all_images_missing_is_a_clear_error():
    """所有图都读不到时要说人话，而不是丢一句 ffmpeg 的报错。"""
    async def scenario(maker):  # noqa: ANN001
        pid = await _seed(maker, _simg_node(), ["1"], {})
        with _stub_storage({}, Path("c:/definitely/not/here")):
            try:
                await canvas_runner.render_animatic(pid, "simg")
            except canvas_runner.AnimaticInputError as e:
                assert "读不到" in str(e) and "分镜图" in str(e)
                return
        raise AssertionError("图全都不在了却出片成功了")

    _run(scenario)


def test_rendering_does_not_create_tasks():
    """出样片不建任务、不调模型：它是本地渲染，不该在任务中心留痕。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        p1 = root / "shot1.png"
        p1.write_bytes(_png(1280, 720))
        out = root / "sample.mp4"
        out.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(64))

        async def scenario(maker):  # noqa: ANN001
            pid = await _seed(maker, _simg_node(), ["1"], {"1": p1})
            async with maker() as db:
                before = len((await db.execute(select(Task))).scalars().all())
            captured: dict = {}
            with _stub_storage({"shot1.png": p1}, root):
                with _stub_ffmpeg(out, captured):
                    await canvas_runner.render_animatic(pid, "simg")
            async with maker() as db:
                after = len((await db.execute(select(Task))).scalars().all())
            assert after == before, "出样片建了任务（应当零模型调用、零任务）"

        _run(scenario)


# ================================================================ 3. 真跑 ffmpeg


def _ffmpeg_or_skip() -> str | None:
    ok, _version = asyncio.run(ffmpeg_service.ffmpeg_available())
    if not ok:
        print("    跳过：本机没有可用的 ffmpeg", file=sys.stderr)
        return None
    binary, _ = asyncio.run(ffmpeg_service._resolve_binaries())
    return binary


def _testsrc(ffmpeg: str, out: Path, size: str) -> None:
    """用 testsrc 当源图，不是纯色块。

    纯色图测不出缓动：把一块纯色放大 1.25 倍，像素值一个都不变，
    「没动」和「动了」看起来一模一样（我第一版就是这么被骗过去的）。
    testsrc 满屏高频细节，任何缩放/平移都立刻体现在像素差异上。
    """
    r = subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=1",
         "-frames:v", "1", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr[-400:]


def _gray(ffmpeg: str, src: Path, out: Path, *, vf: str = "") -> bytes:
    """把一张图统一缩到 160x90 的灰度 BMP，用来逐像素比。

    比之前先降采样到小图：一来快，二来能盖掉编码噪声（我们要看的是「画面有没有
    整体位移/缩放」，不是几个像素的压缩抖动）。窗口大小选 160x90 而不是更小，
    是为了让画面里的测试图案还留得住细节——压得太狠会把运动也平均掉。
    """
    chain = f"{vf}," if vf else ""
    cmd = [
        ffmpeg, "-v", "error", "-y", "-i", str(src),
        "-frames:v", "1", "-vf", f"{chain}scale=160:90,format=gray",
        "-f", "image2", "-c:v", "bmp", str(out),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr[-500:]
    blob = out.read_bytes()
    return blob[struct.unpack_from("<I", blob, 10)[0]:]


def _frame_at(ffmpeg: str, video: Path, t: float, out: Path) -> bytes:
    r = subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr[-400:]
    return _gray(ffmpeg, out, out.with_name("g.bmp"))


def _ratio(a: bytes, b: bytes) -> float:
    assert len(a) == len(b) and a, f"帧长度异常：{len(a)} vs {len(b)}"
    return sum(1 for x, y in zip(a, b) if abs(x - y) > 12) / len(a)


def test_real_render_moves_the_right_way():
    """真渲染一遍：推近要往**里**推、右摇要往**右**走、固定要一动不动。

    只验「画面变了没有」不够——推和拉、左摇和右摇都会让画面变，
    方向反了照样「动了」。所以这里把 spec 手算出来的取景框裁一份当标准答案，
    与成片实际帧逐像素比。
    """
    ffmpeg = _ffmpeg_or_skip()
    if not ffmpeg:
        return

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "src.png"
        _testsrc(ffmpeg, src, "1920x1080")

        shots = storyboard_sheet.parse_storyboard(SHEET)
        plan = animatic.plan(shots, {s.no: object() for s in shots})
        assert [c.spec.kind for c in plan.clips] == [animatic.PUSH, animatic.PAN_RIGHT, animatic.STATIC]

        items = [(src, clip, (1920, 1080)) for clip in plan.clips]
        out_size = (1280, 720)
        video = asyncio.run(ffmpeg_service.render_animatic(items, out_size=out_size))
        try:
            _assert_output(video, ffmpeg, src, root, out_size)
        finally:
            # 真跑一遍会往 storage 里落一个 mp4（不是资产，只是文件）：用例自己清掉，
            # 别让每次跑测试都在用户的数据目录里留垃圾。
            Path(video).unlink(missing_ok=True)


def _assert_output(video: Path, ffmpeg: str, src: Path, root: Path, out_size: tuple[int, int]) -> None:
    meta = asyncio.run(ffmpeg_service.probe(video))
    assert meta["duration"] == 9.0, meta
    assert (meta["width"], meta["height"]) == out_size, meta
    assert meta["codec"] == "h264", meta

    # 镜头1 推近（0–4s）：末帧应等于原图中心 1536x864 放大到 1280x720
    got = _frame_at(ffmpeg, video, 3.95, root / "a.png")
    want = _gray(ffmpeg, src, root / "b.bmp", vf="crop=1536:864:192:108")
    d = _ratio(got, want)
    assert d < 0.06, f"推近的末帧与「中心 80% 放大」差 {d * 100:.1f}%，方向或幅度不对"

    # 镜头2 右摇（4–7s）：末帧应等于工作画布（1600x900）最右侧那块
    got = _frame_at(ffmpeg, video, 6.95, root / "c.png")
    want = _gray(ffmpeg, src, root / "d.bmp", vf="scale=1600:900,crop=1280:720:320:90")
    d = _ratio(got, want)
    assert d < 0.06, f"右摇的末帧与「最右块」差 {d * 100:.1f}%，方向不对"

    # 镜头2 首帧应等于最左那块（确认起点，也确认它是真的在扫，不是跳变）
    got = _frame_at(ffmpeg, video, 4.0, root / "e.png")
    want = _gray(ffmpeg, src, root / "f.bmp", vf="scale=1600:900,crop=1280:720:0:90")
    d = _ratio(got, want)
    assert d < 0.10, f"右摇的起点与「最左块」差 {d * 100:.1f}%"

    # 镜头3 固定（7–9s）：两帧必须**完全一样**（「固定」这个承诺不能打折）
    a = _frame_at(ffmpeg, video, 7.1, root / "g.png")
    b = _frame_at(ffmpeg, video, 8.9, root / "h.png")
    assert _ratio(a, b) == 0.0, "固定镜头竟然动了"

    # 收尾：临时片段目录必须清干净（不然每出一版样片都留一堆垃圾）
    work_root = ffmpeg_service.settings_storage_dir() / "tmp"
    leftovers = list(work_root.glob("animatic-*")) if work_root.exists() else []
    assert not leftovers, f"临时片段目录没删干净：{leftovers}"


def test_real_render_of_a_single_shot_still_works():
    """只有一镜的样片也要能出：合并那一步对单个片段同样成立。"""
    ffmpeg = _ffmpeg_or_skip()
    if not ffmpeg:
        return

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "src.png"
        _testsrc(ffmpeg, src, "1280x720")
        clip = animatic.plan(
            storyboard_sheet.parse_storyboard("### 镜头1 | 中景 | 固定 | 2s\n- 画面：甲\n"),
            {"1": object()},
        ).clips[0]
        video = asyncio.run(
            ffmpeg_service.render_animatic([(src, clip, (1280, 720))], out_size=(1280, 720))
        )
        try:
            meta = asyncio.run(ffmpeg_service.probe(video))
            assert meta["duration"] == 2.0, meta
            assert (meta["width"], meta["height"]) == (1280, 720), meta
        finally:
            Path(video).unlink(missing_ok=True)


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

