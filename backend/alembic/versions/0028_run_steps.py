"""整图运行检查点：canvas_runs + run_steps。

进程重启后要能接着跑、已完成节点不重复付费，靠的就是这两张表：
- `canvas_runs`：有哪些「一跑」还没收尾（以前只在内存里，重启即丢）；
- `run_steps`：这一跑里每个节点在**提交上游之前**落库的检查点
  （输入指纹 + 派出的任务 id + 状态）。

Revision ID: 0028_run_steps
Revises: 0027_drop_video_duration_options
Create Date: 2026-11-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0028_run_steps"
down_revision = "0027_drop_video_duration_options"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "canvas_runs",
        sa.Column("run_id", sa.String(length=32), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("expected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_canvas_runs_project_id", "canvas_runs", ["project_id"])
    op.create_index("ix_canvas_runs_status", "canvas_runs", ["status"])

    op.create_table(
        "run_steps",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="submitted"),
        sa.Column("task_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint(
            "run_id", "node_id", "input_hash", name="uq_run_steps_run_node_hash"
        ),
    )
    op.create_index("ix_run_steps_run_id", "run_steps", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_run_steps_run_id", table_name="run_steps")
    op.drop_table("run_steps")
    op.drop_index("ix_canvas_runs_status", table_name="canvas_runs")
    op.drop_index("ix_canvas_runs_project_id", table_name="canvas_runs")
    op.drop_table("canvas_runs")
