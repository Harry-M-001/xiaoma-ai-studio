"""阿里云百炼 DashScope 适配器（可灵 4.0 / 3.0 系列 + 通义万相 3.0 / 2.7 系列）。

- 图片：POST /services/aigc/image-generation/generation（异步任务，messages 多模态格式）
- 视频：POST /services/aigc/video-generation/video-synthesis（异步任务，input.media 多模态格式）
- 轮询：GET /tasks/{task_id}（视频与图片任务通用）

两家模型共用同一套任务接口，差异只在 media type 枚举与参数命名，按模型 ID 前缀分支：
- kling/*：参考图 type=refer，resolution 参数 mode=std/pro/4k（由 720p/1080p/4k 映射）
- wan*：参考图 type=reference_image，视频参考 type=reference_video，分辨率 720P/1080P

素材格式：参考图收 base64；参考视频 / 参考音频只收公网 URL 或 OSS 临时地址，
本机文件满足不了，所以适配器会在提交前明确报错（不静默丢素材）。

文档：
- Kling 视频：https://help.aliyun.com/zh/model-studio/kling-video-generation-api-reference
- Wan 3.0 视频：https://help.aliyun.com/zh/model-studio/wan3-video-generation-api-reference
- Wan 2.7 图像：https://help.aliyun.com/zh/model-studio/wan-image-generation-and-editing-api-reference
"""

from __future__ import annotations

import asyncio
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
from app.providers.openai_compat import raise_upstream_error

_POLL_INTERVAL = 3.0
_POLL_MAX_WAIT = 300.0


def _sniff_image_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def wan_size(size: str) -> str:
    """把 `1024x1536` 转成百炼要的 `1024*1536`。

    万相 2.7 的 `size` 只收两种形状：档位（`2K` / `4K`）或 `宽*高`；
    它**没有** `aspect_ratio` 参数。认不出来就把原值透传，让上游报错——
    不要在这里随手改成一个「差不多」的尺寸（尺寸直接决定出图大小和计费）。
    """
    raw = str(size or "").strip()
    if "x" not in raw.lower():
        return raw or "2K"
    try:
        w, h = (int(x) for x in raw.lower().split("x", 1))
    except (TypeError, ValueError):
        return raw
    return f"{w}*{h}"


def _is_kling_v4(model: str) -> bool:
    return model.startswith("kling/") and "-v4-" in model


def _kling_ratio(ratio: str, *, v4: bool) -> str:
    """可灵 3.0 只认 16:9 / 9:16 / 1:1；4.0 多一个 21:9。其余一律回落到 16:9。"""
    allowed = {"16:9", "9:16", "1:1"} | ({"21:9"} if v4 else set())
    raw = (ratio or "").strip()
    return raw if raw in allowed else "16:9"


def _clamp_duration(model: str, duration: int) -> int:
    """按模型夹时长：可灵 3.0 是 3-15、4.0 是 3-30；万相 3.0 是 2-30、2.7 是 2-15。

    上游对超范围的时长会直接报 400，但报错只说「参数不合法」；
    这里先在本地夹到一个能跑的整数，并在日志里留下来（不静默改钱以外的参数）。
    """
    value = int(duration or 5)
    name = (model or "").strip().lower()
    if name.startswith("kling/"):
        return max(3, min(30 if _is_kling_v4(name) else 15, value))
    if name.startswith("wan3"):
        return max(2, min(30, value))
    if name.startswith("wan"):
        return max(2, min(15, value))
    return value


