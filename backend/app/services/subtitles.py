"""字幕的纯口径：版式库、画风推荐、ASS 文本生成。

判据与文案要和「调 ffmpeg 烧录」分开——这样「几条字幕、时间轴对不对、字号多少像素」
这些**用户能看见的结论**可以直接断言，不必真跑一次编码去测（与 `transitions.py` 同一个理由）。

四条口径，改之前先读：

1. **版式是用户直接选的，画风只提供默认值。** 2026-09-20 用户明确否掉了「字幕版式完全
   跟画风走」：`subtitle_style_key` 空 = 跟画风（用推荐值），非空 = 用户自己选的，谁都不许改。
   这与 #32 的回滚、#33 的定稿是同一个口径——**用户显式说过的话不被别的东西推翻**。
2. **一条字幕一个时间区间，时间轴按「逐镜时长」累加**。烧字幕最怕的是对不上口型，
   所以时间轴从已有的镜头时长算，不另做一套。
3. **颜色要按 BGR 写，不是 RGB。** ASS 的颜色是 `&HAABBGGRR`——`#FF8800` 要写成
   `&H000088FF`。这个写反了画面还是"有颜色"的，只是红蓝互换，很容易被当成"风格问题"
   而一路带下去，所以单独立一个 `ass_color()` 并单独测。
4. **文本里的换行必须转成 `\\N`**。ASS 是按行解析的，text 字段里出现真换行会把整个文件
   拆坏（后面的行会被当成新字段），而 libass 只会静默丢掉——不是报错。

关于**竖排**：ASS 没有真正的竖排，只能靠「每个字后面加一个 `\\N`」近似，标点位置仍然
按横排处理。所以版式里不提供竖排，需要竖排的用户请自己排好文本。这一条写在这里是为了
避免以后有人以为"加个竖排选项"是个小改动。
"""

from __future__ import annotations

from app.services import subtitle_fonts

# 字号（相对画面高度）、边距、描边宽也都用相对值——一套版式换到 720p / 1080p / 4K 都成立，
# 不必为每种分辨率各调一遍。渲染时乘上真实高度。
STYLES: tuple[dict[str, object], ...] = (
    {
        "key": "doc_bottom",
        "label": "纪录片下三分之一",
        "hint": "左下角、白字深描边，最通用的一种",
        "font": "noto_sans", "size": 0.048, "bold": False,
        "align": 1, "margin_v": 0.05, "margin_h": 0.05,
        "primary": "#FFFFFF", "outline": "#000000", "outline_w": 0.0018, "shadow": 0.0012,
        "box": None, "spacing": 0.0,
    },
    {
        "key": "center_minimal",
        "label": "居中极简",
        "hint": "正下方居中、细描边，不抢画面",
        "font": "noto_sans", "size": 0.05, "bold": False,
        "align": 2, "margin_v": 0.075, "margin_h": 0.06,
        "primary": "#FFFFFF", "outline": "#000000", "outline_w": 0.0012, "shadow": 0.0,
        "box": None, "spacing": 1.0,
    },
    {
        "key": "movie_big",
        "label": "电影感大字",
        "hint": "居中偏下、字号大、字距松，适合旁白与金句",
        "font": "noto_sans", "size": 0.066, "bold": True,
        "align": 2, "margin_v": 0.09, "margin_h": 0.08,
        "primary": "#FFFFFF", "outline": "#000000", "outline_w": 0.0022, "shadow": 0.0015,
        "box": None, "spacing": 2.0,
    },
    {
        "key": "variety_pop",
        "label": "综艺花字",
        "hint": "亮黄加粗黑描边，冲击力强、适合卡点",
        "font": "noto_sans", "size": 0.076, "bold": True,
        "align": 2, "margin_v": 0.11, "margin_h": 0.06,
        "primary": "#FFE44D", "outline": "#1A1A1A", "outline_w": 0.0035, "shadow": 0.0022,
        "box": None, "spacing": 1.0,
    },
    {
        "key": "news_bar",
        "label": "新闻条",
        "hint": "贴底、加半透明黑底条，密而清楚",
        "font": "noto_sans", "size": 0.05, "bold": False,
        "align": 2, "margin_v": 0.012, "margin_h": 0.02,
        "primary": "#FFFFFF", "outline": "#000000", "outline_w": 0.0, "shadow": 0.0,
        "box": "#000000B3", "spacing": 0.5,
    },
    {
        "key": "ink_serif",
        "label": "宋体文艺",
        "hint": "宋体配米白，人文 / 书卷气",
        "font": "noto_serif", "size": 0.05, "bold": False,
        "align": 2, "margin_v": 0.08, "margin_h": 0.07,
        "primary": "#F2EDE4", "outline": "#1A1A1A", "outline_w": 0.0016, "shadow": 0.001,
        "box": None, "spacing": 1.5,
    },
    {
        "key": "guofeng_kai",
        "label": "古风楷体",
        "hint": "楷体配暖白，古风 / 手写感",
        "font": "wenkai", "size": 0.055, "bold": False,
        "align": 2, "margin_v": 0.085, "margin_h": 0.07,
        "primary": "#F5EFE2", "outline": "#2A1F14", "outline_w": 0.0016, "shadow": 0.0012,
        "box": None, "spacing": 1.5,
    },
    {
        "key": "dialogue_line",
        "label": "对白式",
        "hint": "字号偏小、两行以内，贴近日常看片的习惯",
        "font": "noto_sans", "size": 0.044, "bold": False,
        "align": 2, "margin_v": 0.065, "margin_h": 0.09,
        "primary": "#FFFFFF", "outline": "#000000", "outline_w": 0.0014, "shadow": 0.001,
        "box": None, "spacing": 0.0,
    },
    {
        "key": "poster_smiley",
        "label": "标题美术字",
        "hint": "倾斜美术字，只建议给标题用（生僻字会缺）",
        "font": "smiley", "size": 0.078, "bold": False,
        "align": 2, "margin_v": 0.1, "margin_h": 0.06,
        "primary": "#FFFFFF", "outline": "#000000", "outline_w": 0.003, "shadow": 0.002,
        "box": None, "spacing": 2.0,
    },
    {
        "key": "top_note",
        "label": "顶部说明",
        "hint": "贴顶部，用来标时间 / 地点 / 注释",
        "font": "noto_sans", "size": 0.042, "bold": False,
        "align": 8, "margin_v": 0.04, "margin_h": 0.06,
        "primary": "#E8E8E8", "outline": "#000000", "outline_w": 0.0014, "shadow": 0.001,
        "box": None, "spacing": 0.5,
    },
)

