"""运镜词表：**一份表，四处共用**。

为什么要有这个模块：在它之前，「运镜」这件事在四个地方各有一份说法——

| 地方 | 原来是什么 |
| --- | --- |
| 分镜提示词 | 只说「一个镜头只指定 1 种运镜，禁止写组合」，**从没告诉模型该用哪些词** |
| 静图样片 `animatic` | 一份自己排过序的关键词表（表内顺序还兼着优先级，注释里写着「容易踩」） |
| 分镜体检 `storyboard_lint` | 另一份散词表 `_MOVE_HINTS`，用来判「有没有写运镜」 |
| 体检的建议文案 | 手写着第三份：「推/拉/摇/移/跟/升降/环绕/固定」 |

四份表的直接后果，一是**模型自造词**（「跟拍」「缓推」「推轨」都出现过），二是
`animatic` 认不出就**静默套一个推近**——样片动了、但动的不是分镜表写的意思，而
界面上完全看不出来。所以这里的做法是：**提示词把词表给模型（它才会写规范词）、
解析与体检认同一份表、样片照它映射**。

三条口径：

1. **只收「运动」**，不收机位与景别。「主观镜头」「过肩镜头」「插入镜头」是机位/镜头类型，
   不是运镜——它们单列在 `NOT_MOVES` 里，体检会明说「这是机位不是运镜」，
   而不是含糊地说「不认识的运镜」。
2. **认不出来的要能说清**，不静默兜底。`match()` 回 `None`，由调用方决定怎么办；
   体检把它列出来并给一个最接近的建议。
3. **样片做不到的就说不做**。静图样片只有 8 种缓动，像「移焦」「手持微晃」「甩镜」
   这类它做不到的，映射到最接近的那种并**留下 `animatic_note` 说明为什么**——
   宁可明确写「这条样片里近似成不动」，也不要假装动了一下。
"""

from __future__ import annotations

import difflib

# 分组（只为界面与提示词里好读，不参与任何判据）
GROUPS = (
    ("static", "静止"),
    ("push", "推"),
    ("pull", "拉"),
    ("pan", "摇"),
    ("truck", "移"),
    ("tilt", "俯仰"),
    ("crane", "升降"),
    ("aerial", "航拍"),
    ("orbit", "环绕"),
    ("special", "特殊"),
)

