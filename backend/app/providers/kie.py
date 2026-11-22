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

## 两代入参形状（2026-11 按官方文档核对）

- **老一代**（`bytedance/seedream-v4-*`、`kling/v2-5-*`）：图片用
  `image_size` + `image_resolution` + `max_images`；视频用 `image_url` + 字符串时长。
- **新一代**（`seedream/5-*`、`seedream/4.5-*`、`nano-banana-2*`、`gpt-image-2*`、
  `bytedance/seedance-2*`、`kling-4.0/*`、`wan/3-0-video*`）：图片统一改成
  `aspect_ratio`（不再传像素），编辑图字段各不相同（`image_urls` / `image_input` /
  `input_urls`）；视频改用 `first_frame_url` + 整数时长。**字段名不能互相套用**，
  所以按家族分派，而不是「差不多就发」。
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
#: 大文件（参考视频）走流式上传，免得 base64 再胖 33%
UPLOAD_STREAM_URL = "https://kieai.redpandaai.co/api/file-stream-upload"

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


def image_aspect_ratio(size: str) -> str:
    """把 `1024x1536` 这类画幅映射成 `3:4`。新一代图片模型只吃比例，不收像素。

    与 `image_size_enum` 同一套阈值，只是输出形状不同；认不出来按方图。
    """
    try:
        w, h = (int(x) for x in str(size).lower().split("x", 1))
        ratio = w / h if h else 1.0
    except (TypeError, ValueError):
        return "1:1"
    if 0.95 <= ratio <= 1.05:
        return "1:1"
    if ratio > 1:
        return "16:9" if ratio >= 1.6 else "4:3"
    return "9:16" if ratio <= 0.625 else "3:4"


def image_family(model: str) -> str:
    """图片模型的入参家族。老一代返回 `legacy`，其余按新形状分派。"""
    name = (model or "").strip().lower()
    if name.startswith("seedream/4.5-") or name.startswith("seedream/5"):
        return "seedream_new"
    if name.startswith("nano-banana-2"):
        return "nano_banana_2"
    if name.startswith("gpt-image-2"):
        return "gpt_image_2"
    return "legacy"


def accepts_image_refs(model: str) -> bool:
    """这个图片模型吃不吃参考图。

    `nano-banana-2` 的文生/图生是同一个模型（`image_input` 可为空），所以整族都收；
    GPT Image 2 则拆成 text / image 两条，只有 `*-image-to-image` 收。
    """
    name = (model or "").strip().lower()
    if "image-to-image" in name or "edit" in name:
        return True
    return name.startswith("nano-banana-2")


def is_new_image_family(model: str) -> bool:
    """新一代图片模型一次只出一张（没有 `max_images`），要 n 张得提交 n 次。"""
    return image_family(model) != "legacy"


def image_input_payload(
    model: str, prompt: str, size: str, n: int, image_urls: list[str]
) -> dict:
    """图片任务的 input 形状。纯函数，便于单测（不联网）。"""
    family = image_family(model)
    name = (model or "").strip().lower()

    if family == "seedream_new":
        # Seedream 5.0 系 / 4.5：比例 + 档位；Flash 用 size，Pro/Lite/4.5 用 quality
        payload: dict = {
            "prompt": prompt,
            "aspect_ratio": image_aspect_ratio(size),
            "nsfw_checker": False,
        }
        if "flash" in name:
            payload["size"] = "1K"
        else:
            payload["quality"] = "basic"
        if name.startswith("seedream/5"):
            # 4.5 的输出格式不固定（文档明说可能 PNG 也可能 JPEG），不能硬塞
            payload["output_format"] = "png"
        if image_urls:
            payload["image_urls"] = list(image_urls)
        return payload

    if family == "nano_banana_2":
        payload = {"prompt": prompt, "aspect_ratio": image_aspect_ratio(size)}
        if name == "nano-banana-2":
            payload["resolution"] = "1K"
            payload["output_format"] = "png"
            if image_urls:
                payload["image_input"] = list(image_urls)
        elif image_urls:
            # Lite 的字段名与标准版不同（文档如此），不能混用
            payload["image_urls"] = list(image_urls)
        return payload

    if family == "gpt_image_2":
        payload = {"prompt": prompt, "aspect_ratio": image_aspect_ratio(size)}
        if image_urls:
            payload["input_urls"] = list(image_urls)
        return payload

    payload = {
        "prompt": prompt,
        "image_size": image_size_enum(size),
        "image_resolution": "1K",
        "max_images": max(1, int(n)),
    }
    if image_urls:
        # 只有 *-edit 模型吃 image_urls；调用方已经校验过模型名
        payload["image_urls"] = list(image_urls)
    return payload


