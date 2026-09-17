"""任务重跑血缘：tasks.retry_of_task_id。

「重新生成」出来的是新任务（原记录保留作对照），记下原任务 id 之后，
任务中心就能区分「这是重跑」和「这是首次」，也能顺着链看到改了什么。

Revision ID: 0011_task_retry_link
Revises: 0010_director_styles
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa

revision = "0011_task_retry_link"
down_revision = "0010_director_styles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("retry_of_task_id", sa.Integer(), nullable=True))
    op.create_index("ix_tasks_retry_of_task_id", "tasks", ["retry_of_task_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_retry_of_task_id", table_name="tasks")
    op.drop_column("tasks", "retry_of_task_id")
