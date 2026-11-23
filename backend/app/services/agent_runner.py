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

import asyncio
import json
import logging
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentProject, AgentSession, AgentStep, Asset, Task
from app.providers.base import AdapterError
from app.registry.canvas_nodes import NODE_SCHEMAS
from app.services import agent_skills, agent_tools, provider_store, storage
from app.services.agent_tools import TIER_IRREVERSIBLE, TIER_LABELS, TIER_READ, ToolContext

logger = logging.getLogger("xiaoma.agent")

#: 一轮里最多几次模型往返。超过就停：这比「无限重试」诚实得多。
#: 8 轮不够一次标准流程（读画布 + 查模型 + 草稿 + 落定 + 回查 + 预览 + 派发 + 等结果），
#: 实测 Agent 在「补一个默认就有的参数」上耗两轮后就会撞线，任务停在半路。
MAX_ROUNDS = 12

#: 单条工具结果进上下文的上限（完整的还在库里与界面上）
MAX_RESULT_CHARS = 4000

#: 模型说完「我现在去做…」却没有调工具就收尾时，最多替它续几次。
#: 有上限是因为这本质上是「替模型补一次意图」，不是无限重试。
MAX_NUDGES = 2

#: 续跑时追加的那句话（只在这一次请求里，不落库、不显示成用户消息）
NUDGE_TEXT = (
    "（继续执行。你上一条回复里说要做的事还没做——不要只说明计划，"
    "现在直接调用对应的工具，拿到结果再汇报。）"
)

RECOVERY_NUDGE = (
    "（收尾检查）上一项工具操作没有成功。重新对照用户目标和已完成的步骤。"
    "还有授权范围内的安全办法时，修正参数、补查必要信息或换一个工具继续。"
    "付费提交结果不明时先查状态。确实无法推进时，说明已完成什么、"
    "具体卡在哪里，以及需要用户提供什么。不要宣称失败的操作成功。"
)

EMPTY_REPLY_NUDGE = "（继续这一轮）上一轮没有给出结果。需要行动就调用工具，否则给出可交付的答复或具体阻塞。"

#: 收尾话里出现这些词，说明它把「下一步」写成了结束语。
_PLAN_MARKERS = (
    "我现在", "我这就", "我这就去", "我马上", "我准备", "我接着", "我先", "我再",
    "现在我", "现在运行", "现在整图", "现在开始", "现在跑", "现在回查", "现在落定",
    "现在等", "等结果",
    "接下来", "下面我", "先做", "先跑", "先看", "先去", "先确认",
    "先预估", "先算", "再查一下", "再确认", "回查一遍",
    "开始做", "开始跑", "开始逐", "然后去", "再做一次",
)

#: 但这些是在让用户动手 / 征求意见，不该被当成「它要自己做」。
_HANDOFF_MARKERS = (
    "你可以", "您可以", "请你", "请先", "建议你", "要不要", "需要我",
    "你确认", "由你", "你点", "你选",
)


def looks_like_unfinished_plan(text: str) -> bool:
    """这句话是不是「宣布下一步，然后停了」而不是一个完整收尾。

    只看结尾 80 字：模型经常先汇报一段结果，最后一句才是计划。计划句之后
    再出现「你可以 / 要不要」这类词，说明它把下一步交给用户了，不算未完成。
    纯函数，便于单测。
    """
    tail = (text or "").strip()[-80:]
    if not tail:
        return False
    if tail.endswith(("？", "?")):
        return False
    if any(word in tail for word in _HANDOFF_MARKERS):
        return False
    return any(word in tail for word in _PLAN_MARKERS)


#: 输入框里以 / 开头的三条命令。spec / plan 先出方案等人确认，goal 持续跑到完成。
SLASH_COMMANDS = ("spec", "plan", "goal")

