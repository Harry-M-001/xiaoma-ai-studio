"""去字幕：把模型漂移出来的硬字幕从画面里拿掉。

**为什么要有这一层**：有些视频模型会在画面里烧上一行字幕，效果往往不如后期自己配的。
用户要的是「先去干净，再配我的」——而字幕烧进去之后就是像素了，只能靠画面处理把它盖掉。

## 四种手法，先实测再定（三段探针量出来的，不是猜的）

在 640x360 上造「本来就没字」与「烧了字」两版，对**不改画面几何**的手法量字幕区
PSNR（越高越接近「从来就没字」）：

| 底图 | 不去 | 抹平 | 遮住 | 模糊 |
| --- | --- | --- | --- | --- |
| 竖直渐变（天空/墙/虚化） | 17.5 | **41.6** | 33.5 | 27.4 |
| 纹理密集（testsrc2） | 14.8 | **20.3** | 17.9 | 16.8 |

四条结论（每条都影响界面怎么写）：

1. **抹平在平整背景上几乎完美**（17.5 → 41.6 dB），纹理密集时提升有限。
2. **抹平的框不能碰画面四边**：碰了就 `Conversion failed`（实测 x=0、y+h=画面高都失败，
   留 1px 就成）。字幕贴着画面底边是最常见的情形，所以这里**自动内缩 1px** 并把这件事
   说给用户——不缩就用不了，缩了那一行原样保留（1 像素，实际看不出来）。
3. **遮住（把框上方那条拉下来盖住）任何位置都能用**，平整背景 33.5 dB，比模糊好；
   纹理密集时会把上面的内容拉出一道痕。
4. **模糊最稳但一定会留下一条糊痕**；**裁掉最干净但画面会变矮**——裁掉改了几何，
   跨几何比 PSNR 没有意义（第二轮实测过：裁掉拉伸在字幕区里反而「更不像」，因为整幅都错位了），
   所以这一条只能靠人眼看，界面上给真帧对比。

## 一个不报错但等于没做的坑

**零宽 / 零高的框 ffmpeg 不报错**（实测 `delogo=w=0` 退出码 0），也就是说用户框成一条线时
会「成功」且什么也没做。所以最小边由 `MIN_SIDE` 自己拦，不指望 ffmpeg。
"""

from __future__ import annotations

from typing import Any

# 框的最小边（像素）。再小就是误触；而且退化框 ffmpeg 不报错，只能自己拦。
MIN_SIDE = 8
# 抹平要求框不碰画面边缘，自动内缩这么多（实测 1px 就够）
DELOGO_INSET = 1
# 「遮住」往上/下借的那条有多高、与被盖区域隔开多少（免得把框的上边线也拉进来）
COVER_STRIP = 6
COVER_GAP = 4
# 判定「框是不是碰到了这条边」的容差：字幕带与边缘差一两像素也算贴边
EDGE_TOL = 2

METHODS: tuple[dict[str, Any], ...] = (
    {
        "key": "delogo",
        "label": "抹平",
        "short": "按四周的像素推回来填",
        "hint": "背景平整（天空 / 墙 / 虚化）时几乎看不出来；背景有横向色块或细密纹理时"
                "会留下竖向拖痕。框贴到画面边缘会自动内缩 1px（ffmpeg 的硬要求）。",
    },
    {
        "key": "cover",
        "label": "遮住",
        "short": "把紧邻的一条画面拉过来盖住",
        "hint": "画面尺寸不变，任何位置都能用（包括贴着画面底边的字幕）。"
                "竖直渐变上最自然；把画面里一条有内容的边搭过去时会拉出一道痕。",
    },
    {
        "key": "blur",
        "label": "模糊",
        "short": "把这一条糊掉",
        "hint": "最稳、任何位置都能用，字一定看不见了——但那条糊痕一定在。"
                "拿不准时先用它，再用真帧预览和另外两个比一比。",
    },
    {
        "key": "crop",
        "label": "裁掉",
        "short": "把这一条从画面里剪掉",
        "hint": "最干净：那一条直接不存在了。代价是画面会变矮（1080p 裁掉 15% 就是 918p），"
                "而且只有框贴着画面上下边缘时才是「一刀切干净」——框在画面中间时不让用。",
    },
)

_BY_KEY = {str(m["key"]): m for m in METHODS}


def by_key(key: object) -> dict[str, Any] | None:
    return _BY_KEY.get(str(key or "").strip())


def labels() -> list[str]:
    return [str(m["label"]) for m in METHODS]


def _even(n: float) -> int:
    """取偶数：编码器要偶数尺寸，宽高都是偶数时裁切/缩放的取值也少一类意外。"""
    return int(n) - (int(n) % 2)


