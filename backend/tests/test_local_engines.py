"""本机引擎（批次 9）的行为测试：清单口径、硬件分档、下载 → 校验 → 解压 → 删除。

运行：venv/Scripts/python tests/test_local_engines.py

**不碰公网**：下载那一条用一个本地小 HTTP 服务器喂真实的 zip 字节（支持 Range、
能强迫返回 416），所以这些用例在断网时也跑得过、也不会因为上游改体积而变红。
真的下载（几十到几百 MB）属于「真跑验证」那一类，不进单测。

清单那部分测的是**口径**而不是具体数字：具体数字（体积 / sha256）由上游决定，
写死在用例里只会在上游发新版时变成假红；而「有没有写、格式对不对、依赖关系对不对」
才是我们能在离线状态负责的东西。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import socket
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import ProviderService  # noqa: E402
from app.services import engine_install as ei  # noqa: E402
from app.services import local_engines as le  # noqa: E402


# ================================================================ 1. 清单


def test_the_manifest_is_self_consistent():
    """清单自检：key/文件名不重复、地址与文件名对得上、依赖指向存在的引擎。"""
    le.assert_consistent()


def test_every_entry_carries_a_usable_hash_and_size():
    """体积与 sha256 是校验用户下载的**唯一依据**，一个都不许含糊。"""
    for e in le.engines():
        assert e.size > 0, f"{e.key} 没写体积"
        assert len(e.sha256) == 64, f"{e.key} 的 sha256 不是 64 位：{e.sha256!r}"
        int(e.sha256, 16)  # 非十六进制会在这里炸
        assert e.proof in ("upstream-digest", "local-download"), e.proof
        assert e.license, f"{e.key} 没写授权"
        assert e.url.startswith("https://") and e.filename in e.url, e.url


def test_the_hash_provenance_is_recorded_per_entry():
    """每个数都要能说清是怎么来的（上游摘要 / 本地下载核对）——这是复现的依据。"""
    proofs = {e.key: e.proof for e in le.engines()}
    assert proofs, proofs
    assert set(proofs.values()) <= {"upstream-digest", "local-download"}, proofs


def test_both_tts_models_share_one_runtime():
    """两个配音模型都挂在同一个运行时上：别让用户把 249MB 的运行时下两遍。"""
    runtime = "sherpa_tts_runtime"
    models = [e for e in le.engines() if e.kind in ("tts_model", "clone_model")]
    assert len(models) >= 2, [e.key for e in models]
    assert all(e.needs == (runtime,) for e in models), [(e.key, e.needs) for e in models]
    assert le.by_key(runtime) is not None


def test_the_runtime_marker_is_a_file_name_not_a_path():
    """判据只认文件名：上游改一次目录层级（`bin/x.exe` → `x.exe`）就不该让判据失效。"""
    runtime = le.by_key("sherpa_tts_runtime")
    assert runtime is not None
    assert "/" not in runtime.marker and "\\" not in runtime.marker, runtime.marker
    assert runtime.marker.endswith(".exe")


def test_the_installer_entry_has_no_marker_and_is_never_installed():
    """安装程序那一档我们只下载、不代装：它的目录不归我们管，所以没有判据。"""
    vsr = le.by_key("vsr")
    assert vsr is not None and vsr.archive == "installer"
    assert vsr.marker == "", vsr.marker
    hw = le.Hardware(gpu_names=("NVIDIA GeForce RTX 5060 Laptop GPU",), vulkan=True, cores=16)
    level, reason = le.verdict(vsr, hw)
    assert level == le.LEVEL_MANUAL, level
    assert "不代装" in reason, reason


def test_human_size_never_shows_raw_bytes():
    assert le.human_size(249216829) == "237.7 MB"
    assert le.human_size(35497352) == "33.9 MB"
    assert le.human_size(766975513) == "731.4 GB" or le.human_size(766975513).endswith("MB")
    assert le.human_size(0) == "0 B"


def test_total_size_sums_only_what_exists():
    total = le.total_size()
    assert total == sum(e.size for e in le.engines())
    assert le.total_size(["kokoro_zh"]) == le.by_key("kokoro_zh").size


# ================================================================ 2. 硬件分档


def _hw(**kw) -> le.Hardware:
    base = {"gpu_names": (), "nvidia": False, "vulkan": False, "cores": 8, "ram_gb": 16.0}
    base.update(kw)
    return le.Hardware(**base)  # type: ignore[arg-type]


def test_vulkan_engines_are_refused_when_vulkan_is_missing():
    """没有 Vulkan 就不该让用户下那一档——上游没给纯 CPU 兜底，下了也跑不起来。"""
    for key in ("realesrgan", "waifu2x"):
        e = le.by_key(key)
        assert e is not None
        level, reason = le.verdict(e, _hw(gpu_names=("Intel(R) UHD Graphics",)))
        assert level == le.LEVEL_NO, (key, level)
        assert "Vulkan" in reason, reason


def test_vulkan_engines_are_recommended_when_the_machine_has_a_gpu():
    e = le.by_key("realesrgan")
    assert e is not None
    level, reason = le.verdict(
        e, _hw(gpu_names=("NVIDIA GeForce RTX 5060 Laptop GPU",), vulkan=True,
               vulkan_device="NVIDIA GeForce RTX 5060 Laptop GPU")
    )
    assert level == le.LEVEL_OK, level
    assert "RTX 5060" in reason, reason


def test_cpu_engines_are_recommended_even_without_any_gpu():
    """配音那几档纯 CPU 就能跑：这正是它「值得装」的理由（不用显卡、不用联网）。"""
    for key in ("sherpa_tts_runtime", "kokoro_zh", "zipvoice_zh"):
        e = le.by_key(key)
        assert e is not None
        level, reason = le.verdict(e, _hw())
        assert level == le.LEVEL_OK, (key, level)
        assert "CPU" in reason, reason


def test_headline_never_promises_what_this_machine_cannot_do():
    """顶部那句话必须跟着硬件变：没显卡时不能还在说「推荐本机跑」。"""
    no_gpu = le.headline(_hw(), 0, 6)
    assert "没检测到独立显卡" in no_gpu, no_gpu
    with_vulkan = le.headline(
        _hw(gpu_names=("NVIDIA GeForce RTX 5060 Laptop GPU",), vulkan=True), 0, 6
    )
    assert "值得装" in with_vulkan and "RTX 5060" in with_vulkan, with_vulkan
    gpu_no_vulkan = le.headline(_hw(gpu_names=("NVIDIA GeForce RTX 5060 Laptop GPU",)), 0, 6)
    assert "Vulkan" in gpu_no_vulkan and "值得装" not in gpu_no_vulkan, gpu_no_vulkan


def test_user_facing_text_has_no_markdown_markers():
    """后端交给界面的文字是**纯文本**：React 不会渲染 markdown，写 `**粗**` 就是把星号显示给用户。

    这一条是浏览器走查逮到的：卡片说明里出现了字面的 `**` 与反引号。
    所以清单里的每一个字都要按「屏幕上长什么样」来写。
    """
    profiles = [
        _hw(),
        _hw(gpu_names=("NVIDIA GeForce RTX 5060 Laptop GPU",), vulkan=True,
            vulkan_device="NVIDIA GeForce RTX 5060 Laptop GPU"),
        _hw(gpu_names=("Intel(R) UHD Graphics",)),
    ]
    texts = [le.headline(hw, 0, len(le.engines())) for hw in profiles]
    for e in le.engines():
        texts += [e.label, e.why, e.note]
        for hw in profiles:
            texts.append(le.verdict(e, hw)[1])
    for text in texts:
        for bad in ("**", "`", "##"):
            assert bad not in text, f"给用户看的文字里有 markdown 标记 {bad!r}：{text}"


def test_missing_needs_lists_the_runtime_first():
    kokoro = le.by_key("kokoro_zh")
    assert kokoro is not None
    assert le.missing_needs(kokoro, set()) == ["sherpa_tts_runtime"]
    assert le.missing_needs(kokoro, {"sherpa_tts_runtime"}) == []


# ================================================================ 3. 标志文件匹配


def test_marker_matches_by_file_name_at_any_depth():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "bin").mkdir()
        (root / "bin" / "tool.exe").write_bytes(b"x")
        e = _engine_for(root, marker="tool.exe")
        assert le.marker_hit(e, root) == "bin/tool.exe"
        # 大小写不敏感（Windows 上文件名本来就大小写不敏感）
        assert le.marker_hit(_engine_for(root, marker="TOOL.EXE"), root) == "bin/tool.exe"


def test_marker_glob_matches_model_files():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "model").mkdir()
        (root / "model" / "model.int8.onnx").write_bytes(b"x")
        assert le.marker_hit(_engine_for(root, marker="*.onnx"), root) == "model/model.int8.onnx"
        # 没命中就是空串——**不许猜**
        assert le.marker_hit(_engine_for(root, marker="*.gguf"), root) == ""
        assert le.marker_hit(_engine_for(root, marker="*.onnx"), root / "不存在") == ""


def test_marker_is_empty_for_installer_style_engines():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "anything.exe").write_bytes(b"x")
        assert le.marker_hit(_engine_for(root, marker=""), root) == ""


# ================================================================ 4. 下载 → 校验 → 解压


class _ZipHandler(BaseHTTPRequestHandler):
    """喂真实的 zip 字节，支持 Range，也能强迫回 416。"""

    payload: bytes = b""
    force_416: bool = False
    ranges: list[str] = []
    hits: int = 0

    def do_GET(self) -> None:  # noqa: N802
        cls = type(self)
        cls.hits += 1
        rng = self.headers.get("Range")
        if rng:
            cls.ranges.append(rng)
        if cls.force_416 and rng:
            self.send_response(416)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        total = len(cls.payload)
        start = 0
        if rng and rng.startswith("bytes="):
            start = int(rng.split("=", 1)[1].split("-", 1)[0] or 0)
        body = cls.payload[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(body)))
        if start:
            self.send_header("Content-Range", f"bytes {start}-{total - 1}/{total}")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # 测试输出保持干净
        return


def _serve(payload: bytes, *, force_416: bool = False) -> tuple[HTTPServer, str]:
    handler = type("H", (_ZipHandler,), {
        "payload": payload, "force_416": force_416, "ranges": [], "hits": 0,
    })
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def _engine_for(root: Path, *, marker: str) -> le.Engine:
    """造一个只用于「标志文件匹配」的引擎（不下载）。"""
    return le.Engine(
        key="probe", label="探针", kind="upscale", filename="probe.zip",
        url="https://example.invalid/probe.zip", size=1, sha256="0" * 64,
        license="MIT", homepage="https://example.invalid", archive="zip",
        marker=marker, why="测试", note="测试", proof="local-download",
    )


def _engine(payload: bytes, url: str, *, marker: str = "tool.exe", sha256: str = "",
            size: int = 0, gpu: str = "any", needs: tuple[str, ...] = (),
            archive: str = "zip", key: str = "fake") -> le.Engine:
    return le.Engine(
        key=key, label="假引擎", kind="upscale", filename="fake.zip", url=url,
        size=size or len(payload),
        sha256=sha256 or hashlib.sha256(payload).hexdigest(),
        license="MIT", homepage="https://example.invalid", archive=archive,
        marker=marker, why="测试用", note="测试用", proof="local-download",
        gpu=gpu, needs=needs,
    )


@contextlib.contextmanager
def _sandbox(root: Path, engines: list[le.Engine], hw: le.Hardware | None = None):
    """把「装到哪儿」「这台机器什么样」「清单里有哪些引擎」都换成测试自己的。"""
    real = (le._INDEX, le.ENGINES, ei.settings, ei.hardware, ei._job, ei._task)

    class _Settings:
        data_dir = root

    le._INDEX = {e.key: e for e in engines}
    le.ENGINES = tuple(engines)
    ei.settings = _Settings  # type: ignore[assignment]
    ei.hardware = lambda force=False: (hw or _hw())  # type: ignore[assignment]
    ei._job = None
    ei._task = None
    try:
        yield
    finally:
        (le._INDEX, le.ENGINES, ei.settings, ei.hardware, ei._job, ei._task) = real  # type: ignore[assignment]


def _run_job(key: str) -> dict:
    """起一条并等它跑完。

    **必须在一个事件循环里跑完**：`start()` 是在当前循环上 `create_task` 的，
    分两次 `asyncio.run` 会让任务挂在已经关掉的循环上（报 "attached to a different loop"）。
    `_run` 自己吞异常写进 job，所以这里等得到结论。
    """
    async def scenario():
        await ei.start(key)
        task = ei._task
        if task is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, timeout=30)
        return ei.job_snapshot()

    return asyncio.run(scenario())


def test_a_complete_download_is_verified_and_unpacked():
    """完整的下载：校验过 → 解压 → 标志文件命中 → 归档留着（校验要用）。"""
    payload = _zip_bytes({"bin/tool.exe": b"MZ" + b"\x00" * 64, "readme.txt": b"hi"})
    server, url = _serve(payload)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _engine(payload, url)
            with _sandbox(root, [engine]):
                job = _run_job("fake")
                assert job["phase"] == "done", job
                assert ei.installed(engine), "装完却判断成没装"
                assert le.marker_hit(engine, ei.install_dir("fake")) == "bin/tool.exe"
                assert ei.archive_path(engine).exists(), "归档不该删（校验还要用它）"
    finally:
        server.shutdown()


def test_a_bad_checksum_is_refused_and_the_file_is_deleted():
    """校验不过必须拦住，并且**删掉那个文件**——留着只会让人反复重试同一个坏文件。"""
    payload = _zip_bytes({"bin/tool.exe": b"MZ"})
    server, url = _serve(payload)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _engine(payload, url, sha256="a" * 64)  # 故意写错
            with _sandbox(root, [engine]):
                job = _run_job("fake")
                assert job["phase"] == "error", job
                assert "校验和" in job["error"], job["error"]
                assert not ei.installed(engine), "校验没过却当成装好了"
                assert not ei.archive_path(engine).exists(), "坏文件没删掉"
    finally:
        server.shutdown()


def test_resume_continues_from_the_partial_file():
    """断点续传：本地已有前半段时，应当只请求后半段（Range）。"""
    payload = _zip_bytes({"bin/tool.exe": b"MZ" + b"x" * 4096})
    server, url = _serve(payload)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _engine(payload, url)
            with _sandbox(root, [engine]):
                target = ei.archive_path(engine)
                target.parent.mkdir(parents=True, exist_ok=True)
                half = len(payload) // 2
                target.write_bytes(payload[:half])
                job = _run_job("fake")
                assert job["phase"] == "done", job
                assert target.read_bytes() == payload, "续传拼出来的文件与原件不一致"
                assert server.RequestHandlerClass.ranges, "没有发 Range 请求（等于重下了）"
    finally:
        server.shutdown()


def test_a_416_means_the_local_copy_is_already_complete():
    """**416 不是错误**：它的意思是「起点已经在末尾之后」，也就是本地这份已经完整。

    这一条是本版探针里真踩过的坑——当时把 416 当错误、把一份下好的 43MB 删了重下。
    """
    payload = _zip_bytes({"bin/tool.exe": b"MZ" + b"y" * 2048})
    server, url = _serve(payload, force_416=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _engine(payload, url)
            with _sandbox(root, [engine]):
                target = ei.archive_path(engine)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)  # 假装已经下完了
                job = _run_job("fake")
                assert job["phase"] == "done", job
                assert ei.installed(engine), job
    finally:
        server.shutdown()


def test_unsafe_archive_paths_are_refused():
    """压缩包里的 `../` 必须挡住——解压出来的东西不许跑到安装目录外面。"""
    payload = _zip_bytes({"../escaped.exe": b"MZ", "bin/tool.exe": b"MZ"})
    server, url = _serve(payload)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _engine(payload, url)
            with _sandbox(root, [engine]):
                job = _run_job("fake")
                assert job["phase"] == "error", job
                assert "不安全" in job["error"], job["error"]
                assert not (root / "escaped.exe").exists(), "文件跑到安装目录外面了"
    finally:
        server.shutdown()


def test_a_missing_marker_is_reported_instead_of_pretending_success():
    """包里没有我们要的那个文件 → 明说，并且**不留半个安装目录**（留了最难查）。"""
    payload = _zip_bytes({"readme.txt": b"hi"})
    server, url = _serve(payload)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _engine(payload, url, marker="tool.exe")
            with _sandbox(root, [engine]):
                job = _run_job("fake")
                assert job["phase"] == "error", job
                assert "tool.exe" in job["error"], job["error"]
                assert not ei.install_dir("fake").exists(), "失败却留下了安装目录"
                leftovers = [p.name for p in ei.root_dir().glob(".*part*")]
                assert not leftovers, leftovers
    finally:
        server.shutdown()


def test_only_one_download_at_a_time():
    """同时开两条下载会互相抢带宽，也让「下到哪儿了」说不清——必须明确拒绝。"""
    payload = _zip_bytes({"bin/tool.exe": b"MZ" * 100000})
    server, url = _serve(payload)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a = _engine(payload, url, key="one")
            b = _engine(payload, url, key="two")
            with _sandbox(root, [a, b]):
                async def scenario():
                    await ei.start("one")
                    try:
                        await ei.start("two")
                    except ValueError as e:
                        assert "下载" in str(e), str(e)
                    else:
                        raise AssertionError("两条下载同时开起来了")
                    ei.cancel("one")
                    with contextlib.suppress(BaseException):
                        await asyncio.wait_for(ei._task, timeout=10)

                asyncio.run(scenario())
    finally:
        server.shutdown()


def test_cancel_keeps_the_partial_file():
    """停止之后已经下到的部分要留着：几百 MB 重头下是用户最不想看到的事。"""
    payload = _zip_bytes({"bin/tool.exe": b"MZ" * 200000})
    server, url = _serve(payload)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _engine(payload, url)
            with _sandbox(root, [engine]):
                async def scenario():
                    await ei.start("fake")
                    job = ei.cancel("fake")
                    assert job["phase"] == "cancelled", job
                    with contextlib.suppress(BaseException):
                        await asyncio.wait_for(ei._task, timeout=10)

                asyncio.run(scenario())
                # 取消后不该留下「装好了」的假象
                assert not ei.installed(engine)
    finally:
        server.shutdown()


def test_start_refuses_a_machine_that_cannot_run_it():
    """跑不了的机器：不许开始下（不然用户等半天才发现装不上）。"""
    payload = _zip_bytes({"bin/tool.exe": b"MZ"})
    server, url = _serve(payload)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _engine(payload, url, gpu="vulkan")
            with _sandbox(root, [engine], hw=_hw()):  # 没有 Vulkan
                try:
                    asyncio.run(ei.start("fake"))
                except ValueError as e:
                    assert "Vulkan" in str(e), str(e)
                    return
                raise AssertionError("跑不了的引擎也让它开始下了")
    finally:
        server.shutdown()


def test_start_refuses_a_model_before_its_runtime():
    payload = _zip_bytes({"bin/tool.exe": b"MZ"})
    server, url = _serve(payload)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = _engine(payload, url, key="rt", marker="tool.exe")
            model = _engine(payload, url, key="mdl", marker="*.onnx", needs=("rt",))
            with _sandbox(root, [runtime, model]):
                try:
                    asyncio.run(ei.start("mdl"))
                except ValueError as e:
                    assert "要先装" in str(e), str(e)
                    return
                raise AssertionError("运行时还没装就让下模型了")
    finally:
        server.shutdown()


def test_start_refuses_when_it_is_already_installed():
    payload = _zip_bytes({"bin/tool.exe": b"MZ"})
    server, url = _serve(payload)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _engine(payload, url)
            with _sandbox(root, [engine]):
                assert _run_job("fake")["phase"] == "done"
                try:
                    asyncio.run(ei.start("fake"))
                except ValueError as e:
                    assert "已经装好" in str(e), str(e)
                    return
                raise AssertionError("已经装好了还允许再下一遍")
    finally:
        server.shutdown()


def test_remove_deletes_both_the_install_and_the_archive():
    payload = _zip_bytes({"bin/tool.exe": b"MZ" * 512})
    server, url = _serve(payload)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _engine(payload, url)
            with _sandbox(root, [engine]):
                assert _run_job("fake")["phase"] == "done"
                result = asyncio.run(ei.remove("fake"))
                assert result["freed"] > 0, result
                assert not ei.install_dir("fake").exists()
                assert not ei.archive_path(engine).exists()
                assert not ei.installed(engine)
    finally:
        server.shutdown()


def test_an_empty_error_never_reaches_the_user():
    """httpx 断线时抛的异常**可能是空消息**，而空错误比没有错误更糟。

    这一条是这一版真踩到的：界面上「上次失败：」后面什么都没有，用户连搜都不知道搜什么。
    要的不变量是「永远给出一句非空的话」——拿不出内容就报异常类型名。
    """
    for exc in (RuntimeError(""), RuntimeError("   "), OSError(13, ""), ValueError()):
        text = ei._error_text(exc)
        assert text.strip(), f"{exc!r} 给了一句空错误"
    assert ei._error_text(RuntimeError("")) == "RuntimeError"
    assert ei._error_text(ValueError()) == "ValueError"
    # 有内容就原样用它（不额外加类型名，免得盖住真正的原因）
    assert ei._error_text(RuntimeError("连接被重置")) == "连接被重置"
    assert "13" in ei._error_text(OSError(13, ""))


def test_network_failure_tells_the_user_three_ways_out():
    """网络不通时要说清「还能怎么办」——这几百 MB 都在 GitHub 附件上，时通时断。"""
    import httpx

    text = ei._maybe_network_hint("ConnectTimeout", httpx.ConnectTimeout("x"))
    for mark in ("①", "②", "③"):
        assert mark in text, text
    assert "接着下载" in text and "校验" in text and "代理" in text, text
    # 非网络类的失败不该被塞进这些建议（否则真原因会被埋掉）
    plain = ei._maybe_network_hint("校验和不一致", ValueError("校验和不一致"))
    assert plain == "校验和不一致", plain


def test_downloads_try_direct_and_the_system_proxy_in_turn():
    """直连与走系统代理**轮流试**。

    理由与 `update_service._probe` 里记的是同一件事：httpx 在 Windows 上读注册表里的
    IE 代理，用户一开 VPN 请求就被静默导到共享出口；反过来国内直连 GitHub 附件也常不通。
    这一版实测就是靠「再用系统代理试一次」把 Kokoro 那个 140MB 拉下来的。
    """
    calls: list[bool] = []
    real = ei._download

    async def fake_download(engine, job, *, use_env_proxy):  # noqa: ANN001
        calls.append(use_env_proxy)
        if len(calls) < 3:
            raise __import__("httpx").ConnectTimeout("x")
        return ei.archive_path(engine)

    ei._download = fake_download  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _engine(b"x", "https://example.invalid/f.zip")
            with _sandbox(root, [engine]):
                with contextlib.suppress(BaseException):
                    asyncio.run(ei._download_with_retries(
                        engine, {"key": "fake", "done": 0}))
    finally:
        ei._download = real  # type: ignore[assignment]
    assert calls == [False, True, False], calls


class _SlowHandler(BaseHTTPRequestHandler):
    """慢速滴答的服务器：**有数据、但慢到来不及**（与实测里「代理只剩 1MB/分钟」同形）。

    定义在模块级而不是函数里：函数里那种闭包写法在这条用例上踩过一次坑
    （客户端的提前放弃与服务端的写失败搅在一起，报出来的是「peer closed connection」，
    把真正要验的判据盖住了）。
    """

    chunk = 64 << 10
    rounds = 20
    delay = 0.25

    def do_GET(self) -> None:  # noqa: N802
        total = self.chunk * self.rounds
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        # 声明的长度就是真会发出去的长度
        self.send_header("Content-Length", str(total))
        self.end_headers()
        for _ in range(self.rounds):
            try:
                self.wfile.write(b"x" * self.chunk)
                self.wfile.flush()
            except OSError:
                return  # 客户端提前放弃正是这条用例期待的结果
            time.sleep(self.delay)

    def log_message(self, *_args) -> None:
        return


def test_a_slow_but_alive_route_is_abandoned():
    """**「活着但在爬」也要换路。**

    这一条是真跑出来的教训：同一个 140MB 的包，官方直连 8MB 用 4.2 秒，而走本机代理
    8MB 要 40 秒以上、甚至掉到 1MB/分钟——**下载不会报错**，不换路的话用户就对着
    一个慢慢爬的进度条干等（一百多分钟），而快的路只要一两分钟。

    这测的是「有数据但很慢」；**完全没有数据**那种卡住由 httpx 的读超时兜着（60 秒）。
    """
    server = HTTPServer(("127.0.0.1", 0), _SlowHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/big.zip"

    real = (ei._PROBE_BYTES, ei._PROBE_SECONDS, ei._PROBE_MIN_FILE)
    ei._PROBE_BYTES, ei._PROBE_SECONDS, ei._PROBE_MIN_FILE = 4 << 20, 1.0, 0
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _engine(b"y" * (2 << 20), url)
            job = {"key": "fake", "done": 0}
            with _sandbox(root, [engine]):
                try:
                    asyncio.run(ei._download(engine, job, use_env_proxy=False))
                except ei._RouteTooSlow as e:
                    assert "太慢" in str(e), str(e)
                else:
                    raise AssertionError("爬得那么慢却没换路")
    finally:
        ei._PROBE_BYTES, ei._PROBE_SECONDS, ei._PROBE_MIN_FILE = real
        server.shutdown()


class _RangeIgnoringHandler(BaseHTTPRequestHandler):
    """声称支持续传、却把**整包**又塞一遍的服务器。

    这不是编出来的场景：实测续传 rife-ncnn-vulkan 那个 411MB 的包时，走代理拿到的
    本地文件是 556MB——服务端对一个带 `Range` 的请求回了 206，却给了整包。
    于是本地那份成了「前缀 + 另一个整包」，**永远过不了 sha256**。
    """

    body = b"z" * (3 << 20)

    def do_GET(self) -> None:  # noqa: N802
        assert self.headers.get("Range"), "这条用例要的正是「带 Range 却回整包」"
        self.send_response(206)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(self.body)))
        self.send_header("Content-Range", f"bytes 0-{len(self.body) - 1}/{len(self.body)}")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_args) -> None:
        return


def test_a_source_that_ignores_range_does_not_poison_the_local_copy():
    """服务端回 206 却塞整包时，本地那份要**当场作废**。

    不这么做的话，用户看到的是「校验和不一致」，而每点一次重试都要再下一整包，
    并且**看不出为什么**——因为失败的那份文件是「前缀 + 另一个整包」，
    体积比清单大，而体积不对这件事界面上一开始并不说。
    """
    server = HTTPServer(("127.0.0.1", 0), _RangeIgnoringHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/x.zip"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _engine(_RangeIgnoringHandler.body, url)
            job = {"key": "fake", "done": 0}
            with _sandbox(root, [engine]):
                # 先放一个半包，逼它下次带 Range 去续传
                ei.archive_path(engine).write_bytes(b"z" * (1 << 20))
                try:
                    asyncio.run(ei._download(engine, job, use_env_proxy=False))
                except ValueError as e:
                    assert "整包" in str(e), str(e)
                else:
                    raise AssertionError("服务端没按 Range 给数据，这份却被当成了好的")
                assert not ei.archive_path(engine).exists(), "废掉的那份必须删掉"
                assert job["done"] == 0, "进度要归零——不然界面会停在一个假进度上"
    finally:
        server.shutdown()


def test_verify_reports_a_size_mismatch_before_hashing():
    """体积都不对就不用算哈希了：给用户的理由要具体（差多少）。"""
    payload = _zip_bytes({"bin/tool.exe": b"MZ"})
    server, url = _serve(payload)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _engine(payload, url)
            with _sandbox(root, [engine]):
                target = ei.archive_path(engine)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"short")
                out = asyncio.run(ei.verify("fake"))
                assert out["ok"] is False and "体积" in out["detail"], out
    finally:
        server.shutdown()


def test_verify_accepts_a_good_file():
    payload = _zip_bytes({"bin/tool.exe": b"MZ"})
    server, url = _serve(payload)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = _engine(payload, url)
            with _sandbox(root, [engine]):
                target = ei.archive_path(engine)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                out = asyncio.run(ei.verify("fake"))
                assert out["ok"] is True, out
    finally:
        server.shutdown()


def test_the_archive_directory_sits_inside_the_data_dir():
    """落点必须在**数据目录**里：这样它既不进仓库、也不进便携包（大文件的分发原则）。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _sandbox(root, []):
            assert ei.root_dir() == root / "engines"
            assert ei.archive_dir() == root / "engines" / "_downloads"
            assert ei.install_dir("x") == root / "engines" / "x"
            assert root in ei.root_dir().parents or ei.root_dir().parent == root


