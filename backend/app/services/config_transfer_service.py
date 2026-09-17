"""配置导入导出：把「配置注册制」里的数据装进一个 JSON 快照，搬到另一份安装上。

开源场景里用户最想分享的是提示词库、导演风格、创作 Agent 与服务商预设——
它们全都在数据库里（见 `registry/schema_registry.py` 的 SCHEMA_REGISTRY），
所以「导出 / 导入」本质上就是「按注册表把行搬出去、再按注册表把行搬回来」。

三条产品底线（改这个文件时不要越过）：

**1. 绝不导出密钥。**
模型服务的 API Key 存在 `provider_services.api_key_enc`，用 `data/secret.key`
加密，而 secret.key 是**每台机器各自生成**的：把密文搬到另一台机器上根本解不开
（`security.decrypt` 遇到别的机器的密文只会返回空串）。所以导出它既没有用
又纯粹是泄漏面。导出侧只写 `name / kind / base_url / enabled / sort_order / models`，
连「到底有没有 Key」这个布尔都不写；导入侧收到 `api_key_enc` 或 `api_key`
就整行拒收（见 `FORBIDDEN_SERVICE_FIELDS`）——哪怕有人手工把 Key 塞进快照里分享，
本程序也不会拿它去用。

**2. 本机相关参数一律不带走。**
绝对路径、本机地址、跟安装位置 / 端口 / 数据目录绑定的值，换台机器要么失效、
要么指向错的东西。排除表是**写死的两张表**（`MACHINE_BOUND_CONFIG_KEYS` 与
`LOCAL_HOSTS`），不靠正则猜——正则猜出来的结论「今天不漏、明天漏」，
而且用户看不出到底漏了什么。表里每一项都写清了判断依据，并会出现在
导出响应的 `excluded` 里，用户看得见「什么被丢下了、为什么」。
（正则只用于**额外提醒**：见 `_machine_hint`，它只产生 warnings，不做排除决策。）

**3. 导入先预览、再写库；写库必须留痕。**
结构与字段都按 SCHEMA_REGISTRY 校验：只写声明过的字段、类型按 FieldSpec 转换、
必填字段缺了就拒收那一行。脏数据只影响它自己那一行（记进 `conflicts`），
不让整个接口 500。写库复用 `config_center_service` 的 create_row / update_row，
于是每一行都自动落一条审计（actor=import），可以按行回滚。

导出格式（schemaVersion = 1）::

    {
      "format": "xiaoma-config",
      "schemaVersion": 1,
      "appVersion": "1.1.6",
      "exportedAt": "2026-09-17T12:00:00+08:00",
      "scopes": ["settings", "prompts", "styles", "agents", "presets", "services"],
      "tables": {
        "prompts":      [{"key": "...", "title": "...", "content": "..."}],
        "config_items": [{"key": "app.name", "value": "小马AI工坊"}]
      },
      "excluded": [{"table": "config_items", "field": "value", "reason": "..."}],
      "warnings": ["..."],
      "notes":    ["..."],
      "containsSecrets": false
    }

导入用同一个结构，可以额外带 `mode: "merge"`（目前只支持合并）与
`scopes: [...]`（本次只想导入的子集，前端勾选后覆写）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.models import ProviderService
from app.registry import adapters
from app.registry.schema_registry import SCHEMA_REGISTRY, FieldSpec, TableSpec
from app.services import config_center_service as cc
from app.services import provider_store

# 快照的格式标识与版本：认不出就拒绝，别硬着头皮读
FORMAT = "xiaoma-config"
SCHEMA_VERSION = 1
MODE_MERGE = "merge"
IMPORT_ACTOR = "import"
# 单表行数上限：防止有人把几十万行的文件丢进来把接口拖死
MAX_ROWS_PER_TABLE = 5000


# ============================================================
# 范围（scopes）：一次导出/导入包含哪些表
# ============================================================
#
# default=True 的那几个就是「用户资产」——提示词库、导演风格、创作 Agent、
# 服务商预设、模型服务，外加站点配置。参数档位 / 能力类型 / 导航菜单
# 更偏「安装本身的骨架」，默认不带，但用户想整机复制时可以自己勾上。


@dataclass(frozen=True)
class ScopeSpec:
    name: str
    label: str
    description: str
    tables: tuple[str, ...]
    default: bool = False


SCOPES: tuple[ScopeSpec, ...] = (
    ScopeSpec("settings", "系统配置", "站点名称、默认值、限制、模块开关", ("config_items",), True),
    ScopeSpec("presets", "服务商预设", "添加模型服务时的一键填充模板", ("provider_presets",), True),
    ScopeSpec("prompts", "提示词库", "可复用的提示词模板", ("prompts",), True),
    ScopeSpec("agents", "创作 Agent", "自动链各阶段的系统提示词与模板", ("agent_prompts",), True),
    ScopeSpec("styles", "导演风格", "风格卡（镜头语言 / 光影 / 提示词片段）", ("director_styles",), True),
    ScopeSpec("services", "模型服务", "服务地址与模型清单（不含 Key）", ("provider_services",), True),
    ScopeSpec("options", "参数选项", "尺寸 / 时长 / 比例 / 清晰度等档位", ("param_options",)),
    ScopeSpec("modalities", "能力类型", "文本 / 图片 / 视频等能力定义", ("modalities",)),
    ScopeSpec("nav", "导航菜单", "侧边栏入口与显示顺序", ("nav_items",)),
)

_SCOPE_BY_NAME = {s.name: s for s in SCOPES}
DEFAULT_SCOPES: tuple[str, ...] = tuple(s.name for s in SCOPES if s.default)
# 非注册表管理的表（key 是表名，值是展示名）：模型服务是加密存储的，
# 走 providers 那套接口，不进配置注册表，但它同样值得跟着快照走。
EXTRA_TABLES: dict[str, str] = {"provider_services": "模型服务"}
ALL_TABLES: tuple[str, ...] = tuple(
    t for s in SCOPES for t in s.tables
) + tuple(EXTRA_TABLES)


def table_label(table: str) -> str:
    spec = SCHEMA_REGISTRY.get(table)
    return spec.label if spec is not None else EXTRA_TABLES.get(table, table)


def scope_meta() -> dict[str, Any]:
    """范围清单：前端的多选框、说明文字都由这里驱动，加一个范围不用改前端。"""
    return {
        "format": FORMAT,
        "schemaVersion": SCHEMA_VERSION,
        "scopes": [
            {
                "name": s.name,
                "label": s.label,
                "description": s.description,
                "default": s.default,
                "tables": [{"name": t, "label": table_label(t)} for t in s.tables],
            }
            for s in SCOPES
        ],
        "defaultScopes": list(DEFAULT_SCOPES),
        "excluded": list(STATIC_EXCLUSIONS),
        "containsSecrets": False,
        "notes": list(EXPORT_NOTES),
    }


def resolve_scopes(raw: str | None) -> list[str]:
    """把 `scopes=settings,prompts` 解析成范围名列表；空 → 默认范围。"""
    if raw is None or not str(raw).strip():
        return list(DEFAULT_SCOPES)
    names: list[str] = []
    for piece in str(raw).replace("|", ",").split(","):
        name = piece.strip()
        if not name:
            continue
        if name not in _SCOPE_BY_NAME:
            raise cc.ConfigError(
                f"不认识的导出范围「{name}」，可用范围：{'、'.join(_SCOPE_BY_NAME)}"
            )
        if name not in names:
            names.append(name)
    return names or list(DEFAULT_SCOPES)


def _normalize_scope_names(value: Any) -> list[str] | None:
    """快照 / 请求体里的 scopes：支持数组与逗号串；不认识的范围直接拒绝。"""
    if value is None:
        return None
    if isinstance(value, str):
        raw = [p.strip() for p in value.split(",")]
    elif isinstance(value, (list, tuple)):
        raw = [str(p).strip() for p in value]
    else:
        raise cc.ConfigError("scopes 需要是数组或逗号分隔的字符串")
    names: list[str] = []
    for name in raw:
        if not name:
            continue
        if name not in _SCOPE_BY_NAME:
            raise cc.ConfigError(
                f"快照里出现了本程序不认识的导出范围「{name}」，"
                f"可用范围：{'、'.join(_SCOPE_BY_NAME)}"
            )
        if name not in names:
            names.append(name)
    return names


def _tables_of(scopes: list[str]) -> list[str]:
    tables: list[str] = []
    for name in scopes:
        for t in _SCOPE_BY_NAME[name].tables:
            if t not in tables:
                tables.append(t)
    return tables


# ============================================================
# 排除表一：本机相关的配置键（写死的，不靠正则猜）
# ============================================================
#
# 查证方式：把 `config_items` 里现有的键逐条对着「是不是跟这台机器绑定」看一遍
# （`select key, value_type, value_json from config_items`，当前种子共 37 个键），
# 结论是下面这几项。其余键（app.* / modules.* / safety.* / limits 里的数值项 /
# update.repo*）都是可搬运的偏好设置，照常导出。

MACHINE_BOUND_CONFIG_KEYS: dict[str, str] = {
    # 值是「可执行文件的绝对路径」，也可能填的是 PATH 里的命令名；
    # 换机器后路径不一定还在（Windows 上尤其如此），新机器留空让程序自动探测更稳。
    "limits.ffmpeg_path": "本机 ffmpeg 可执行文件的位置（绝对路径），换机器后大概率失效；新机器留空让程序自动探测",
    # 「服务ID:模型名」里的服务ID 是 provider_services 的自增主键，是本机编号。
    # 把它搬过去，等于让新机器按别人的编号去找服务：编号撞上了会静默指向别的服务
    # （可能还是收费的），编号不存在则一选就报错。模型清单本身随 services 一起带走，
    # 用户在新机器上重选一次即可。
    "defaults.text_model": "值形如「服务ID:模型名」，服务ID 是本机自增编号，换一份安装会指向别的服务",
    "defaults.image_model": "值形如「服务ID:模型名」，服务ID 是本机自增编号，换一份安装会指向别的服务",
    "defaults.video_model": "值形如「服务ID:模型名」，服务ID 是本机自增编号，换一份安装会指向别的服务",
    # 代码在容器里会写这个键（update_service 读它判断安装形态），属于环境探测结果。
    # 当前种子里没有它，但排除表按 key 写，一旦出现就排除。
    "update.in_docker": "本机运行环境探测结果（是否跑在容器里），跟这台机器的安装方式绑定",
}

# ============================================================
# 排除表二：本机地址（同样写死，不用模糊正则）
# ============================================================
#
# 指向这些地址的模型服务，在别人的机器上要么没人监听、要么监听的是**另一个人的**
# 服务（本地网关常常不带鉴权），所以整个服务行都不导出。
# 新机器上如果也装了同样的本地服务，用「粘贴 Key 一键接入」或本机 Ollama 检测
# 重新加一次就行——那本来就是这个程序给本地服务准备的入口。

LOCAL_HOSTS: frozenset[str] = frozenset(
    {"127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal"}
)

# ============================================================
# 排除表三：整行/字段级的固定说明（安全红线 + 本机编号）
# ============================================================

STATIC_EXCLUSIONS: tuple[dict[str, str], ...] = (
    {
        "table": "provider_services",
        "field": "api_key_enc",
        "reason": "API Key 的密文。密钥在每台机器的 data/secret.key 里，密文搬到别的机器解不开；"
        "而且密钥本来就不该跟着配置文件走。导出侧不写，导入侧也拒收。",
    },
    {
        "table": "provider_services",
        "field": "api_key",
        "reason": "明文 Key 的字段名。导出侧不写、导入侧拒收，避免有人手工把 Key 塞进快照里分享出去。",
    },
    {
        "table": "*",
        "field": "id",
        "reason": "自增主键只是本机数据库的行号，换一份安装没有意义；导入时按各表的唯一标识匹配。",
    },
    {
        "table": "*",
        "field": "version",
        "reason": "乐观锁版本号随本机修改次数变化，跨机器比对没有意义；导入时只做内容比对。",
    },
)

# 导入侧绝不接受的字段（出现即拒收该行）
FORBIDDEN_SERVICE_FIELDS: tuple[str, ...] = ("api_key_enc", "api_key", "has_api_key")

# 导出响应的 notes：把「排除了什么、为什么」直接写在快照里，别人打开文件也看得懂
EXPORT_NOTES: tuple[str, ...] = (
    "不含任何 API Key：provider_services 会导出名称、协议、地址、模型清单，"
    "但 api_key_enc 与明文 api_key 既不导出、也不接受导入。",
    "本机相关参数固定排除（写死的排除表）："
    + "、".join(sorted(MACHINE_BOUND_CONFIG_KEYS))
    + "；指向本机地址（"
    + "、".join(sorted(LOCAL_HOSTS))
    + "）的模型服务整行跳过。",
    "每行的 id 与 version 不导出：它们是本机数据库的编号，导入时按各表的唯一标识合并。",
    "快照只包含配置本身（提示词、风格、Agent、预设、服务、站点配置），"
    "不包含作品、任务、资产与聊天记录。",
)

# 唯一标识（导入的「按什么去重 / 合并」表）。与 config_center_service.ensure_seed
# 的种子判定保持一致：key 优先，其次 (kind, value)。
TABLE_IDENTITY: dict[str, tuple[str, ...]] = {
    "config_items": ("key",),
    "param_options": ("kind", "value"),
    "modalities": ("key",),
    "nav_items": ("key",),
    "provider_presets": ("key",),
    "prompts": ("key",),
    "agent_prompts": ("key",),
    "director_styles": ("key",),
    # 服务没法用 key 认（表里没有 key 字段），地址相同即视为同一个服务——
    # 这正是 provider_store.find_by_base_url 的既有判定，保持一致。
    "provider_services": ("base_url",),
}


# ============================================================
# 小工具
# ============================================================


def _is_local_url(url: str) -> bool:
    """地址是否指向本机。没有 scheme 的写法（127.0.0.1:11434/v1）也算。"""
    raw = (url or "").strip()
    if not raw:
        return False
    if "://" not in raw:
        raw = "http://" + raw
    try:
        host = (urlsplit(raw).hostname or "").strip().lower()
    except ValueError:
        return False
    return host in LOCAL_HOSTS


_MACHINE_HINT_RE = re.compile(
    r"(?i)(?:https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?)"
    r"|(?:[A-Za-z]:\\[^\s\"'，。]+)"
    r"|(?:/(?:home|Users|root|tmp|var|opt|mnt|srv)/[^\s\"'，。]+)"
)

_MACHINE_HINT_LABEL = {
    "http": "本机地址",
    "win": "Windows 绝对路径",
    "posix": "类 Unix 绝对路径",
}


def _machine_hint(text: str) -> str:
    """值看起来像本机地址 / 绝对路径时给出提示词。

    注意：**这里只用来提醒，不做排除决策**。排除只认上面的三张表——
    正则能认出来的东西正则也一定能漏掉，把「排不排除」交给它等于把风险交给运气。
    但用户自己加的配置键我们无法预先列举，提醒一句总比默默带走好。
    """
    match = _MACHINE_HINT_RE.search(text or "")
    if not match:
        return ""
    hit = match.group(0)
    if hit[:4].lower() == "http":
        return _MACHINE_HINT_LABEL["http"]
    if re.match(r"[A-Za-z]:\\", hit):
        return _MACHINE_HINT_LABEL["win"]
    return _MACHINE_HINT_LABEL["posix"]


def _canon(value: Any) -> Any:
    """把值规范化成可比较的形状（dict 键排序），避免顺序差异被当成「有改动」。"""
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, list):
        return [_canon(v) for v in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return value if isinstance(value, str) else str(value)


def _strip_local_fields(data: dict[str, Any]) -> dict[str, Any]:
    """去掉本机编号：id 与 version 都不进快照。"""
    return {k: v for k, v in data.items() if k not in ("id", "version")}


# ============================================================
# 导出
# ============================================================


async def export_snapshot(db: AsyncSession, scopes: str | None = None) -> dict[str, Any]:
    """按范围导出快照。返回值就是给用户下载的那个 JSON。"""
    names = resolve_scopes(scopes)
    tables: dict[str, list[dict[str, Any]]] = {}
    excluded: list[dict[str, str]] = [dict(e) for e in STATIC_EXCLUSIONS]
    warnings: list[str] = []

    for table in _tables_of(names):
        if table == "provider_services":
            rows, more_excluded, more_warnings = await _export_services(db)
        else:
            rows, more_excluded, more_warnings = await _export_registered(db, SCHEMA_REGISTRY[table])
        tables[table] = rows
        excluded.extend(more_excluded)
        warnings.extend(more_warnings)

    return {
        "format": FORMAT,
        "schemaVersion": SCHEMA_VERSION,
        "appVersion": __version__,
        "exportedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scopes": names,
        "tables": tables,
        "excluded": excluded,
        "warnings": warnings,
        "notes": list(EXPORT_NOTES),
        # 一句话把「这份文件里有没有密钥」钉死，前端也拿它显示「不含 API Key」
        "containsSecrets": False,
    }


async def _export_registered(
    db: AsyncSession, spec: TableSpec
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
    rows = await cc.list_rows(db, spec)
    out: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    warnings: list[str] = []

    for row in rows:
        clean = _strip_local_fields(row)
        if spec.name == "config_items":
            key = str(clean.get("key") or "")
            reason = MACHINE_BOUND_CONFIG_KEYS.get(key)
            if reason:
                excluded.append(
                    {"table": spec.name, "field": "value", "reason": f"本机参数「{key}」：{reason}"}
                )
                continue
            if clean.get("is_secret"):
                # 标了敏感的行（用户自建的令牌之类）不该跟着快照外发
                excluded.append(
                    {
                        "table": spec.name,
                        "field": "*",
                        "reason": f"「{key}」被标记为敏感项（is_secret），整行不导出",
                    }
                )
                continue
            hint = _machine_hint(str(clean.get("value") or ""))
            if hint:
                warnings.append(
                    f"「{key}」的值看起来是{hint}，已照原样导出；换到别的机器上请确认是否还可用"
                )
        out.append(clean)
    return out, excluded, warnings


async def _export_services(
    db: AsyncSession,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
    out: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    warnings: list[str] = []
    skipped_local: list[str] = []

    for row in await provider_store.list_services(db):
        if _is_local_url(row.base_url):
            skipped_local.append(f"{row.name}（{row.base_url}）")
            excluded.append(
                {
                    "table": "provider_services",
                    "field": "*",
                    "reason": f"「{row.name}」的地址 {row.base_url} 指向本机，"
                    "换机器后不是失效就是连到别人的服务，整行不导出；"
                    "新机器上请重新接入本机服务",
                }
            )
            continue
        out.append(
            {
                "name": row.name,
                "kind": row.kind,
                "base_url": row.base_url,
                "enabled": bool(row.enabled),
                "sort_order": int(row.sort_order or 0),
                # 模型清单原样带走：它是分享的重点（对方要的正是「你都在用哪些模型」）
                "models": [m.model_dump() for m in provider_store.parse_models(row.models_json)],
            }
        )

    if skipped_local:
        warnings.append(
            "以下模型服务指向本机地址，未导出：" + "、".join(skipped_local)
        )
    return out, excluded, warnings


# ============================================================
# 导入：结构校验 → 逐行计划（dry-run 到这一步为止）→ 落库
# ============================================================


@dataclass
class _Plan:
    """一行将要做的事。dry-run 只产出它，不落库。"""

    table: str
    action: str  # create | update | skip
    payload: dict[str, Any]
    row_id: int | None = None
    identity: str = ""


# 动作用动词（计划里），计数用过去分词（响应里）——分开写，免得两处各叫各的
_COUNTER_OF = {"create": "created", "update": "updated", "skip": "skipped"}


def _empty_summary() -> dict[str, dict[str, int]]:
    return {}


def _bump(summary: dict[str, dict[str, int]], table: str, action: str) -> None:
    bucket = summary.setdefault(table, {"created": 0, "updated": 0, "skipped": 0})
    bucket[_COUNTER_OF[action]] += 1


def _read_envelope(snapshot: Any) -> tuple[list[str] | None, dict[str, Any]]:
    """校验最外层的 format / schemaVersion / mode / tables，返回(范围, 表集合)。"""
    if not isinstance(snapshot, dict):
        raise cc.ConfigError("导入内容必须是一个 JSON 对象（就是导出的那份文件本身）")

    fmt = str(snapshot.get("format") or "").strip()
    if fmt != FORMAT:
        raise cc.ConfigError(
            f"这不是本程序的配置快照：format 读到的是「{fmt or '空'}」，"
            f"应该是「{FORMAT}」。请确认选对了文件。"
        )

    raw_version = snapshot.get("schemaVersion")
    try:
        version = int(raw_version)
    except (TypeError, ValueError) as e:
        raise cc.ConfigError(
            f"快照缺少可识别的 schemaVersion（读到「{raw_version}」），无法确认结构是否兼容"
        ) from e
    if version < 1:
        raise cc.ConfigError(f"schemaVersion={version} 不合法（从 1 开始）")
    if version > SCHEMA_VERSION:
        raise cc.ConfigError(
            f"快照的 schemaVersion={version} 高于当前程序支持的 {SCHEMA_VERSION}，"
            f"它可能来自更新版本的程序。请先升级本程序，或用当前版本重新导出后再导入。"
        )

    mode = str(snapshot.get("mode") or MODE_MERGE).strip().lower()
    if mode != MODE_MERGE:
        raise cc.ConfigError(
            f"不支持的导入模式「{mode}」：目前只支持 {MODE_MERGE}"
            "（按唯一标识合并，只新增与更新，绝不删除你已有的数据）"
        )

    tables = snapshot.get("tables")
    if not isinstance(tables, dict):
        raise cc.ConfigError("快照里没有 tables 字段（或它不是对象），无法确定要导入哪些配置表")
    if not tables:
        raise cc.ConfigError("快照的 tables 是空的，没有可导入的内容")

    unknown = [t for t in tables if t not in SCHEMA_REGISTRY and t not in EXTRA_TABLES]
    if unknown:
        listed = "、".join(str(t) for t in unknown)
        known = "、".join(sorted(list(SCHEMA_REGISTRY) + list(EXTRA_TABLES)))
        raise cc.ConfigError(
            f"快照里有本程序不认识的配置表「{listed}」。本程序认识的是：{known}。"
            "如果这份快照来自更新版本的程序，请先升级本程序。"
        )
    return _normalize_scope_names(snapshot.get("scopes")), tables


def _identity_fields(table: str) -> tuple[str, ...]:
    known = TABLE_IDENTITY.get(table)
    if known:
        return known
    # 未来新注册的表没写进 TABLE_IDENTITY 时，按种子同款规则兜底，不让导入直接崩
    spec = SCHEMA_REGISTRY.get(table)
    names = [f.name for f in spec.fields] if spec is not None else []
    if "key" in names:
        return ("key",)
    if "kind" in names and "value" in names:
        return ("kind", "value")
    return (names[0],) if names else ("key",)


def _identity_of(payload: dict[str, Any], fields: tuple[str, ...]) -> tuple[str, str] | None:
    """算出唯一标识；缺字段或空值 → None（调用方记为冲突）。"""
    parts: list[str] = []
    for name in fields:
        raw = payload.get(name)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        parts.append(str(raw).strip())
    return "|".join(parts), "、".join(parts)


def _coerce_kv_value(value: Any, value_type: str) -> Any:
    """config_items 的值按 value_type 转换（前端设置页也是同一套规则）。"""
    kind = (value_type or "string").strip()
    if kind == "json":
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError as e:
                raise cc.ConfigError("值声明为 JSON，但内容不是合法 JSON") from e
        return value
    if kind in ("bool", "int", "float"):
        return cc._coerce(FieldSpec("value", "值", kind), value)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _normalize_registered_payload(
    spec: TableSpec, raw: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """按注册表把快照里的一行整理成可落库的 API 载荷。

    只保留 SCHEMA_REGISTRY 声明过的字段（未知字段忽略并回一条提示），
    类型按 FieldSpec 转换，转换失败抛 ConfigError（调用方记成该行的冲突）。
    """
    declared = {f.name for f in spec.fields}
    payload: dict[str, Any] = {}
    unknown = [k for k in raw if k not in declared and k != "value"]

    for f in spec.fields:
        if f.readonly:
            continue
        if spec.name in cc._KV_VALUE_TABLES and f.name == cc._KV_FIELD:
            # 键值表的库内字段是 value_json，API 字段是 value。
            # 导出只写 value；手工拼的快照若写的是 value_json，也认（它同样是声明过的字段）。
            if cc._KV_API_FIELD in raw:
                raw_value = raw[cc._KV_API_FIELD]
            elif f.name in raw:
                raw_value = raw[f.name]
            else:
                continue
            value_type = str(raw.get("value_type") or "").strip()
            payload[cc._KV_API_FIELD] = _coerce_kv_value(raw_value, value_type)
            continue
        if f.name not in raw:
            continue
        payload[f.name] = cc._coerce(f, raw[f.name])

    return payload, unknown


def _required_missing(spec: TableSpec, payload: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for f in spec.fields:
        if not f.required:
            continue
        if spec.name in cc._KV_VALUE_TABLES and f.name == cc._KV_FIELD:
            continue
        value = payload.get(f.name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(f.label or f.name)
    return missing


def _existing_shape(spec: TableSpec, obj: Any) -> dict[str, Any]:
    return _strip_local_fields(cc.serialize_row(spec, obj))


def _is_unchanged(existing: dict[str, Any], payload: dict[str, Any]) -> bool:
    """只比对快照里出现过的字段：一样就跳过，避免重复导入刷版本号与审计。"""
    for key, value in payload.items():
        if _canon(existing.get(key)) != _canon(value):
            return False
    return True


def _service_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """整理 provider_services 的一行（不含任何密钥字段）。"""
    models_raw = raw.get("models")
    if models_raw is None:
        models: list[dict[str, Any]] = []
    elif isinstance(models_raw, list):
        models = []
        for item in models_raw:
            if not isinstance(item, dict):
                raise cc.ConfigError("models 里每一项都必须是对象（形如 {name, modality, label}）")
            models.append(
                {
                    "name": str(item.get("name") or "").strip(),
                    "modality": str(item.get("modality") or "").strip(),
                    "label": str(item.get("label") or ""),
                }
            )
    else:
        raise cc.ConfigError("models 需要是数组")

    return {
        "name": str(raw.get("name") or "").strip(),
        "kind": str(raw.get("kind") or "openai").strip() or "openai",
        "base_url": str(raw.get("base_url") or "").strip().rstrip("/"),
        "enabled": cc._coerce(FieldSpec("enabled", "启用", "bool"), raw.get("enabled", True)),
        "sort_order": cc._coerce(FieldSpec("sort_order", "排序", "int"), raw.get("sort_order", 0)),
        "models": [m for m in models if m["name"] and m["modality"]],
    }


def _service_shape(row: ProviderService) -> dict[str, Any]:
    """服务的可比对形状 + 审计用形状。

    刻意**不包含** api_key_enc：审计日志里也不该出现密钥（哪怕是密文）。
    """
    return {
        "name": row.name,
        "kind": row.kind,
        "base_url": (row.base_url or "").rstrip("/"),
        "enabled": bool(row.enabled),
        "sort_order": int(row.sort_order or 0),
        "models": [m.model_dump() for m in provider_store.parse_models(row.models_json)],
    }


async def _plan_registered_table(
    db: AsyncSession,
    spec: TableSpec,
    rows: list[Any],
    summary: dict[str, dict[str, int]],
    conflicts: list[dict[str, Any]],
    warnings: list[str],
) -> list[_Plan]:
    existing_rows = list((await db.execute(select(spec.model))).scalars().all())
    fields = _identity_fields(spec.name)
    index: dict[str, Any] = {}
    for obj in existing_rows:
        ident = _identity_of(_existing_shape(spec, obj), fields)
        if ident:
            index[ident[0]] = obj

    plans: list[_Plan] = []
    seen: dict[str, int] = {}

    for position, raw in enumerate(rows, 1):
        where = f"{spec.label}第 {position} 行"
        if not isinstance(raw, dict):
            conflicts.append(
                {"table": spec.name, "row": position, "identity": "", "reason": f"{where}不是一个对象，已跳过"}
            )
            continue
        try:
            payload, unknown = _normalize_registered_payload(spec, raw)
        except cc.ConfigError as e:
            conflicts.append(
                {"table": spec.name, "row": position, "identity": "", "reason": f"{where}：{e.message}"}
            )
            continue
        if unknown:
            warnings.append(f"{where}有本程序不认识的字段（{'、'.join(unknown)}），已忽略")

        missing = _required_missing(spec, payload)
        if missing:
            conflicts.append(
                {
                    "table": spec.name,
                    "row": position,
                    "identity": "",
                    "reason": f"{where}缺少必填字段：{'、'.join(missing)}",
                }
            )
            continue

        ident = _identity_of(payload, fields)
        if ident is None:
            conflicts.append(
                {
                    "table": spec.name,
                    "row": position,
                    "identity": "",
                    "reason": f"{where}没有唯一标识（{'、'.join(fields)}），无法判断该新增还是更新",
                }
            )
            continue
        key, shown = ident
        if key in seen:
            conflicts.append(
                {
                    "table": spec.name,
                    "row": position,
                    "identity": shown,
                    "reason": f"{where}与快照第 {seen[key]} 行的标识重复（{shown}），已跳过",
                }
            )
            continue
        seen[key] = position

        obj = index.get(key)
        if obj is None:
            action = "create"
        elif _is_unchanged(_existing_shape(spec, obj), payload):
            action = "skip"
        else:
            action = "update"
        _bump(summary, spec.name, action)
        plans.append(
            _Plan(
                table=spec.name,
                action=action,
                payload=payload,
                row_id=getattr(obj, "id", None),
                identity=shown,
            )
        )
    return plans


async def _plan_services_table(
    db: AsyncSession,
    rows: list[Any],
    summary: dict[str, dict[str, int]],
    conflicts: list[dict[str, Any]],
    warnings: list[str],
) -> list[_Plan]:
    existing = {
        (row.base_url or "").strip().rstrip("/"): row
        for row in await provider_store.list_services(db)
    }
    known_kinds = {k["kind"] for k in adapters.list_kinds()}
    plans: list[_Plan] = []
    seen: dict[str, int] = {}

    for position, raw in enumerate(rows, 1):
        where = f"模型服务第 {position} 行"
        if not isinstance(raw, dict):
            conflicts.append(
                {"table": "provider_services", "row": position, "identity": "", "reason": f"{where}不是一个对象，已跳过"}
            )
            continue

        # 安全红线：密钥字段一律不接受（导出的文件里本来就不该有）
        leaked = [k for k in FORBIDDEN_SERVICE_FIELDS if k in raw]
        if leaked:
            conflicts.append(
                {
                    "table": "provider_services",
                    "row": position,
                    "identity": str(raw.get("name") or ""),
                    "reason": f"{where}带有密钥字段（{'、'.join(leaked)}），已整行拒收。"
                    "API Key 在每台机器上各自加密，搬过来也用不了；请在新机器上重新填写。",
                }
            )
            continue

        try:
            payload = _service_payload(raw)
        except cc.ConfigError as e:
            conflicts.append(
                {"table": "provider_services", "row": position, "identity": "", "reason": f"{where}：{e.message}"}
            )
            continue

        if not payload["name"]:
            conflicts.append(
                {"table": "provider_services", "row": position, "identity": "", "reason": f"{where}缺少服务名称"}
            )
            continue
        if not payload["base_url"]:
            conflicts.append(
                {
                    "table": "provider_services",
                    "row": position,
                    "identity": payload["name"],
                    "reason": f"{where}缺少接口地址（base_url）",
                }
            )
            continue
        if payload["kind"] not in known_kinds:
            conflicts.append(
                {
                    "table": "provider_services",
                    "row": position,
                    "identity": payload["name"],
                    "reason": f"{where}的协议「{payload['kind']}」本程序不认识，"
                    f"已知协议：{'、'.join(sorted(known_kinds))}",
                }
            )
            continue

        key = payload["base_url"]
        if key in seen:
            conflicts.append(
                {
                    "table": "provider_services",
                    "row": position,
                    "identity": payload["name"],
                    "reason": f"{where}与快照第 {seen[key]} 行的接口地址相同，已跳过",
                }
            )
            continue
        seen[key] = position

        if _is_local_url(payload["base_url"]):
            # 导出侧会跳过本机服务；手工拼的快照里出现时放行，但必须提醒一句
            warnings.append(
                f"{where}「{payload['name']}」指向本机地址，已导入；请确认新机器上确实有这个服务"
            )

        row = existing.get(key)
        if row is None:
            action = "create"
        elif _is_unchanged(_service_shape(row), payload):
            action = "skip"
        else:
            action = "update"
        _bump(summary, "provider_services", action)
        plans.append(
            _Plan(
                table="provider_services",
                action=action,
                payload=payload,
                row_id=getattr(row, "id", None),
                identity=payload["name"],
            )
        )
    return plans


async def _apply_services(db: AsyncSession, plan: _Plan) -> None:
    """写 provider_services。任何情况下都不改 api_key_enc：
    更新时保留机器上已有的 Key（留空 = 保持原 Key 不变，与 providers 接口同义）。
    """
    payload = plan.payload
    models_json = json.dumps(payload["models"], ensure_ascii=False)

    if plan.action == "create":
        row = ProviderService(
            name=payload["name"],
            kind=payload["kind"],
            base_url=payload["base_url"],
            api_key_enc="",  # 快照里没有 Key，导入后需要用户自己填
            enabled=payload["enabled"],
            sort_order=payload["sort_order"],
            models_json=models_json,
        )
        db.add(row)
        await db.flush()
        await cc._audit(db, "provider_services", row.id, "create", None, _service_shape(row), IMPORT_ACTOR)
        await db.commit()
        return

    row = await db.get(ProviderService, plan.row_id)
    if row is None:  # 计划与落库之间被删了：跳过即可，不算错误
        return
    before = _service_shape(row)
    row.name = payload["name"]
    row.kind = payload["kind"]
    row.base_url = payload["base_url"]
    row.enabled = payload["enabled"]
    row.sort_order = payload["sort_order"]
    row.models_json = models_json
    await db.flush()
    await cc._audit(db, "provider_services", row.id, "update", before, _service_shape(row), IMPORT_ACTOR)
    await db.commit()


async def _apply_plan(
    db: AsyncSession, plans: list[_Plan], summary: dict[str, dict[str, int]], conflicts: list[dict[str, Any]]
) -> None:
    """按计划落库。逐行提交：某一行失败只影响它自己，前面的行不会被回滚掉。"""
    for plan in plans:
        if plan.action == "skip":
            continue
        try:
            if plan.table == "provider_services":
                await _apply_services(db, plan)
                continue
            spec = SCHEMA_REGISTRY[plan.table]
            if plan.action == "create":
                await cc.create_row(db, spec, plan.payload, actor=IMPORT_ACTOR)
            else:
                await cc.update_row(db, spec, int(plan.row_id or 0), plan.payload, actor=IMPORT_ACTOR)
        except cc.ConfigError as e:
            # 落库失败（唯一约束、必填、行被删…）→ 降级成这一行的冲突，不让整次导入崩掉
            bucket = summary.setdefault(plan.table, {"created": 0, "updated": 0, "skipped": 0})
            counter = _COUNTER_OF[plan.action]
            if bucket.get(counter):
                bucket[counter] -= 1
            bucket["skipped"] += 1
            conflicts.append(
                {
                    "table": plan.table,
                    "row": None,
                    "identity": plan.identity,
                    "reason": f"「{plan.identity}」写入失败：{e.message}",
                }
            )


async def import_snapshot(db: AsyncSession, snapshot: Any, *, dry_run: bool = True) -> dict[str, Any]:
    """导入快照。dry_run=True 时只算「会新增/更新/跳过多少行」，一行都不写。"""
    scopes, tables = _read_envelope(snapshot)
    selected = _tables_of(scopes) if scopes else list(ALL_TABLES)

    summary: dict[str, dict[str, int]] = _empty_summary()
    conflicts: list[dict[str, Any]] = []
    warnings: list[str] = []
    plans: list[_Plan] = []

    for table, rows in tables.items():
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise cc.ConfigError(f"快照里「{table_label(table)}」不是数组，结构不对")
        if len(rows) > MAX_ROWS_PER_TABLE:
            raise cc.ConfigError(
                f"快照里「{table_label(table)}」有 {len(rows)} 行，超过单表 {MAX_ROWS_PER_TABLE} 行的上限；"
                "请拆分后再导入"
            )
        if table not in selected:
            warnings.append(f"快照里的「{table_label(table)}」不在本次导入范围内，已跳过")
            continue

        if table == "provider_services":
            plans.extend(await _plan_services_table(db, rows, summary, conflicts, warnings))
        else:
            plans.extend(
                await _plan_registered_table(db, SCHEMA_REGISTRY[table], rows, summary, conflicts, warnings)
            )

    if not dry_run:
        await _apply_plan(db, plans, summary, conflicts)
        # 配置值可能变了（模块开关、站点名…）→ 刷一次运行期缓存
        await cc._refresh_cache(db)

    totals = {"created": 0, "updated": 0, "skipped": 0}
    for bucket in summary.values():
        for key in totals:
            totals[key] += bucket[key]

    source = {
        "format": str(snapshot.get("format") or ""),
        "schemaVersion": int(snapshot.get("schemaVersion") or 0),
        "appVersion": str(snapshot.get("appVersion") or ""),
        "exportedAt": str(snapshot.get("exportedAt") or ""),
    }
    return {
        # ok = 流程本身跑通了（没有致命结构错误）。逐行的问题都在 conflicts 里，不在这里藏。
        "ok": True,
        "dryRun": bool(dry_run),
        "mode": MODE_MERGE,
        "scopes": scopes if scopes else list(DEFAULT_SCOPES),
        "source": source,
        "summary": summary,
        "totals": totals,
        "conflicts": conflicts,
        "warnings": warnings,
        "notes": list(EXPORT_NOTES),
    }
