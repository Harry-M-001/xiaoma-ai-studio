"""「按当时的参数重新生成」的回归测试（直接 python 运行）。

运行：venv/Scripts/python tests/test_task_retry.py

这个功能的价值全在「快照」二字：型号、提示词、params_json 与原任务一字不差，
只把用户明确改过的字段盖上去。所以测试盯的就是「有没有悄悄丢东西」。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import Base, Task  # noqa: E402
from app.routers import generation  # noqa: E402
from app.schemas import TaskRetryIn  # noqa: E402
from app.services.runner import runner as task_runner  # noqa: E402


async def _with_db(fn):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        await fn(db)
    await engine.dispose()


class _Spy:
    """拦下执行通道：这里只关心「分派对了没有」，不要真去跑任务。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def install(self) -> "_Spy":
        self._saved = {}
        for name in ("start_image", "start_video", "start_comfy", "start_text"):
            self._saved[name] = getattr(task_runner, name)
            setattr(task_runner, name, self._make(name))
        return self

    def _make(self, name: str):
        def _fn(task_id: int) -> None:
            self.calls.append((name, task_id))

        return _fn

    def restore(self) -> None:
        for name, fn in self._saved.items():
            setattr(task_runner, name, fn)


async def _make_task(
    db,
    *,
    kind: str = "image",
    params: dict | None = None,
    canvas: tuple[int, str] | None = (7, "n1"),
) -> Task:
    task = Task(
        kind=kind,
        status="failed",
        service_id=3,
        model="3:cv-img",
        prompt="原提示词",
        params_json=json.dumps(params or {"size": "1024x1024", "n": 1, "ref_asset_ids": [11, 12]}),
        canvas_project_id=canvas[0] if canvas else None,
        canvas_node_id=canvas[1] if canvas else None,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


def _run(coro_fn):
    spy = _Spy().install()
    try:
        asyncio.run(coro_fn(spy))
    finally:
        spy.restore()


def test_no_overrides_keeps_everything():
    async def scenario(spy: _Spy):
        async with _ctx() as db:
            src = await _make_task(db)
            out = await generation.retry_task(src.id, None, db)
            assert out.prompt == "原提示词"
            # 参数一个都不能丢：参考图、尺寸、张数全在
            assert out.params == {"size": "1024x1024", "n": 1, "ref_asset_ids": [11, 12]}
            assert out.model == "3:cv-img"
            assert out.service_id == 3
            # 画布归属必须带过去，否则产物回不到那个节点上
            assert out.retry_of_task_id == src.id
            assert spy.calls == [("start_image", out.id)]

    _run(scenario)


def test_overrides_only_touch_named_fields():
    async def scenario(spy: _Spy):
        async with _ctx() as db:
            src = await _make_task(db)
            out = await generation.retry_task(
                src.id, TaskRetryIn(prompt="新提示词", n=3), db
            )
            assert out.prompt == "新提示词"
            assert out.params["n"] == 3
            # 没点的字段照旧，尤其是参考图
            assert out.params["size"] == "1024x1024"
            assert out.params["ref_asset_ids"] == [11, 12]

    _run(scenario)


def test_blank_prompt_does_not_wipe_the_original():
    """弹窗里把提示词清空不该变成「用空提示词重跑」。"""
    async def scenario(spy: _Spy):
        async with _ctx() as db:
            src = await _make_task(db)
            out = await generation.retry_task(src.id, TaskRetryIn(prompt="   "), db)
            assert out.prompt == "原提示词"

    _run(scenario)


def test_canvas_linkage_survives():
    async def scenario(spy: _Spy):
        async with _ctx() as db:
            src = await _make_task(db, canvas=(42, "node_x"))
            new = await db.get(Task, (await generation.retry_task(src.id, None, db)).id)
            assert new.canvas_project_id == 42
            assert new.canvas_node_id == "node_x"

    _run(scenario)


def test_original_task_is_untouched():
    """原记录要留着作对照，不能被改。"""
    async def scenario(spy: _Spy):
        async with _ctx() as db:
            src = await _make_task(db)
            before = (src.prompt, src.params_json, src.status)
            await generation.retry_task(src.id, TaskRetryIn(prompt="改了", n=4), db)
            fresh = await db.get(Task, src.id)
            assert (fresh.prompt, fresh.params_json, fresh.status) == before
            assert fresh.retry_of_task_id is None

    _run(scenario)


def test_video_kind_dispatches_to_video_channel():
    async def scenario(spy: _Spy):
        async with _ctx() as db:
            src = await _make_task(
                db,
                kind="video",
                params={"mode": "first_last", "duration": 5, "ratio": "16:9", "resolution": "720p"},
            )
            out = await generation.retry_task(
                src.id, TaskRetryIn(duration=10, ratio="9:16"), db
            )
            assert out.params["duration"] == 10
            assert out.params["ratio"] == "9:16"
            assert out.params["resolution"] == "720p"
            assert spy.calls == [("start_video", out.id)]

    _run(scenario)


def test_workflow_kind_dispatches_to_comfy_channel():
    """回归背景：早先重跑写死了图片通道，ComfyUI 任务重跑会被当成图片跑。"""
    async def scenario(spy: _Spy):
        async with _ctx() as db:
            src = await _make_task(db, kind="workflow", params={"workflow_id": 5})
            out = await generation.retry_task(src.id, None, db)
            assert out.params["workflow_id"] == 5
            assert spy.calls == [("start_comfy", out.id)]

    _run(scenario)


def test_text_kind_dispatches_to_text_channel():
    async def scenario(spy: _Spy):
        async with _ctx() as db:
            src = await _make_task(db, kind="text", params={"agent_key": "novel"})
            out = await generation.retry_task(src.id, None, db)
            assert out.params["agent_key"] == "novel"
            assert spy.calls == [("start_text", out.id)]

    _run(scenario)


def test_shot_task_keeps_its_shot_number():
    """逐镜任务重跑不能丢掉镜号，否则产物标签和首帧关联就错位了。"""
    async def scenario(spy: _Spy):
        async with _ctx() as db:
            src = await _make_task(
                db,
                kind="video",
                params={
                    "mode": "first_last",
                    "shot_no": "3",
                    "shot_label": "镜头3（近景）",
                    "first_frame_asset_id": 88,
                },
            )
            out = await generation.retry_task(src.id, None, db)
            assert out.params["shot_no"] == "3"
            assert out.params["first_frame_asset_id"] == 88

    _run(scenario)


def test_running_task_cannot_be_retried():
    from fastapi import HTTPException

    async def scenario(spy: _Spy):
        async with _ctx() as db:
            src = await _make_task(db)
            src.status = "processing"
            await db.commit()
            try:
                await generation.retry_task(src.id, None, db)
            except HTTPException as exc:
                assert exc.status_code == 400
                return
            raise AssertionError("进行中的任务不该允许重跑")

    _run(scenario)


class _ctx:
    """每次用的独立内存库。"""

    async def __aenter__(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.maker()
        return await self.db.__aenter__()

    async def __aexit__(self, *exc: object) -> bool:
        await self.db.__aexit__(*exc)
        await self.engine.dispose()
        return False


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
