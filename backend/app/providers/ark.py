"""火山引擎方舟（Volcengine Ark）适配器。

- 文本：/api/v3/chat/completions（OpenAI 兼容）
- 图片：/api/v3/images/generations（Seedream，支持参考图改图）
- 视频：/api/v3/contents/generations/tasks（Seedance 异步任务：文生 / 首帧图生 /
  首尾帧 / 全能参考 / **参考音频（对白口播）**）

文档：https://www.volcengine.com/docs/82379
音频参考：https://docs.volcengine.com/docs/82379/1520757
"""

from __future__ import annotations

import base64
from typing import Any, AsyncIterator

import httpx

from app.providers.base import (
    AdapterError,
    AudioRef,
    BaseAdapter,
    SpeechResult,
    VideoStatus,
    network_error_message,
    network_error_detail,
    unsupported,
    url_host,
)
from app.providers.openai_compat import iter_chat_sse, raise_upstream_error

DEFAULT_ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"

# 参考音频的硬限制（上游口径）：单个 ≤15 MB、格式 wav/mp3。
# 这两条本地就能判，先在本地拦住——省下一次必然失败的上游往返，错误也更短。
AUDIO_MAX_BYTES = 15 * 1024 * 1024


def _sniff_image_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/png"


def _data_uri(data: bytes) -> str:
    mime = _sniff_image_mime(data)
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _audio_mime(ref: AudioRef) -> str:
    """参考音频的 MIME：以库里记的 `content_type` 为准，认不出再用魔数兜一下。

    上游只吃 wav / mp3 两种。本地生成的配音（我们的 TTS）也是这两种之一，
    所以这里不扩张名单——真出别的格式，调用方那句报错会告诉用户该怎么办。
    认不出来返回空串，由调用方给出可读错误。
    """
    ct = (ref.content_type or "").split(";")[0].strip().lower()
    if ct in ("audio/x-wav", "audio/wave"):
        return "audio/wav"
    if ct == "audio/mp3":
        return "audio/mpeg"
    if ct in ("audio/wav", "audio/mpeg"):
        return ct
    if ref.data[:4] == b"RIFF" and ref.data[8:12] == b"WAVE":
        return "audio/wav"
    if ref.data[:3] == b"ID3" or ref.data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    return ""


def _audio_data_uri(ref: AudioRef) -> str:
    mime = _audio_mime(ref)
    if not mime:
        raise AdapterError(
            f"这条参考音频的格式认不出来（记录的类型是 {ref.content_type or '空'}）——"
            "方舟只收 wav 与 mp3。请换一条配音，或到「配音」页重新生成一段",
            log_detail=f"invalid_audio_type ct={ref.content_type or '-'}",
        )
    if len(ref.data) > AUDIO_MAX_BYTES:
        raise AdapterError(
            f"这条参考音频有 {len(ref.data) / 1048576:.1f} MB，超过上限 15 MB。"
            "请换一段更短的配音（长台词建议拆成两镜）",
            log_detail=f"audio_too_large bytes={len(ref.data)}",
        )
    return f"data:{mime};base64,{base64.b64encode(ref.data).decode('ascii')}"


# 参考音频（`role=reference_audio`）是 Seedance 较新几档才有的能力：官方模型能力表里
# **1.5 pro / 2.0 / 2.5 为 ✓，1.0 pro 与 1.0 pro fast 为 ✗**。
# 这里只用来把上游那句 400 翻译成人话，**不做闸门**：名单一定会过期（新模型一直在出），
# 拿它拦人就会把「本来能跑的组合」也拦掉，而放过去最坏只是上游回一句 400、不花钱。
AUDIO_REF_OK_PREFIXES = ("doubao-seedance-2", "doubao-seedance-1-5-pro", "seedance-2", "seedance-1-5-pro")
AUDIO_REF_NO_PREFIXES = ("doubao-seedance-1-0", "seedance-1-0")


