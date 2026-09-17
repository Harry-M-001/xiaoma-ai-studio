"""全应用只有一个时钟。

库里所有时间列都由 SQLite 的 `CURRENT_TIMESTAMP` 写入——那是 **UTC 裸时间**。
Python 侧只要有一处用 `datetime.now()`（本地时间）写同一张表，同一行里就会出现
两个时钟。实测本机 `created_at` 与 `completed_at` 相差整整一个时区偏移（+8 小时）。

后果不像「时间显示不准」那么轻：

- 「耗时多久」「最近 N 分钟有没有发生」这类判断全都会错，而且是**安静地错**——
  不报任何错就能得出荒谬结论（例如把 10 分钟前刚失败的任务算成 8 小时前）。
- 前端拿到不带时区标记的时间串，会按**本地时间**解析，于是任务中心与资产库
  显示的时间整体偏早一个时区。

所以这里只留一个函数，写库一律用它：库里统一是 UTC 裸时间（与 SQLite 一致），
出接口时再统一标上 UTC（见 `app.schemas.UtcDateTime`），由前端换成本地时间。
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """当前 UTC 时间，去掉时区标记。

    去掉标记是为了跟 SQLite `CURRENT_TIMESTAMP` 的存储格式对齐——SQLite 的
    DateTime 列不接受带时区的值，硬塞进去只会读出来一串解析不了的东西。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_iso(value: datetime | None) -> str | None:
    """把库里的裸时间标成 UTC 后出接口。

    直接原样吐给前端会被当成**本地时间**解析（`new Date("2026-09-17T02:14:56")`
    按本地处理），于是任务中心、资产库、审计记录里的时间整体偏早一个时区。
    带上 `+00:00` 之后，浏览器自己会换算成本地时间，前端不需要知道这件事。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()
