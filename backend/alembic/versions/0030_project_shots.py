"""镜头工作台：project_shots（稳定 shot_id + 审核状态）。

分镜表本来就逐镜有编号，但那个编号只活在文本里：任务重新生成、镜头重排之后就分不清
「这一条是哪一镜的」。这里把「项目里的第几镜」落成一行，第一次见到时分配一个稳定
`shot_id`，之后按镜补跑与审核都认它。老项目不迁移内容，第一次列工作台时按镜号补号。

Revision ID: 0030_project_shots
Revises: 0029_canvas_revision
Create Date: 2026-11-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0030_project_shots"
down_revision = "0029_canvas_revision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_shots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("shot_no", sa.String(length=16), nullable=False),
        sa.Column("shot_id", sa.String(length=32), nullable=False),
        sa.Column("heading", sa.Text(), nullable=False, server_default=""),
        sa.Column("dialogue", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("project_id", "shot_no", name="uq_project_shots_no"),
    )
    op.create_index("ix_project_shots_project_id", "project_shots", ["project_id"])
    op.create_index("ix_project_shots_shot_id", "project_shots", ["shot_id"])
    op.create_index("ix_project_shots_status", "project_shots", ["status"])


def downgrade() -> None:
    op.drop_index("ix_project_shots_status", table_name="project_shots")
    op.drop_index("ix_project_shots_shot_id", table_name="project_shots")
    op.drop_index("ix_project_shots_project_id", table_name="project_shots")
    op.drop_table("project_shots")