def default_box(width: int, height: int) -> dict[str, int]:
    """推荐的默认框：画面底部**一条**（模型烧的字幕九成在这儿）。

    宽度铺满、高度按画面高取 16%（夹在 24px 与画面高的 1/3 之间）。
    铺满宽度是有意的：字幕多半居中但长度不定，让用户从「一条」开始缩比从「一个点」
    开始拉容易得多。
    """
    w = max(MIN_SIDE, _even(width))
    h = _even(max(24, min(height / 3, height * 0.16)))
    h = max(MIN_SIDE, h)
    y = max(0, _even(height - h))
    return {"x": 0, "y": y, "w": w, "h": h}


def sanitize_box(raw: object, width: int, height: int) -> dict[str, int]:
    """把前端传来的框夹进画面、并取偶数。**故意很宽容**：宁可把框挪回去一点，
    也不要在用户拖到边界时报错。"""
    box = raw if isinstance(raw, dict) else {}

    def pick(name: str, fallback: int) -> int:
        try:
            return int(float(box.get(name, fallback)))  # type: ignore[union-attr]
        except (TypeError, ValueError):
            return fallback

    w = max(MIN_SIDE, _even(pick("w", width)))
    h = max(MIN_SIDE, _even(pick("h", height)))
    w = min(w, _even(width))
    h = min(h, _even(height))
    x = min(max(0, _even(pick("x", 0))), max(0, _even(width) - w))
    y = min(max(0, _even(pick("y", 0))), max(0, _even(height) - h))
    return {"x": x, "y": y, "w": w, "h": h}


def box_problem(box: dict[str, int], width: int, height: int) -> str:
    """框本身有没有问题（空串 = 没问题）。"""
    if width < MIN_SIDE or height < MIN_SIDE:
        return f"画面太小（{width}x{height}），去字幕做不了"
    if box["w"] < MIN_SIDE or box["h"] < MIN_SIDE:
        return f"框太小了：宽高都要 ≥ {MIN_SIDE} 像素（框成一条线时 ffmpeg 会「成功」但什么也没做）"
    if box["x"] + box["w"] > width or box["y"] + box["h"] > height:
        return "框越出了画面"
    return ""


def edges_of(box: dict[str, int], width: int, height: int) -> list[str]:
    """框贴到了哪几条边（用来决定「裁掉」能不能用、以及提醒用户抹平会内缩）。"""
    hit: list[str] = []
    if box["x"] <= EDGE_TOL:
        hit.append("left")
    if box["y"] <= EDGE_TOL:
        hit.append("top")
    if box["x"] + box["w"] >= width - EDGE_TOL:
        hit.append("right")
    if box["y"] + box["h"] >= height - EDGE_TOL:
        hit.append("bottom")
    return hit


def _crop_args(box: dict[str, int], width: int, height: int) -> list[str] | None:
    """「裁掉」的滤镜：框贴上边缘 → 从框的上边切；贴下边缘 → 从框的上边切。

    两种都是**一刀切**（保留一侧、丢掉另一侧），而不是「把中间挖掉再对接」——
    中间挖掉再对接会把上下两条不相干的画面硬接在一起，看着比一条糊痕更糟。
    所以框不在画面上下边缘时直接不让用（见 `method_rows`）。
    """
    if box["y"] + box["h"] >= height - EDGE_TOL:
        keep = box["y"]
        if keep >= MIN_SIDE:
            return ["-vf", f"crop={width}:{keep}:0:0"]
    if box["y"] <= EDGE_TOL:
        keep = height - (box["y"] + box["h"])
        if keep >= MIN_SIDE:
            return ["-vf", f"crop={width}:{keep}:0:{box['y'] + box['h']}"]
    return None


def _cover_args(box: dict[str, int], width: int, height: int) -> list[str] | None:
    """「遮住」的滤镜：优先借框上方那条，上方不够就借下方那条。"""
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    above = y - COVER_GAP - COVER_STRIP
    below = y + h + COVER_GAP
    if above >= 0:
        src_y = above
    elif below + COVER_STRIP <= height:
        src_y = below
    else:
        return None
    return [
        "-filter_complex",
        f"[0:v]crop={w}:{COVER_STRIP}:{x}:{src_y},scale={w}:{h}[band];"
        f"[0:v][band]overlay={x}:{y}",
    ]


