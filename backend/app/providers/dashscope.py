"""阿里云百炼 DashScope 适配器（可灵 Kling v3 系列 + 通义万相 wan2.7 系列）。

- 图片：POST /services/aigc/image-generation/generation（异步任务，messages 多模态格式）
- 视频：POST /services/aigc/video-generation/video-synthesis（异步任务，input.media 多模态格式）
- 轮询：GET /tasks/{task_id}（视频与图片任务通用）

两家模型共用同一套任务接口，差异只在 media type 枚举与参数命名，按模型 ID 前缀分支：
- kling/*：参考图 type=refer，resolution 参数 mode=std/pro/4k（由 720p/1080p/4k 映射）
- wan*：参考图 type=reference_image，视频参考 type=video，resolution=720P/1080P

文档：
- Kling 视频：https://help.aliyun.com/zh/model-studio/kling-video-generation-api-reference
- Wan 视频（wan2.7-i2v / wan2.7-videoedit）：https://help.aliyun.com/zh/model-studio/
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any, AsyncIterator

import httpx

from app.providers.base import (
    AdapterError,
    BaseAdapter,
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
        parameters: dict[str, Any] = {"n": max(1, min(int(n or 1), 9))}
        # size 传 "1024x1024" / "2K" 两种风格：x 分隔的按 ratio 换算 aspect_ratio
        if "x" in size.lower():
            w, h = size.lower().split("x", 1)
            try:
                parameters["aspect_ratio"] = _ratio_of(int(w), int(h))
            except (TypeError, ValueError):
                parameters["aspect_ratio"] = "1:1"
        else:
            parameters["size"] = size
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
    ) -> str:
        if not self.base_url or not self.api_key:
            raise AdapterError("该服务尚未填写 Base URL 或 API Key", log_detail="config_invalid missing_credentials")
        kling = self._is_kling(model)
        media: list[dict[str, str]] = []
        if first_frame:
            media.append({"type": "first_frame", "url": _data_uri(first_frame, _sniff_image_mime(first_frame))})
        if last_frame:
            media.append({"type": "last_frame", "url": _data_uri(last_frame, _sniff_image_mime(last_frame))})
        ref_type = "refer" if kling else "reference_image"
        for img in ref_images or []:
            media.append({"type": ref_type, "url": _data_uri(img, _sniff_image_mime(img))})
        if not kling:
            for vid in ref_videos or []:
                media.append({"type": "video", "url": _data_uri(vid, "video/mp4")})

        parameters: dict[str, Any] = {
            "duration": int(duration or 5),
        }
        if kling:
            parameters["aspect_ratio"] = ratio or "16:9"
            parameters["mode"] = {"480p": "std", "720p": "std", "1080p": "pro", "4k": "4k"}.get(
                (resolution or "720p").lower(), "std"
            )
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


def _ratio_of(w: int, h: int) -> str:
    from math import gcd

    g = gcd(w, h) or 1
    rw, rh = w // g, h // g
    # 百炼只接受有限枚举，落到最近的常用比例
    for a, b in ((1, 1), (16, 9), (9, 16), (4, 3), (3, 4), (21, 9), (3, 2), (2, 3)):
        if abs(rw / rh - a / b) < 0.05:
            return f"{a}:{b}"
    return "16:9" if rw > rh else "9:16"
