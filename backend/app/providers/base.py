"""模型供应商适配器基类与统一数据结构。

设计目标：用户只需要填写 Base URL + API Key，
文本 / 图片走 OpenAI 兼容协议，视频走火山方舟风格的异步任务接口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

from app.config import settings


class AdapterError(Exception):
    """对上游接口错误的统一包装，消息可直接展示给用户。"""


@dataclass
class VideoStatus:
    status: str  # processing | succeeded | failed
    video_url: str | None = None
    progress: int = 0
    error: str | None = None


def normalize_base_url(url: str) -> str:
    """去掉末尾斜杠与用户误填的完整端点后缀，只保留 API 根路径。"""
    base = (url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/images/generations", "/contents/generations/tasks"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base.rstrip("/")


def request_timeout() -> float:
    """请求超时取自配置表（可在系统设置中调整），未加载时回落环境变量。"""
    from app.services.config_center_service import runtime_value

    try:
        return float(runtime_value("limits.request_timeout", settings.REQUEST_TIMEOUT))
    except (TypeError, ValueError):
        return float(settings.REQUEST_TIMEOUT)


class BaseAdapter(ABC):
    """所有供应商适配器的统一接口。"""

    kind: str = "base"

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = normalize_base_url(base_url)
        self.api_key = api_key or ""
        self._client: httpx.AsyncClient | None = None

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(request_timeout(), connect=20.0),
                follow_redirects=True,
            )
        return self._client

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ---- 连接测试 ----

    @abstractmethod
    async def test_connection(self, model: str | None = None) -> None:
        """验证 Base URL / Key 是否可用；失败抛 AdapterError。"""

    # ---- 文本 ----

    @abstractmethod
    def chat_stream(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """流式对话，逐段 yield 文本增量。必须是 async generator。

        max_tokens 为 None 表示交给上游默认值（长文分块生成时会显式限制单次长度）。
        """

    # ---- 图片 ----

    @abstractmethod
    async def generate_image(
        self,
        *,
        model: str,
        prompt: str,
        n: int = 1,
        size: str = "1024x1024",
        ref_images: list[bytes] | None = None,
    ) -> list[bytes]:
        """文生图 / 参考图生图，返回各图片的二进制内容。"""

    # ---- 视频 ----

    @abstractmethod
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
        """提交异步视频任务，返回上游任务 ID。

        - first_frame / last_frame：首尾帧模式
        - ref_images / ref_videos：全能参考模式（多模态参考）
        不支持的实现应给出可读错误（raise AdapterError）。
        """

    @abstractmethod
    async def poll_video(self, remote_id: str) -> VideoStatus:
        """查询视频任务状态。"""


def unsupported(modality: str) -> AdapterError:
    return AdapterError(f"该类型的模型服务不支持{modality}能力，请检查模型服务类型与模型配置")