def video_family(model: str) -> str:
    """视频模型的入参家族；与图片一样按名字分派，不猜参数。"""
    name = (model or "").strip().lower()
    if name.startswith("bytedance/seedance-2"):
        return "seedance2"
    if name.startswith("kling-4.0/keyframe-to-video"):
        return "kling4_keyframe"
    if name.startswith("wan/3-0-video"):
        return "wan3"
    if "image-to-video" in name or "text-to-video" in name or "i2v" in name or "t2v" in name:
        return "legacy"
    return "unknown"


def _video_ratio(ratio: str, family: str) -> str:
    raw = (ratio or "").strip()
    if family == "wan3":
        return raw if raw == "adaptive" or raw in _ASPECTS else "adaptive"
    return raw if raw == "adaptive" or raw in _ASPECTS else "16:9"


def _video_duration(model: str, family: str, duration: int) -> int:
    """按家族夹时长：Seedance 2.0 是 4–15，2.5 是 4–30，Wan 是 2–30，Kling 4 是 3–30。"""
    value = int(duration or 5)
    name = (model or "").strip().lower()
    if family == "seedance2":
        upper = 30 if "2-5" in name else 15
        return max(4, min(upper, value))
    if family == "wan3":
        return max(2, min(30, value))
    if family == "kling4_keyframe":
        return max(3, min(30, value))
    return value


def _check_reference_limits(model: str, family: str, images: int, videos: int, audios: int) -> None:
    """参考素材数量按官方上限拦：上游的超限报错只会给一句笼统的 422。"""
    name = (model or "").strip().lower()
    if family == "seedance2":
        caps = (30, 10, 10) if "2-5" in name else (9, 3, 3)
    elif family == "wan3":
        caps = (10, 5, 5)
    else:
        return
    for label, got, cap in (("参考图", images, caps[0]), ("参考视频", videos, caps[1]), ("参考音频", audios, caps[2])):
        if got > cap:
            raise AdapterError(
                f"「{model}」最多收 {cap} 个{label}，这次给了 {got} 个；先删掉几个再跑。",
                log_detail=f"kie_video_too_many_refs kind={label} got={got} cap={cap}",
            )


def _reference_payload(
    model: str,
    family: str,
    prompt: str,
    image_url: str,
    ref_image_urls: list[str],
    ref_video_urls: list[str],
    ref_audio_urls: list[str],
) -> dict:
    """多模态参考的 input 形状（Seedance 2 / Wan 3 共用一套字段名）。

    官方把「首帧 / 首尾帧」和「多模态参考」定义为互斥的两种任务：
    同时给会被上游直接拒，所以这里先把首帧并进参考图（这是数字人那条路：
    一张图 + 一条配音，没有参考图字段可用），并在提示词里点明它是首帧。
    """
    images = ([image_url] if image_url else []) + list(ref_image_urls)
    payload: dict = {"prompt": prompt}
    if images:
        payload["reference_image_urls"] = images
    if ref_video_urls:
        payload["reference_video_urls"] = list(ref_video_urls)
    if ref_audio_urls:
        payload["reference_audio_urls"] = list(ref_audio_urls)
    # 只有「一张图 + 一条配音」这种组合才补这句：它是数字人/口播，不是随便的参考图
    if image_url and ref_audio_urls and not ref_image_urls and not ref_video_urls:
        payload["prompt"] = (
            f"{prompt}\n\n（第一张参考图作为视频首帧；参考音频作为这条视频的声音/对白。）"
        )
    return payload


