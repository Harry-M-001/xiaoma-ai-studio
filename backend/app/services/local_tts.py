"""本机配音的纯口径：装好的目录里有什么、命令行怎么拼、有哪些音色。

与 `providers/local_tts.py`（跑子进程那一层）分工：这边全是可以在没有引擎的机器上
测的东西——路径解析、命令行拼装、音色表解析、错误文案。

## 三条口径

1. **不猜命令行参数。** sherpa-onnx 的参数是「指向模型目录里的具体文件」
   （`--kokoro-model` / `--kokoro-voices` / `--kokoro-tokens` / `--kokoro-data-dir` …），
   而不同包的**文件名并不完全一样**（int8 包里有可能是 `model.int8.onnx`）。
   所以这里按**目录里真实存在什么**去 glob，缺哪个就报哪个的名字——
   写死文件名的话，上游换一次命名就是「引擎装好了但一点就报错」。
2. **音色表来自模型包自己，拿不到就自己算数。** 有音色表的包（`0->af_alloy` 那种）
   就把表读出来；**没表的包不编名字**——实测这个 Kokoro int8 多语言包的
   `README.md` 只有 114 字节，里面没有表。这种情况改从 `voices.bin` 的**体积**推出
   音色**个数**（每个音色占固定的 522240 字节），于是界面上能给出真实存在的号段
   （0–102），并且老实说「包里没带音色名，按号选」。**两条都不猜**：既不在代码里抄一份
   会过时的表，也不假装有名字。
3. **报错要能自己排查**：缺文件时报文件名与所在目录，跑失败时报退出码与 stderr 尾部。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

# 运行时与模型这两个引擎的 key（与 `local_engines.py` 的清单一一对应）
RUNTIME_KEY = "sherpa_tts_runtime"
MODEL_KEY = "kokoro_zh"
CLONE_KEY = "zipvoice_zh"

RUNTIME_EXE = "sherpa-onnx-offline-tts.exe"
# 音色表的写法：`45->zf_xiaobei`。**只认左边是数字的**：
# 有表的包往往同时给一张反方向的表（`zf_xiaobei->45`），
# 一把抓会把名字当 id、把音色表搞成两倍长。
_VOICE_PAIR = re.compile(r"(\d{1,4})\s*->\s*([A-Za-z0-9_\-]+)")

# `voices.bin` 里**每个音色占的字节数**：Kokoro 的 style 向量的形状是
# 「510 个 style token × 256 维 × 4 字节（float32）」= 522240。
# 用它算音色个数：实测这个包的 voices.bin 是 53,790,720 字节，
# 53,790,720 ÷ 522,240 = **103**，与官方文档写的「103 speakers」一致。
#
# 为什么不用「读 README 里那张表」当唯一来源：**这个包里根本没有那张表**
# （README.md 只有 114 字节）。所以名字只能靠号，而**号的个数可以自己算出来**。
# 除不尽就当一个都不认识（不猜）——宁可只显示 0 号，也不要显示一堆不存在的号。
_VOICE_BLOCK = 510 * 256 * 4
_MAX_VOICES = 2000

# 中文文本要用的文本正则：数字、电话、日期（模型包里带这三个 fst）
_RULE_FST_ORDER = ("date-zh.fst", "phone-zh.fst", "number-zh.fst")


def engines_root() -> Path:
    """与 `engine_install.root_dir()` 同一个落点（那边负责建目录，这里只读）。"""
    return settings.data_dir / "engines"


def runtime_dir() -> Path:
    return engines_root() / RUNTIME_KEY


def model_dir(key: str = MODEL_KEY) -> Path:
    return engines_root() / key


def find_exe(root: Path | None = None) -> Path | None:
    """找 TTS 可执行文件。**按文件名找**（不写死目录层级），与引擎清单同一条口径。"""
    base = root or runtime_dir()
    if not base.exists():
        return None
    for path in sorted(base.rglob(RUNTIME_EXE)):
        if path.is_file():
            return path
    return None


def find_model_file(dir_path: Path, *, prefer: tuple[str, ...] = ()) -> Path | None:
    """在模型目录里找一个文件：先按 `prefer` 里的名字找，再按后缀找。"""
    if not dir_path.exists():
        return None
    for name in prefer:
        candidate = dir_path / name
        if candidate.is_file():
            return candidate
    for pattern in prefer:
        hit = sorted(dir_path.glob(pattern))
        if hit:
            return hit[0]
    return None


@dataclass(frozen=True)
class Layout:
    """装好的目录里**真实存在**的那几样东西。缺什么就报什么，不补默认值。"""

    exe: Path
    model: Path
    voices: Path
    tokens: Path
    data_dir: Path
    lexicon: tuple[Path, ...]
    rule_fsts: tuple[Path, ...]

    def missing(self) -> list[str]:
        """还缺哪些（空表示齐了）。`data_dir` 不存在也算缺——音素化要用它。"""
        gaps: list[str] = []
        for label, path in (("可执行文件", self.exe), ("模型", self.model),
                            ("音色文件", self.voices), ("词表", self.tokens)):
            if not path.is_file():
                gaps.append(label)
        if not self.data_dir.is_dir():
            gaps.append("音素数据目录（espeak-ng-data）")
        return gaps


def resolve_layout(key: str = MODEL_KEY) -> Layout | None:
    """把装好的目录解析成一个 `Layout`；运行时或模型没装齐时返回 None。

    返回 None 而不是抛异常：调用方（引擎页 / 配音页）要的是「能不能用 + 为什么」，
    不是一条异常。原因由 `check_ready()` 给。
    """
    exe = find_exe()
    mdir = model_dir(key)
    if exe is None or not mdir.exists():
        return None
    model = find_model_file(mdir, prefer=("model.int8.onnx", "model.onnx", "*.onnx"))
    voices = find_model_file(mdir, prefer=("voices.bin",))
    tokens = find_model_file(mdir, prefer=("tokens.txt",))
    if model is None or voices is None or tokens is None:
        return None
    data_candidates = [p for p in sorted(mdir.rglob("espeak-ng-data")) if p.is_dir()]
    lexicon = tuple(sorted(p for p in mdir.glob("lexicon*.txt") if p.is_file()))
    by_name = {p.name: p for p in mdir.glob("*.fst") if p.is_file()}
    rules = tuple(by_name[n] for n in _RULE_FST_ORDER if n in by_name)
    return Layout(
        exe=exe,
        model=model,
        voices=voices,
        tokens=tokens,
        data_dir=data_candidates[0] if data_candidates else mdir / "espeak-ng-data",
        lexicon=lexicon,
        rule_fsts=rules,
    )


def check_ready(key: str = MODEL_KEY) -> tuple[bool, str]:
    """能不能本机配音；不能时给一句**能照做**的话。"""
    from app.services import local_engines as le

    runtime = le.by_key(RUNTIME_KEY)
    model = le.by_key(key)
    label_rt = runtime.label if runtime else RUNTIME_KEY
    label_md = model.label if model else key
    if find_exe() is None:
        return False, f"还没装「{label_rt}」——到「本机引擎」页下载它"
    if not model_dir(key).exists():
        return False, f"还没装「{label_md}」——到「本机引擎」页下载它"
    layout = resolve_layout(key)
    if layout is None:
        return False, (
            f"「{label_md}」的目录里缺文件（模型 / 音色 / 词表）——"
            "可能是下载没完成或解压不完整，到「本机引擎」页删掉重新下一次"
        )
    gaps = layout.missing()
    if gaps:
        return False, f"「{label_md}」还缺：{'、'.join(gaps)}——到「本机引擎」页重新下一次"
    return True, ""


def plan_args(
    layout: Layout,
    *,
    text: str,
    out_path: Path,
    sid: int = 0,
    speed: float = 1.0,
    threads: int = 2,
) -> list[str]:
    """拼出 sherpa-onnx-offline-tts 的完整命令行（纯函数，好测）。

    几个细节：
    - `--sid` 是**整数音色号**：Kokoro 的音色就是 sid（有音色表的包按名字翻过来，
      没表的包直接填号，见 `voice_list()`）；
    - 语速走 **`--kokoro-length-scale`**，而它是**时长倍率**：语速 2 倍 = 时长 0.5 倍。
      这个反比关系写错的表现是「把语速调快，声音反而更慢」；
    - **不能用 `--vits-length-scale`**：那个参数这个二进制**认**（不报错），但对 Kokoro
      这条路**完全不起作用**——实测同一句话 1 倍速与 2 倍速都是 6.17 秒。
      正确的名字是从 `sherpa-onnx-offline-tts.exe`（不带参数运行）打出来的用法表里读到的：
      `--kokoro-length-scale : Speech speed. Larger->Slower; Smaller->faster.`
      **静默不生效**是这一版最难查的一类错，所以这里记下判据的来源；
    - `--debug=0` 关掉它的调试输出（默认会打一大堆日志，我们只看退出码）；
    - 文本放最后（位置参数）。
    """
    scale = 1.0 / float(speed or 1.0) if speed else 1.0
    args = [
        str(layout.exe),
        "--debug=0",
        f"--kokoro-model={layout.model}",
        f"--kokoro-voices={layout.voices}",
        f"--kokoro-tokens={layout.tokens}",
        f"--kokoro-data-dir={layout.data_dir}",
    ]
    if layout.lexicon:
        args.append("--kokoro-lexicon=" + ",".join(str(p) for p in layout.lexicon))
    if layout.rule_fsts:
        args.append("--tts-rule-fsts=" + ",".join(str(p) for p in layout.rule_fsts))
    args += [
        f"--num-threads={max(1, int(threads))}",
        f"--sid={max(0, int(sid))}",
    ]
    if abs(scale - 1.0) > 1e-6:
        args.append(f"--kokoro-length-scale={scale:.3f}")
    args += [f"--output-filename={out_path}", text]
    return args


# ---------------------------------------------------------------- 音色


@dataclass(frozen=True)
class Voice:
    sid: int
    name: str

    @property
    def id(self) -> str:
        """给用户填的那一栏用的值：**用名字**（可读），界面上也按名字显示。"""
        return self.name or str(self.sid)

    @property
    def label(self) -> str:
        return f"{self.name}（{self.sid}）" if self.name else f"音色 {self.sid}"


def parse_voices(readme_text: str) -> list[Voice]:
    """从模型包 README 里读 `音色号 -> 音色名` 那张表。

    只认左边是数字的写法（同一份 README 还有反方向那张表，见 `_VOICE_PAIR` 的注释）；
    同一 sid 出现多次时以第一次为准（表里可能有分组重复）。
    """
    seen: dict[int, str] = {}
    for digits, name in _VOICE_PAIR.findall(readme_text or ""):
        sid = int(digits)
        if sid not in seen:
            seen[sid] = name
    return [Voice(sid=sid, name=seen[sid]) for sid in sorted(seen)]


def speaker_count(voices_path) -> int:
    """从 `voices.bin` 的体积算音色个数（除不尽返回 0——**不猜**）。"""
    try:
        size = int(voices_path.stat().st_size)
    except (OSError, TypeError, ValueError):
        return 0
    if size <= 0 or size % _VOICE_BLOCK:
        return 0
    count = size // _VOICE_BLOCK
    return count if 0 < count <= _MAX_VOICES else 0


def voice_list(key: str = MODEL_KEY) -> list[Voice]:
    """这个模型能用的音色。

    来源按可靠性排：① 模型包里的音色表（有名字，最好用）；② 从 `voices.bin` 的体积
    算出来的个数（只有号，但**是真的**——实测 103 与官方文档一致）；③ 一个默认音色。
    """
    mdir = model_dir(key)
    for name in ("README.md", "readme.md", "README"):
        readme = mdir / name
        if readme.is_file():
            try:
                voices = parse_voices(readme.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                voices = []
            if voices:
                return voices
    packed = find_model_file(mdir, prefer=("voices.bin",))
    count = speaker_count(packed) if packed is not None else 0
    if count:
        return [Voice(sid=i, name="") for i in range(count)]
    return [Voice(sid=0, name="")]


def voice_hint(count: int, *, named: bool) -> str:
    """界面上那句说明音色怎么填的话（**名字有没有**要分开说）。"""
    if count <= 1:
        return "这个本机模型只带一个默认音色。"
    if named:
        return (
            f"这个本机模型自带 {count} 个音色：填名字或音色号都行，"
            "留空用第 0 号。"
        )
    return (
        f"这个本机模型自带 {count} 个音色，包里没带音色名，所以按号选（0–{count - 1}）："
        "填数字即可，留空用第 0 号。哪个号对应哪种嗓子见 sherpa-onnx 的模型说明页"
        "（k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/kokoro.html）。"
    )


def sid_for(voice: str, key: str = MODEL_KEY) -> int:
    """把用户填的音色（名字或号码）翻成 sid。

    填了认不出来的名字时**报错而不是悄悄用默认音色**：那种表现是
    「我明明选了小焰的嗓子，出来却是别人」——用户只会以为功能坏了。
    """
    raw = str(voice or "").strip()
    if not raw:
        return 0
    if raw.isdigit():
        return int(raw)
    known = [v.name for v in voice_list(key) if v.name]
    if known:
        if any(v.lower() == raw.lower() for v in known):
            return next(v.sid for v in voice_list(key)
                        if v.name and v.name.lower() == raw.lower())
        hint = "、".join(known[:6]) + " …"
    else:
        hint = f"这个模型只按号选（0–{max(0, len(voice_list(key)) - 1)}）"
    raise ValueError(
        f"这个本机模型没有音色「{raw}」。{hint}；留空则用默认音色"
    )


def default_threads() -> int:
    """给它几个线程。**不是越多越快**：Kokoro 是 82M 的小模型，线程开太多反而抢核。

    取 min(4, 核数 // 2)：四核以下的机器至少给 1 个，24 核的机器给 4 个。
    """
    import os

    cores = os.cpu_count() or 2
    return max(1, min(4, cores // 2))
