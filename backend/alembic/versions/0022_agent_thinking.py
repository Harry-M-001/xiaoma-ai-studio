"""Agent 思考模式：会话开关 + 每步思维链。

为什么两列都要：

- `agent_sessions.thinking` 是**用户意图**（这个会话要不要先想再答）；
- `agent_steps.reasoning` 是**上游真给回来的思维链**。它不能只留在内存里——
  DeepSeek 的思考模式在**带 tools 的请求**里要求把历史轮次的 `reasoning_content`
  原样回传，缺了直接 400（实测：round 2 报 must be passed back）。所以它和
  content 一样属于「可回放的最小集」，必须落库。

默认开（与 DeepSeek V4 的默认一致）；关掉时对 DeepSeek 显式下发
`thinking={"type":"disabled"}`，不靠上游默认值——默认值改一次，行为就变了。

Revision ID: 0022_agent_thinking
Revises: 0021_model_catalog
Create Date: 2026-11-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0022_agent_thinking"
down_revision = "0021_model_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_sessions",
        sa.Column("thinking", sa.Boolean(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        "agent_steps",
        sa.Column("reasoning", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("agent_steps", "reasoning")
    op.drop_column("agent_sessions", "thinking")
