"""tasks 表挂画布关联列：canvas_project_id / canvas_node_id。

画布节点执行复用统一任务体系，节点 → Task 的映射靠这两列查询。

Revision ID: 0006_canvas_task_link
Revises: 0005_projects
Create Date: 2026-09-14
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_canvas_task_link"
down_revision = "0005_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("canvas_project_id", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("canvas_node_id", sa.String(64), nullable=True))
    op.create_index("ix_tasks_canvas_node", "tasks", ["canvas_project_id", "canvas_node_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_canvas_node", table_name="tasks")
    op.drop_column("tasks", "canvas_node_id")
    op.drop_column("tasks", "canvas_project_id")
