"""按 key 的 single-flight：同一个 key 同一时刻只跑一次，并发请求共享同一个结果。

缩略图是最典型的用法：资产页一屏几十张图，浏览器对同一张图也可能并发发两次请求。
不合并的话会同时跑多次 ffmpeg、还抢同一个临时文件——既慢又容易写出半个 JPEG。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

_tasks: dict[str, asyncio.Task] = {}


async def run_once(key: str, factory: Callable[[], Awaitable[T]]) -> T:
    """执行（或加入）这个 key 正在跑的那一次。

    用 `shield` 等：某个调用方断开只取消它自己，不该把别人还在等的任务一起取消。
    """
    task = _tasks.get(key)
    if task is None:
        task = asyncio.create_task(factory())
        _tasks[key] = task

        def _cleanup(done: asyncio.Task) -> None:
            if _tasks.get(key) is done:
                _tasks.pop(key, None)

        task.add_done_callback(_cleanup)
    return await asyncio.shield(task)
