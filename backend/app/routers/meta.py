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
