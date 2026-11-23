"""画布修订号 + 运行模式（增量 / 全量）。

- `projects.canvas_revision`：每次保存 +1。保存时带上「我看到的版本」，
  落后就返回 409，避免两个标签页互相覆盖。
- `canvas_runs.incremental`：这一跑是不是只派输入变过的节点。落库是为了重启续跑时
  沿用当时的模式——「全量重跑」跑到一半重启变成增量，会少跑一半。

Revision ID: 0029_canvas_revision
Revises: 0028_run_steps
Create Date: 2026-11-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0029_canvas_revision"
down_revision = "0028_run_steps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("canvas_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "canvas_runs",
        sa.Column("incremental", sa.Boolean(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("canvas_runs", "incremental")
    op.drop_column("projects", "canvas_revision")