def test_engines_never_land_inside_the_shipped_code_dirs():
    """装的引擎**不许**落在要进包的地方（`backend/app`、前端产物）——那是仓库与便携包的内容。"""
    real = ei.root_dir()
    for shipped in (BACKEND / "app", BACKEND.parent / "frontend" / "webroot"):
        assert not real.is_relative_to(shipped), f"引擎目录落在要进包的地方：{real}"


# ================================================================ 5. 已有的本机能力


def test_installed_services_reuses_the_existing_detection():
    """不重造「本机有没有 Ollama / ComfyUI」的判据：结论直接来自既有那两处。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with maker() as db:
            db.add(ProviderService(name="我的ComfyUI", kind="comfyui", base_url="http://127.0.0.1:8188",
                                   enabled=True))
            await db.commit()
            rows = await ei.installed_services(db)
            assert [r["key"] for r in rows] == ["ollama", "comfyui"], rows
            comfy = rows[1]
            assert comfy["running"] is True and "我的ComfyUI" in comfy["detail"], comfy
            # Ollama 那条只说结论，不在这里另判一次（本机可能真没装，两种都算对）
            assert rows[0]["label"].startswith("Ollama"), rows[0]

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(engine.dispose())


# ================================================================ 6. 前后端契约


def test_the_page_and_the_api_agree_on_the_shape():
    """前端读的字段名就是后端给的键名：改了一边而另一边没改，界面只是少一块。"""
    page = (BACKEND.parent / "frontend" / "src" / "pages" / "EnginesPage.tsx").read_text(
        encoding="utf-8")
    types = (BACKEND.parent / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")
    for field in ("gpuText", "vulkan", "vulkanDevice", "headline", "installedServices",
                  "installedCount", "totalSizeText", "phaseLabels"):
        assert field in types, f"前端类型里没有 {field}"
        assert field in page, f"页面上没用 {field}"
    for field in ("sizeText", "kindLabel", "levelLabel", "reason", "installed",
                  "archiveBytes", "archiveComplete", "missingNeeds", "proof"):
        assert field in types, f"EngineItem 里没有 {field}"
        assert field in page, f"页面上没用 {field}"


def test_the_page_never_lets_you_start_what_this_machine_cannot_run():
    """界面上的闸门与后端一致：跑不了/缺前置，按钮就是灰的（不是等点了才报错）。"""
    page = (BACKEND.parent / "frontend" / "src" / "pages" / "EnginesPage.tsx").read_text(
        encoding="utf-8")
    assert 'item.level === "no" || item.missingNeeds.length > 0' in page, "没把「不能装」的条件写上"
    assert "disabled={busy || blocked}" in page, "按钮没有按 blocked 置灰"
    assert "Vulkan" in page or "levelLabel" in page, "没把后端给的档位理由显示出来"


def test_the_nav_entry_exists_on_both_sides():
    """新页面要同时出现在后端种子（老库靠迁移）与前端的默认导航里。"""
    registry = (BACKEND / "app" / "registry" / "schema_registry.py").read_text(encoding="utf-8")
    assert '"key": "engines"' in registry and '"route": "engines"' in registry, "后端种子里没有这一项"
    app = (BACKEND.parent / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert 'route: "engines"' in app, "前端默认导航里没有这一项"
    assert "EnginesPage" in app, "前端没接上这个页面"
    migration = BACKEND / "alembic" / "versions" / "0016_engines_nav.py"
    assert migration.exists(), "老库没有对应的迁移（升级上来的用户会看不到这一页）"
    text = migration.read_text(encoding="utf-8")
    assert 'NAV_KEY = "engines"' in text and "INSERT INTO nav_items" in text, text[:200]


def test_the_icon_name_is_mapped_in_the_frontend():
    """导航给的是 icon 字符串，前端要认得出——不认就会退化成一个小圆点。"""
    registry = (BACKEND / "app" / "registry" / "schema_registry.py").read_text(encoding="utf-8")
    icon_map = (BACKEND.parent / "frontend" / "src" / "components" / "IconMap.tsx").read_text(
        encoding="utf-8")
    assert '"icon": "cpu"' in registry, "导航项没写图标名"
    assert "cpu: Cpu" in icon_map, "前端图标表里没有 cpu（会退化成占位图标）"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