def _blur_args(box: dict[str, int], width: int, height: int) -> list[str]:
    """「模糊」的滤镜。半径按框高定，并夹在 boxblur 能接受的范围内。

    夹上限是必要的：boxblur 的半径不能相对被模糊的那块太大（会 `Conversion failed`），
    而框可能只有 8px 高——那种尺寸下半径还按比例算就会炸。
    """
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    radius = max(1, min(40, round(h / 5), max(1, min(w, h) // 2 - 1)))
    return [
        "-filter_complex",
        f"[0:v]crop={w}:{h}:{x}:{y},boxblur={radius}:2[band];[0:v][band]overlay={x}:{y}",
    ]


def _delogo_args(box: dict[str, int], width: int, height: int) -> list[str]:
    """「抹平」的滤镜，带 1px 内缩（不然贴边时 ffmpeg 直接失败）。"""
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    # 只往「确实贴到了边」的那几条边内缩：能多留一点真实像素就多留一点
    ix = x + DELOGO_INSET if x <= EDGE_TOL else x
    iy = y + DELOGO_INSET if y <= EDGE_TOL else y
    iw = w - DELOGO_INSET if x + w >= width - EDGE_TOL else w
    ih = h - DELOGO_INSET if y + h >= height - EDGE_TOL else h
    iw = max(3, _even(iw))
    ih = max(3, _even(ih))
    return ["-vf", f"delogo=x={ix}:y={iy}:w={iw}:h={ih}"]


def filter_args(method: str, box: dict[str, int], width: int, height: int) -> list[str]:
    """这一手法对应的 ffmpeg 参数（已经过几何检查；本函数不再校验）。"""
    if method == "delogo":
        return _delogo_args(box, width, height)
    if method == "cover":
        args = _cover_args(box, width, height)
        if args is None:
            raise ValueError("框占满了画面高度，没有可借用的相邻画面")
        return args
    if method == "crop":
        args = _crop_args(box, width, height)
        if args is None:
            raise ValueError("框不在画面上下边缘，裁掉会让上下两条画面错位")
        return args
    if method == "blur":
        return _blur_args(box, width, height)
    raise ValueError(f"不认识的手法：{method}")


def method_rows(box: dict[str, int], width: int, height: int) -> list[dict[str, Any]]:
    """四种手法 + 在当前这个框上**能不能用、为什么不能**。

    不能用的要给出原因（而不是静默地从列表里消失）：用户会想知道「抹平为什么选不了」。
    """
    rows: list[dict[str, Any]] = []
    edges = edges_of(box, width, height)
    for spec in METHODS:
        row: dict[str, Any] = {
            "key": spec["key"],
            "label": spec["label"],
            "short": spec["short"],
            "hint": spec["hint"],
            "available": True,
            "reason": "",
        }
        if spec["key"] == "crop" and _crop_args(box, width, height) is None:
            row["available"] = False
            row["reason"] = (
                "框不在画面上下边缘。把中间一条挖掉再上下对接，画面会错位——"
                "想让画面完整就用抹平或遮住。"
            )
        if spec["key"] == "cover" and _cover_args(box, width, height) is None:
            row["available"] = False
            row["reason"] = "框占满了整个画面高度，没有相邻的画面可以借来盖住它。"
        if spec["key"] == "delogo" and edges:
            # 不是不可用，而是要说明它会内缩（不说的话，贴边用户会以为程序坏了）
            row["hint"] = (
                "框贴到了画面边缘，抹平会自动内缩 1px 并把那一行原样保留"
                "（ffmpeg 要求抹平的框不能碰边缘，否则直接报错）。"
            ) + "背景平整（天空 / 墙 / 虚化）时几乎看不出来；有横向色块或细密纹理时会留下竖向拖痕。"
        rows.append(row)
    return rows


def default_method(box: dict[str, int], width: int, height: int) -> str:
    """默认手法：**抹平**（两种底图上实测都是最高的那个）。

    只在抹平不可用时才退：框占满画面高度时抹平照样能用（内缩 1px 即可），
    所以这里基本总是回 `delogo`。
    """
    for spec in METHODS:
        if spec["key"] == "delogo":
            return str(spec["key"])
    return "blur"


def as_dicts() -> list[dict[str, Any]]:
    """给接口/界面的手法清单（不含滤镜细节，那是后端的事）。"""
    return [dict(m) for m in METHODS]


def assert_consistent() -> None:
    keys = [str(m["key"]) for m in METHODS]
    if len(keys) != len(set(keys)):
        raise RuntimeError(f"手法 key 重复：{keys}")
    if "delogo" not in keys:
        raise RuntimeError("抹平是默认手法，不能在表里去掉")
    for m in METHODS:
        if not m["label"] or not m["short"] or not m["hint"]:
            raise RuntimeError(f"{m['key']} 缺文案（label / short / hint 都要有）")
