"""配音（语音合成）的纯逻辑：文本校验、音色处理、旁白拼装、报告措辞。

为什么单独立一层：这一版要同时服务两个入口——独立的「配音」页（试听一段文本）
和「分镜图」节点的样片旁白（把整份分镜表的台词读成一条音轨）。两个入口对
「什么算合法文本」「台词为空怎么办」的口径必须完全一致，否则用户会遇到
「页面上能生成、样片里说没有台词」这种最难解释的不一致（与分镜体检、样片共用一个
`parse_storyboard` 是同一个道理）。

三条口径写在这里，改之前先读：

1. **长度上限 2000 字**：不是不能更长，而是「一段话」的上限。更长的内容应当分段
   （分镜表天然就是一镜一段），一次性塞几千字进 TTS，失败时的错误信息也很难看。
2. **音色不做白名单**：各家音色名不通用（`alloy` / `zh-CN-XiaoxiaoNeural` / 自建音色 id），
   写死白名单等于逼用户等我们发版。只做「去掉首尾空白 + 拒绝空白音色」。
3. **台词里的「（无）」不是台词**：分镜表里没台词的镜头通常写成 `（无）` / `无` / `—`。
   把它们当台词读出来，旁白里就会出现一串「无无无」——这些占位要滤掉。
"""

from __future__ import annotations

import re
from typing import Iterable

# 一段文本的上限（按字符数，中文一个字算一个）
MAX_CHARS = 2000
MIN_CHARS = 1

# 语速夹取范围。各家允许的范围不一样（OpenAI 是 0.25–4.0），这里取一个
# 「听起来还像人」的窄区间：超出这个范围的取值要么是误填，要么本来就该在
# 上游参数里调，而不是在配音这一步。
SPEED_MIN = 0.5
SPEED_MAX = 2.0
SPEED_DEFAULT = 1.0

# 常见音色：只作为「快捷选项」列在界面上，不做校验。
# 用 OpenAI 命名的那一批（兼容面最广），用户也可以直接手填别家的音色 id。
VOICE_PRESETS: tuple[dict[str, str], ...] = (
    {"id": "alloy", "label": "alloy（中性）"},
    {"id": "echo", "label": "echo（男声）"},
    {"id": "fable", "label": "fable（叙述）"},
    {"id": "nova", "label": "nova（女声）"},
    {"id": "onyx", "label": "onyx（低沉男声）"},
    {"id": "shimmer", "label": "shimmer（明亮女声）"},
)

# 分镜表里表示「这一镜没台词」的写法。台账里见过这些，都当没有。
_EMPTY_LINE = re.compile(
    r"^(?:[（(【\[]?\s*(?:无|没有|无台词|无对白|无旁白|静音|none|n/?a|-{1,2}|—+|…+|。+|\.+)\s*[)）】\]]?)$",
    re.I,
)
# 台词前常带的说话人标签：「小焰：」「小焰:」「【小焰】」。只认短标签（≤12 字）。
_SPEAKER_PREFIX = re.compile(
    r"^\s*(?:"
    r"[【\[][\u4e00-\u9fa5A-Za-z0-9_ ]{1,12}[】\]]"      # 【小焰】你终于来了
    r"|[\u4e00-\u9fa5A-Za-z0-9_ ]{1,12}[：:]"            # 小焰：你终于来了
    r")\s*"
)
# 句末标点：拼旁白时用来判断要不要补一个句号
_END_PUNCT = "。！？!?…；;：:，,、."


def sanitize_text(raw: object) -> str:
    """整理待合成的文本：去首尾空白、把连续空行压成一个换行。

    **不改写内容**（不去标点、不删语气词）：用户写什么就念什么。
    只有一件事要做——去掉首尾空白，因为上游收到带空行的文本会念出一段空白。
    """
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if not line.strip() and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


def text_error(text: str) -> str:
    """文本不合规时的说明；合规返回空串（便于调用方直接 `if text_error(...)`）。"""
    n = len(text)
    if n < MIN_CHARS:
        return "请先写下要念的内容"
    if n > MAX_CHARS:
        return f"这一段有 {n} 字，超过单次上限 {MAX_CHARS} 字。请分段生成（每段单独存成一条音频）"
    return ""


def sanitize_voice(raw: object) -> str:
    """音色：只去首尾空白、去掉控制字符、限长。**留空是合法的**（用服务默认音色）。"""
    voice = re.sub(r"[\x00-\x1f\x7f]", "", str(raw or "")).strip()
    return voice[:80]


def sanitize_speed(raw: object) -> float:
    """语速：读不出来就用默认；超出范围夹到边界（夹取而不是报错，用户改一下就好）。"""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return SPEED_DEFAULT
    if value != value:  # NaN
        return SPEED_DEFAULT
    return max(SPEED_MIN, min(SPEED_MAX, round(value, 2)))


