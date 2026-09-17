"""火山引擎方舟（Volcengine Ark）适配器。

- 文本：/api/v3/chat/completions（OpenAI 兼容）
- 图片：/api/v3/images/generations（Seedream，支持参考图改图）
- 视频：/api/v3/contents/generations/tasks（Seedance 异步任务：文生 / 首帧图生）

文档：https://www.volcengine.com/docs/82379
"""

from __future__ import annotations

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
from app.providers.openai_compat import iter_chat_sse, raise_upstream_error

DEFAULT_ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"


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
