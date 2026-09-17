"""分享 API：导出 / 导入分享单元。

**服务端不存任何分享内容**（`路线图.md` #20 的「零服务器」）：这里只有两个纯函数式的
端点，进来什么、出去什么，不落库、不生成链接、不提供下载地址。分享靠用户把一段文本
或一个文件给对方，不靠我们托管。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import __version__
from app.database import SessionLocal
from app.deps import require_auth
from app.registry.canvas_nodes import NODE_SCHEMAS
from app.services import provider_store, share_service

router = APIRouter(prefix="/api/share", tags=["share"], dependencies=[Depends(require_auth)])


async def get_db():
    async with SessionLocal() as session:
        yield session


class ShareExportIn(BaseModel):
    # 传的是**当前画布状态**而不是 project_id：用户想分享的是屏幕上看到的东西，
    # 不该逼他先点一次保存（自动保存是 1 秒后的事，用户很可能刚刚才改完）
    doc: dict = {}
    title: str = ""
    description: str = ""
    author: str = ""
    license: str = ""


class ShareCodeIn(BaseModel):
    text: str = ""


@router.get("/licenses")
async def share_licenses() -> dict:
    """可选授权范围。给固定几档，前端不写死一份。"""
    return {
        "default": share_service.DEFAULT_LICENSE,
        "options": [
            {"key": k, "label": v} for k, v in share_service.LICENSES.items()
        ],
    }


@router.post("/export")
async def share_export(payload: ShareExportIn) -> dict:
    """把画布打包成分享快照 + 分享码。不落库。"""
    try:
        snapshot = share_service.export_canvas(
            payload.doc,
            title=payload.title,
            description=payload.description,
            author=payload.author,
            license_key=payload.license or share_service.DEFAULT_LICENSE,
            app_version=__version__,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"当前画布还不能分享：{e}")

    code = share_service.encode_code(snapshot)
    return {
        "snapshot": snapshot,
        "code": code,
        # 太长就明说别用分享码：聊天窗口里贴不下、还容易被截断
        "codeTooLong": len(code) > share_service.CODE_SOFT_LIMIT,
        "codeLength": len(code),
    }


@router.post("/import")
async def share_import(payload: ShareCodeIn, db=Depends(get_db)) -> dict:
    """读一份分享快照，回「能不能用、缺什么」。不落库。"""
    rows = await provider_store.list_services(db)
    modalities = {o.modality for o in provider_store.list_model_options(rows)}
    result = share_service.import_share(
        payload.text,
        local_node_kinds=set(NODE_SCHEMAS),
        local_modalities=modalities,
    )
    return result.to_dict()
