"""转场与转场音效的纯逻辑：预设、校验、时长对账、音效配方。

为什么单独立一层：判据与文案要和 ffmpeg 的滤镜图分开，这样「转场时长能不能放下」
「成片会短多少」这些**用户能看见的结论**可以直接断言，不必真跑一次编码去测。

四条口径，改之前先读：

1. **转场会让成片变短**。`xfade` 是把相邻两段**交叠**起来，不是插一段新的：三段各 5 秒、
   转场 1 秒，成片是 13 秒而不是 15 秒。这件事必须**在合并之前就说出来**（前端显示
   「成片约 13 秒，比硬切短 2 秒」），而不是等用户拿到片子发现短了再去猜。
2. **转场必须放得下**。`xfade` 要求转场时长短于相邻两段的时长，否则那一段会被吃掉或
   直接失败。所以宁可在这里拦住并说清「第 2 段只有 0.8 秒，比转场还短」，也不要让
   ffmpeg 报一句 `Invalid duration`。
3. **硬切是默认，且不加任何滤镜**。老行为（流拷贝拼接）是最快的，绝大多数素材也不需要
   转场；把它做成 `cut` 这一档，而不是「默认叠化」，免得让所有人的老流程悄悄变慢。
4. **音效是我们现场合成的，不是下载来的素材**。理由见 `SFX_RECIPES` 的注释：
   零授权风险、零下载、可复现。想换成自己的素材就走「选一条资产库音频」那条路
   （接口上就是 `sfx_asset_id`），那条路同样是通的。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 转场预设

# `xfade` 是要传给 ffmpeg 的 transition 名；空串 = 不加滤镜（硬切）
PRESETS: tuple[dict[str, str], ...] = (
    {"key": "cut", "label": "硬切", "xfade": "", "hint": "直接接上，不加过渡。最快，也最不挑素材"},
    {"key": "dissolve", "label": "叠化", "xfade": "dissolve", "hint": "两段交叠淡入淡出，最通用的一种"},
    {"key": "fade", "label": "过黑", "xfade": "fadeblack", "hint": "先淡到黑再淡出来，段落感强"},
    {"key": "flash", "label": "白闪", "xfade": "fadewhite", "hint": "一道白光带过，适合卡点"},
    {"key": "wipe", "label": "横划", "xfade": "wipeleft", "hint": "画面被推走，节奏偏快"},
    {"key": "circle", "label": "圆形展开", "xfade": "circleopen", "hint": "从画面中心圈开，适合开场"},
)

# 转场时长：0.2 秒以下看不出过渡、2 秒以上像卡住，两端都夹住而不是报错
MIN_SECONDS = 0.2
MAX_SECONDS = 2.0
DEFAULT_SECONDS = 0.5

# 音效尾巴：转场结束后再响一会儿才自然（否则「呼」的一声会跟着转场一起被切掉）
SFX_TAIL = 0.35

# 音效配方：`kind` → (ffmpeg lavfi 输入, 额外滤镜)。
# 全部是**现场合成**的：不下载任何素材，也就没有授权问题、没有仓库体积问题，
# 换台机器出来的还是同一段声音。要真接 CC0 素材库时，走 `sfx_asset_id` 那条路。
SFX_RECIPES: dict[str, tuple[str, str]] = {
    # 粉噪声 + 带通 + 两端淡：一声短促的气流
    "whoosh": (
        "anoisesrc=color=pink:amplitude=0.6:sample_rate=48000",
        "highpass=f=320,lowpass=f=4200,afade=t=in:st=0:d=0.10",
    ),
    # 频率上扫的正弦：把情绪推起来。相位项写成 (f0+k*t)*t，瞬时频率就是线性上升的
    "riser": (
        "aevalsrc=exprs='0.30*sin(2*PI*(180+520*t)*t)':s=48000",
        "afade=t=in:st=0:d=0.15",
    ),
    # 低频一击 + 指数衰减：撞击感。短促，所以不吃满整段时长
    "impact": (
        "aevalsrc=exprs='0.65*sin(2*PI*58*t)*exp(-7*t)':s=48000",
        "",
    ),
    # 很轻的一声高频点，用来打节奏
    "tick": (
        "aevalsrc=exprs='0.40*sin(2*PI*1900*t)*exp(-45*t)':s=48000",
        "",
    ),
}

SFX_PRESETS: tuple[dict[str, str], ...] = (
    {"key": "", "label": "不加音效", "hint": "只做画面过渡"},
    {"key": "whoosh", "label": "呼啸 whoosh", "hint": "一声短促的气流，最百搭"},
    {"key": "riser", "label": "上扬 riser", "hint": "音调往上走，把情绪推起来"},
    {"key": "impact", "label": "闷响 impact", "hint": "低频一击，配硬切或白闪"},
    {"key": "tick", "label": "轻点 tick", "hint": "很轻的一声，打节奏用"},
)


def sanitize_key(raw: object) -> str:
    """认不出来的转场就当硬切（老行为），不报错——老画布 / 分享码里可能没有这个字段。"""
    key = str(raw or "").strip()
    return key if any(p["key"] == key for p in PRESETS) else "cut"


def preset(key: object) -> dict[str, str]:
    want = sanitize_key(key)
    return next(p for p in PRESETS if p["key"] == want)


def is_cut(key: object) -> bool:
    return not preset(key)["xfade"]


def sanitize_seconds(raw: object) -> float:
    """转场时长：读不出来用默认，超范围夹到边界（改一下就好，不必报错）。"""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_SECONDS
    if value != value:  # NaN
        return DEFAULT_SECONDS
    return max(MIN_SECONDS, min(MAX_SECONDS, round(value, 2)))


def sanitize_sfx(raw: object) -> str:
    key = str(raw or "").strip()
    return key if key in SFX_RECIPES else ""


def check_clips(durations: list[float | None], key: object, seconds: float) -> str:
    """这批片段能不能这样接。返回空串 = 可以；否则是一句可直接展示的理由。

    **读不出时长的片段直接拦住**：转场位置是按每段时长算出来的（`offset`），
    算不出来还硬接，画面会对不上而 ffmpeg 不一定会报错——那是最坏的一种结果。
    """
    if is_cut(key):
        return ""
    if len(durations) < 2:
        return ""
    for i, d in enumerate(durations):
        if not d or d <= 0:
            return (
                f"读不出第 {i + 1} 段的时长，没法算转场位置。"
                "这一段可能是还没下载完或编码不完整——先确认它能正常播放再合并"
            )
    shortest = min(d for d in durations if d)
    if shortest <= seconds:
        idx = durations.index(shortest) + 1
        return (
            f"转场要 {seconds:g} 秒，而第 {idx} 段一共只有 {shortest:.1f} 秒——放不下。"
            "把转场调短一点（至少要短于最短那一段），或者把那一段剪长一些"
        )
    return ""


def total_seconds(durations: list[float], key: object, seconds: float) -> float:
    """成片总时长。转场是把相邻两段**交叠**，所以每接一次就少一个转场时长。"""
    if not durations:
        return 0.0
    total = sum(durations)
    if is_cut(key):
        return total
    return max(0.0, total - seconds * (len(durations) - 1))


def shortfall(durations: list[float], key: object, seconds: float) -> float:
    """比硬切短多少秒（0 = 一样长）。前端就是拿它说「会变短」。"""
    if is_cut(key) or len(durations) < 2:
        return 0.0
    return seconds * (len(durations) - 1)


def sfx_seconds(transition_seconds: float) -> float:
    """音效该合成多长：比转场长一个尾巴，让它响在转场之后而不是被一起切掉。"""
    return round(min(MAX_SECONDS + SFX_TAIL, transition_seconds + SFX_TAIL), 3)
