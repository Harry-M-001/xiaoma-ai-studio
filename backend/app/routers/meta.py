"""公开元数据接口。

前端不写死任何清单：能力类型、参数选项、导航、协议、系统配置全部从这里拉。
新增一行配置 → 前端立刻可见，无需重新构建。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.models import Modality, NavItem, ParamOption
from app.registry import adapters
from app.services import config_center_service as config_center

router = APIRouter(prefix="/api/meta", tags=["meta"])


@router.get("/config")
async def public_config(db: AsyncSession = Depends(get_db)) -> dict:
    """公开的系统配置（敏感项自动过滤），供前端展示站点名、默认值、模块开关等。"""
    return await config_center.get_config_map(db, public_only=True)


@router.get("/modalities")
async def list_modalities(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = await db.execute(
        select(Modality).where(Modality.enabled.is_(True)).order_by(Modality.sort_order, Modality.id)
    )
    return [
        {"key": m.key, "label": m.label, "icon": m.icon, "sort_order": m.sort_order}
        for m in rows.scalars().all()
    ]


@router.get("/param-options")
async def list_param_options(
    kind: str | None = Query(default=None, description="不传则返回全部，按 kind 分组"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = select(ParamOption).where(ParamOption.enabled.is_(True))
    if kind:
        stmt = stmt.where(ParamOption.kind == kind)
    rows = await db.execute(stmt.order_by(ParamOption.kind, ParamOption.sort_order, ParamOption.id))

    grouped: dict[str, list[dict]] = {}
    for opt in rows.scalars().all():
        grouped.setdefault(opt.kind, []).append(
            {
                "id": opt.id,
                "value": opt.value,
                "label": opt.label or opt.value,
                "meta": config_center._load_json(opt.meta_json, {}),
                "sort_order": opt.sort_order,
            }
        )
    return grouped if kind is None else {kind: grouped.get(kind, [])}


@router.get("/nav")
async def list_nav(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """侧边栏导航。加模块 / 隐藏模块只改配置表，前端自动跟随。"""
    rows = await db.execute(
        select(NavItem).where(NavItem.enabled.is_(True)).order_by(NavItem.sort_order, NavItem.id)
    )
    return [
        {
            "key": n.key,
            "label": n.label,
            "icon": n.icon,
            "route": n.route,
            "group": n.group_name,
            "requires_auth": n.requires_auth,
        }
        for n in rows.scalars().all()
    ]


@router.get("/prompts")
async def list_prompts(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """提示词模板（仅启用项）。新增模板走通用配置接口，保存后立即出现在这里。"""
    from app.models import Prompt

    rows = await db.execute(
        select(Prompt).where(Prompt.enabled.is_(True)).order_by(Prompt.sort_order, Prompt.id)
    )
    return [
        {
            "id": p.id,
            "key": p.key,
            "title": p.title,
            "modality": p.modality,
            "content": p.content,
            "description": p.description,
            "tags": p.tags,
            "version": p.version,
        }
        for p in rows.scalars().all()
    ]


# 块数参数 → 中文标签（节点浮框的数字框标题）
_CHUNK_LABELS = {
    "chapterCount": "章数",
    "sceneCount": "场数",
    "shotCount": "镜头数",
}


@router.get("/agent-prompts")
async def list_agent_prompts(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """创作 Agent 元数据（自动链文档节点用）。

    只下发前端渲染浮框需要的字段：补充要求提示、块数参数名与上限。
    system_prompt / user_template 属于后端配置，不下发。
    """
    from app.models import AgentPrompt
    from app.services.doc_service import MAX_CHUNKS

    rows = await db.execute(
        select(AgentPrompt).where(AgentPrompt.enabled.is_(True)).order_by(
            AgentPrompt.sort_order, AgentPrompt.id
        )
    )
    return [
        {
            "key": a.key,
            "label": a.label,
            "varHint": a.var_hint,
            "chunkParam": a.chunk_param,
            "chunkLabel": _CHUNK_LABELS.get(a.chunk_param, ""),
            "chunked": bool(a.plan_prompt.strip() and a.chunk_prompt.strip()),
            "maxChunks": MAX_CHUNKS,
        }
        for a in rows.scalars().all()
    ]


@router.get("/provider-kinds")
async def list_provider_kinds() -> list[dict[str, str]]:
    """已注册的模型协议。新增协议后此处自动出现，前端下拉无需修改。"""
    return adapters.list_kinds()


@router.get("/camera-moves")
async def list_camera_moves() -> dict:
    """运镜词表：分镜表「运镜」那一栏的合法写法。

    为什么前端需要它：分镜表虽然是模型写的，但**用户会手改**；而体检报
    「不在词表里」时，用户得先能看到词表才知道该改成什么。这条以前不存在的原因很直接——
    词表本身不存在，`animatic` 与体检各藏了一份对得上的词，界面无从展示。

    与其它 meta 接口同一个口径：清单只在后端一处（`services/camera_moves.py`），
    前端不写死。纯静态数据，不查库。
    """
    from app.services import camera_moves

    return {
        "groups": [{"key": k, "label": v} for k, v in camera_moves.GROUPS],
        # 不下发 aliases：那是解析用的，摆到界面上只会让前端的判断跟着词表一起漂
        "moves": camera_moves.as_dicts(),
        "notMoves": list(camera_moves.NOT_MOVES),
    }


@router.get("/director-styles")
async def list_director_styles(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """导演风格卡（画布上每个节点的「风格」下拉用）。

    只下发 key 与名称：agent_prompt 是给 LLM 的、生图片段是给模型的，
    都属于后端配置，前端不需要也不应该拿到。
    """
    from app.models import DirectorStyle

    rows = await db.execute(
        select(DirectorStyle)
        .where(DirectorStyle.enabled.is_(True))
        .order_by(DirectorStyle.sort_order, DirectorStyle.id)
    )
    return [{"key": s.key, "name": s.name} for s in rows.scalars().all()]


@router.get("/provider-presets")
async def list_provider_presets(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """服务商预设。新增预设 = 配置表插一行，界面按钮自动出现。"""
    from app.models import ProviderPreset

    rows = await db.execute(
        select(ProviderPreset)
        .where(ProviderPreset.enabled.is_(True))
        .order_by(ProviderPreset.sort_order, ProviderPreset.id)
    )
    return [
        {
            "key": p.key,
            "name": p.name,
            "kind": p.kind,
            "base_url": p.base_url,
            "models": config_center._load_json(p.models_json, []),
            "hint": p.hint,
        }
        for p in rows.scalars().all()
    ]


# 首屏引导用的三项能力：够判断「能不能开始创作」了。
# 顺序固定，前端直接按这个顺序拼文案。
SETUP_MODALITIES = ("text", "image", "video")


@router.get("/setup-status")
async def setup_status(db: AsyncSession = Depends(get_db)) -> dict:
    """首次进入时的「该配什么」状态：有没有服务、各能力有几个可用模型、本机有没有 Ollama。

    单独做一个接口而不是让前端拉 `/api/providers` 自己算：Ollama 探测是**服务端**的动作
    （浏览器探不到本机 11434 之外的网络位置，也不该去探），而且它带缓存，只有服务端放得住。
    """
    from app.services import ollama_service, provider_store

    rows = await provider_store.list_services(db)
    options = provider_store.list_model_options(rows)
    counts: dict[str, int] = {}
    for o in options:
        counts[o.modality] = counts.get(o.modality, 0) + 1

    ollama = await ollama_service.detect()
    connected = await provider_store.find_by_base_url(db, f"{ollama['baseUrl']}/v1")

    return {
        "services": len(rows),
        "enabledServices": len([r for r in rows if r.enabled]),
        "modelCounts": counts,
        "ready": bool(options),
        "missing": [m for m in SETUP_MODALITIES if not counts.get(m)],
        # 服务名与模型名把「已接入但一个模型都没填」这种半配置状态也暴露出来
        "servicesDetail": [
            {"id": r.id, "name": r.name, "enabled": bool(r.enabled), "models": len(provider_store.parse_models(r.models_json))}
            for r in rows
        ],
        "ollama": {**ollama, "connected": connected is not None},
    }