DEFAULT_STYLE = "doc_bottom"

_BY_KEY = {str(s["key"]): s for s in STYLES}

# 画风 → **推荐**版式。只用于「用户没选过」时的默认值，不是绑定。
# 键是 `director_styles` 里的 style key（与画布/导演台共用同一份风格表）。
RECOMMENDED: dict[str, str] = {
    "spielberg_face": "movie_big",      # 面孔与视线 → 大字居中
    "cameron_spectacle": "movie_big",   # 奇观 → 大字居中
    "nolan_realism": "doc_bottom",      # 写实 → 左下角纪实
    "wong_kar_wai": "ink_serif",        # 情绪与留白 → 宋体文艺
    "wes_anderson": "center_minimal",   # 对称构图 → 居中极简
    "miyazaki_nature": "ink_serif",     # 自然与生活感 → 宋体文艺
    "tarantino_tension": "poster_smiley",  # 张力 → 美术字
    "villeneuve_silence": "center_minimal",  # 沉默与极简 → 居中极简
    "guoman": "poster_smiley",          # 国漫 → 美术字
    "cyberpunk": "variety_pop",         # 赛博朋克 → 高对比亮色
    "ink_wash": "guofeng_kai",          # 水墨 → 古风楷体
    "handheld_doc": "doc_bottom",       # 纪实 → 左下角纪实
}

MIN_SCALE = 0.5
MAX_SCALE = 2.0


# ------------------------------------------------------------------ 版式


def style(key: object) -> dict[str, object] | None:
    return _BY_KEY.get(str(key or "").strip())


def resolve_style(chosen: object, genre_key: object = None) -> str:
    """定下来最终用哪一款版式。**这是「用户说了算」唯一的实现点。**

    - 用户选过（`chosen` 认得出）→ 就用它，不理会画风；
    - 用户没选（空 / 认不出）→ 拿画风的推荐值；画风也不认识就用 `DEFAULT_STYLE`。

    上层只调这一个函数，别在两处各判一次——两处各判一次就会出现
    「面板上显示这款、导出用的那款」。
    """
    key = str(chosen or "").strip()
    if style(key):
        return key
    return recommend(genre_key)


def recommend(genre_key: object) -> str:
    """某个画风推荐的版式。认不出画风就给默认。"""
    return RECOMMENDED.get(str(genre_key or "").strip(), DEFAULT_STYLE)


def following_genre(chosen: object) -> bool:
    """当前是不是「跟画风」（用户没自己选过）。界面据此措辞。"""
    return not style(chosen)


