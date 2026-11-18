"""KIE（聚合网关）的**异步任务**适配器：图片（文生图 / 参考图编辑）+ 视频（首帧 / 文生）。

## 为什么单独一个适配器，而不复用 OpenAI 兼容

KIE 的聊天是 OpenAI 兼容的（`/<模型>/v1/chat/completions`），但图片/视频走的是
**统一异步任务接口**：`POST /api/v1/jobs/createTask` → `GET /api/v1/jobs/recordInfo`。
那套接口与 `images/generations` 的同步返回完全不同，硬塞进 OpenAI 兼容适配器只会
写出一个「看名字是兼容、实际不通」的东西。

## 参考图要先换成 URL

KIE 的模型只吃公网 URL：本地图片必须先走它的上传服务
（`https://kieai.redpandaai.co/api/file-base64-upload`，**与 api.kie.ai 不同域**），
拿 `downloadUrl` 再塞进 `image_urls` / `image_url`。上传的文件 **24 小时**后删除。

## 三条必须记住的网关特性

1. **HTTP 200 里包着业务错误**：`{"code":402,"msg":"insufficient credits"}`。
2. **结果是字符串套字符串**：`resultJson` 是 JSON 字符串，里面的 `resultUrls` 才是链接。
3. **模型名决定参数形状**：`*-edit` 才吃 `image_urls`；视频分 `*-image-to-video` 与
   `*-text-to-video`。认不出的模型**直接报错**，不猜参数（视频是真金白银）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import AsyncIterator

import httpx

from app.providers.base import (
    AdapterError,
    AudioRef,
    BaseAdapter,
    SpeechResult,
    VideoStatus,
    network_error_detail,
    network_error_message,
    unsupported,
    url_host,
)

_POLL_INTERVAL = 3.0
_POLL_MAX_WAIT = 300.0
_DOWNLOAD_TIMEOUT = 120.0

#: 参考图必须先换成 URL 才能喂给 KIE 的模型；这是它自己的上传服务（与 api.kie.ai 不同域）
UPLOAD_URL = "https://kieai.redpandaai.co/api/file-base64-upload"

#: Kling 系视频只认这几个画幅
_ASPECTS = {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9"}


def image_size_enum(size: str) -> str:
    """把 `1024x1536` 这类画幅映射成 KIE 的枚举。认不出来就按方图。"""
    try:
        w, h = (int(x) for x in str(size).lower().split("x", 1))
        ratio = w / h if h else 1.0
    except (TypeError, ValueError):
        return "square_hd"
    if 0.95 <= ratio <= 1.05:
        return "square_hd"
    if ratio > 1:
        return "landscape_16_9" if ratio >= 1.6 else "landscape_4_3"
    return "portrait_16_9" if ratio <= 0.625 else "portrait_4_3"


def image_input_payload(
    model: str, prompt: str, size: str, n: int, image_urls: list[str]
) -> dict:
    """图片任务的 input 形状。纯函数，便于单测（不联网）。"""
    payload: dict = {
        "prompt": prompt,
        "image_size": image_size_enum(size),
        "image_resolution": "1K",
        "max_images": max(1, int(n)),
    }
    if image_urls:
        # 只有 *-edit 模型吃 image_urls；调用方已经校验过模型名
        payload["image_urls"] = list(image_urls)
    return payload


def video_input_payload(
    model: str, prompt: str, duration: int, ratio: str, image_url: str
) -> dict:
    """视频任务的 input 形状。按模型名分辨「图生视频 / 文生视频」。

    这里**不猜**：认不出的模型直接报错并列出支持的两类，避免把文生视频模型塞进首帧。
    """
    name = str(model).lower()
    baked: dict = {"prompt": prompt, "duration": "10" if int(duration) >= 10 else "5"}
    i2v = "image-to-video" in name or "i2v" in name
    t2v = "text-to-video" in name or "t2v" in name
    if i2v:
        if not image_url:
            raise AdapterError(
                f"模型「{model}」是图生视频，必须给首帧；没有首帧请换成同家族的 *-text-to-video 模型。",
                log_detail="kie_video_needs_first_frame",
            )
        baked["image_url"] = image_url
    elif t2v:
        if image_url:
            raise AdapterError(
                f"模型「{model}」是文生视频，不接受首帧；要首帧请换成 *-image-to-video 模型。",
                log_detail="kie_video_rejects_first_frame",
            )
        baked["aspect_ratio"] = ratio if ratio in _ASPECTS else "16:9"
    else:
        raise AdapterError(
            f"KIE 的视频模型「{model}」还没适配：目前只认 *-image-to-video / *-text-to-video "
            "这两类命名（如 kling/v2-5-turbo-image-to-video-pro）。",
            log_detail="kie_video_model_unsupported",
        )
    baked["negative_prompt"] = "blur, distort, and low quality"
    return baked


class KieAdapter(BaseAdapter):
    kind = "kie"

    # ---- 内部：统一处理「200 里的业务错误」 ----

    async def _call(self, client: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
        # 上传服务在另一个域，所以允许直接传绝对 URL
        url = path if path.startswith("http") else f"{self.base_url.rstrip('/')}{path}"
        try:
            resp = await client.request(method, url, headers=self.headers(), **kw)
        except httpx.HTTPError as e:
            raise AdapterError(network_error_message(e), log_detail=network_error_detail(e, url)) from e
        try:
            data = resp.json()
        except ValueError as e:
            raise AdapterError("上游返回的不是 JSON", log_detail=f"bad_json host={url_host(url)}") from e
        code = data.get("code")
        if code is not None and int(code) != 200:
            raise AdapterError(
                f"KIE 返回错误（{code}）：{str(data.get('msg') or '')[:200]}",
                log_detail=f"kie_code={code} host={url_host(url)}",
            )
        return data.get("data") or {}

    async def _poll(self, client: httpx.AsyncClient, task_id: str) -> dict:
        waited = 0.0
        while waited < _POLL_MAX_WAIT:
            await asyncio.sleep(_POLL_INTERVAL)
            waited += _POLL_INTERVAL
            data = await self._call(
                client, "GET", "/api/v1/jobs/recordInfo", params={"taskId": task_id}
            )
            state = str(data.get("state") or "")
            if state == "success":
                return data
            if state == "fail":
                raise AdapterError(
                    f"KIE 任务失败：{str(data.get('failMsg') or '未知原因')[:200]}",
                    log_detail=f"kie_fail code={data.get('failCode')}",
                )
        raise AdapterError(
            f"KIE 任务 {task_id} 超过 {int(_POLL_MAX_WAIT)} 秒仍未完成",
            log_detail="kie_timeout",
        )

    async def _upload(self, data: bytes, filename: str) -> str:
        """把本地图片换成公网 URL（KIE 的模型只吃 URL）。

        上传走它自己的 base64 服务；文件名要唯一（同名会覆盖、还可能命中缓存），
        所以带时间戳前缀。返回 `downloadUrl`。
        """
        b64 = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
        payload = {
            "base64Data": b64,
            "uploadPath": "images/kie-agent",
            "fileName": f"{int(time.time() * 1000)}-{filename}",
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(
                    UPLOAD_URL,
                    headers={**self.headers(), "Content-Type": "application/json"},
                    json=payload,
                )
            except httpx.HTTPError as e:
                raise AdapterError(
                    network_error_message(e), log_detail=network_error_detail(e, UPLOAD_URL)
                ) from e
        if resp.status_code != 200:
            raise AdapterError(
                f"上传参考图失败（HTTP {resp.status_code}）",
                log_detail=f"upload_status={resp.status_code}",
            )
        try:
            body = resp.json()
        except ValueError as e:
            raise AdapterError("上传参考图的响应不是 JSON", log_detail="upload_bad_json") from e
        url = str(((body.get("data") or {}).get("downloadUrl")) or "")
        if not url:
            raise AdapterError(
                f"上传参考图失败：{str(body.get('msg') or body)[:160]}",
                log_detail="upload_no_url",
            )
        return url

    @staticmethod
    async def _download(url: str) -> bytes:
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as c:
            try:
                resp = await c.get(url)
            except httpx.HTTPError as e:
                raise AdapterError(
                    network_error_message(e), log_detail=network_error_detail(e, url)
                ) from e
            if resp.status_code != 200:
                raise AdapterError(
                    f"下载生成结果失败（HTTP {resp.status_code}）",
                    log_detail=f"download_status={resp.status_code} host={url_host(url)}",
                )
            return resp.content

    # ---- 连通性 ----

    async def test_connection(self, model: str | None = None) -> None:
        """用一个不存在的 taskId 探活：**422 = 鉴权通过**，401/402 才是真的不可用。

        KIE 没有「列模型」这类只读接口，而创建任务会花钱——所以拿查询接口探活，
        并且**只认错误码的语义**（它把 401/422 都包在 HTTP 200 里）。
        """
        if not self.base_url or not self.api_key:
            raise AdapterError("Base URL 或 API Key 为空", log_detail="config_invalid")
        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                resp = await client.get(
                    f"{self.base_url.rstrip('/')}/api/v1/jobs/recordInfo",
                    headers=self.headers(),
                    params={"taskId": "0"},
                )
            except httpx.HTTPError as e:
                raise AdapterError(
                    network_error_message(e), log_detail=network_error_detail(e, self.base_url)
                ) from e
        try:
            data = resp.json()
        except ValueError as e:
            raise AdapterError("KIE 返回的不是 JSON", log_detail="bad_json") from e
        code = int(data.get("code") or 0)
        if code in (401, 402):
            raise AdapterError(
                f"KIE 鉴权失败（{code}）：{str(data.get('msg') or '')[:120]}",
                log_detail=f"kie_code={code}",
            )
        # 200（居然查到了）/ 422（任务不存在）/ 429（限流）都说明 Key 是通的
        return None

    # ---- 文本 ----

    def chat_stream(
        self, model: str, messages: list[dict], temperature: float = 0.7, max_tokens: int | None = None
    ) -> AsyncIterator[str]:
        raise AdapterError(
            "KIE 的文本对话走的是另一条端点（/<模型>/v1/chat/completions），"
            "请把它建成「OpenAI 兼容」服务，类型不要选 KIE。",
            log_detail="kie_chat_wrong_kind",
        )

    # ---- 图片 ----

    async def generate_image(
        self,
        *,
        model: str,
        prompt: str,
        n: int = 1,
        size: str = "1024x1024",
        ref_images: list[bytes] | None = None,
    ) -> list[bytes]:
        async with httpx.AsyncClient(timeout=60.0) as client:
            urls: list[str] = []
            if ref_images:
                if "edit" not in model.lower():
                    raise AdapterError(
                        f"模型「{model}」是文生图，不接受参考图。要参考图请换成同家族的 *-edit 模型"
                        "（如 bytedance/seedream-v4-edit）。",
                        log_detail="kie_ref_needs_edit_model",
                    )
                for i, data in enumerate(ref_images, 1):
                    urls.append(await self._upload(data, f"ref-{i}.png"))
            payload = {
                "model": model,
                "input": image_input_payload(model, prompt, size, n, urls),
            }
            created = await self._call(client, "POST", "/api/v1/jobs/createTask", json=payload)
            task_id = str(created.get("taskId") or "")
            if not task_id:
                raise AdapterError("KIE 没有返回任务 id", log_detail="kie_no_task_id")
            done = await self._poll(client, task_id)

        urls_out: list[str] = []
        parsed = done.get("response") or {}
        if isinstance(parsed, dict) and parsed.get("resultUrls"):
            urls_out = list(parsed["resultUrls"])
        if not urls_out:
            try:
                urls_out = list(json.loads(done.get("resultJson") or "{}").get("resultUrls") or [])
            except (TypeError, ValueError):
                urls_out = []
        if not urls_out:
            raise AdapterError(
                "KIE 任务成功但没给出结果链接（可能是模型返回结构与预期不同）",
                log_detail="kie_no_result_urls",
            )
        return [await self._download(u) for u in urls_out[: max(1, int(n))]]

    # ---- 视频 ----

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
        if last_frame or ref_images or ref_videos or ref_audio:
            # 「全能参考」这套我们本期没接：静默丢掉会让用户以为参考生效了，
            # 而视频是真金白银，出一段不对的片子比报错更糟
            raise AdapterError(
                "KIE 这一版只接了「首帧生视频 / 文生视频」；尾帧、多图参考、视频参考、"
                "参考音频都还没接。",
                log_detail="kie_video_ref_unsupported",
            )
        image_url = ""
        async with httpx.AsyncClient(timeout=60.0) as client:
            if first_frame is not None:
                image_url = await self._upload(first_frame, "first-frame.png")
            payload = {
                "model": model,
                "input": video_input_payload(model, prompt, duration, ratio, image_url),
            }
            created = await self._call(client, "POST", "/api/v1/jobs/createTask", json=payload)
        task_id = str(created.get("taskId") or "")
        if not task_id:
            raise AdapterError("KIE 没有返回任务 id", log_detail="kie_no_task_id")
        return task_id

    async def poll_video(self, remote_id: str) -> VideoStatus:
        async with httpx.AsyncClient(timeout=30.0) as client:
            data = await self._call(
                client, "GET", "/api/v1/jobs/recordInfo", params={"taskId": remote_id}
            )
        state = str(data.get("state") or "")
        progress = int(data.get("progress") or 0)
        if state == "success":
            url = ""
            try:
                url = (json.loads(data.get("resultJson") or "{}").get("resultUrls") or [""])[0]
            except (TypeError, ValueError, IndexError):
                url = ""
            if not url:
                return VideoStatus(status="failed", error="KIE 说成功但没给视频链接")
            return VideoStatus(status="succeeded", video_url=url, progress=100)
        if state == "fail":
            return VideoStatus(
                status="failed", error=str(data.get("failMsg") or "KIE 任务失败")[:200]
            )
        return VideoStatus(status="processing", progress=min(99, max(0, progress)))

    async def synthesize_speech(
        self, *, model: str, text: str, voice: str = "", speed: float = 1.0
    ) -> SpeechResult:
        raise unsupported("KIE 的语音合成（未接入）")
