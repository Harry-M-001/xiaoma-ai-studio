"""时间只有一个时钟（直接 python 运行）。

运行：venv/Scripts/python tests/test_clock.py

这个文件是补一个**已经存在很久、但没人注意到**的洞：

库里所有时间列都由 SQLite 的 `CURRENT_TIMESTAMP` 写，那是 UTC 裸时间；
而 Python 侧有 13 处用 `datetime.now()`（本地时间）写 `completed_at`。
同一行两个时钟，实测差 8 小时（整整一个时区偏移）。

它安静得可怕：不报错、不崩溃，只是任务中心与资产库的时间整体偏早一个时区，
任何「耗时多久」「最近 N 分钟有没有发生」的判断都会得出荒谬结论。
所以这里钉两条线：

1. **写库只有一个时钟**（`app.clock.utcnow`），源码层面不许再出现 `= datetime.now()`；
2. **出口必须标明是 UTC**（`+00:00`），否则浏览器会把 UTC 串当本地时间解析。
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.clock import utc_iso, utcnow  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import Task  # noqa: E402
from app.schemas import TaskOut  # noqa: E402

APP_DIR = BACKEND / "app"


def _run(fn):
    async def main():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as db:
                return await fn(db)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _py_files() -> list[Path]:
    return [p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts]


# ============================================================
# 一、写库只有一个时钟
# ============================================================


def test_no_python_local_clock_writes_to_a_timestamp_column():
    """源码层面禁止 `xxx_at = datetime.now()`。

    允许 `datetime.now()` 存在的地方只剩「日志文件名、日志正文」这类
    本来就该用本地时间的展示场景，所以判据限定在**赋值给时间列**这一种写法上。
    """
    pattern = re.compile(r"\b(created_at|updated_at|completed_at|finished_at)\s*=\s*datetime\.now\(")
    offenders = []
    for path in _py_files():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if pattern.search(line):
                offenders.append(f"{path.relative_to(BACKEND)}:{i}")
    assert not offenders, f"这些地方还在用本地时钟写时间列（库里是 UTC）：{offenders}"


# 只在这些模块里，`datetime.now()`（本地时间）是**对的**：它们产出的是给人看的
# 字符串，不是入库的时间列。新增白名单要写清楚理由，否则就是在给这个洞开后门。
LOCAL_CLOCK_OK = {
    "app/clock.py": "统一时钟本身就定义在这里，它就是那个「唯一允许读本地/UTC 现在」的地方",
    "app/routers/logs.py": "日志导出文件名用本地时间，用户按文件名找日志才对得上",
    "app/services/log_service.py": "日志正文里的时间戳是给人看的，本地时间更直观",
    "app/services/config_transfer_service.py": "快照里的 exportedAt 是带时区的展示值，不是入库时间",
    "app/services/storage.py": "媒体按月分目录，按**本地**月份分，用户照自己的日历才找得到",
    "app/services/update_service.py": "更新检查时间只以字符串直接展示，不经浏览器做时区换算",
}


def test_local_clock_only_appears_where_it_is_display_only():
    """把守卫从「写库」扩到**整个 app 目录**：只要用本地时钟就必须在白名单里。

    这次踩的坑正长这样：预检里拿 `datetime.now()` 去减库里的 `created_at`
    （UTC），算出「10 分钟前其实是 8 小时前」，于是告警永远不触发——
    不改任何一行赋值语句，也能安静地错。只盯赋值是漏的。
    """
    pattern = re.compile(r"datetime\.now\(|datetime\.utcnow\(")
    offenders = []
    for path in _py_files():
        rel = path.relative_to(BACKEND).as_posix()
        if rel in LOCAL_CLOCK_OK:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if pattern.search(stripped):
                offenders.append(f"{rel}:{i}")
    assert not offenders, (
        f"这些地方用了本地时钟但不在白名单里：{offenders}\n"
        "要用统一时钟（app.clock.utcnow / utc_iso）；"
        "确实只是展示用，就加进 LOCAL_CLOCK_OK 并写明理由"
    )


def test_completed_at_is_written_from_the_shared_clock():
    """完成时间必须走 `utcnow`——它是与库里时钟对齐的那一个。"""
    runner = (APP_DIR / "services" / "runner.py").read_text(encoding="utf-8")
    assert "from app.clock import utcnow" in runner, "runner 没有引用统一的时钟"
    assert runner.count("task.completed_at = utcnow()") >= 12, (
        "runner 里还有没改成 utcnow 的完成时间写入"
    )
    # 取消任务那条路径也要用同一个时钟
    generation = (APP_DIR / "routers" / "generation.py").read_text(encoding="utf-8")
    assert "task.completed_at = utcnow()" in generation, "取消任务没走统一时钟"


def test_written_completed_at_matches_the_databases_own_clock():
    """回归测试：同一行的 created_at（库里的 UTC）与 completed_at 不能再差一个时区。

    旧代码这一条会差 8 小时——这正是当初那个洞的形状。
    """

    async def case(db):
        task = Task(kind="image", status="processing", model="m", prompt="x")
        db.add(task)
        await db.commit()
        await db.refresh(task)

        task.status = "succeeded"
        task.completed_at = utcnow()
        await db.commit()
        await db.refresh(task)

        drift = abs((task.completed_at - task.created_at).total_seconds())
        assert drift < 60, (
            f"created_at 与 completed_at 差了 {drift / 3600:.1f} 小时，"
            "说明两个时间列用了不同的时钟"
        )
        return True

    assert _run(case) is True


def test_utcnow_is_naive_and_close_to_real_time():
    """`utcnow` 必须是不带时区标记的 UTC：带了时区，SQLite 那列就写不进去。"""

    async def case(db):
        now = utcnow()
        assert now.tzinfo is None, "写入库的时间不能带时区标记"
        from datetime import datetime, timezone as tz

        real = datetime.now(tz.utc).replace(tzinfo=None)
        assert abs((real - now).total_seconds()) < 5, "utcnow 明显偏离当前 UTC 时间"
        return True

    assert _run(case) is True


# ============================================================
# 二、出口必须标明是 UTC
# ============================================================


def test_utc_iso_labels_bare_times_as_utc():
    from datetime import datetime

    bare = datetime(2026, 9, 17, 2, 14, 56)
    assert utc_iso(bare) == "2026-09-17T02:14:56+00:00"
    assert utc_iso(None) is None


def test_no_schema_field_serializes_a_bare_datetime():
    """schema 里不许再有裸 `datetime` 字段——漏一个就会有一处时间偏一个时区。"""
    src = (APP_DIR / "schemas.py").read_text(encoding="utf-8")
    bad = [
        line.strip()
        for line in src.splitlines()
        if re.match(r"^\s+\w+:\s*datetime(\s*\|\s*None)?\s*(=\s*None)?\s*$", line)
    ]
    assert not bad, f"这些字段还在裸用 datetime，应改成 UtcDateTime：{bad}"


def test_task_out_serializes_with_an_offset():
    from datetime import datetime

    out = TaskOut(
        id=1, kind="image", status="succeeded", service_id=1, model="m", prompt="x",
        params={}, error=None, progress=100, assets=[],
        created_at=datetime(2026, 9, 17, 2, 14, 56),
        completed_at=datetime(2026, 9, 17, 2, 15, 10),
    )
    dumped = out.model_dump(mode="json")
    assert dumped["created_at"] == "2026-09-17T02:14:56+00:00", dumped["created_at"]
    assert dumped["completed_at"] == "2026-09-17T02:15:10+00:00", dumped["completed_at"]


def test_live_api_times_carry_an_offset():
    """真打一次接口：项目列表里的时间必须带时区标记。"""

    async def case():
        from app.main import app

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/projects")
            assert r.status_code == 200, r.status_code
            rows = r.json()
            for row in rows[:5]:
                for field in ("created_at", "updated_at"):
                    value = row.get(field)
                    if value is None:
                        continue
                    assert re.search(r"([+-]\d{2}:\d{2}|Z)$", value), (
                        f"{field} 没有时区标记：{value}"
                    )
        return True

    assert asyncio.run(case()) is True


# ============================================================
# 三、顺带钉住：预检在 HTTP 层确实不拦人
# ============================================================


def test_preflight_endpoints_answer_200_and_never_block():
    """预检的承诺最终要在 HTTP 层成立：给什么输入都回 200，且 blocking 恒为 False。"""

    async def case():
        from app.main import app

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            cases = [
                ("/api/images/preflight", {"prompt": "", "n": 0, "model_key": ""}),
                ("/api/images/preflight", {"prompt": "猫", "n": 3, "model_key": "不存在"}),
                ("/api/images/batch/preflight", {"prompts": [], "model_keys": []}),
                ("/api/videos/preflight", {"prompt": "", "first_frame_asset_id": 999999,
                                           "ratio": "9:16", "resolution": "1080p"}),
            ]
            for path, body in cases:
                r = await c.post(path, json=body)
                assert r.status_code == 200, f"{path} 返回了 {r.status_code}：{r.text[:200]}"
                data = r.json()
                assert data["blocking"] is False, f"{path} 竟然说要拦人"
                assert isinstance(data["warnings"], list)
        return True

    assert asyncio.run(case()) is True


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