def sanitize_scale(raw: object) -> float:
    """字号缩放：读不出来用 1.0，超范围夹到边界（改一下就好，不必报错）。"""
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1.0
    if value != value:  # NaN
        return 1.0
    return max(MIN_SCALE, min(MAX_SCALE, round(value, 2)))


# ------------------------------------------------------------------ 颜色与时间


def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    """`#RRGGBB` → ASS 的 `&HAABBGGRR`。

    **ASS 是 BGR，不是 RGB。** 写反了画面照样有颜色，只是红蓝互换——很容易被当成
    「风格问题」一路带下去，所以这里单独一个函数、单独测。
    `alpha` 0 = 完全不透明（ASS 的 AA 是「透明度」，00 才是不透明，与 CSS 相反）。
    """
    raw = str(hex_rgb or "").strip().lstrip("#")
    if len(raw) == 8:  # 允许直接写 #RRGGBBAA
        raw, alpha = raw[:6], int(raw[6:8], 16)
    if len(raw) != 6 or any(c not in "0123456789abcdefABCDEF" for c in raw):
        raise ValueError(f"颜色要写成 #RRGGBB，收到：{hex_rgb!r}")
    r, g, b = raw[0:2], raw[2:4], raw[4:6]
    return f"&H{max(0, min(255, int(alpha))):02X}{b}{g}{r}".upper()


def ass_time(seconds: float) -> str:
    """秒 → ASS 的 `H:MM:SS.CC`（1 位小时、2 位百分秒）。

    负值夹到 0：ffprobe 偶尔给出 -0.01 这类起点，直接格式化会变成 `-1:59:59.99`，
    整条字幕就不显示了（libass 不报错）。
    """
    total = max(0.0, float(seconds or 0.0))
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    secs = int(total % 60)
    cents = int(round((total - int(total)) * 100))
    if cents >= 100:  # 四舍五入到 100 要进位，否则会出现 .100
        cents = 0
        secs += 1
    return f"{hours}:{minutes:02d}:{secs:02d}.{cents:02d}"


