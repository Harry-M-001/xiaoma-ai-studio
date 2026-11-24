"""single-flight：同一个 key 的并发请求只跑一次，失败不缓存。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.services import keyed_tasks  # noqa: E402


def test_concurrent_callers_share_one_run():
    calls = 0

    async def scenario():
        async def factory():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return "ok"

        return await asyncio.gather(
            *(keyed_tasks.run_once("thumb:a.png:320", factory) for _ in range(8))
        )

    assert asyncio.run(scenario()) == ["ok"] * 8
    assert calls == 1, "并发请求各自跑了一遍，single-flight 没生效"


def test_failure_reaches_everyone_and_next_call_retries():
    calls = 0

    async def scenario():
        async def boom():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            raise RuntimeError("ffmpeg 挂了")

        results = await asyncio.gather(
            *(keyed_tasks.run_once("thumb:b.png:320", boom) for _ in range(3)),
            return_exceptions=True,
        )
        assert all(isinstance(r, RuntimeError) for r in results)

        async def good():
            return "done"

        return await keyed_tasks.run_once("thumb:b.png:320", good)

    assert asyncio.run(scenario()) == "done"
    assert calls == 1, "失败的那次被重跑了，或者失败结果被缓存了"
