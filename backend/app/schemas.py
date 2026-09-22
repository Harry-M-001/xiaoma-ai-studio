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
    # 转场：cut（默认，不加滤镜、走原来的流拷贝）/ dissolve / fade / flash / wipe / circle
    transition: str = "cut"
    # 转场时长（秒）。不传用默认；超范围由服务端夹到边界
    transition_seconds: float | None = None
    # 转场音效：内置合成音效的 key（whoosh / riser / impact / tick）
    sfx: str = ""
    # 或者直接用资产库里的一条音频当音效；给了它就以它为准（内置 key 忽略）
    sfx_asset_id: int | None = None
    # ---- 字幕（v1.1.25）----
    # 版式 key。**空 = 没选过**，用默认版式；非空 = 用户选的，谁都不许改
    subtitle_style: str = ""
    # 字号缩放，1.0 为版式自带大小；超范围由服务端夹到边界
    subtitle_scale: float | None = None
    # 逐段一句字幕，按下标与 asset_ids 对齐；空串 / 缺位 = 这一段不出字幕。
    # 时间轴由**成片**里各段的区间算（`transitions.clip_spans`）——
    # 配了转场之后成片会变短，用「各段时长累加」会让字幕一段比一段提前。
    subtitles: list[str] = Field(default_factory=list)


class SubtitlePreviewIn(BaseModel):
    """字幕版式预览：真渲染一帧，让用户看到最终效果。"""

    style: str = ""
    scale: float | None = None
    # 留空则用内置示例文本（带繁体与生僻字，用来暴露缺字与字体回退）
    text: str = ""
    width: int = 1280
    height: int = 720


class RemovalBoxIn(BaseModel):
    """要处理的那一块字幕区域。坐标是**源视频像素**（前端按显示尺寸缩放换算）。

    四项都可省：服务端会把框夹进画面、并把宽高拉齐到偶数——用户拖到边界差一两像素
    不该报错。
    """

    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0


class RemovalFrameIn(BaseModel):
    """取一帧来框选（加 `t` 就是「换一帧看看」）。"""

    asset_id: int
    t: float = Field(default=0.0, ge=0)


class RemovalPreviewIn(BaseModel):
    """把去字幕真渲一帧出来看。"""

    asset_id: int
    t: float = Field(default=0.0, ge=0)
    box: RemovalBoxIn = RemovalBoxIn()
    # 空 = 没选过 → 用推荐手法（抹平）
    method: str = ""


class RemovalCheckIn(BaseModel):
    """只问「这个框上四种手法各能不能用」。

    单开一个接口是因为**拖动之后可选项会变**（框挪到画面中间时「裁掉」就不能用了），
    而算这个只用到几何、不碰 ffmpeg，值得在每次松手后立刻问一次：
    否则界面会显示「能选」，等用户点了才报错——「界面说行、后端说不行」最难解释。
    """

    asset_id: int
    box: RemovalBoxIn = RemovalBoxIn()


class RemovalApplyIn(BaseModel):
    """按这个框与手法处理整段视频，产物落资产库。"""

    asset_id: int
    box: RemovalBoxIn = RemovalBoxIn()
    method: str = ""


class UpscaleIn(BaseModel):
    """放大一张图或一段视频。

    `route` 是路线 key：`local:realesrgan` / `local:waifu2x` / `comfyui`
    （可选项与各自能用的模型、倍数由 `GET /api/upscale/options` 给，界面不写死）。

    `gpu` 是 `-1`（交给引擎自己挑）或具体设备号。**这个字段存在的理由**：
    实测同一张图换块卡差好几倍（waifu2x 集显比独显慢 22 倍），
    而「自动挑哪一块」是上游的行为、不是我们的——所以要把选择权交出来。

    `tta` 是引擎的 `-x` 开关（更慢但略好），界面上没有暴露，留着给以后；
    `workflow_id` / `param_values` 只有 `route=comfyui` 时用。
    """

    asset_id: int
    route: str
    model: str = ""
    scale: int = Field(default=2, ge=1, le=4)
    gpu: int = -1
    tta: bool = False
    workflow_id: int = 0
    param_values: dict = {}


class VideoGenerateIn(BaseModel):
    model_key: str
    prompt: str = Field(..., min_length=1)
    first_frame_asset_id: int | None = None
    # 对白音轨（数字人/口播）：一条音频资产的 id，随请求作为参考音频附发。
    # 只有支持参考音频的视频模型（如 Seedance 1.5 pro / 2.0 / 2.5）才认它。
    audio_ref_asset_id: int | None = None
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
