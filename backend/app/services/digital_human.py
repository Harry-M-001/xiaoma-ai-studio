"""数字人（对白 / 口播）的纯逻辑：一条配音挂到一次视频生成上，什么情况下不能发。

这一版只做**「一张图 + 一条配音 → 一段会说话的视频」**（口播镜头），也是数字人三块
里最基础的一块。逐镜台词自动配音留给下一版——先把「一条能真跑通」验出来，
比铺开一大堆没验证的自动化划算（`路线图.md` 里「动手前先实测 provider 能力」
说的就是这件事）。

三条口径，改之前先读：

1. **配音比所选时长长时，直接拦住，不自动改时长。**
   自动把 5 秒改成 10 秒看着贴心，实际是**在用户没点头的情况下多花钱**，
   而「这一笔花了多少」恰恰是这个功能最不该含糊的地方。拦住并给出可照做的建议
   （把时长调大 / 把台词拆到下一镜），下拉框就在旁边，改一下是一秒钟的事。
2. **留 0.05 秒的容差**：配音探测出来的 4.02 秒不该逼用户去选 5 秒档——
   结尾那零点零几秒是编码器的尾巴，不是台词。反过来 4.6 秒就得当 5 秒算。
3. **量不出音频时长时不装懂**：`probe` 读不出来只返回 None，那就按用户选的时长走
   （并在任务说明里写明「时长没量出来」），不替用户猜一个数。
"""

from __future__ import annotations

import math

# 配音结尾那零点零几秒是编码器的尾巴，不是台词——比它短就当作「读得完」
TAIL_TOLERANCE = 0.05

# 我们自己的时长档位能到 15 秒（种子里的那个档默认关着，但用户可以打开）。
# 建议里说「把时长调到 ≥N 秒」时要先确认 N 是调得出来的，否则就是让用户白找一圈。
MAX_SUGGESTED_SECONDS = 15


class DigitalHumanError(ValueError):
    """对白这一路发不出去的原因（**消息可直接展示给用户**）。"""


def dialogue_seconds(audio_seconds: float | None) -> int | None:
    """这条配音实际需要几秒的**整秒**；量不出来返回 None。

    「需要几秒」是向上取整：4.6 秒的台词，视频得有 5 秒才读得完。
    """
    if audio_seconds is None:
        return None
    try:
        value = float(audio_seconds)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return max(1, math.ceil(value - TAIL_TOLERANCE))


def check_duration(*, requested: int, audio_seconds: float | None, name: str) -> str:
    """这个时长够不够读完整句。够 → 空串；不够 → 一句可照做的拒绝理由。

    **不在这里改时长**：改时长就是改钱，必须由用户自己点那一下（见文件头第 1 条）。
    """
    need = dialogue_seconds(audio_seconds)
    if need is None:
        return ""
    if need <= int(requested or 0):
        return ""
    where = f"「{name}」" if name else "这条配音"
    tail = (
        f"请把视频时长调到 {need} 秒以上"
        if need <= MAX_SUGGESTED_SECONDS
        else f"配音有 {need} 秒，已经超过单段视频能给的时长上限（{MAX_SUGGESTED_SECONDS} 秒）"
    )
    return (
        f"{where}有 {_pretty(audio_seconds)} 秒，比所选时长 {requested} 秒长——"
        f"视频会在第 {requested} 秒结束，这句话读不完。{tail}，"
        "或者把这段台词拆到下一镜（多一镜比听半句话强）"
    )


def attach_note(*, name: str, seconds: float | None) -> str:
    """写进任务说明/日志的那一句：这一段带了哪条配音、多长。"""
    label = name or "未命名配音"
    if seconds is None:
        # 量不出时长要说出来：不然「时长对不对」这件事事后无从查起
        return f"对白音轨：{label}（时长没量出来）"
    return f"对白音轨：{label}（{_pretty(seconds)} 秒）"


def _pretty(seconds: float | None) -> str:
    """秒数显示成一位小数，别给用户看 4.620000000000001。"""
    try:
        return f"{float(seconds):.1f}"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"
