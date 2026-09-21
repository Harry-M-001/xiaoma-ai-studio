"""自动链：文档生成（创意 → 小说 → 剧本 → 分镜）。

设计原则（详见《自动链与导演风格架构方案》2.1 与 2.3）:

1. **内容层全是 Markdown 正文**，只让 LLM 写内容；JSON 只用于节点拓扑与资产清单。
   旧实现在剧本/分镜/资产三步用 JSON，结果就是「丢内容」——这里不重蹈覆辙。
2. **单次调用输出上限固定**（默认 2000 tokens）。超长内容必须分块续写，
   否则必然被截断，这正是自动链最容易翻车的地方。
3. 模板占位符一律走安全替换（未知占位符原样保留），用户改了提示词也不会 500。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Callable

from . import camera_moves

# 单条链最多分多少块（防止误填参数导致一次烧掉几十次调用）
MAX_CHUNKS = 30

_PLACEHOLDER = re.compile(r"\{(\w+)\}")

# 有些规则**必须由代码附在系统提示词末尾**，而不是写在可编辑的提示词里。
#
# 判据是「它是不是与下游的契约」：这类规则改没了不会报错，只会静默降级
# （模型自造说法 → 解析认不出 → 样片按默认动效走），而界面上看不出来。
#
# 现实原因同样重要：`config_center.ensure_seed` **从不覆盖已存在的行**，
# 所以任何只写在提示词种子里的规则，对所有升级上来的用户都不存在——而他们正是多数。
# 实测过这一条：升级后的库里那份分镜提示词仍是旧文本，没有运镜词表。
HARD_RULES: dict[str, Callable[[], str]] = {
    "storyboard": camera_moves.prompt_block,
}


def hard_rules_for(agent_key: str) -> str:
    """这一岗由代码附加的硬规则（没有则空串）。"""
    maker = HARD_RULES.get(str(agent_key or "").strip())
    return maker() if maker else ""


def apply_hard_rules(spec: "AgentSpec") -> "AgentSpec":
    """把硬规则接在系统提示词末尾。**排在最后**，因此优先于用户改过的正文。"""
    block = hard_rules_for(spec.key)
    if not block:
        return spec
    return replace(spec, system_prompt=f"{spec.system_prompt.rstrip()}\n\n{block}")


@dataclass
class AgentSpec:
    """AgentPrompt 行快照（脱离 ORM 会话后继续使用）。"""

    key: str
    label: str
    system_prompt: str
    user_template: str
    plan_prompt: str
    chunk_prompt: str
    chunk_param: str
    model_key: str
    temperature: float
    max_tokens: int


def to_spec(row: Any) -> AgentSpec:
    return AgentSpec(
        key=row.key,
        label=row.label or row.key,
        system_prompt=row.system_prompt or "",
        user_template=row.user_template or "",
        plan_prompt=row.plan_prompt or "",
        chunk_prompt=row.chunk_prompt or "",
        chunk_param=row.chunk_param or "",
        model_key=row.model_key or "",
        temperature=float(row.temperature or 0.8),
        max_tokens=int(row.max_tokens or 2000),
    )


def render(template: str, **values: Any) -> str:
    """安全占位符替换：{key} 有值就替换，未知占位符原样保留，绝不抛异常。"""

    def _sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in values:
            return m.group(0)
        v = values[key]
        return "" if v is None else str(v)

    return _PLACEHOLDER.sub(_sub, template or "")


def resolve_total(spec: AgentSpec, node_params: dict[str, Any]) -> int:
    """块数 = 节点上 chunk_param 指定参数的值（如 chapterCount），限制在 1..MAX_CHUNKS。"""
    if not spec.chunk_param:
        return 1
    raw = node_params.get(spec.chunk_param)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(n, MAX_CHUNKS))


def has_plan(spec: AgentSpec) -> bool:
    """是否需要先出大纲/清单再逐块续写。"""
    return bool(spec.plan_prompt.strip())


def needs_chunking(spec: AgentSpec, total: int) -> bool:
    """总块数 > 1 或配了大纲提示词时，走分块流程。"""
    return bool(spec.chunk_prompt.strip()) and (total > 1 or has_plan(spec))


def _wrap(spec: AgentSpec, user: str) -> list[dict]:
    messages: list[dict] = []
    if spec.system_prompt.strip():
        messages.append({"role": "system", "content": spec.system_prompt})
    messages.append({"role": "user", "content": user})
    return messages


def single_messages(spec: AgentSpec, content: str, extra: str, total: int = 1) -> list[dict]:
    """一次成文（不分块）。"""
    return _wrap(spec, render(spec.user_template, content=content, params=extra, total=total))


def plan_messages(spec: AgentSpec, content: str, extra: str, total: int) -> list[dict]:
    """第一次调用：只出大纲 / 清单。"""
    return _wrap(spec, render(spec.plan_prompt, content=content, params=extra, total=total))


def chunk_messages(
    spec: AgentSpec, content: str, extra: str, total: int, index: int, plan: str
) -> list[dict]:
    """逐块调用：第 index 块（从 1 开始）。"""
    return _wrap(
        spec,
        render(
            spec.chunk_prompt,
            content=content,
            params=extra,
            total=total,
            index=index,
            plan=plan,
        ),
    )


def compose_document(plan: str, chunks: list[str], with_plan: bool = True) -> str:
    """拼接成最终 Markdown 文档。大纲放在最前，便于用户回顾与人工修改。"""
    parts: list[str] = []
    if with_plan and plan.strip():
        parts.append(plan.strip())
    parts.extend(c.strip() for c in chunks if c and c.strip())
    return "\n\n".join(parts)


def progress_for(done: int, total: int, base: int = 10, span: int = 80) -> int:
    """分块进度：base..base+span。"""
    if total <= 0:
        return base
    return min(base + span, base + int(span * done / total))
