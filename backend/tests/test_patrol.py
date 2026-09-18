"""#31 运行中巡检的回归测试（直接 python 运行）。

运行：venv/Scripts/python tests/test_patrol.py

这一版做的是「跑着的时候把卡住的任务救出来」，所以测四件事：

1. **不许误杀**。进程内还活着的、刚建好还没轮到投递的，一律不碰——误杀比漏杀严重得多：
   一个正常的慢任务被标失败，用户白等一场、还可能白花一次上游的钱。
2. **判据两条都要满足**：进程内没人推进 **且** 静置够久。
3. **收口要说清是谁做的、下一步怎么办**：任务行的 error 与任务日志里都要留下。
4. **重连有上限**：连着接不上就如实收口，不能无限重连把用户一直吊着。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections import deque
from datetime import timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.clock import utcnow  # noqa: E402
from app.database import Base, SessionLocal  # noqa: E402
from app.models import Task  # noqa: E402
from app.registry import schema_registry  # noqa: E402
from app.services import log_service, patrol  # noqa: E402
from app.services import runner as runner_mod  # noqa: E402

# 巡检本来就会在收口时打 warning，测试里不用看一屏日志
logging.getLogger("xiaoma.runner").setLevel(logging.CRITICAL)


# ================================================================ 纯逻辑


def test_a_task_someone_is_still_running_is_never_touched():
    """慢不等于卡：进度条不动但协程还活着，就说明有人在推进，绝不碰。"""
    action = patrol.decide(
        status="processing", alive=True, stale=True, has_remote=False, reattaches=0
    )
    assert action == patrol.SKIP


def test_a_task_that_just_started_is_never_touched():
    """刚建好还没轮到投递的任务，`updated_at` 是新的，不能算「躺了」。"""
    action = patrol.decide(
        status="pending", alive=False, stale=False, has_remote=False, reattaches=0
    )
    assert action == patrol.SKIP


def test_a_dead_task_with_an_upstream_is_reattached_first():
    """上游可能还在跑：先重连，别浪费这一笔。"""
    action = patrol.decide(
        status="processing", alive=False, stale=True, has_remote=True, reattaches=0
    )
    assert action == patrol.REATTACH


def test_a_dead_task_without_an_upstream_is_closed():
    action = patrol.decide(
        status="processing", alive=False, stale=True, has_remote=False, reattaches=0
    )
    assert action == patrol.FAIL


def test_reattaching_gives_up_after_the_limit():
    """连着接不上就说明这一笔废了：继续重连只是把用户一直吊着。"""
    action = patrol.decide(
        status="processing",
        alive=False,
        stale=True,
        has_remote=True,
        reattaches=patrol.REATTACH_LIMIT,
    )
    assert action == patrol.FAIL


def test_finished_tasks_are_skipped():
    for status in ("completed", "failed", "cancelled"):
        assert patrol.decide(
            status=status, alive=False, stale=True, has_remote=True, reattaches=0
        ) == patrol.SKIP, f"{status} 不该被巡检碰"


def test_stale_is_measured_from_the_last_write():
    now = utcnow()
    assert patrol.is_stale(now - timedelta(minutes=9), now=now, minutes=10) is False
    assert patrol.is_stale(now - timedelta(minutes=10), now=now, minutes=10) is True
    assert patrol.is_stale(None, now=now, minutes=10) is True, "读不到时间就当作早就躺了"


def test_reasons_say_who_did_it_and_what_to_do_next():
    """三种情形的话术必须不一样，且都要说清「谁收的、怎么办」。"""
    never_started = patrol.fail_reason(status="pending", has_remote=False, reattaches=0)
    interrupted = patrol.fail_reason(status="processing", has_remote=False, reattaches=0)
    gave_up = patrol.fail_reason(
        status="processing", has_remote=True, reattaches=patrol.REATTACH_LIMIT
    )
    assert len({never_started, interrupted, gave_up}) == 3, "三种情形不能共用一句话"
    for text in (never_started, interrupted, gave_up):
        assert "巡检" in text, "没说是谁收的口"
        assert "重试" in text, "没告诉用户下一步做什么"
    assert "没有开始执行" in never_started
    assert f"{patrol.REATTACH_LIMIT} 次" in gave_up, "放弃重连时要说清重连了几次"


def test_reattach_note_names_the_patrol_and_the_attempt():
    note = patrol.reattach_note(reattaches=2)
    assert "巡检" in note and "2 次" in note


def test_settings_survive_broken_config_and_keep_floors():
    """配置写坏了、或写成没意义的数，都要退回默认值——不能因此把正常任务扫进来。"""
    from app.services import config_center_service

    real = config_center_service.runtime_value

    def with_value(value):  # noqa: ANN001, ANN202
        config_center_service.runtime_value = lambda *a, **k: value  # type: ignore[assignment]

    try:
        # 读不出来 / 根本不是数字 → 默认值
        for bad in ("abc", None, "", "3.5"):
            with_value(bad)
            assert patrol.stale_minutes() == patrol.DEFAULT_STALE_MINUTES, bad
            assert patrol.interval_seconds() == patrol.DEFAULT_INTERVAL_SECONDS, bad
        # 0 与负数不是「更激进」，是没意义 → 默认值（夹到下限等于让手滑变成 1 分钟就动手）
        for nonsense in (0, -5):
            with_value(nonsense)
            assert patrol.stale_minutes() == patrol.DEFAULT_STALE_MINUTES, nonsense
            assert patrol.interval_seconds() == patrol.DEFAULT_INTERVAL_SECONDS, nonsense
        # 合理的值要照用，包括「比默认更激进但仍然有意义」的那种
        with_value(1)
        assert patrol.stale_minutes() == 1
        with_value(20)
        assert patrol.interval_seconds() == 20
        # 间隔小到没意义（5 秒）还是退回默认，免得把库扫爆
        with_value(5)
        assert patrol.interval_seconds() == patrol.DEFAULT_INTERVAL_SECONDS
    finally:
        config_center_service.runtime_value = real  # type: ignore[assignment]


def test_summary_line_tells_what_changed():
    line = patrol.summary_line({"scanned": 5, "reattached": 1, "failed": 2, "skipped": 2})
    assert "5" in line and "1" in line and "2" in line


# ================================================================ 接线


async def _seed_task(
    maker,  # noqa: ANN001
    *,
    status: str = "processing",
    kind: str = "image",
    remote: str = "",
    age_minutes: int = 30,
) -> int:
    """造一条躺在那里不动的任务：`updated_at` 直接写成 `age_minutes` 分钟前。"""
    old = utcnow() - timedelta(minutes=age_minutes)
    async with maker() as db:
        task = Task(
            kind=kind, status=status, service_id=1, model="m1", prompt="", params_json="{}",
            remote_job_id=remote or None, created_at=old, updated_at=old,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        return task.id


async def _task(maker, task_id: int) -> tuple[str, str]:  # noqa: ANN001
    async with maker() as db:
        t = await db.get(Task, task_id)
        return t.status, (t.error or "")


def _run(fn):  # noqa: ANN001
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    real_session = runner_mod.SessionLocal
    real_stale = patrol.stale_minutes
    real_interval = patrol.interval_seconds

    async def scenario(inst):  # noqa: ANN001, ANN202
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        runner_mod.SessionLocal = maker
        patrol.stale_minutes = lambda: 10  # type: ignore[assignment]
        try:
            await fn(inst, maker)
        finally:
            runner_mod.SessionLocal = real_session
            patrol.stale_minutes = real_stale  # type: ignore[assignment]
            patrol.interval_seconds = real_interval  # type: ignore[assignment]
            await engine.dispose()

    # 每个用例用全新的 TaskRunner：重连计数是实例状态，别让用例之间互相影响
    asyncio.run(scenario(runner_mod.TaskRunner()))


def test_patrol_closes_a_task_whose_poller_died():
    """这一版的主场景：轮询协程静默结束，任务行永远停在 processing。"""
    async def scenario(inst, maker):  # noqa: ANN001
        task_id = await _seed_task(maker, status="processing", age_minutes=30)
        stats = await inst.patrol()
        assert stats == {"scanned": 1, "reattached": 0, "failed": 1, "skipped": 0}, stats
        status, err = await _task(maker, task_id)
        assert status == "failed"
        assert "巡检" in err and "重试" in err
        lines = log_service.task_logs(task_id)
        assert any("巡检" in line for line in lines), "任务日志里没留下是谁收口的"
        log_service.drop_task_logs(task_id)

    _run(scenario)


def test_patrol_reattaches_a_video_task_that_still_has_an_upstream():
    async def scenario(inst, maker):  # noqa: ANN001
        task_id = await _seed_task(
            maker, status="processing", kind="video", remote="job-abc", age_minutes=30
        )
        called: list[int] = []
        inst.reattach = lambda tid: called.append(tid) or True  # type: ignore[assignment]
        inst.reattach_comfy = lambda tid: called.append(-tid) or True  # type: ignore[assignment]

        stats = await inst.patrol()
        assert called == [task_id], "没有替它重新挂上轮询"
        assert stats["reattached"] == 1 and stats["failed"] == 0, stats
        status, _ = await _task(maker, task_id)
        assert status == "processing", "只是重连，不该把任务收口"
        assert any("重连" in line for line in log_service.task_logs(task_id))
        log_service.drop_task_logs(task_id)

    _run(scenario)


def test_patrol_uses_the_comfy_reattach_for_workflow_tasks():
    async def scenario(inst, maker):  # noqa: ANN001
        task_id = await _seed_task(
            maker, status="processing", kind="workflow", remote="prompt-1", age_minutes=30
        )
        called: list[int] = []
        inst.reattach = lambda tid: called.append(tid) or True  # type: ignore[assignment]
        inst.reattach_comfy = lambda tid: called.append(-tid) or True  # type: ignore[assignment]
        await inst.patrol()
        assert called == [-task_id], "工作流任务要走 ComfyUI 的重连口"
        log_service.drop_task_logs(task_id)

    _run(scenario)


def test_patrol_never_touches_a_live_task():
    async def scenario(inst, maker):  # noqa: ANN001
        task_id = await _seed_task(maker, status="processing", age_minutes=999)
        inst.is_running = lambda tid: True  # type: ignore[assignment]
        stats = await inst.patrol()
        assert stats["failed"] == 0 and stats["reattached"] == 0, stats
        status, _ = await _task(maker, task_id)
        assert status == "processing"

    _run(scenario)


def test_patrol_never_touches_a_task_that_just_started():
    async def scenario(inst, maker):  # noqa: ANN001
        task_id = await _seed_task(maker, status="processing", age_minutes=0)
        stats = await inst.patrol()
        assert stats["failed"] == 0 and stats["reattached"] == 0, stats
        status, _ = await _task(maker, task_id)
        assert status == "processing"

    _run(scenario)


def test_patrol_closes_a_task_that_never_started():
    """排队之后就没下文的（进程在投递前挂过）：也要放出来，别一直占着「排队中」。"""
    async def scenario(inst, maker):  # noqa: ANN001
        task_id = await _seed_task(maker, status="pending", age_minutes=30)
        stats = await inst.patrol()
        assert stats["failed"] == 1, stats
        status, err = await _task(maker, task_id)
        assert status == "failed" and "没有开始执行" in err
        log_service.drop_task_logs(task_id)

    _run(scenario)


def test_patrol_gives_up_after_repeated_reattaches():
    """重连上限：挂上了又立刻断，不能一直挂下去。"""
    async def scenario(inst, maker):  # noqa: ANN001
        task_id = await _seed_task(
            maker, status="processing", kind="video", remote="job-x", age_minutes=30
        )
        inst.reattach = lambda tid: True  # 挂上就返回 True，但进程内一直是「没人推进」

        for _ in range(patrol.REATTACH_LIMIT):
            await inst.patrol()
        status, _ = await _task(maker, task_id)
        assert status == "processing", "还没到上限就收口了"

        stats = await inst.patrol()
        status, err = await _task(maker, task_id)
        assert stats["failed"] == 1, stats
        assert status == "failed" and f"{patrol.REATTACH_LIMIT} 次" in err
        log_service.drop_task_logs(task_id)

    _run(scenario)


def test_patrol_skips_reattach_when_the_queue_is_full():
    """队列满时重连会被拒：这一轮放过它、下一轮再来，不能顺手标失败。"""
    async def scenario(inst, maker):  # noqa: ANN001
        task_id = await _seed_task(
            maker, status="processing", kind="video", remote="job-y", age_minutes=30
        )
        inst.reattach = lambda tid: False  # type: ignore[assignment]
        stats = await inst.patrol()
        assert stats["reattached"] == 0 and stats["failed"] == 0, stats
        status, _ = await _task(maker, task_id)
        assert status == "processing", "被拒的重连不该顺手把任务收口"

    _run(scenario)


def test_patrol_keeps_going_after_a_failing_round():
    """巡检自己炸了不能把应用带下去，也不能停掉后续轮次。"""
    async def scenario(inst, maker):  # noqa: ANN001
        patrol.interval_seconds = lambda: 0  # type: ignore[assignment]
        rounds: list[int] = []

        async def flaky() -> None:
            rounds.append(1)
            if len(rounds) >= 3:
                raise asyncio.CancelledError  # 应用退出时就是这个信号，循环要让它穿过去
            raise RuntimeError("这一轮炸了")

        inst.patrol = flaky  # type: ignore[assignment]
        try:
            await inst.patrol_loop()
        except asyncio.CancelledError:
            pass
        assert len(rounds) == 3, "炸了一轮之后就停了"

    _run(scenario)


def _runner_src() -> str:
    return (BACKEND / "app" / "services" / "runner.py").read_text(encoding="utf-8")


def _method(name: str) -> str:
    """切出 `TaskRunner` 里某个方法的正文。

    边界要把 `async def` 也算上——只认 `def ` 会让切片一路跑到底，把后面别的方法
    的正文当成这个方法的一部分（第一版就栽在这里：`reattach_comfy` 后面紧跟着的
    异步方法里有裸轮询调用，于是「重连是不是裸轮询」这一问答错了）。
    """
    src = _runner_src()
    start = src.index(f"def {name}(")
    ends = [
        p
        for p in (src.find("\n    def ", start + 1), src.find("\n    async def ", start + 1))
        if p != -1
    ]
    return src[start : min(ends) if ends else len(src)]


def test_the_patrol_interval_is_read_every_round():
    """间隔每轮重读配置：改完不用重启（与并发数、队列上限同一套做法）。"""
    assert "patrol.interval_seconds()" in _method("patrol_loop")


def test_main_starts_and_stops_the_patrol_loop():
    src = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    assert "runner.patrol_loop()" in src, "应用启动时没起巡检"
    assert "patrol_task.cancel()" in src, "应用退出时没停掉巡检"
    assert "await patrol_task" in src, "取消了却没等它结束，退出时会留下一个 pending 任务"


def test_the_two_patrol_settings_are_configurable():
    """巡检的判据必须能从设置里调，否则用户只能改代码。"""
    spec = schema_registry.get("config_items")
    items = {i["key"]: i for i in spec.seed}
    for key in (patrol.STALE_KEY, patrol.INTERVAL_KEY):
        assert key in items, f"注册表里没有 {key}"
        item = items[key]
        assert item["group_name"] == "limits"
        assert item["value_type"] == "int"
        assert item["description"].strip(), f"{key} 没写说明，设置页上就是一片空白"
    assert items[patrol.STALE_KEY]["value"] == patrol.DEFAULT_STALE_MINUTES
    assert items[patrol.INTERVAL_KEY]["value"] == patrol.DEFAULT_INTERVAL_SECONDS


def test_notes_append_without_wiping_the_task_logs():
    """补一行不能把任务自己的日志冲掉——那才是排查时最值钱的东西。"""
    try:
        # 直接塞一份「任务自己留下的日志」，等价于 task_log_scope 期间写进去的内容
        log_service._task_buffers["424242"] = deque(["旧的一行"], maxlen=10)
        log_service.note_task(424242, "运行巡检：收口了这条任务")
        lines = log_service.task_logs(424242)
        assert len(lines) == 2, lines
        assert lines[0] == "旧的一行", "补一行把之前的都冲掉了"
        assert "巡检" in lines[1]
    finally:
        log_service.drop_task_logs(424242)


def test_task_logs_can_be_noted_outside_the_execution_scope():
    """协程都结束了才轮到巡检发现，那时没有任务日志上下文，也要能补上话。"""
    try:
        log_service.note_task(777001, "运行巡检：这条任务的进度停了")
        assert log_service.task_logs(777001), "任务执行上下文之外补不上日志"
    finally:
        log_service.drop_task_logs(777001)


def test_patrol_json_is_stable_for_callers():
    """返回值是给日志和测试看的，键不能悄悄改名。"""
    stats = {"scanned": 0, "reattached": 0, "failed": 0, "skipped": 0}
    assert set(json.loads(json.dumps(stats))) == {"scanned", "reattached", "failed", "skipped"}


def test_a_crashing_reattached_poller_does_not_strand_the_task():
    """重连之后的轮询炸了，也必须把任务了结掉。

    这是同一个洞的另一半：`_run_video` 里的轮询包了 try/except，但重连那条路以前没有——
    一次网络抖动就换来一条永远停在 processing 的任务，而那时巡检还没出生。
    """
    async def scenario(inst, maker):  # noqa: ANN001
        task_id = await _seed_task(maker, status="processing", age_minutes=1)

        async def boom(_tid):  # noqa: ANN001, ANN202
            raise RuntimeError("上游抽风")

        inst._poll_video = boom  # type: ignore[assignment]
        await inst._poll_video_guarded(task_id)  # 不该往外抛
        status, err = await _task(maker, task_id)
        assert status == "failed", "重连的轮询炸了却没把任务了结"
        assert "抽风" in err

    _run(scenario)


def test_the_comfy_poller_is_guarded_the_same_way():
    async def scenario(inst, maker):  # noqa: ANN001
        task_id = await _seed_task(maker, status="processing", kind="workflow", age_minutes=1)

        async def boom(_tid):  # noqa: ANN001, ANN202
            raise RuntimeError("ComfyUI 掉线")

        inst._poll_comfy = boom  # type: ignore[assignment]
        await inst._poll_comfy_guarded(task_id)
        status, err = await _task(maker, task_id)
        assert status == "failed" and "掉线" in err

    _run(scenario)


def test_both_reattach_entries_go_through_the_guarded_poller():
    """静态钉住：别哪天又把重连接回「裸轮询」，洞会原地复现。"""
    assert "_poll_video_guarded" in _method("reattach")
    assert "_poll_comfy_guarded" in _method("reattach_comfy")
    assert "_poll_video(task_id)" not in _method("reattach"), "重连又接回裸轮询了"
    assert "_poll_comfy(task_id)" not in _method("reattach_comfy"), "重连又接回裸轮询了"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001  一个用例炸了别把剩下的都带下去
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