class DashScopeAdapter(BaseAdapter):
    kind = "dashscope"

    def headers(self) -> dict[str, str]:
        h = super().headers()
        h["X-DashScope-Async"] = "enable"
        return h

    async def test_connection(self, model: str | None = None) -> None:
        if not self.base_url:
            raise AdapterError("Base URL 为空", log_detail="config_invalid base_url_empty")
        client = await self.client()
        try:
            resp = await client.get(
                f"{self.base_url}/tasks/0", headers={"Authorization": f"Bearer {self.api_key}"}
            )
        except httpx.HTTPError as e:
            raise AdapterError(network_error_message(e), log_detail=network_error_detail(e, self.base_url)) from e
        # 404/400 = 鉴权通过但任务不存在；401/403 = Key 无效
        if resp.status_code in (401, 403):
            raise AdapterError(f"API Key 校验失败：HTTP {resp.status_code}", log_detail=f"HTTP {resp.status_code} auth_failed host={url_host(self.base_url)}")
        return

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        raise unsupported("文本（请使用 OpenAI 兼容协议接入百炼 compatible-mode 地址）")
        yield ""  # pragma: no cover

    # ---------- 图片（Kling 图片 / wan2.7-image，异步任务 + 内部轮询） ----------

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
        content: list[dict[str, Any]] = [{"text": prompt}]
        for img in ref_images or []:
            content.append({"image": _data_uri(img, _sniff_image_mime(img))})
        parameters: dict[str, Any] = {
            "n": max(1, min(int(n or 1), 9)),
            # 万相 2.7 的 size 只收「档位」或「宽*高」，没有 aspect_ratio
            "size": wan_size(size),
        }
        body = {
            "model": model,
            "input": {"messages": [{"role": "user", "content": content}]},
            "parameters": parameters,
        }
        client = await self.client()
        try:
            resp = await client.post(
                f"{self.base_url}/services/aigc/image-generation/generation",
                headers={**self.headers(), "Content-Type": "application/json"},
                json=body,
            )
        except httpx.HTTPError as e:
            raise AdapterError(network_error_message(e), log_detail=network_error_detail(e, self.base_url)) from e
        if resp.status_code not in (200, 201):
            raise_upstream_error(resp)
        task_id = (resp.json() or {}).get("output", {}).get("task_id")
        if not task_id:
            raise AdapterError(f"提交图片任务失败：{str(resp.json())[:200]}", log_detail=f"submit_image status=200 body={len(resp.content)}B")

        # 内部轮询直到出图
        waited = 0.0
        while waited < _POLL_MAX_WAIT:
            await asyncio.sleep(_POLL_INTERVAL)
            waited += _POLL_INTERVAL
            data = await self._get_task(client, str(task_id))
            status = str(data.get("status") or "").upper()
            if status == "SUCCEEDED":
                return await self._download_images(client, data)
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                msg = (data.get("message") or data.get("code") or "图片生成失败")[:300]
                raise AdapterError(str(msg))
        raise AdapterError("图片生成超时，请稍后在任务中心查看", log_detail="timeout image_generation")

    async def _download_images(self, client: httpx.AsyncClient, task: dict) -> list[bytes]:
        output = task.get("output") or {}
        urls: list[str] = []
        # wan 结构：output.results[].image_path / url
        for r in output.get("results") or []:
            u = r.get("image_path") or r.get("url") or r.get("image_url")
            if u:
                urls.append(str(u))
        # kling 结构：choices[].message.content[].image
        for ch in output.get("choices") or []:
            for c in (ch.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("image"):
                    urls.append(str(c["image"]))
        results: list[bytes] = []
        for u in urls:
            dl = await client.get(u)
            if dl.status_code != 200:
                raise AdapterError(f"下载结果图片失败：HTTP {dl.status_code}", log_detail=f"download HTTP {dl.status_code}")
            results.append(dl.content)
        if not results:
            raise AdapterError(f"任务完成但未解析到图片地址：{str(task)[:200]}", log_detail=f"missing_image_url status=200 body={len(str(task))}B")
        return results

    async def _get_task(self, client: httpx.AsyncClient, task_id: str) -> dict:
        try:
            resp = await client.get(
                f"{self.base_url}/tasks/{task_id}", headers=self.headers()
            )
        except httpx.HTTPError as e:
            raise AdapterError(f"查询任务失败：{e}", log_detail=network_error_detail(e, self.base_url)) from e
        if resp.status_code != 200:
            raise_upstream_error(resp)
        return (resp.json() or {}).get("output") or {}

    # ---------- 视频（Kling v3 系列 / wan2.7 系列） ----------

    @staticmethod
    def _is_kling(model: str) -> bool:
        return model.startswith("kling/")

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
        if ref_audio is not None:
            # 万相 3.0 虽然收 `reference_audio`，但只认**公网 URL / OSS 临时地址**；
            # 本机工作台的配音在本地磁盘上，发不出去。与其静默丢掉音频出一段
            # 没人说话的片子，不如直说，并指向能收 base64 音频的那一家。
            raise unsupported(
                "参考音频（对白/口播）——百炼的参考音频只收公网 URL 或 OSS 临时地址，"
                "本机文件满足不了。要走对白请用「火山方舟 Ark」类型接入 Seedance 1.5 pro / 2.0 / 2.5"
            )
        if ref_videos:
            # 视频同理：只收公网 URL / OSS，不收 base64。参考图才收 base64。
            raise unsupported(
                "参考视频——百炼只收公网 URL 或 OSS 临时地址，本机文件满足不了。"
                "想用参考视频请先把素材传到可公开访问的地址，或改用「KIE」类型接入"
            )
        if not self.base_url or not self.api_key:
            raise AdapterError("该服务尚未填写 Base URL 或 API Key", log_detail="config_invalid missing_credentials")
        kling = self._is_kling(model)
        kling_v4 = _is_kling_v4(model)
        if kling and "turbo" in model and last_frame:
            raise AdapterError(
                "可灵 3.0 Turbo 只支持首帧、不支持尾帧；要首尾帧请换成 "
                "kling/kling-v3-video-generation 或 kling/kling-v4-video-generation",
                log_detail="kling_turbo_no_last_frame",
            )
        media: list[dict[str, str]] = []
        if first_frame:
            media.append({"type": "first_frame", "url": _data_uri(first_frame, _sniff_image_mime(first_frame))})
        if last_frame:
            media.append({"type": "last_frame", "url": _data_uri(last_frame, _sniff_image_mime(last_frame))})
        ref_type = "refer" if kling else "reference_image"
        for img in ref_images or []:
            media.append({"type": ref_type, "url": _data_uri(img, _sniff_image_mime(img))})

        parameters: dict[str, Any] = {
            "duration": _clamp_duration(model, duration),
        }
        if kling:
            parameters["aspect_ratio"] = _kling_ratio(ratio, v4=kling_v4)
            mode = {"480p": "std", "720p": "std", "1080p": "pro", "4k": "4k"}.get(
                (resolution or "720p").lower(), "std"
            )
            # 可灵 4.0 与 3.0 Turbo 不支持 4k；4.0 默认 pro
            if mode == "4k" and (kling_v4 or "turbo" in model):
                mode = "pro"
            parameters["mode"] = mode
        else:
            parameters["resolution"] = (resolution or "720p").upper()
            parameters["ratio"] = ratio or "16:9"

        input_: dict[str, Any] = {"prompt": prompt}
        if media:
            input_["media"] = media
        body: dict[str, Any] = {"model": model, "input": input_, "parameters": parameters}

        client = await self.client()
        try:
            resp = await client.post(
                f"{self.base_url}/services/aigc/video-generation/video-synthesis",
                headers={**self.headers(), "Content-Type": "application/json"},
                json=body,
            )
        except httpx.HTTPError as e:
            raise AdapterError(network_error_message(e), log_detail=network_error_detail(e, self.base_url)) from e
        if resp.status_code not in (200, 201):
            raise_upstream_error(resp)
        task_id = (resp.json() or {}).get("output", {}).get("task_id")
        if not task_id:
            raise AdapterError(f"提交视频任务失败：{str(resp.json())[:200]}", log_detail=f"submit_video status=200 body={len(resp.content)}B")
        return str(task_id)

    async def poll_video(self, remote_id: str) -> VideoStatus:
        client = await self.client()
        try:
            output = await self._get_task(client, remote_id)
        except AdapterError as e:
            # 网络抖动按处理中处理，下轮再查
            return VideoStatus(status="processing", progress=0, error=str(e))
        status = str(output.get("status") or "").upper()
        if status == "SUCCEEDED":
            video_url = output.get("video_url")
            if not video_url:
                # wan 结构：output.results[].url / video_path
                for r in output.get("results") or []:
                    video_url = r.get("url") or r.get("video_path")
                    if video_url:
                        break
            if not video_url:
                return VideoStatus(status="failed", error="任务完成但未返回视频地址")
            return VideoStatus(status="succeeded", video_url=str(video_url), progress=100)
        if status in ("FAILED", "CANCELED", "UNKNOWN"):
            msg = output.get("message") or output.get("code") or f"任务状态：{status}"
            return VideoStatus(status="failed", error=str(msg)[:300])
        # PENDING / RUNNING
        progress = 30 if status == "RUNNING" else 10
        return VideoStatus(status="processing", progress=progress)

    async def synthesize_speech(self, **kwargs) -> SpeechResult:
        # 百炼的语音合成走的是另一套接口与参数（不是 OpenAI 的 /audio/speech），
        # 想要语音请用 OpenAI 兼容类型接入百炼的 compatible-mode 地址。
        raise unsupported("语音合成（请用 OpenAI 兼容类型接入百炼 compatible-mode 地址）")