MODE_RULES = {
    "spec": (
        "当前是 **/spec 模式**：先把这件事细化成规范——目标、范围、分几步、每步产出什么、"
        "怎么算验收通过。**不要调用任何写入或花钱工具**（create_project、draft_canvas、"
        "apply_canvas_draft、update_node、connect_nodes、add_storyboard_image、run_canvas、"
        "run_node 都先别调），只读工具（get_canvas / canvas_status / preview_canvas 等）可以。"
        "末尾用一句话请用户确认；确认后用户会再发一条消息，那时才执行。"
    ),
    "plan": (
        "当前是 **/plan 模式**：先给执行计划——分几步、每步调什么工具、大概几次调用、"
        "哪些会花钱。**不要调用写入或花钱工具**，只读工具可以。等用户确认后再执行。"
    ),
    "goal": (
        "当前是 **/goal 模式**：这是一个以目标为导向的任务，会持续运行到完成为止。"
        "有任务在跑、或你刚派了任务时，用 canvas_status 看真实进度；**没有任务在跑时不要再反复查**，"
        "上游完成的就主动继续下游。只有目标真的完成、或需要用户做不可逆的决定时才收尾；"
        "目标已完成时直接给结论，**不要再调用任何工具**（包括 canvas_status）。"
    ),
}

#: 目标模式的监测节奏
GOAL_POLL_SECONDS = 8.0
GOAL_MAX_MINUTES = 30
GOAL_MAX_REVIEWS = 12
GOAL_PROGRESS_PROMPT = (
    "（持续监测）画布上还有 {running} 个任务在跑。先用 canvas_status 看节点级进度；"
    "已经跑完的上游就继续推进下游，不要只汇报状态，也不要要求用户回话才继续。"
)
GOAL_REVIEW_PROMPT = (
    "（持续监测）画布上现在没有任务在跑了。检查目标完成没有："
    "没完成就继续跑该跑的节点（缺输入就说清缺什么）；**已完成就不要再调用任何工具**，"
    "直接给出最终结论并收尾。"
)
GOAL_REVIEW_NO_CANVAS_PROMPT = (
    "（持续监测）这次没有可监测的画布。检查用户的目标完成没有："
    "没完成就继续做（调需要的工具）；**已完成就不要再调用任何工具**，直接给出最终结论并收尾。"
)


def parse_command(text: str) -> tuple[str, str]:
    """把开头的 `/spec` `/plan` `/goal` 拆出来（纯函数）。

    返回 `(mode, 去掉命令后的正文)`；不是这三条命令时 mode 为空、正文原样返回。
    """
    stripped = (text or "").lstrip()
    if not stripped.startswith("/"):
        return "", text
    head, _, rest = stripped[1:].partition(" ")
    command = head.strip().lower()
    if command not in SLASH_COMMANDS:
        return "", text
    return command, rest.strip()


# ============================================================ 历史重建


_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".log", ".py", ".js", ".ts", ".tsx", ".jsx", ".html",
    ".css", ".scss", ".sql", ".xml", ".srt", ".vtt", ".tex", ".bat", ".ps1", ".sh",
}
#: 单个文件最多塞进提示词的字符数；再多就截断并说明——上下文是有限的，
#: 一份一百页的文档整段塞进去只会把这一轮后面的工具调用挤掉。
_ATTACH_PER_FILE = 6000
_ATTACH_TOTAL = 12000


def _docx_text(data: bytes) -> str:
    try:
        import io

        from docx import Document

        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception:  # noqa: BLE001
        return ""


def _attachment_text(asset: Asset) -> str:
    """从资产里尽量读出正文；读不出来就返回空串（由调用方说明原因）。"""
    path = storage.abs_path(asset.filename)
    if not path.is_file():
        return ""
    suffix = path.suffix.lower()
    try:
        if suffix == ".docx":
            return _docx_text(path.read_bytes())
        if suffix in _TEXT_SUFFIXES or (asset.content_type or "").startswith("text/"):
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return ""


