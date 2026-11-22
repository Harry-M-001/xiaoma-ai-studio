"""OpenAI 兼容协议适配器。

适用于所有兼容 OpenAI 接口的服务：OpenAI、DeepSeek、Moonshot、通义千问、
智谱、OpenRouter 以及各类中转站。覆盖：
- 文本：POST {base}/chat/completions（SSE 流式）
- 图片：POST {base}/images/generations（同步，b64/url 均可解析）
- 语音：POST {base}/audio/speech（同步，直接返回音频二进制）
"""

from __future__ import annotations

import base64
import json
from typing import Any, AsyncIterator

import httpx

from app.providers.base import (
    AdapterError,
    BaseAdapter,
    SpeechResult,
    VideoStatus,
    network_error_message,
    network_error_detail,
    summarize_upstream_error,
    unsupported,
    url_host,
)


async def iter_chat_sse(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> AsyncIterator[str]:
    """通用 OpenAI /chat/completions SSE 解析，逐段 yield 文本增量。"""
    try:
        resp = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as e:
        raise AdapterError(
            network_error_message(e), log_detail=network_error_detail(e, url)
        ) from e

    if resp.status_code != 200:
        raise_upstream_error(resp)

    async for line in resp.aiter_lines():
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        piece = delta.get("content")
        if piece:
            yield piece


def _extract_error(resp: httpx.Response) -> str:
    """从上游响应里提取可读的错误信息（给用户看的那一份）。"""
    return summarize_upstream_error(resp)[0]


def raise_upstream_error(resp: httpx.Response) -> None:
    """抛出带安全日志摘要的上游错误。

    统一走这里而不是 `raise AdapterError(_extract_error(resp))`，是为了保证
    每条上游错误都带 `log_detail`——否则日志会退回打印完整堆栈，
    而堆栈里的异常消息带着上游正文（可能含用户提示词）。
    """
    message, log_detail = summarize_upstream_error(resp)
    raise AdapterError(message, log_detail=log_detail)


_AUDIO_CT_FALLBACK = "audio/mpeg"


def _audio_content_type(resp: httpx.Response) -> str:
    """从响应头取音频类型；**只认 audio/* 这一族**，其余一律按 mp3 处理。

    有的服务在这一栏回 `application/octet-stream`（甚至 `binary/octet-stream`）。
    那不是音频身份：拿它去存文件只会得到 `.bin`，`/media/` 于是给它
    `application/octet-stream`，`<audio>` 直接不认、进度条也拖不动，而后端一声不吭。
    与其一个个去列「哪些不算」，不如反过来只认「哪些算」——按最通用的 mp3 存，
    至少能播。
    """
    raw = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    return raw if raw.startswith("audio/") else _AUDIO_CT_FALLBACK


class OpenAICompatAdapter(BaseAdapter):
    kind = "openai"

    async def test_connection(self, model: str | None = None) -> None:
        if not self.base_url:
            raise AdapterError("Base URL 为空", log_detail="config_invalid base_url_empty")
        client = await self.client()
        url = f"{self.base_url}/models"
        try:
            resp = await client.get(url, headers=self.headers())
        except httpx.HTTPError as e:
            raise AdapterError(
                network_error_message(e), log_detail=network_error_detail(e, url)
            ) from e
        if resp.status_code == 200:
            return
        if resp.status_code in (401, 403):
            raise AdapterError(
                f"API Key 校验失败：HTTP {resp.status_code}",
                log_detail=f"HTTP {resp.status_code} auth_failed host={url_host(url)}",
            )
        # 部分中转站不开放 /models，用一次极简对话验证
        probe_model = model or "gpt-4o-mini"
        async for _ in iter_chat_sse(
            client,
            f"{self.base_url}/chat/completions",
            {**self.headers(), **{"Content-Type": "application/json"}},
            {
                "model": probe_model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "stream": True,
            },
        ):
            break
        return

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
            {**self.headers(), **{"Content-Type": "application/json"}},
            body,
        ):
            yield piece

    async def chat_once(
        self,
        model: str,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        thinking: bool | None = None,
    ) -> dict:
        """非流式 + 工具调用。

        为什么要单独一条而不是复用 `chat_stream`：流式响应里的 `tool_calls` 是按
        **下标增量**拼的（`index` + 分片的 `arguments` 字符串），各家还各有偏差；
        Agent 每步只等一次完整响应，非流式拿到的就是完整的 `message.tool_calls`，
        拼装这件事根本不存在。

        `thinking` 只对认这个参数的端点下发（见 `_thinking_supported`）：
        DeepSeek 的 Chat Completions 用 `{"thinking": {"type": "enabled"|"disabled"}}`，
        且**默认就是 enabled**——所以「关掉思考」必须显式下发 disabled，不能省。
        思维链在响应的 `message.reasoning_content` 里，与 content 同级。
        """
        if not self.base_url or not self.api_key:
            raise AdapterError(
                "该服务尚未填写 Base URL 或 API Key", log_detail="config_invalid missing_credentials"
            )
        client = await self.client()
        url = f"{self.base_url}/chat/completions"
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        if thinking is not None and self.supports_thinking(model):
            body["thinking"] = {"type": "enabled" if thinking else "disabled"}

        try:
            resp = await client.post(url, headers={**self.headers(),
                                                   "Content-Type": "application/json"}, json=body)
        except httpx.HTTPError as e:
            raise AdapterError(
                network_error_message(e), log_detail=network_error_detail(e, url)
            ) from e
        if resp.status_code != 200:
            raise_upstream_error(resp)

        try:
            data = resp.json()
        except ValueError as e:
            raise AdapterError(
                "上游返回的不是可解析的对话结果", log_detail=f"bad_json host={url_host(url)}"
            ) from e
        # **HTTP 200 里的业务错误**：KIE 这类聚合网关会把 401/400/429 包在 body 的
        # `{"code":400,"msg":"…"}` 里返回，只看状态码会把它当成一次「模型没说话」的
        # 成功响应——表现是 Agent 静默结束、界面上一句回答都没有。
        code = data.get("code") if isinstance(data, dict) else None
        if code is not None and int(code) not in (0, 200):
            raise AdapterError(
                f"上游返回错误（{code}）：{str(data.get('msg') or '')[:200]}",
                log_detail=f"upstream_code={code} host={url_host(url)}",
            )
        try:
            message = (data.get("choices") or [{}])[0].get("message") or {}
        except (AttributeError, IndexError) as e:
            raise AdapterError(
                "上游返回的不是可解析的对话结果", log_detail=f"bad_shape host={url_host(url)}"
            ) from e

        calls: list[dict[str, Any]] = []
        for i, tc in enumerate(message.get("tool_calls") or []):
            fn = (tc or {}).get("function") or {}
            name = str(fn.get("name") or "").strip()
            if not name:
                continue
            args = fn.get("arguments")
            if not isinstance(args, str):
                # 少数服务直接给对象；统一成字符串，后面 parse 才是同一条路
                args = json.dumps(args if args is not None else {}, ensure_ascii=False)
            calls.append({
                "id": str((tc or {}).get("id") or f"call_{i}"),
                "name": name,
                "arguments": args,
            })

        return {
            "content": message.get("content") or "",
            "tool_calls": calls,
            # 思维链：DeepSeek 用 reasoning_content；少数兼容层叫 reasoning（字符串）
            "reasoning": str(message.get("reasoning_content") or message.get("reasoning") or ""),
            "finish_reason": (data.get("choices") or [{}])[0].get("finish_reason") or "",
            "usage": data.get("usage") or {},
        }

    def supports_thinking(self, model: str) -> bool:
        """这个端点认不认 `thinking` 参数（纯判据）。

        只认 DeepSeek：`thinking` 是它家的入参形状，别家收到不认识的字段多半直接 400，
        而「猜错了就整轮失败」比「这次不思考」糟得多。所以判不出来就不下发，
        界面上也不把开关做成可用（`provider_store.supports_thinking` 用的是同一判据）。
        """
        return "deepseek" in (self.base_url or "").lower() or model.lower().startswith("deepseek")

    async def generate_image(
        self,
        *,
        model: str,
        prompt: str,
        n: int = 1,
        size: str = "1024x1024",
        ref_images: list[bytes] | None = None,
    ) -> list[bytes]:
        if ref_images:
            raise AdapterError(
                "OpenAI 兼容类型暂不支持参考图；参考图/改图请使用火山方舟类型的 Seedream 模型"
            )
        if not self.base_url or not self.api_key:
            raise AdapterError("该服务尚未填写 Base URL 或 API Key", log_detail="config_invalid missing_credentials")
        client = await self.client()
        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "n": n,
            "response_format": "b64_json",
        }
        try:
            resp = await client.post(
                f"{self.base_url}/images/generations",
                headers={**self.headers(), "Content-Type": "application/json"},
                json=body,
            )
        except httpx.HTTPError as e:
            raise AdapterError(
                network_error_message(e),
                log_detail=network_error_detail(e, f"{self.base_url}/images/generations"),
            ) from e
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
                    raise AdapterError(
                        f"下载结果图片失败：HTTP {dl.status_code}",
                        log_detail=f"download HTTP {dl.status_code}",
                    )
                results.append(dl.content)
        if not results:
            raise AdapterError(
                f"上游响应中没有图片：{str(data)[:200]}",
                log_detail=f"empty_result status=200 body={len(str(data))}B",
            )
        return results

    async def submit_video(self, **kwargs) -> str:  # type: ignore[override]
        raise unsupported("视频")

    async def poll_video(self, remote_id: str) -> VideoStatus:
        raise unsupported("视频")

    async def synthesize_speech(
        self,
        *,
        model: str,
        text: str,
        voice: str = "",
        speed: float = 1.0,
    ) -> SpeechResult:
        """POST {base}/audio/speech（与 OpenAI、硅基流动、各家中转站同一套口径）。

        两个刻意的取舍：

        - **请求 mp3**：兼容面最广；有的服务只认 `mp3` / `opus` 这几个名字，
          写 `wav` 反而容易 400。真拿到别的类型也不用慌，跟着响应头存。
        - **voice 留空就不带这个字段**：让服务端用它自己的默认音色。硬塞一个
          `alloy` 进去只会让不认这个名字的服务直接报错。
        """
        if not self.base_url or not self.api_key:
            raise AdapterError(
                "该服务尚未填写 Base URL 或 API Key",
                log_detail="config_invalid missing_credentials",
            )
        client = await self.client()
        body: dict[str, Any] = {"model": model, "input": text, "response_format": "mp3"}
        if voice:
            body["voice"] = voice
        # 语速用默认值时不带这个字段：不是所有兼容服务都认它，带了就是替用户冒险
        if abs(float(speed) - 1.0) > 1e-6:
            body["speed"] = round(float(speed), 2)
        url = f"{self.base_url}/audio/speech"
        try:
            resp = await client.post(
                url, headers={**self.headers(), "Content-Type": "application/json"}, json=body
            )
        except httpx.HTTPError as e:
            raise AdapterError(
                network_error_message(e), log_detail=network_error_detail(e, url)
            ) from e
        if resp.status_code != 200:
            raise_upstream_error(resp)
        audio = resp.content
        if not audio:
            raise AdapterError(
                "上游返回了空音频，请重试或换一个音色",
                log_detail="empty_result status=200 body=0B",
            )
        return SpeechResult(audio=audio, content_type=_audio_content_type(resp))
