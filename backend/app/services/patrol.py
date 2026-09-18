"""运行中巡检：把「已经没人在推进」的任务从卡住状态里救出来。

**为什么需要它**：视频与工作流的推进挂在一个后台轮询协程上（`TaskRunner._poll_video` /
`_poll_comfy`）。那个协程一旦因为意外异常静默结束（上游接口抽风、数据库忙、网络抖动），
**任务行会永远停在 processing**：进度不动、按钮灰着、用户只能重启服务才走得出来。
启动恢复（`runner.recover()`）只在重启时兜一次，跑着的时候没有任何人管这件事。

**判据只有一条**（两条都要满足，缺一不可）：

1. **进程内已经没有人在推进它**（`TaskRunner.is_running` 为假）；
2. **超过 N 分钟没有任何写入**（`tasks.updated_at` 一直没动过）。

为什么必须两条一起看——

- 只看「慢」会**误杀正常的慢任务**：一个大模型出图十分钟、一个视频等半小时，
  都是正常的。所以轮询协程每写一次进度就刷新 `updated_at`，它是「还活着」的证据。
- 只看「进程内没有协程」会**误杀刚建好还没投递的任务**：那只是一个几百毫秒的窗口。
  加上时间下限之后，只有真的躺了 N 分钟才会被碰。
- 反过来说，**进程内还活着的任务我们一律不碰**，连警告都不发：轮询协程自己带着
  上限（`limits.video_max_wait_minutes`，默认 30 分钟），慢不等于卡。

**收口方式分两种**，取决于「还有没有可能接回去」：

- 有上游任务 id 的视频/工作流任务 → **重连**（把轮询重新挂上，上游还在跑就不浪费这一笔），
  但同一任务最多重连 `REATTACH_LIMIT` 次，过了就如实收口，免得无限重连下去；
- 其余（图片/文本，或连上游 id 都没有）→ **收口成失败**，并写清是谁收的、下一步该干什么。
"""

from __future__ import annotations

from datetime import datetime, timedelta

STALE_KEY = "limits.task_stale_minutes"
INTERVAL_KEY = "limits.patrol_interval_seconds"
DEFAULT_STALE_MINUTES = 10
DEFAULT_INTERVAL_SECONDS = 60

# 巡检的最小间隔：这是一条兜底巡检，不需要秒级频率，扫得太勤只会白读库
MIN_INTERVAL_SECONDS = 15

# 同一条任务最多替它重连几次。重连是「上游可能还在跑，别浪费这一笔」的补救，
# 不是兜底机制：连着几次都接不上，就说明这一笔已经废了，如实说比继续挂着强。
REATTACH_LIMIT = 3

# 巡检要管的两种状态：还没开始的、正在跑的
OPEN_STATUSES = ("pending", "processing")

SKIP = "skip"
REATTACH = "reattach"
FAIL = "fail"


def _config_int(key: str, default: int) -> int:
    from app.services.config_center_service import runtime_value

    try:
        return int(runtime_value(key, default) or default)
    except (TypeError, ValueError):
        # 配置坏了不能把巡检整个停掉，退回默认值继续干活
        return default


def stale_minutes() -> int:
    """多久没有任何写入就算「躺了」。

    配成 0 或负数不是「更激进」，而是**没有意义**：退回默认值。
    夹到下限会让一个手滑的 -5 变成「1 分钟就动手」，把正常任务扫进来——
    这正是巡检最不该做的事（判据宁可保守）。
    """
    value = _config_int(STALE_KEY, DEFAULT_STALE_MINUTES)
    return value if value >= 1 else DEFAULT_STALE_MINUTES


def interval_seconds() -> int:
    """巡检间隔。太小（含 0/负数）同样退回默认值，理由同上。"""
    value = _config_int(INTERVAL_KEY, DEFAULT_INTERVAL_SECONDS)
    return value if value >= MIN_INTERVAL_SECONDS else DEFAULT_INTERVAL_SECONDS


def is_stale(updated_at: datetime | None, *, now: datetime, minutes: int) -> bool:
    """这一行有多久没被写过了。`updated_at` 缺失时按「很久没动」处理。"""
    if updated_at is None:
        return True
    return now - updated_at >= timedelta(minutes=minutes)


def decide(
    *,
    status: str,
    alive: bool,
    stale: bool,
    has_remote: bool,
    reattaches: int,
) -> str:
    """这一条该怎么办：`skip` / `reattach` / `fail`。

    判断顺序就是优先级：**进程内还活着的一律不碰**（慢不等于卡），
    没到时间下限的一律不碰（刚建的任务还没轮到投递）。
    """
    if status not in OPEN_STATUSES:
        return SKIP
    if alive:
        return SKIP
    if not stale:
        return SKIP
    if has_remote and reattaches < REATTACH_LIMIT:
        return REATTACH
    return FAIL


def fail_reason(*, status: str, has_remote: bool, reattaches: int) -> str:
    """收口时写给用户看的话：是谁收的、为什么、下一步怎么办，三样都要有。"""
    if status == "pending":
        return (
            "这条任务排队之后就一直没有开始执行，后台推进已经中断（运行巡检发现），"
            "已替你收口。直接重试即可。"
        )
    if has_remote and reattaches >= REATTACH_LIMIT:
        return (
            f"这条任务反复重连上游都没能继续推进（巡检重连了 {reattaches} 次），已替你收口。"
            "可以重试；如果每次都这样，多半是上游那边这一笔已经失效了。"
        )
    return (
        "这条任务长时间没有任何进展，后台推进已经中断（运行巡检发现），已替你收口。"
        "直接重试即可。"
    )


def reattach_note(*, reattaches: int) -> str:
    """重连时留给这条任务的日志：用户去任务中心时能看出「进度停过，是自己接上的」。"""
    return (
        f"运行巡检发现这条任务的进度已经停了，正在尝试重新接上上游"
        f"（第 {reattaches} 次重连）。"
    )


def summary_line(stats: dict[str, int]) -> str:
    """只在真干了活的时候打日志：每次都喊一句「一切正常」等于把日志变成噪音。"""
    return (
        f"运行巡检：扫了 {stats.get('scanned', 0)} 条未完成任务，"
        f"重连 {stats.get('reattached', 0)} 条，收口 {stats.get('failed', 0)} 条"
    )