def video_input_payload(
    model: str,
    prompt: str,
    duration: int,
    ratio: str,
    image_url: str,
    resolution: str = "720p",
    *,
    last_frame_url: str = "",
    ref_image_urls: list[str] | None = None,
    ref_video_urls: list[str] | None = None,
    ref_audio_urls: list[str] | None = None,
) -> dict:
    """视频任务的 input 形状。按模型家族分派「文生 / 首帧 / 首尾帧 / 多模态参考」。

    三条来自官方文档的硬约束（2026-11 逐条核对）：

    1. **首帧 / 首尾帧 与 参考图·参考视频·参考音频 互斥**，同时给会被拒；
    2. `last_frame_url` 不能单独给，必须跟首帧一起；
    3. 参考素材有数量上限（Seedance 2.0 是 9/3/3，2.5 是 30/10/10，Wan 是 10/5/5）。

    这里**不猜**：认不出的模型、给不了的组合，都在提交前报错并说清换成什么。
    """
    family = video_family(model)
    res = (resolution or "720p").strip().lower()
    ref_images = [u for u in (ref_image_urls or []) if u]
    ref_videos = [u for u in (ref_video_urls or []) if u]
    ref_audios = [u for u in (ref_audio_urls or []) if u]
    has_refs = bool(ref_images or ref_videos or ref_audios)

    if family == "seedance2":
        _check_reference_limits(model, family, len(ref_images) + (1 if image_url else 0),
                                len(ref_videos), len(ref_audios))
        payload: dict = {
            "prompt": prompt,
            "resolution": res if res in ("480p", "720p", "1080p", "4k") else "720p",
            "duration": _video_duration(model, family, duration),
        }
        if last_frame_url and not image_url:
            raise AdapterError(
                "尾帧不能单独给：请同时提供首帧。",
                log_detail="kie_video_last_frame_without_first",
            )
        if last_frame_url and has_refs:
            raise AdapterError(
                "首尾帧和多模态参考不能同时用（官方定义为两种互斥任务）。"
                "要严格对齐首尾画面，就只给首尾帧；要用参考图/参考音，就把图放进参考图位。",
                log_detail="kie_video_frames_and_refs",
            )
        if image_url and not has_refs:
            payload["first_frame_url"] = image_url
            if last_frame_url:
                payload["last_frame_url"] = last_frame_url
            return payload
        if has_refs:
            return {
                **_reference_payload(
                    model, family, prompt, image_url, ref_images, ref_videos, ref_audios
                ),
                "resolution": payload["resolution"],
                "duration": payload["duration"],
                "aspect_ratio": _video_ratio(ratio, family),
            }
        payload["aspect_ratio"] = _video_ratio(ratio, family)
        return payload

    if family == "kling4_keyframe":
        # Kling 4.0 关键帧：只收首帧 / 中间帧 / 尾帧，没有参考图/参考音字段
        if not image_url:
            raise AdapterError(
                "Kling 4.0 是「关键帧生视频」，必须给首帧；纯文生视频请换 "
                "bytedance/seedance-2-5 或 wan/3-0-video。",
                log_detail="kie_video_needs_first_frame",
            )
        if last_frame_url and has_refs:
            raise AdapterError(
                "Kling 4.0 的关键帧模式不收参考图/参考视频/参考音频；"
                "要多模态参考请换 bytedance/seedance-2-5 或 wan/3-0-video。",
                log_detail="kie_video_frames_and_refs",
            )
        if has_refs:
            raise AdapterError(
                "Kling 4.0 的关键帧模式不收参考素材：参考图请换 "
                "bytedance/seedance-2-5 或 wan/3-0-video。",
                log_detail="kie_video_refs_unsupported",
            )
        payload = {
            "prompt": prompt,
            "duration": _video_duration(model, family, duration),
            "resolution": "1080p" if res == "1080p" else "720p",
            "first_frame_url": image_url,
        }
        if last_frame_url:
            payload["last_frame_url"] = last_frame_url
        return payload

    if family == "wan3":
        _check_reference_limits(model, family, len(ref_images) + (1 if image_url else 0),
                                len(ref_videos), len(ref_audios))
        payload = {
            "prompt": prompt,
            "resolution": res.upper() if res in ("480p", "720p", "1080p") else "720P",
            "duration": _video_duration(model, family, duration),
        }
        if last_frame_url and not image_url:
            raise AdapterError(
                "尾帧不能单独给：请同时提供首帧。",
                log_detail="kie_video_last_frame_without_first",
            )
        if last_frame_url and has_refs:
            raise AdapterError(
                "首尾帧和多模态参考不能同时用（官方定义为两种互斥任务）。",
                log_detail="kie_video_frames_and_refs",
            )
        if image_url and not has_refs:
            payload["first_frame_url"] = image_url
            if last_frame_url:
                payload["last_frame_url"] = last_frame_url
            return payload
        if has_refs:
            return {
                **_reference_payload(
                    model, family, prompt, image_url, ref_images, ref_videos, ref_audios
                ),
                "resolution": payload["resolution"],
                "duration": payload["duration"],
                "aspect_ratio": _video_ratio(ratio, family),
            }
        payload["aspect_ratio"] = _video_ratio(ratio, family)
        return payload

    name = (model or "").strip().lower()
    if last_frame_url or has_refs:
        raise AdapterError(
            f"「{model}」只吃首帧；首尾帧/多参考请换 bytedance/seedance-2-5 或 wan/3-0-video。",
            log_detail="kie_video_refs_unsupported",
        )
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
            f"KIE 的视频模型「{model}」还没适配：目前认 *-image-to-video / *-text-to-video、"
            "bytedance/seedance-2*、kling-4.0/keyframe-to-video、wan/3-0-video。",
            log_detail="kie_video_model_unsupported",
        )
    baked["negative_prompt"] = "blur, distort, and low quality"
    return baked


