"""本机引擎（批次 9 的地基）：一份**清单** + 一台机器的**体检** + 一句**实话**。

这一层要解决的问题不是「怎么调用模型」，而是「**本机跑这件事在这台机器上值不值**」：

- 项目里已经有两条本机路（`ollama` 本机文本模型、`ComfyUI` 本机出图/工作流），
  但各写了一套「有没有 / 在哪 / 能不能用」。批次 9 要加的是一批**几十到几百 MB 的
  外部程序与权重**，它们只有一个共同点：**大文件不进仓库、不进便携包**——
  仓库里只放清单（名称 / 体积 / sha256 / 授权 / 下载地址），下载由用户触发、校验和必须比对。
- 而这些「本机跑」的引擎**不是每台机器都值得装**：ncnn-vulkan 那几档要 Vulkan 驱动，
  没有独显时纯 CPU 慢一个数量级。所以清单必须带上**按硬件分档的实话**，
  而不是一句「支持本机跑」。

## 四条口径（每条都是踩过才知道要写下来的）

1. **体积与 sha256 只能来自上游，不能来自印象。** 这一版的探针里我凭印象写过期望体积，
   比上游真实值大 5 万字节，于是「永远下不完」。所以每个 `Engine` 都带 `proof`，
   写清这个数是怎么来的：`upstream-digest`（取自 release asset 自带的 `digest`）
   或 `local-download`（本地真下载后自己算的）。**两种都不许是「查到的」**。
2. **收到 416 说明本地那份已经完整。** 断点续传发 `Range: bytes=N-`，起点落到末尾之后时
   服务器回 416——那是「不用下了」，不是「出错」。这一版的探针把它当错误、删掉重下，
   把一份已经下好的 43MB 文件删了。判断逻辑在 `engine_install.py`，口径写在这里。
3. **`sha256` 是我们校验用户下载的唯一依据**：算错就是「下完了几百 MB，然后说校验失败」。
   所以清单里绝不允许出现「大概是这样」的哈希——上游没给就自己下回来算。
4. **装完的落点是用户的数据目录**（`data/engines/<key>/`），不是仓库、不是便携包。
   `make_portable.py` 只拷 `backend/app` 与前端产物，所以升级与搬动都不会把这几百 MB 带上。

## 与「已有的本机能力」的关系

这一层**不重造** Ollama / ComfyUI 的检测，只把结论收在同一页上：
`engine_install.py` 里的 `installed_local_services()` 直接复用 `ollama_service.detect`
与模型服务表，避免出现第二份「本机有没有 Ollama」的判据。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

# 硬件档位的四个结论
LEVEL_OK = "ok"           # 这台机器上推荐用本机跑
LEVEL_SLOW = "slow"       # 能跑，但明显慢（或者要额外装东西）
LEVEL_NO = "no"           # 这台机器上跑不起来
LEVEL_MANUAL = "manual"   # 我们不代装：它是个独立安装程序，要用户自己走一遍

LEVEL_LABELS = {
    LEVEL_OK: "推荐本机跑",
    LEVEL_SLOW: "能跑但慢",
    LEVEL_NO: "这台机器跑不了",
    LEVEL_MANUAL: "要你自己安装",
}

KIND_LABELS = {
    "tts_runtime": "配音运行时",
    "tts_model": "配音模型",
    "clone_model": "音色克隆",
    "upscale": "超分",
    "interpolate": "补帧",
    "subtitle": "去字幕",
}

ARCHIVE_LABELS = {
    "tar.bz2": "tar.bz2",
    "zip": "zip",
    "installer": "安装程序（不代装）",
}


@dataclass(frozen=True)
class Engine:
    """清单里的一项。字段都是「说给用户听」的，写错一个就是一个坑。"""

    key: str
    label: str
    kind: str
    filename: str
    url: str
    size: int
    sha256: str
    license: str
    homepage: str
    archive: str
    # 装好之后安装目录里应当能匹配到的东西（**文件名级**通配，大小写不敏感）。
    # 用它回答「装好了没有」。特意**不写死上游的目录层级**：上游改一次层级
    # （`bin/x.exe` → `x.exe`）就会让判据失效，而表现是「装完了界面上还说没装」——
    # 所以判据只认文件名。安装程序那一档留空（我们不代装、也不看它的目录）。
    marker: str
    # 这个引擎是干什么的（一句话，说给用户听）
    why: str
    # 代价与限制（必须明说；藏起来的表现是用户装完才发现跑不动）
    note: str
    # 这个数是怎么来的：upstream-digest / local-download
    proof: str
    # 需要哪些显卡能力：vulkan / cpu / any
    gpu: str = "any"
    # 依赖的其它引擎（先装运行时再装模型）
    needs: tuple[str, ...] = field(default_factory=tuple)

    @property
    def size_text(self) -> str:
        return human_size(self.size)

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def archive_label(self) -> str:
        return ARCHIVE_LABELS.get(self.archive, self.archive)


# --------------------------------------------------------------------------
# 清单
# --------------------------------------------------------------------------
#
# 全部在北京时间 2026-09-21 逐项核对过：
# - 体积与 sha256：有 `digest` 的直接取上游 release asset 的（字节数与哈希同一个来源，
#   不会出现「体积是新的、哈希是旧的」这种错位）；上游没给 digest 的（realesrgan）
#   自己下载回来算。
# - 授权：以项目/模型卡上的声明为准（Kokoro-82M-v1.1-zh 的模型卡写明 Apache-2.0）。
#
# Windows 运行时的选择：sherpa-onnx 有 MT / MD 两档（MT = 静态 CRT，MD = 动态 CRT）。
# 取 **MT**：静态 CRT 不依赖系统里的 VC++ 运行库，才配得上「零环境」。代价是 +33MB——
# 对一个几百 MB 的包来说，用「多 33MB」换「不用让用户去装运行库」是划算的。
#
# `sherpa_tts_runtime` 的 marker（`sherpa-onnx-offline-tts.exe`）为什么可以确定在包里：
# 上游的 `windows-x64.yaml` 构建脚本里写着 `export EXE=sherpa-onnx-offline-tts.exe`，
# 而且**另外**打了个 `-no-tts` 变体——默认包必然带它（bz2 是流式压缩，
# 这个 exe 在 tar 里的位置很靠后，没法只看头部就确认，所以去看了打包它的 workflow）。

ENGINES: tuple[Engine, ...] = (
    Engine(
        key="sherpa_tts_runtime",
        label="sherpa-onnx 运行时（Windows x64）",
        kind="tts_runtime",
        filename="sherpa-onnx-v1.13.8-win-x64-static-MT-Release.tar.bz2",
        url=(
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/v1.13.8/"
            "sherpa-onnx-v1.13.8-win-x64-static-MT-Release.tar.bz2"
        ),
        size=249216829,
        sha256="849ea51f860cefbe0ae0074ed01190cd24b91ae3da80c2637261a9e7dabe39c9",
        license="Apache-2.0",
        homepage="https://github.com/k2-fsa/sherpa-onnx",
        archive="tar.bz2",
        marker="sherpa-onnx-offline-tts.exe",
        why="本机配音的执行程序。纯 C++、自带推理引擎，不需要 Python、不需要显卡、不需要联网。",
        note="249MB 里绝大部分是随包一起发布的推理引擎。取的是静态 CRT 那一版，"
        "所以不用先装 VC++ 运行库。包内是「一个顶层目录 + bin/ 下一堆 exe」"
        "（实测是 sherpa-onnx-v1.13.8-win-x64-static-MT-Release/bin/…），"
        "解压时会把这个顶层目录拆平。",
        proof="upstream-digest",
        gpu="cpu",
    ),
    Engine(
        key="kokoro_zh",
        label="Kokoro 中文模型（int8 量化）",
        kind="tts_model",
        filename="kokoro-int8-multi-lang-v1_1.tar.bz2",
        url=(
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
            "kokoro-int8-multi-lang-v1_1.tar.bz2"
        ),
        size=147031220,
        sha256="a1e94694776049035c4f2c6529f003aaece993c76aae9a78995831c3c4dcafc6",
        license="Apache-2.0（模型卡：hexgrad/Kokoro-82M-v1.1-zh）",
        homepage="https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh",
        archive="tar.bz2",
        marker="*.onnx",
        why="82M 参数的小模型，中英双语、103 个音色。本机跑起来是秒级的，"
        "不用显卡、不用联网，也就没有「每读一句花一次钱」这回事。",
        note="不支持音色克隆——它给的是模型自带的那些音色。要克隆自己的声音得加装"
        "下面那个 ZipVoice 模型。int8 量化版的体积只有 fp32 版的一半不到，"
        "听感上的差别在这个用途里听不出来。",
        proof="upstream-digest",
        gpu="cpu",
        needs=("sherpa_tts_runtime",),
    ),
    Engine(
        key="zipvoice_zh",
        label="ZipVoice 音色克隆模型（int8 蒸馏版）",
        kind="clone_model",
        filename="sherpa-onnx-zipvoice-distill-int8-zh-en-emilia.tar.bz2",
        url=(
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
            "sherpa-onnx-zipvoice-distill-int8-zh-en-emilia.tar.bz2"
        ),
        size=109162785,
        sha256="77219c8b40f4ee8d73a7f902305ff6c1128ef9b54461c41b4ca6ed890b6c2803",
        license="Apache-2.0",
        homepage="https://github.com/k2-fsa/sherpa-onnx",
        archive="tar.bz2",
        marker="*.onnx",
        why="零样本音色克隆：给一段参考音频，就能用那个声音说话。"
        "和 Kokoro 共用同一个运行时，所以不用再下一遍执行程序。",
        note="比 Kokoro 慢得多（蒸馏 int8 版在 CPU 上大约是实时的一点几倍，"
        "读十秒的话要等十几秒），所以界面上要给进度、别让人以为卡死了。"
        "参考音频越干净效果越好；背景有噪声时克隆出来的声音也会带上那个噪声。",
        proof="upstream-digest",
        gpu="cpu",
        needs=("sherpa_tts_runtime",),
    ),
    Engine(
        key="realesrgan",
        label="Real-ESRGAN 超分（图片 / 视频）",
        kind="upscale",
        filename="realesrgan-ncnn-vulkan-20220424-windows.zip",
        url=(
            "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/"
            "realesrgan-ncnn-vulkan-20220424-windows.zip"
        ),
        size=45474481,
        sha256="abc02804e17982a3be33675e4d471e91ea374e65b70167abc09e31acb412802d",
        license="BSD-3-Clause",
        homepage="https://github.com/xinntao/Real-ESRGAN",
        archive="zip",
        marker="realesrgan-ncnn-vulkan.exe",
        why="把图片或视频放大：分镜图、角色设定图、成片都能用。免安装、免 Python、免 CUDA，"
        "带上显卡就能跑。",
        note="它走 Vulkan，没有 Vulkan 驱动的机器上跑不起来（上游没给纯 CPU 那一档）。"
        "2022 年的版本号是上游最后一个正式包，社区至今仍在用。",
        proof="local-download",
        gpu="vulkan",
    ),
    Engine(
        key="waifu2x",
        label="waifu2x 超分（插画 / 线稿友好）",
        kind="upscale",
        filename="waifu2x-ncnn-vulkan-20250915-windows.zip",
        url=(
            "https://github.com/nihui/waifu2x-ncnn-vulkan/releases/download/20250915/"
            "waifu2x-ncnn-vulkan-20250915-windows.zip"
        ),
        size=35497352,
        sha256="7425be94b94e4c8f37a1e433ac0e0100c43790e2c37418f4b65d8235adfbdc87",
        license="MIT",
        homepage="https://github.com/nihui/waifu2x-ncnn-vulkan",
        archive="zip",
        marker="waifu2x*.exe",
        why="给插画、线稿、动画截图用的放大与降噪。同一个东西在插画上比通用超分模型干净，"
        "两类都留着让用户自己挑。",
        note="同样走 Vulkan。2025-09 上游仍在发版，是这份清单里维护得最勤的一个。"
        "写实照片请用上面那个 Real-ESRGAN。",
        proof="upstream-digest",
        gpu="vulkan",
    ),
    Engine(
        key="rife",
        label="RIFE 补帧（把 24 帧补顺到 60 帧）",
        kind="interpolate",
        filename="rife-ncnn-vulkan-20221029-windows.zip",
        url=(
            "https://github.com/nihui/rife-ncnn-vulkan/releases/download/20221029/"
            "rife-ncnn-vulkan-20221029-windows.zip"
        ),
        size=431540241,
        # 上游这个 release **没有给摘要**（GitHub API 的 digest 是 null），所以这个是
        # 我们自己下完整包之后算的，算完又与 API 的字节数对过（431540241，一致）。
        sha256="d8e4d772d26cd8006ef0ad0bc82eb191b53c68677d1ae2f42506d74cbbbea606",
        license="MIT",
        homepage="https://github.com/nihui/rife-ncnn-vulkan",
        archive="zip",
        marker="rife-ncnn-vulkan.exe",
        why="把片子补顺：24 帧补成 60 帧之后，镜头横移与人物动作不再一格一格。"
        "它是拿前后两帧算出中间那一帧，所以补出来的是模型猜的画面，不是简单的平均。",
        note="这个包 411MB，是因为上游把 12 个模型全塞进去了（rife / rife-HD / rife-UHD / "
        "rife-anime / rife-v2 ~ v4.6）。常用的其实是 rife-v4.6 与 rife-anime，"
        "但我们不动手删上游的文件：删了之后哪天要用另一个模型就得整包重下。"
        "包里的 vcomp140.dll 是它需要的 VC 运行库，已经随包带着，不用另装。"
        "上游 2022-10 之后没再发版（与 Real-ESRGAN 同一年），社区至今仍在用。",
        proof="local-download",
        gpu="vulkan",
    ),
    Engine(
        key="vsr",
        label="VSR 去字幕（高质量档，AI 补全）",
        kind="subtitle",
        filename="VSR_v1.4.0_windows_x64_cpu_Setup.exe",
        url=(
            "https://github.com/YaoFANGUK/video-subtitle-remover/releases/download/1.4.0/"
            "VSR_v1.4.0_windows_x64_cpu_Setup.exe"
        ),
        size=766975513,
        sha256="458cc84b1d67d199a868e55079034d8be94f8947149efe1e3dedab1cb0231fad",
        license="Apache-2.0",
        homepage="https://github.com/YaoFANGUK/video-subtitle-remover",
        archive="installer",
        marker="",
        why="真正把字幕「补掉」而不是盖掉：它是 AI 补全，字幕那一带会按周围画面重新画出来，"
        "所以不会有抹平、模糊、遮住留下的痕迹。",
        note="这是个 731MB 的独立安装程序，我们不代装、也不代跑：它自带一整套 "
        "Python + Paddle/Torch 运行环境，装完是它自己的界面。导演台里「去字幕」那一版"
        "（四种 ffmpeg 手法）是零成本档、只盖像素；这一档是 AI 补全，字幕那一带按周围"
        "画面重新画，不留痕。真要无痕时才来下——用它自己处理好，成品再拖回导演台接着剪。",
        proof="upstream-digest",
        gpu="cpu",
    ),
)

_INDEX = {e.key: e for e in ENGINES}


def engines() -> tuple[Engine, ...]:
    return ENGINES


def by_key(key: str) -> Engine | None:
    return _INDEX.get(str(key or "").strip())


def human_size(num: int) -> str:
    """体积说成人话。给用户看的数不要出现 249216829 这种。"""
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def total_size(keys: list[str] | tuple[str, ...] | None = None) -> int:
    picks = [by_key(k) for k in keys] if keys else list(ENGINES)
    return sum(e.size for e in picks if e is not None)


# --------------------------------------------------------------------------
# 硬件体检：这台机器值不值得本机跑
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Hardware:
    """一台机器的体检结果。字段都是检测出来的，检测不出就是空/False（**不猜**）。"""

    gpu_names: tuple[str, ...] = ()
    nvidia: bool = False
    vulkan: bool = False
    vulkan_device: str = ""
    cores: int = 0
    ram_gb: float = 0.0

    @property
    def has_gpu(self) -> bool:
        return bool(self.gpu_names)

    @property
    def gpu_text(self) -> str:
        return " + ".join(self.gpu_names) if self.gpu_names else "没检测到独立显卡"


def verdict(engine: Engine, hw: Hardware) -> tuple[str, str]:
    """这台机器上「这个引擎能不能跑、值不值」——返回 `(档位, 一句实话)`。

    **不许说「支持本机跑」这种没有信息量的话**：跑不了就说跑不了，
    能跑但慢就说慢在哪儿，要让用户看完能自己决定。
    """
    if engine.archive == "installer":
        return (
            LEVEL_MANUAL,
            "它是个独立安装程序（自带一整套运行环境），我们不代装、也不代跑；"
            "真要无痕去字幕时再下它。",
        )

    if engine.gpu == "vulkan":
        if not hw.vulkan:
            return (
                LEVEL_NO,
                "这台机器上没有可用的 Vulkan 运行时，而它是走 Vulkan 的（上游没给纯 CPU 档）。"
                "装显卡驱动（含 Vulkan 运行时）之后再来。",
            )
        if not hw.has_gpu:
            return (
                LEVEL_SLOW,
                "有 Vulkan 运行时但没检测到独立显卡，多半会落在集显上——能跑，"
                "但放大一张图可能要等十几秒。",
            )
        device = f"（{hw.vulkan_device}）" if hw.vulkan_device else ""
        return LEVEL_OK, f"检测到 Vulkan 可用{device}，本机放大是秒级的，比调 API 划算。"

    if engine.gpu == "cpu":
        if engine.kind == "subtitle":
            return LEVEL_MANUAL, "见上面的说明：这一档我们不代装。"
        threads = f"{hw.cores} 线程" if hw.cores else "未知线程数"
        return (
            LEVEL_OK,
            f"纯 CPU 就能跑（这台机器 {threads}），不用显卡；"
            "配音这件事本机跑省下来的就是每次调用那笔钱。",
        )

    return LEVEL_OK, "这台机器上可以跑。"


def row(engine: Engine, hw: Hardware, state: dict) -> dict:
    """给界面一行。**状态与建议分开**：装没装是一件事，值不值得装是另一件事。"""
    level, reason = verdict(engine, hw)
    return {
        "key": engine.key,
        "label": engine.label,
        "kind": engine.kind,
        "kindLabel": engine.kind_label,
        "why": engine.why,
        "note": engine.note,
        "size": engine.size,
        "sizeText": engine.size_text,
        "license": engine.license,
        "homepage": engine.homepage,
        "url": engine.url,
        "filename": engine.filename,
        "archive": engine.archive,
        "archiveLabel": engine.archive_label,
        "marker": engine.marker,
        "proof": engine.proof,
        "needs": list(engine.needs),
        "level": level,
        "levelLabel": LEVEL_LABELS.get(level, level),
        "reason": reason,
        **state,
    }


def missing_needs(engine: Engine, installed: set[str]) -> list[str]:
    """还差哪些前置引擎（装模型前要先有运行时）。"""
    return [k for k in engine.needs if k not in installed]


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    """算一个文件的 sha256。校验是这条链上唯一能证明「下对了」的东西，所以单独拎出来。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def digest_matches(engine: Engine, hexdigest: str) -> bool:
    """大小写不敏感地比一下（上游给的是小写，但别的地方写大写也不该算错）。"""
    return str(hexdigest or "").strip().lower() == engine.sha256.strip().lower()


