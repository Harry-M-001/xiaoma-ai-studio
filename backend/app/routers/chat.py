"""对话接口：会话管理 + SSE 流式聊天。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.deps import require_auth
from app.models import ChatMessage, ChatSession
from app.providers.base import AdapterError
from app.schemas import ChatMessageOut, ChatRequest, ChatSessionOut
from app.services import provider_store

router = APIRouter(prefix="/api/chat", tags=["chat"], dependencies=[Depends(require_auth)])


async def get_db() -> AsyncSession:
    async with SessionLocal() as db:
        yield db


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


@router.get("/sessions", response_model=list[ChatSessionOut])
async def list_sessions(db: AsyncSession = Depends(get_db)) -> list[ChatSession]:
    rows = await db.execute(select(ChatSession).order_by(ChatSession.updated_at.desc()))
    return list(rows.scalars().all())


@router.post("/sessions", response_model=ChatSessionOut)
async def create_session(db: AsyncSession = Depends(get_db)) -> ChatSession:
    row = ChatSession(title="新对话")
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(ChatSession, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    await db.execute(delete(ChatMessage).where(ChatMessage.session_id == session_id))
    await db.delete(row)
    await db.commit()
    return {"ok": True}


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessageOut])
async def list_messages(session_id: int, db: AsyncSession = Depends(get_db)) -> list[ChatMessage]:
    rows = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.id)
    )
    return list(rows.scalars().all())


@router.post("/stream")
async def chat_stream(payload: ChatRequest) -> StreamingResponse:
    messages = [{"role": m.role, "content": m.content} for m in payload.messages]
    if not messages:
        raise HTTPException(status_code=400, detail="消息为空")

    async def generator():
        session_id = payload.session_id
        model_name = ""
        label = payload.model_key
        try:
            async with SessionLocal() as db:
                resolved = await provider_store.resolve_model(db, payload.model_key, "text")
                model_name = resolved.model_name
                label = resolved.label
                adapter = resolved.adapter

                # 建立会话并落库用户消息
                session = None
                if session_id:
                    session = await db.get(ChatSession, session_id)
                if session is None:
                    session = ChatSession(title="新对话")
                    db.add(session)
                    await db.flush()
                    session_id = session.id
                first_text = next(
                    (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
                )
                if session.title == "新对话" and first_text:
                    session.title = first_text.strip().replace("\n", " ")[:24] or "新对话"
                db.add(
                    ChatMessage(
                        session_id=session_id,
                        role="user",
                        content=first_text,
                        model=model_name,
                    )
                )
                await db.commit()
                sid = session_id
                stitle = session.title

            yield _sse({"type": "meta", "session_id": sid, "title": stitle})

            collected: list[str] = []
            async for piece in adapter.chat_stream(
                model_name, messages, temperature=payload.temperature
            ):
                collected.append(piece)
                yield _sse({"type": "delta", "text": piece})
            await adapter.close()

            answer = "".join(collected)
            async with SessionLocal() as db:
                db.add(
                    ChatMessage(
                        session_id=sid,
                        role="assistant",
                        content=answer,
                        model=model_name,
                    )
                )
                await db.commit()
            yield _sse({"type": "done", "model": label})

        except AdapterError as e:
            yield _sse({"type": "error", "message": str(e)})
        except Exception as e:  # noqa: BLE001
            yield _sse({"type": "error", "message": f"生成失败：{e}"})

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