async def describe_attachments(db: AsyncSession, asset_ids: list[int]) -> str:
    """把用户随消息上传的文件整理成一段可读说明，拼在用户那句话后面。

    - 文本类（txt / md / csv / json / 代码 / docx）直接把正文给模型；
    - 图片 / 音视频只给资产 id 与规格——它们不是「读」的对象，而是后续工具
      （参考图、首帧、配音）要引用的素材；
    - PDF 这类读不出正文的格式如实说明，不假装读过。
    """
    if not asset_ids:
        return ""
    lines = ["【用户上传的文件】"]
    used = 0
    for raw_id in asset_ids[:20]:
        try:
            asset = await db.get(Asset, int(raw_id))
        except (TypeError, ValueError):
            asset = None
        if asset is None:
            lines.append(f"- 资产 #{raw_id} 已不存在（可能被删除了），忽略。")
            continue
        name = asset.original_name or asset.filename
        head = f"- @资产#{asset.id}「{name}」（{asset.kind}，{asset.size or 0} 字节）"
        if asset.kind == "image":
            lines.append(head + f"\n  已存入资产库，需要时可作为参考图 / 首帧引用（id={asset.id}）。")
            continue
        if asset.kind in ("video", "audio"):
            lines.append(head + f"\n  已存入资产库，可在画布节点或工具里引用（id={asset.id}）。")
            continue
        text = _attachment_text(asset)
        if not text.strip():
            lines.append(head + "\n  正文没有解析出来（可能是 PDF / 二进制文档）。需要内容的话，请让用户先导出成 txt 或 md。")
            continue
        room = min(_ATTACH_PER_FILE, max(0, _ATTACH_TOTAL - used))
        snippet = text[:room]
        used += len(snippet)
        clipped = "（已截断）" if len(text) > len(snippet) else ""
        lines.append(head + f"\n  正文{clipped}：\n```\n{snippet}\n```")
    return "\n".join(lines)


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
    session: AgentSession, project: AgentProject | None = None, mode: str = ""
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
        where = "这次会话还没归入任何 Agent 项目（未归档）。"
    mode_block = f"\n\n当前模式：{MODE_RULES[mode]}" if mode in MODE_RULES else ""
    return f"""你是「小马AI工坊」里的通用 Agent。你能调用工坊自己的工具：读画布、搭画布、改节点、连线、预估成本、执行。

规矩：
1. **事实只信工具返回**：不要编造画布 id、节点 id、任务状态或产物；没查过就说没查。
2. **改之前先读**：动某张画布前先 get_canvas；改完用返回的 id 回查一次（get_canvas 或 canvas_status）再说结论。跑整图之后用 canvas_status 看节点级进度，**不要**凭感觉说「快好了」「已经完成」。确认模型是否存在时**只调一次** list_models（不传 modality 就返回全部），不要按能力分两三次查。
3. {approval_rule(session)}
4. **每次只调一个工具**，看一眼结果再决定下一步。不要一口气连调五个。**看任务进度用 wait_for_tasks 等**（一次最多等两分钟），不要连续调 get_task 轮询——同一个查询连叫三次会被系统挡回。
5. {bound}。只有任务依赖画布且没有明确目标画布时，才用 list_projects。用户确实需要新画布时才 create_project。
6. 节点类型：{kinds}。
7. 要搭画布用 draft_canvas（它会再调一次模型生成草稿，可能慢几秒），落定用 apply_canvas_draft。
8. {where}
9. 回答用中文、短句，说清「做了什么 / 对象 id / 下一步」。不要输出大段 JSON 或代码。
10. **说了就要做**：不要在回复里写「我现在去…」「接下来…」「先做…」然后结束这一轮。真要调用工具就在这一轮直接调，拿到结果再汇报；只有已经查过 / 跑过，才能下结论。收尾前自查：最后一句如果是在宣布下一步，那就还没结束。
11. 用户要「整条链跑完 / 做完直到成品」时，用 **run_canvas** 派发，再用 **wait_for_tasks** 等结果。**wait_for_tasks 会跟住整图**（单次上限 2 分钟）。它返回 done=true 时，等待已经结束，逐项看 tasks 的成功、失败与取消状态，再判断用户目标是否完成。preview_canvas 的 calls 只是**下界**，pendingNodes 要等上游跑完才知道。局部失败时先判断原因，能修复就只处理失败节点和仍缺产物的下游。付费提交结果不明时先查状态，不盲目重发。
12. **以交付物为目标**：先区分用户在问问题、要方案，还是授权执行。简单问题直接回答；多步任务先明确交付物、约束和验收条件，优先复用现有项目和产物。根据每次工具结果调整下一步。缺信息时先用低成本、可逆的手段查证；最后只问会改变结果或需要授权的问题。收尾用工具证据区分已完成、运行中和受阻。{mode_block}
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
    session: AgentSession,
    rows: list[AgentStep],
    project: AgentProject | None = None,
    *,
    replay_reasoning: bool = False,
    mode: str = "",
) -> list[dict]:
    """把步骤还原成 OpenAI 兼容的消息数组（纯函数，便于单测）。

    `replay_reasoning`：本次请求是否处于思考模式。开着的时候，**每条 assistant
    消息都要带 `reasoning_content`，哪怕历史里那一步没有思维链也要带空串**——
    实测少这个键直接 400（"The `reasoning_content` in the thinking mode must be
    passed back to the API"），空串则放行。
    """
    msgs: list[dict] = [{"role": "system", "content": build_system_prompt(session, project, mode)}]
    for st in rows:
        if st.role == "user":
            msgs.append({"role": "user", "content": st.content})
        elif st.role == "assistant":
            if st.content.strip():
                msg: dict = {"role": "assistant", "content": st.content}
                if st.reasoning or replay_reasoning:
                    msg["reasoning_content"] = st.reasoning
                msgs.append(msg)
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
            tool_msg: dict = {
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
            }
            if st.reasoning or replay_reasoning:
                tool_msg["reasoning_content"] = st.reasoning
            msgs.append(tool_msg)
            msgs.append({"role": "tool", "tool_call_id": call_id, "content": result})
    return msgs


async def _next_seq(db: AsyncSession, session_id: int) -> int:
    last = (await db.execute(
        select(AgentStep.seq).where(AgentStep.session_id == session_id)
        .order_by(AgentStep.seq.desc()).limit(1)
    )).scalar()
    return int(last or 0) + 1


async def stage_step(db: AsyncSession, session_id: int, **kw: Any) -> AgentStep:
    """建一步但**先不提交**：调用方可能需要在提交前登记等待者。

    MCP 的阻塞式确认门就是这种调用方：如果先提交、后登记，界面轮询会在这个
    极短窗口里看到一条「没人等的 pending」，把它误判成超时。
    """
    step = AgentStep(session_id=session_id, seq=await _next_seq(db, session_id), **kw)
    db.add(step)
    await db.flush()  # 拿到 id，事务仍未提交
    return step


async def add_step(db: AsyncSession, session_id: int, **kw: Any) -> AgentStep:
    step = await stage_step(db, session_id, **kw)
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


async def run_loop(
    db: AsyncSession,
    session: AgentSession,
    *,
    mode: str = "",
    kickoff: str | None = None,
    cancel: asyncio.Event | None = None,
) -> dict:
    """从当前步骤继续跑，直到模型不再调工具、或需要人确认、或到轮数上限。

    - `mode`：`/spec` `/plan` `/goal` 三条命令带来的行为约束（空 = 普通模式）。
    - `kickoff`：这次请求开头要追加的一句「续跑提示」（只在本请求里，不落库）。
    - `cancel`：目标模式下的停止信号，在每轮开头与每次工具执行前检查。
    """
    adapter, model_name, label = await resolve_brain(db, session)
    project = await project_of(db, session)
    ctx = ToolContext(db=db, session=session, model_key=session.model_key, agent_project=project)
    # 让「等任务」这类阻塞工具也能被 /goal 的停止按钮打断
    ctx.cancel = cancel
    tools = agent_tools.openai_tools()
    # 这个大脑 + 模型认不认思考模式：决定了「下发开关」与「历史里要不要补 reasoning_content」
    thinking_on = bool(session.thinking) and adapter.supports_thinking(model_name)
    rounds = 0
    nudges = 0
    recovery_reviewed = False
    tool_calls_seen = 0
    write_calls_seen = 0
    call_counts: dict[str, int] = {}
    nudge: str | None = kickoff
    try:
        while rounds < MAX_ROUNDS:
            if cancel is not None and cancel.is_set():
                return {"status": "cancelled", "reply": "已停止。", "model": label,
                        "rounds": rounds, "toolCalls": tool_calls_seen,
                        "writeCalls": write_calls_seen}
            rounds += 1
            rows = await _steps(db, session.id)
            messages = messages_from_steps(
                session, rows, project, replay_reasoning=thinking_on, mode=mode
            )
            if nudge is not None:
                # 只在这一次请求里补一句，不落库：它不是用户说的话，时间线不该多一个气泡
                messages.append({"role": "user", "content": nudge})
                nudge = None
            try:
                result = await adapter.chat_once(
                    model_name,
                    messages,
                    tools=tools,
                    thinking=thinking_on,
                )
            except AdapterError as e:
                # 不支持工具调用是最常见的失败；原样把这句话交给界面，别再包一层
                raise AdapterError(str(e), log_detail="agent_chat_failed") from e

            text = str(result.get("content") or "").strip()
            # 思维链：思考模式下上游会额外给一段；它既要在时间线上看得见，
            # 也要落库——下一轮带 tools 的请求必须把它原样回传，否则上游 400。
            reasoning = str(result.get("reasoning") or "").strip()
            calls = result.get("tool_calls") or []
            if not calls:
                # 只检查本条用户消息后的工具。旧任务的错误不该影响新的问题。
                latest_tool = next(
                    (step for step in reversed(rows) if step.role in ("user", "tool")), None
                )
                if nudges < MAX_NUDGES:
                    if not text:
                        nudge = EMPTY_REPLY_NUDGE
                    elif mode not in ("spec", "plan") and looks_like_unfinished_plan(text):
                        nudge = NUDGE_TEXT
                    elif (
                        mode not in ("spec", "plan") and not recovery_reviewed
                        and latest_tool is not None and latest_tool.role == "tool"
                        and latest_tool.status == "error"
                    ):
                        nudge = RECOVERY_NUDGE
                        recovery_reviewed = True
                    if nudge is not None:
                        # 候选收尾只发回本轮模型，避免在时间线留下未兑现的承诺。
                        nudges += 1
                        if text:
                            nudge = f"上一次拟回复：{text[:MAX_RESULT_CHARS]}\n\n{nudge}"
                        continue
                if not text:
                    text = "模型没有返回可用答复。已执行的工具及结果可以在这轮时间线查看。"
                elif mode not in ("spec", "plan") and looks_like_unfinished_plan(text):
                    text = f"有步骤尚未执行，以下是模型最后给出的进度与计划：\n\n{text}"
            if text or reasoning:
                usage = result.get("usage") or {}
                await add_step(
                    db, session.id, role="assistant", content=text, reasoning=reasoning,
                    tokens=int(usage.get("total_tokens") or 0),
                )
            if not calls:
                return {
                    "status": "done",
                    "reply": text,
                    "model": label,
                    "rounds": rounds,
                    "toolCalls": tool_calls_seen,
                    "writeCalls": write_calls_seen,
                }

            for index, call in enumerate(calls):
                # 这次响应的思维链只挂在第一条工具步上：重建历史时它是「发起 tool_calls
                # 的那个 assistant 回合」，DeepSeek 要求这个回合带 reasoning_content。
                turn_reasoning = reasoning if index == 0 else ""
                name = str(call.get("name") or "")
                spec = agent_tools.by_name(name)
                tool_calls_seen += 1
                if spec is None:
                    await add_step(
                        db, session.id, role="tool", tool_name=name or "(空)",
                        tool_args=str(call.get("arguments") or ""),
                        tool_result=json.dumps(
                            {"ok": False, "error": f"没有叫 {name} 的工具"},
                            ensure_ascii=False,
                        ),
                        reasoning=turn_reasoning,
                        status="error",
                    )
                    continue

                if mode in ("spec", "plan") and spec.tier != TIER_READ:
                    # 方案模式只出文档：写画布 / 花钱的调用当场挡回去，让模型把它写进方案里
                    await add_step(
                        db, session.id, role="tool", tool_name=name,
                        tool_args=str(call.get("arguments") or "{}"),
                        tool_result=json.dumps(
                            {
                                "ok": False,
                                "blocked_by_mode": mode,
                                "error": f"当前是 /{mode} 模式：只出方案，等用户确认后才能执行。",
                                "hint": "把这一步写进方案（要做什么、几次调用、产出什么），"
                                        "并请用户确认；不要在这个模式下真的执行。",
                            },
                            ensure_ascii=False,
                        ),
                        tier=spec.tier, reasoning=turn_reasoning, status="error",
                    )
                    continue

                if cancel is not None and cancel.is_set():
                    return {"status": "cancelled", "reply": "已停止。", "model": label,
                            "rounds": rounds, "toolCalls": tool_calls_seen,
                            "writeCalls": write_calls_seen}

                try:
                    args = _parse_args(str(call.get("arguments") or "{}"))
                except agent_tools.ToolError as e:
                    await add_step(
                        db, session.id, role="tool", tool_name=name,
                        tool_args=str(call.get("arguments") or ""),
                        tool_result=json.dumps(
                            {"ok": False, "error": str(e), "hint": e.hint}, ensure_ascii=False
                        ),
                        tier=spec.tier, reasoning=turn_reasoning, status="error",
                    )
                    continue

                # 没有成功写入时，重复查询最多执行两次。写入后允许用同一查询回查。
                call_key = f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True)}"
                tracked_read = spec.tier == TIER_READ and name != "wait_for_tasks"
                if tracked_read and call_counts.get(call_key, 0) >= 2:
                    hint = (
                        "任务仍在运行时用 wait_for_tasks 等状态变化。"
                        if name in ("get_task", "canvas_status")
                        else "用已经查到的结果推进；如果缺少信息，换一个查询范围或工具。"
                    )
                    await add_step(
                        db, session.id, role="tool", tool_name=name,
                        tool_args=json.dumps(args, ensure_ascii=False),
                        tool_result=json.dumps(
                            {
                                "ok": False,
                                "error": "同一个查询已经执行两次，本轮暂时跳过重复查询。",
                                "hint": hint,
                            },
                            ensure_ascii=False,
                        ),
                        tier=spec.tier, reasoning=turn_reasoning, status="error",
                    )
                    continue
                if tracked_read:
                    call_counts[call_key] = call_counts.get(call_key, 0) + 1

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
                        tier=spec.tier, reasoning=turn_reasoning, status="pending",
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
                        "toolCalls": tool_calls_seen,
                        "writeCalls": write_calls_seen,
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
                if spec.tier != TIER_READ and outcome.get("ok"):
                    write_calls_seen += 1
                    call_counts.clear()
                if auto_irreversible:
                    outcome = {**outcome, "autoApproved": True, "preview": auto_preview}
                await add_step(
                    db, session.id, role="tool", tool_name=name,
                    tool_args=json.dumps(args, ensure_ascii=False),
                    tool_result=json.dumps(outcome, ensure_ascii=False),
                    tier=spec.tier,
                    reasoning=turn_reasoning,
                    status="ok" if outcome.get("ok") else "error",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            # 继续下一轮：让模型看到工具结果
        # /goal 模式到头不停：外层 monitor_goal 会接着推。
        # 这里若还提示「请用 /goal 重发一次」，用户会在已经持续监测的对话里
        # 看到一句自相矛盾的「到上限了」——它并不是结束。
        if mode == "goal":
            return {"status": "max_rounds", "reply": "", "model": label,
                    "rounds": rounds, "toolCalls": tool_calls_seen,
                    "writeCalls": write_calls_seen}
        note = (
            "这一轮调用次数到上限了，先停在这里。已经派出去的任务会在后台继续跑；"
            "想看结果就发一句「继续」，或者用 /goal 让它一路推进到完成。"
        )
        await add_step(db, session.id, role="assistant", content=note)
        return {"status": "max_rounds", "reply": note, "model": label,
                "rounds": rounds, "toolCalls": tool_calls_seen,
                "writeCalls": write_calls_seen}
    finally:
        try:
            await adapter.close()
        except Exception:  # noqa: BLE001
            pass


async def send(
    db: AsyncSession,
    session: AgentSession,
    text: str,
    *,
    mode: str = "",
    cancel: asyncio.Event | None = None,
    attachments: str = "",
) -> dict:
    """用户说一句话：落库 → 跑一轮。`mode` 由斜杠命令决定（spec/plan/goal）。

    `attachments` 是随消息上传的文件说明（见 `describe_attachments`）。它拼在
    用户原话后面一起落库：历史重建要靠库里的步骤，附件内容不能只活在这一次请求里，
    否则下一轮模型就忘了自己刚读过什么。
    """
    content = (text or "").strip()
    if not content:
        raise ValueError("消息为空")
    if attachments.strip():
        content = f"{content}\n\n{attachments.strip()}"
    await add_step(db, session.id, role="user", content=content)
    if session.title in ("", "新会话"):
        session.title = content.replace("\n", " ")[:24]
    await db.commit()
    return await run_loop(db, session, mode=mode, cancel=cancel)


# ============================================================ /goal 持续监测


async def _running_tasks(db: AsyncSession, canvas_id: int) -> int:
    """画布上还有几个任务在跑（pending / processing）。

    查完要放掉读事务：SQLite 的读快照在同一个事务里不会更新，而目标模式
    等的恰恰是任务状态变化——不结束事务就会一直看到「还在跑」。
    """
    rows = await db.execute(
        select(Task.id).where(
            Task.canvas_project_id == canvas_id,
            Task.status.in_(("pending", "processing")),
        )
    )
    count = len(rows.all())
    # 结束读事务才能看到新状态；用 commit 而不是 rollback——rollback 会把
    # 调用方手里的 session / project 对象 expire，下一句访问就 MissingGreenlet。
    await db.commit()
    return count


async def _sleep_or_cancel(cancel: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(cancel.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


def goal_done(reply: str, rounds: int = 0, tool_calls: int = 0) -> dict:
    return {
        "status": "goal_done",
        "reply": reply,
        "rounds": rounds,
        "toolCalls": tool_calls,
    }


async def monitor_goal(
    db: AsyncSession,
    session: AgentSession,
    cancel: asyncio.Event,
    *,
    canvas_id: int | None = None,
    poll_seconds: float | None = None,
    max_minutes: int = GOAL_MAX_MINUTES,
) -> dict:
    """`/goal` 的持续监测：有任务在跑就等，状态一变就让模型看一眼并推进下游。

    只有「跑着的任务数发生变化」或「全部停下」时才叫模型——每 8 秒叫一次
    既贵又没有新信息。全部停下后做一次收尾检查：模型还调工具就继续，给出结论就结束。

    **画布要先按项目绑定解析**：会话自己的 `canvas_project_id` 经常是空的，
    真正绑在 Agent 项目（`AgentProject.canvas_project_id`）上。只看会话字段会
    误判成「没有绑定画布」，把正在推进的目标硬生生停掉。
    """
    project = await project_of(db, session) if db is not None else None
    canvas = (
        canvas_id
        or (project.canvas_project_id if project is not None else None)
        or session.canvas_project_id
    )

    deadline = time.monotonic() + max_minutes * 60
    # 运行期再取默认值：单测可以把节奏调快，不会因为默认参数在 import 时被固定住
    poll = GOAL_POLL_SECONDS if poll_seconds is None else poll_seconds
    # 初始的「在跑数量」当作已知：刚发完那一轮模型已经看过一次，不再立刻叫醒它
    last_running: int | None = await _running_tasks(db, canvas) if canvas else 0
    reviews = 0
    rounds = 0
    tool_calls = 0
    write_calls = 0

    while time.monotonic() < deadline and not cancel.is_set():
        running = await _running_tasks(db, canvas) if canvas else 0
        if running:
            if running != last_running:
                last_running = running
                out = await run_loop(
                    db, session, mode="goal",
                    kickoff=GOAL_PROGRESS_PROMPT.format(running=running),
                    cancel=cancel,
                )
                rounds += int(out.get("rounds") or 0)
                tool_calls += int(out.get("toolCalls") or 0)
                write_calls += int(out.get("writeCalls") or 0)
                if out.get("status") in ("need_confirm", "cancelled"):
                    return out
            await _sleep_or_cancel(cancel, poll)
            continue

        # 没有任务在跑：让模型检查目标完成没有。它还调工具说明要接着做。
        reviews += 1
        out = await run_loop(
            db, session, mode="goal",
            kickoff=GOAL_REVIEW_PROMPT if canvas else GOAL_REVIEW_NO_CANVAS_PROMPT,
            cancel=cancel,
        )
        rounds += int(out.get("rounds") or 0)
        tool_calls += int(out.get("toolCalls") or 0)
        write_calls += int(out.get("writeCalls") or 0)
        if out.get("status") in ("need_confirm", "cancelled"):
            return out
        # 只看「有没有写操作」：收尾检查里再查一次 canvas_status 只是为了核对，
        # 不该被当成「还在干活」——否则目标永远收不了尾（实测空转了 30 分钟）。
        if not out.get("writeCalls"):
            return out
        if reviews >= GOAL_MAX_REVIEWS:
            break
        # 收尾检查可能又派了节点：把它们当成已知的「在跑数量」，别下一轮立刻再叫一次
        if canvas:
            last_running = await _running_tasks(db, canvas)
        await _sleep_or_cancel(cancel, poll)

    if cancel.is_set():
        reply = "已停止持续监测。"
        if db is not None:
            await add_step(db, session.id, role="assistant", content=reply)
        return {"status": "cancelled", "reply": reply, "rounds": rounds,
                "toolCalls": tool_calls}
    reply = f"持续监测到 {max_minutes} 分钟的时限，先停在这里；需要的话再发 /goal 继续。"
    if db is not None:
        await add_step(db, session.id, role="assistant", content=reply)
    return {
        "status": "goal_timeout",
        "reply": reply,
        "rounds": rounds,
        "toolCalls": tool_calls,
    }


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