# 镜头的运动方式。每一条：
#
#   key      代码里用的稳定标识（ASCII，不许改）
#   label    中文名，**分镜表的「运镜」栏里就该写这个词**
#   en       英文名，供生视频提示词用（英文模型更认这个）
#   group    分组
#   aliases  模型/用户可能写的各种说法，解析时都归到这一条
#   hint     什么时候用（给人与模型看的）
#   animatic 静图样片近似成哪一种（见 `animatic.py` 的 8 种缓动）
#   note     近似时的损失说明；为空表示能如实表现
#
# `aliases` 里刻意保留单字（推/拉/摇/移/升/降/绕）：它们只在这一栏里出现，
# 而这一栏的词汇量就这么大（`storyboard_sheet` 只把标题里 `|` 分隔的第 3 段当运镜）。
CAMERA_MOVES: tuple[dict[str, object], ...] = (
    # ---------------- 静止 ----------------
    {
        "key": "static", "label": "固定", "en": "static shot", "group": "static",
        # 「lock」要连单独的写法一起收：分镜表里常直接写「lock」，
        # 只留「lock off / locked」会让它掉进「不认识」里（旧词表本来是认识的，
        # 漏掉就是移植时弄丢的回归）。
        "aliases": ("固定", "静止", "不动", "机位不变", "定格", "static", "lock", "lock off", "locked"),
        "hint": "机位完全不动。对话戏与情绪停顿的底子，也是「让观众自己看」的拍法",
        "animatic": "static", "note": "",
    },
    {
        "key": "handheld", "label": "手持微晃", "en": "handheld shot", "group": "static",
        "aliases": ("手持", "手持微晃", "轻微晃动", "handheld", "shaky cam"),
        "hint": "轻微呼吸感，像有人端着机器。写实、纪录片、紧张段落",
        "animatic": "static",
        "note": "静图样片做不出晃动，这里近似成不动——节奏对，质感要靠后面的真视频",
    },
    # ---------------- 推 ----------------
    {
        "key": "push", "label": "慢推", "en": "slow push in", "group": "push",
        "aliases": ("慢推", "推近", "推进", "缓推", "推", "slow push", "push in", "dolly in"),
        "hint": "缓慢靠近，把注意力收拢到一个人或一件物上。最常用、最不容易出错",
        "animatic": "push", "note": "",
    },
    {
        "key": "push_fast", "label": "快推", "en": "fast push in", "group": "push",
        "aliases": ("快推", "急推近", "快速推近", "fast push", "crash zoom"),
        "hint": "迅速凑近，制造压迫或惊觉。语气重，一场戏里用一次就够",
        "animatic": "push", "note": "样片按同一种推近表现（速度差看不出来），强度靠真视频",
    },
    {
        "key": "snap_push", "label": "急推", "en": "snap push", "group": "push",
        "aliases": ("急推", "猛推", "冲击推", "snap push", "punch in"),
        "hint": "一下怼上去，用在打斗命中、情绪爆点。转场也可以用它当切口",
        "animatic": "push", "note": "样片按同一种推近表现，冲击感要靠真视频",
    },
    {
        "key": "dolly_zoom", "label": "变焦推", "en": "dolly zoom", "group": "push",
        "aliases": ("变焦推", "希区柯克变焦", "推轨变焦", "dolly zoom", "vertigo"),
        "hint": "人物大小不变而背景被压扁/拉开，表现眩晕与心理失衡",
        "animatic": "push",
        "note": "静图样片做不出「人物不变而背景变」，只近似成推近",
    },
    {
        "key": "zoom_in", "label": "变焦推近", "en": "zoom in", "group": "push",
        "aliases": ("变焦推近", "变焦", "zoom in", "zoom"),
        "hint": "不动机位、只改焦距。省事，但空间关系会变扁，慎用在大景深镜头上",
        "animatic": "push", "note": "",
    },
    # ---------------- 拉 ----------------
    {
        "key": "pull", "label": "慢拉", "en": "slow pull out", "group": "pull",
        "aliases": ("慢拉", "拉远", "拉出", "后拉", "拉", "slow pull", "pull out", "dolly out"),
        "hint": "缓缓退开，让人从情境里抽离。常放在一场戏的收尾",
        "animatic": "pull", "note": "",
    },
    {
        "key": "pull_reveal", "label": "拉远揭示", "en": "reveal pull out", "group": "pull",
        "aliases": ("拉远揭示", "揭示", "拉镜揭示", "reveal", "pull back reveal"),
        "hint": "退开的同时把原本在画外的东西带进来，用来抖包袱或交代处境",
        "animatic": "pull", "note": "",
    },
    {
        "key": "snap_pull", "label": "急拉", "en": "snap pull out", "group": "pull",
        "aliases": ("急拉", "猛拉", "快速拉远", "snap pull", "punch out"),
        "hint": "猛地退开，制造错愕或「原来如此」",
        "animatic": "pull", "note": "样片按同一种拉远表现",
    },
    {
        "key": "zoom_out", "label": "变焦拉远", "en": "zoom out", "group": "pull",
        "aliases": ("变焦拉远", "缩小", "zoom out"),
        "hint": "不动机位、只拉焦距。省事，空间会变平",
        "animatic": "pull", "note": "",
    },
    # ---------------- 摇 ----------------
    {
        "key": "pan_left", "label": "左摇", "en": "pan left", "group": "pan",
        "aliases": ("左摇", "向左摇", "向左", "pan left"),
        "hint": "原地左转。用来带出画外的信息，或者顺着人物的视线走",
        "animatic": "pan_left", "note": "",
    },
    {
        "key": "pan_right", "label": "右摇", "en": "pan right", "group": "pan",
        "aliases": ("右摇", "向右摇", "向右", "pan right"),
        "hint": "原地右转。与左摇对称，选哪个看你想先给观众看什么",
        "animatic": "pan_right", "note": "",
    },
    {
        "key": "pan_scan", "label": "横摇扫过", "en": "scanning pan", "group": "pan",
        "aliases": ("横摇", "扫过", "扫视", "横扫", "摇", "pan", "swish pan"),
        "hint": "从左到右（或反过来）扫过整个空间，交代环境与人物站位",
        "animatic": "pan_right", "note": "",
    },
    {
        "key": "whip_pan", "label": "甩镜", "en": "whip pan", "group": "pan",
        "aliases": ("甩镜", "甩", "快速摇", "whip pan", "whip"),
        "hint": "极快地甩过去，画面糊成一条。常当软切用，把两个空间接起来",
        "animatic": "pan_right", "note": "样片按普通横摇表现，糊成一条的运动模糊要靠真视频",
    },
    # ---------------- 移 ----------------
    {
        "key": "truck_left", "label": "左移", "en": "truck left", "group": "truck",
        "aliases": ("左移", "向左移", "左平移", "truck left", "track left"),
        "hint": "机位整体左移。与左摇的区别是透视会变，空间更立体",
        "animatic": "pan_left", "note": "样片用横移的视窗位移近似，看不出是摇还是移",
    },
    {
        "key": "truck_right", "label": "右移", "en": "truck right", "group": "truck",
        "aliases": ("右移", "向右移", "右平移", "移", "truck right", "track right"),
        "hint": "机位整体右移。常用来与人物并行，边走边说",
        "animatic": "pan_right", "note": "样片用横移的视窗位移近似",
    },
    {
        "key": "track_follow", "label": "跟拍", "en": "tracking shot", "group": "truck",
        "aliases": ("跟拍", "跟随", "跟", "tracking", "follow", "follow shot"),
        "hint": "跟着人物走，观众的注意力黏在他身上。走路戏与长镜头常用",
        "animatic": "pan_right", "note": "静图样片做不出「跟着走」，只近似成横向位移",
    },
    {
        "key": "dolly", "label": "推轨", "en": "dolly shot", "group": "truck",
        "aliases": ("推轨", "轨道", "dolly", "truck", "dolly shot"),
        "hint": "用轨道平稳移动，是「移」这一类的标准做法。比手持稳、比摇镜更有空间感",
        "animatic": "pan_right", "note": "样片按横向位移近似，具体往哪边走看不出来",
    },
    # ---------------- 俯仰 ----------------
    {
        "key": "tilt_up", "label": "上摇", "en": "tilt up", "group": "tilt",
        "aliases": ("上摇", "向上摇", "仰摇", "向上", "升", "tilt up"),
        "hint": "从下往上抬，把人拍得高大。人物登场与仰视主体常用",
        "animatic": "tilt_up", "note": "",
    },
    {
        "key": "tilt_down", "label": "下摇", "en": "tilt down", "group": "tilt",
        "aliases": ("下摇", "向下摇", "俯摇", "向下", "降", "tilt down"),
        "hint": "从上往下俯视，表现压迫、审视或者「发现」。也常用来交代脚下的东西",
        "animatic": "tilt_down", "note": "",
    },
    {
        "key": "tilt_reveal", "label": "俯仰揭示", "en": "tilt reveal", "group": "tilt",
        "aliases": ("俯仰揭示", "摇镜揭示", "仰起揭示", "tilt reveal"),
        "hint": "边抬（或边压）边把新的主体带进画面，比直给的镜头多一层悬念",
        "animatic": "tilt_up", "note": "",
    },
    # ---------------- 升降 ----------------
    {
        "key": "crane_up", "label": "升镜", "en": "crane up", "group": "crane",
        "aliases": ("升镜", "升起", "升高", "上升", "crane up", "boom up"),
        "hint": "机位整体升高。常放在结尾，把人留在原地、视野一点点拉开",
        "animatic": "tilt_up", "note": "样片做不出「升高」，只近似成向上位移",
    },
    {
        "key": "crane_down", "label": "降镜", "en": "crane down", "group": "crane",
        "aliases": ("降镜", "下降", "降落", "落下", "crane down", "boom down"),
        "hint": "机位整体下降，从环境收进具体的人或物",
        "animatic": "tilt_down", "note": "样片做不出「下降」，只近似成向下位移",
    },
    {
        "key": "crane", "label": "升降", "en": "crane shot", "group": "crane",
        "aliases": ("升降", "升降臂", "摇臂", "crane", "jib", "boom"),
        "hint": "用摇臂做大幅度的上下运动。气势足，一场戏里用一次",
        "animatic": "tilt_up", "note": "样片按向上位移近似，方向看不出来",
    },
    # ---------------- 航拍 ----------------
    {
        "key": "aerial_up", "label": "航拍上升", "en": "drone ascend", "group": "aerial",
        "aliases": ("航拍上升", "航拍", "无人机上升", "drone ascend", "aerial"),
        "hint": "无人机垂直拉高，把人和地方的关系一次交代清楚。开场与收尾都好用",
        "animatic": "tilt_up", "note": "样片按向上位移近似，拍摄高度差不出来",
    },
    {
        "key": "aerial_down", "label": "航拍下降", "en": "drone descend", "group": "aerial",
        "aliases": ("航拍下降", "无人机下降", "drone descend"),
        "hint": "从高处压下来，落到具体的人身上",
        "animatic": "tilt_down", "note": "样片按向下位移近似",
    },
    {
        "key": "aerial_follow", "label": "航拍跟拍", "en": "drone follow", "group": "aerial",
        "aliases": ("航拍跟拍", "无人机跟拍", "drone follow", "aerial tracking"),
        "hint": "在空中跟着主体移动，适合车、马、奔跑这类大范围运动",
        "animatic": "pan_right", "note": "样片按横向位移近似",
    },
    {
        "key": "aerial_orbit", "label": "航拍环绕", "en": "drone orbit", "group": "aerial",
        "aliases": ("航拍环绕", "无人机环绕", "drone orbit"),
        "hint": "在空中绕着主体转，把四周环境一并给到",
        "animatic": "orbit", "note": "",
    },
    # ---------------- 环绕 ----------------
    {
        "key": "orbit_left", "label": "环绕左", "en": "orbit left", "group": "orbit",
        "aliases": ("环绕左", "左环绕", "orbit left"),
        "hint": "绕着主体逆时针走，多用于对话与对峙，让两个人的关系随位置变化",
        "animatic": "orbit", "note": "静图样片只能近似成「缓慢推近 + 轻微横移」",
    },
    {
        "key": "orbit_right", "label": "环绕右", "en": "orbit right", "group": "orbit",
        "aliases": ("环绕右", "右环绕", "环绕", "旋转", "绕", "orbit", "circle"),
        "hint": "绕着主体顺时针走。环绕是「让画面有呼吸」最稳的一招",
        "animatic": "orbit", "note": "静图样片只能近似，看不出绕的方向",
    },
    {
        "key": "arc", "label": "弧线", "en": "arc shot", "group": "orbit",
        "aliases": ("弧线", "弧线运动", "半环绕", "arc", "arc shot"),
        "hint": "只走一段弧，比整圈环绕轻。想动一下又不想太抢戏时用它",
        "animatic": "orbit", "note": "静图样片按环绕同一种近似",
    },
    {
        "key": "spiral_up", "label": "螺旋上升", "en": "spiral up", "group": "orbit",
        "aliases": ("螺旋上升", "螺旋", "盘旋上升", "spiral"),
        "hint": "边绕边升高。收尾拉升气势用，慎用在人物对话里",
        "animatic": "orbit", "note": "样片按环绕近似，升高那部分表现不出来",
    },
    # ---------------- 特殊 ----------------
    {
        "key": "roll", "label": "翻滚", "en": "roll", "group": "special",
        "aliases": ("翻滚", "侧倾", "镜头侧倾", "roll", "canted"),
        "hint": "画面本身打转或倾斜。表现眩晕、失重、失控。别连着用两次",
        "animatic": "orbit", "note": "样片按环绕近似，画面本身不打转",
    },
    {
        "key": "rack_focus", "label": "移焦", "en": "rack focus", "group": "special",
        "aliases": ("移焦", "变焦对焦", "焦段转移", "rack focus", "pull focus", "focus pull"),
        "hint": "机位与焦距都不变，只把对焦点从一个人移到另一个人。讲关系、讲「谁在听」",
        "animatic": "static",
        "note": "样片做不出虚实变化，近似成不动——**这一条要靠真视频**",
    },
)

