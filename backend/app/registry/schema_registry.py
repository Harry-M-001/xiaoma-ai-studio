"""配置表注册制。

把一张「配置表」注册进来，它立刻获得一整套能力（由 config_center_service 驱动）：

- 通用列表 / 新增 / 修改 / 删除 API（`/api/admin/schema/{table}`）
- 乐观锁：修改需带 `version`，不匹配返回 409，避免并发覆盖
- 审计留痕：每次变更记录 before / after
- 一键回滚：按审计记录还原
- 默认值兜底：读取缺失时回落到注册时声明的默认值

新增一张配置表 = 定义 ORM 模型 + 建一条 Alembic 迁移 + 在下方注册一条 TableSpec。
核心框架（服务 / 路由 / 管理接口）零改动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models import (
    AgentPrompt,
    ConfigItem,
    DirectorStyle,
    Modality,
    NavItem,
    ParamOption,
    Prompt,
    ProviderPreset,
)


@dataclass
class FieldSpec:
    """字段描述，供管理 API 与前端动态表单使用。"""

    name: str
    label: str = ""
    # string | text | int | float | bool | json | select
    type: str = "string"
    required: bool = False
    options: list[str] = field(default_factory=list)
    default: Any = None
    help: str = ""
    readonly: bool = False


@dataclass
class TableSpec:
    """一张受注册制管理的配置表。"""

    name: str  # 等于 __tablename__，同时是路由里的 {table}
    model: Any
    label: str = ""
    description: str = ""
    group: str = "config"
    fields: list[FieldSpec] = field(default_factory=list)
    order_by: str = "sort_order"
    has_version: bool = True
    # 是否允许无鉴权读取（仅返回非 secret 字段）
    public: bool = False
    # 幂等种子数据：不存在才插入，不覆盖用户修改
    seed: list[dict] = field(default_factory=list)


SCHEMA_REGISTRY: dict[str, TableSpec] = {}


def register(spec: TableSpec) -> TableSpec:
    SCHEMA_REGISTRY[spec.name] = spec
    return spec


def get(table: str) -> TableSpec | None:
    return SCHEMA_REGISTRY.get(table)


def all_specs() -> list[TableSpec]:
    return list(SCHEMA_REGISTRY.values())


# ============================================================
# 注册：系统配置
# ============================================================

register(
    TableSpec(
        name="config_items",
        model=ConfigItem,
        label="系统配置",
        description="影响全局运行的参数，改完即生效，无需重启或改代码。",
        group="system",
        fields=[
            FieldSpec("key", "配置键", "string", required=True, help="程序内引用用的唯一标识"),
            FieldSpec("label", "名称", "string"),
            FieldSpec(
                "group_name",
                "分组",
                "select",
                options=["basic", "defaults", "limits", "modules", "advanced", "update"],
                default="basic",
            ),
            FieldSpec("description", "说明", "text"),
            FieldSpec(
                "value_type",
                "值类型",
                "select",
                options=["string", "int", "float", "bool", "json"],
                default="string",
            ),
            FieldSpec("value_json", "值", "json", help="按值类型填写；字符串直接填内容即可"),
            FieldSpec("is_secret", "敏感", "bool", default=False, help="敏感项不在公开接口返回"),
            FieldSpec("sort_order", "排序", "int", default=0),
        ],
        seed=[
            # ---- 基础 ----
            {"key": "app.name", "group_name": "basic", "label": "站点名称", "value_type": "string",
             "value": "小马AI工坊", "sort_order": 1, "description": "显示在侧边栏与登录页"},
            {"key": "app.subtitle", "group_name": "basic", "label": "站点副标题", "value_type": "string",
             "value": "个人 AI 创作工作台", "sort_order": 2, "description": ""},
            {"key": "app.logo_text", "group_name": "basic", "label": "Logo 文字", "value_type": "string",
             "value": "马", "sort_order": 3, "description": "侧边栏方形 Logo 内的文字（1 个汉字最佳）"},
            # ---- 默认值 ----
            {"key": "defaults.text_model", "group_name": "defaults", "label": "默认文本模型", "value_type": "string",
             "value": "", "sort_order": 11, "description": "格式 服务ID:模型名，留空则由用户每次手动选择"},
            {"key": "defaults.image_model", "group_name": "defaults", "label": "默认图片模型", "value_type": "string",
             "value": "", "sort_order": 12, "description": ""},
            {"key": "defaults.video_model", "group_name": "defaults", "label": "默认视频模型", "value_type": "string",
             "value": "", "sort_order": 13, "description": ""},
            {"key": "defaults.temperature", "group_name": "defaults", "label": "默认温度", "value_type": "float",
             "value": 0.7, "sort_order": 14, "description": "对话模型的默认 temperature"},
            {"key": "defaults.image_size", "group_name": "defaults", "label": "默认图片尺寸", "value_type": "string",
             "value": "1024x1024", "sort_order": 15, "description": ""},
            {"key": "defaults.image_count", "group_name": "defaults", "label": "默认生成张数", "value_type": "int",
             "value": 1, "sort_order": 16, "description": ""},
            {"key": "defaults.video_duration", "group_name": "defaults", "label": "默认视频时长（秒）", "value_type": "int",
             "value": 5, "sort_order": 17, "description": ""},
            {"key": "defaults.video_ratio", "group_name": "defaults", "label": "默认视频画幅", "value_type": "string",
             "value": "16:9", "sort_order": 18, "description": ""},
            {"key": "defaults.video_resolution", "group_name": "defaults", "label": "默认视频清晰度", "value_type": "string",
             "value": "720p", "sort_order": 19, "description": ""},
            # ---- 限制 ----
            {"key": "limits.upload_max_mb", "group_name": "limits", "label": "上传大小上限（MB）", "value_type": "int",
             "value": 10, "sort_order": 21, "description": "单文件上传体积上限"},
            {"key": "limits.task_concurrency", "group_name": "limits", "label": "任务并发数", "value_type": "int",
             "value": 4, "sort_order": 22, "description": "同时执行的生成任务数量上限"},
            {"key": "limits.video_poll_interval", "group_name": "limits", "label": "视频轮询间隔（秒）", "value_type": "int",
             "value": 5, "sort_order": 23, "description": ""},
            {"key": "limits.video_max_wait_minutes", "group_name": "limits", "label": "视频最长等待（分钟）", "value_type": "int",
             "value": 30, "sort_order": 24, "description": "超时后任务标记失败"},
            {"key": "limits.request_timeout", "group_name": "limits", "label": "接口请求超时（秒）", "value_type": "int",
             "value": 300, "sort_order": 25, "description": "调用上游模型接口的超时时间"},
            {"key": "limits.asset_page_size", "group_name": "limits", "label": "资产库每页数量", "value_type": "int",
             "value": 60, "sort_order": 26, "description": ""},
            {"key": "limits.batch_max_tasks", "group_name": "limits", "label": "批量任务上限", "value_type": "int",
             "value": 20, "sort_order": 27, "description": "单次批量提交最多创建的任务数（提示词数 × 模型数）"},
            {"key": "limits.video_upload_max_mb", "group_name": "limits", "label": "视频导入上限（MB）", "value_type": "int",
             "value": 500, "sort_order": 28, "description": "导演台导入视频的单文件大小上限"},
            {"key": "limits.ffmpeg_path", "group_name": "limits", "label": "FFmpeg 路径", "value_type": "string",
             "value": "", "sort_order": 29, "description": "留空自动检测（PATH → winget / scoop 等常见位置，按能力择优）；填写 ffmpeg 可执行文件完整路径可强制指定"},
            # ---- 模块开关 ----
            {"key": "modules.chat", "group_name": "modules", "label": "文本对话", "value_type": "bool",
             "value": True, "sort_order": 31, "description": "关闭后导航与页面隐藏"},
            {"key": "modules.image", "group_name": "modules", "label": "图片生成", "value_type": "bool",
             "value": True, "sort_order": 32, "description": ""},
            {"key": "modules.video", "group_name": "modules", "label": "视频生成", "value_type": "bool",
             "value": True, "sort_order": 33, "description": ""},
            {"key": "modules.assets", "group_name": "modules", "label": "资产库", "value_type": "bool",
             "value": True, "sort_order": 34, "description": ""},
            {"key": "modules.community", "group_name": "modules", "label": "社区（预留）", "value_type": "bool",
             "value": False, "sort_order": 35, "description": "预留开关：开启后显示社区入口"},
            {"key": "modules.prompts", "group_name": "modules", "label": "提示词库", "value_type": "bool",
             "value": True, "sort_order": 36, "description": "关闭后导航与页面隐藏"},
            {"key": "modules.tasks", "group_name": "modules", "label": "任务中心", "value_type": "bool",
             "value": True, "sort_order": 37, "description": "关闭后导航与页面隐藏"},
            {"key": "modules.director", "group_name": "modules", "label": "导演台", "value_type": "bool",
             "value": True, "sort_order": 38, "description": "关闭后导航与页面隐藏"},
            {"key": "modules.projects", "group_name": "modules", "label": "项目", "value_type": "bool",
             "value": True, "sort_order": 39, "description": "关闭后导航与页面隐藏"},
            {"key": "safety.confirm_before_generate", "group_name": "safety", "label": "生成前二次确认", "value_type": "bool",
             "value": True, "sort_order": 1, "description": "开启后，提交图片 / 视频生成前弹窗确认，防止误点烧钱"},
            # ---- 在线更新 ----
            {"key": "update.repo", "group_name": "update", "label": "更新源仓库（GitHub）", "value_type": "string",
             "value": "Harry-M-001/xiaoma-ai-studio", "sort_order": 41,
             "description": "形如 用户名/仓库名。填好后「关于与更新」才能检查新版本"},
            {"key": "update.repo_gitee", "group_name": "update", "label": "更新源仓库（Gitee）", "value_type": "string",
             "value": "haoruiM/xiaoma-ai-studio", "sort_order": 42,
             "description": "Gitee 的账号名与 GitHub 不同时必须单独填；留空则复用上面那个"},
            {"key": "update.source", "group_name": "update", "label": "更新源", "value_type": "string",
             "value": "gitee", "sort_order": 43,
             "description": "gitee 或 github。国内访问 Gitee 更稳；配置的源不通时会自动尝试另一个"},
            {"key": "update.check_on_start", "group_name": "update", "label": "启动时检查更新", "value_type": "bool",
             "value": True, "sort_order": 44,
             "description": "开启后每次打开界面静默检查一次，有新版本时在「系统设置」入口提示"},
        ],
    )
)

# ============================================================
# 注册：创作参数可选项
# ============================================================

register(
    TableSpec(
        name="param_options",
        model=ParamOption,
        label="参数选项",
        description="创作页的可选档位。新增一档 = 插一行，前后端自动生效。",
        group="system",
        fields=[
            FieldSpec("kind", "参数类型", "string", required=True,
                      help="如 image_size / image_count / video_duration / video_ratio / video_resolution"),
            FieldSpec("value", "值", "string", required=True, help="提交给接口的原始值"),
            FieldSpec("label", "显示名", "string", help="界面上展示的文字，留空则显示值"),
            FieldSpec("meta_json", "附加信息", "json", default={}, help="如 {\"ratio\":\"1:1\"}"),
            FieldSpec("sort_order", "排序", "int", default=0),
            FieldSpec("enabled", "启用", "bool", default=True),
        ],
        seed=[
            {"kind": "image_size", "value": "1024x1024", "label": "1:1", "meta_json": {"ratio": "1:1", "width": 1024, "height": 1024}, "sort_order": 1},
            {"kind": "image_size", "value": "864x1152", "label": "3:4", "meta_json": {"ratio": "3:4", "width": 864, "height": 1152}, "sort_order": 2},
            {"kind": "image_size", "value": "1152x864", "label": "4:3", "meta_json": {"ratio": "4:3", "width": 1152, "height": 864}, "sort_order": 3},
            {"kind": "image_size", "value": "1280x720", "label": "16:9", "meta_json": {"ratio": "16:9", "width": 1280, "height": 720}, "sort_order": 4},
            {"kind": "image_size", "value": "720x1280", "label": "9:16", "meta_json": {"ratio": "9:16", "width": 720, "height": 1280}, "sort_order": 5},
            {"kind": "image_size", "value": "2048x2048", "label": "2K 方图", "meta_json": {"ratio": "1:1", "width": 2048, "height": 2048}, "sort_order": 6, "enabled": False},

            {"kind": "image_count", "value": "1", "label": "1 张", "sort_order": 1},
            {"kind": "image_count", "value": "2", "label": "2 张", "sort_order": 2},
            {"kind": "image_count", "value": "3", "label": "3 张", "sort_order": 3},
            {"kind": "image_count", "value": "4", "label": "4 张", "sort_order": 4},

            {"kind": "video_duration", "value": "5", "label": "5 秒", "sort_order": 1},
            {"kind": "video_duration", "value": "10", "label": "10 秒", "sort_order": 2},
            {"kind": "video_duration", "value": "15", "label": "15 秒", "sort_order": 3, "enabled": False},

            {"kind": "video_ratio", "value": "16:9", "label": "16:9", "sort_order": 1},
            {"kind": "video_ratio", "value": "9:16", "label": "9:16", "sort_order": 2},
            {"kind": "video_ratio", "value": "1:1", "label": "1:1", "sort_order": 3},
            {"kind": "video_ratio", "value": "4:3", "label": "4:3", "sort_order": 4},
            {"kind": "video_ratio", "value": "3:4", "label": "3:4", "sort_order": 5},
            {"kind": "video_ratio", "value": "21:9", "label": "21:9", "sort_order": 6},

            {"kind": "video_resolution", "value": "480p", "label": "480p", "sort_order": 1},
            {"kind": "video_resolution", "value": "720p", "label": "720p", "sort_order": 2},
            {"kind": "video_resolution", "value": "1080p", "label": "1080p", "sort_order": 3},
        ],
    )
)

# ============================================================
# 注册：模型能力类型
# ============================================================

register(
    TableSpec(
        name="modalities",
        model=Modality,
        label="能力类型",
        description="模型可承担的能力。新增一种模态 = 插一行 + 补一个适配器实现。",
        group="system",
        fields=[
            FieldSpec("key", "标识", "string", required=True, help="英文小写，如 text / image / video / audio"),
            FieldSpec("label", "名称", "string", required=True),
            FieldSpec("icon", "图标名", "string", help="前端图标标识，可留空"),
            FieldSpec("sort_order", "排序", "int", default=0),
            FieldSpec("enabled", "启用", "bool", default=True),
        ],
        seed=[
            {"key": "text", "label": "文本", "icon": "message", "sort_order": 1},
            {"key": "image", "label": "图片", "icon": "image", "sort_order": 2},
            {"key": "video", "label": "视频", "icon": "video", "sort_order": 3},
            # 预留示例：把 enabled 打开即可启用音频能力
            {"key": "audio", "label": "音频", "icon": "audio", "sort_order": 4, "enabled": False},
        ],
    )
)

# ============================================================
# 注册：导航
# ============================================================

register(
    TableSpec(
        name="nav_items",
        model=NavItem,
        label="导航菜单",
        description="侧边栏入口。加模块 / 隐藏模块 / 预留功能都只改这里。",
        group="system",
        public=True,
        fields=[
            FieldSpec("key", "标识", "string", required=True),
            FieldSpec("label", "名称", "string", required=True),
            FieldSpec("icon", "图标名", "string", help="message / image / video / library / settings / workflow / community"),
            FieldSpec("route", "路由", "string", required=True, help="对应前端的页面标识"),
            FieldSpec("group_name", "分组", "select", options=["main", "settings"], default="main"),
            FieldSpec("sort_order", "排序", "int", default=0),
            FieldSpec("enabled", "显示", "bool", default=True),
            FieldSpec("requires_auth", "需登录", "bool", default=False),
        ],
        seed=[
            {"key": "home", "label": "首页", "icon": "home", "route": "home", "group_name": "main", "sort_order": 0},
            {"key": "projects", "label": "项目", "icon": "folder", "route": "projects", "group_name": "main", "sort_order": 1},
            {"key": "chat", "label": "文本对话", "icon": "message", "route": "chat", "group_name": "main", "sort_order": 2},
            {"key": "image", "label": "图片生成", "icon": "image", "route": "image", "group_name": "main", "sort_order": 3},
            {"key": "video", "label": "视频生成", "icon": "video", "route": "video", "group_name": "main", "sort_order": 4},
            {"key": "assets", "label": "资产库", "icon": "library", "route": "assets", "group_name": "main", "sort_order": 5},
            {"key": "prompts", "label": "提示词库", "icon": "sparkles", "route": "prompts", "group_name": "main", "sort_order": 6},
            {"key": "tasks", "label": "任务中心", "icon": "tasks", "route": "tasks", "group_name": "main", "sort_order": 7},
            {"key": "director", "label": "导演台", "icon": "clapperboard", "route": "director", "group_name": "main", "sort_order": 8},
            {"key": "workflow", "label": "工作流", "icon": "workflow", "route": "workflow", "group_name": "main", "sort_order": 9, "enabled": False},
            {"key": "community", "label": "社区", "icon": "community", "route": "community", "group_name": "main", "sort_order": 10, "enabled": False, "requires_auth": True},
            {"key": "providers", "label": "模型服务", "icon": "settings", "route": "providers", "group_name": "settings", "sort_order": 1},
            {"key": "settings", "label": "系统设置", "icon": "sliders", "route": "settings", "group_name": "settings", "sort_order": 2},
        ],
    )
)

# ============================================================
# 注册：服务商预设
# ============================================================

register(
    TableSpec(
        name="provider_presets",
        model=ProviderPreset,
        label="服务商预设",
        description="添加服务时的一键填充模板。新增服务商 = 插一行，界面按钮自动出现。",
        group="system",
        public=True,
        fields=[
            FieldSpec("key", "标识", "string", required=True),
            FieldSpec("name", "名称", "string", required=True),
            FieldSpec("kind", "协议", "string", required=True,
                      help="已注册的协议名，如 openai / ark（见模型协议注册表）"),
            FieldSpec("base_url", "接口地址", "string", required=True),
            FieldSpec("models_json", "预设模型", "json", default=[],
                      help='形如 [{"name":"gpt-4o","modality":"text","label":"GPT-4o"}]'),
            FieldSpec("hint", "提示文案", "text"),
            FieldSpec("sort_order", "排序", "int", default=0),
            FieldSpec("enabled", "显示", "bool", default=True),
        ],
        seed=[
            {
                "key": "openai", "name": "OpenAI", "kind": "openai",
                "base_url": "https://api.openai.com/v1", "sort_order": 1,
                "hint": "官方接口，模型名为 gpt-4o / gpt-image-1 等",
                "models_json": [
                    {"name": "gpt-4o", "modality": "text", "label": "GPT-4o"},
                    {"name": "gpt-4o-mini", "modality": "text", "label": "GPT-4o mini"},
                    {"name": "gpt-image-1", "modality": "image", "label": "GPT Image"},
                ],
            },
            {
                "key": "deepseek", "name": "DeepSeek", "kind": "openai",
                "base_url": "https://api.deepseek.com/v1", "sort_order": 2,
                "hint": "性价比高的中文文本模型",
                "models_json": [
                    {"name": "deepseek-chat", "modality": "text", "label": "DeepSeek Chat"},
                    {"name": "deepseek-reasoner", "modality": "text", "label": "DeepSeek R1"},
                ],
            },
            {
                "key": "kimi", "name": "月之暗面 Kimi", "kind": "openai",
                "base_url": "https://api.moonshot.cn/v1", "sort_order": 3,
                "hint": "长上下文文本模型",
                "models_json": [
                    {"name": "moonshot-v1-8k", "modality": "text", "label": "Moonshot 8K"},
                    {"name": "moonshot-v1-32k", "modality": "text", "label": "Moonshot 32K"},
                    {"name": "moonshot-v1-128k", "modality": "text", "label": "Moonshot 128K"},
                ],
            },
            {
                "key": "qwen", "name": "通义千问", "kind": "openai",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "sort_order": 4,
                "hint": "阿里云百炼的 OpenAI 兼容模式",
                "models_json": [
                    {"name": "qwen-plus", "modality": "text", "label": "Qwen Plus"},
                    {"name": "qwen-turbo", "modality": "text", "label": "Qwen Turbo"},
                    {"name": "qwen-max", "modality": "text", "label": "Qwen Max"},
                ],
            },
            {
                "key": "zhipu", "name": "智谱 GLM", "kind": "openai",
                "base_url": "https://open.bigmodel.cn/api/paas/v4", "sort_order": 5,
                "hint": "文本与图片均可",
                "models_json": [
                    {"name": "glm-4-plus", "modality": "text", "label": "GLM-4 Plus"},
                    {"name": "glm-4-flash", "modality": "text", "label": "GLM-4 Flash"},
                    {"name": "cogview-3-plus", "modality": "image", "label": "CogView 3"},
                ],
            },
            {
                "key": "ark", "name": "火山方舟 Ark", "kind": "ark",
                "base_url": "https://ark.cn-beijing.volces.com/api/v3", "sort_order": 6,
                "hint": "豆包 Seedream 图片 / Seedance 视频，模型名填具体版本 ID",
                "models_json": [
                    {"name": "doubao-seedream-4-0-250828", "modality": "image", "label": "豆包 Seedream 4.0"},
                    {"name": "doubao-seedance-1-0-pro-250528", "modality": "video", "label": "豆包 Seedance 1.0 Pro"},
                ],
            },
            {
                "key": "openrouter", "name": "OpenRouter", "kind": "openai",
                "base_url": "https://openrouter.ai/api/v1", "sort_order": 7,
                "hint": "聚合网关，模型名如 anthropic/claude-3.5-sonnet",
                "models_json": [],
            },
            {
                "key": "ollama", "name": "本地 Ollama", "kind": "openai",
                "base_url": "http://127.0.0.1:11434/v1", "sort_order": 8,
                "hint": "完全本地运行，API Key 可任意填写",
                "models_json": [],
            },
        ],
    )
)

# ============================================================
# 注册：提示词库
# ============================================================

register(
    TableSpec(
        name="prompts",
        model=Prompt,
        label="提示词库",
        description="可复用的提示词模板。{花括号} 为占位符，使用时替换；key 用于导入导出与分享。",
        group="system",
        fields=[
            FieldSpec("key", "标识", "string", required=True, help="唯一标识，用于导入导出与分享"),
            FieldSpec("title", "标题", "string", required=True),
            FieldSpec(
                "modality", "适用场景", "select",
                options=["text", "image", "video"], default="image",
            ),
            FieldSpec("content", "提示词内容", "text", required=True),
            FieldSpec("description", "使用说明", "text"),
            FieldSpec("tags", "标签", "string", help="逗号分隔，如：人像,摄影,电影感"),
            FieldSpec("sort_order", "排序", "int", default=0),
            FieldSpec("enabled", "启用", "bool", default=True),
        ],
        seed=[
            {
                "key": "cinematic-portrait", "title": "电影感人像摄影", "modality": "image",
                "tags": "人像,摄影,电影感", "sort_order": 1,
                "description": "把 {主体} 替换成想画的人或角色即可",
                "content": "电影感人像摄影：{主体}，半身特写，柔和侧逆光，浅景深虚化背景，胶片颗粒质感，自然肤色，35mm 镜头，电影调色，高细节",
            },
            {
                "key": "ink-wash-landscape", "title": "国风水墨山水", "modality": "image",
                "tags": "国风,插画,水墨", "sort_order": 2,
                "description": "描述场景填入 {场景}，例如「江南烟雨中的乌篷船」",
                "content": "中国水墨画风格：{场景}，留白构图，远山如黛，淡墨晕染，几笔飞白，意境悠远，宣纸纹理，大师笔法",
            },
            {
                "key": "isometric-3d-scene", "title": "等距 3D 小场景", "modality": "image",
                "tags": "3D,插画,等距", "sort_order": 3,
                "description": "适合做封面插画或图标",
                "content": "等距视角 3D 微缩场景（isometric）：{场景}，柔和漫射光，低饱和马卡龙配色，圆润造型，C4D 渲染，干净背景，精致细节",
            },
            {
                "key": "product-shot", "title": "产品悬浮摄影", "modality": "image",
                "tags": "产品,电商,摄影", "sort_order": 4,
                "description": "电商主图常用",
                "content": "商业产品摄影：{产品}悬浮在画面中心，柔和影棚光，浅色渐变背景，微妙投影，水珠与光斑点缀，超高清细节，广告级质感",
            },
            {
                "key": "product-video-loop", "title": "产品展示镜头", "modality": "video",
                "tags": "产品,电商,运镜", "sort_order": 5,
                "description": "电商短视频常用运镜",
                "content": "产品展示视频：{产品}置于旋转展台，环绕运镜缓慢推近，影棚柔光，浅景深，细节高光流转，商业广告质感，5 秒",
            },
            {
                "key": "cinematic-establishing", "title": "电影开场空镜", "modality": "video",
                "tags": "电影感,空镜,氛围", "sort_order": 6,
                "description": "把 {场景} 换成具体环境，如「雨后的老城街道」",
                "content": "电影开场空镜头：{场景}，清晨薄雾，镜头缓慢横移，自然光，空气中的尘埃颗粒，胶片质感调色，宁静氛围",
            },
            {
                "key": "xiaohongshu-title", "title": "小红书爆款标题", "modality": "text",
                "tags": "文案,小红书,运营", "sort_order": 7,
                "description": "一次生成 10 个候选标题",
                "content": "你是小红书资深运营。请为以下主题生成 10 个小红书爆款标题，要求：带 emoji、制造好奇或痛点、口语化、每个不超过 20 字，并标注最推荐的 1 个及理由。\n主题：{主题}",
            },
            {
                "key": "short-video-script", "title": "短视频口播脚本", "modality": "text",
                "tags": "文案,短视频,脚本", "sort_order": 8,
                "description": "60 秒口播结构：钩子→痛点→干货→行动号召",
                "content": "你是爆款短视频编导。请围绕「{主题}」写一条 60 秒口播脚本：前 3 秒强钩子，结构为钩子→痛点→干货（3 点）→行动号召，口语化有网感，标注每段秒数。",
            },
            {
                "key": "prompt-enhancer", "title": "提示词优化助手", "modality": "text",
                "tags": "工具,提示词,效率", "sort_order": 9,
                "description": "把一句话想法扩写成专业图片提示词",
                "content": "你是提示词工程专家。请把我的原始想法改写成高质量的图片生成提示词：补充主体细节、环境、光线、构图、风格与画质词；输出英文版和中文版各一条，并解释你补充了什么。\n我的想法：{想法}",
            },
            {
                "key": "translate-polish", "title": "翻译润色", "modality": "text",
                "tags": "工具,翻译,写作", "sort_order": 10,
                "description": "信达雅翻译，专有名词括注保留",
                "content": "请把下面的内容翻译成{目标语言}，要求信达雅，保留原意与语气，专有名词保留原文并括注：\n\n{内容}",
            },
        ],
    )
)

# ============================================================
# 注册：创作 Agent（自动链）
# ============================================================
# 自动链的核心原则：**内容层全部是 Markdown 正文，只让 LLM 写内容**，
# 结构化的东西（节点拓扑、资产清单）才用 JSON/表格。
#
# 每个阶段一个 Agent = 一份 system_prompt（角色 + 硬性规则）
# + user_template（用户消息模板）+ 可选的分块续写模板。
# 占位符：{content} 上游全文、{params} 节点补充要求、{plan} 大纲、{index}/{total} 块序号。

register(
    TableSpec(
        name="agent_prompts",
        model=AgentPrompt,
        label="创作 Agent",
        description="自动链每个阶段的提示词。改这里就能调整全链的写作风格与产出格式。",
        group="system",
        public=True,
        fields=[
            FieldSpec("key", "阶段标识", "string", required=True,
                      help="同时是画布节点类型，如 idea / novel / script / storyboard"),
            FieldSpec("label", "名称", "string", required=True),
            FieldSpec("stage", "归类", "string", default="document"),
            FieldSpec("system_prompt", "系统提示词", "text", required=True,
                      help="角色设定与硬性规则；写清楚输出格式，模型才不会乱写"),
            FieldSpec("user_template", "用户消息模板", "text", required=True,
                      help="占位符 {content} 上游全文、{params} 节点上的补充要求"),
            FieldSpec("var_hint", "补充要求提示", "text",
                      help="展示在节点浮框里的填写说明，仅作提示"),
            FieldSpec("plan_prompt", "大纲提示词", "text",
                      help="填了就先调一次产出大纲/清单，再逐块续写；留空则一次生成"),
            FieldSpec("chunk_prompt", "分块续写模板", "text",
                      help="逐块调用，占位符 {index} 第几块、{total} 总块数、{plan} 大纲、{content} 上游全文"),
            FieldSpec("chunk_param", "块数参数名", "string",
                      help="节点上控制块数的参数，如 chapterCount / sceneCount / shotCount"),
            FieldSpec("model_key", "指定模型", "string",
                      help="留空则用节点上选择的模型"),
            FieldSpec("temperature", "温度", "float", default=0.8),
            FieldSpec("max_tokens", "单次输出上限", "int", default=2000,
                      help="写太长会被截断，建议 2000 左右并配合分块续写"),
            FieldSpec("sort_order", "排序", "int", default=0),
            FieldSpec("enabled", "启用", "bool", default=True),
        ],
        seed=[
            {
                "key": "idea",
                "label": "创意策划",
                "stage": "document",
                "sort_order": 1,
                "temperature": 0.9,
                "max_tokens": 2000,
                "var_hint": "补充要求可写：题材、目标受众、集数/时长、必须有的元素",
                "system_prompt": (
                    "你是深耕短剧与网文的资深策划。任务：把用户的一句话想法，扩写成一份可以直接开写的创意简报。\n"
                    "硬性要求：\n"
                    "1. 只输出简报正文，不要任何寒暄、解释或自我评价。\n"
                    "2. 严格照抄下面给定的 Markdown 结构与小节标题，不要增删小节。\n"
                    "3. 每个小节都要具体可执行，禁止「很有趣」「很感人」这类空话。\n"
                    "4. 主角必须有明确的欲望与弱点，冲突必须落到具体的人与事上。\n\n"
                    "输出结构（照抄）：\n"
                    "## 一句话卖点\n<20 字以内，要有钩子>\n\n"
                    "## 题材与基调\n<题材 + 情绪基调 + 目标受众>\n\n"
                    "## 主角\n- 姓名：\n- 身份：\n- 核心欲望：\n- 致命弱点：\n\n"
                    "## 核心冲突\n<谁与谁因何对立，赌注是什么>\n\n"
                    "## 三幕结构\n1. 开局（约 20%）：\n2. 转折（约 50%）：\n3. 高潮与结局（约 30%）：\n\n"
                    "## 爽点与钩子清单\n<5 条，每条一句话>\n\n"
                    "## 视觉基调\n<场景、色调、年代感，供后面分镜使用>\n"
                ),
                "user_template": "【我的想法】\n{content}\n\n【补充要求】\n{params}",
                "plan_prompt": "",
                "chunk_prompt": "",
                "chunk_param": "",
            },
            {
                "key": "novel",
                "label": "小说",
                "stage": "document",
                "sort_order": 2,
                "temperature": 0.85,
                "max_tokens": 2000,
                "var_hint": "补充要求可写：人称、文风、必须出现的情节",
                "chunk_param": "chapterCount",
                "system_prompt": (
                    "你是擅长强情节的网文小说家。任务：根据【创意简报】写小说正文。\n"
                    "硬性要求：\n"
                    "1. 只输出小说正文，禁止输出大纲、设定说明、作者旁白或任何解释。\n"
                    "2. 开篇即冲突，不要大段背景铺陈。\n"
                    "3. 每章 800-1500 字，章末必须留钩子。\n"
                    "4. 多用具体动作与对话推进，少用形容词堆砌。\n"
                    "5. 严格保持人称与人物设定一致。\n"
                ),
                "user_template": "【创意简报】\n{content}\n\n【补充要求】\n{params}",
                "plan_prompt": (
                    "先只做一件事：根据【创意简报】列出全书的章节大纲。\n"
                    "输出格式（每章两行，不要写正文）：\n"
                    "## 第1章 <章节名>\n"
                    "<一句话剧情梗概 + 章末钩子>\n\n"
                    "共列出 {total} 章。\n\n"
                    "【创意简报】\n{content}\n\n【补充要求】\n{params}"
                ),
                "chunk_prompt": (
                    "现在只写第 {index} 章（全书共 {total} 章），不要写其它章节，也不要重复前面内容。\n\n"
                    "【全书大纲】\n{plan}\n\n"
                    "【创意简报】\n{content}\n\n【补充要求】\n{params}\n\n"
                    "输出格式：\n## 第{index}章 <章节名>\n<正文 800-1500 字，开篇即冲突，章末留钩子>"
                ),
            },
            {
                "key": "script",
                "label": "剧本",
                "stage": "document",
                "sort_order": 3,
                "temperature": 0.8,
                "max_tokens": 2000,
                "var_hint": "补充要求可写：目标集数、必须保留的台词、删减方向",
                "chunk_param": "sceneCount",
                "system_prompt": (
                    "你是专业短剧编剧。任务：把【小说/创意】改编成可拍摄的剧本。\n"
                    "硬性要求：\n"
                    "1. 按场景切场次，每场标题固定格式：`## S01 | 内景 · 咖啡厅 | 黄昏`。\n"
                    "2. 动作用自然段描写；台词格式：`角色名：（表情/动作提示）台词`。\n"
                    "3. 只写戏，不写任何镜头语言（景别、运镜、剪辑术语一律不出现），那是分镜阶段的事。\n"
                    "4. 忠实保留核心情节与台词精神，不增删主线。\n"
                    "5. 一次只输出一场戏，不要输出后续场次。\n"
                ),
                "user_template": "【上游内容】\n{content}\n\n【补充要求】\n{params}",
                "plan_prompt": (
                    "先只做一件事：列出全剧的场次清单，每场一行，不要写剧本正文。\n"
                    "格式：`S01 | 内景/外景 · 地点 | 时间 | 本场要发生的事（一句话）`\n"
                    "共列出 {total} 场。\n\n"
                    "【上游内容】\n{content}\n\n【补充要求】\n{params}"
                ),
                "chunk_prompt": (
                    "现在只写第 {index} 场（全剧共 {total} 场），不要写其它场次，也不要重复已写内容。\n\n"
                    "【场次清单】\n{plan}\n\n"
                    "【上游内容】\n{content}\n\n【补充要求】\n{params}\n\n"
                    "输出格式：`## S{index} | 内景/外景 · 地点 | 时间` + 本场动作与台词。"
                ),
            },
            {
                "key": "storyboard",
                "label": "分镜",
                "stage": "document",
                "sort_order": 4,
                "temperature": 0.8,
                "max_tokens": 2000,
                "var_hint": "补充要求可写：整体影调、必须出现的画面、参考片风格",
                "chunk_param": "shotCount",
                "system_prompt": (
                    "你是分镜导演。任务：把【剧本/内容】拆成可直接投喂给生图与生视频模型的镜头表。\n"
                    "硬性要求：\n"
                    "1. 一个镜头只指定 1 种运镜，禁止写「推拉摇移」组合。\n"
                    "2. 情绪必须用身体细节外化，禁止「很悲伤」这类抽象词。\n"
                    "3. 首帧提示词用英文，公式：精准主体 + 场景环境 + 光影色调 + 视觉风格 + 画质。\n"
                    "4. 不要写精确秒数区间（模型支持不稳定），时长给整数即可。\n"
                    "5. 每个镜头严格使用下面的小节结构，只输出镜头表，不要解释。\n"
                ),
                "user_template": "【上游内容】\n{content}\n\n【补充要求】\n{params}",
                "plan_prompt": (
                    "先只做一件事：列出全部镜头清单，每个镜头一行，不要展开细节。\n"
                    "格式：`镜头N | 景别 | 运镜 | 时长s | 这一镜要表达什么`\n"
                    "共列出 {total} 个镜头，每场至少 1 个建立镜头、1 个特写。\n\n"
                    "【上游内容】\n{content}\n\n【补充要求】\n{params}"
                ),
                "chunk_prompt": (
                    "现在只写第 {index} 个镜头（全片共 {total} 个），不要写其它镜头，也不要重复。\n\n"
                    "【镜头清单】\n{plan}\n\n"
                    "【上游内容】\n{content}\n\n【补充要求】\n{params}\n\n"
                    "输出格式（严格照抄，{index} 换成实际镜号）：\n"
                    "### 镜头{index} | <景别> | <运镜（仅 1 种）> | <时长>s\n"
                    "- 画面：<主体 + 动作（肢体细化、程度量化）+ 空间变化>\n"
                    "- 台词：<有则写，无则留空>\n"
                    "- 情绪外化：<用身体细节表现情绪>\n"
                    "- 首帧提示词：<英文，遵循公式>\n"
                    "- 约束：无字幕、无 Logo、无水印；人物面部稳定不变形"
                ),
            },
            {
                "key": "assetSheet",
                "label": "资产表",
                "stage": "document",
                "sort_order": 5,
                "temperature": 0.6,
                "max_tokens": 2600,
                "var_hint": "补充要求可写：统一画风词、必须保留的角色、道具细节要求",
                # 资产表故意不做分块：表被拆成两半既难读也难解析
                "chunk_param": "",
                "system_prompt": (
                    "你是影视/动画的前期美术统筹。任务：通读【上游内容】，整理出一份可以直接交给美术做设定图的资产表。\n"
                    "硬性要求：\n"
                    "1. 只输出 Markdown 表格，禁止任何解释、寒暄、前后缀说明文字。\n"
                    "2. 严格使用下面这 4 列，表头照抄：\n"
                    "| 中文名 | 类型 | 英文视觉描述 | 出现场次 |\n"
                    "| --- | --- | --- | --- |\n"
                    "3. 「类型」只能填 角色 / 场景 / 道具 三者之一。\n"
                    "4. 「中文名」沿用上游内容里原本的叫法，2-8 个字，全表唯一，不要加书名号或括号注释。\n"
                    "5. 「英文视觉描述」是逗号分隔的英文关键词，只写看得见的东西：物种/年龄段/发型发色/服装材质与颜色/体型/标志性特征（道具写形状、材质、颜色）。"
                    "禁止抽象评价（beautiful、cool）、禁止剧情与情绪、禁止中文。\n"
                    "6. 「出现场次」写上游里的场次号（如 S01,S03）；全片都出现写「全片」；上游没有场次号就写镜头号（如 SH01）。\n"
                    "7. 覆盖范围：所有有名有姓的角色、所有反复出现的场景、所有推动剧情的道具；一次性路人与临时背景不收录。\n"
                    "8. 同一角色的不同装扮或年龄段各占一行，名字写成「角色名（装扮）」。\n"
                    "9. 最多 24 行；超出时优先保留角色与关键场景。\n"
                    "10. 描述必须互相可区分：两个角色不能写成同一种外观。\n"
                ),
                "user_template": "【上游内容】\n{content}\n\n【补充要求】\n{params}",
                "plan_prompt": "",
                "chunk_prompt": "",
            },
        ],
    )
)

# ============================================================
# 注册：导演风格库
# ============================================================
# 风格卡 = 一套可复用的「镜头语言 + 光影色调 + 提示词片段」，三个注入点：
#   agent_prompt    → 给创作 Agent（分镜/剧本/小说）的风格要求，中文，**可以出现导演名**
#   image_prompt    → 拼进生图提示词，英文，**只写技法特征，不出现导演名**
#   video_prompt    → 拼进生视频提示词，英文，**只写技法特征，不出现导演名**
#   negative_prompt → 模型侧约束词
#
# 这条边界是刻意的（合规 + 过审）：导演名只帮 LLM 理解技法，
# 绝不把它甩给生图/生视频模型——既避开肖像与版权风险，也躲开平台关键词审核。
# 种子内容只描述公开的镜头语言与影调特征，不引用任何具体作品。

register(
    TableSpec(
        name="director_styles",
        model=DirectorStyle,
        label="导演风格",
        description="画布节点上可选的风格卡。改这里 = 改全链的风格注入，前端下拉自动跟着变。",
        group="system",
        fields=[
            FieldSpec("key", "标识", "string", required=True, help="英文小写，供画布节点引用"),
            FieldSpec("name", "名称", "string", required=True, help="下拉里显示的名字"),
            FieldSpec("agent_prompt", "给 Agent 的风格要求", "text",
                      help="中文，注入分镜/剧本/小说的系统提示词；这里可以写导演名，帮模型理解技法"),
            FieldSpec("image_prompt", "生图风格词", "text",
                      help="英文，拼进生图提示词。只写技法特征，禁止出现导演人名（合规）"),
            FieldSpec("video_prompt", "生视频风格词", "text",
                      help="英文，拼进生视频提示词。同样禁止出现导演人名"),
            FieldSpec("negative_prompt", "约束词", "text", help="英文负面词，拼在提示词末尾"),
            FieldSpec("sort_order", "排序", "int", default=0),
            FieldSpec("enabled", "启用", "bool", default=True),
        ],
        seed=[
            {
                "key": "spielberg_face", "name": "斯皮尔伯格·面孔", "sort_order": 1,
                "agent_prompt": (
                    "你按「斯皮尔伯格式」的镜头语言工作。\n"
                    "- 镜头语言：主观视角与反应镜头优先；用人物面孔特写驱动情绪；平滑推轨与缓慢升降；"
                    "大场面先给一个仰望视角交代体量。\n"
                    "- 光影色调：自然光为主，暖调高饱和，逆光轮廓带柔和眩光；夜戏只用实用光源（灯、车灯、窗光）。\n"
                    "- 节奏与剪辑：情绪累积后给一个释放点；让观众比角色先知道，用视线与画外空间制造悬念。\n"
                    "- 代表技法：面孔特写、仰望视角、窗光斜射进室内。"
                ),
                "image_prompt": (
                    "subjective eye-level framing, expressive face close-up as the emotional anchor, "
                    "warm natural light, high saturation, soft backlight rim with gentle lens flare, "
                    "practical light sources at night, cinematic 35mm look"
                ),
                "video_prompt": (
                    "smooth dolly-in toward a face, slow crane rise revealing scale, "
                    "one continuous movement per shot, wonder-and-reveal pacing, warm natural color"
                ),
                "negative_prompt": "no text, no subtitle, no watermark, no logo, no distorted face",
            },
            {
                "key": "cameron_spectacle", "name": "卡梅隆·奇观", "sort_order": 2,
                "agent_prompt": (
                    "你按「卡梅隆式」的镜头语言工作。\n"
                    "- 镜头语言：先用大尺度建立镜头交代体量与空间，再切到人的尺度做情感锚点；"
                    "机械与载具给完整运动轨迹；水下或悬浮场景用低角度平视。\n"
                    "- 光影色调：体积光与光柱，冷暖对比（青蓝环境 + 橙红光源），金属与湿润表面高光。\n"
                    "- 节奏与剪辑：情感优先于特效——每个奇观镜头之后必须给人物的反应镜头；段落内部镜头逐渐加速。\n"
                    "- 代表技法：大尺度建立镜头、机械完整轨迹、水下低角度。"
                ),
                "image_prompt": (
                    "epic establishing wide shot showing full scale, volumetric light beams, "
                    "cold blue ambience with warm orange practical light, wet metallic surfaces with "
                    "specular highlights, deep focus, high detail"
                ),
                "video_prompt": (
                    "slow low-angle push through a vast space, full mechanical trajectory in one shot, "
                    "gradual acceleration across the sequence, scale-first then human-scale beat"
                ),
                "negative_prompt": "no text, no subtitle, no watermark, no logo, no flat even lighting",
            },
            {
                "key": "nolan_realism", "name": "诺兰·实感", "sort_order": 3,
                "agent_prompt": (
                    "你按「诺兰式」的镜头语言工作。\n"
                    "- 镜头语言：大画幅实景质感（天地占比大、人物偏小），肩扛跟拍与固定机位交替；"
                    "对话戏用正反打，机位贴近人脸高度。\n"
                    "- 光影色调：硬光、低饱和、大反差；以剪影和逆光为主，夜景几乎不补光，让暗部真的暗下去。\n"
                    "- 节奏与剪辑：多线交叉剪辑，用动作与声音做转场；同一事件的不同视角并置。\n"
                    "- 代表技法：大画幅实景、交叉剪辑、剪影。"
                ),
                "image_prompt": (
                    "large-format realistic cinematography, hard directional light, low saturation, "
                    "deep contrast, silhouette against a bright background, practical real locations, "
                    "natural film grain, wide composition with small human figures"
                ),
                "video_prompt": (
                    "handheld tracking shot at human eye level, static wide framing on the same subject, "
                    "cross-cut parallel action, hard-light contrast, deliberately unpolished look"
                ),
                "negative_prompt": (
                    "no text, no subtitle, no watermark, no logo, no oversaturated color, no glossy plastic look"
                ),
            },
            {
                "key": "wong_kar_wai", "name": "王家卫·暧昧", "sort_order": 4,
                "agent_prompt": (
                    "你按「王家卫式」的镜头语言工作。\n"
                    "- 镜头语言：手持微晃与抽帧慢门制造暧昧感；大量用门框、窗框、镜子做前景遮挡；"
                    "用局部特写（手、烟、鞋、背影）替代正脸；主体常常不在画面中心。\n"
                    "- 光影色调：霓虹夜色、高对比，绿与粉撞色，暖黄钨丝灯；潮湿反光的地面与玻璃。\n"
                    "- 节奏与剪辑：慢门拖影 + 突兀跳切；画外音旁白承担时间跳跃；重复同一动作表现时间流逝。\n"
                    "- 代表技法：前景遮挡、局部特写、霓虹潮湿夜。"
                ),
                "image_prompt": (
                    "stepped motion-blur look, neon night ambience, high contrast, teal-green and magenta "
                    "color clash, warm tungsten practical light, wet reflective street and glass, "
                    "foreground obstruction, shallow depth of field, moody haze"
                ),
                "video_prompt": (
                    "handheld subtle sway, stepped slow-shutter look, foreground obstruction framing, "
                    "jump-cut rhythm, neon night color, quiet intimate pacing"
                ),
                "negative_prompt": (
                    "no text, no subtitle, no watermark, no logo, no clean bright daylight, "
                    "no centered symmetrical composition"
                ),
            },
            {
                "key": "wes_anderson", "name": "韦斯·安德森·对称", "sort_order": 5,
                "agent_prompt": (
                    "你按「韦斯·安德森式」的镜头语言工作。\n"
                    "- 镜头语言：严格正面平视、居中对称构图；90 度横摇与快速甩镜做转场；章节卡片直接进画面。\n"
                    "- 光影色调：高饱和糖果色（粉、芥末黄、湖蓝），均匀柔光，几乎无阴影；美术道具整齐排列。\n"
                    "- 节奏与剪辑：节拍规整，动作与配乐卡点；段落切换干脆利落。\n"
                    "- 代表技法：绝对对称、平移横摇、章节卡片。"
                ),
                "image_prompt": (
                    "perfectly centered symmetrical composition, front-facing flat framing, pastel candy "
                    "palette, even soft light with minimal shadow, meticulously arranged props, "
                    "dollhouse-like production design, crisp detail"
                ),
                "video_prompt": (
                    "locked-off symmetrical shot, precise 90-degree whip pan, lateral tracking with a "
                    "centered subject, beat-matched cuts, chapter card transition"
                ),
                "negative_prompt": (
                    "no watermark, no logo, no subtitle bar, no handheld shake, no harsh shadow, "
                    "no off-center cluttered framing"
                ),
            },
            {
                "key": "miyazaki_nature", "name": "宫崎骏·自然", "sort_order": 6,
                "agent_prompt": (
                    "你按「宫崎骏式」的镜头语言工作。\n"
                    "- 镜头语言：广阔自然远景与俯瞰全景；飞行、奔跑用追随镜头；"
                    "安排大量「无台词的空镜时刻」（风、云、草、水）。\n"
                    "- 光影色调：手绘水彩质感，晨昏金光，通透的蓝天白云，柔和高光，色彩干净不脏。\n"
                    "- 节奏与剪辑：呼吸感强，情绪段落留白，日常细节（吃饭、洗衣、走路）与奇观交替出现。\n"
                    "- 代表技法：自然远景、飞行追随、留白空镜。"
                ),
                "image_prompt": (
                    "hand-painted watercolor background art, vast natural landscape, golden morning or "
                    "dusk light, clean bright blue sky with soft cumulus clouds, gentle highlights, "
                    "detailed grass and foliage, nostalgic warm palette"
                ),
                "video_prompt": (
                    "slow aerial following shot over a landscape, gentle pan across nature, "
                    "wind-blown grass and drifting clouds, quiet contemplative pacing with held moments"
                ),
                "negative_prompt": (
                    "no text, no subtitle, no watermark, no logo, no photorealistic 3D render, no gritty dark tone"
                ),
            },
            {
                "key": "tarantino_tension", "name": "昆汀·张力", "sort_order": 7,
                "agent_prompt": (
                    "你按「昆汀式」的镜头语言工作。\n"
                    "- 镜头语言：长镜头对峙（低机位、环绕）、极端特写（脚、眼睛、枪口、食物）、突然的推近；"
                    "章节式叙事分隔。\n"
                    "- 光影色调：顶光与硬阴影，暖黄室内色调，高对比，胶片颗粒质感。\n"
                    "- 节奏与剪辑：长对话蓄势 → 瞬间爆发；配乐与场景情绪故意错位；非线性章节。\n"
                    "- 代表技法：低机位对峙、极端特写、章节分隔。"
                ),
                "image_prompt": (
                    "low-angle standoff framing, extreme close-up detail inserts, top light with hard "
                    "shadows, warm amber interior, high contrast, coarse film grain, 70s exploitation "
                    "cinema look"
                ),
                "video_prompt": (
                    "slow low-angle circular dolly around a standoff, sudden snap zoom to an extreme "
                    "close-up, held long take, stillness that builds tension then abrupt motion"
                ),
                "negative_prompt": (
                    "no text, no subtitle, no watermark, no logo, no soft flat lighting, no clean modern digital look"
                ),
            },
            {
                "key": "villeneuve_silence", "name": "维伦纽瓦·静默", "sort_order": 8,
                "agent_prompt": (
                    "你按「维伦纽瓦式」的镜头语言工作。\n"
                    "- 镜头语言：大尺度静默空镜、剪影群像、缓慢推近；人物常被巨大的环境或结构压住；极简构图。\n"
                    "- 光影色调：单色低饱和（沙金、灰蓝、雾白），沙尘与雾气弥漫，逆光剪影，几乎不用暖色点缀。\n"
                    "- 节奏与剪辑：极慢节奏，声音承担叙事；长时间静止之后只给一次移动。\n"
                    "- 代表技法：静默空镜、剪影、缓慢推近。"
                ),
                "image_prompt": (
                    "monumental silent wide shot, monolithic structure dwarfing tiny human silhouettes, "
                    "monochrome low saturation, sand-dust or fog haze, backlit silhouette, minimal "
                    "composition, heavy atmosphere"
                ),
                "video_prompt": (
                    "very slow push-in on a vast landscape, silhouetted figures held in long shot, "
                    "dust haze drifting, minimal movement, sound-driven stillness"
                ),
                "negative_prompt": (
                    "no text, no subtitle, no watermark, no logo, no bright saturated color, no fast-cut energy"
                ),
            },
            {
                "key": "guoman", "name": "国漫", "sort_order": 9,
                "agent_prompt": (
                    "你按「国漫」的镜头语言工作。\n"
                    "- 镜头语言：写意的大幅度运动镜头（冲天而上、横扫千军）；打斗讲究招式连贯与留白；"
                    "人物登场给仰拍。\n"
                    "- 光影色调：明亮的赛璐璐上色，边缘描边高光，冷暖对比强，法术与灵力用发光渐变。\n"
                    "- 节奏与剪辑：快慢交替，命中的一击用慢镜，收势用定帧。\n"
                    "- 代表技法：写意大幅运镜、发光特效、定帧收势。"
                ),
                "image_prompt": (
                    "chinese animation cel-shaded style, crisp ink outline, bright saturated color, "
                    "glowing energy effects, dynamic action pose, clean painted background, dramatic rim light"
                ),
                "video_prompt": (
                    "large sweeping camera move, dynamic action with clear pose-to-pose flow, "
                    "slow motion on impact then a snap freeze, glowing energy trails"
                ),
                "negative_prompt": (
                    "no text, no subtitle, no watermark, no logo, no photorealistic skin, no western cartoon proportions"
                ),
            },
            {
                "key": "cyberpunk", "name": "赛博朋克", "sort_order": 10,
                "agent_prompt": (
                    "你按「赛博朋克」的镜头语言工作。\n"
                    "- 镜头语言：低角度仰拍高楼与广告牌，穿过雨幕和蒸汽的推轨，第一人称穿行；"
                    "镜头允许轻微畸变。\n"
                    "- 光影色调：青蓝与洋红霓虹，雨夜湿地反光，全息广告投影，暗部浓重。\n"
                    "- 节奏与剪辑：电子节拍驱动剪辑，信息密集的蒙太奇，偶尔来一次突然静音。\n"
                    "- 代表技法：霓虹雨夜、全息投影、低角度仰拍。"
                ),
                "image_prompt": (
                    "cyberpunk city on a rainy night, cyan and magenta neon signage, holographic "
                    "advertisements, wet asphalt reflections, steam vents, dense dark shadow, "
                    "slight lens distortion, retro-futuristic dystopian production design"
                ),
                "video_prompt": (
                    "low-angle push along a neon street, rain and steam drifting, first-person traversal, "
                    "electronic beat-matched cuts, one sudden silence pause"
                ),
                "negative_prompt": (
                    "no text, no subtitle, no watermark, no logo, no daylight rural scene, no pastel color"
                ),
            },
            {
                "key": "ink_wash", "name": "水墨", "sort_order": 11,
                "agent_prompt": (
                    "你按「水墨」的镜头语言工作。\n"
                    "- 镜头语言：横向长卷式横移，大面积留白，主体偏安一隅；用雾与水的流动代替运镜。\n"
                    "- 光影色调：黑白灰墨阶，朱红或石青单点设色；宣纸纹理，墨色晕染边缘。\n"
                    "- 节奏与剪辑：呼吸般的缓慢节拍，笔画落下对应一次镜头切换。\n"
                    "- 代表技法：长卷横移、大面积留白、单点设色。"
                ),
                "image_prompt": (
                    "chinese ink wash painting, sumi-e brush texture, rice paper grain, vast negative "
                    "space, sparse composition, black and grey ink with a single vermilion accent, "
                    "ink bleed edges, misty mountain"
                ),
                "video_prompt": (
                    "slow lateral scroll like unrolling a handscroll, ink bleeding into water, "
                    "drifting mist, minimal movement, brush-stroke cut transitions"
                ),
                "negative_prompt": (
                    "no text, no subtitle, no watermark, no logo, no photographic texture, no heavy saturated color"
                ),
            },
            {
                "key": "handheld_doc", "name": "纪实手持", "sort_order": 12,
                "agent_prompt": (
                    "你按「纪实」的镜头语言工作。\n"
                    "- 镜头语言：手持跟拍，允许偶尔脱焦再对焦，越过肩膀的观察视角；"
                    "不做精致构图，抓拍感优先。\n"
                    "- 光影色调：自然可用光，不补光；轻微欠曝与噪点，白平衡不追求精准。\n"
                    "- 节奏与剪辑：长镜头跟随，保留真实的停顿与口误；剪辑不做卡点。\n"
                    "- 代表技法：手持跟拍、抓拍构图、自然光。"
                ),
                "image_prompt": (
                    "documentary handheld look, available natural light, slightly underexposed with "
                    "sensor noise, imperfect candid framing, realistic skin texture, no studio lighting"
                ),
                "video_prompt": (
                    "handheld follow shot with a breathing camera, occasional focus hunting, "
                    "long uninterrupted take, no music-video pacing"
                ),
                "negative_prompt": (
                    "no text, no subtitle, no watermark, no logo, no studio lighting, "
                    "no glossy color grading, no perfect symmetry"
                ),
            },
        ],
    )
)
