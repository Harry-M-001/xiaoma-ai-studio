"""Agent 的运行循环（harness）：装配上下文 → 调模型 → 执行工具 → 落库 → 复用的历史。

## 为什么「历史从库里重建」而不是放在内存里

确认门会把一轮对话**切成两次请求**（先停下来问人，人点了再继续）。如果上下文只存在
内存里，进程一重启、或者用户刷新后过一会儿才点确认，这一轮就接不上了。所以每一步都落库，
每次继续时按步骤**重建消息数组**：

- 用户说的 → `user`
- 模型的正文 → `assistant`
- 一次工具调用 → `assistant(tool_calls)` + `tool(result)`（`tool_call_id` 用步骤 id 生成，稳定可复现）

好处是「可回放」与「可续跑」是同一套数据，不用再维护一份会话状态。

## 三条硬规矩

1. **不可逆工具默认停下来等人点**（`run_canvas` / `run_node`）。停下来时先把代价算好，
   确认弹窗直接用那份数——不让用户对着「要花钱」四个字做决定。
   **自动审批**的会话（`session.auto_approve`）例外：用户已经授权，直接按预览的调用数放行，
   但代价仍然记在那一步里。
2. **工具失败不打断整轮**：一次失败变成一条 `{ok:false}` 的结果回到模型，由它决定换个做法。
3. **轮数有上限**：8 轮。真正的死循环不是「模型笨」，而是 harness 没设上限。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentProject, AgentSession, AgentStep
from app.providers.base import AdapterError
from app.registry.canvas_nodes import NODE_SCHEMAS
from app.services import agent_skills, agent_tools, provider_store
from app.services.agent_tools import TIER_IRREVERSIBLE, TIER_LABELS, ToolContext

logger = logging.getLogger("xiaoma.agent")

#: 一轮里最多几次模型往返。超过就停：这比「无限重试」诚实得多。
MAX_ROUNDS = 8

#: 单条工具结果进上下文的上限（完整的还在库里与界面上）
MAX_RESULT_CHARS = 4000


# ============================================================ 历史重建


def _history_text(step: AgentStep) -> str:
    text = step.tool_result or ""
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + "\n…（结果太长，已截断；完整内容在界面的这一步里）"


def approval_rule(session: AgentSession) -> str:
    """提示词里那条「不可逆要不要人点」的规矩，跟着审批方式变。

    自动审批时如果还写着「系统会拦下来让用户确认」，模型会一直让用户去点一个
    根本不会出现的弹窗——提示词必须与真实行为一致。
    """
    if session.auto_approve:
        return (
            "3. **run_canvas / run_node 会花钱且不可逆**：本会话已开启**自动审批**，"
            "这两种调用会直接执行（用户已授权）。执行前仍要说清要做什么、大概几次调用；"
            "执行后严格按工具返回如实报告，**不能**把「准备跑」说成「跑好了」。"
        )
    return (
        "3. **run_canvas / run_node 会花钱且不可逆**：系统会把这次调用拦下来让用户点确认。"
        "你**不能**声称已经跑完，也不要把「准备跑」说成「跑好了」。"
    )


def needs_human_approval(tier: str, auto_approve: bool) -> bool:
    """这个工具这一步要不要停下来等人点（纯函数，便于单测）。"""
    return tier == TIER_IRREVERSIBLE and not auto_approve


def build_system_prompt(
    session: AgentSession, project: AgentProject | None = None
) -> str:
    kinds = "、".join(f"{k}={v.get('label')}" for k, v in NODE_SCHEMAS.items())
    canvas_id = (project.canvas_project_id if project is not None else None) or session.canvas_project_id
    bound = (
        f"这次会话绑定在自由画布 id={canvas_id}"
        if canvas_id
        else "这次还没绑定自由画布"
    )
    if project is not None:
        where = (
            f"当前 Agent 项目：「{project.name}」（id={project.id}）"
            + (f"，说明：{project.description}" if project.description else "")
            + "。项目内的任务是这个项目的一部分；**别的项目的会话你看不到，也不要假装知道**。"
        )
    else:
        where = "这次会话还没归入任何 Agent 项目（未归档）。如果用户要做一件新的事，建议先建一个项目。"
    return f"""你是「小马AI工坊」里的通用 Agent。你能调用工坊自己的工具：读画布、搭画布、改节点、连线、预估成本、执行。

