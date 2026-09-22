"""超分的纯口径：走哪几条路、每条能用什么模型与倍数、命令行怎么拼、大概要多久。

与 `upscale_run.py`（真跑子进程那一层）分工：这边全是可以在**没有引擎、没有显卡**的
机器上测的东西——路线表、模型清单、命令行拼装、设备解析、上限与预估、错误文案。

## 六条口径（每条都对应一个探针实测出来的事实，不是读文档抄的）

1. **两档都只认图片，不认视频。** 实测 `-i x.mp4` 一律被拒：realesrgan 报
   `invalid outputpath extension type`，waifu2x 对图片目录之外的输入直接说
   `invalid outputpath extension type`。所以**视频超分是我们自己做三段式**：
   ffmpeg 拆帧 → 目录批量放大 → 合帧并把**原音轨拷回来**。上游 realesrgan 包里的
   `README_windows.md` 写的就是这三步（它的示例把帧率写死成 23.98，那是它自己 demo 的
   帧率——照抄会把用户的视频改成另一个速度，所以帧率必须按 `probe()` 读回来的真实值回填）。
2. **模型清单只能来自盘上真有的文件。** 上游用法表里列了 `realesrnet-x4plus`，而这个包里
   没有那个 param 文件——照着用法表给用户，就是「选了就报错」。
3. **倍数菜单跟着模型走，不跟着引擎走。** waifu2x 的两个 upconv 模型没有 `noise0_model.param`，
   所以 1 倍跑不起来（实测 rc=3221226505，报 `_wfopen ... noise0_model.param failed`）；
   cunet 有，所以只有 cunet 才有 1 倍这一档。
4. **`-g` 要显式给，而且这件事不能交给「自动」。** 同一个引擎换块卡差好几倍：
   实测这台机器（RTX 5060 Laptop + Intel 集显）上 waifu2x 选集显慢 **22 倍**（0.7s → 15.4s）、
   realesrgan 慢 **3.3 倍**（2.2s → 7.3s）。设备清单可以从 exe 自己的输出里读出来（见
   `parse_devices`）：它一启动就把每个设备的能力打一遍。
5. **退出码 0 也可能是失败。** 实测 `decode image ... failed` 那次 rc 就是 0。
   所以判据一律是「产物在不在、尺寸对不对」，不看退出码——与 v1.1.30 本机配音同一条。
6. **视频的预估只能给个范围。** 逐帧跑，重的模型一段几十秒的片子要几十分钟。
   宁可笑话说在前面（界面上写「大约 12–32 分钟」），也不要让用户点完才发现要等一小时。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.services import local_engines as le

# ------------------------------------------------------------------ 路线

ROUTE_COMFY = "comfyui"
COMFY_LABEL = "走我的 ComfyUI"

# 顺序就是界面上的顺序：Real-ESRGAN 更通用（而且它是唯一对视频调过的），放前面。
LOCAL_ENGINE_ORDER: tuple[str, ...] = ("realesrgan", "waifu2x")


def local_route(engine_key: str) -> str:
    """本机路线的 key 形如 `local:realesrgan`。"""
    return f"local:{engine_key}"


def engine_of(route: str) -> str:
    """从路线 key 取出引擎 key；`comfyui` 或认不出的一律返回空串。"""
    text = str(route or "").strip()
    if not text.startswith("local:"):
        return ""
    key = text.split(":", 1)[1]
    return key if key in LOCAL_ENGINE_ORDER else ""


def is_comfy(route: str) -> bool:
    return str(route or "").strip() == ROUTE_COMFY


def exe_path(engine_key: str) -> Path | None:
    """装好的那个 exe 的绝对路径；没装返回 None。

    判据与 v1.1.29 一致：`marker_hit` 只按**文件名**匹配，所以这里把命中的相对路径
    拼回安装目录，不写死 `bin/` 之类的层级。
    """
    engine = le.by_key(engine_key)
    if engine is None:
        return None
    root = le_install_dir(engine_key)
    hit = le.marker_hit(engine, root)
    if not hit:
        return None
    path = root / hit
    return path if path.is_file() else None


def le_install_dir(engine_key: str) -> Path:
    """单独包一层是为了让这个模块在测试里能换根目录（真实现见 `engine_install`）。"""
    from app.services import engine_install as ei

    return ei.install_dir(engine_key)


# ------------------------------------------------------------------ 模型


@dataclass(frozen=True)
class Model:
    """一个可选的模型。`scales` 是**它真能跑的倍数**（实测过，见文件头口径 3）。"""

    key: str
    label: str
    note: str
    scales: tuple[int, ...]
    #: 是否适合视频逐帧（realesrgan 的 animevideov3 是专门为视频调的）
    video_ok: bool = True
    #: 每个倍数是不是各有一份**独立网络**。它决定「倍数越高越贵」还是「三档同价」：
    #: `realesr-animevideov3` 带 -x2/-x3/-x4 三份 param，所以 4 倍比 2 倍贵约 3.4 倍；
    #: 而 `realesrgan-x4plus` 只有一份 x4 网络，2/3/4 倍都是跑它再缩放，
    #: 实测三档的每帧耗时几乎一样（比值 0.88~0.99）。**这一条是从盘上的文件推出来的**，
    #: 不是按模型名硬编的。
    per_scale_network: bool = True


# `-n` / `-m` 的名字 → 给人看的中文名与一句话说明。
# **这里只写「怎么叫它」，不写「有没有它」**——有没有由盘上的文件决定（口径 2）。
_MODEL_TEXT: dict[str, tuple[str, str, bool]] = {
    # realesrgan（-n 的名字）
    "realesrgan-x4plus": ("通用照片 / 生成图", "真实照片与 AI 生成图都吃，最通用的一档", True),
    "realesrgan-x4plus-anime": ("动漫插画", "二次元插画、线条干净的图更合适", True),
    "realesr-animevideov3": ("视频逐帧", "为视频调的模型，逐帧跑起来画面抖动最小", True),
    # waifu2x（-m 的目录名）
    "models-cunet": ("通用（cunet）", "waifu2x 的默认档，速度快、噪声图也稳", True),
    "models-upconv_7_anime_style_art_rgb": ("动漫（upconv）", "老牌动漫模型，线条锐利", True),
    "models-upconv_7_photo": ("照片（upconv）", "偏真实照片的一张", True),
}

# realesrgan 的 `-s` 可以给 2/3/4（用法表里就是这么写的），而且**三个倍数实测全通**，
# 与「这个包里有没有 -x2/-x3 文件」无关——`realesrgan-x4plus` 只有一个 param，
# 2/3 倍照样出图（工具自己会缩放）。所以这里不按文件推断，直接给实测过的三档。
_REALESRGAN_SCALES: tuple[int, ...] = (2, 3, 4)


def _realesrgan_models(root: Path) -> list[Model]:
    """`-n` 的候选 = `models/*.param` 去掉 `-x2/-x3/-x4` 与后缀。

    不扫 `models` 之外的目录：上游哪天把模型放到别处，`-n` 也找不到。
    """
    models_dir = root / "models"
    if not models_dir.is_dir():
        # 解压时会把「只有一层顶层目录」的包拆平，所以也可能直接就放在根下
        models_dir = root
    stems: dict[str, bool] = {}
    for param in models_dir.glob("*.param"):
        name = param.name[: -len(".param")]
        per_scale = bool(re.search(r"-x[0-9]+$", name))
        stem = re.sub(r"-x[0-9]+$", "", name)
        stems[stem] = stems.get(stem, False) or per_scale
    out: list[Model] = []
    for name in sorted(stems):
        label, note, video_ok = _MODEL_TEXT.get(name, (name, "", True))
        out.append(Model(key=name, label=label, note=note,
                         scales=_REALESRGAN_SCALES, video_ok=video_ok,
                         per_scale_network=stems[name]))
    return out


def _waifu2x_models(root: Path) -> list[Model]:
    """`-m` 的候选 = 根下**带 .param 的目录**（waifu2x 的模型是按目录给的）。"""
    if not root.is_dir():
        return []
    out: list[Model] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not any(child.glob("*.param")):
            continue
        scales: list[int] = []
        if (child / "noise0_model.param").is_file():
            # 1 倍（只降噪不放大）靠这个文件；两个 upconv 目录都没有它，所以那里的 1 倍跑不起来
            scales.append(1)
        if any(child.glob("scale2.0x_model.param")):
            # 4 倍是 2 倍叠两次，两个实测过的目录都能出图；没有 2 倍的目录我们也不给 4 倍
            scales += [2, 4]
        if not scales:
            # 一个倍数都给不出来的目录不列给用户（列了就是「选了就报错」）
            continue
        label, note, video_ok = _MODEL_TEXT.get(child.name, (child.name, "", True))
        out.append(Model(key=child.name, label=label, note=note,
                         scales=tuple(scales), video_ok=video_ok))
    return out


def models(engine_key: str) -> list[Model]:
    """这台机器上这个引擎真能用的模型（空表示没装或装得不全）。"""
    exe = exe_path(engine_key)
    if exe is None:
        return []
    root = exe.parent
    if engine_key == "realesrgan":
        return _realesrgan_models(root)
    if engine_key == "waifu2x":
        return _waifu2x_models(root)
    return []


def find_model(engine_key: str, name: str) -> Model | None:
    want = str(name or "").strip()
    for m in models(engine_key):
        if m.key == want:
            return m
    return None


def default_model(engine_key: str, *, video: bool = False) -> str:
    """默认选哪个模型。空表示这个引擎现在一个都不可用。

    视频优先 `realesr-animevideov3`（它就是为逐帧调的）；图片优先最通用的那张。
    都没命中就取第一个可用的——**不硬编一个可能不存在的名字**。
    """
    available = models(engine_key)
    if not available:
        return ""
    order = (["realesr-animevideov3", "realesrgan-x4plus"] if video
             else ["realesrgan-x4plus", "models-cunet"])
    for want in order:
        if any(m.key == want for m in available):
            return want
    return available[0].key


def default_scale(model: Model) -> int:
    """默认倍数：优先 2 倍（最能看出差别又不至于太大），没有就取最小的。"""
    return 2 if 2 in model.scales else model.scales[0]


# ------------------------------------------------------------------ 命令行


def plan_image_args(
    engine_key: str,
    *,
    src: Path,
    out: Path,
    model: str,
    scale: int,
    gpu: int = -1,
    tta: bool = False,
) -> list[str]:
    """单张图片放大的命令行（纯函数，好测）。"""
    exe = exe_path(engine_key)
    if exe is None:
        raise ValueError(f"这个引擎还没装好：{engine_key}")
    args = [str(exe), "-i", str(src), "-o", str(out), *_model_args(engine_key, model)]
    args += ["-s", str(int(scale))]
    if gpu is not None and int(gpu) >= 0:
        args += ["-g", str(int(gpu))]
    if tta:
        args.append("-x")
    return args


def plan_dir_args(
    engine_key: str,
    *,
    in_dir: Path,
    out_dir: Path,
    model: str,
    scale: int,
    gpu: int = -1,
) -> list[str]:
    """目录批量放大的命令行（视频那条三段式的中间一步）。

    **目录模式必须给 `-f`**：它按输出名的后缀判格式，而目录名没有后缀。
    实测输出目录不存在时会直接报 `invalid outputpath extension type`，所以
    `upscale_run` 那边会先把输出目录建出来。
    """
    exe = exe_path(engine_key)
    if exe is None:
        raise ValueError(f"这个引擎还没装好：{engine_key}")
    args = [str(exe), "-i", str(in_dir), "-o", str(out_dir),
            *_model_args(engine_key, model), "-s", str(int(scale)), "-f", "png"]
    if gpu is not None and int(gpu) >= 0:
        args += ["-g", str(int(gpu))]
    return args


def _model_args(engine_key: str, model: str) -> list[str]:
    """`-n`（realesrgan）与 `-m`（waifu2x）不是一回事，参数名也不同。"""
    name = str(model or "").strip()
    if engine_key == "realesrgan":
        return ["-n", name] if name else []
    if engine_key == "waifu2x":
        return ["-m", name] if name else []
    return []


# ------------------------------------------------------------------ 设备


_DEVICE_LINE = re.compile(r"^\[(\d+)\s+([^\]]+)\]\s*(.*)$")
# 计算队列数：`queueC=2[8]`（独显）/ `queueC=0[1]`（集显）。**两档都会打这一项**。
_QUEUE = re.compile(r"queueC=(\d+)")
# 矩阵乘（coop-matrix）支持：`fp16-cm=16x16x16/...`（有）/ `fp16-cm=0`（没有）。
# **只有新版 ncnn 才打这一行**——实测 realesrgan（2022 年的包）整行都没有，
# 所以它只能当「有就更好」的加分项，不能当判据。
_COOP_MATRIX = re.compile(r"(fp16|int8|bf16)-cm=([0-9x/]+)")


@dataclass(frozen=True)
class Device:
    """exe 报出来的一个计算设备。字段都是它自己打的，没有一个是猜的。"""

    id: int
    name: str
    #: 计算队列数（`queueC`）。0 通常意味着这块是集显
    queues: int = 0
    #: exe 报出的矩阵乘能力（非 0 才有值）；老 ncnn 不打这一项，所以可能是空
    coop: str = ""
    #: 名字看着像独立显卡
    discrete: bool = False

    @property
    def capable(self) -> bool:
        return bool(self.coop)

    @property
    def recommended(self) -> bool:
        """界面上打「推荐」小标的那一块。

        **判据是名字**（`discrete`），因为它两档都有；`coop` 只当加分项——
        实测 realesrgan 那一档根本不打 `-cm` 行，拿它当唯一依据会让 realesrgan
        永远一个「推荐」都没有。
        """
        return self.discrete or self.capable

    @property
    def note(self) -> str:
        if self.discrete and self.capable:
            return "独立显卡，而且它报告了硬件加速支持"
        if self.discrete:
            return "名字上是独立显卡"
        if self.capable:
            return "报告了硬件加速支持"
        return "没有独立显卡的特征，多半是集成显卡"


# 名字里出现这些词的，按独显算。这只是**推荐**的依据，不是判据——
# 用户照样可以自己选，实测结论也会写在界面提示里。
_DISCRETE_HINTS = ("nvidia", "geforce", "rtx", "gtx", "quadro", "radeon", "arc ", "iris xe")


def parse_devices(text: str) -> list[Device]:
    """从 exe 的输出里读设备清单。

    格式（实测，两台引擎的行数不一样，所以按 id 合并、逐项找）::

        realesrgan（2022 年的包，4 行/台，**没有 -cm 那一行**）
        [0 NVIDIA GeForce RTX 5060 Laptop GPU]  queueC=2[8]  queueG=0[16]  queueT=1[2]
        [0 NVIDIA GeForce RTX 5060 Laptop GPU]  fp16-p/s/a=1/1/1  int8-p/s/a=1/1/1
        [1 Intel(R) ...]  queueC=0[1]  queueG=0[1]  queueT=0[1]

        waifu2x（新版 ncnn，4 行/台，多一行能力）
        [0 NVIDIA ...]  fp16-cm=16x16x16/16x8x16/16x8x8  int8-cm=...  bf16-cm=...  fp8-cm=...
        [1 Intel(R) ...]  fp16-cm=0  int8-cm=0  bf16-cm=0  fp8-cm=0
    """
    found: dict[int, Device] = {}
    for line in (text or "").splitlines():
        m = _DEVICE_LINE.match(line.strip())
        if not m:
            continue
        did, name, rest = int(m.group(1)), m.group(2).strip(), m.group(3)
        prev = found.get(did)
        queues = prev.queues if prev else 0
        if not queues:
            q = _QUEUE.search(rest)
            if q:
                queues = int(q.group(1))
        coop = prev.coop if prev else ""
        if not coop:
            for tag, value in _COOP_MATRIX.findall(rest):
                if value and value != "0":
                    coop = f"{tag}={value}"
                    break
        low = name.lower()
        found[did] = Device(
            id=did,
            name=name,
            queues=queues,
            coop=coop,
            discrete=any(h in low for h in _DISCRETE_HINTS),
        )
    return [found[k] for k in sorted(found)]


def device_note(devices: list[Device]) -> str:
    """界面上那句「用哪块卡」的说明。

    **必须把「选错代价很大」写出来**，否则用户不知道为什么要在意这一栏；
    同时也要说明同一块卡可能被报成几个入口，免得他以为机器上有三块显卡。
    """
    if len(devices) <= 1:
        return ""
    names = [d.name for d in devices]
    parts = [
        f"这台机器上引擎报出 {len(devices)} 个可用设备。不选就交给它自己挑；"
        "实测选错设备会慢好几倍（同一张图，waifu2x 用集显比用独显慢 22 倍）。"
    ]
    if len(set(names)) < len(names):
        parts.append("同一块卡被报成了多个入口，名字一样的就是同一块。")
    if any(d.recommended for d in devices):
        parts.append("带「推荐」的是按设备名认出来的独立显卡。")
    return "".join(parts)


# ------------------------------------------------------------------ 尺寸与上限

# 每帧输出像素上限：8K（7680×4320）。再大图片查看器与播放器基本都打不开，
# 而且 4 倍之后的体积会失控。
MAX_OUTPUT_PIXELS = 7680 * 4320
# 视频一次最多处理多少帧：1800 = 60 秒 30fps。逐帧跑，再多就不是「等不等得起」的问题了。
MAX_FRAMES = 1800
# PNG 每像素大约占多少字节（实测：1024²→4 倍得 4.86MB / 16.8MP = 0.29；1080p→4 倍得 7.13MB / 33MP = 0.22）
PNG_BYTES_PER_PIXEL = 0.3
# 临时文件超过这个量就在界面上提醒（不拦）
TEMP_WARN_BYTES = 20 * 1024 ** 3


def target_size(width: int, height: int, scale: int) -> tuple[int, int]:
    """放大后的尺寸。**不取偶数**——这两档都不做视频编码，奇数尺寸是合法的。"""
    return int(width) * int(scale), int(height) * int(scale)


def output_pixels(width: int, height: int, scale: int) -> int:
    w, h = target_size(width, height, scale)
    return w * h


def size_problem(width: int, height: int, scale: int) -> str:
    """这一组尺寸能不能做；不能就回一句人话（空串表示能做）。"""
    if scale < 1:
        return "倍数至少是 1。"
    w, h = target_size(width, height, scale)
    if w * h > MAX_OUTPUT_PIXELS:
        return (f"{scale} 倍之后是 {w}×{h}，超过上限（每帧最多 {MAX_OUTPUT_PIXELS // 1000000} "
                f"百万像素，约 8K）。这么大的图图片查看器和播放器基本都打不开，"
                "建议降一档倍数。")
    return ""


def frames_problem(frames: int) -> str:
    if frames > MAX_FRAMES:
        return (f"这一段有 {frames} 帧，超过一次能处理的 {MAX_FRAMES} 帧"
                f"（约 60 秒 30fps）。逐帧跑，再多就不是等不等得起的问题了——"
                "先剪短一点，或者降一档倍数。")
    return ""


def temp_bytes(frames: int, width: int, height: int, scale: int) -> int:
    """拆帧 + 放大帧同时留在盘上的**大约**体积。

    两套帧同时在盘上（输入帧与输出帧），所以是「输入 + 输出」。系数见 `PNG_BYTES_PER_PIXEL`，
    取的是实测偏大的那一档——估小了的代价是跑到一半没盘。
    """
    in_px = int(width) * int(height)
    out_px = output_pixels(width, height, scale)
    return int((in_px + out_px) * PNG_BYTES_PER_PIXEL * max(0, int(frames)))


# ------------------------------------------------------------------ 时间预估

# **按模型**记，不按引擎记——这是这一版最重要的一个纠正。
# 一开始我按引擎记（「realesrgan 每百万像素 3.1 秒」），结果预估值差了十几倍：
# 同一个包里，`realesr-animevideov3` 每百万像素只要 **0.058 秒**，而 `realesrgan-x4plus`
# 要 **4.39 秒**（约 75 倍）。前者是为视频专门做轻的，后者是完整的 RRDB 重网络。
# 按引擎记的话，给视频默认的那一档会被估成几十分钟，其实只要几十秒。
#
# 实测口径（RTX 5060 Laptop，-g 0，2026-09-22）：目录批量、640x360、16 帧、2 倍。
# 第一个数是**启动开销**（含第一帧，所以略微偏大——启动那点误差对整段无影响），
# 第二个数是每百万像素秒。
_COST: dict[str, tuple[float, float]] = {
    "realesr-animevideov3": (1.42, 0.058),
    "realesrgan-x4plus": (4.53, 4.392),
    "realesrgan-x4plus-anime": (2.21, 1.408),
    "models-cunet": (1.09, 0.151),
    "models-upconv_7_anime_style_art_rgb": (0.95, 0.058),
    "models-upconv_7_photo": (1.01, 0.037),
}
#: 没量过的模型（上游换了包、换了名字）：按偏重的一档估。**估久比估短好**——
#: 估短了用户点完才发现要等一小时。
_COST_FALLBACK: tuple[float, float] = (2.0, 1.5)

#: 倍数带来的成本系数。两种情形（见 `Model.per_scale_network`）：
#: 每档各有一份网络的，4 倍实测约是 2 倍的 3.4 倍（animevideov3）、4.7 倍（cunet），
#: 这里统一按 4 倍算；只有一份 x4 网络再缩放的，三档同价（实测比值 0.88~0.99）。
_SCALE_FACTOR: dict[int, float] = {1: 0.6, 2: 1.0, 3: 2.5, 4: 4.0}


def estimate_seconds(model: Model, scale: int, pixels: int, frames: int = 1) -> int:
    """粗估要跑多久（秒）。`frames=1` 就是图片。

    批量模式只加载一次模型，所以启动开销按一次算。

    **这只是估算**：常数来自一台机器的实测，换台显卡就会偏；界面上必须以
    「大约」的口径出现，而且真正的进度要以跑起来的帧数为准。
    """
    startup, per_mp = _COST.get(model.key, _COST_FALLBACK)
    factor = 1.0 if model.per_scale_network is False else _SCALE_FACTOR.get(
        max(1, min(4, int(scale))), 1.0)
    mp = max(0.0, float(pixels)) / 1_000_000.0
    return int(startup + per_mp * factor * mp * max(1, int(frames)))


def estimate_range(model: Model, scale: int, pixels: int,
                   frames: int = 1) -> tuple[int, int]:
    """给一个区间而不是一个数：估单一值会让人以为是承诺。

    上下浮动取 0.5~2 倍——实测同一档在不同尺寸下的单位成本能差一倍多
    （小图受启动开销影响大），换显卡差别更大。
    """
    mid = estimate_seconds(model, scale, pixels, frames)
    return max(1, int(mid * 0.5)), max(2, int(mid * 2.0))


def human_seconds(sec: int) -> str:
    """把秒说成人话。**不要出现 3600 这种数**（用户要自己换算就是我们的问题）。"""
    s = max(0, int(sec))
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"约 {s // 60} 分钟"
    hours = s / 3600
    return f"约 {hours:.1f} 小时" if hours < 10 else f"约 {int(hours)} 小时"


def estimate_for(engine_key: str, model_key: str, scale: int, pixels: int,
                 frames: int = 1) -> int:
    """按 key 找模型再估：给「手上只有两个字符串」的调用点（子进程超时、接口层）用。

    找不到那个模型时**不报错、退到兜底常数**——估时只决定超时与界面上一句话，
    为它把一件正经事（放大）判失败，代价完全不成比例。
    """
    spec = find_model(engine_key, model_key)
    if spec is None:
        spec = Model(key=str(model_key or ""), label="", note="", scales=(2,))
    return estimate_seconds(spec, scale, pixels, frames)


def estimate_text(model: Model, scale: int, pixels: int, frames: int = 1) -> str:
    lo, hi = estimate_range(model, scale, pixels, frames)
    if frames <= 1:
        return f"这台机器上大约 {human_seconds(lo)}–{human_seconds(hi)}"
    return (f"{frames} 帧逐帧过一遍，这台机器上大约 "
            f"{human_seconds(lo)}–{human_seconds(hi)}（以实际进度为准）")


# ------------------------------------------------------------------ 就绪判断


@dataclass
class RouteState:
    """一条路线此刻能不能用、为什么不能、以及「去哪儿解决」。"""

    available: bool
    reason: str = ""
    action: str = ""
    action_route: str = ""
    extra: dict = field(default_factory=dict)


def local_route_state(
    engine_key: str,
    *,
    installed: set[str],
    vulkan: bool,
) -> RouteState:
    """本机路线的状态。**按顺序**说第一个卡住的原因，不要一次抛一堆。"""
    engine = le.by_key(engine_key)
    label = engine.label if engine else engine_key
    if engine_key not in installed or exe_path(engine_key) is None:
        return RouteState(
            False,
            reason=f"还没装「{label}」。它走显卡，免安装、免 Python、免 CUDA。",
            action="到「本机引擎」页下载它",
            action_route="engines",
        )
    if not vulkan:
        return RouteState(
            False,
            reason=("这个引擎走 Vulkan，而这台机器上没有可用的 Vulkan 运行时，"
                    "装了也起不来（上游没给纯 CPU 档）。"),
            action="装显卡驱动（含 Vulkan 运行时）之后再来",
            action_route="engines",
        )
    if not models(engine_key):
        return RouteState(
            False,
            reason=f"「{label}」的目录里没找到模型文件（models/*.param）。",
            action="到「本机引擎」页删掉重新下一次",
            action_route="engines",
        )
    return RouteState(True)