def marker_hit(engine: Engine, root: Path) -> str:
    """安装目录里有没有符合 `marker` 的文件；命中就返回那个文件的相对路径，否则空串。

    只按**文件名**匹配（`fnmatch`，大小写不敏感），刻意不看目录层级——见 `Engine.marker`
    的注释：判据盯死上游的目录结构，只会在上游改名时变成「装完了还说没装」。
    没有 marker 的（安装程序那一档）永远返回空串：它的目录不归我们管。
    """
    if not engine.marker or not root.exists():
        return ""
    pattern = engine.marker.lower()
    for path in sorted(root.rglob("*")):
        try:
            if path.is_file() and fnmatch(path.name.lower(), pattern):
                return path.relative_to(root).as_posix()
        except OSError:
            continue
    return ""


def assert_consistent() -> None:
    """清单的自检。**清单写错的表现是用户白下几百 MB**，所以这些必须在测试里钉住。"""
    seen_key: set[str] = set()
    seen_file: set[str] = set()
    for e in ENGINES:
        assert e.key and e.key not in seen_key, f"引擎 key 重复：{e.key}"
        seen_key.add(e.key)
        assert e.filename and e.filename not in seen_file, f"文件名重复：{e.filename}"
        seen_file.add(e.filename)
        assert e.label and e.why and e.note, f"{e.key} 少了给人看的说明"
        assert e.kind in KIND_LABELS, f"{e.key} 的类别不认识：{e.kind}"
        assert e.size > 0, f"{e.key} 的体积没写"
        assert len(e.sha256) == 64 and all(c in "0123456789abcdef" for c in e.sha256), (
            f"{e.key} 的 sha256 不是 64 位十六进制（现在是 {e.sha256!r}）——"
            "这个数是我们校验用户下载的唯一依据，不许含糊"
        )
        assert e.proof in ("upstream-digest", "local-download"), (
            f"{e.key} 的 proof 要写清这个数是怎么来的"
        )
        assert e.url.startswith("https://") and e.filename in e.url, (
            f"{e.key} 的下载地址与文件名对不上：{e.url}"
        )
        assert e.archive in ARCHIVE_LABELS, f"{e.key} 的包类型不认识：{e.archive}"
        assert e.gpu in ("any", "cpu", "vulkan"), f"{e.key} 的 gpu 档不认识：{e.gpu}"
        assert e.license, f"{e.key} 没写授权"
        for need in e.needs:
            assert need in _INDEX, f"{e.key} 依赖了一个不存在的引擎：{need}"
            assert need != e.key, f"{e.key} 依赖自己"
        if e.archive == "installer":
            assert e.marker == "", f"{e.key} 是安装程序，不该有 marker（我们不看它的目录）"
        else:
            assert e.marker, f"{e.key} 没写装好之后的标志文件"


# --------------------------------------------------------------------------
# 界面上的编排：一句总的话
# --------------------------------------------------------------------------


def headline(hw: Hardware, installed: int, total: int) -> str:
    """本机引擎页顶部那句话。用户来这一页最想知道的是「我该不该装」。"""
    if not hw.has_gpu:
        return (
            "这台机器没检测到独立显卡：配音那几档纯 CPU 就能跑、值得装；"
            "超分那两档走 Vulkan，装之前建议先想想这台机器上是不是真的比调 API 划算。"
        )
    if hw.vulkan:
        return (
            f"检测到 {hw.gpu_text} 且 Vulkan 可用：本机配音与超分都值得装，"
            "装完就是零调用成本（几档都很吃显存，跑的时候别同时开大游戏）。"
        )
    return (
        f"检测到 {hw.gpu_text}，但没检测到可用的 Vulkan 运行时："
        "配音那几档纯 CPU 能跑；超分那两档要先去装显卡驱动里的 Vulkan 运行时。"
    )
