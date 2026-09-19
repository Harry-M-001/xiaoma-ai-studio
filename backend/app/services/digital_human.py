"""数字人（对白 / 口播）的纯逻辑：一条配音挂到一次视频生成上，什么情况下不能发。

两块都在这里：**「一张图 + 一条配音 → 一段会说话的视频」**（口播镜头，视频页/画布
视频节点上手工挂一条），以及**逐镜台词自动配音**（勾上「逐镜对白」后，每一镜的台词
各自合成一条配音、按说话人映射到音色、附给这一镜的视频）。

逐镜这块是本文件里最需要小心的部分，它把「一次点击」变成了「N 次付费调用」，
所以三条口径里最要紧的是第 1 条——**任何一镜配不上就不派这一镜**，
而不是硬发一段没声音的视频出去。这不是完美主义：出片之后钱已经花了，
用户拿到一段没声音的片子只会以为功能坏了。

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


def check_duration(
    *, requested: int, audio_seconds: float | None, name: str, duration_hint: str = "视频时长"
) -> str:
    """这个时长够不够读完整句。够 → 空串；不够 → 一句可照做的拒绝理由。

    **不在这里改时长**：改时长就是改钱，必须由用户自己点那一下（见文件头第 1 条）。

    `duration_hint` 是指「时长」这个东西在用户那儿叫什么：视频页上叫「视频时长」，
    逐镜出片时它来自分镜表，叫「分镜表里这一镜的时长」——建议必须指向他真正能改的那个地方。
    """
    need = dialogue_seconds(audio_seconds)
    if need is None:
        return ""
    if need <= int(requested or 0):
        return ""
    where = f"「{name}」" if name else "这条配音"
    tail = (
        f"请把{duration_hint}调到 {need} 秒以上"
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


# 「角色=音色」小表里允许的分隔符。中文冒号必须认：国内用户十有八九会打「小焰：nova」，
# 而认不出来时的表现是「静默用了默认音色」，用户只会觉得这个功能坏了。
_VOICE_SEP = ("=", "：", ":")


def parse_voice_map(raw: object) -> tuple[dict[str, str], list[str]]:
    """解析节点上那张「角色=音色」小表，返回 `(映射, 认不出的行)`。

    格式：每行一条 `角色=音色`（`=` / `：` / `:` 都认）；空行与 `#` 开头的注释行跳过。

    **认不出的行要交回调用方**（由它报错），不许悄悄丢掉：用户写了「小焰:nova」这种
    我们能认，写了「小焰 nova」这种我们认不出——后者静默不生效的话，他会以为音色没生效
    是产品的锅。而这一条错会跟着一次**付费**的配音一起发生。
    """
    mapping: dict[str, str] = {}
    bad: list[str] = []
    for raw_line in str(raw or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        role = voice = ""
        for sep in _VOICE_SEP:
            if sep in line:
                role, voice = (p.strip() for p in line.split(sep, 1))
                break
        # 角色名要短（和说话人标签同一个尺度），音色随便填（各家命名不通用，不做白名单）
        if not role or not voice or len(role) > 12:
            bad.append(line)
            continue
        mapping[role] = voice
    return mapping, bad


def voice_for(speaker: str, mapping: dict[str, str], default: str) -> str:
    """这一句该用哪个音色：说话人有配就用配的，认不出说话人或没配就用默认。

    认不出说话人（台词没写「小焰：」）时**不猜**——用默认音色，并在报告里说清楚
    「这一镜没认出说话人」。猜一个角色出来，用户会听到别人嗓子说话。
    """
    name = str(speaker or "").strip()
    if name and name in mapping:
        return mapping[name]
    return str(default or "")


def skip_reason(*, shot_no: str, audio_seconds: float | None, shot_seconds: int) -> str:
    """逐镜对白里「这一镜为什么没配上」的那一小句（列进日志与报告）。

    写成一小句而不是复用 `check_duration` 的长句子：它会被拼进「N 镜没配上（…）」里，
    逐条列出来时太长会看不清是哪儿的问题。该给的两条路一样给。
    """
    need = dialogue_seconds(audio_seconds)
    return (
        f"镜头{shot_no} 的台词要 {need} 秒、这一镜只有 {shot_seconds} 秒"
        "（把台词改短，或把分镜表里这一镜的时长写长）"
    )


def dialogue_summary(*, voiced: int, skipped: list[str]) -> str:
    """逐镜对白做完之后那一段人话（写进日志/报告）。

    「没配上的」那几镜**必须逐条列出原因**：用户看到成片少了一段，最想知道的就是为什么。
    """
    if not skipped:
        return f"逐镜对白：{voiced} 镜配上了对白"
    return f"逐镜对白：{voiced} 镜配上了对白；{len(skipped)} 镜没配上（" + "；".join(skipped) + "）"


def _pretty(seconds: float | None) -> str:
    """秒数显示成一位小数，别给用户看 4.620000000000001。"""
    try:
        return f"{float(seconds):.1f}"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"
