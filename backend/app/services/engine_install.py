"""本机引擎的落地层：**检测这台机器 → 下载（续传 + 校验）→ 解压 → 删掉**。

纯口径在 `local_engines.py`（清单、硬件档位判断、标志文件匹配），这里只做 IO。

## 四条实现口径（每一条都对应一个真踩过的坑）

1. **体积以清单为准，而清单里的数来自上游**。下载到多少字节算完，不看「流结束了没有」
   （断流与下完在 httpx 里长得一样），只比 `engine.size`。
2. **续传时收到 416 就是「已经完整」**。`Range: bytes=N-` 的起点落到末尾之后时服务器回 416；
   把它当错误处理会把一份已经下好的几百 MB 文件删掉重下（这一版的探针真删过一次）。
3. **先解到临时目录、成功了再改名**。中途失败留在盘上的必须是**一个临时目录**，
   而不是「看起来装好了、其实只有一半」的安装目录——后者最难查（用户会以为能用）。
4. **解压要挡住路径穿越**。压缩包来自上游，但 `zipfile` / `tarfile` 默认会老老实实写出
   `../../` 这种成员；标准库的 `filter="data"`（3.11.4+）会挡，旧解释器上没有就用自查。

## 同一时间只允许一条

下载是几百 MB 的事，同时开两条会互相抢带宽、也让「下到哪儿了」说不清。
所以和样片渲染同一条口径：已经在下了就明确拒绝（前端据此把按钮置灰）。
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
import zipfile
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ProviderService
from app.schemas import ModelSpec
from app.services import local_engines as le
from app.services import local_tts as le_tts

logger = logging.getLogger("xiaoma.engines")

# 硬件检测带缓存：本机引擎页一次要问好几件事，而注册表 / vulkaninfo 都不便宜。
# 与 ollama 探测同一条口径（60 秒），用户点「重新检测」时 force。
_HW_TTL = 60.0
_hw_cache: tuple[float, le.Hardware] | None = None

# 下载块大小与进度刷新阈值
_CHUNK = 1 << 20
_TIMEOUT = httpx.Timeout(60.0, connect=15.0, read=60.0)
# 重试之间歇一下。三条路（直连 / 系统代理 / 直连）之间只隔 2 秒：这几条路的差别
# 是「出口不同」，不是「对方过载」，等久了没有意义。
_RETRY_PAUSE = 2.0

# **「活着但在爬」也要换路。** 这一版实测同一个文件在不同出口上能差十几倍：
# 官方直连 8MB 用 4.2 秒（≈113MB/分钟），而走本机代理时 8MB 要 40 秒以上（≈12MB/分钟），
# 甚至掉到 1MB/分钟——而**下载不会报错**，不换的话用户就对着一个慢慢爬的进度条干等
# （140MB 要一百多分钟，而快的路只要一两分钟）。
# 规则：大文件的前 `_PROBE_BYTES` 必须在 `_PROBE_SECONDS` 内下完，否则换下一条路
# （已下到的部分留着，下一条路会续传，见 `_download` 里的 Range）。
_PROBE_BYTES = 8 << 20
_PROBE_SECONDS = 60.0
_PROBE_MIN_FILE = 16 << 20

# 当前这一条下载任务（同一时间只允许一条）
_job: dict | None = None
_task: asyncio.Task | None = None

PHASES = ("downloading", "verifying", "unpacking", "done", "error", "cancelled")
PHASE_LABELS = {
    "downloading": "下载中",
    "verifying": "校验中",
    "unpacking": "解压中",
    "done": "已完成",
    "error": "失败",
    "cancelled": "已停止",
}


# ============================================================ 路径


def root_dir() -> Path:
    """装到用户的**数据目录**里（跟着数据走）。

    刻意不放进仓库、也不放进便携包：`make_portable.py` 只拷 `backend/app` 与前端产物，
    所以这几百 MB 既不会让便携包变大，也不会在升级时被覆盖掉。
    """
    p = settings.data_dir / "engines"
    p.mkdir(parents=True, exist_ok=True)
    return p


def install_dir(key: str) -> Path:
    return root_dir() / str(key)


def archive_dir() -> Path:
    p = root_dir() / "_downloads"
    p.mkdir(parents=True, exist_ok=True)
    return p


def archive_path(engine: le.Engine) -> Path:
    return archive_dir() / engine.filename


# ============================================================ 硬件检测


def _windows_gpus() -> tuple[str, ...]:
    """从注册表里读显卡名（`Win32_VideoController` 那条路要 wmic/PowerShell，太慢）。

    键路径是显示适配器类的固定 GUID（微软文档里的 `{4d36e968-e325-11ce-bfc1-08002be10318}`），
    下面按 `0000`、`0001`… 编号排；`DriverDesc` 就是设备管理器里显示的那个名字。
    读不到就返回空——**不猜**（猜错的表现是界面上写着「检测到独立显卡」而实际没有）。
    """
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:  # 非 Windows
        return ()
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    names: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub) as k:
                        desc = str(winreg.QueryValueEx(k, "DriverDesc")[0] or "").strip()
                except OSError:
                    continue
                if desc and desc not in names:
                    names.append(desc)
    except OSError:
        return ()
    return tuple(n for n in names if not _is_virtual_adapter(n))


# 明显不是「能算的显卡」的那些显示适配器：远程桌面的虚拟显示、投屏软件装的 IDD 设备等。
# 不过滤的表现是界面上写着「检测到独立显卡」（这台机器上就有一个 OrayIddDriver Device），
# 而用户按这句话去装超分，跑起来才发现用的是集显。
_VIRTUAL_HINTS = ("idd", "virtual", "mirror", "basic display", "remote display", "spacedesk",
                  "parsec", "usb display")


def _is_virtual_adapter(name: str) -> bool:
    low = str(name or "").lower()
    return any(hint in low for hint in _VIRTUAL_HINTS)


def _nvidia_gpus() -> tuple[str, ...]:
    """兜底：`nvidia-smi -L`。没装驱动或没这个命令时返回空。"""
    exe = _smi_binary()
    if not exe:
        return ()
    try:
        r = subprocess.run([exe, "-L"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=8)
    except (OSError, subprocess.SubprocessError):
        return ()
    if r.returncode != 0:
        return ()
    out = []
    for line in (r.stdout or "").splitlines():
        name = line.split("(UUID", 1)[0].replace("GPU 0:", "").strip()
        if name:
            out.append(name)
    return tuple(out)


def _looks_nvidia(name: str) -> bool:
    """名字看着像 N 卡就算（注册表里的 `DriverDesc` 与 `nvidia-smi -L` 的名字格式不同）。"""
    low = str(name or "").lower()
    return "nvidia" in low or "geforce" in low or "rtx" in low


def _smi_binary() -> str:
    """`nvidia-smi` 在哪。

    **不能只靠 PATH**：实测这台机器的 PATH 里没有 System32（某些环境就是这样），
    而驱动确实把 nvidia-smi 装在 `C:\\Windows\\System32\\` 下了。只查 PATH 的表现是
    「明明有 N 卡，却读不到显存」——而显存正是这一版的解锁门槛。
    """
    found = shutil.which("nvidia-smi")
    if found:
        return found
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    program_files = os.environ.get("ProgramFiles") or r"C:\Program Files"
    for path in (
        Path(root) / "System32" / "nvidia-smi.exe",
        Path(program_files) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe",
    ):
        if path.exists():
            return str(path)
    return ""


def _registry_vram_bytes() -> int:
    """注册表里的显存字节数：**只认 N 卡**那几个适配器的最大值。

    键值是 `HardwareInformation.qwMemorySize`（QWORD）。**不能用 WMI 的 `AdapterRAM`**：
    那个字段是 32 位，超过 4GB 就溢出——8GB 的卡会读成 4095MB，正好卡在这类门槛上，
    而且判错的方向刚好是「不给你用」。
    """
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:  # 非 Windows
        return 0
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    best = 0
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub) as k:
                        desc = str(winreg.QueryValueEx(k, "DriverDesc")[0] or "")
                        size = int(winreg.QueryValueEx(k, "HardwareInformation.qwMemorySize")[0])
                except (OSError, ValueError, TypeError):
                    continue
                if _looks_nvidia(desc):
                    best = max(best, size)
    except OSError:
        return 0
    return best


def _vram_gb() -> float:
    """独显显存（GB）。**读不到就是 0，不按型号猜**。

    两条路都要：`nvidia-smi` 是驱动自己报的（0.07 秒，最准），注册表那条不需要任何工具、
    但要求驱动把 `qwMemorySize` 写进去（某些驱动版本不写）。先问 smi，读不到再看注册表。
    """
    exe = _smi_binary()
    if exe:
        try:
            r = subprocess.run(
                [exe, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8,
            )
            if r.returncode == 0:
                got = le.vram_gb_from_smi(r.stdout)
                if got:
                    return got
        except (OSError, subprocess.SubprocessError):
            pass
    return le.vram_gb_from_bytes(_registry_vram_bytes())


def _vulkaninfo_device() -> str:
    """`vulkaninfo --summary` 的第一块设备名——比我门自己解析 ICD 更硬。

    它不在（多数机器上也没有）就返回空，由调用方退回「看加载器在不在」。
    """
    exe = shutil.which("vulkaninfo") or shutil.which("vulkaninfo.exe")
    if not exe:
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "vulkaninfo.exe"
        exe = str(system32) if system32.exists() else ""
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, "--summary"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in (r.stdout or "").splitlines():
        text = line.strip()
        if text.lower().startswith("devicename"):
            return text.split("=", 1)[-1].strip()
    return ""


def _vulkan_icd_registered() -> bool:
    """注册表里有没有驱动登记的 Vulkan ICD（`...\\Khronos\\Vulkan\\Drivers` 下的值）。

    这一条比「`vulkan-1.dll` 在不在」有意义得多：加载器是系统组件、几乎总在，
    而**没有任何 ICD 时一个设备都跑不起来**（`vulkaninfo` 会直接说 no ICD）。
    """
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Khronos\Vulkan\Drivers") as k:
            winreg.EnumValue(k, 0)
            return True
    except OSError:
        return False


def _vulkan_available() -> bool:
    """有没有可用的 Vulkan：先看加载器，再看**有没有驱动在提供实现**。

    只看加载器（`vulkan-1.dll`）是不够的：它在 Windows 上因为是系统组件而普遍存在，
    但没有任何 ICD 时 `vulkaninfo` 会直接报「no ICD」——那时界面上说「Vulkan 可用」
    就是骗人。所以：加载器在 **且**（注册表里有 ICD **或** 设备名读得到）才算可用。
    """
    if sys.platform.startswith("win"):
        loader = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "vulkan-1.dll"
        if not loader.exists():
            return False
        if _vulkan_icd_registered():
            return True
        # 注册表读不到（有的驱动不写这里）时，用 vulkaninfo 兜底
        return bool(_vulkaninfo_device())
    if sys.platform == "darwin":
        return Path("/usr/local/lib/libvulkan.dylib").exists() or Path(
            "/opt/homebrew/lib/libvulkan.dylib").exists()
    return any(Path(p).exists() for p in (
        "/usr/lib/x86_64-linux-gnu/libvulkan.so.1",
        "/usr/lib/libvulkan.so.1",
        "/usr/lib/aarch64-linux-gnu/libvulkan.so.1",
    ))


def _ram_gb() -> float:
    try:
        if sys.platform.startswith("win"):
            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            stat = _MemStatus()
            stat.dwLength = ctypes.sizeof(_MemStatus)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return 0.0
            return round(stat.ullTotalPhys / (1024 ** 3), 1)
        pages = os.sysconf("SC_PHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
        return round(pages * size / (1024 ** 3), 1)
    except Exception:  # noqa: BLE001
        return 0.0


def hardware(force: bool = False) -> le.Hardware:
    """这台机器的体检。**检测不出来的就是空/0，不猜**。"""
    global _hw_cache
    if not force and _hw_cache is not None:
        stamp, cached = _hw_cache
        if time.monotonic() - stamp < _HW_TTL:
            return cached

    gpus = _windows_gpus()
    if not gpus:
        gpus = _nvidia_gpus()
    device = _vulkaninfo_device()
    hw = le.Hardware(
        gpu_names=gpus,
        nvidia=any(_looks_nvidia(g) for g in gpus) or bool(_nvidia_gpus()),
        vulkan=_vulkan_available(),
        vulkan_device=device,
        cores=os.cpu_count() or 0,
        ram_gb=_ram_gb(),
        vram_gb=_vram_gb(),
    )
    _hw_cache = (time.monotonic(), hw)
    return hw


# ============================================================ 状态


def installed(engine: le.Engine) -> bool:
    """装好了没有：看安装目录里有没有 `marker` 命中的文件。"""
    return bool(le.marker_hit(engine, install_dir(engine.key)))


def _archive_state(engine: le.Engine) -> dict:
    # 「不给下载」那一档没有包可谈：它的 `filename` 是空的，而空文件名会拼出
    # **`_downloads` 这个目录本身**——`exists()` 为真、`st_size` 是 0，
    # 于是「体积对不对」会拿一个目录去比 size，删引擎时更会把整个下载目录端掉。
    # 所以先短路：没有包名的档，就当它没有包。
    if not engine.filename:
        return {"archiveBytes": 0, "archiveComplete": False, "archivePath": ""}
    path = archive_path(engine)
    if not path.exists():
        return {"archiveBytes": 0, "archiveComplete": False, "archivePath": str(path)}
    size = path.stat().st_size
    return {
        "archiveBytes": size,
        "archiveComplete": size == engine.size,
        "archivePath": str(path),
    }


def state(engine: le.Engine) -> dict:
    """一个引擎此刻的状态（纯读盘，不下载也不校验哈希）。"""
    marker = le.marker_hit(engine, install_dir(engine.key))
    arch = _archive_state(engine)
    job = dict(_job) if (_job or {}).get("key") == engine.key else None
    return {
        "installed": bool(marker),
        "installedMarker": marker,
        **arch,
        "downloading": bool(job and job.get("phase") in ("downloading", "verifying", "unpacking")),
        "job": job,
    }


def rows() -> list[dict]:
    """界面上要显示的这几行。**不够门槛的档整行不下发**（理由见 `le.unlocked`）。"""
    hw = hardware()
    keys = {e.key for e in le.engines() if installed(e)}
    out = []
    for e in le.engines():
        if not le.unlocked(e, hw):
            continue
        item = le.row(e, hw, state(e))
        item["missingNeeds"] = le.missing_needs(e, keys)
        item["needsLabel"] = "、".join(
            (le.by_key(k).label if le.by_key(k) else k) for k in item["missingNeeds"]
        )
        out.append(item)
    return out


def locked_rows() -> list[dict]:
    """这一版**没显示**的档（被硬件门槛拦下的）。

    为什么要回报、而不是默默滤掉：用户会看到「别人的界面上有一档、我这台没有」。
    页面末尾拿这几行说一句「还有一档：要 ≥6GB 的 N 卡，这台机器显存 X」——
    **隐藏本身也要有交代**，沉默才是最难解释的那种。
    """
    hw = hardware()
    return [
        {
            "key": e.key,
            "label": e.label,
            "why": e.why,
            "minVramGb": e.min_vram_gb,
            "reason": le.lock_reason(e, hw),
        }
        for e in le.engines()
        if not le.unlocked(e, hw)
    ]


def job_snapshot() -> dict | None:
    return dict(_job) if _job else None


def external_tier(key: str) -> dict:
    """「只给地址、不代装」那一档此刻的状态：下没下、下好了在哪儿。

    为什么只回安装包的状态、不回「装没装」：这一档（去字幕高质量档 VSR）是个**自带
    整套运行环境的独立安装程序**，装完是它自己的界面，它的目录不归我们管，所以**没有
    判据**——`marker` 是空的（见 `Engine.marker`）。与其猜一个「装好了」的结论，不如把
    事实（安装包在不在、路径是什么）原样交给界面，让界面照实说。

    未知的 key 回空字典：界面据此不显示这一块，而不是显示一个字段全是 undefined 的空壳。
    """
    engine = le.by_key(key)
    if engine is None:
        return {}
    arch = _archive_state(engine)
    job = dict(_job) if (_job or {}).get("key") == engine.key else None
    # 「下了一半」才有人话体积：没下（0 字节）与下完整了都是空——「已经下了 0 B」
    # 这种话写进界面只会让人以为哪里错了
    partial = 0 if arch["archiveComplete"] else int(arch["archiveBytes"])
    return {
        "key": engine.key,
        "label": engine.label,
        "size": engine.size,
        "sizeText": engine.size_text,
        "license": engine.license,
        "homepage": engine.homepage,
        "url": engine.url,
        "filename": engine.filename,
        "why": engine.why,
        "note": engine.note,
        # 没下也回路径：界面照实说「它会下到这儿」，比留一句「还没下」有用
        "installerPath": arch["archivePath"],
        "downloaded": bool(arch["archiveComplete"]),
        "partialBytes": partial,
        "partialText": le.human_size(partial) if partial else "",
        "downloading": bool(job and job.get("phase") in ("downloading", "verifying", "unpacking")),
    }


# ============================================================ 下载 + 解压


def _verify_and_progress(job: dict, path: Path, engine: le.Engine) -> None:
    """整包算一遍 sha256。**这是唯一能证明「下对了」的东西**，所以不计较这几秒。"""
    job["phase"] = "verifying"
    job["done"] = engine.size
    got = le.sha256_file(path)
    if not le.digest_matches(engine, got):
        raise ValueError(
            f"{engine.filename} 的校验和不一致：下载到的是 {got[:12]}…，"
            f"清单里是 {engine.sha256[:12]}…。文件已删掉，请重新下载一次"
        )


def _safe_members(names: list[str]) -> None:
    """挡住路径穿越。压缩包来自上游，但不该因此假设它是干净的。"""
    for name in names:
        pure = Path(name.replace("\\", "/"))
        if pure.is_absolute() or any(part == ".." for part in pure.parts):
            raise ValueError(f"压缩包里有不安全的路径：{name}")


def _flatten_single_dir(target: Path) -> None:
    """只有一个顶层目录时把它拆上来，让「装好了没有」的判据与安装路径都稳定。"""
    children = list(target.iterdir())
    dirs = [c for c in children if c.is_dir()]
    files = [c for c in children if c.is_file()]
    if len(dirs) != 1 or files:
        return
    only = dirs[0]
    for item in only.iterdir():
        shutil.move(str(item), str(target / item.name))
    only.rmdir()


def _unpack(engine: le.Engine, archive: Path, job: dict) -> None:
    job["phase"] = "unpacking"
    final = install_dir(engine.key)
    staging = final.parent / f".{engine.key}-{uuid.uuid4().hex[:8]}.part"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        if engine.archive == "zip":
            with zipfile.ZipFile(archive) as z:
                names = z.namelist()
                _safe_members(names)
                z.extractall(staging)
        elif engine.archive == "tar.bz2":
            with tarfile.open(archive, "r:bz2") as t:
                names = t.getnames()
                _safe_members(names)
                if hasattr(tarfile, "data_filter"):
                    t.extractall(staging, filter="data")
                else:  # 3.11.4 之前没有 data_filter
                    t.extractall(staging)
        else:
            raise ValueError(f"{engine.key} 是安装程序，我们不解压也不代装")
        _flatten_single_dir(staging)
        hit = le.marker_hit(engine, staging)
        if not hit:
            # 报的时候要指向**还在盘上的**那个东西：解压出来的临时目录马上就收走了，
            # 而下载下来的压缩包留着（要它去对上游的打包结构）。
            raise ValueError(
                f"解压完了但没找到 {engine.marker}（上游可能改了打包结构）。"
                f"要报这一问题请把 {archive.name} 一起发过来"
            )
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        staging.rename(final)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


class _RouteTooSlow(RuntimeError):
    """这条路「活着但在爬」——换下一条，不是失败。"""


def _error_text(exc: BaseException) -> str:
    """把异常说成一句人话。

    为什么专门写这个：httpx 在网络断掉时抛的异常**可能是空消息**
    （这一版实测：Kokoro 那次下载失败，界面上「上次失败：」后面什么都没有），
    而一句空错误比没有错误更糟——用户连搜都不知道搜什么。空的时候就报异常类型名。
    """
    text = str(exc).strip()
    if text:
        return text
    errno = getattr(exc, "errno", None)
    return f"{type(exc).__name__}{f'（errno {errno}）' if errno else ''}"


# 网络类的失败要额外告诉用户「还能怎么办」。这几类异常都是「没连上/断了」，
# 而不是「下游说不行」——前者换条路再试往往就好了。
_NETWORK_ERRORS = (
    httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ReadError,
    httpx.WriteError, httpx.RemoteProtocolError, httpx.NetworkError, httpx.ProxyError,
)


def _maybe_network_hint(text: str, exc: BaseException) -> str:
    """网络不通时补一句**能照做**的话。

    这几百 MB 的包都放在 GitHub 的 release 附件上，而它在国内时通时不通
    （这一版实测：同一个网络里，`release-assets.githubusercontent.com` 时通时断）。
    只报一句 ConnectTimeout 等于让用户干等，所以把三条出路写清楚。
    """
    if not isinstance(exc, _NETWORK_ERRORS):
        return text
    return (
        f"{text}\n下载走不通时可以：① 过一会儿再点一次「接着下载」（已下到的部分会留着）；"
        "② 自己在浏览器里把这个文件下好，放进 "
        f"{archive_dir()}，再点「校验」——校验通过就能装；"
        "③ 换网络或挂上代理（应用会同时尝试直连与系统代理）后重启，再点一次。"
    )


async def _download(engine: le.Engine, job: dict, *, use_env_proxy: bool) -> Path:
    """下载到 `_downloads/<文件名>`，能续就续。返回完整文件的路径。

    `use_env_proxy` 决定要不要读系统代理设置。**两条路都得试**，理由与
    `update_service._probe` 里记的是同一件事：httpx 在 Windows 上会读注册表里的
    IE/WinINET 代理，用户一开 VPN 请求就被静默导向代理出口（共享 IP 常被 GitHub 限流）；
    反过来，国内直连 GitHub 附件也常常不通。所以不是「听天由命」，而是轮流试。
    """
    path = archive_path(engine)
    have = path.stat().st_size if path.exists() else 0
    if have > engine.size:
        # 比清单大：上一版中途改过体积或上次写坏了，重来
        path.unlink(missing_ok=True)
        have = 0
    if have == engine.size:
        job["done"] = have
        return path

    job["phase"] = "downloading"
    job["done"] = have
    attempt_start = have
    started = time.monotonic()
    slow_reason = ""
    # 服务端**没按 Range** 返回时置位：有些代理会把整包塞给一个带 Range 的请求，
    # 于是本地那份成了「前缀 + 另一个整包」，永远过不了校验
    overflowed = False
    headers = {"Range": f"bytes={have}-"} if have else {}
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True,
                                 trust_env=use_env_proxy) as c:
        async with c.stream("GET", engine.url, headers=headers) as r:
            if r.status_code == 416:
                # 「起点在末尾之后」= 本地这份已经完整（探针里正是这里搞错过）
                job["done"] = path.stat().st_size
                return path
            r.raise_for_status()
            if have and r.status_code != 206:
                have = 0
                job["done"] = 0
                attempt_start = 0
                started = time.monotonic()
            mode = "ab" if have else "wb"
            last_note = 0.0
            with path.open(mode) as f:
                async for chunk in r.aiter_bytes(_CHUNK):
                    if job.get("cancelled"):
                        raise asyncio.CancelledError()
                    f.write(chunk)
                    job["done"] = job.get("done", 0) + len(chunk)
                    if job["done"] > engine.size:
                        # 比清单还大：说明服务端没有只给缺的那一段，而是又塞了一个整包
                        # （实测：走代理续传 rife 那个 411MB 的包时，本地文件变成了 556MB）。
                        # 本地这份已经废了。**当场作废**远好过让用户对着「校验和不一致」
                        # 反复重试——那种情况下每点一次都要重下一整包，而且看不出为什么。
                        overflowed = True
                        break
                    now = time.monotonic()
                    # 「活着但在爬」：大文件的前几 MB 超时就换下一条路（见文件头的常量注释）。
                    # **完全没有数据**那种卡住不在这里管——httpx 的读超时（60 秒）会兜住；
                    # 这个判据只管「有数据、但慢到来不及」。
                    if (engine.size >= _PROBE_MIN_FILE
                            and job["done"] - attempt_start < _PROBE_BYTES
                            and now - started > _PROBE_SECONDS):
                        # **只记原因、不在这里抛**：响应还没读完就抛异常时，退出
                        # `async with` 的收尾过程里 httpx 可能先报一句
                        # 「peer closed connection…」，把真正的原因盖掉。
                        # 先 break 让它把连接正常收掉，出了 with 再抛。
                        slow_reason = (
                            f"这条路太慢：{_PROBE_SECONDS:.0f} 秒才下了 "
                            f"{(job['done'] - attempt_start) / 1048576:.1f}MB"
                        )
                        break
                    if now - last_note > 5:
                        last_note = now
                        logger.info("%s 下载中：%.1f / %.1f MB",
                                    engine.key, job["done"] / 1048576, engine.size / 1048576)
    if overflowed:
        path.unlink(missing_ok=True)
        job["done"] = 0
        raise ValueError(
            "这个下载源没有按「从断点续传」给数据，把整包又塞了一遍，"
            "本地那份已经作废并删掉了。换一条路重新下"
        )
    if slow_reason:
        raise _RouteTooSlow(slow_reason)
    return path


async def _download_with_retries(engine: le.Engine, job: dict) -> Path:
    """下载，断了或**太慢**就换下一条路，直连与走系统代理轮流来。"""
    plan = (False, True, False)
    last: BaseException | None = None
    for attempt, use_env_proxy in enumerate(plan, start=1):
        try:
            return await _download(engine, job, use_env_proxy=use_env_proxy)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            job["attempt"] = attempt
            logger.info(
                "%s 第 %d/%d 次中断（%s%s），已下 %.1f MB",
                engine.key, attempt, len(plan), _error_text(e),
                "，走系统代理" if use_env_proxy else "，直连",
                (job.get("done") or 0) / 1048576,
            )
            if attempt < len(plan):
                await asyncio.sleep(_RETRY_PAUSE)
    raise last if last is not None else RuntimeError("下载失败（没有记录到原因）")


async def _run(engine: le.Engine, job: dict) -> None:
    try:
        path = await _download_with_retries(engine, job)
        got = path.stat().st_size
        if got != engine.size:
            raise ValueError(
                f"下载没下完：{got} 字节，应当是 {engine.size} 字节。"
                "已下载的部分留着，再点一次下载会从断的地方接着下"
            )
        await asyncio.to_thread(_verify_and_progress, job, path, engine)
        if engine.archive == "installer":
            # 安装程序只下不装：装它要走它自己的界面（731MB 自带一整套运行环境）
            job["phase"] = "done"
            job["note"] = "安装包已下好，请到「下载」目录里双击安装（我们不代装）"
            return
        await asyncio.to_thread(_unpack, engine, path, job)
        job["phase"] = "done"
        job["installedMarker"] = le.marker_hit(engine, install_dir(engine.key))
        logger.info("%s 装好了：%s", engine.key, job["installedMarker"])
    except asyncio.CancelledError:
        job["phase"] = "cancelled"
        raise
    except Exception as e:  # noqa: BLE001
        job["phase"] = "error"
        # **不许出现空错误**（httpx 断线时真的会抛空消息，见 `_error_text`）；
        # 网络类失败再补一句「还能怎么办」
        text = _error_text(e)
        if isinstance(e, _RouteTooSlow):
            text = (
                f"{text}（三条路都试过了：直连、系统代理、再直连。"
                "已下到的部分留着，过一会儿再点一次「接着下载」会从断的地方继续）"
            )
        job["error"] = _maybe_network_hint(text, e)
        # 校验不过的文件留着只会让用户反复重试同一个坏文件
        if "校验和不一致" in job["error"]:
            archive_path(engine).unlink(missing_ok=True)
        logger.warning("%s 安装失败：%s", engine.key, _error_text(e))
    finally:
        job["finishedAt"] = time.time()


async def start(key: str) -> dict:
    """开始下载 + 校验 + 解压。**同一时间只允许一条**。"""
    global _job, _task
    engine = le.by_key(key)
    if engine is None:
        raise ValueError(f"没有这个引擎：{key}")
    if engine.archive == "external":
        # 界面不给按钮只是「顺手」，**判据必须在服务端**：放过去的话，一个空的
        # `filename` 会让下载链把 `_downloads` 目录当成目标文件。
        raise ValueError(
            f"「{engine.label}」我们不提供下载：它要自己装 Python + PyTorch + CUDA，"
            "装法与用法看它项目主页上的说明"
        )
    if _job and _job.get("phase") in ("downloading", "verifying", "unpacking"):
        raise ValueError(f"已经有「{_job.get('label')}」在下载了，等它跑完或先停掉它")
    if installed(engine):
        raise ValueError(f"「{engine.label}」已经装好了")

    hw = hardware()
    level, _reason = le.verdict(engine, hw)
    if level == le.LEVEL_NO:
        raise ValueError(f"这台机器上跑不了「{engine.label}」：{_reason}")
    missing = le.missing_needs(engine, {e.key for e in le.engines() if installed(e)})
    if missing:
        names = "、".join((le.by_key(k).label if le.by_key(k) else k) for k in missing)
        raise ValueError(f"要先装「{names}」——模型得靠那个运行时跑")

    _job = {
        "key": engine.key,
        "label": engine.label,
        "phase": "downloading",
        "done": 0,
        "total": engine.size,
        "startedAt": time.time(),
        "error": "",
        "note": "",
        "cancelled": False,
    }
    _task = asyncio.create_task(_run(engine, _job))
    return dict(_job)


def cancel(key: str) -> dict:
    """停掉正在下的那一条。**已经下到的部分留着**，下次点下载会从断的地方续。"""
    if not _job or _job.get("key") != key:
        raise ValueError("这条没有在下载")
    if _job.get("phase") not in ("downloading", "verifying", "unpacking"):
        raise ValueError("它已经结束了")
    _job["cancelled"] = True
    if _task is not None:
        _task.cancel()
    _job["phase"] = "cancelled"
    _job["finishedAt"] = time.time()
    return dict(_job)


def _size_of(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _rm(path: Path) -> None:
    """删文件或整目录（不存在也不报错）。"""
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


async def remove(key: str) -> dict:
    """删掉装好的那一份（以及下好的压缩包）。磁盘空间是用户自己的，要能收回来。"""
    engine = le.by_key(key)
    if engine is None:
        raise ValueError(f"没有这个引擎：{key}")
    if _job and _job.get("key") == key and _job.get("phase") in (
            "downloading", "verifying", "unpacking"):
        raise ValueError("它正在下载，先停掉再删")
    freed = 0
    # 没有包名的档（「不给下载」那一路）不碰 `_downloads`——那个空文件名拼出来的
    # 是**下载目录本身**，删它会顺手把别的引擎下好的包一起端掉
    targets = [install_dir(key)] + ([archive_path(engine)] if engine.filename else [])
    for path in targets:
        if not path.exists():
            continue
        freed += await asyncio.to_thread(_size_of, path)
        await asyncio.to_thread(_rm, path)
    return {"key": key, "freed": freed, "freedText": le.human_size(freed)}


async def verify(key: str) -> dict:
    """把已经下好的压缩包**重新算一遍** sha256（怀疑文件坏了时用）。"""
    engine = le.by_key(key)
    if engine is None:
        raise ValueError(f"没有这个引擎：{key}")
    if not engine.filename:
        raise ValueError(f"「{engine.label}」我们不提供包，没有可校验的东西")
    path = archive_path(engine)
    if not path.exists():
        raise ValueError("还没有下载过这个引擎")
    size = path.stat().st_size
    if size != engine.size:
        return {"key": key, "ok": False, "bytes": size,
                "detail": f"体积就不对：{size} 字节，应当是 {engine.size} 字节"}
    got = await asyncio.to_thread(le.sha256_file, path)
    ok = le.digest_matches(engine, got)
    return {"key": key, "ok": ok, "bytes": size,
            "detail": "校验通过" if ok else f"校验和不一致：{got[:12]}…"}


# ============================================================ 已有的本机能力


async def installed_services(db: AsyncSession) -> list[dict]:
    """这台机器上**已经有的**本机能力（Ollama / ComfyUI / 用户自己填的本机服务）。

    刻意不另写一套「本机有没有 Ollama」的判据：`ollama_service.detect` 与
    `ProviderService` 表就是那份判据，这里只把结论收在同一页上。
    """
    from app.services import ollama_service

    out: list[dict] = []
    info = await ollama_service.detect()
    out.append({
        "key": "ollama",
        "label": "Ollama（本机文本模型）",
        "running": bool(info.get("running")),
        "detail": (f"已接入，本机有 {len(info.get('models') or [])} 个可用的对话模型"
                   if info.get("running") else "这台机器上没检测到在跑的 Ollama"),
        "route": "providers",
    })
    rows = (await db.execute(
        select(ProviderService).where(ProviderService.kind == "comfyui",
                                      ProviderService.enabled == True)  # noqa: E712
    )).scalars().all()
    out.append({
        "key": "comfyui",
        "label": "ComfyUI（本机出图 / 工作流）",
        "running": bool(rows),
        "detail": (f"已接入 {len(rows)} 个：{'、'.join(r.name for r in rows[:3])}"
                   if rows else "还没接入。已经有 ComfyUI 的话，超分与补帧可以先走它，不必再下这几档"),
        "route": "providers",
    })
    return out


# ============================================================ 接成本机配音模型
#
# 「装好了」与「能用上」是两件事：引擎躺在磁盘上不会让配音页多出一个选项。
# 这一节把「装好的 sherpa-onnx」**接成一条普通的模型服务**（kind=local_tts），
# 于是配音页、样片旁白、画布逐镜对白全都自动多出一个「本机跑」的选项——
# 那几条路都是从模型下拉里取音频模型的，一行都不用改。

LOCAL_TTS_URL = "local://sherpa-onnx"
LOCAL_TTS_MODEL = "kokoro"
LOCAL_TTS_LABEL = "本机跑 · Kokoro 中文（不花调用费）"


def _missing_engine_keys() -> list[str]:
    """本机配音要的两个引擎里还没装上的那些（顺序就是用户该下载的顺序）。"""
    gaps: list[str] = []
    for key in (le_tts.RUNTIME_KEY, le_tts.MODEL_KEY):
        engine = le.by_key(key)
        if engine is not None and not installed(engine):
            gaps.append(key)
    return gaps


async def local_tts_status(db: AsyncSession) -> dict:
    """本机配音这条线的状态：引擎装好没有、接成模型服务没有、有哪些音色。

    三件事分开说，因为用户能做的动作不同：**没装**要去下（这一页）、
    **装了没接**点一下接入、**接好了**去配音页选模型。
    """
    ready, why = le_tts.check_ready()
    row = (await db.execute(
        select(ProviderService).where(ProviderService.kind == "local_tts")
        .order_by(ProviderService.id)
    )).scalars().first()
    voices = le_tts.voice_list() if ready else []
    return {
        "ready": ready,
        "problem": why,
        "missingEngines": _missing_engine_keys(),
        "connected": bool(row and row.enabled),
        "disabled": bool(row and not row.enabled),
        "serviceId": row.id if row else None,
        "serviceName": row.name if row else "",
        "modelKey": f"{row.id}:{LOCAL_TTS_MODEL}" if row else "",
        "voices": [{"id": v.id, "label": v.label, "sid": v.sid} for v in voices],
        "voiceCount": len(voices),
        # 音色**有没有名字**要单独说：实测 Kokoro 的 int8 包里没有那张表（只按号选），
        # 而界面上的示例（「小焰=zf_xiaoxiao」）在那种包里是不成立的写法。
        "named": any(v.name for v in voices),
    }


async def connect_local_tts(db: AsyncSession) -> dict:
    """把装好的 sherpa-onnx 接成一个音频模型服务（重复点不会加出第二份）。"""
    from app.services import provider_store

    info = await local_tts_status(db)
    if not info["ready"]:
        raise ValueError(info["problem"] or "本机配音引擎还没装好")

    models = [ModelSpec(name=LOCAL_TTS_MODEL, modality="audio", label=LOCAL_TTS_LABEL)]
    await provider_store.upsert_by_base_url(
        db,
        name="本机配音（sherpa-onnx）",
        kind="local_tts",
        base_url=LOCAL_TTS_URL,
        # 本机服务没有凭据：适配器也不看这个字段（云服务少填 Key 才是配置错误）
        api_key="",
        models=models,
        sort_order=9,
    )
    return await local_tts_status(db)


async def disconnect_local_tts(db: AsyncSession) -> dict:
    """把这条服务停用（**不删**：已有的配音资产还挂在它名下，而用户可能只是想换一个）。

    停用而不是删除：删掉之后资产库里那些「配音 · xxx」的来源服务就没了，
    而用户点「停用」时的本意通常只是「先别用它」。
    """
    row = (await db.execute(
        select(ProviderService).where(ProviderService.kind == "local_tts")
    )).scalars().first()
    if row is not None:
        row.enabled = False
        await db.commit()
    return await local_tts_status(db)


async def shutdown() -> None:
    global _task
    if _task is not None and not _task.done():
        if _job:
            _job["cancelled"] = True
        _task.cancel()
        try:
            await _task
        except BaseException:  # noqa: BLE001  取消与失败都不该在关闭流程里往上抛
            pass
    _task = None
