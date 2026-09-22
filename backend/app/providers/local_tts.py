"""本机配音适配器：把装好的 sherpa-onnx 当成一条**普通的配音模型服务**。

为什么不给配音页开一个「本机跑」的特例：配音页、静图样片旁白、画布逐镜对白
全都是从模型下拉里取音频模型的（`/api/providers/models?modality=audio`）。
把它接成一条普通服务之后，**那几条路一行都不用改**就能选「本机跑」——
这正是批次 9 那句「本机引擎 = 把已有模式收成一条统一的路」。

四条口径：
1. **不假装它是云端**：没有 Base URL、不要 API Key，报错文案里也不提 Key。
2. **命令行按目录里真实存在的东西拼**（见 `services/local_tts.py`），不写死文件名。
3. **失败要能自己排查**：退出码 + stderr 尾部原样带出来。
4. **认不出音色就报错，不悄悄用默认音色**（那样用户听到的是别人的嗓子）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
from pathlib import Path
from typing import AsyncIterator

from app.providers.base import AdapterError, BaseAdapter, SpeechResult, unsupported
from app.services import local_tts

logger = logging.getLogger("xiaoma.local_tts")

# 合成一段话的超时。Kokoro 是 82M 的小模型，实测一句话是秒级的；
# 给 300 秒是留足「很长的旁白 + 老机器」的余量，而**不是**让它无限等下去。
SYNTH_TIMEOUT = 300.0
# 「测试连接」用的一段短文本与更紧的超时：这条路上用户就在等结果。
PROBE_TEXT = "你好"
PROBE_TIMEOUT = 120.0


class LocalTTSAdapter(BaseAdapter):
    """跑本机的 sherpa-onnx-offline-tts，产物是 wav 字节。"""

    kind = "local_tts"

    def __init__(self, base_url: str = "", api_key: str = "", *, engine_key: str = "") -> None:
        # base_url / api_key 在这条路上没有意义：调用对象是本机的一个 exe。
        # 仍按基类的签名收着，是为了让「装配适配器」那一处保持同一套写法。
        super().__init__(base_url, api_key)
        self.engine_key = engine_key or local_tts.MODEL_KEY

    # ---- 唯一的真能力 ----

    async def synthesize_speech(
        self,
        *,
        model: str,
        text: str,
        voice: str = "",
        speed: float = 1.0,
    ) -> SpeechResult:
        ready, why = local_tts.check_ready(self.engine_key)
        if not ready:
            raise AdapterError(why, log_detail="local_tts not_ready")
        layout = local_tts.resolve_layout(self.engine_key)
        if layout is None:
            raise AdapterError(
                "本机配音引擎的文件不完整——到「本机引擎」页删掉重新下一次",
                log_detail="local_tts broken_layout",
            )
        try:
            sid = local_tts.sid_for(voice, self.engine_key)
        except ValueError as e:
            raise AdapterError(str(e), log_detail="local_tts unknown_voice") from e

        data = await _run_tts(
            layout, text=text, sid=sid, speed=speed, timeout=SYNTH_TIMEOUT
        )
        logger.info(
            "本机配音：%s 字（音色 sid=%s，语速 %.2f，模型 %s）",
            len(text), sid, speed, layout.model.name,
        )
        return SpeechResult(audio=data, content_type="audio/wav")

    async def test_connection(self, model: str | None = None) -> None:
        """真合成一句短文本——**这才叫「测试连接」**。

        只检查「文件在不在」的话，一个坏掉的 exe、缺 DLL、或者模型与运行时版本不匹配
        都测不出来，而要等到用户正式配音时才报错。
        """
        ready, why = local_tts.check_ready(self.engine_key)
        if not ready:
            raise AdapterError(why, log_detail="local_tts not_ready")
        layout = local_tts.resolve_layout(self.engine_key)
        if layout is None:
            raise AdapterError("本机配音引擎的文件不完整", log_detail="local_tts broken_layout")
        data = await _run_tts(
            layout, text=PROBE_TEXT, sid=0, speed=1.0, timeout=PROBE_TIMEOUT
        )
        if len(data) < 1000:
            raise AdapterError(
                f"本机配音跑通了，但产物只有 {len(data)} 字节——"
                "这不是一段正常的语音，请到「本机引擎」页重新下载模型",
                log_detail=f"local_tts tiny_output bytes={len(data)}",
            )

    # ---- 其余能力：明确说「不支持」，不静默失败 ----

    def chat_stream(
        self, model: str, messages: list[dict], temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        raise unsupported("文本对话")

    async def generate_image(
        self, *, model: str, prompt: str, n: int = 1, size: str = "1024x1024",
        ref_images: list[bytes] | None = None,
    ) -> list[bytes]:
        raise unsupported("图片生成")

    async def submit_video(
        self, *, model: str, prompt: str, first_frame: bytes | None = None,
        duration: int = 5, ratio: str = "16:9", resolution: str = "720p",
        last_frame: bytes | None = None, ref_images: list[bytes] | None = None,
        ref_videos: list[bytes] | None = None, ref_audio=None,  # noqa: ANN001
    ) -> str:
        raise unsupported("视频生成")

    async def poll_video(self, remote_id: str):
        raise unsupported("视频生成")


async def _run_tts(
    layout, *, text: str, sid: int, speed: float, timeout: float  # noqa: ANN001
) -> bytes:
    """跑一次合成，返回 wav 字节。**产物落在临时目录里、用完即删。**"""
    with tempfile.TemporaryDirectory(prefix="localtts-") as tmp:
        out = Path(tmp) / "out.wav"
        args = local_tts.plan_args(
            layout, text=text, out_path=out, sid=sid, speed=speed,
            threads=local_tts.default_threads(),
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            raise AdapterError(
                f"起不了本机配音程序（{layout.exe}）：{e}。"
                "到「本机引擎」页确认它还在、或删掉重新下一次",
                log_detail=f"local_tts spawn_failed errno={getattr(e, 'errno', '-')}",
            ) from e

        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError as e:
            await _kill_and_reap(proc)
            raise AdapterError(
                f"本机配音超过 {int(timeout)} 秒还没出结果，已经停掉。"
                "文本太长可以拆成两段；这台机器上跑得慢的话，建议还是用云端模型",
                log_detail=f"local_tts timeout={int(timeout)}",
            ) from e

        tail = (stderr or b"").decode("utf-8", "replace").strip()
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            detail = tail[-400:] or "（没有任何输出）"
            raise AdapterError(
                f"本机配音失败（退出码 {proc.returncode}）：{detail}。"
                "常见原因：模型文件不完整（到「本机引擎」页删掉重新下一次）"
                "或引擎与模型版本不匹配",
                log_detail=f"local_tts exit={proc.returncode} stderr={len(tail)}B",
            )
        data = out.read_bytes()
        if not data:
            raise AdapterError("本机配音没产出音频", log_detail="local_tts empty_output")
        # 慢的机器上它会往 stderr 打一堆日志（我们已经 --debug=0），留一行便于排查
        if tail:
            logger.debug("本机配音 stderr：%s", tail[-200:])
        return data


async def _kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    """杀掉并**收尸**。

    只 `kill()` 不 `wait()` 的话，子进程会变成僵尸、管道也不会关
    （实测表现是 asyncio 在退出时打一串 `unclosed transport` 的告警，
    以及一个一直挂着的进程句柄）——超时这条路本来就不常走，更要收拾干净。
    """
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except (asyncio.TimeoutError, ProcessLookupError, OSError):
        pass
    # 管道也关掉：不然 Windows 上那个 transport 要等到进程退出才回收
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            with contextlib.suppress(Exception):
                pipe.close()