def audio_ref_support(model: str) -> bool | None:
    """这个模型认不认参考音频。True / False / **None = 不知道**。

    「不知道」是合法答案，也是常态（新模型一直在出）——不知道时照常发出去，
    让上游判，不要替用户猜。
    """
    name = (model or "").strip().lower()
    if not name:
        return None
    if any(name.startswith(p) or p in name for p in AUDIO_REF_OK_PREFIXES):
        return True
    if any(name.startswith(p) or p in name for p in AUDIO_REF_NO_PREFIXES):
        return False
    return None


AUDIO_REF_NO_HINT = (
    "参考音频（对白/口播）只有 Seedance 1.5 pro / 2.0 / 2.5 这几档认，1.0 系列不支持。"
    "到「模型服务」里把这个服务下的视频模型换成其中一档即可；"
    "只想让它动起来、不需要说话，就把「对白音轨」去掉"
)


class ArkAdapter(BaseAdapter):
    kind = "ark"

    async def test_connection(self, model: str | None = None) -> None:
        if not self.base_url:
            raise AdapterError("Base URL 为空", log_detail="config_invalid base_url_empty")
        client = await self.client()
        try:
            resp = await client.get(
                f"{self.base_url}/models",
                headers=self.headers(),
                params={"limit": 1} if model is None else None,
            )
        except httpx.HTTPError as e:
            raise AdapterError(network_error_message(e), log_detail=network_error_detail(e, self.base_url)) from e
        if resp.status_code == 200:
            return
        if resp.status_code in (401, 403):
            raise AdapterError(f"API Key 校验失败：HTTP {resp.status_code}", log_detail=f"HTTP {resp.status_code} auth_failed host={url_host(self.base_url)}")
        raise_upstream_error(resp)

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        if not self.base_url or not self.api_key:
            raise AdapterError("该服务尚未填写 Base URL 或 API Key", log_detail="config_invalid missing_credentials")
        client = await self.client()
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        async for piece in iter_chat_sse(
            client,
            f"{self.base_url}/chat/completions",
            {**self.headers(), "Content-Type": "application/json"},
            body,
        ):
            yield piece

    async def generate_image(
        self,
        *,
        model: str,
        prompt: str,
        n: int = 1,
        size: str = "1024x1024",
        ref_images: list[bytes] | None = None,
    ) -> list[bytes]:
        if not self.base_url or not self.api_key:
            raise AdapterError("该服务尚未填写 Base URL 或 API Key", log_detail="config_invalid missing_credentials")
        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "response_format": "b64_json",
        }
        if n and n > 1:
            body["n"] = n
        if ref_images:
            # Seedream 4.0+：image 数组支持 1~多张参考图（URL 或 base64）
            body["image"] = [_data_uri(img) for img in ref_images]
        client = await self.client()
        try:
            resp = await client.post(
                f"{self.base_url}/images/generations",
                headers={**self.headers(), "Content-Type": "application/json"},
                json=body,
            )
        except httpx.HTTPError as e:
            raise AdapterError(network_error_message(e), log_detail=network_error_detail(e, self.base_url)) from e
        if resp.status_code != 200:
            raise_upstream_error(resp)
        data = resp.json()
        items = data.get("data") or []
        results: list[bytes] = []
        for item in items:
            b64 = item.get("b64_json")
            if b64:
                results.append(base64.b64decode(b64))
            elif item.get("url"):
                dl = await client.get(item["url"])
                if dl.status_code != 200:
                    raise AdapterError(f"下载结果图片失败：HTTP {dl.status_code}", log_detail=f"download HTTP {dl.status_code}")
                results.append(dl.content)
        if not results:
            raise AdapterError(f"上游响应中没有图片：{str(data)[:200]}", log_detail=f"empty_result status=200 body={len(str(data))}B")
        return results

    async def submit_video(
        self,
        *,
        model: str,
        prompt: str,
        first_frame: bytes | None = None,
        duration: int = 5,
        ratio: str = "16:9",
        resolution: str = "720p",
        last_frame: bytes | None = None,
        ref_images: list[bytes] | None = None,
        ref_videos: list[bytes] | None = None,
        ref_audio: AudioRef | None = None,
    ) -> str:
        if not self.base_url or not self.api_key:
            raise AdapterError("该服务尚未填写 Base URL 或 API Key", log_detail="config_invalid missing_credentials")
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if first_frame:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _data_uri(first_frame)},
                    "role": "first_frame",
                }
            )
        if last_frame:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _data_uri(last_frame)},
                    "role": "last_frame",
                }
            )
        # 多模态参考（Seedance 2.0：参考图 1-9 张，参考视频最多 3 个）
        for img in ref_images or []:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _data_uri(img)},
                    "role": "reference_image",
                }
            )
        for vid in ref_videos or []:
            content.append(
                {
                    "type": "video_url",
                    "video_url": {"url": f"data:video/mp4;base64,{base64.b64encode(vid).decode('ascii')}"},
                    "role": "reference_video",
                }
            )
        # 参考音频（对白/口播）：`role` 固定是 reference_audio，是**参考**不是嘴型驱动。
        # 官方参数表里没有 lip_sync 之类的开关，所以出来的片子是「这个人用这个音色说这句话」，
        # 口型看着对得上，但不保证逐字对齐——界面上的文案也照着这个口径写，别过度承诺。
        # 另外：只有 Seedance 1.5 pro / 2.0 / 2.5 认这一段，1.0 系列会直接 400
        # （见 audio_ref_support / AUDIO_REF_NO_HINT，那句 400 会被翻译成人话）。
        if ref_audio is not None:
            content.append(
                {
                    "type": "audio_url",
                    "audio_url": {"url": _audio_data_uri(ref_audio)},
                    "role": "reference_audio",
                }
            )
        body: dict[str, Any] = {
            "model": model,
            "content": content,
            "duration": duration,
            "ratio": ratio,
            "resolution": resolution,
        }
        client = await self.client()
        try:
            resp = await client.post(
                f"{self.base_url}/contents/generations/tasks",
                headers={**self.headers(), "Content-Type": "application/json"},
                json=body,
            )
        except httpx.HTTPError as e:
            raise AdapterError(network_error_message(e), log_detail=network_error_detail(e, self.base_url)) from e
        if resp.status_code != 200:
            if ref_audio is not None and resp.status_code == 400:
                # 带参考音频的 400 绝大多数是「这一档模型不吃音频」。上游原文照旧带上
                # （它能说清是哪个参数不对），再补一句我们知道的名单——不然用户只会
                # 看到一句「上游拒绝了这次请求」，而这一句其实是有解的。
                try:
                    raise_upstream_error(resp)
                except AdapterError as e:
                    raise AdapterError(f"{e}\n{AUDIO_REF_NO_HINT}", log_detail=e.log_detail) from e
            raise_upstream_error(resp)
        data = resp.json()
        remote_id = data.get("id")
        if not remote_id:
            raise AdapterError(f"提交视频任务失败，响应中没有任务 ID：{str(data)[:200]}", log_detail=f"missing_task_id status=200 body={len(str(data))}B")
        return str(remote_id)

    async def poll_video(self, remote_id: str) -> VideoStatus:
        client = await self.client()
        try:
            resp = await client.get(
                f"{self.base_url}/contents/generations/tasks/{remote_id}",
                headers=self.headers(),
            )
        except httpx.HTTPError as e:
            return VideoStatus(status="processing", progress=0, error=None)
        if resp.status_code != 200:
            return VideoStatus(status="processing", progress=0)
        data = resp.json()
        status = str(data.get("status") or "").lower()
        if status == "succeeded":
            content = data.get("content") or {}
            url = content.get("video_url") if isinstance(content, dict) else None
            if not url:
                return VideoStatus(status="failed", error="任务完成但未返回视频地址")
            return VideoStatus(status="succeeded", video_url=url, progress=100)
        if status in ("failed", "cancelled", "expired"):
            err = data.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            return VideoStatus(status="failed", error=msg or f"任务状态：{status}")
        # queued / running
        progress = 30 if status == "running" else 10
        return VideoStatus(status="processing", progress=progress)

    async def synthesize_speech(self, **kwargs) -> SpeechResult:
        # 方舟的语音合成不在这一套鉴权里（豆包语音是另一个服务、另一套 appid/token），
        # 所以不在这里硬做：要走语音请单独配一个 OpenAI 兼容的服务。
        raise unsupported("语音合成（方舟的语音是另一套服务，请用 OpenAI 兼容类型接入）")