# 不是运镜、但常被写进「运镜」这一栏的词。体检会明说它是机位/镜头类型，
# 而不是含糊地报「不认识的运镜」——用户要知道的是「该改哪一栏」。
NOT_MOVES: tuple[str, ...] = (
    "主观镜头", "客观镜头", "过肩", "过肩镜头", "插入镜头", "建立镜头", "空镜",
    "正反打", "双人镜头", "pov", "insert", "establishing",
)

_BY_KEY: dict[str, dict[str, object]] = {str(m["key"]): m for m in CAMERA_MOVES}


def by_key(key: object) -> dict[str, object] | None:
    return _BY_KEY.get(str(key or ""))


def labels() -> list[str]:
    """全部中文名，按 `CAMERA_MOVES` 的顺序。**体检的建议文案从这里生成**，
    不再手写一份（手写的那份必然与表脱节）。"""
    return [str(m["label"]) for m in CAMERA_MOVES]


def group_label(group: str) -> str:
    for key, label in GROUPS:
        if key == group:
            return label
    return group


def match(raw: object) -> dict[str, object] | None:
    """把「运镜」栏里的文字认到某一条运镜上；认不出来回 `None`。

    判据分两层：

    1. **先用多字别名判**：先在文字里出现的说了算；同一位置命中多个时取更长的那个。
    2. **一个多字别名都没命中时，才拿单字别名兜底**（推/拉/摇/移/升/降/绕/跟/甩）。
       单字只是为了接住「推」「左移」这种极简写法，**不能拿它跟多字别名抢**。

    第 2 条是抽查时补上的：「飘移推近」本来被认成**右移**——「移」在第 1 个字上命中，
    而真正表示运镜的「推近」在第 3 个字上，位置优先反而判错了。分两层之后
    「飘移推近」正确落到慢推，而「横移跟拍」仍是移动、「推进跟拍」仍是推近。

    这个规则比「按表内顺序」稳得多——`animatic` 原来的注释里专门写着「表内顺序只在
    同一位置命中多个词时当优先级」，而那意味着**改表的顺序会改识别结果**，是个埋着的坑。
    """
    text = str(raw or "").strip().lower()
    if not text:
        return None
    for min_len in (2, 1):
        best: tuple[int, int, str] | None = None
        for move in CAMERA_MOVES:
            for alias in move["aliases"]:  # type: ignore[union-attr]
                word = str(alias).lower()
                # 第一轮只看多字别名，第二轮只看单字别名——两轮不混着比
                if (len(word) >= 2) != (min_len == 2):
                    continue
                pos = text.find(word)
                if pos < 0:
                    continue
                # 位置优先、其次别名更长（`-len` 让「更长」在元组比较里更小）
                cand = (pos, -len(word), str(move["key"]))
                if best is None or cand < best:
                    best = cand
        if best is not None:
            return _BY_KEY[best[2]]
    return None


