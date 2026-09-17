"""本地 Ollama 检测与一键接入。

为什么单独做这个：第一次打开应用的人，手上往往没有任何云 API Key，
而「要先去注册账号、充值、拿 Key」这一步就足以让人关掉页面。
Ollama 是「已经装好、只是没人告诉应用它在哪」的那类服务——
探一下 127.0.0.1:11434 就能把可用模型列出来，一键接进来，全程零成本。

两个细节：
- 检测结果**带 60 秒缓存**：引导横幅每次进页面都会问一次状态，
  不缓存就会把本机端口探测变成高频轮询。`force=True` 用于用户主动点「重新检测」。
- 只登记文本模型：Ollama 的 OpenAI 兼容端点主要服务对话模型，
  向量模型（名字带 embed 的）塞进来只会让模型下拉里多出一堆选了就报错的项。
"""

from __future__ import annotations

import logging
import time

import httpx

from app.providers.base import AdapterError
from app.schemas import ModelSpec

DEFAULT_HOST = "http://127.0.0.1:11434"
CACHE_TTL_SECONDS = 60
# 实测本机 `/api/tags` 在 0.6–2.6 秒之间波动（Ollama 要现读盘上的模型清单）。
# 第一版给了 1.5 秒，结果是**跑着的 Ollama 被偶尔判成「没装」**——
# 而「本机有 Ollama」正是第一次打开应用时最有用的一条信息，不能靠运气。
# 超时放宽到 4 秒没有代价：真没装的话连都连不上（立刻 RST），不会等到超时。
PROBE_TIMEOUT = 4.0

logger = logging.getLogger("xiaoma.ollama")

# (取到的时刻, host, 结果)
_cache: tuple[float, str, dict] | None = None


def _usable(name: str) -> bool:
    """向量模型与重排模型不能对话，不进模型清单。

    不筛的话，模型下拉里会混进 `*-reranker`、`*-embed` 这类项，
    用户选中之后只会在生成时报一个看不懂的错。
    """
    lowered = name.lower()
    return not any(bad in lowered for bad in ("embed", "rerank"))


async def detect(*, host: str = "", force: bool = False) -> dict:
    """探测本机 Ollama。返回 {running, baseUrl, models, error}，不抛异常。"""
    global _cache
    base = (host or DEFAULT_HOST).rstrip("/")
    if not force and _cache is not None:
        stamp, cached_host, cached = _cache
        if cached_host == base and time.monotonic() - stamp < CACHE_TTL_SECONDS:
            return cached

    result: dict = {"running": False, "baseUrl": base, "models": [], "error": ""}
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
            resp = await client.get(f"{base}/api/tags")
        if resp.status_code == 200:
            names = [
                str(m.get("name") or "").strip()
                for m in (resp.json().get("models") or [])
                if isinstance(m, dict)
            ]
            result["models"] = sorted({n for n in names if n and _usable(n)})
            result["running"] = True
        else:
            result["error"] = f"HTTP {resp.status_code}"
    except httpx.HTTPError as e:
        # 没装 / 没启动都是这一类，属正常情况，不值得记 error 级日志
        result["error"] = f"{type(e).__name__}"
        logger.debug("Ollama 探测未命中：%s", e)
    except ValueError as e:  # 200 但不是 JSON
        result["error"] = "返回内容不是 JSON"
        logger.debug("Ollama 响应无法解析：%s", e)

    _cache = (time.monotonic(), base, result)
    return result


def invalidate() -> None:
    """接入之后清缓存，界面上「已接入」的状态要立刻刷新。"""
    global _cache
    _cache = None


async def connect(db, *, host: str = "") -> dict:
    """把本机 Ollama 接成一个模型服务（重复点不会加出第二份）。

    Key 固定填 `ollama`：Ollama 的 OpenAI 兼容端点不校验凭据，
    但适配器要求 Key 非空（那是有意的——云服务少填 Key 是配置错误，
    不该等到发请求时才报错），所以这里给一个占位值。
    """
    from app.services import provider_store

    info = await detect(host=host, force=True)
    if not info["running"]:
        raise AdapterError(
            "没检测到本机 Ollama。请确认已经安装并启动（默认端口 11434），"
            "再回来点一次「重新检测」。"
        )
    models = [ModelSpec(name=n, modality="text", label=n) for n in info["models"]]
    if not models:
        raise AdapterError(
            "Ollama 在运行，但里面还没有任何对话模型。"
            "先在终端执行一次 `ollama pull qwen2.5:7b`（或你想要的模型）再回来接入。"
        )

    row = await provider_store.upsert_by_base_url(
        db,
        name="本地 Ollama",
        kind="openai",
        base_url=f"{info['baseUrl']}/v1",
        api_key="ollama",
        models=models,
        sort_order=8,
    )
    invalidate()
    return {"ok": True, "service": row, "models": [m.model_dump() for m in models]}

