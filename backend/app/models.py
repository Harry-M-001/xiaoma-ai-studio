"""SQLAlchemy ORM 模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ProviderService(Base):
    """用户自行配置的模型服务（BYOK：Base URL + API Key）。"""

    __tablename__ = "provider_services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # openai = OpenAI 兼容协议；ark = 火山引擎方舟（含异步视频任务）
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="openai")
    base_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    api_key_enc: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # [{"name": "gpt-4o", "modality": "text|image|video", "label": "展示名"}]
    models_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class ChatSession(Base):
    """对话会话。"""

    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="新对话")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class ChatMessage(Base):
    """对话消息。"""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user/assistant
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Task(Base):
    """生成任务（图片 / 视频）。"""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # image|video|text|workflow
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    service_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    model: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    remote_job_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 画布节点执行时记录归属，便于画布查询节点状态（普通任务为空）
    canvas_project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    canvas_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 「重新生成」出来的任务记下原任务 id，便于在任务中心区分是重跑还是首次
    retry_of_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 「一次运行」的标识：整图运行 / 单节点运行各生成一个，把这一跑派出的任务串起来，
    # 事后才能算出「这一次实际派了多少、成了几条」（见 canvas_runner.run_summary）
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Asset(Base):
    """资产（生成或上传的图片 / 视频），文件保存在本地数据目录。"""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # image|video|document
    # 相对存储根目录的路径，如 2026-09/xxxx.png
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    original_name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    content_type: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="generated")
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 资产链身份：资产名（如「暮光闪闪」）与类型（角色/场景/道具）。
    # 有了稳定业务名，下游提示词提到即可自动挂参考图（见 canvas_runner._inject_asset_refs）。
    name: Mapped[str] = mapped_column(String(80), nullable=False, default="", index=True)
    category: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


# ============================================================
# 配置域：以下表由「schema 注册制」统一管理，
# 新增一行 = 新增一个可配置项，核心框架零改动。
# 注册信息见 app/registry/schema_registry.py
# ============================================================


class ConfigItem(Base):
    """系统配置项（KV 行式）。设置页可直接读改，改完即生效。"""

    __tablename__ = "config_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    group_name: Mapped[str] = mapped_column(String(50), nullable=False, default="basic")
    label: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # string | int | float | bool | json
    value_type: Mapped[str] = mapped_column(String(20), nullable=False, default="string")
    value_json: Mapped[str] = mapped_column(Text, nullable=False, default='""')
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class ParamOption(Base):
    """创作参数可选项（图片尺寸 / 视频时长 / 比例 / 清晰度 / 数量…）。

    新增一个参数档位 = 插一行，前后端自动生效，无需发版。
    """

    __tablename__ = "param_options"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    value: Mapped[str] = mapped_column(String(100), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    # 附加信息：比例、宽高、说明等
    meta_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Modality(Base):
    """模型能力类型（文本 / 图片 / 视频 / …）。取代写死的枚举。"""

    __tablename__ = "modalities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(30), nullable=False, unique=True, index=True)
    label: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    icon: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class NavItem(Base):
    """导航项。加模块 / 隐藏模块 = 插一行或改 enabled，界面无需改代码。"""

    __tablename__ = "nav_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    label: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    icon: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    route: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    group_name: Mapped[str] = mapped_column(String(50), nullable=False, default="main")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 预留：标记为需要登录后才可见（社区/发布类）
    requires_auth: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Prompt(Base):
    """提示词模板。内容中的 {占位符} 由使用者替换；key 用于导入导出与分享。"""

    __tablename__ = "prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # 适用场景：text（对话）/ image（图片）/ video（视频）
    modality: Mapped[str] = mapped_column(String(30), nullable=False, default="image", index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 逗号分隔，如：人像,摄影,电影感
    tags: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class AgentPrompt(Base):
    """创作 Agent 提示词（自动链：每个阶段一个 Agent）。

    自动链的思路是「内容层全是 Markdown 文本，只让 LLM 写正文」，
    所以每个阶段只需要一份系统提示词 + 用户侧拼装模板：
    - system_prompt：角色设定与硬性规则（输出必须遵守）
    - user_template：用户消息模板，占位符 {content} = 上游全文，{params} = 节点参数
    - var_hint：告诉用户这个节点上「补充要求」框填什么（仅展示用）
    """

    __tablename__ = "agent_prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # idea / novel / script / storyboard …（同时是画布节点类型）
    key: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    label: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    stage: Mapped[str] = mapped_column(String(30), nullable=False, default="document")
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    user_template: Mapped[str] = mapped_column(Text, nullable=False, default="")
    var_hint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # ---- 长文本分块（单次调用输出上限 ≈ 2000 字，超长内容必须分块续写）----
    # plan_prompt：首次调用，产出「大纲/场次清单」，占位符见 user_template
    # chunk_prompt：逐块调用，占位符 {index} {total} {plan} {content} {params}
    # chunk_param：节点上控制块数的参数名（chapterCount / shotCount…）
    plan_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    chunk_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    chunk_param: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    # 留空则用节点上选择的模型 / 系统默认文本模型
    model_key: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.8)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=2000)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class ConfigAuditLog(Base):
    """配置变更审计：所有注册表的新增/修改/删除都会留痕，支持回滚。"""

    __tablename__ = "config_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    row_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(20), nullable=False)  # create|update|delete|rollback
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(50), nullable=False, default="local")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class ProviderPreset(Base):
    """服务商预设（一键填充地址与常用模型）。新增预设 = 插一行，界面自动出现。"""

    __tablename__ = "provider_presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 对应已注册的协议名（见 app/registry/adapters.py）
    kind: Mapped[str] = mapped_column(String(30), nullable=False, default="openai")
    base_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # [{"name": "...", "modality": "text|image|video", "label": "..."}]
    models_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    hint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 「粘贴一个 Key 自动接入」用来认归属：「|」分隔的若干正则，命中任意一条即算认出来。
    # 留空表示这个服务商不从 Key 形状上认（例如本地 Ollama 压根不需要 Key）。
    key_pattern: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class DirectorStyle(Base):
    """导演风格卡（风格库）。

    本质是「一套可复用的镜头语言 + 光影色调 + 提示词片段」，分三个注入点：

    - `agent_prompt`：给**创作 Agent**（分镜/剧本/小说）的风格要求，中文，**可以出现导演名**
      （它的作用是帮 LLM 理解技法）；
    - `image_prompt` / `video_prompt`：拼进**生图 / 生视频提示词**的英文片段，
      **一律只写技法特征，不出现导演名字**——规避肖像与版权风险，也避免平台审核误伤；
    - `negative_prompt`：约束词，同样只给模型侧用。

    拆成不同字段就是为了让这条合规边界在数据层就固定下来，改内容时不会越界。
    """

    __tablename__ = "director_styles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    agent_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    image_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    video_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    negative_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Project(Base):
    """创作项目：一次完整作品的容器（画布 / 素材 / 任务的归属单位）。

    画布数据结构等画布模块重做后再挂接（预留接口）；
    目前提供名称 / 描述 / 状态的 CRUD，前端可创建并管理项目。
    """

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # draft | active | archived
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # 画布文档（JSON），画布模块重做后启用；v1 恒为空
    canvas_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class ComfyWorkflow(Base):
    """ComfyUI 工作流（workflow_api.json 上传后存档）。

    graph_json 是 ComfyUI API 格式的原始图；param_map_json 是解析出的
    可调参数表（浮框按此动态渲染表单），output_kind 预判产物类型。
    """

    __tablename__ = "comfy_workflows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    graph_json: Mapped[str] = mapped_column(Text, nullable=False)
    param_map_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # image | video | mixed（按输出节点预判，仅展示用）
    output_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="image")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

