"""分镜静态体检：生成前**零成本**的镜头语言检查。

为什么要有这一层：逐镜出图 + 逐镜出视频是真金白银，而分镜表里的毛病（十镜十个推镜、
景别全挤在中景、通篇「仿佛 / 宛如 / 顿时」）**在这个阶段就能看出来**。规则全部本地、
不调模型、不花钱，所以可以随手跑，跑一百遍也不心疼。

与 `preflight` 的分工（两者都只告警不阻断，但答的问题不一样）：

- `preflight` 答「**这样能不能跑**」——参数各自都合法、但组合起来大概率不是你要的
  （尺寸超出模型上限、首帧资产不存在…）。放在提交生成的那一刻。
- 本模块答「**拍出来会不会难看**」——技术上完全跑得通，但镜头语言重复、景别单调、
  文本有 AI 腔。放在分镜定稿、还没开始烧钱的时候。

阈值都写在下面的常量里，每个都注了为什么是这个数。**宁可少报也不要误报**：
一份只会喊「狼来了」的体检报告，用户看两次就不看了。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from . import camera_moves, storyboard_sheet
from .storyboard_sheet import Shot

# ---------------------------------------------------------------- 阈值

# 少于 5 个镜头时，「某个景别占比 60%」这种话没有意义（3 镜里 2 个就是 67%）
MIN_SHOTS_FOR_DISTRIBUTION = 5
# 同一个景别超过六成 → 观众会觉得「一直是一个距离」
SIZE_DOMINANT_RATIO = 0.6
# 同一个运镜超过六成 → 「怎么一直在推」
#
# 定 0.6 而不是 0.5 是踩出来的：最初写 0.5，结果一份完全正常的六镜分镜
# （固定 / 推近 / 固定 / 横移 / 跟拍 / 固定，固定占 50%）被判成「运镜雷同」。
# 固定镜头在一场对话戏里占一半是常规拍法，不是毛病。
MOVE_DOMINANT_RATIO = 0.6
# 连续 3 镜同一运镜就够明显了（两次可能是巧合）
MOVE_RUN_LEN = 3
# 只用过 1 种景别 = 单调；只用到 2 种且镜头不少 = 偏窄
SIZE_KINDS_NARROW = 2
# 首帧提示词短于这个长度，生图模型拿不到足够的画面信息
SHORT_FIRST_FRAME = 10
# 相邻两镜的「画面」前 N 个字相同 → 基本可以断定是复制粘贴没改
SIMILAR_PREFIX = 12
# 套话词命中的镜头占比超过这个数就算「通篇 AI 腔」
SLOP_SHOT_RATIO = 0.25
# 单镜里出现这么多套话词，即使占比不高也值得点出来
SLOP_PER_SHOT = 2
# 镜头数到这么多却只有一个场景 → 提示可能漏了场景切换
SINGLE_SCENE_MIN_SHOTS = 8

# 「AI 腔」词表：这些词本身不是错，但成片出现频率高就会显得是机器写的。
# 选词标准：① 中文写作里被反复点名的那种套话；② 换掉之后意思不变（可替代）。
SLOP_TERMS = (
    "仿佛", "宛如", "犹如", "好似", "像是", "似乎",
    "不禁", "不由得", "顿时", "瞬间", "刹那间",
    "空气中弥漫", "弥漫着", "交织", "诉说", "见证",
    "令人", "让人不禁", "无声地诉说", "画面定格", "时间仿佛",
)

# 景别：从远到近，用来判断「跨度」。分镜表里的写法五花八门，先归一化。
_SIZE_ORDER = ("大远景", "远景", "全景", "中景", "中近景", "近景", "特写", "大特写")
_SIZE_ALIAS = {
    "极远景": "大远景",
    "wide": "远景",
    "full": "全景",
    "medium": "中景",
    "close": "特写",
    "medium close up": "近景",
    "mcu": "近景",
    "ecu": "大特写",
    "cu": "特写",
}
# 运镜的判词全部搬去 `services/camera_moves.py`（**一份表四处共用**：分镜提示词 /
# 静图样片 / 这里的体检 / 体检的建议文案）。这里原来有一份 `_MOVE_HINTS`，
# 而 `animatic` 另有排过序的一份、提示词里还手写了第三份——三份必然漂移。


@dataclass(frozen=True)
class Finding:
    """一条体检结论。字段形状与生成前的 preflight 告警一致，前端可复用同一个组件画。"""

    code: str
    level: str  # warn=大概率不是你想要的；info=只是提醒
    message: str
    suggestion: str
    shots: tuple[str, ...] = field(default_factory=tuple)

    def as_warning(self, label: str = "") -> dict[str, str]:
        prefix = f"「{label}」" if label else ""
        return {
            "code": self.code,
            "level": self.level,
            "message": prefix + self.message,
            "suggestion": self.suggestion,
        }


def normalize_size(raw: str) -> str:
    """把景别写法归一化：`近景（含胸）` / `MCU` / `Medium Close Up` → `近景`。"""
    text = (raw or "").strip().lower()
    if not text:
        return ""
    for alias, canon in _SIZE_ALIAS.items():
        if alias in text:
            return canon
    for size in _SIZE_ORDER:
        if size in text:
            return size
    return ""


def _shot_names(shots: list[Shot]) -> str:
    """把镜号拼成「镜头3、镜头5、镜头7」；太多就只说前几个。"""
    no = [f"镜头{s.no}" for s in shots if s.no]
    if not no:
        return ""
    if len(no) <= 5:
        return "、".join(no)
    return "、".join(no[:5]) + f" 等 {len(no)} 个镜头"


def _has_move_hint(raw: str) -> bool:
    """写了运镜没有。

    **判据只有一份**（`camera_moves.match`）：以前这里自己列了一份 `_MOVE_HINTS`，
    而 `animatic` 另有一份排过序的表、提示词里还手写着第三份——三份表必然漂移，
    表现就是「体检说写了运镜、样片却认不出所以套了个推近」。
    """
    return camera_moves.match(raw) is not None


def _is_static_move(raw: str) -> bool:
    """固定/静止镜头。

    单独拎出来是因为它**不是毛病**：一场对话戏连着三个固定镜头是完全正常的拍法，
    「连续三镜同一运镜」这条规则对固定镜头不成立，只对「一直在推/一直在摇」成立。
    """
    return camera_moves.is_static(raw)


# ---------------------------------------------------------------- 规则

def _rule_missing_fields(shots: list[Shot]) -> list[Finding]:
    """缺字段：缺景别 / 缺运镜 / 缺时长 / 缺首帧提示词。

    这几项直接决定下游拿不拿得到镜头语言：缺景别与运镜，生视频的提示词里就没有
    「镜头怎么动」；缺首帧提示词，生图会退回中文画面描述（能用，但风格一致性会差）。
    """
    out: list[Finding] = []
    if not shots:
        return out

    no_size = [s for s in shots if not normalize_size(s.size)]
    if no_size:
        out.append(
            Finding(
                code="missing_size",
                level="warn",
                message=f"{len(no_size)}/{len(shots)} 个镜头没写景别（{_shot_names(no_size)}）。"
                "生视频的提示词里就不会有「镜头离多远」，成片容易一直是一个距离。",
                suggestion="在镜头标题里补上景别：`### 镜头3 | 中景 | 缓慢推近 | 4s`。",
                shots=tuple(s.no for s in no_size),
            )
        )

    no_move = [s for s in shots if not (s.move or "").strip()]
    if no_move:
        out.append(
            Finding(
                code="missing_move",
                level="warn",
                message=f"{len(no_move)}/{len(shots)} 个镜头没写运镜（{_shot_names(no_move)}）。",
                # 建议从词表生成，**不再手写一份**——手写的那份必然与表脱节
                suggestion=(
                    "在这一栏补上运镜，可选：" + "、".join(camera_moves.labels())
                    + "。不写的话镜头不会动。"
                ),
                shots=tuple(s.no for s in no_move),
            )
        )

    no_dur = [s for s in shots if s.duration_seconds <= 0]
    if no_dur:
        out.append(
            Finding(
                code="missing_duration",
                level="info",
                message=f"{len(no_dur)}/{len(shots)} 个镜头没写时长（{_shot_names(no_dur)}），"
                "会全部退回节点设置里的默认秒数。",
                suggestion="时长写成「4s」或「约 4 秒」都能认；整片节奏靠它。",
                shots=tuple(s.no for s in no_dur),
            )
        )

    no_ff = [s for s in shots if not (s.first_frame or "").strip()]
    if no_ff:
        out.append(
            Finding(
                code="first_frame_missing",
                level="warn",
                message=f"{len(no_ff)}/{len(shots)} 个镜头没有首帧提示词（{_shot_names(no_ff)}），"
                "出图只能用中文画面描述兜底，跨镜风格容易散。",
                suggestion="给每镜补一行 `- 首帧提示词：`（英文、写清主体/景别/光线/风格）。",
                shots=tuple(s.no for s in no_ff),
            )
        )
    return out


def _rule_size_distribution(shots: list[Shot]) -> list[Finding]:
    """景别分布：全是一种 / 一种占六成以上 / 只用到两三种。"""
    sized = [(s, normalize_size(s.size)) for s in shots]
    sized = [(s, size) for s, size in sized if size]
    if len(sized) < MIN_SHOTS_FOR_DISTRIBUTION:
        return []

    counter = Counter(size for _, size in sized)
    kinds = len(counter)
    top_size, top_n = counter.most_common(1)[0]
    out: list[Finding] = []

    if kinds == 1:
        out.append(
            Finding(
                code="size_single_kind",
                level="warn",
                message=f"全部 {len(sized)} 个镜头都是「{top_size}」。这不是风格统一，是没分镜。",
                suggestion="交代环境用远景/全景，对话用中景，情绪拐点用近景/特写——先拉开差距。",
                shots=tuple(s.no for s, _ in sized),
            )
        )
    elif top_n / len(sized) >= SIZE_DOMINANT_RATIO:
        hits = [s for s, size in sized if size == top_size]
        out.append(
            Finding(
                code="size_dominant",
                level="warn",
                message=f"「{top_size}」占了 {top_n}/{len(sized)}（{round(top_n / len(sized) * 100)}%），"
                f"整片景别几乎没变化（{_shot_names(hits)}）。",
                suggestion="把其中一部分改成别的距离：情绪抬起来时切近景，换场时用远景。",
                shots=tuple(s.no for s in hits),
            )
        )
    elif kinds <= SIZE_KINDS_NARROW:
        out.append(
            Finding(
                code="size_span_narrow",
                level="info",
                message=f"只用到 {kinds} 种景别（{'、'.join(counter)}），镜头之间的距离感偏窄。",
                suggestion="至少让远景/全景与近景/特写各出现几次。",
            )
        )
    return out


def _rule_unknown_move(shots: list[Shot]) -> list[Finding]:
    """写了运镜，但不在词表里。

    这是**加了运镜词表之后才有的一条规则**，也是这一项对用户最直接的好处：

    以前「运镜」这一栏随便写什么都行，而静图样片认不出时会**静默套一个推近**——
    样片动了，但动的不是分镜表写的意思，而界面上完全看不出来。现在把认不出来的
    挑出来说清，并给一个最接近的建议。

    两种情形分开说，因为**用户要改的地方不一样**：

    - 写的是「主观镜头」这类**机位/镜头类型**：那是镜头类型不是运动方式，
      该挪到画面描述里，运镜栏另选一个运动方式。
    - 写的是没见过的说法（「镜头缓缓飘过」）：从词表里挑一个最接近的。
    """
    out: list[Finding] = []
    unknown: dict[str, list[str]] = {}
    for s in shots:
        raw = (s.move or "").strip()
        if not raw or camera_moves.match(raw) is not None:
            continue
        unknown.setdefault(raw, []).append(s.no)

    for raw, shot_nos in unknown.items():
        not_a_move = camera_moves.looks_like_not_a_move(raw)
        shot_list = "、".join(f"镜头{n}" for n in shot_nos if n) or "有镜头"
        if not_a_move:
            out.append(
                Finding(
                    code="move_not_a_move",
                    level="warn",
                    message=f"{shot_list} 的运镜写的是「{not_a_move}」——那是"
                    "镜头类型 / 机位，不是运动方式。",
                    suggestion=(
                        "把「" + not_a_move + "」挪到画面描述里，运镜那一栏改成运动方式，例如："
                        + "、".join(camera_moves.labels()[:8])
                        + "。否则静图样片不知道该怎么动。"
                    ),
                    shots=tuple(shot_nos),
                )
            )
            continue
        near = camera_moves.closest(raw)
        tip = f"最接近的是「{near}」；" if near else ""
        out.append(
            Finding(
                code="move_unknown",
                level="warn",
                message=f"{shot_list} 的运镜「{raw}」不在运镜词表里（{len(camera_moves.CAMERA_MOVES)} 种）。",
                suggestion=(
                    tip
                    + "从词表里选一个改掉。不在词表里的写法，静图样片认不出来会按默认动效处理，"
                    "与分镜表写的意思对不上。"
                ),
                shots=tuple(shot_nos),
            )
        )
    return out


def _move_key_of(raw: str) -> str:
    """比较「是不是同一个运镜」用的键：认得出就用词表 key，认不出就用原文。

    用 key 而不是原文，是因为「推近」与「缓推」是同一个运镜——按原文比会把它们
    当成两种，于是「一直在推」这条规则漏掉；按 key 比才看得出来。
    """
    key = camera_moves.move_key(raw)
    return key or f"raw:{(raw or '').strip()}"


def _move_label(raw: str) -> str:
    """概览里显示的名字：认得出用词表的中文名，认不出用用户自己写的原文。

    认不出来的不藏起来——`move_unknown` 那条提醒会点名它，两处要能对上。
    """
    hit = camera_moves.match(raw)
    return str(hit["label"]) if hit else (raw or "").strip()


def _rule_move_repeat(shots: list[Shot]) -> list[Finding]:
    """运镜重复：连续几镜同一个运镜，或某个运镜占了一半以上。"""
    moves = [(s, (s.move or "").strip(), _move_key_of(s.move)) for s in shots]
    moves = [(s, m, k) for s, m, k in moves if m]
    out: list[Finding] = []
    if len(moves) < MOVE_RUN_LEN:
        return out

    def _name(group: list[tuple[Shot, str, str]]) -> str:
        """给人看的这一组叫什么。写法和词表名不同时把写法附在括号里——不然用户会奇怪
        「我写的是缓推，怎么说我连着三个慢推」。

        返回的是**括号里的内容**，引号由调用方加：这里自己带一段「」出来，
        外面再套一层就会变成「慢推」（…）」（这种括号错位真出现过）。
        """
        keys = {k for _s, _m, k in group}
        canon = camera_moves.by_key(next(iter(keys))) if len(keys) == 1 else None
        raws = {m for _s, m, _k in group}
        if canon is None:
            return group[0][1]
        if len(raws) > 1:
            return f"{canon['label']}（你写的是 {'、'.join(sorted(raws))}）"
        return str(canon["label"])

    # 连续段
    run_start = 0
    for i in range(1, len(moves) + 1):
        same = i < len(moves) and moves[i][2] == moves[run_start][2]
        if same:
            continue
        run = moves[run_start:i]
        # 固定镜头连排不算毛病（见 _is_static_move）
        if len(run) >= MOVE_RUN_LEN and not _is_static_move(run[0][1]):
            out.append(
                Finding(
                    code="move_run",
                    level="warn",
                    message=f"连续 {len(run)} 个镜头都是「{_name(run)}」（{_shot_names([s for s, _m, _k in run])}），"
                    "连在一起看会像卡住了。",
                    suggestion="中间至少插一个不同的运镜，或者干脆改成固定镜头留一口气。",
                    shots=tuple(s.no for s, _m, _k in run),
                )
            )
        run_start = i

    counter = Counter(k for _s, _m, k in moves)
    if len(moves) >= MIN_SHOTS_FOR_DISTRIBUTION:
        top_key, top_n = counter.most_common(1)[0]
        if top_n / len(moves) >= MOVE_DOMINANT_RATIO and not any(
            f.code == "move_run" for f in out
        ):
            hits = [(s, m, k) for s, m, k in moves if k == top_key]
            name = _name(hits)
            # 固定镜头占多数的性质轻一档：可能是刻意的冷静风格，提醒一句就够
            static = _is_static_move(hits[0][1])
            out.append(
                Finding(
                    code="move_dominant",
                    level="info" if static else "warn",
                    message=f"「{name}」占了 {top_n}/{len(moves)}"
                    f"（{round(top_n / len(moves) * 100)}%）（{_shot_names([s for s, _m, _k in hits])}）。",
                    suggestion="换掉一部分：固定镜头最省，跟拍/环绕留给动作戏。",
                    shots=tuple(s.no for s, _m, _k in hits),
                )
            )
        elif len(counter) <= 2 and len(moves) >= SINGLE_SCENE_MIN_SHOTS:
            kinds = "、".join(dict.fromkeys(_name([(s, m, k) for s, m, k in moves if k == key])
                                            for key in counter))
            out.append(
                Finding(
                    code="move_kinds_few",
                    level="info",
                    message=f"{len(moves)} 个镜头只用到 {len(counter)} 种运镜（{kinds}）。",
                    suggestion="运镜种类多一点，剪辑时才有东西可用。",
                )
            )
    return out


def _rule_duplicate_scene_text(shots: list[Shot]) -> list[Finding]:
    """相邻镜头的画面描述开头一模一样 → 基本是复制粘贴没改。"""
    hits: list[Shot] = []
    for prev, cur in zip(shots, shots[1:]):
        a = re.sub(r"\s+", "", prev.scene or "")
        b = re.sub(r"\s+", "", cur.scene or "")
        if len(a) >= SIMILAR_PREFIX and len(b) >= SIMILAR_PREFIX and a[:SIMILAR_PREFIX] == b[:SIMILAR_PREFIX]:
            hits.append(cur)
    if not hits:
        return []
    return [
        Finding(
            code="scene_duplicate_adjacent",
            level="warn",
            message=f"{len(hits)} 个镜头的画面与上一镜开头相同（{_shot_names(hits)}），"
            "像是上一镜复制过来忘了改。",
            suggestion="逐镜改画面：每镜至少换掉动作或机位，否则出图会得到一张几乎一样的图。",
            shots=tuple(s.no for s in hits),
        )
    ]


def _rule_ai_slop(shots: list[Shot]) -> list[Finding]:
    """AI 腔：套话词密度。"""
    hit_shots: list[Shot] = []
    total_hits = 0
    per_shot_max = 0
    for s in shots:
        text = " ".join(p for p in (s.scene, s.emotion, s.dialogue) if p)
        n = sum(text.count(term) for term in SLOP_TERMS)
        if n:
            hit_shots.append(s)
            total_hits += n
            per_shot_max = max(per_shot_max, n)
    if not hit_shots:
        return []
    ratio = len(hit_shots) / max(1, len(shots))
    if ratio < SLOP_SHOT_RATIO and per_shot_max < SLOP_PER_SHOT:
        return []
    found = [
        term for term in SLOP_TERMS if any(term in (s.scene + s.emotion + s.dialogue) for s in shots)
    ]
    return [
        Finding(
            code="ai_slop",
            level="warn",
            message=f"有 {len(hit_shots)}/{len(shots)} 个镜头用了套话词（共 {total_hits} 处），"
            f"例如：{'、'.join(found[:6])}。",
            suggestion="把这些词换成具体动作或可拍的东西：「仿佛在诉说」→「手指在桌沿敲了两下」。",
            shots=tuple(s.no for s in hit_shots),
        )
    ]


def _rule_short_first_frame(shots: list[Shot]) -> list[Finding]:
    """首帧提示词太短 → 模型拿不到画面信息。"""
    short = [s for s in shots if 0 < len((s.first_frame or "").strip()) < SHORT_FIRST_FRAME]
    if not short:
        return []
    return [
        Finding(
            code="first_frame_too_short",
            level="info",
            message=f"{len(short)} 个镜头的首帧提示词过短（{_shot_names(short)}）。",
            suggestion="补到一句话以上：主体 + 景别 + 光线 + 风格，四样里至少三样。",
            shots=tuple(s.no for s in short),
        )
    ]


def _rule_single_scene(shots: list[Shot]) -> list[Finding]:
    """镜头不少但只有一个场景 → 可能漏了场景切换。"""
    scenes = {s.scene_no or s.scene_title for s in shots if (s.scene_no or s.scene_title)}
    if len(shots) < SINGLE_SCENE_MIN_SHOTS or len(scenes) != 1:
        return []
    return [
        Finding(
            code="single_scene",
            level="info",
            message=f"{len(shots)} 个镜头都在同一个场景（{next(iter(scenes))}）。",
            suggestion="确认这是有意的（一场戏一镜到底）；不是的话补场景标题，"
            "换场时出图的环境一致性会好很多。",
        )
    ]


_RULES = (
    _rule_missing_fields,
    _rule_size_distribution,
    _rule_unknown_move,
    _rule_move_repeat,
    _rule_duplicate_scene_text,
    _rule_ai_slop,
    _rule_short_first_frame,
    _rule_single_scene,
)


def lint_shots(shots: list[Shot]) -> list[Finding]:
    """跑全部规则。纯函数：给一串镜头，出一串结论，不碰数据库也不调模型。"""
    out: list[Finding] = []
    for rule in _RULES:
        try:
            out.extend(rule(shots))
        except Exception:  # noqa: BLE001
            # 体检是辅助功能：一条规则自己写崩了，不能让整张报告变成 500。
            # 代价是那条规则静默失效——这在单测里用「每条规则都有用例」来兜。
            continue
    # warn 排在 info 前面：用户先看要紧的
    return sorted(out, key=lambda f: (f.level != "warn", f.code))


def summarize(shots: list[Shot]) -> dict[str, Any]:
    """给界面用的一行概览。键名用 camelCase，与其它接口返回保持一致。

    运镜这一栏按**词表的规范名**归类：「缓推」与「推近」是同一种运镜，
    按原文分组会把它算成两种，于是「怎么一直在推」在概览里看不出来——
    而概览正是用户第一眼看的那一行。认不出来的写法原样显示，那正是要给人看见的
    （下面那条提醒会点名它）。
    """
    sizes = Counter(normalize_size(s.size) for s in shots if normalize_size(s.size))
    moves = Counter(
        _move_label((s.move or "").strip()) for s in shots if (s.move or "").strip()
    )
    return {
        "shots": len(shots),
        "sizes": dict(sizes.most_common()),
        "moves": dict(moves.most_common()),
        "scenes": len({s.scene_no or s.scene_title for s in shots if (s.scene_no or s.scene_title)}),
        "totalSeconds": sum(s.duration_seconds for s in shots),
    }


def lint_text(text: str) -> tuple[list[Shot], list[Finding], dict[str, Any]]:
    """从原始文本一路跑到结论（解析用的就是生图/生视频那一个解析器）。

    共用解析器很重要：体检说「有 12 个镜头」而真正开拍时说「解析出 15 个」，
    这种对不上的报告会直接让人不信它。
    """
    shots = storyboard_sheet.parse_storyboard(text or "")
    if not shots:
        return [], [], summarize([])
    return shots, lint_shots(shots), summarize(shots)
