"""生成前软校验：只告警，不阻断。

分寸是这个模块的全部难点，所以先把边界说清楚：

**这里不重复接口层已有的硬校验。** 档位是否合法（`option_service.validate_option`）、
参考图是否存在、模型是否可用、批量是否超上限——那些该 400 就 400，报错就该报得硬气，
用户改完能立刻重试。把同样的检查在这里再写一遍，只会多出一份会漂移的副本。

**这里只回答一类问题**：参数各自都合法，但**组合起来**很可能不是你想要的，
或者大概率白花一次配额。例如首帧是 16:9 却选了 9:16 画幅（首帧会被裁掉两侧）、
首帧只有 720p 却要 1080p（上游只能放大，画面发糊）、批量一次排的任务数超过队列
剩余名额（多出来的会被直接拒）。这类事上游不会报错，出来的片子也能看，
所以只能用「告警 + 你决定」的分寸。

因此每条告警都必须写清两件事：**为什么会有问题**、**怎么改**。
只丢一句「参数可能有问题」等于没说，用户只能猜。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.models import Asset, Task

# 提示词短于这个字数，画面基本交给模型自由发挥
THIN_PROMPT_CHARS = 4
# 宽高比的相对偏差超过这个比例，就认为「不是同一个画幅」
RATIO_TOLERANCE = 0.05
# 批量一次性排这么多任务就提醒一句（提醒，不是拒绝）
BATCH_SOFT_LIMIT = 12
# 最近这么久的同类任务全失败，就认为这个模型当前大概率还是不行
RECENT_FAILURE_WINDOW_MIN = 30
RECENT_FAILURE_RUN = 3
# 清晰度档位 → 短边像素。档位值本身来自 param_options，这里只做数字解读
RESOLUTION_SHORT_SIDE = {"480p": 480, "720p": 720, "1080p": 1080}


@dataclass(frozen=True)
class Warning:
    """一条软告警。`code` 给前端做稳定标识，改文案不会打破它。"""

    code: str
    message: str
    suggestion: str = ""
    # warn = 大概率不是你想要的；info = 只是提醒一下
    level: str = "warn"


@dataclass
class Report:
    warnings: list[Warning] = field(default_factory=list)

    def add(self, w: Warning | None) -> None:
        if w is not None:
            self.warnings.append(w)


# ---------- 小工具 ----------


def _parse_ratio(text: str) -> float | None:
    """把「16:9」解析成 1.777…；解析不出来就返回 None（当作没配，不告警）。"""
    left, _, right = (text or "").partition(":")
    try:
        a, b = float(left), float(right)
    except ValueError:
        return None
    if a <= 0 or b <= 0:
        return None
    return a / b


def _ratio_text(width: int, height: int) -> str:
    """把像素反推成一个人话比例，用于告警文案里对帐。"""
    ratio = width / height
    for text in ("1:1", "4:3", "3:4", "16:9", "9:16", "21:9"):
        parsed = _parse_ratio(text)
        if parsed and abs(ratio - parsed) / parsed <= RATIO_TOLERANCE:
            return text
    return f"{ratio:.2f}:1"


def _prompt_warning(prompt: str) -> Warning | None:
    stripped = (prompt or "").strip()
    if len(stripped) >= THIN_PROMPT_CHARS:
        return None
    return Warning(
        code="prompt.thin",
        message=(
            f"提示词只有 {len(stripped)} 个字，画面基本交给模型自由发挥——"
            "同一条提示词跑两次，出来的差别会很大。"
        ),
        suggestion="补上主体、动作、镜头和质感，例如「少女站在窗边，逆光，中景，胶片质感」。",
    )


async def _recent_failure_warning(
    db: AsyncSession, model_key: str, kind: str
) -> Warning | None:
    """最近几次同类任务是不是全失败。

    这条比任何参数检查都值钱：参数对不对要看到片子才知道，但「这个模型现在就是不行」
    从任务记录里能直接看出来，早点说一声就能省掉一次白等。
    """
    if not model_key:
        return None
    rows = await db.execute(
        select(Task.status, Task.error)
        .where(Task.model == model_key, Task.kind == kind)
        .order_by(Task.id.desc())
        .limit(RECENT_FAILURE_RUN)
    )
    recent = rows.all()
    if len(recent) < RECENT_FAILURE_RUN:
        return None
    if any(status != "failed" for status, _ in recent):
        return None

    # 全失败还要够新，否则可能是上周的旧账
    stamp = await db.execute(
        select(Task.created_at)
        .where(Task.model == model_key, Task.kind == kind, Task.status == "failed")
        .order_by(Task.id.desc())
        .limit(1)
    )
    newest = stamp.scalar_one_or_none()
    if newest is None:
        return None
    # 必须用统一时钟比对：created_at 是库里写的 UTC，用本地 now() 去减会差一个时区，
    # 结果是把 10 分钟前刚失败的任务算成 8 小时前的旧账，于是永远不告警
    if utcnow() - newest > timedelta(minutes=RECENT_FAILURE_WINDOW_MIN):
        return None

    last_error = next((e for _, e in recent if e), "") or "（未记录原因）"
    return Warning(
        code="model.recent_failures",
        message=(
            f"「{model_key}」最近 {RECENT_FAILURE_RUN} 次同类任务全部失败，"
            f"最近一次报的是「{last_error}」。现在再发，大概率还是同一个结果。"
        ),
        suggestion="先去「任务中心」看一眼那条失败任务，或者换一个模型 / 服务再试。",
    )


def _queue_headroom() -> tuple[int, dict[str, int]]:
    """队列还能接几个任务。返回 (剩余名额, 概况)。"""
    from app.services.runner import runner

    stats = runner.queue_stats()
    return max(0, stats["maxLive"] - stats["live"]), stats


def _queue_warning(planned: int) -> Warning | None:
    """这次要排的任务数会不会直接撑爆队列。

    这是「生成前告警」最有价值的用法：队列满时后端会把任务标失败（不是排队），
    如果等到那一刻用户才知道，看到的是一片失败任务，而不是一句「你一次点太多了」。
    """
    if planned <= 1:
        return None
    headroom, stats = _queue_headroom()
    if planned <= headroom:
        return None
    return Warning(
        code="queue.will_reject",
        message=(
            f"这次会一次排 {planned} 个任务，但队列只剩 {headroom} 个名额"
            f"（上限 {stats['maxLive']}，当前在跑 + 在排队 {stats['live']} 个）——"
            "超出的那些会被直接拒绝并记为失败。"
        ),
        suggestion=(
            "分批提交；或者到「系统设置 → 系统配置」把「排队上限」调大"
            "（调大「任务并发数」能让队列消化得更快）。"
        ),
        level="warn",
    )


def _batch_scale_warning(planned: int, prompts: int, models: int) -> Warning | None:
    if planned < BATCH_SOFT_LIMIT:
        return None
    from app.services.runner import runner

    concurrency = runner.queue_stats()["concurrency"] or 1
    rounds = (planned + concurrency - 1) // concurrency
    return Warning(
        code="image.batch_scale",
        message=(
            f"这次会一次排 {planned} 个任务（{prompts} 行提示词 × {models} 个模型）。"
            f"按当前并发 {concurrency}，大约要 {rounds} 轮才能跑完。"
        ),
        suggestion="想先看效果，可以只用 1 行提示词试一次；确认构图方向对了再整批跑。",
        level="info",
    )


# ---------- 图片 ----------


async def check_image(
    db: AsyncSession,
    *,
    prompt: str,
    model_key: str = "",
    n: int = 1,
    ref_asset_ids: list[int] | None = None,
) -> Report:
    report = Report()
    report.add(_prompt_warning(prompt))
    report.add(await _recent_failure_warning(db, model_key, "image"))

    count = max(1, int(n or 1))
    report.add(_queue_warning(count))

    # 参考图张数 × 张数：上游一般按「一次请求出一个批次」处理，张数越多越容易中途失败，
    # 而失败之后是整批重来。这里只在确实偏多时提一句。
    refs = len(ref_asset_ids or [])
    if refs >= 4 and count >= 4:
        report.add(
            Warning(
                code="image.heavy_request",
                message=(
                    f"这次是 {refs} 张参考图 × {count} 张出图，单次请求偏重——"
                    "上游中途失败的话，这一批要从头再来。"
                ),
                suggestion="可以先把张数降到 1～2 张，确认参考图生效了再加量。",
                level="info",
            )
        )
    return report


async def check_image_batch(
    db: AsyncSession,
    *,
    prompts: list[str],
    model_keys: list[str],
    n: int = 1,
) -> Report:
    report = Report()
    clean = [p for p in prompts if p and p.strip()]
    uniq_models = list(dict.fromkeys(model_keys))

    thin = [p for p in clean if len(p.strip()) < THIN_PROMPT_CHARS]
    if clean and len(thin) == len(clean):
        report.add(_prompt_warning(clean[0]))
    elif thin:
        report.add(
            Warning(
                code="prompt.thin",
                message=f"有 {len(thin)} 行提示词短于 {THIN_PROMPT_CHARS} 个字，这几行出来的画面基本靠模型自由发挥。",
                suggestion="补上主体、动作、镜头和质感再整批跑，省得回头重来。",
            )
        )

    for mk in uniq_models:
        report.add(await _recent_failure_warning(db, mk, "image"))

    planned = max(0, len(clean) * len(uniq_models))
    report.add(_queue_warning(planned))
    report.add(_batch_scale_warning(planned, len(clean), len(uniq_models)))
    return report


# ---------- 视频 ----------


async def check_video(
    db: AsyncSession,
    *,
    prompt: str,
    model_key: str = "",
    first_frame_asset_id: int | None = None,
    ratio: str = "",
    resolution: str = "",
) -> Report:
    report = Report()
    report.add(_prompt_warning(prompt))
    report.add(await _recent_failure_warning(db, model_key, "video"))
    report.add(_queue_warning(1))
    report.add(await _first_frame_warnings(db, first_frame_asset_id, ratio, resolution))
    return report


def _frame_pixels(frame: Asset) -> tuple[int, int] | None:
    """首帧的像素尺寸：先看库里记的，没记就现读一次文件。

    为什么要兜这一下：`assets.width/height` 是后来才开始记的，在那之前入库的图片
    是空的。少了这层兜底，老资产用图生视频时这条告警会**静默失效**——
    不报错、不提示，看起来就像「这个功能对老素材不生效」。
    """
    if frame.width and frame.height:
        return int(frame.width), int(frame.height)

    from app.services import storage
    from app.services.image_size import read_image_size_from_file

    try:
        path = storage.abs_path(frame.filename)
    except (ValueError, OSError):
        return None
    return read_image_size_from_file(path)


async def _first_frame_warnings(
    db: AsyncSession, first_frame_asset_id: int | None, ratio: str, resolution: str
) -> Warning | None:
    """首帧与画幅 / 清晰度对不对得上。

    图生视频最容易被忽略的一步：首帧定了调，但比例和清晰度是另一组参数。
    对不上时上游不会报错——它会闷头裁切或者放大，成片看起来「怪怪的」，
    而用户只会觉得是模型不行。
    """
    if first_frame_asset_id is None:
        return None
    frame = await db.get(Asset, first_frame_asset_id)
    if frame is None:
        return None

    size = _frame_pixels(frame)
    if size is None:
        return None

    fw, fh = size
    frame_ratio_text = f"{fw}×{fh}（{_ratio_text(fw, fh)}）"

    want = _parse_ratio(ratio)
    if want:
        have = fw / fh
        if abs(have - want) / want > RATIO_TOLERANCE:
            return Warning(
                code="video.first_frame_ratio_mismatch",
                message=(
                    f"首帧是 {frame_ratio_text}，这次选的画幅是 {ratio}——"
                    f"出片会按 {ratio} 出，首帧多出来的那一边会被裁掉。"
                ),
                suggestion=(
                    f"要么把画幅改成 {_ratio_text(fw, fh)}，"
                    "要么先出一张对应画幅的首帧（图片生成里选对应尺寸）。"
                ),
            )

    short = min(fw, fh)
    target = RESOLUTION_SHORT_SIDE.get((resolution or "").strip().lower())
    if target and short < target:
        return Warning(
            code="video.first_frame_upscaled",
            message=(
                f"首帧短边只有 {short}px，但清晰度选了 {resolution}——"
                "上游只能把它放大，画面会发糊（糊掉之后就再也回不来了）。"
            ),
            suggestion=(
                f"把清晰度降到 {short}p 附近；或者先用图片生成出一张短边 ≥ {target}px 的首帧。"
            ),
        )
    return None
