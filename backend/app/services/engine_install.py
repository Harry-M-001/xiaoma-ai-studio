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
from app.services import local_engines as le

logger = logging.getLogger("xiaoma.engines")

# 硬件检测带缓存：本机引擎页一次要问好几件事，而注册表 / vulkaninfo 都不便宜。
# 与 ollama 探测同一条口径（60 秒），用户点「重新检测」时 force。
_HW_TTL = 60.0
_hw_cache: tuple[float, le.Hardware] | None = None

# 下载块大小与进度刷新阈值
_CHUNK = 1 << 20
_TIMEOUT = httpx.Timeout(60.0, connect=15.0, read=60.0)

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
    exe = shutil.which("nvidia-smi")
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
        nvidia=any("nvidia" in g.lower() or "geforce" in g.lower() or "rtx" in g.lower()
                   for g in gpus) or bool(_nvidia_gpus()),
        vulkan=_vulkan_available(),
        vulkan_device=device,
        cores=os.cpu_count() or 0,
        ram_gb=_ram_gb(),
    )
    _hw_cache = (time.monotonic(), hw)
    return hw


# ============================================================ 状态


def installed(engine: le.Engine) -> bool:
    """装好了没有：看安装目录里有没有 `marker` 命中的文件。"""
    return bool(le.marker_hit(engine, install_dir(engine.key)))


def _archive_state(engine: le.Engine) -> dict:
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
    hw = hardware()
    keys = {e.key for e in le.engines() if installed(e)}
    out = []
    for e in le.engines():
        item = le.row(e, hw, state(e))
        item["missingNeeds"] = le.missing_needs(e, keys)
        item["needsLabel"] = "、".join(
            (le.by_key(k).label if le.by_key(k) else k) for k in item["missingNeeds"]
        )
        out.append(item)
    return out


def job_snapshot() -> dict | None:
    return dict(_job) if _job else None


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


async def _download(engine: le.Engine, job: dict) -> Path:
    """下载到 `_downloads/<文件名>`，能续就续。返回完整文件的路径。"""
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
    headers = {"Range": f"bytes={have}-"} if have else {}
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True, trust_env=False) as c:
        async with c.stream("GET", engine.url, headers=headers) as r:
            if r.status_code == 416:
                # 「起点在末尾之后」= 本地这份已经完整（探针里正是这里搞错过）
                job["done"] = path.stat().st_size
                return path
            r.raise_for_status()
            if have and r.status_code != 206:
                have = 0
                job["done"] = 0
            mode = "ab" if have else "wb"
            last_note = 0.0
            with path.open(mode) as f:
                async for chunk in r.aiter_bytes(_CHUNK):
                    if job.get("cancelled"):
                        raise asyncio.CancelledError()
                    f.write(chunk)
                    job["done"] = job.get("done", 0) + len(chunk)
                    now = time.monotonic()
                    if now - last_note > 5:
                        last_note = now
                        logger.info("%s 下载中：%.1f / %.1f MB",
                                    engine.key, job["done"] / 1048576, engine.size / 1048576)
    return path


async def _run(engine: le.Engine, job: dict) -> None:
    try:
        path = await _download(engine, job)
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
        job["error"] = str(e)
        # 校验不过的文件留着只会让用户反复重试同一个坏文件
        if "校验和不一致" in str(e):
            archive_path(engine).unlink(missing_ok=True)
        logger.warning("%s 安装失败：%s", engine.key, e)
    finally:
        job["finishedAt"] = time.time()


async def start(key: str) -> dict:
    """开始下载 + 校验 + 解压。**同一时间只允许一条**。"""
    global _job, _task
    engine = le.by_key(key)
    if engine is None:
        raise ValueError(f"没有这个引擎：{key}")
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
    for path in (install_dir(key), archive_path(engine)):
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


async def shutdown() -> None:
    """应用关闭时把在下的那条停下来（不要留一个孤儿任务）。"""
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