规矩：
1. **事实只信工具返回**：不要编造画布 id、节点 id、任务状态或产物；没查过就说没查。
2. **改之前先读**：动某张画布前先 get_canvas；改完用返回的 id 回查一次（get_canvas 或 canvas_status）再说结论。
3. {approval_rule(session)}
4. **每次只调一个工具**，看一眼结果再决定下一步。不要一口气连调五个。
5. {bound}。用户没说是哪张时，先 list_projects；一张都没有就 create_project。
6. 节点类型：{kinds}。
7. 要搭画布用 draft_canvas（它会再调一次模型生成草稿，可能慢几秒），落定用 apply_canvas_draft。
8. {where}
9. 回答用中文、短句，说清「做了什么 / 对象 id / 下一步」。不要输出大段 JSON 或代码。
{agent_skills.prompt_block()}
"""


async def project_of(db: AsyncSession, session: AgentSession) -> AgentProject | None:
    if not session.agent_project_id:
        return None
    return await db.get(AgentProject, session.agent_project_id)


async def _steps(db: AsyncSession, session_id: int) -> list[AgentStep]:
    return list((await db.execute(
        select(AgentStep).where(AgentStep.session_id == session_id).order_by(AgentStep.id)
    )).scalars().all())


def messages_from_steps(
    session: AgentSession, rows: list[AgentStep], project: AgentProject | None = None
) -> list[dict]:
    """把步骤还原成 OpenAI 兼容的消息数组（纯函数，便于单测）。"""
    msgs: list[dict] = [{"role": "system", "content": build_system_prompt(session, project)}]
    for st in rows:
        if st.role == "user":
            msgs.append({"role": "user", "content": st.content})
        elif st.role == "assistant":
            if st.content.strip():
                msgs.append({"role": "assistant", "content": st.content})
        elif st.role == "tool":
            if st.status == "pending":
                # 还没确认的那一步不进上下文：它会在确认后变成 ok / rejected
                continue
            if st.status == "rejected":
                result = json.dumps(
                    {"ok": False, "error": "用户拒绝了这次调用", "hint": "换一种做法，或先问用户想怎么做。"},
                    ensure_ascii=False,
                )
            else:
                result = _history_text(st)
            call_id = f"call_{st.id}"
            msgs.append({
                "role": "assistant",
                # **content 不能为空**：KIE 这类聚合网关会直接拒「content 为空的 assistant 消息」
                # （报 Message content is blank，而且是 HTTP 200 里的业务错误）。给一句人话既合规
                # 又好读，回放时也知道这一步在干什么。
                "content": f"（调用工具 {st.tool_name}）",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": st.tool_name, "arguments": st.tool_args or "{}"},
                }],
            })
            msgs.append({"role": "tool", "tool_call_id": call_id, "content": result})
    return msgs


async def _next_seq(db: AsyncSession, session_id: int) -> int:
    last = (await db.execute(
        select(AgentStep.seq).where(AgentStep.session_id == session_id)
        .order_by(AgentStep.seq.desc()).limit(1)
    )).scalar()
    return int(last or 0) + 1


async def add_step(db: AsyncSession, session_id: int, **kw: Any) -> AgentStep:
    step = AgentStep(session_id=session_id, seq=await _next_seq(db, session_id), **kw)
    db.add(step)
    await db.commit()
    await db.refresh(step)
    return step


# ============================================================ 模型


async def resolve_brain(db: AsyncSession, session: AgentSession):
    """返回 (adapter, model_name, label)。会话没指定就挑第一个可用的文本模型。"""
    if session.model_key:
        resolved = await provider_store.resolve_model(db, session.model_key, "text")
        return resolved.adapter, resolved.model_name, resolved.label
    rows = await provider_store.list_services(db)
    option = next(iter(provider_store.list_model_options(rows, "text")), None)
    if option is None:
        raise AdapterError(
            "还没有可用的文本模型。到「模型服务」接入一个（支持工具调用的模型最好，如 DeepSeek / GPT / Claude）。"
        )
    resolved = await provider_store.resolve_model(db, option.key, "text")
    return resolved.adapter, resolved.model_name, resolved.label


def _parse_args(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        raise agent_tools.ToolError(
            f"参数不是合法 JSON：{raw[:80]}", hint="只传一个合法的 JSON 对象。"
        )
    if not isinstance(value, dict):
        raise agent_tools.ToolError("参数必须是 JSON 对象。", hint="例如 {\"project_id\": 3}。")
    return value


# ============================================================ 主循环


async def run_loop(db: AsyncSession, session: AgentSession) -> dict:
    """从当前步骤继续跑，直到模型不再调工具、或需要人确认、或到轮数上限。"""
    adapter, model_name, label = await resolve_brain(db, session)
    project = await project_of(db, session)
    ctx = ToolContext(db=db, session=session, model_key=session.model_key, agent_project=project)
    tools = agent_tools.openai_tools()
    rounds = 0
    try:
        while rounds < MAX_ROUNDS:
            rounds += 1
            rows = await _steps(db, session.id)
            try:
                result = await adapter.chat_once(
                    model_name, messages_from_steps(session, rows, project), tools=tools
                )
            except AdapterError as e:
                # 不支持工具调用是最常见的失败；原样把这句话交给界面，别再包一层
                raise AdapterError(str(e), log_detail="agent_chat_failed") from e

            text = str(result.get("content") or "").strip()
            calls = result.get("tool_calls") or []
            if text:
                usage = result.get("usage") or {}
                await add_step(
                    db, session.id, role="assistant", content=text,
                    tokens=int(usage.get("total_tokens") or 0),
                )
            if not calls:
                return {
                    "status": "done",
                    "reply": text,
                    "model": label,
                    "rounds": rounds,
                }

            for index, call in enumerate(calls):
                name = str(call.get("name") or "")
                spec = agent_tools.by_name(name)
                if spec is None:
                    await add_step(
                        db, session.id, role="tool", tool_name=name or "(空)",
                        tool_args=str(call.get("arguments") or ""),
                        tool_result=json.dumps(
                            {"ok": False, "error": f"没有叫 {name} 的工具"},
                            ensure_ascii=False,
                        ),
                        status="error",
                    )
                    continue

                try:
                    args = _parse_args(str(call.get("arguments") or "{}"))
                except agent_tools.ToolError as e:
                    await add_step(
                        db, session.id, role="tool", tool_name=name,
                        tool_args=str(call.get("arguments") or ""),
                        tool_result=json.dumps(
                            {"ok": False, "error": str(e), "hint": e.hint}, ensure_ascii=False
                        ),
                        tier=spec.tier, status="error",
                    )
                    continue

                if needs_human_approval(spec.tier, bool(session.auto_approve)):
                    # 先把代价算好再停下来：确认弹窗不该只有「要花钱」四个字
                    preview: dict = {}
                    try:
                        if spec.preview is not None:
                            preview = await spec.preview(ctx, args)
                    except agent_tools.ToolError as e:
                        preview = {"error": str(e), "hint": e.hint}
                    step = await add_step(
                        db, session.id, role="tool", tool_name=name,
                        tool_args=json.dumps(args, ensure_ascii=False),
                        tool_result=json.dumps({"preview": preview}, ensure_ascii=False),
                        tier=spec.tier, status="pending",
                    )
                    # 同一条回复里排在它后面的调用先记成 skipped：否则确认之后它们就凭空丢了
                    for extra in calls[index + 1:]:
                        await add_step(
                            db, session.id, role="tool",
                            tool_name=str(extra.get("name") or ""),
                            tool_args=str(extra.get("arguments") or ""),
                            tool_result=json.dumps(
                                {"ok": False, "skipped": True,
                                 "error": "上一个调用需要用户确认，本次没有执行"},
                                ensure_ascii=False,
                            ),
                            status="skipped",
                        )
                    return {
                        "status": "need_confirm",
                        "step": step.id,
                        "tool": name,
                        "tierLabel": TIER_LABELS.get(spec.tier, spec.tier),
                        "preview": preview,
                        "model": label,
                        "rounds": rounds,
                    }

                # 自动审批的不可逆工具：仍然先算一遍代价——run_canvas 的预算闸要用它
                # 放行（与手动确认同一口径），结果里也留一份，事后能回看这一步花了多少次调用。
                auto_irreversible = spec.tier == TIER_IRREVERSIBLE
                auto_preview: dict = {}
                if auto_irreversible:
                    try:
                        if spec.preview is not None:
                            auto_preview = await spec.preview(ctx, args)
                    except agent_tools.ToolError as e:
                        auto_preview = {"error": str(e), "hint": e.hint}
                    if name == "run_canvas" and auto_preview.get("calls"):
                        args = {**args, "ack_calls": int(auto_preview["calls"])}

                started = time.monotonic()
                outcome = await agent_tools.execute(ctx, name, args)
                if auto_irreversible:
                    outcome = {**outcome, "autoApproved": True, "preview": auto_preview}
                await add_step(
                    db, session.id, role="tool", tool_name=name,
                    tool_args=json.dumps(args, ensure_ascii=False),
                    tool_result=json.dumps(outcome, ensure_ascii=False),
                    tier=spec.tier,
                    status="ok" if outcome.get("ok") else "error",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            # 继续下一轮：让模型看到工具结果
        return {"status": "max_rounds", "reply": "这一轮调用次数到上限了，先停在这里。", "model": label,
                "rounds": rounds}
    finally:
        try:
            await adapter.close()
        except Exception:  # noqa: BLE001
            pass


async def send(db: AsyncSession, session: AgentSession, text: str) -> dict:
    """用户说一句话：落库 → 跑一轮。"""
    content = (text or "").strip()
    if not content:
        raise ValueError("消息为空")
    await add_step(db, session.id, role="user", content=content)
    if session.title in ("", "新会话"):
        session.title = content.replace("\n", " ")[:24]
    await db.commit()
    return await run_loop(db, session)


async def confirm(db: AsyncSession, session: AgentSession, step_id: int, approve: bool) -> dict:
    """人点了确认 / 拒绝：改这一步的状态，然后接着跑。"""
    step = await db.get(AgentStep, int(step_id))
    if step is None or step.session_id != session.id:
        raise ValueError("这一步不存在")
    if step.status != "pending":
        raise ValueError("这一步已经处理过了")
    spec = agent_tools.by_name(step.tool_name)
    if spec is None:
        raise ValueError(f"工具 {step.tool_name} 已经不在了")

    if not approve:
        step.status = "rejected"
        step.tool_result = json.dumps({"ok": False, "error": "用户拒绝了这次调用"}, ensure_ascii=False)
        await db.commit()
        return await run_loop(db, session)

    try:
        args = _parse_args(step.tool_args or "{}")
    except agent_tools.ToolError as e:
        step.status = "error"
        step.tool_result = json.dumps({"ok": False, "error": str(e), "hint": e.hint}, ensure_ascii=False)
        await db.commit()
        return await run_loop(db, session)

    # 预算闸要用「确认时看到的那个数」放行：预览已经给用户看过，这里不重新问一遍
    try:
        preview = json.loads(step.tool_result or "{}").get("preview") or {}
    except (TypeError, ValueError):
        preview = {}
    if step.tool_name == "run_canvas" and preview.get("calls"):
        args["ack_calls"] = int(preview["calls"])

    ctx = ToolContext(
        db=db, session=session, model_key=session.model_key,
        agent_project=await project_of(db, session),
    )
    started = time.monotonic()
    outcome = await agent_tools.execute(ctx, step.tool_name, args)
    step.status = "ok" if outcome.get("ok") else "error"
    step.tool_result = json.dumps(outcome, ensure_ascii=False)
    step.duration_ms = int((time.monotonic() - started) * 1000)
    await db.commit()
    return await run_loop(db, session)
