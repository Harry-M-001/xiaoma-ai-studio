"""D 期「逐镜出视频」的回归测试（直接 python 运行）。

运行：venv/Scripts/python tests/test_shot_video.py

覆盖两块：
1. 分镜表解析新增的场景分组、视频提示词、逐镜时长；
2. 视频节点按镜号配对分镜图，按模式生成首帧/尾帧/参考图。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import Asset, Base, Task  # noqa: E402
from app.services import canvas_runner, provider_store, storyboard_sheet  # noqa: E402

# 「有镜头没分镜图」是预期路径，测试里不该刷一屏警告
logging.getLogger("app.services.canvas_runner").setLevel(logging.CRITICAL)

SHEET = """## 场景1 | 黄昏的花园

### 镜头1 | 全景 | 缓慢推近 | 4s
- 画面：暮光闪闪站在花园中央，缓缓抬头
- 情绪外化：手指无意识绞紧衣角
- 首帧提示词：Twilight standing in a crystal garden
- 约束：无字幕、无 Logo

### 镜头2 | 中景 | 固定 | 3s
- 画面：云宝从树后探出头
- 首帧提示词：Rainbow Dash peeking from behind a tree

## 场景2 | 白天的城堡

### 镜头3 | 近景 | 手持 | 2.5s
- 画面：两马对视
- 首帧提示词：Two ponies facing each other
"""


# ---------- 解析 ----------


def test_scene_grouping():
    shots = storyboard_sheet.parse_storyboard(SHEET)
    assert [s.no for s in shots] == ["1", "2", "3"]
    assert [s.scene_no for s in shots] == ["1", "1", "2"]
    assert shots[0].scene_title == "黄昏的花园"
    assert shots[2].scene_title == "白天的城堡"


def test_scene_titles_are_not_shots():
    """场景标题只能切换分组，不能被当成一镜。"""
    shots = storyboard_sheet.parse_storyboard(SHEET)
    assert all("场景" not in s.heading for s in shots)


def test_scene_without_number_still_groups():
    """有「场景」字样但没编号时要退化成按标题文本分组，不能整组丢掉。"""
    sheet = (
        "## 场景：黄昏的花园\n\n### 镜头1\n- 画面：甲\n\n### 镜头2\n- 画面：乙\n"
    )
    shots = storyboard_sheet.parse_storyboard(sheet)
    assert [s.scene_no for s in shots] == ["场景：黄昏的花园"] * 2


def test_no_scene_headings_leaves_scene_empty():
    """没写场景标题时不能瞎编：整表就是一组，scene_no 留空。"""
    sheet = "### 镜头1 | 全景 | 推 | 4s\n- 画面：甲\n"
    shots = storyboard_sheet.parse_storyboard(sheet)
    assert shots[0].scene_no == ""
    assert shots[0].scene_title == ""


def test_video_prompt_composes_action_and_camera():
    shots = storyboard_sheet.parse_storyboard(SHEET)
    prompt = shots[0].video_prompt
    # 生视频要的是「动作」，画面描述里正好有；运镜与情绪也要带上
    assert "暮光闪闪站在花园中央" in prompt
    assert "缓慢推近" in prompt
    assert "绞紧衣角" in prompt
    assert "无字幕" in prompt


def test_video_prompt_prefers_explicit_field():
    sheet = (
        "### 镜头1 | 全景 | 推 | 4s\n- 画面：甲\n- 视频提示词：一只猫跳上窗台，镜头跟摇\n"
    )
    shots = storyboard_sheet.parse_storyboard(sheet)
    assert shots[0].video_prompt == "一只猫跳上窗台，镜头跟摇"


def test_duration_seconds():
    assert storyboard_sheet.parse_storyboard(SHEET)[0].duration_seconds == 4
    assert storyboard_sheet.parse_storyboard(SHEET)[2].duration_seconds == 2  # 2.5s 四舍五入


def test_duration_out_of_range_is_ignored():
    sheet = "### 镜头1 | 全景 | 推 | 120s\n- 画面：甲\n"
    assert storyboard_sheet.parse_storyboard(sheet)[0].duration_seconds == 0


def test_non_shot_blocks_are_skipped_but_scenes_kept():
    """顺序遍历之后仍要跳过前言之类的非镜头块（回归：改成顺序遍历时容易漏）。"""
    sheet = (
        "# 分镜表\n\n"
        "这是一段说明。\n\n"
        "## 场景1 | 花园\n\n"
        "### 镜头1 | 全景 | 推 | 4s\n- 画面：甲\n\n"
        "### 小结\n- 画面：这是小结算不算镜头？\n"
    )
    shots = storyboard_sheet.parse_storyboard(sheet)
    assert [s.no for s in shots] == ["1"]


def test_fallback_when_no_shot_titles():
    """一个「镜头」标题都没有时，退回「每个标题块都是一镜」。"""
    sheet = "## 开头\n- 画面：甲\n\n## 结尾\n- 画面：乙\n"
    shots = storyboard_sheet.parse_storyboard(sheet)
    assert len(shots) == 2


# ---------- 逐镜出片 ----------


async def _with_db(fn):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        await fn(db)
    await engine.dispose()


class _FakeResolved:
    class _Svc:
        id = 1

    service = _Svc()


async def _make_shot_image(db, project_id: int, node_id: str, shot_no: str) -> Asset:
    """造一张「分镜图节点产出的图」：任务参数里带镜号。"""
    task = Task(
        kind="image",
        status="completed",
        service_id=1,
        model="img",
        prompt="p",
        params_json=json.dumps({"shot_no": shot_no, "shot_label": f"镜头{shot_no}"}),
        canvas_project_id=project_id,
        canvas_node_id=node_id,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    asset = Asset(
        kind="image",
        filename=f"shot{shot_no}.png",
        original_name=f"shot{shot_no}.png",
        content_type="image/png",
        size=1,
        task_id=task.id,
        name=f"镜头{shot_no}",
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return asset


def _video_node(**data):
    base = {"model_key": "1:vid", "mode": "first_last", "shotVideo": "each", "duration": 5}
    base.update(data)
    return {"id": "v1", "type": "video", "data": base}


def _run(coro_fn):
    original = provider_store.resolve_model

    async def fake_resolve(db, model_key, modality):  # noqa: ANN001
        return _FakeResolved()

    provider_store.resolve_model = fake_resolve
    try:
        asyncio.run(coro_fn())
    finally:
        provider_store.resolve_model = original


def test_each_shot_gets_its_own_first_frame():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            images = [await _make_shot_image(db, 1, "s1", n) for n in ("1", "2", "3")]
            tasks = await canvas_runner._create_shot_video_tasks(
                db, 1, _video_node(), SHEET, images
            )
            assert len(tasks) == 3
            params = [json.loads(t.params_json) for t in tasks]
            assert [p["shot_no"] for p in params] == ["1", "2", "3"]
            # 每一镜用自己那张图当首帧，不能错位
            for p, img in zip(params, images):
                assert p["first_frame_asset_id"] == img.id
            # each 模式不做首尾相连
            assert all("last_frame_asset_id" not in p for p in params)
            # 逐镜时长来自分镜表
            assert [p["duration"] for p in params] == [4, 3, 2]
            assert params[0]["scene_no"] == "1"
            assert params[2]["scene_no"] == "2"
        await engine.dispose()

    _run(scenario)


def test_chain_links_each_shot_to_the_next():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            images = [await _make_shot_image(db, 1, "s1", n) for n in ("1", "2", "3")]
            tasks = await canvas_runner._create_shot_video_tasks(
                db, 1, _video_node(shotVideo="chain"), SHEET, images
            )
            params = [json.loads(t.params_json) for t in tasks]
            # 第 N 镜的尾帧 = 第 N+1 镜的首帧，接缝处同图才能拼得连贯
            assert params[0]["last_frame_asset_id"] == images[1].id
            assert params[1]["last_frame_asset_id"] == images[2].id
            # 末镜没有下一镜，不该编一个尾帧出来
            assert "last_frame_asset_id" not in params[2]
        await engine.dispose()

    _run(scenario)


def test_omni_ref_chains_same_scene_only():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            images = [await _make_shot_image(db, 1, "s1", n) for n in ("1", "2", "3")]
            tasks = await canvas_runner._create_shot_video_tasks(
                db,
                1,
                _video_node(mode="omni_ref", shotVideo="each"),
                SHEET,
                images,
            )
            params = [json.loads(t.params_json) for t in tasks]
            # 镜 2 与镜 1 同场景 → 带上镜 1
            assert params[1]["ref_asset_ids"] == [images[1].id, images[0].id]
            # 镜 3 换了场景 → 不能带上镜 2（跨场景串联是错的）
            assert params[2]["ref_asset_ids"] == [images[2].id]
        await engine.dispose()

    _run(scenario)


def test_scene_refs_can_be_turned_off():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            images = [await _make_shot_image(db, 1, "s1", n) for n in ("1", "2")]
            tasks = await canvas_runner._create_shot_video_tasks(
                db,
                1,
                _video_node(mode="omni_ref", sceneRefs=False),
                SHEET,
                images,
            )
            params = [json.loads(t.params_json) for t in tasks]
            assert params[1]["ref_asset_ids"] == [images[1].id]
        await engine.dispose()

    _run(scenario)


def test_missing_shot_images_are_skipped_not_fatal():
    """出一部分总比一镜都不出好；缺的那些交给日志去说。"""

    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            # 只有镜 1 和镜 3 有图
            images = [
                await _make_shot_image(db, 1, "s1", "1"),
                await _make_shot_image(db, 1, "s1", "3"),
            ]
            tasks = await canvas_runner._create_shot_video_tasks(
                db, 1, _video_node(), SHEET, images
            )
            assert [json.loads(t.params_json)["shot_no"] for t in tasks] == ["1", "3"]
        await engine.dispose()

    _run(scenario)


def test_all_missing_raises_with_actionable_message():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            images = [await _make_shot_image(db, 1, "s1", "9")]
            try:
                await canvas_runner._create_shot_video_tasks(
                    db, 1, _video_node(), SHEET, images
                )
            except ValueError as exc:
                text = str(exc)
                assert "对不上" in text
                assert "生成镜数" in text
                return
            raise AssertionError("镜号完全对不上时应该报错")

    _run(scenario)


def test_no_shot_images_at_all_raises():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            try:
                await canvas_runner._create_shot_video_tasks(db, 1, _video_node(), SHEET, [])
            except ValueError as exc:
                assert "分镜图" in str(exc)
                return
            raise AssertionError("没有带镜号的图时应该报错")

    _run(scenario)


def test_text2video_is_rejected_in_shot_mode():
    async def scenario():
        try:
            await canvas_runner._create_shot_video_tasks(
                None, 1, _video_node(mode="text2video"), SHEET, []
            )
        except ValueError as exc:
            assert "首尾帧" in str(exc)
            return
        raise AssertionError("文生视频模式下逐镜出片应该报错")

    _run(scenario)


def test_no_storyboard_raises():
    async def scenario():
        try:
            await canvas_runner._create_shot_video_tasks(None, 1, _video_node(), "没有镜头表", [])
        except ValueError as exc:
            assert "分镜表" in str(exc)
            return
        raise AssertionError("上游没有分镜表时应该报错")

    _run(scenario)


def test_assets_by_shot_pairs_by_number_not_order():
    """按镜号配对：上游被截断或乱序都不能张冠李戴。"""

    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            a3 = await _make_shot_image(db, 1, "s1", "3")
            a1 = await _make_shot_image(db, 1, "s1", "1")
            # 故意乱序传入
            by_shot = await canvas_runner._assets_by_shot(db, [a3, a1])
            assert set(by_shot) == {"1", "3"}
            assert by_shot["1"].id == a1.id
            assert by_shot["3"].id == a3.id
        await engine.dispose()

    _run(scenario)


def test_duplicate_shot_numbers_are_deduped():
    """回归背景：上游正文若是多份分镜表拼起来的（重跑过上游节点、或者用户手工粘了两份），
    不去重就会按镜号重复出片 —— 3 镜变 9 段，费用三倍，而且从任务数量上看不出异常。

    这个是端到端跑的时候撞出来的：当时上游文档被拼了 3 遍。
    """
    shots = storyboard_sheet.parse_storyboard(SHEET * 3)
    assert len(shots) == 9  # 解析层保持忠实
    unique = canvas_runner._dedupe_shots(shots, "测试节点")
    assert [s.no for s in unique] == ["1", "2", "3"]
    # 保留的是首次出现的那份（场景号来自第一份分镜表）
    assert [s.scene_no for s in unique] == ["1", "1", "2"]


def test_dedupe_keeps_distinct_shot_numbers():
    sheet = "### 镜头1A | 全景 | 推 | 4s\n- 画面：甲\n\n### 镜头1 | 中景 | 推 | 4s\n- 画面：乙\n"
    shots = storyboard_sheet.parse_storyboard(sheet)
    assert len(canvas_runner._dedupe_shots(shots, "测试节点")) == 2


def test_prev_shot_image_scopes_to_scene():
    shots = storyboard_sheet.parse_storyboard(SHEET)

    class _A:
        def __init__(self, i: int) -> None:
            self.id = i

    by_shot = {"1": _A(1), "2": _A(2), "3": _A(3)}
    # 镜 3 是新场景的第一镜，往前找只会撞到场景边界 → 不串联
    assert canvas_runner._prev_shot_image(shots, 2, by_shot) is None
    assert canvas_runner._prev_shot_image(shots, 1, by_shot).id == 1
    assert canvas_runner._prev_shot_image(shots, 0, by_shot) is None


def test_prev_shot_image_skips_shots_without_images():
    """中间那镜没出图时顺延到更前一镜，而不是直接放弃串联。"""
    shots = storyboard_sheet.parse_storyboard(SHEET)

    class _A:
        def __init__(self, i: int) -> None:
            self.id = i

    # 镜 2 没图，镜 3 也在同场景（换个表来构造）
    sheet = "## 场景1 | 花园\n\n### 镜头1 | 全景 | 推 | 4s\n- 画面：甲\n\n### 镜头2 | 中景 | 推 | 4s\n- 画面：乙\n\n### 镜头3 | 近景 | 推 | 4s\n- 画面：丙\n"
    shots_same = storyboard_sheet.parse_storyboard(sheet)
    by_shot = {"1": _A(1), "3": _A(3)}
    assert canvas_runner._prev_shot_image(shots_same, 2, by_shot).id == 1
    assert shots_same[0].scene_no == "1"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failed else 0)
