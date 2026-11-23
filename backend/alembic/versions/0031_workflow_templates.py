"""工作流模板：workflow_templates。

把一段画布（节点 + 连线 + 输入说明）存下来，插到别的项目里复用。
内置的三份模板由代码提供（不占表），这张表只放用户自己存下来的。

Revision ID: 0031_workflow_templates
Revises: 0030_project_shots
Create Date: 2026-11-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0031_workflow_templates"
down_revision = "0030_project_shots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("doc_json", sa.Text(), nullable=False),
        sa.Column("inputs_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("requirements_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
    )


def downgrade() -> None:
    op.drop_table("workflow_templates")
