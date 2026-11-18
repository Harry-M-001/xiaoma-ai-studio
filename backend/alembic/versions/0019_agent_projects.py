"""Agent 项目层：把会话（任务）按项目分开，上下文互不串。

为什么加这一层：v2.0.0 的会话是平铺的——「选角」和「剪片」的会话混在一起，
用户每开一个新任务都得重新说清「我们现在在做哪件事」。项目把这件事固定下来：
一个项目挂一张画布、一组会话；会话继承项目的画布绑定。

Revision ID: 0019_agent_projects
Revises: 0018_agent_tables
Create Date: 2026-11-18
"""

from alembic import op
import sqlalchemy as sa

revision = "0019_agent_projects"
down_revision = "0018_agent_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_projects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("canvas_project_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_agent_projects_created_at", "agent_projects", ["created_at"])
    op.create_index("ix_agent_projects_canvas_project_id", "agent_projects", ["canvas_project_id"])

    # 会话挂到项目上；历史会话留空（界面上进「未归档」），**不自动塞进某个项目**
    op.add_column("agent_sessions", sa.Column("agent_project_id", sa.Integer(), nullable=True))
    op.create_index("ix_agent_sessions_agent_project_id", "agent_sessions", ["agent_project_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_sessions_agent_project_id", table_name="agent_sessions")
    op.drop_column("agent_sessions", "agent_project_id")
    op.drop_index("ix_agent_projects_canvas_project_id", table_name="agent_projects")
    op.drop_index("ix_agent_projects_created_at", table_name="agent_projects")
    op.drop_table("agent_projects")
