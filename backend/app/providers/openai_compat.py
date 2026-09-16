"""OpenAI 兼容协议适配器。

适用于所有兼容 OpenAI 接口的服务：OpenAI、DeepSeek、Moonshot、通义千问、
智谱、OpenRouter 以及各类中转站。覆盖：
- 文本：POST {base}/chat/completions（SSE 流式）
- 图片：POST {base}/images/generations（同步，b64/url 均可解析）
"""

from __future__ import annotations

import base64
import json
from typing import Any, AsyncIterator

import httpx

from app.providers.base import (
    AdapterError,
    BaseAdapter,
    VideoStatus,
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
            f"网络请求失败：{e}", log_detail=network_error_detail(e, url)
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
                f"网络请求失败：{e}", log_detail=network_error_detail(e, url)
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
                f"网络请求失败：{e}",
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