def ass_text(raw: object) -> str:
    """把一条字幕文本清成能放进 ASS 的样子。

    三件事：**换行转 `\\N`**（真换行会把 ASS 文件按行拆坏，而 libass 只是静默丢掉）、
    首尾空白去掉、**花括号转义**（`{` 会开启一个覆盖标签块，正文里出现就会被当成样式指令）。
    """
    text = str(raw if raw is not None else "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = text.strip()
    text = text.replace("{", r"\{").replace("}", r"\}")
    return text.replace("\n", r"\N")


# ------------------------------------------------------------------ 时间轴


def cues_from_spans(spans: list[tuple[float, float]], texts: list[object]) -> list[dict[str, object]]:
    """按「每一段在成片里占的区间」+「这一段的一句话」排时间轴。

    路由层该用这个而不是 `cues_from_shots`：区间由 `transitions.clip_spans()` 算出来，
    **已经考虑了转场把成片变短**。用「各段时长累加」的话，配了转场之后字幕会一段比一段
    提前，越往后偏得越多。
    """
    cues: list[dict[str, object]] = []
    for i, (start, end) in enumerate(spans):
        body = ass_text(texts[i] if i < len(texts) else "")
        if not body or end <= start:
            continue
        cues.append({"start": round(float(start), 3), "end": round(float(end), 3), "text": body})
    return cues


def cues_from_shots(shots: list[tuple[float, object]]) -> list[dict[str, object]]:
    """把「每镜时长 + 这一镜的台词」按顺序排成时间轴（硬切口径）。

    为什么按累加而不是让用户填时间码：镜头时长本来就已经定下来了（分镜表 / 逐镜视频
    都是按它跑的），再让用户手填一遍就是给他一次写错的机会，而且改镜头长度以后
    两处必然对不上。
    """
    spans: list[tuple[float, float]] = []
    cursor = 0.0
    for seconds, _text in shots:
        try:
            dur = max(0.0, float(seconds or 0.0))
        except (TypeError, ValueError):
            dur = 0.0
        spans.append((cursor, cursor + dur))
        cursor += dur
    return cues_from_spans(spans, [t for _s, t in shots])


def cue_total_seconds(cues: list[dict[str, object]]) -> float:
    """字幕覆盖到第几秒（用它和对齐成片时长）。"""
    return round(max((float(c["end"]) for c in cues), default=0.0), 3)


def check_cues(cues: list[dict[str, object]]) -> str:
    """这批字幕能不能烧。空串 = 可以；否则是一句可直接展示的理由。

    拦两种：**一条都没有**（用户其实忘了写内容）与**时间区间是反的/零长的**
    （libass 遇到会直接跳过那一条，而且不报错——静默少一句字幕比报错更难查）。
    单条内容为空的在这之前就已经被 `cues_from_shots` / 路由丢掉了，不算错。
    """
    if not cues:
        return "还没有字幕内容——先写几句台词，或者从已有的配音把文字带出来"
    for i, c in enumerate(cues, 1):
        try:
            start, end = float(c["start"]), float(c["end"])
        except (KeyError, TypeError, ValueError):
            return f"第 {i} 条字幕的时间轴读不出来"
        if end <= start:
            return (
                f"第 {i} 条字幕的结束时间（{end:g}s）不晚于开始时间（{start:g}s）。"
                "这一条会被静默丢掉——检查一下这一镜的时长"
            )
    return ""


# ------------------------------------------------------------------ ASS 生成


def build_ass(
    cues: list[dict[str, object]],
    *,
    style_key: object,
    width: int,
    height: int,
    size_scale: object = 1.0,
) -> str:
    """把字幕列表与一款版式拼成一份完整的 ASS 文本。

    尺寸全部**按画面高度换算成像素**（`PlayResX/Y` 就设成真实分辨率），所以同一套版式
    在 720p / 1080p / 4K 上表现一致；用户调的是「相对大小」，不是某个分辨率的绝对像素。
    """
    st = style(resolve_style(style_key)) or style(DEFAULT_STYLE)
    assert st is not None
    w = max(2, int(width or 1280))
    h = max(2, int(height or 720))
    scale = sanitize_scale(size_scale)

    font_name = subtitle_fonts.family_of(st["font"])
    font_size = max(8, int(round(h * float(st["size"]) * scale)))  # type: ignore[arg-type]
    outline = int(round(h * float(st["outline_w"])))  # type: ignore[arg-type]
    shadow = int(round(h * float(st["shadow"])))  # type: ignore[arg-type]
    margin_v = int(round(h * float(st["margin_v"])))  # type: ignore[arg-type]
    margin_h = int(round(w * float(st["margin_h"])))  # type: ignore[arg-type]
    spacing = float(st["spacing"]) * scale  # type: ignore[arg-type]
    box = st["box"]

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {w}",
        f"PlayResY: {h}",
        "WrapStyle: 2",  # 只按 \N 换行，不自动折行——自动折行会破坏版式的行数预期
        "ScaledBorderAndShadow: yes",  # 描边随分辨率缩放，否则 4K 上细到看不见
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    ]
    if box:
        # BorderStyle 3 = 用 BackColour 画一个不透明底板，衬在文字后面
        border_style, back = 3, ass_color(str(box))
    else:
        border_style, back = 1, "&H00000000"
    lines.append(
        "Style: Default,{font},{size},{primary},&H000000FF,{outline_c},{back},"
        "{bold},0,0,0,100,100,{spacing},0,{bs},{ow},{sh},{align},{ml},{mr},{mv},1".format(
            font=font_name, size=font_size,
            primary=ass_color(str(st["primary"])), outline_c=ass_color(str(st["outline"])),
            back=back, bold=1 if st["bold"] else 0, spacing=f"{spacing:g}",
            bs=border_style, ow=outline, sh=shadow,
            align=int(st["align"]),  # type: ignore[arg-type]
            ml=margin_h, mr=margin_h, mv=margin_v,
        )
    )
    lines += [
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for c in cues:
        text = ass_text(c.get("text"))
        if not text:
            continue
        lines.append(
            "Dialogue: 0,{s},{e},Default,,0,0,0,,{t}".format(
                s=ass_time(float(c["start"])), e=ass_time(float(c["end"])), t=text
            )
        )
    return "\n".join(lines) + "\n"


def style_options() -> list[dict[str, object]]:
    """给界面的版式列表：带上「这款要的字体在不在本机」。"""
    usable = set(subtitle_fonts.usable_keys())
    out: list[dict[str, object]] = []
    for s in STYLES:
        item = dict(s)
        item["fontReady"] = str(s["font"]) in usable
        item["fontLabel"] = str((subtitle_fonts.font(s["font"]) or {}).get("label") or s["font"])
        out.append(item)
    return out


def fonts_used(style_key: object) -> list[str]:
    """这款版式要用到哪几款字体。

    `stage_fonts` 只挂这几份、不整库挂——libass 会把目录里的字体**全量解析**
    （实测整库 80MB 要 0.16s、单份 0.08s），逐镜渲染时这个差价会累积。
    """
    st = style(resolve_style(style_key))
    return [str((st or {}).get("font") or subtitle_fonts.BUILTIN_KEY)]