def move_key(raw: object) -> str:
    """认到的运镜 key（认不出回空串）。"""
    hit = match(raw)
    return str(hit["key"]) if hit else ""


def is_static(raw: object) -> bool:
    """「运镜」栏是不是固定机位。

    用 `key` 判而不是自己再列一份词——`animatic` 与 `storyboard_lint` 以前各有一份
    等价实现，**两处必须永远给出同一个答案**（否则会出现「体检说不是固定、样片却不动」），
    所以收敛到这一处。
    """
    return move_key(raw) == "static"


def looks_like_not_a_move(raw: object) -> str:
    """看着像「机位/镜头类型」而不是运镜时，回那个词；否则回空串。"""
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    for term in NOT_MOVES:
        if term in text:
            return term
    return ""


def closest(raw: object) -> str:
    """认不出来时给一个最接近的中文名（给体检的建议用）。没有相近的则回空串。

    阈值放到 0.3 是为了**宁可给一个不太准的提示，也不要什么都不说**——
    用户看到「不在词表里」时，最想知道的就是「那我该写哪个」。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    pool = labels() + [str(m["en"]) for m in CAMERA_MOVES]
    hit = difflib.get_close_matches(text, pool, n=1, cutoff=0.3)
    if not hit:
        return ""
    got = hit[0]
    for m in CAMERA_MOVES:  # 命中的可能是英文名，统一回中文
        if str(m["en"]) == got:
            return str(m["label"])
    return got


def prompt_vocabulary(*, with_en: bool = True, per_line: int = 6) -> str:
    """给分镜提示词用的词表（分组、紧凑）。

    为什么要塞进提示词：以前只写「一个镜头只指定 1 种运镜，禁止写组合」，
    **从没告诉模型该用哪些词**，于是模型自造「跟拍」「缓推」「推轨」这类说法；
    而解析认不出时会静默套一个推近——样片动了，但动的不是分镜表写的意思。
    给一份词表，模型才会写规范词，下游三处（解析/体检/样片）才有共同语言。
    """
    lines: list[str] = []
    for gkey, glabel in GROUPS:
        items = [m for m in CAMERA_MOVES if m["group"] == gkey]
        if not items:
            continue
        chunks = [
            f"{m['label']}（{m['en']}）" if with_en else str(m["label"]) for m in items
        ]
        for i in range(0, len(chunks), per_line):
            piece = "、".join(chunks[i : i + per_line])
            prefix = f"- {glabel}：" if i == 0 else "  "
            lines.append(prefix + piece)
    return "\n".join(lines)


def prompt_block() -> str:
    """附加到「分镜」系统提示词末尾的那一段（一句硬要求 + 词表）。

    **为什么不写在提示词种子数据里**（本节最容易做错的一处）：

    1. `ensure_seed` 从不覆盖已存在的行。提示词一旦写进库里，老库升级上来仍是旧文本——
       一份只存在于种子里的词表，**对所有升级上来的用户等于不存在**，而他们正是多数。
       实测过：升级后的库里那份分镜提示词还是旧的，没有词表。
    2. 它是**与下游解析的契约**，不是风格偏好。用户当然可以改分镜提示词，但如果把词表
       改没了，模型又开始自造词，而样片认不出时会静默套一个默认动效——分镜写的和看到的
       对不上，界面上还看不出来。所以这一段的管辖权重在代码里，与 `style_service` 注入
       风格要求是同一个机制（见 `doc_service.HARD_RULES`）。

    代价说明白：系统设置里的「创作 Agent」看不到这一段。提示词里第 1 条会写明
    「词表由程序附加、不在这里维护」，免得管理员在一个看不见的表上纠结。
    """
    return (
        "【运镜词表】「运镜」那一栏只能从下面这份词表里选 1 个，写词表里的中文名；"
        "禁止写「推拉摇移」这类组合，禁止自造说法：\n" + prompt_vocabulary()
    )


def animatic_kind(raw: object, *, default: str = "push") -> str:
    """这一栏对应静图样片的哪一种缓动。认不出来回 `default`。

    **认不出来是调用方的事，不是这里的事**：这里只做映射，不回退到「猜一个」——
    要报「认不出」的地方（体检）自己去调 `match()`。
    """
    hit = match(raw)
    return str(hit["animatic"]) if hit else default


def animatic_note(raw: object) -> str:
    """静图样片对这一栏做了什么近似（空串 = 如实表现）。界面拿它说清楚。"""
    hit = match(raw)
    return str(hit.get("note") or "") if hit else ""


def as_dicts() -> list[dict[str, object]]:
    """给接口/界面的列表（去掉 aliases，那是解析用的，摆到前端没意义）。"""
    out: list[dict[str, object]] = []
    for m in CAMERA_MOVES:
        item = dict(m)
        item.pop("aliases", None)
        item["groupLabel"] = group_label(str(m["group"]))
        out.append(item)
    return out


def assert_consistent() -> None:
    """自检：key 不重复、别名不冲突、分组都存在、样片映射都是合法值。

    别名冲突是最要命的一条——两条运镜共用同一个别名时，谁赢取决于遍历顺序，
    那种「今天对明天错」的识别最难查。所以在这里直接拦住。
    """
    keys = [str(m["key"]) for m in CAMERA_MOVES]
    if len(keys) != len(set(keys)):
        raise RuntimeError(f"运镜 key 重复：{keys}")
    groups = {g for g, _ in GROUPS}
    seen: dict[str, str] = {}
    for m in CAMERA_MOVES:
        if m["group"] not in groups:
            raise RuntimeError(f"{m['key']} 的分组不在 GROUPS 里：{m['group']}")
        if not m["label"] or not m["en"]:
            raise RuntimeError(f"{m['key']} 缺 label 或 en")
        if m["animatic"] not in ("push", "pull", "pan_left", "pan_right",
                                "tilt_up", "tilt_down", "orbit", "static"):
            raise RuntimeError(f"{m['key']} 的 animatic 映射不合法：{m['animatic']}")
        for alias in m["aliases"]:  # type: ignore[union-attr]
            a = str(alias).lower()
            if a in seen and seen[a] != m["key"]:
                raise RuntimeError(f"别名「{alias}」被 {seen[a]} 与 {m['key']} 共用")
            seen[a] = str(m["key"])
    # 每条别名的解析结果必须是它自己（别名的存在意义就是能认回来）
    for m in CAMERA_MOVES:
        for alias in m["aliases"]:  # type: ignore[union-attr]
            got = move_key(str(alias))
            if got != m["key"]:
                raise RuntimeError(
                    f"别名「{alias}」本该认成 {m['key']}，实际认成 {got or '（认不出）'}"
                )