def is_speakable(line: str) -> bool:
    """这一行是不是真的台词（滤掉「（无）」这类占位）。"""
    stripped = line.strip()
    if not stripped:
        return False
    return not _EMPTY_LINE.match(stripped)


def strip_speaker(line: str) -> str:
    """去掉「小焰：」这样的说话人标签——念出来不该带名字。

    只剥一层、且只在标签是短串（≤12 字）时才剥，避免把正文里的长句误伤。
    """
    return _SPEAKER_PREFIX.sub("", line.strip(), count=1)


def speaker_of(line: str) -> str:
    """这一行是谁说的。认不出说话人时返回空串。

    **与 `strip_speaker` 共用同一个正则**：它俩必须对同一批写法给出同一个答案。
    各写一个正则迟早会漂移，而漂移的表现是「标签剥掉了、说话人却没认出来」——
    于是配音悄悄用了默认音色，用户只会觉得「我明明给这个角色配了音色，怎么没生效」。

    只认行首那个短标签（「小焰：」「【小焰】」），与剥标签的口径完全一致。
    """
    match = _SPEAKER_PREFIX.match(str(line or "").strip())
    if not match:
        return ""
    # 剥掉包裹符号与冒号，标签内部的空格留着（「Xiao Yan:」是个人名）
    return match.group(0).strip().strip("【[]】:： \t").strip()


def _join_lines(lines: list[str]) -> str:
    """把各条台词接成一段：只在上一条**没有句末标点**时才补一个句号。

    一律用「。」去 join 会在「……还没来。」后面再补一个，念出来是一段多余的停顿；
    一律不补又会让「他在门口站住」和「她回过头」粘成一句。
    """
    out = ""
    for line in lines:
        if out and out[-1] not in _END_PUNCT:
            out += "。"
        out += line
    return out


def _speakable_lines(dialogue: object) -> list[str]:
    """把「台词」那一栏切成真正要念的几句（**原文，未剥标签**）。

    整段旁白（`narration_from_shots`）与逐镜对白（`dialogue_parts`）都从这里走，
    免得两个入口对「哪些行算台词」各有一套说法。
    """
    return [p for p in str(dialogue or "").split("\n") if is_speakable(p)]


def narration_from_shots(shots: Iterable[object]) -> tuple[str, int]:
    """把分镜表的台词拼成一段旁白，返回 (文本, 台词条数)。

    只取台词，**不拿画面描述凑数**：画面描述是给生图模型的提示词
    （「中景，暮光闪闪站在花园中央，缓缓抬头」），念出来是制作说明而不是旁白。
    一条台词都没有时返回空文本，由调用方给出「先去补台词」的引导——
    悄悄拿画面描述顶上，用户会听到一版莫名其妙的解说词。
    """
    lines: list[str] = []
    for shot in shots:
        for part in _speakable_lines(getattr(shot, "dialogue", "")):
            text = strip_speaker(part)
            if text:
                lines.append(text)
    return _join_lines(lines), len(lines)


def dialogue_parts(shot: object) -> tuple[str, str]:
    """这一镜的台词与它的说话人，返回 `(要念的文本, 说话人)`；没有台词返回 `("", "")`。

    说话人取**第一条**可念台词上的标签。一镜里换好几个人说话是另一件事
    （那要按句切分、每句一条音轨），不是这一版的范围——这一版一镜一条配音。
    """
    parts = _speakable_lines(getattr(shot, "dialogue", ""))
    speaker = speaker_of(parts[0]) if parts else ""
    lines = [t for t in (strip_speaker(p) for p in parts) if t]
    return _join_lines(lines), speaker


def asset_name(text: str) -> str:
    """给资产库起一个能认出来的名字：取前 16 个字，换行压成空格。"""
    head = " ".join(text.split())[:16]
    return f"配音 · {head}" if head else "配音"


def truncation_note(*, audio_seconds: float | None, video_seconds: int | None) -> str:
    """旁白比画面长时的说明（短了不用说什么，画面先结束是正常观感）。"""
    if not audio_seconds or not video_seconds:
        return ""
    if audio_seconds <= video_seconds + 0.5:
        return ""
    over = int(round(audio_seconds - video_seconds))
    return (
        f"旁白 {int(round(audio_seconds))} 秒，比画面长 {over} 秒，末尾会被截掉。"
        "想让旁白读得完，可以把「兜底时长」调大，或者精简台词"
    )


def duration_seconds(raw_seconds: object) -> int | None:
    """把探测到的秒数转成 `Asset.duration` 用的整数秒（读不出来返回 None）。"""
    try:
        value = float(raw_seconds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return max(1, int(round(value)))
