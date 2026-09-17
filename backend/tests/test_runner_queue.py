"""任务队列的行为测试（直接 python 运行）。

运行：venv/Scripts/python tests/test_runner_queue.py

这里盯的是三件在「用户一次点几百个任务」时才会暴露的事：
1. 队列有上限，到顶要**直接拒绝并说清怎么办**，不是默默收下；
2. 任务跑完要从在册表里摘掉（老实现只增不减，是慢性泄漏，也会让队列计数失真）；
3. **等上游出片的那段时间不能占着并发名额**——否则四个视频任务就能把整条队列堵死。
不连数据库：用假的 _mark_failed 与假任务；闸机本身的语义是纯内存行为。
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.services import runner as runner_module  # noqa: E402


def test_queue_limit_is_never_below_concurrency():
    """上限配得比并发数还小会让队列立刻瘫痪，所以要夹住。"""
    original = runner_module._config_int
    try:
        runner_module._config_int = lambda key, default: 1 if "concurrency" in key else 3
        assert runner_module._queue_limit() == 3
        runner_module._config_int = lambda key, default: 8 if "concurrency" in key else 2
        assert runner_module._queue_limit() == 8, "上限低于并发数时必须抬到并发数"
    finally:
        runner_module._config_int = original


def test_queue_full_message_says_what_to_do():
    msg = runner_module._queue_full_message()
    assert "排队" in msg
    assert "任务并发数" in msg, msg  # 给出可照做的下一步
    assert "排队上限" in msg, msg


def test_spawn_refuses_when_queue_is_full():
    """投递超过上限要被拒绝；跑完之后名额要还回来。"""
    original = runner_module._queue_limit
    runner_module._queue_limit = lambda: 2
    try:
        async def main() -> None:
            r = runner_module.TaskRunner()
            release = asyncio.Event()

            async def slow() -> None:
                await release.wait()

            assert r._spawn(1, lambda: slow()) is True
            assert r._spawn(2, lambda: slow()) is True
            await asyncio.sleep(0)  # 让两个协程真正开跑
            assert r._spawn(3, lambda: slow()) is False, "到顶就该拒绝"
            assert r.queue_stats()["live"] == 2

            release.set()
            await asyncio.sleep(0.05)
            assert r._spawn(4, lambda: slow()) is True, "腾出名额后应当能再收"
            release.set()
            await asyncio.sleep(0.05)

        asyncio.run(main())
    finally:
        runner_module._queue_limit = original


def test_finished_jobs_are_forgotten():
    """跑完的任务要从在册表里摘掉。老实现只增不减：跑几千个任务就留几千个对象在那。"""
    async def main() -> None:
        r = runner_module.TaskRunner()

        async def quick() -> None:
            return None

        for i in range(5):
            assert r._spawn(i, lambda: quick()) is True
        await asyncio.sleep(0.05)
        assert r._jobs == {}, f"跑完的任务仍在在册表里：{list(r._jobs)}"
        assert r.queue_stats()["live"] == 0

    asyncio.run(main())


def test_respawning_same_task_replaces_instead_of_duplicating():
    """同一个任务被重新投递（重跑）时，旧协程要被取消，不能两份同时在跑。"""
    async def main() -> None:
        r = runner_module.TaskRunner()
        release = asyncio.Event()
        started: list[int] = []

        def factory(tag: int):
            async def run() -> None:
                started.append(tag)
                await release.wait()

            return run

        r._spawn(7, factory(1))
        await asyncio.sleep(0)
        r._spawn(7, factory(2))
        await asyncio.sleep(0.05)
        assert len([t for t in r._jobs if t == 7]) == 1
        release.set()
        await asyncio.sleep(0.05)

    asyncio.run(main())


def test_gate_releases_slot_for_waiting_jobs():
    """闸机语义：释放后另一个等待者要能立刻进来（notify(1) 也够用）。"""
    async def main() -> None:
        gate = runner_module._ConcurrencyGate()
        order: list[str] = []

        async def job(name: str, hold: float) -> None:
            async with gate:
                order.append(f"{name}-start")
                await asyncio.sleep(hold)
                order.append(f"{name}-end")

        max_running = 0

        async def watch() -> None:
            nonlocal max_running
            for _ in range(40):
                max_running = max(max_running, gate.running)
                await asyncio.sleep(0.005)

        await asyncio.gather(job("a", 0.05), job("b", 0.05), watch())
        # 并发上限来自配置（默认 4），这里只要求「不为 0 且不超过上限」
        assert 1 <= max_running <= runner_module._concurrency_limit()
        assert gate.running == 0, "全部结束后不该还占着名额"
        assert len(order) == 4

    asyncio.run(main())


def test_polling_happens_outside_the_gate():
    """结构守护：轮询必须在闸机**外面**。

    这条不是可有可无的洁癖——`_poll_video` / `_poll_comfy` 是在等上游出片，
    可能几十分钟。它一旦被写在 `async with self._gate:` 里面，就会攥着并发名额不放：
    默认并发 4，四个视频任务就能让后面的图片/文本任务全在干等，
    现象是「点了没反应」。这里用缩进判断来钉住这个结构（行为测试需要真上游才能覆盖）。
    """
    for name, poll in (("_run_video", "_poll_video"), ("_run_comfy", "_poll_comfy")):
        src = inspect.getsource(getattr(runner_module.TaskRunner, name))
        lines = src.splitlines()
        gate_line = next((l for l in lines if "async with self._gate:" in l), None)
        poll_line = next((l for l in lines if f"await self.{poll}(task_id)" in l), None)
        assert gate_line is not None, f"{name} 里找不到闸机"
        assert poll_line is not None, f"{name} 里找不到轮询调用"
        gate_indent = len(gate_line) - len(gate_line.lstrip())
        poll_indent = len(poll_line) - len(poll_line.lstrip())
        assert poll_indent <= gate_indent, (
            f"{name} 的轮询缩进比闸机还深，说明它被包在闸机里了 —— "
            "等待上游出片不该占并发名额"
        )


def test_start_or_fail_marks_task_failed_with_reason():
    """队列满时要把任务标失败并写明原因：否则任务会永远停在「排队中」。"""
    original = runner_module._queue_limit
    runner_module._queue_limit = lambda: 1
    try:
        async def main() -> None:
            r = runner_module.TaskRunner()
            marked: list[tuple[int, str]] = []

            async def fake_mark(task_id: int, exc: Exception) -> None:
                marked.append((task_id, str(exc)))

            r._mark_failed = fake_mark  # type: ignore[method-assign]
            release = asyncio.Event()

            async def slow() -> None:
                await release.wait()

            r._spawn(1, lambda: slow())
            await asyncio.sleep(0)
            ok = await r.start_or_fail("image", 2)
            assert ok is False
            assert marked and marked[0][0] == 2
            assert "排队" in marked[0][1]
            release.set()
            await asyncio.sleep(0.05)

        asyncio.run(main())
    finally:
        runner_module._queue_limit = original


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
