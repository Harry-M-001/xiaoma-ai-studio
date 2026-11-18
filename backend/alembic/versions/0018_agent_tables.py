"""Agent 工作台（2.0）：会话与步骤两张表。

为什么单独两张表、而不是复用 `chat_sessions/messages`：聊天的产物是文字，
Agent 的产物是**工坊里的对象**（画布、任务、资产），每一步还带参数、耗时与权限级别。
混进聊天表会让「文本对话」那页莫名其妙多出一堆工具记录。

Revision ID: 0018_agent_tables
Revises: 0017_nav_agent_group
Create Date: 2026-11-18
"""

from alembic import op
import sqlalchemy as sa

revision = "0018_agent_tables"
down_revision = "0017_nav_agent_group"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(120), nullable=False, server_default="新会话"),
        sa.Column("model_key", sa.String(200), nullable=False, server_default=""),
        sa.Column("canvas_project_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_agent_sessions_created_at", "agent_sessions", ["created_at"])
    op.create_index("ix_agent_sessions_canvas_project_id", "agent_sessions", ["canvas_project_id"])

    op.create_table(
        "agent_steps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("role", sa.String(20), nullable=False, server_default="assistant"),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool_name", sa.String(80), nullable=False, server_default=""),
        sa.Column("tool_args", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool_result", sa.Text(), nullable=False, server_default=""),
        sa.Column("tier", sa.String(20), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="ok"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_agent_steps_session_id", "agent_steps", ["session_id"])
    op.create_index("ix_agent_steps_created_at", "agent_steps", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_steps_created_at", table_name="agent_steps")
    op.drop_index("ix_agent_steps_session_id", table_name="agent_steps")
    op.drop_table("agent_steps")
    op.drop_index("ix_agent_sessions_canvas_project_id", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_created_at", table_name="agent_sessions")
    op.drop_table("agent_sessions")
