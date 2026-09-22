"""模型协议适配器注册表。

新增一种协议（例如 Google Gemini、阿里 Wan）= 新建一个适配器文件
（实现 BaseAdapter）+ 在文件末尾 `register(...)` 一行，其余代码零改动。

前端通过 `GET /api/meta/provider-kinds` 读取可选协议，界面上直接出现新选项。
"""

from __future__ import annotations

from typing import Callable

from app.providers.ark import ArkAdapter
from app.providers.base import AdapterError, BaseAdapter
from app.providers.comfyui import ComfyUIAdapter
from app.providers.dashscope import DashScopeAdapter
from app.providers.local_tts import LocalTTSAdapter
from app.providers.openai_compat import OpenAICompatAdapter

AdapterFactory = Callable[[str, str], BaseAdapter]

_FACTORIES: dict[str, AdapterFactory] = {}
_LABELS: dict[str, str] = {}
_HINTS: dict[str, str] = {}


def register(
    kind: str, factory: AdapterFactory, label: str = "", hint: str = ""
) -> None:
    """注册一种协议。kind 是入库的值，label/hint 供界面展示。"""
    _FACTORIES[kind] = factory
    _LABELS[kind] = label or kind
    if hint:
        _HINTS[kind] = hint


def has_kind(kind: str) -> bool:
    return (kind or "").strip() in _FACTORIES


def build_adapter(kind: str, base_url: str, api_key: str) -> BaseAdapter:
    """按协议名装配适配器；未注册的协议给出可读错误。"""
    factory = _FACTORIES.get((kind or "").strip())
    if factory is None:
        available = "、".join(_LABELS.values()) or "（无）"
        raise AdapterError(f"未知的模型服务类型「{kind}」，当前支持：{available}")
    return factory(base_url, api_key)


def list_kinds() -> list[dict[str, str]]:
    return [
        {"kind": k, "label": _LABELS.get(k, k), "hint": _HINTS.get(k, "")}
        for k in _FACTORIES
    ]


# ---------- 内置协议注册 ----------

register(
    "openai",
    lambda base_url, api_key: OpenAICompatAdapter(base_url, api_key),
    "OpenAI 兼容接口",
    "需包含 /v1 路径；兼容 OpenAI 协议的中转 / 网关同样可用。",
)
register(
    "ark",
    lambda base_url, api_key: ArkAdapter(base_url, api_key),
    "火山方舟 Ark",
    "火山方舟使用统一接入地址，模型名填写具体模型版本 ID。",
)
register(
    "dashscope",
    lambda base_url, api_key: DashScopeAdapter(base_url, api_key),
    "阿里百炼 DashScope",
    "可灵 Kling v3 系列 / 通义万相 wan2.7 系列在此接入；模型名如 kling/kling-v3-omni-video-generation、wan2.7-i2v-2026-04-25。",
)
register(
    "comfyui",
    lambda base_url, api_key: ComfyUIAdapter(base_url, api_key),
    "ComfyUI 本地服务",
    "本地 ComfyUI 地址（默认 http://127.0.0.1:8188），无需 API Key；画布中上传 workflow_api.json 工作流即可执行。",
)
register(
    "local_tts",
    lambda base_url, api_key: LocalTTSAdapter(base_url, api_key),
    "本机配音（sherpa-onnx）",
    "跑本机装好的 sherpa-onnx，不联网、不花钱。到「本机引擎」页下载引擎后一键接入；"
    "音色用模型自带的那一套（可填音色名或音色号）。",
)
