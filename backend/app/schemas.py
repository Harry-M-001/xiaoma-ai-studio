"""Pydantic API 模型。

注意：能力类型（modality）与协议类型（kind）**不用枚举写死**——
能力由 `modalities` 表驱动，协议由 `ADAPTERS` 注册表驱动，
参数档位（尺寸 / 时长 / 比例 / 清晰度）由 `param_options` 表驱动。
新增能力、协议或档位都不需要改动本文件。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, PlainSerializer

from app.clock import utc_iso

# 运行期字符串，具体取值范围来自配置表 / 注册表
Modality = str
ProviderKind = str

# 所有返回给前端的时间字段都用这个类型：库里存的是不带时区标记的 UTC，
# 出口统一标上 `+00:00`，由浏览器换算成本地时间（细节见 app.clock）。
UtcDateTime = Annotated[
    datetime, PlainSerializer(utc_iso, return_type=str, when_used="json")
]


# ---------- 模型服务 ----------

class ModelSpec(BaseModel):
    name: str = Field(..., description="模型名，如 gpt-4o / doubao-seedream-4-0-250828")
    modality: Modality
    label: str = ""


class ProviderIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    kind: ProviderKind = "openai"
    base_url: str = Field(..., min_length=1)
    api_key: str | None = Field(None, description="新增/修改时填写；留空表示不改动已有 Key")
    enabled: bool = True
    sort_order: int = 0
    models: list[ModelSpec] = []


class ProviderOut(BaseModel):
    id: int
    name: str
    kind: ProviderKind
    base_url: str
    enabled: bool
    sort_order: int
    models: list[ModelSpec]
    has_api_key: bool
    created_at: UtcDateTime
    updated_at: UtcDateTime


class ProviderTestIn(BaseModel):
    kind: ProviderKind = "openai"
    base_url: str
    api_key: str | None = None  # 为空时使用已保存的 Key
    service_id: int | None = None
    model: str | None = None


class QuickSetupIn(BaseModel):
    """粘贴一个 Key 就接入。"""

    api_key: str = Field(..., min_length=1, description="用户粘贴的 API Key")
    provider_key: str = Field("", description="指定服务商预设的 key；留空表示自动识别")
    force: bool = Field(False, description="连通测试没过也照样保存（给测试方式不适配的服务商留的出口）")


class ModelOption(BaseModel):
    key: str  # "service_id:model_name"，前端选择后原样回传
    service_id: int
    service_name: str
    name: str
    label: str
    modality: Modality


# ---------- 对话 ----------

class ChatMessageIn(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class ChatRequest(BaseModel):
    model_key: str
    messages: list[ChatMessageIn]
    session_id: int | None = None
    temperature: float = 0.7


class ChatSessionOut(BaseModel):
    id: int
    title: str
    created_at: UtcDateTime
    updated_at: UtcDateTime


class ChatMessageOut(BaseModel):
    id: int
    role: str
    content: str
    model: str = ""
    created_at: UtcDateTime


# ---------- 生成任务 ----------

class ImageGenerateIn(BaseModel):
    model_key: str
    prompt: str = Field(..., min_length=1)
    size: str = "1024x1024"
    n: int = Field(1, ge=1, le=16)
    ref_asset_ids: list[int] = []


class ImageBatchGenerateIn(BaseModel):
    """批量生图：每行提示词 × 每个选中模型 = 一个任务。"""

    prompts: list[str] = Field(..., min_length=1)
    model_keys: list[str] = Field(..., min_length=1)
    size: str = "1024x1024"
    n: int = Field(1, ge=1, le=16)
    ref_asset_ids: list[int] = []


# ---------- 生成前软校验 ----------
#
# 预检的输入**故意不做硬校验**：字段全给默认值，不设 min_length / 范围。
# 理由：预检的职责是「照实告警」，而不是「挡住你」——挡人的活在正式生成接口那边。
# 如果这里也 422，用户点一次生成会先收到一个格式错误，而不是一句「首帧会被裁掉」。


class ImagePreflightIn(BaseModel):
    model_key: str = ""
    prompt: str = ""
    n: int = 1
    ref_asset_ids: list[int] = []


class ImageBatchPreflightIn(BaseModel):
    prompts: list[str] = []
    model_keys: list[str] = []
    n: int = 1


class VideoPreflightIn(BaseModel):
    model_key: str = ""
    prompt: str = ""
    first_frame_asset_id: int | None = None
    ratio: str = ""
    resolution: str = ""


class PreflightWarningOut(BaseModel):
    code: str
    level: str = "warn"
    message: str
    suggestion: str = ""


class PreflightOut(BaseModel):
    warnings: list[PreflightWarningOut] = []
    # 恒为 False。留着是为了让前端不必去猜「这类接口会不会挡我」——
    # 将来真要做硬校验，也应该新增 blocking=true 而另开接口，而不是改动这里的语义。
    blocking: bool = False


# ---- 导演台 ----


class DirectorProbeIn(BaseModel):
    asset_id: int


class DirectorExtractIn(BaseModel):
    asset_id: int
    start: float = Field(..., ge=0)
    end: float = Field(..., gt=0)


class DirectorThumbnailIn(BaseModel):
    asset_id: int
    t: float = Field(..., ge=0)


class DirectorMergeIn(BaseModel):
    asset_ids: list[int] = Field(..., min_length=2)


class VideoGenerateIn(BaseModel):
    model_key: str
    prompt: str = Field(..., min_length=1)
    first_frame_asset_id: int | None = None
    # 以下值的可选范围由 param_options 表决定，接口层不写死
    duration: int = 5
    ratio: str = "16:9"
    resolution: str = "720p"


class AssetBrief(BaseModel):
    id: int
    kind: str
    url: str
    width: int | None = None
    height: int | None = None


class TaskOut(BaseModel):
    id: int
    kind: str
    status: str
    service_id: int | None
    model: str
    prompt: str
    params: dict = {}
    error: str | None = None
    progress: int = 0
    assets: list[AssetBrief] = []
    created_at: UtcDateTime
    completed_at: UtcDateTime | None = None
    # 重跑出来的任务记着原任务 id，便于在任务中心区分首次与重跑
    retry_of_task_id: int | None = None


class TaskRetryIn(BaseModel):
    """重新生成时的参数覆盖。

    这几个字段**不传就完整沿用原任务的参数快照**（型号 / 提示词 / params_json
    一个都不丢）。只想调其中一两项时才传——这比让用户把参数重填一遍可靠得多，
    也是原参数快照真正的用处。
    """

    prompt: str | None = None
    n: int | None = None
    size: str | None = None
    duration: int | None = None
    ratio: str | None = None
    resolution: str | None = None


# ---------- 资产 ----------

class AssetOut(BaseModel):
    id: int
    kind: str
    url: str
    original_name: str
    content_type: str
    size: int
    source: str
    # 资产链身份：资产名（角色/场景/道具名）与类型；非资产链产物为空串
    name: str = ""
    category: str = ""
    prompt: str | None = None
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    task_id: int | None = None
    created_at: UtcDateTime


def asset_to_out(a: Any) -> AssetOut:
    """把 `Asset` 行转成对外的形状。

    放在这里（而不是某个路由里）是因为它现在有两个调用方：生成/资产库接口，以及配音接口。
    复制一份必然漂移——新增一个字段就会有一处漏掉，而漏掉的表现是「某个页面看不到时长」
    这种查起来最费劲的一类问题。
    """
    return AssetOut(
        id=a.id,
        kind=a.kind,
        url=f"/media/{a.filename}",
        original_name=a.original_name,
        content_type=a.content_type,
        size=a.size,
        source=a.source,
        name=a.name or "",
        category=a.category or "",
        prompt=a.prompt,
        width=a.width,
        height=a.height,
        duration=a.duration,
        task_id=a.task_id,
        created_at=a.created_at,
    )


class AssetList(BaseModel):
    items: list[AssetOut]
    total: int


# ---------- 系统 ----------

class LoginIn(BaseModel):
    password: str


class LoginOut(BaseModel):
    token: str


class AppInfo(BaseModel):
    name: str
    version: str
    auth_required: bool
