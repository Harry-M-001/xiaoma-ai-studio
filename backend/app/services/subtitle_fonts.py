"""随包/可下载的开源中文字体清册，以及烧字幕要用到的字体准备动作。

为什么单独立一层：字幕能不能烧，取决于三件外部条件——`ass` 滤镜在不在、字体文件在不在、
**字体名写对没有**。三件事都在这一层判断清楚，上层（路由与前端）只管展示结论，
不必各自去猜。

四条实测踩出来的规矩，改代码前先读：

1. **`family` 必须逐字匹配字体内部的 family name，不是文件名。** 写错时 libass 会
   **静默回落系统字体**（中文直接变豆腐块），ffmpeg 不报错、退出码 0。
   实测活样本：得意黑的文件名是 `SmileySans-Oblique.ttf`，族名却是 `Smiley Sans Oblique`——
   按 `Smiley Sans` 写时 libass 回落到 `ArialMT` / `MicrosoftYaHeiUI`。
   所以这个字段**只能从字体文件里读出来**（加字体时用 `probe_font_families.py` 跑一遍），
   不许凭文件名推。
2. **不用可变字体（variable font）。** libass 0.17 不支持 OpenType 可变字体的实例选择
   （带 `Bold` 位也只会渲染成 Regular）；Adobe 还警告 CFF2 可变字体在部分 Windows 10/11
   上会导致文字损坏。所以清册里全是**静态实例**。
3. **给 libass 的路径不用绝对路径。** filtergraph 有两层转义，绝对路径要写成 `C\\:/path`
   （两级反斜杠）才对，极易写错。做法见 `stage_fonts()`：把这次要用到的字体
   **硬链接**进本次渲染的工作目录，然后 `fontsdir=.`——同卷零拷贝，也完全不碰转义。
4. **只挂用到的那几份。** libass 会把 `fontsdir` 里的字体**全量解析**一遍：
   实测整库（80MB）0.16s、单份 0.08s。逐镜渲染时这个差价会累积，所以按需挂。

字体本身的分发边界：思源黑体 / 思源宋体 / 霞鹜文楷 / 得意黑全部是 **SIL OFL 1.1**，
允许再分发。**只有第一款内置进仓库与便携包**（保证「开箱就能烧中文字幕」这一条成立），
其余按需下载——大文件不进仓库，这是 2026-09-20 用户定下的原则。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from app.config import settings
from app.services import ffmpeg_service

# 内置字体目录：随仓库与便携包分发，只读（打包脚本 COPY_TREES 里有 backend/assets）
BUILTIN_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"

# 下载的字体放**数据目录**，不放仓库目录。三个理由：
# 1. 数据目录不进 git、不进便携包——「大文件不进仓库」这条才算真的做到；
# 2. 用户重装应用 / 换便携包时，数据目录是保留的，下过的字体不用重下；
# 3. 存放位置可写。仓库目录在便携包里可能落在只读位置（Program Files 之类）。
def user_dir() -> Path:
    return settings.data_dir / "fonts"


def find_file(name: str) -> Path | None:
    """找一个字体文件。**先看下载目录、再看内置目录**——用户下过的优先，

    这样以后想换一款同名字体（比如升级到新版思源黑体）只要下载覆盖即可，不用动仓库。
    """
    for base in (user_dir(), BUILTIN_DIR):
        p = base / name
        if p.exists():
            return p
    return None


# 内置的那一款：唯一随仓库与便携包分发的，保证「开箱可用」
BUILTIN_KEY = "noto_sans"

# 可下载字体的托管位置。
#
# **为什么用我们自己的 Release 附件、而不是上游直链**：`raw.githubusercontent.com` 国内
# 基本不通，而用户在国内。这两个地址是 `fonts-v1` 这个 Release 的附件，**地址长期不变**
# （单独开一个 Release 而不是挂在版本 Release 下，就是为了这个——挂在版本下的话每次发版
# 地址都变，而地址是写在代码里的）。
#
# 顺序有意义：**先 Gitee 后 GitHub**，国内优先。
MIRROR_BASE = (
    "https://gitee.com/haoruiM/xiaoma-ai-studio/releases/download/fonts-v1/{name}",
    "https://github.com/Harry-M-001/xiaoma-ai-studio/releases/download/fonts-v1/{name}",
)

# 清册。每款的 `family` 都是从字体文件里读出来的实测值，不是猜的。
# `sha256` 也是实测值；下载后**必须比对**，不然「下错文件」会一直到渲染出豆腐块才发现。
FONTS: tuple[dict[str, object], ...] = (
    {
        "key": "noto_sans",
        "label": "思源黑体",
        "family": "Noto Sans CJK SC",
        "files": ("NotoSansCJKsc-Regular.otf", "NotoSansCJKsc-Bold.otf"),
        "sha256": {
            "NotoSansCJKsc-Regular.otf": "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b",
            "NotoSansCJKsc-Bold.otf": "b5f0d1a190a7f9b43c310a8850630af12553df32c4c050543f9059732d9b4c0a",
        },
        "style": "黑体 · 通用",
        "hint": "最百搭的一款，默认就用它",
        "coverage": "GB18030 全部汉字 + 通用规范汉字表 8105 字 + 繁体大五码",
        "bundled": True,
        "bytes": 33_439_612,
    },
    {
        "key": "noto_serif",
        "label": "思源宋体",
        "family": "Noto Serif CJK SC",
        "files": ("NotoSerifCJKsc-Regular.otf",),
        "sha256": {
            "NotoSerifCJKsc-Regular.otf": "2a2eae2628df83556c54018c41e20fa532c1b862c5256ae8b3f23feb918d12ca",
        },
        "style": "宋体 · 叙事",
        "hint": "人文、历史、书卷气，适合旁白型字幕",
        "coverage": "65 535 字形 / 总字数 458 745，含全部大五码",
        "bundled": False,
        "bytes": 24_543_080,
    },
    {
        "key": "wenkai",
        "label": "霞鹜文楷",
        "family": "LXGW WenKai",
        "files": ("LXGWWenKai-Regular.ttf",),
        "sha256": {
            "LXGWWenKai-Regular.ttf": "39ad71264b588165b469e35e6afb162a378dacd1f95348160240ba9038ac3009",
        },
        "style": "楷体 · 古风",
        "hint": "楷体笔锋，古风 / 手写感；笔画细，别用在极小字号",
        "coverage": "GB 2312 全部 6763 字 + 通用规范汉字表，另补扩展 A 区与部分扩展 B 区",
        "bundled": False,
        "bytes": 25_575_676,
    },
    {
        "key": "smiley",
        "label": "得意黑",
        "family": "Smiley Sans Oblique",
        "files": ("SmileySans-Oblique.ttf",),
        "sha256": {
            "SmileySans-Oblique.ttf": "b447d7e781f08bc95c4c9f23ba71ed2b8ebb639aa7184485c71c4ca5afcd25c4",
        },
        "style": "标题 · 美术字",
        "hint": "倾斜锐角、海报感，只建议给标题用",
        # 这一条必须在界面上说出来：拿它排正文，遇到人名里的生僻字就会缺字
        "coverage": "只覆盖通用规范汉字表 8105 字（简体），繁体与生僻字会缺",
        "bundled": False,
        "bytes": 2_629_764,
    },
)

_BY_KEY = {str(f["key"]): f for f in FONTS}


def download_url(name: str, mirror: int = 0) -> str:
    """某个字体文件的下载地址。`mirror` 越界时夹到最后一个。"""
    idx = max(0, min(len(MIRROR_BASE) - 1, int(mirror)))
    return MIRROR_BASE[idx].format(name=name)


def download_size(key: object) -> int:
    """这款字体要下多少字节（界面直接展示，别让用户下完才发现很大）。"""
    f = font(resolve_key(key))
    return int(f["bytes"]) if f else 0


def font_status(key: object) -> str:
    """这款字体现在什么状态：`"ok"` / `"missing"`（没下过）/ `"corrupt"`（下了但内容不对）。

    把「没下过」与「下坏了」分开是有意义的：前者要用户点一下下载，后者要他
    **重新**下载一次——同一句提示会让人以为「我明明下过了」。
    """
    bad = verify_installed(key)
    if not bad:
        return "ok"
    if all(find_file(n) is None for n in bad):
        return "missing"
    return "corrupt"


def list_fonts() -> list[dict[str, object]]:
    """给界面的字体列表：带上「能不能用」「要下多大」。顺序就是清册里的声明顺序。

    `present` 用的是**校验过的**结论而不是「文件在不在」——否则一个半截文件会让界面
    显示「可用」，而导出时才发现问题（或者更糟：libass 静默回落系统字体）。
    哈希结果有缓存，所以这不比存在性检查慢。
    """
    out: list[dict[str, object]] = []
    for f in FONTS:
        item = dict(f)
        item["status"] = font_status(f["key"])
        item["present"] = item["status"] == "ok"
        item["downloadBytes"] = int(f["bytes"])
        # 只给界面的字段里不带 sha256 明细——那是校验用的，摆到前端没有意义
        item.pop("sha256", None)
        out.append(item)
    return out


async def install_font(key: object, *, timeout: int = 900) -> str:
    """下载并安装一款字体。返回一句可直接展示的结果说明。

    四条实现上的讲究：

    1. **先 Gitee 后 GitHub**：国内 Gitee 通、GitHub 的 releases 下载经常不通，
       所以按 `MIRROR_BASE` 的顺序逐个试，第一个成功就停。
    2. **必须比对 sha256**。不比的话，「下到一个截断的文件 / 下到别人放的同名文件」
       会一路走到渲染，最后表现成「中文变豆腐块」——那时候没人会想到是下载坏了。
    3. **先写 `.part` 再改名**。中断/校验失败时不会在字体目录里留下一个半截文件，
       而半截文件在 `missing_files()` 眼里是「已存在」——那就是最坏的一种状态：
       界面说可用、一点就崩。
    4. **下完再核对一次 `verify_installed()`**：确认真的齐了、而且校验和对得上才算成功。
    5. **「已经在本机」必须是校验过的结论**。只看文件在不在是不够的——一个被截断的文件
       在 `missing_files()` 眼里也是「在」，那样这里会回一句「不用再下」，然后拿着坏文件
       去渲染。这个洞是真跑验收里逮到的：把字体截成 1KB，它照样说「不用再下」。
    """
    f = font(key)
    if f is None:
        raise RuntimeError(f"没有这款字体：{key}")
    label = str(f["label"])
    if not verify_installed(f["key"]):
        return f"{label} 已经在本机了，不用再下"
    sums: dict[str, str] = dict(f.get("sha256") or {})  # type: ignore[arg-type]
    hashes = {}
    for name in f["files"]:  # type: ignore[union-attr]
        n = str(name)
        hashes[n] = sums.get(n, "")
        if not hashes[n]:
            raise RuntimeError(f"清册里没有 {n} 的校验和，不能下（比不了就不知道下到的是什么）")

    import httpx

    # 下到**数据目录**：不进仓库、不进包，且重装不丢
    dest_dir = user_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    got: list[str] = []
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=False) as client:
        for name, want in hashes.items():
            dest = dest_dir / name
            if dest.exists() and file_sha256(dest) == want:
                got.append(name)
                continue
            errors: list[str] = []
            for mirror in range(len(MIRROR_BASE)):
                url = download_url(name, mirror)
                tmp = dest.with_suffix(dest.suffix + ".part")
                try:
                    async with client.stream("GET", url) as resp:
                        resp.raise_for_status()
                        size = 0
                        with tmp.open("wb") as out:
                            async for chunk in resp.aiter_bytes(1 << 20):
                                out.write(chunk)
                                size += len(chunk)
                    if file_sha256(tmp) != want:
                        errors.append(f"{url} 校验和不符（下了 {size:,} 字节）")
                        tmp.unlink(missing_ok=True)
                        continue
                    tmp.replace(dest)
                    got.append(name)
                    break
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{url}：{type(e).__name__} {str(e)[:60]}")
                    tmp.unlink(missing_ok=True)
            else:
                raise RuntimeError(
                    f"{label} 下载失败，两个来源都试过了：\n" + "\n".join(errors)
                )
    left = verify_installed(f["key"])
    if left:
        raise RuntimeError(f"{label} 下完了但校验还是不过：{left}")
    total = sum((find_file(n).stat().st_size if find_file(n) else 0) for n in got)
    return f"{label} 已下载并校验通过（{len(got)} 个文件，{total / 1048576:.1f} MiB）"


def font(key: object) -> dict[str, object] | None:
    """按 key 取一款字体；认不出来回 None。"""
    return _BY_KEY.get(str(key or "").strip())


def resolve_key(key: object) -> str:
    """认不出来的字体就用内置那款，不报错——老画布 / 分享码里可能没有这个字段。

    与「认不出的转场当硬切」同一个口径：宁可给一个能用的默认，也不要让整张图打不开。
    """
    return str(key or "").strip() if font(key) else BUILTIN_KEY


def family_of(key: object) -> str:
    """取 ASS 里要写的 `Fontname`。**只从这里取**，别在别处拼字符串。"""
    f = font(resolve_key(key))
    assert f is not None  # resolve_key 已保证存在
    return str(f["family"])


def fonts_dir() -> Path:
    """内置字体目录（随包分发的那一份）。下载目录是 `user_dir()`。"""
    return BUILTIN_DIR


def missing_files(key: object) -> list[str]:
    """这款字体**缺哪些文件**。空列表 = 文件都在。

    只查存在性、**不校验内容**（快）。要判断「真的能用」请用 `verify_installed()`：
    一个被截断/改坏的文件在这里也算「在」，而拿它去渲染就是中文豆腐块。
    这个分工是刻意的——列表展示要快，导出前那次检查才值得算哈希。
    """
    f = font(resolve_key(key))
    if f is None:
        return []
    return [str(n) for n in f["files"] if find_file(str(n)) is None]  # type: ignore[union-attr]


# 哈希缓存：key 是 (路径, 大小, 修改时间)。改了文件 mtime 就变，缓存自动失效——
# 所以不必手动清。没有它的话，每次开界面都要把 33MB 的字体重算一遍哈希。
_HASH_CACHE: dict[tuple[str, int, float], str] = {}


def file_sha256(path: Path) -> str:
    import hashlib

    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime)
    hit = _HASH_CACHE.get(key)
    if hit:
        return hit
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    _HASH_CACHE[key] = digest
    return digest


def verify_installed(key: object) -> list[str]:
    """哪些文件**缺失或校验和不符**。空列表 = 真的可用。

    比 `missing_files()` 严：会把「文件在、但内容不对」也挑出来。这件事必须查，
    因为**一个半截文件在 `missing_files()` 眼里就是「已存在」**——那正是最坏的中间态：
    界面说可用、导出时才崩，或者更糟：libass 静默回落系统字体、出一部字体不对的片子。
    """
    f = font(resolve_key(key))
    if f is None:
        return []
    sums: dict[str, str] = dict(f.get("sha256") or {})  # type: ignore[arg-type]
    bad: list[str] = []
    for name in f["files"]:  # type: ignore[union-attr]
        n = str(name)
        p = find_file(n)
        if p is None:
            bad.append(n)
            continue
        want = sums.get(n, "")
        if want and file_sha256(p) != want:
            bad.append(n)
    return bad


def stage_fonts(workdir: Path, keys: list[str]) -> list[str]:
    """把这次要用到的字体硬链接进渲染工作目录，回实际挂上去的文件名。

    为什么要这一步（而不是直接给 libass 指字体目录）：

    - **躲开路径转义**。filtergraph 有两层转义，Windows 绝对路径得写成 `C\\\\:/path`
      才对；实测一级转义（文档里那种写法）是**失败**的。把字体放进工作目录，
      调用方只要写 `fontsdir=.` 就够了。
    - **只挂用到的那几份**。libass 会把目录里的字体全量解析，80MB 与 16MB 差一倍时间。

    同卷用硬链接（实测准备耗时 0.0 ms）；跨卷或权限不允许时退回复制，行为一样、只是慢。
    """
    staged: list[str] = []
    workdir.mkdir(parents=True, exist_ok=True)
    for key in keys:
        f = font(resolve_key(key))
        if f is None:
            continue
        for name in f["files"]:  # type: ignore[union-attr]
            src = find_file(str(name))
            if src is None:
                continue
            dst = workdir / src.name
            if dst.exists():
                staged.append(dst.name)
                continue
            try:
                os.link(src, dst)
            except OSError:
                # 跨卷 / 网络盘 / 权限问题：退回复制。**不静默跳过**——跳过了这一步，
                # libass 会回落系统字体，中文变豆腐块而 ffmpeg 不报错
                shutil.copy2(src, dst)
            staged.append(dst.name)
    return staged


def check_ready(keys: list[str]) -> str:
    """导出前的能力体检。空串 = 可以用；否则是一句可直接展示的理由。

    三件事分开说清楚——**滤镜不在** / **字体没下过** / **字体下坏了**，要用户做的事
    完全不一样，合成一句话会让人去改错的地方。所以逐条判、逐条说。

    用的是 `verify_installed()`（**校验和**，不是「文件在不在」）：这一条是导出前的最后
    一道关，放一个坏文件过去，libass 会静默回落系统字体、出一部字体不对的片子。
    哈希有缓存，所以这次检查不比存在性检查慢。
    """
    if not filter_available():
        return (
            "这台机器上的 ffmpeg 没有字幕滤镜（libass），烧不了字幕。"
            "换一个完整的 ffmpeg 构建就行——「系统设置 → 环境体检」里能看到当前用的是哪一个"
        )
    missing: list[str] = []
    corrupt: list[str] = []
    for k in keys:
        st = font_status(k)
        if st == "missing":
            missing.append(str((font(k) or {}).get("label") or k))
        elif st == "corrupt":
            corrupt.append(str((font(k) or {}).get("label") or k))
    if missing:
        return (
            f"这些字体还没下载到本机：{'、'.join(missing)}。"
            "在「字体」里点一下下载，或者换一款已经能用的版式"
        )
    if corrupt:
        return (
            f"这些字体的文件损坏了（校验和不符）：{'、'.join(corrupt)}。"
            "重新下载一次就好——不修的话 libass 会悄悄换一款系统字体渲染，"
            "出来的是「有字幕但字体不对」的片子"
        )
    return ""


def usable_keys() -> list[str]:
    """本机现在就能用的字体（内置 + 已下载且校验通过）。界面用它在选择器里标「可用」。"""
    return [str(f["key"]) for f in FONTS if font_status(f["key"]) == "ok"]


def available() -> bool:
    """至少有一款能用，字幕功能就成立。"""
    return bool(usable_keys())


def bundle_bytes() -> int:
    """随包分发的那部分体积，给打包脚本与出厂检查核对用。"""
    return sum(int(f["bytes"]) for f in FONTS if f["bundled"])


def filter_available() -> bool:
    """ffmpeg 有没有 `ass` 滤镜（libass）。没有就没法烧字幕。"""
    return ffmpeg_service.subtitle_filter_available()


def assert_manifest_consistent() -> None:
    """清册自检：key 唯一、文件不重复、内置那款必须真的在磁盘上、内置的有且只有一款。

    在应用启动与单测里都调一次。**「内置的字体却在磁盘上找不到」是最坏的一种情形**：
    界面上看着能用、一点却报错——所以宁可启动时就炸。钉住「有且只有一款内置」也一样：
    内置多了就是往每个用户的包里塞体积，那正是要避免的。
    """
    keys = [str(f["key"]) for f in FONTS]
    if len(keys) != len(set(keys)):
        raise RuntimeError(f"字体清册 key 重复：{keys}")
    seen: dict[str, str] = {}
    for f in FONTS:
        sums: dict[str, str] = dict(f.get("sha256") or {})  # type: ignore[arg-type]
        for name in f["files"]:  # type: ignore[union-attr]
            n = str(name)
            if n in seen:
                raise RuntimeError(f"字体文件被两款字体共用：{n}（{seen[n]} / {f['key']}）")
            seen[n] = str(f["key"])
            # **非内置的每一份都必须有校验和**：没有它就比不了，等于「下到什么算什么」——
            # 那种错会一路走到渲染，最后表现成中文豆腐块，没人会想到是下载坏了。
            if not f["bundled"] and not sums.get(n):
                raise RuntimeError(f"{f['key']} 的 {n} 没有 sha256，不能提供下载")
        if not f["bundled"]:
            continue
        gone = missing_files(f["key"])
        if gone:
            raise RuntimeError(
                f"内置字体 {f['key']} 的文件不在磁盘上：{gone}"
                "——它是随包分发的那一款，缺了就等于「打开就能用」这条不成立"
            )
        for n in f["files"]:  # type: ignore[union-attr]
            found = find_file(str(n))
            if found is None or found.parent != BUILTIN_DIR:
                raise RuntimeError(
                    f"内置字体 {n} 应当就在 {BUILTIN_DIR} 里（随包分发的那一份），实际在 "
                    f"{found.parent if found else '不存在'}"
                )
    bundled = [str(f["key"]) for f in FONTS if f["bundled"]]
    if bundled != [BUILTIN_KEY]:
        raise RuntimeError(f"内置字体应当有且只有 {BUILTIN_KEY} 一款，实际是 {bundled}")