def audio_upload_mime(ref: AudioRef) -> tuple[str, str]:
    """参考音频的 (mime, 扩展名)。上游只认 wav / mp3，其它格式本地就拦下。

    类型以库里记的为准；记成 `application/octet-stream` 时用魔数兜一下——
    TTS 产物存成这种类型是见过的，直接按「不认识」拒掉太粗暴。
    """
    data = ref.data or b""
    ct = (ref.content_type or "").split(";")[0].strip().lower()
    if ct in ("audio/wav", "audio/x-wav", "audio/wave"):
        return "audio/wav", "wav"
    if ct in ("audio/mpeg", "audio/mp3"):
        return "audio/mpeg", "mp3"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav", "wav"
    if data[:3] == b"ID3":
        return "audio/mpeg", "mp3"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "audio/mpeg", "mp3"
    raise AdapterError(
        f"参考音频只认 wav / mp3（这次的类型是「{ref.content_type or '未知'}」）——"
        "换一条配音再试，别让上游在收钱之后才拒。",
        log_detail="kie_audio_container",
    )


def _result_urls(done: dict) -> list[str]:
    """从 recordInfo 的结果里取链接：`resultJson` 是字符串套字符串，先看 `response`。"""
    parsed = done.get("response") or {}
    if isinstance(parsed, dict) and parsed.get("resultUrls"):
        return list(parsed["resultUrls"])
    try:
        return list(json.loads(done.get("resultJson") or "{}").get("resultUrls") or [])
    except (TypeError, ValueError):
        return []


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

    async def _upload(
        self,
        data: bytes,
        filename: str,
        *,
        mime: str = "image/png",
        folder: str = "images/kie-agent",
    ) -> str:
        """把本地文件换成公网 URL（KIE 的模型只吃 URL）。

        上传走它自己的 base64 服务；MIME 由 data URL 前缀带过去（它也认文件名后缀）。
        文件名要唯一（同名会覆盖、还可能命中缓存），所以带时间戳前缀。
        """
        b64 = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
        payload = {
            "base64Data": b64,
            "uploadPath": folder,
            "fileName": f"{int(time.time() * 1000)}-{filename}",
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
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
                f"上传参考素材失败（HTTP {resp.status_code}）",
                log_detail=f"upload_status={resp.status_code}",
            )
        try:
            body = resp.json()
        except ValueError as e:
            raise AdapterError("上传参考素材的响应不是 JSON", log_detail="upload_bad_json") from e
        url = str(((body.get("data") or {}).get("downloadUrl")) or "")
        if not url:
            raise AdapterError(
                f"上传参考素材失败：{str(body.get('msg') or body)[:160]}",
                log_detail="upload_no_url",
            )
        return url

    async def _upload_stream(
        self, data: bytes, filename: str, *, mime: str = "video/mp4",
        folder: str = "videos/kie-agent",
    ) -> str:
        """参考视频走流式上传：超过 10MB 的 base64 会很大，而且官方推荐大文件走这条。"""
        async with httpx.AsyncClient(timeout=180.0) as client:
            try:
                resp = await client.post(
                    UPLOAD_STREAM_URL,
                    headers=self.headers(),
                    files={"file": (filename, data, mime)},
                    data={"uploadPath": folder, "fileName": filename},
                )
            except httpx.HTTPError as e:
                raise AdapterError(
                    network_error_message(e), log_detail=network_error_detail(e, UPLOAD_STREAM_URL)
                ) from e
        if resp.status_code != 200:
            raise AdapterError(
                f"上传参考视频失败（HTTP {resp.status_code}）",
                log_detail=f"upload_stream_status={resp.status_code}",
            )
        try:
            body = resp.json()
        except ValueError as e:
            raise AdapterError("上传参考视频的响应不是 JSON", log_detail="upload_stream_bad_json") from e
        url = str(((body.get("data") or {}).get("downloadUrl")) or "")
        if not url:
            raise AdapterError(
                f"上传参考视频失败：{str(body.get('msg') or body)[:160]}",
                log_detail="upload_stream_no_url",
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
        count = max(1, int(n))
        legacy = not is_new_image_family(model)
        urls: list[str] = []
        if ref_images:
            if not accepts_image_refs(model):
                raise AdapterError(
                    f"模型「{model}」不接受参考图。要参考图请换成同家族的图生图模型"
                    "（如 bytedance/seedream-v4-edit、seedream/5-flash-image-to-image、"
                    "gpt-image-2-image-to-image）。",
                    log_detail="kie_ref_needs_edit_model",
                )
            for i, data in enumerate(ref_images, 1):
                urls.append(await self._upload(data, f"ref-{i}.png"))

        # 老一代一次任务就能出 n 张（max_images）；新一代一次只出一张，要 n 张提交 n 次
        attempts = 1 if legacy else count
        urls_out: list[str] = []
        async with httpx.AsyncClient(timeout=60.0) as client:
            for _ in range(attempts):
                payload = {
                    "model": model,
                    "input": image_input_payload(
                        model, prompt, size, count if legacy else 1, urls
                    ),
                }
                created = await self._call(
                    client, "POST", "/api/v1/jobs/createTask", json=payload
                )
                task_id = str(created.get("taskId") or "")
                if not task_id:
                    raise AdapterError("KIE 没有返回任务 id", log_detail="kie_no_task_id")
                done = await self._poll(client, task_id)
                urls_out.extend(_result_urls(done))
                if len(urls_out) >= count:
                    break

        if not urls_out:
            raise AdapterError(
                "KIE 任务成功但没给出结果链接（可能是模型返回结构与预期不同）",
                log_detail="kie_no_result_urls",
            )
        return [await self._download(u) for u in urls_out[:count]]

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
        """提交视频任务：文生 / 首帧 / 首尾帧 / 多模态参考（图 + 视频 + 音频）。

        **先校验组合、再上传**：首尾帧与参考素材是两种互斥任务，Kling 4 关键帧
        又不收参考素材——这些都能在本地判断，没必要先传几 MB 再被上游拒。
        """
        ref_images = list(ref_images or [])
        ref_videos = list(ref_videos or [])

        # 用占位 URL 先过一遍校验（数量上限、互斥、缺首帧都在这一步拦下）
        video_input_payload(
            model, prompt, duration, ratio,
            "https://precheck/first" if first_frame else "",
            resolution,
            last_frame_url="https://precheck/last" if last_frame else "",
            ref_image_urls=["https://precheck/ref"] * len(ref_images),
            ref_video_urls=["https://precheck/video"] * len(ref_videos),
            ref_audio_urls=["https://precheck/audio"] if ref_audio else [],
        )

        image_url = ""
        last_url = ""
        ref_image_urls: list[str] = []
        ref_video_urls: list[str] = []
        ref_audio_urls: list[str] = []
        if first_frame is not None:
            image_url = await self._upload(first_frame, "first-frame.png")
        if last_frame is not None:
            last_url = await self._upload(last_frame, "last-frame.png")
        if ref_images:
            ref_image_urls = list(await asyncio.gather(*[
                self._upload(data, f"ref-{i}.png") for i, data in enumerate(ref_images)
            ]))
        if ref_videos:
            ref_video_urls = list(await asyncio.gather(*[
                self._upload_stream(data, f"ref-{i}.mp4") for i, data in enumerate(ref_videos)
            ]))
        if ref_audio is not None:
            mime, ext = audio_upload_mime(ref_audio)
            ref_audio_urls = [
                await self._upload(
                    ref_audio.data, f"ref-audio.{ext}", mime=mime, folder="audios/kie-agent"
                )
            ]

        payload = {
            "model": model,
            "input": video_input_payload(
                model, prompt, duration, ratio, image_url, resolution,
                last_frame_url=last_url,
                ref_image_urls=ref_image_urls,
                ref_video_urls=ref_video_urls,
                ref_audio_urls=ref_audio_urls,
            ),
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
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
