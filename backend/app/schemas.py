"""Pydantic API 模型。

注意：能力类型（modality）与协议类型（kind）**不用枚举写死**——
能力由 `modalities` 表驱动，协议由 `ADAPTERS` 注册表驱动，
参数档位（尺寸 / 时长 / 比例 / 清晰度）由 `param_options` 表驱动。
新增能力、协议或档位都不需要改动本文件。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# 运行期字符串，具体取值范围来自配置表 / 注册表
Modality = str
ProviderKind = str


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
    created_at: datetime
    updated_at: datetime


class ProviderTestIn(BaseModel):
    kind: ProviderKind = "openai"
    base_url: str
    api_key: str | None = None  # 为空时使用已保存的 Key
    service_id: int | None = None
    model: str | None = None


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
    created_at: datetime
    updated_at: datetime


class ChatMessageOut(BaseModel):
    id: int
    role: str
    content: str
    model: str = ""
    created_at: datetime


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
    created_at: datetime
    completed_at: datetime | None = None


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
    created_at: datetime


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
