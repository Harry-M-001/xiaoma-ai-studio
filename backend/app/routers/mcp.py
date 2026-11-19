"""HTTP MCP：把画布能力以 MCP 工具的形式交给 dsh sidecar。

为什么不用 stdio：stdio 子进程会被 dsh 的沙箱限制，而且每次会话都得起一个 Python 进程。
HTTP 常驻在我们的 FastAPI 里，天然在沙箱外，也便于做审计。

P1 的工具面刻意很小：四个只读 + 一个**阻塞式确认**的跑整图。
没有任何「批准」类工具——批准只能由界面 REST 接口发起，模型无法自批。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.database import SessionLocal
from app.models import AgentSession
from app.services import agent_tools, mcp_gate

logger = logging.getLogger("xiaoma.mcp")

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

#: dsh 侧 `serverName: xms` 决定了工具前缀 `mcp__xms__<name>`
READONLY_TOOLS: dict[str, str] = {
    "xms_list_projects": "list_projects",
    "xms_get_canvas": "get_canvas",
    "xms_summarize_canvas": "summarize_canvas",
    "xms_preview_canvas": "preview_canvas",
}

_PROJECT_ID = {
    "type": "integer",
    "description": "画布 id（先用 xms_list_projects 查）",
}

TOOLS: list[dict] = [
    {
        "name": "xms_list_projects",
        "description": "列出本机所有自由画布（id、名称、节点数、最近更新时间）。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "xms_get_canvas",
        "description": "读一张画布的节点与连线（节点只回类型、提示词与可读参数）。",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": _PROJECT_ID},
            "required": ["project_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "xms_summarize_canvas",
        "description": "一张画布的中文摘要（节点清单、未连线提示），比整图更省 token。",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": _PROJECT_ID},
            "required": ["project_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "xms_preview_canvas",
        "description": "跑整图前的代价预估：几次调用、几个节点会重做、几个被阻塞。",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": _PROJECT_ID},
            "required": ["project_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "xms_run_canvas",
        "description": (
            "执行整张画布。**会阻塞等待用户在界面上点确认**："
            "确认后才派发任务，拒绝或超时返回 ok:false 且不执行。"
            "调用前先用 xms_preview_canvas 把代价告诉用户。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": _PROJECT_ID},
            "required": ["project_id"],
            "additionalProperties": False,
        },
    },
]


class ConfirmationIn(BaseModel):
    approve: bool


async def get_db() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


def _ok(req_id: Any, result: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
        status_code=200,
    )


def _probe_session(project_id: int | None) -> AgentSession:
    """只读工具只是需要一个「这次调用」的上下文，不落库、不算会话历史。"""
    return AgentSession(canvas_project_id=project_id)


async def _call_tool(name: str, args: dict) -> dict:
    started = time.monotonic()
    try:
        if name == "xms_run_canvas":
            project_id = int(args.get("project_id") or 0)
            if project_id <= 0:
                return {"ok": False, "error": "缺少 project_id。", "hint": "先用 xms_list_projects 查 id。"}
            outcome = await mcp_gate.request_run(project_id)
        else:
            internal = READONLY_TOOLS.get(name)
            if internal is None:
                return {"ok": False, "error": f"工具 {name} 不存在。"}
            async with SessionLocal() as db:
                project_id = args.get("project_id")
                ctx = agent_tools.ToolContext(
                    db=db,
                    session=_probe_session(int(project_id) if project_id else None),
                )
                outcome = await agent_tools.execute(ctx, internal, args)
    except agent_tools.ToolError as e:
        outcome = {"ok": False, "error": str(e), "hint": e.hint}
    except Exception as e:  # noqa: BLE001  工具异常要回给模型，不能把协议层打崩
        logger.warning("mcp tool %s 失败：%s", name, e, exc_info=True)
        outcome = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    logger.info(
        "mcp tool=%s args=%s ok=%s %dms",
        name,
        json.dumps(args, ensure_ascii=False)[:200],
        outcome.get("ok"),
        int((time.monotonic() - started) * 1000),
    )
    return outcome


async def _handle(message: dict) -> Response:
    req_id = message.get("id")
    method = str(message.get("method") or "")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    # 通知没有 id，按 JSON-RPC 不能回响应
    if req_id is None:
        return Response(status_code=202)

    if method == "initialize":
        return _ok(req_id, {
            "protocolVersion": str(params.get("protocolVersion") or "2025-06-18"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "小马AI工坊", "version": __version__},
        })
    if method == "ping":
        return _ok(req_id, {})
    if method == "tools/list":
        return _ok(req_id, {"tools": TOOLS})
    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        outcome = await _call_tool(name, args)
        return _ok(req_id, {
            "content": [{"type": "text", "text": json.dumps(outcome, ensure_ascii=False)}],
            "isError": not bool(outcome.get("ok")),
        })
    return _error(req_id, -32601, f"不支持的方法：{method}")


def _require_local(request: Request) -> None:
    """只服务本机 sidecar：没有 token 的 P1 不能让 MCP 跟着 HOST 一起暴露到局域网。"""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="MCP 只接受本机连接。")


@router.post("")
async def mcp_endpoint(request: Request) -> Response:
    _require_local(request)
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return _error(None, -32700, "请求体不是合法 JSON")

    if isinstance(body, dict):
        return await _handle(body)
    if isinstance(body, list):
        responses = []
        for item in body:
            if not isinstance(item, dict):
                responses.append(
                    {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32600, "message": "无效请求"}}
                )
                continue
            resp = await _handle(item)
            if resp.status_code != 202:
                responses.append(json.loads(resp.body))
        if not responses:
            return Response(status_code=202)
        return JSONResponse(responses)
    return _error(None, -32600, "无效请求")


@router.get("")
async def mcp_stream(request: Request) -> Response:
    """不提供 SSE 下行流：明确 405，别让前端 SPA 的兜底把 index.html 当成事件流。"""
    _require_local(request)
    return JSONResponse(
        {"detail": "本 MCP 服务只支持 streamable-http 的 POST 请求。"},
        status_code=405,
    )


# ============================================================ 界面确认


@router.get("/confirmations")
async def list_confirmations(
    request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    _require_local(request)
    return {"items": await mcp_gate.pending(db)}


@router.post("/confirmations/{step_id}")
async def confirm(step_id: int, payload: ConfirmationIn, request: Request) -> dict:
    _require_local(request)
    if not mcp_gate.resolve(step_id, payload.approve):
        raise HTTPException(
            status_code=409,
            detail="这次确认已经结束（超时或已被处理）。",
        )
    return {"ok": True}
