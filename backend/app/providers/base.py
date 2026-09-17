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
    """对上游接口错误的统一包装，消息可直接展示给用户。

    `message` 是给**用户**看的，为了让他能自己排查，会把上游返回的错误正文带上。
    但这份正文不能进日志：上游收到 400 时经常把整个请求体回显回来，而请求体里
    就是用户的提示词（真实踩过的坑，见 `docs/` 与 MEMORY 的记录）。

    所以另给一个 `log_detail`：只有枚举化的安全信息（HTTP 状态、上游错误类型、
    响应体大小）。日志侧优先用它，并且不再打印完整堆栈。
    没给 `log_detail` 的异常仍会打完整堆栈，属于「这段代码还没改造到」。
    """

    def __init__(self, message: str, *, log_detail: str = "") -> None:
        super().__init__(message)
        self.log_detail = log_detail


def summarize_upstream_error(resp: httpx.Response) -> tuple[str, str]:
    """把一次失败的上游响应拆成 (给用户看的详细消息, 只用于日志的安全摘要)。

    安全摘要里没有任何上游正文，只有状态码、枚举类型与响应体字节数——这三样足够
    做聚类（是鉴权挂了还是参数不对、是空响应还是一大段报错），又不带用户内容。
    """
    status = resp.status_code
    hint = ""
    if status == 400:
        hint = "（上游拒绝了这次请求：多半是模型名写错，或该模型不认这组参数）"
    elif status == 401:
        hint = "（API Key 校验失败：确认 Key 复制完整、且与这个服务商的地址配套）"
    elif status == 403:
        hint = "（没有权限：这个 Key 可能没开通该模型，或账号被限制）"
    elif status == 404:
        hint = "（地址或模型不存在：检查 Base URL 是否填到 /v1 这一级，模型名是否写对）"
    elif status == 413:
        hint = "（请求体过大：提示词或参考图超出了上游限制）"
    elif status == 429:
        hint = "（触发限流或额度不足：稍后重试，或到服务商控制台看余额与配额）"
    elif status in (500, 502, 503, 504):
        hint = "（上游服务异常：通常是服务商侧的问题，稍后重试；持续失败可先换一个模型）"
    elif status >= 500:
        hint = "（上游服务异常：稍后重试）"

    err_type = ""
    err_code = ""
    text = ""
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = None

    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            err_type = str(err.get("type") or "")[:64]
            err_code = str(err.get("code") or "")[:64]
            text = str(err.get("message") or err)[:300]
        elif err is not None:
            text = str(err)[:300]
        else:
            text = str(data)[:300]
    else:
        try:
            text = resp.text[:300]
        except Exception:  # noqa: BLE001
            text = ""

    try:
        body_bytes = len(resp.content)
    except Exception:  # noqa: BLE001
        body_bytes = 0

    if text:
        message = f"HTTP {status} {hint}：{text}"
    else:
        # 上游返回空体时别留一个孤零零的冒号（`HTTP 502 ：`），看着像 bug
        message = f"HTTP {status} {hint}".strip()
        message = f"{message}（响应体为空）"
    log_detail = (
        f"HTTP {status} type={err_type or '-'} code={err_code or '-'} body={body_bytes}B"
    )
    return message, log_detail


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


def url_host(url: str) -> str:
    """只取 URL 的 host。日志里要从「打到哪个主机」判断问题，又不该带上 query。"""
    try:
        from urllib.parse import urlsplit

        return urlsplit(url or "").hostname or ""
    except Exception:  # noqa: BLE001
        return ""


def network_error_detail(exc: Exception, url: str = "") -> str:
    """网络类异常的安全日志摘要。

    httpx 的异常字符串里带完整 URL——中转站常把 key 放在 query 上，用户也可能填了
    内网网关地址。日志只要「哪种错 + 打到哪个主机」，所以这里把 host 单独取出来。
    """
    return f"{type(exc).__name__} host={url_host(url) or '-'}"


def network_error_message(exc: Exception) -> str:
    """给「连不上」配一句能照做的下一步。

    只说「网络请求失败」等于没说：用户能做的判断只有「地址写错了 / 服务没启动 / 网络不通」，
    而这三件事的排查动作完全不同。这里按异常类型给一句对应的动作，异常类型名保留在尾部
    （方便他拿这个词去搜），URL 之类的细节留给日志。
    """
    name = type(exc).__name__
    # 先判 ConnectTimeout：它同时是 TimeoutException 与连接类错误，
    # 顺序写反会把「超时」说成「连不上」，两句建议的排查方向恰好相反
    if isinstance(exc, httpx.ConnectTimeout):
        hint = "连接超时：地址可能不可达（本机服务确认已启动，云服务确认网络或代理可用）"
    elif isinstance(exc, httpx.TimeoutException):
        hint = "请求超时：服务没在超时时间内响应，稍后重试"
    elif isinstance(exc, httpx.ConnectError):
        hint = "连不上这个地址：确认地址没写错、服务已启动（本机服务看端口有没有在听）"
    elif isinstance(exc, httpx.ProxyError):
        hint = "代理出错：检查系统代理设置，或把该地址排除在代理之外"
    elif isinstance(exc, httpx.HTTPError):
        hint = "网络层出错，稍后重试"
    else:
        return f"请求失败（{name}）"
    return f"{hint}（{name}）"


def unsupported(modality: str) -> AdapterError:
    return AdapterError(
        f"该类型的模型服务不支持{modality}能力，请检查模型服务类型与模型配置",
        log_detail=f"unsupported modality={modality}",
    )
