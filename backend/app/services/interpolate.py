"""补帧的纯口径：有哪些模型、能补到多少帧、命令行怎么拼、大概要多久。

与 `interpolate_run.py`（真跑子进程那一层）分工，与 `upscale.py` 同一条思路：
这边全是能在**没装引擎、没有显卡**的机器上测的东西。

## 四条口径（每条都是探针实测出来的，不是读文档抄的）

1. **`-n` 是「目标总帧数」，不是模型名。** 用法表里写的是
   `-n num-frame  target frame count (default=N*2)`，模型是另一个参数 `-m model-path`
   （默认 `rife-v2.3`）。**读错这一个参数，24→60 就会变成 24→1440**，
   所以这条是实测钉住的：8 帧输入给 `-n 20`，产物是**正好 20 帧**。

2. **只有 `rife-v4` / `rife-v4.6` 支持自定义帧数。** 其余 9 个模型给 `-n` 会被直接挡回来
   （报 `only rife-v4 model support custom numframe and timestep`，退出码 -1）。
   所以「能不能补到 60 帧」这件事**跟着模型走**——与超分那边「倍数跟着模型走」同一条，
   但更硬：这里不是贵不贵的问题，是能不能的问题。界面上选到只支持 2 倍的模型时，
   目标帧数必须被锁成 2× 且说明原因。

3. **它只认图片目录，不认视频。** `-i input-path` 明确写的是
   `input image directory (jpg/png/webp)`。所以视频补帧也是我们自己做三段式
   （拆帧 → 目录批量补帧 → 按新帧率合帧 + 原音轨拷回），与超分共用同一套脚手架。

4. **它有一个真的 CPU 档**（`-g -1`，用法表里明写），实测在这台机器的小尺寸上与独显相当
   （1.1s vs 1.0s）。但我们这一版**没有把它摆到界面上**：只在一种尺寸上量过，
   说不出大尺寸下会慢多少，而这一栏的选项是要用户照着选的。留在这里当记录。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.services import local_engines as le
from app.services.upscale import exe_path, le_install_dir  # 判据同一套：只按文件名匹配

ENGINE_KEY = "rife"

#: **只有这两个支持 `-n` / `-s`**（实测，见文件头口径 2）。
CUSTOM_FRAME_MODELS: tuple[str, ...] = ("rife-v4", "rife-v4.6")

# 模型目录名 → 给人看的中文名与一句话说明。
# 只写「怎么叫它」，不写「有没有它」——有没有由盘上的目录决定。
_MODEL_TEXT: dict[str, tuple[str, str]] = {
    "rife-v4.6": ("通用（最新）", "v4 架构的最新版，又快又稳，默认用它"),
    "rife-v4": ("通用（v4）", "v4 架构的初版，与 4.6 同源"),
    "rife-anime": ("动画 / 插画", "为动画调过，二次元素材抖动更小"),
    "rife-v2.3": ("旧版默认", "上游把这一版设成了默认，比 v4 慢、效果也差一档"),
    "rife-v2.4": ("旧版 v2.4", ""),
    "rife-v3.0": ("旧版 v3.0", ""),
    "rife-v3.1": ("旧版 v3.1", ""),
    "rife-v2": ("旧版 v2", ""),
    "rife-HD": ("高清档", "给 720p 以上的素材调的"),
    "rife-UHD": ("超高清档", "给 4K 素材调的"),
    "rife": ("最早期", ""),
}

#: 2 倍之外，界面能给的常见目标帧率。**不按 2 的幂限死**——
#: 24→60 是最常见的诉求，而它只有 v4 系做得到（这正是口径 2 的意义）。
COMMON_TARGETS: tuple[int, ...] = (24, 25, 30, 48, 50, 60, 120)

# 输出帧数上限：一次最多让它吐多少帧。**与超分共用同一档**（1800），
# 但那边的上限是「能拆多少」，这边的上限是「能吐多少」，所以另算一个更保守的。
MAX_OUT_FRAMES = 3600
# 放大倍数上限（补帧只提高时间上的密度，不该改画幅；这是防手滑）
MAX_FACTOR = 8.0


@dataclass(frozen=True)
class Model:
    """一个可选的补帧模型。`custom` 决定它能不能补到任意帧数。"""

    key: str
    label: str
    note: str
    #: 能不能用 `-n` 指定目标总帧数（只有 v4 系可以）
    custom: bool
    #: 只支持 2 倍时，界面要把它写出来
    note_2x_only: str = ""


def models() -> list[Model]:
    """这台机器上真能用的模型（目录里带 contextnet/flownet/fusionnet 或 flownet 的那种）。

    **判据是盘上的目录**：上游哪个版本增删了模型，界面跟着变，不写死清单。
    """
    exe = exe_path(ENGINE_KEY)
    if exe is None:
        return []
    out: list[Model] = []
    for child in sorted(exe.parent.iterdir()):
        if not child.is_dir():
            continue
        params = {p.name for p in child.glob("*.param")}
        # v1~v3 系是「contextnet + flownet + fusionnet」三个；v4 系只有 flownet
        if not params or not ({"flownet.param"} <= params):
            continue
        label, note = _MODEL_TEXT.get(child.name, (child.name, ""))
        custom = child.name in CUSTOM_FRAME_MODELS
        out.append(Model(
            key=child.name, label=label, note=note, custom=custom,
            note_2x_only="" if custom else "这一版只做 2 倍（自定义帧数只有 v4 系支持）",
        ))
    return out


def find_model(name: str) -> Model | None:
    want = str(name or "").strip()
    for m in models():
        if m.key == want:
            return m
    return None


def default_model() -> str:
    """默认 `rife-v4.6`（最新、也支持自定义帧数）。

    空表示一个都没有——**不硬编一个可能不存在的名字**。
    """
    available = models()
    if not available:
        return ""
    for want in ("rife-v4.6", "rife-v4"):
        if any(m.key == want for m in available):
            return want
    return available[0].key


def target_counts(src_fps: float, *, model: Model) -> list[int]:
    """界面能选的目标帧率。

    只做 2 倍的模型就只有一个选项（2 倍），能自定义的给一列常见帧率 + 2 倍。
    **不是所有模型都给 60**——给不出来却列在界面上，就是「选了就报错」。
    """
    fps = float(src_fps or 0)
    two = int(round(fps * 2)) if fps else 0
    if not model.custom:
        return [two] if two else []
    out: list[int] = []
    for t in COMMON_TARGETS:
        if t > two:
            out.append(t)
    if two:
        out.append(two)
    return sorted(set(out))


def factor_text(src_fps: float, target: int) -> str:
    """把「目标帧率」说成用户能核对的一句话：24 → 60 是补 2.5 倍。"""
    if not src_fps:
        return ""
    return f"{src_fps:g} → {target} 帧（补 {target / float(src_fps):.2f} 倍）"


def out_frames(src_frames: int, src_fps: float, target: int) -> int:
    """按目标帧率算要吐出多少帧。

    **按时长算，不按「输入帧数 × 倍数」算**：`-n` 要的是总帧数，
    而用户想的是「把这段 24 帧的片子变成 60 帧的」。两者在时长不变时应当一致，
    差一帧的地方以输入帧数为准（多出来的那一点时间由合帧时的帧率吸收）。
    """
    if src_frames <= 0 or src_fps <= 0 or target <= 0:
        return 0
    seconds = src_frames / float(src_fps)
    return max(1, int(round(seconds * target)))


def target_problem(src_frames: int, src_fps: float, target: int, *, model: Model) -> str:
    """这一组能不能做；不能就回一句人话（空串表示能做）。"""
    if src_frames <= 0 or src_fps <= 0:
        return "读不出这段的帧率或帧数，没法补帧。"
    if target <= src_fps:
        return (f"目标 {target} 帧不高于原来的 {src_fps:g} 帧——补帧是把密度提高，"
                "目标是降帧的话请用别的方式。")
    if not model.custom and target != int(round(src_fps * 2)):
        return (f"「{model.label}」只做 2 倍（也就是 {int(round(src_fps * 2))} 帧）。"
                "要补到任意帧数请选 v4 系（rife-v4 / rife-v4.6）——"
                "上游对别的模型直接拒绝这个参数。")
    f = target / float(src_fps)
    if f > MAX_FACTOR:
        return f"{target} 帧是原来的 {f:.1f} 倍，超过一次能补的 {MAX_FACTOR:g} 倍。"
    n = out_frames(src_frames, src_fps, target)
    if n > MAX_OUT_FRAMES:
        return (f"补完是 {n} 帧，超过一次能处理的 {MAX_OUT_FRAMES} 帧。"
                "先剪短一点，或者把目标帧率降一档。")
    return ""


# ------------------------------------------------------------------ 命令行


def plan_dir_args(
    *,
    in_dir: Path,
    out_dir: Path,
    model: str,
    src_frames: int,
    src_fps: float,
    target: int,
    gpu: int = -1,
) -> list[str]:
    """目录批量补帧的命令行。

    - `-m` 给模型目录名（**不是** `-n`）；
    - `-n` 只在模型支持时给，且给的是**总帧数**（口径 1）；
    - `-f` 与超分那边同理：目录输出要按扩展名判格式，不给我就得靠输出名的后缀猜。
    """
    exe = exe_path(ENGINE_KEY)
    if exe is None:
        raise ValueError("补帧引擎还没装好")
    args = [str(exe), "-i", str(in_dir), "-o", str(out_dir), "-m", str(model),
            "-f", "%08d.png"]
    spec = find_model(model)
    if spec is not None and spec.custom:
        n = out_frames(src_frames, src_fps, target)
        if n > 0:
            args += ["-n", str(n)]
    if gpu is not None and int(gpu) >= 0:
        args += ["-g", str(int(gpu))]
    return args


def expected_out_frames(model: str, src_frames: int, src_fps: float, target: int) -> int:
    """这一次应当产出多少帧。

    不能自定义帧数的模型只会做 2 倍——**按它实际会给的算**，
    不然产物对帐会一直失败（上游给 2N 帧而我们期待 N×倍数）。
    """
    spec = find_model(model)
    if spec is not None and not spec.custom:
        return src_frames * 2
    return out_frames(src_frames, src_fps, target)


def out_fps(target: int, *, model: str, src_fps: float) -> float:
    """合帧时该按什么帧率写。

    与我们请求的目标帧率一致；但只做 2 倍的模型实际给的是 2N 帧，
    所以按「实际帧数 ÷ 原时长」回推，**不硬写目标帧率**——写错了片子就会变速。
    """
    spec = find_model(model)
    if spec is not None and not spec.custom:
        return float(src_fps) * 2.0
    return float(target)


# ------------------------------------------------------------------ 上限与预估

# 实测（RTX 5060 Laptop，-g 0，2026-09-23）：1280x720 下每个**输入帧**的耗时。
#   rife-v4.6 0.27s   rife-anime 0.44s   rife-v2.3 0.48s
# 只按「输入帧数」计费：补出来的帧是模型一次算出来的，不等于再跑一遍。
_COST_PER_INPUT_FRAME_MP: dict[str, float] = {
    "rife-v4.6": 0.29, "rife-v4": 0.29,
    "rife-anime": 0.48, "rife-HD": 0.48, "rife-UHD": 0.48,
}
#: 每百万像素、每个输入帧的秒数；没量过的模型按偏慢的一档估（估久比估短好）
_COST_FALLBACK = 0.55


def estimate_seconds(model: str, width: int, height: int, src_frames: int) -> int:
    """粗估要跑多久（秒）。**只是估算**，常数来自一台机器的实测。"""
    per = _COST_PER_INPUT_FRAME_MP.get(str(model), _COST_FALLBACK)
    mp = max(0.0, float(width) * float(height)) / 1_000_000.0
    return int(max(1, src_frames) * per * mp + 1.5)  # +1.5 是加载模型的启动开销


def estimate_range(model: str, width: int, height: int, src_frames: int) -> tuple[int, int]:
    mid = estimate_seconds(model, width, height, src_frames)
    return max(1, int(mid * 0.5)), max(2, int(mid * 2.0))


def estimate_text(model: str, width: int, height: int, src_frames: int) -> str:
    from app.services.upscale import human_seconds

    lo, hi = estimate_range(model, width, height, src_frames)
    return (f"{src_frames} 帧过一遍，这台机器上大约 "
            f"{human_seconds(lo)}–{human_seconds(hi)}（以实际进度为准）")


# ------------------------------------------------------------------ 就绪判断


def check_ready(*, installed: set[str], vulkan: bool) -> tuple[bool, str, str, str]:
    """`(能不能用, 原因, 该做什么, 跳到哪一页)`。按顺序说第一个卡住的原因。"""
    engine = le.by_key(ENGINE_KEY)
    label = engine.label if engine else ENGINE_KEY
    if ENGINE_KEY not in installed or exe_path(ENGINE_KEY) is None:
        return (False,
                f"还没装「{label}」。它走显卡，免安装、免 Python、免 CUDA。",
                "到「本机引擎」页下载它", "engines")
    if not vulkan:
        return (False,
                "这个引擎走 Vulkan，而这台机器上没有可用的 Vulkan 运行时，装了也起不来。",
                "装显卡驱动（含 Vulkan 运行时）之后再来", "engines")
    if not models():
        return (False,
                "「补帧」的目录里没找到模型文件（每个模型目录下要有 flownet.param）。",
                "到「本机引擎」页删掉重新下一次", "engines")
    return (True, "", "", "")
