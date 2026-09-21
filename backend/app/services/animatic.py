"""静图缓动样片（animatic）：把分镜图按分镜表的「时长 + 运镜」串成一条能看的片子。

为什么要有这一层：逐镜出视频是**真金白银**，但「这些镜头连起来节奏对不对、运镜方案
成不成立」在出视频之前就能看出来。分镜图已经出好了，分镜表里也明明白白写着每镜几秒、
怎么运镜——把它们翻译成 ffmpeg 的缓动参数，就能零生成成本地先出一版。

与 `storyboard_lint` 是同一层思路的两种用法：那个用规则挑毛病，这个用画面看节奏。
两者都复用 `storyboard_sheet.parse_storyboard`，所以「体检说几镜」与「样片几镜」永远一致。

三个值得记住的设计决定：

1. **运镜照分镜表来，不是随机挑一个动效。** 分镜表写「固定」就真的不动——那样才能
   看出「这场戏全是固定镜头，是不是太闷」。给所有镜头套同一个推近，等于把分镜表的
   运镜栏当废纸，样片也就失去了审片的意义。
2. **只有需要「挪视窗」的运镜才做超采样。** 推/拉只是改缩放，源图按输出尺寸取即可，
   画面是像素级的；摇/移/升降需要四周有余量，才把源图放大 1.25 倍再来回挪。
   这样静止与推拉镜头不会被无谓地重采样一遍（细节是真的会糊）。
3. **不做裁切就不做**：画幅默认「跟随图片」，此时不存在裁切，看到的构图就是用户出的
   那张图。只有用户显式选了别的画幅，才按选定的比例裁切填满——宁可说清楚，也不要
   悄悄改掉他已经认可的构图。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import camera_moves
from .storyboard_sheet import Shot

# ---------------------------------------------------------------- 常量

# 预览帧率：样片是用来看节奏的，30 够用且到处都能放（24 在某些播放器上会抖）
FPS = 30
# 需要挪视窗时源图的超采样倍数：1.25 意味着每个方向都有 25% 的余量可挪
OVERSCAN = 1.25
# 单镜时长夹取范围：1 秒以下看不清，12 秒以上没人有耐心看完一版样片
MIN_SHOT_SECONDS = 1
MAX_SHOT_SECONDS = 12
# 整条样片的时长上限。超了就只渲染前面若干镜并在报告里说清楚——
# 1080p 下 zoompan 是纯 CPU 过滤，再长就不是「随手出一版」而是一次漫长的等待了。
MAX_TOTAL_SECONDS = 120
# 未写时长时的兜底（可在节点上改）
DEFAULT_SHOT_SECONDS = 3

# 长边像素档位（节点上选）：1280 够审节奏，1920 给需要看清细节的场合。
# 真实输出还会被源图尺寸压一次——**不放大**，避免把 512px 的图糊成 1920。
LONG_SIDE_CHOICES = (1280, 1920)
DEFAULT_LONG_SIDE = 1280

# 画幅档位：source = 跟随图片（不裁切）；其余为固定比例（裁切填满）
RATIO_CHOICES = ("source", "16:9", "9:16", "1:1", "4:3")
DEFAULT_RATIO = "source"
_RATIO_VALUE: dict[str, tuple[int, int]] = {
    "16:9": (16, 9),
    "9:16": (9, 16),
    "1:1": (1, 1),
    "4:3": (4, 3),
}

# 运镜类型。**判词在 `camera_moves`，这里只管「key → 缓动参数」**。
# 以前这里有一份自己排过序的关键词表，而 `storyboard_lint` 另有等价的一份、
# 提示词里又手写了第三份——四份表必然各自漂移。现在收敛：
# 提示词给模型同一份词表、解析与体检认同一份、这里只做映射。
PUSH = "push"
PULL = "pull"
PAN_RIGHT = "pan_right"
PAN_LEFT = "pan_left"
TILT_UP = "tilt_up"
TILT_DOWN = "tilt_down"
ORBIT = "orbit"
STATIC = "static"

# 判定「固定镜头」用的词表已经搬去 `camera_moves.is_static()`。
# 保留这个名字只为兼容老调用点与测试，**不要在这里再加词**。
STATIC_WORDS = ("固定", "静止", "不动", "定格", "static", "lock")


@dataclass(frozen=True)
class MoveSpec:
    """一个镜头的缓动参数。

    `x_from/x_to/y_from/y_to` 是**视窗在可用范围内的相对位置**（0=最左/最上，1=最右/最下），
    不是像素——这样换分辨率、换画幅都不用重算。
    """

    kind: str
    zoom_from: float = 1.0
    zoom_to: float = 1.0
    x_from: float = 0.5
    x_to: float = 0.5
    y_from: float = 0.5
    y_to: float = 0.5

    @property
    def need_room(self) -> bool:
        """是否需要「四周的余量」才能实现（摇/移/升降/环绕要，推拉与固定不要）。"""
        return (
            abs(self.x_from - self.x_to) > 1e-6 or abs(self.y_from - self.y_to) > 1e-6
        )

    @property
    def overscan(self) -> float:
        return OVERSCAN if self.need_room else 1.0


def is_static_move(move_text: str) -> bool:
    """分镜表的「运镜」栏是不是固定机位。

    **判据只有一处**（`camera_moves.is_static`）：以前这里与 `storyboard_lint`
    各有一份等价实现，而两份必须在「固定/静止/不动/static/lock」上永远给出同一个答案
    ——否则会出现「体检说不是固定、样片却不动」这种自相矛盾。
    """
    return camera_moves.is_static(move_text)


def move_spec(move_text: str, *, default: str = PUSH) -> MoveSpec:
    """把分镜表的运镜文字翻译成缓动参数。

    **认词在 `camera_moves.match()`**（先用多字别名、再用单字兜底），这里只把认到的
    key 映射成缓动参数——两边各留一份关键词表必然漂移。

    一个都没认出来时返回 `default` 指定的运镜（默认轻微推近）：样片是给人看节奏的，
    一整条死画面比「动得不太对」更没用。节点上可以把默认改成「固定」
    （`sampleDefaultMove=static`），那样认不出来就真的不动。

    注意：**「认不出来」这件事本身要由体检说，不是这里悄悄兜底**——
    `storyboard_lint` 会把不在词表里的运镜列出来并给建议。
    """
    kind = camera_moves.animatic_kind(move_text, default=default)
    return _SPECS.get(kind, _SPECS[PUSH])


# 每种运镜的具体参数。数值都是「看得出来的最小幅度」：
# 推近只到 1.25 倍（再多会显得在硬怼），摇移吃满 1.25 的余量（刚好不多不少）。
_SPECS: dict[str, MoveSpec] = {
    # 推近：从用户认可的构图起步往里推，起手是像素级的
    PUSH: MoveSpec(PUSH, 1.0, 1.25),
    # 拉远：从 1.25 倍退回构图，落点是像素级的
    PULL: MoveSpec(PULL, 1.25, 1.0),
    PAN_RIGHT: MoveSpec(PAN_RIGHT, OVERSCAN, OVERSCAN, 0.0, 1.0),
    PAN_LEFT: MoveSpec(PAN_LEFT, OVERSCAN, OVERSCAN, 1.0, 0.0),
    TILT_DOWN: MoveSpec(TILT_DOWN, OVERSCAN, OVERSCAN, 0.5, 0.5, 0.0, 1.0),
    TILT_UP: MoveSpec(TILT_UP, OVERSCAN, OVERSCAN, 0.5, 0.5, 1.0, 0.0),
    # 环绕：静图做不到真的绕圈，用「缓慢推近 + 轻微横移」近似——
    # 至少能让人看出这一镜是要动的，且方向与被摄主体一致
    ORBIT: MoveSpec(ORBIT, 1.15, 1.3, 0.5, 0.15, 0.55, 0.45),
    STATIC: MoveSpec(STATIC, 1.0, 1.0),
}

MOVE_LABELS: dict[str, str] = {
    PUSH: "推近",
    PULL: "拉远",
    PAN_RIGHT: "右摇",
    PAN_LEFT: "左摇",
    TILT_UP: "上摇",
    TILT_DOWN: "下摇",
    ORBIT: "环绕",
    STATIC: "固定",
}


@dataclass(frozen=True)
class AnimaticClip:
    """一条样片里的一个镜头。"""

    shot_no: str
    label: str
    seconds: int
    move_text: str
    spec: MoveSpec

    @property
    def frames(self) -> int:
        return max(1, round(self.seconds * FPS))

    @property
    def move_label(self) -> str:
        return MOVE_LABELS.get(self.spec.kind, self.spec.kind)


@dataclass
class AnimaticPlan:
    """一次样片渲染的计划：能进的镜头 + 没进的镜头（都要如实说）。"""

    clips: list[AnimaticClip] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    # 这次用的总时长上限：报告里要说清楚「截在哪里」，否则「没收录」就成了没头没尾的一句话
    max_seconds: int = MAX_TOTAL_SECONDS

    @property
    def seconds(self) -> int:
        return sum(c.seconds for c in self.clips)

    @property
    def shot_nos(self) -> list[str]:
        return [c.shot_no for c in self.clips]

    def to_report(self) -> dict[str, Any]:
        """给接口/节点面板看的一份说明（与体检报告的措辞口径一致）。"""
        return {
            "shots": len(self.clips),
            "seconds": self.seconds,
            "skipped": self.skipped,
            "truncated": self.truncated,
            "maxSeconds": self.max_seconds,
        }


def _even(value: float) -> int:
    """取整到偶数：奇数边长会被 libx264 的 yuv420p 拒绝。"""
    n = int(round(value))
    return max(2, n - (n % 2))


def output_size(
    src_w: int, src_h: int, *, ratio: str = DEFAULT_RATIO, long_side: int = DEFAULT_LONG_SIDE
) -> tuple[int, int]:
    """算出成片尺寸。

    - `ratio="source"`：保持原图比例，长边取 min(设定值, 原图长边)——**只缩不放**；
    - 固定比例：长边取设定值，另一边按比例算（源图比例不同则由 ffmpeg 裁切填满）。
    """
    src_w = max(2, int(src_w or 0))
    src_h = max(2, int(src_h or 0))
    if ratio in _RATIO_VALUE:
        rw, rh = _RATIO_VALUE[ratio]
        if rw >= rh:
            w = _even(long_side)
            h = _even(long_side * rh / rw)
        else:
            h = _even(long_side)
            w = _even(long_side * rw / rh)
        return w, h
    # 跟随图片：不放大，避免把 512px 的图糊成 1920
    target = min(int(long_side), max(src_w, src_h))
    if src_w >= src_h:
        return _even(target), _even(target * src_h / src_w)
    return _even(target * src_w / src_h), _even(target)


def _num(value: float) -> str:
    """写进 ffmpeg 表达式的数字：定点，避免 0.30000000000000004 这种。"""
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def kenburns_filter(clip: AnimaticClip, *, src_size: tuple[int, int], out_size: tuple[int, int]) -> str:
    """生成这一镜的滤镜链（纯字符串拼接，可单独测）。

    结构固定三步：
    1. 把源图按「填满」缩放到工作画布，多出来的部分居中裁掉（固定比例时才会真的裁）；
    2. `zoompan` 做缓动：`z` 控制缩放，`x`/`y` 是取景框左上角在**工作画布**里的像素位置；
    3. `format=yuv420p` 统一像素格式，否则和后面的片段合不到一起。
    """
    src_w, src_h = src_size
    out_w, out_h = out_size
    ov = clip.spec.overscan
    work_w = _even(src_w * ov)
    work_h = _even(src_h * ov)
    n = clip.frames
    # 进度：最后一帧正好是 1，不是 (N-1)/N（差这一点点，收尾会显得没走完）
    den = max(n - 1, 1)

    if abs(clip.spec.zoom_to - clip.spec.zoom_from) > 1e-6:
        zoom = f"{_num(clip.spec.zoom_from)}+({_num(clip.spec.zoom_to)}-{_num(clip.spec.zoom_from)})*on/{den}"
    else:
        zoom = _num(clip.spec.zoom_from)

    def axis(prefix_from: float, prefix_to: float, limit: str) -> str:
        if abs(prefix_from - prefix_to) < 1e-6:
            pos = _num(prefix_from)
            return f"({limit})*{pos}"
        return (
            f"({limit})*({_num(prefix_from)}+({_num(prefix_to)}-{_num(prefix_from)})*on/{den})"
        )

    # iw/ih 在 zoompan 里指工作画布；取景框宽 = iw/zoom，可挪范围 = iw - iw/zoom
    x = axis(clip.spec.x_from, clip.spec.x_to, "iw-iw/zoom")
    y = axis(clip.spec.y_from, clip.spec.y_to, "ih-ih/zoom")

    return (
        f"scale={work_w}:{work_h}:force_original_aspect_ratio=increase,"
        f"crop={work_w}:{work_h},setsar=1,"
        f"zoompan=z='{zoom}':x='{x}':y='{y}':d={n}:s={out_w}x{out_h}:fps={FPS},"
        f"format=yuv420p"
    )


def clip_command(
    ffmpeg: str,
    image: Path,
    out: Path,
    clip: AnimaticClip,
    *,
    src_size: tuple[int, int],
    out_size: tuple[int, int],
) -> list[str]:
    """渲染单镜片段的完整命令。"""
    return [
        ffmpeg, "-y",
        "-i", str(image),
        "-vf", kenburns_filter(clip, src_size=src_size, out_size=out_size),
        "-frames:v", str(clip.frames),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-an",
        str(out),
    ]


def plan(
    shots: Iterable[Shot],
    image_of: Mapping[str, Any],
    *,
    default_seconds: int = DEFAULT_SHOT_SECONDS,
    default_move: str = PUSH,
    max_total_seconds: int = MAX_TOTAL_SECONDS,
) -> AnimaticPlan:
    """把「分镜表 + 这一镜的图」排成样片计划。

    `image_of` 是 镜号 → 图（任何能 .get 的东西都行，方便单测传 dict）。
    **镜号对不上就跳过并记下来**，不按顺序硬凑：上游可能被「生成镜数」截断过，
    按顺序配会张冠李戴，让用户看着一版张冠李戴的样片去判断节奏。
    """
    out = AnimaticPlan(max_seconds=max_total_seconds)
    for shot in shots:
        no = str(shot.no or "").strip()
        if not no:
            continue
        if image_of.get(no) is None:
            out.skipped.append({"shot": no, "reason": "这一镜还没有分镜图"})
            continue
        raw = shot.duration_seconds or int(default_seconds or DEFAULT_SHOT_SECONDS)
        seconds = max(MIN_SHOT_SECONDS, min(MAX_SHOT_SECONDS, int(raw)))
        if out.seconds + seconds > max_total_seconds:
            out.truncated.append(no)
            continue
        out.clips.append(
            AnimaticClip(
                shot_no=no,
                label=shot.label,
                seconds=seconds,
                move_text=(shot.move or "").strip(),
                spec=move_spec(shot.move, default=default_move),
            )
        )
    return out


def summarize(plan_: AnimaticPlan) -> str:
    """写进产物 `prompt` 的一句话来路说明（资产库里能看到，方便日后回头查）。"""
    if not plan_.clips:
        return "静图样片：没有可用的镜头"
    bits = [f"{c.shot_no}（{c.move_label}·{c.seconds}s）" for c in plan_.clips]
    text = f"静图样片 · {len(plan_.clips)} 镜 / {plan_.seconds}s：" + "、".join(bits)
    if plan_.skipped:
        text += "；跳过：" + "、".join(f"{s['shot']}（{s['reason']}）" for s in plan_.skipped)
    if plan_.truncated:
        text += "；超出时长上限未收录：" + "、".join(plan_.truncated)
    return text[:2000]


def sanitize_ratio(value: str) -> str:
    v = (value or "").strip()
    return v if v in RATIO_CHOICES else DEFAULT_RATIO


def sanitize_long_side(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LONG_SIDE
    return n if n in LONG_SIDE_CHOICES else DEFAULT_LONG_SIDE


def sanitize_seconds(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SHOT_SECONDS
    return max(MIN_SHOT_SECONDS, min(MAX_SHOT_SECONDS, n))


def sanitize_default_move(value: str) -> str:
    return STATIC if (value or "").strip() == STATIC else PUSH
