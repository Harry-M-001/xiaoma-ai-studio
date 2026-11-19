"""Agent 会话的审批方式：手动（默认）/ 自动。

自动审批的会话里，`run_canvas` / `run_node` 不再停下来等人点，直接按预览的
调用次数放行（与手动确认走同一套预算口径）。默认仍是手动——花钱的操作不该
因为「上次开过自动」而悄悄继续自动。

Revision ID: 0020_agent_auto_approve
Revises: 0019_agent_projects
Create Date: 2026-11-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0020_agent_auto_approve"
down_revision = "0019_agent_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_sessions",
        sa.Column("auto_approve", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("agent_sessions", "auto_approve")
