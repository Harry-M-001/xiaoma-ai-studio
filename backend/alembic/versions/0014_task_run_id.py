"""预算闸：tasks.run_id（把「一次运行」派出的任务串起来）。

为什么需要它：整图运行是**一次点击派出几十次调用**的入口，而「这次实际派了多少、成了几条、
失败几条」在事后必须能算出来。原本只能按 canvas_project_id 把该项目的全部任务捞出来 ——
那是「这个项目历来所有任务」，不是「刚才这一跑」，两者混在一起就没法对账。

run_id 由 API 层在受理运行请求时生成（不落 runs 表：这一版要的是「跑完立刻对账」，
预估本身由前端持有；要不要把每次运行当成一条记录长期存下来，等真的要做运行历史时再说）。

Revision ID: 0014_task_run_id
Revises: 0013_ark_key_pattern
Create Date: 2026-09-18
"""

from alembic import op
import sqlalchemy as sa

revision = "0014_task_run_id"
down_revision = "0013_ark_key_pattern"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("run_id", sa.String(length=32), nullable=True))
    op.create_index("ix_tasks_run_id", "tasks", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_run_id", table_name="tasks")
    op.drop_column("tasks", "run_id")
