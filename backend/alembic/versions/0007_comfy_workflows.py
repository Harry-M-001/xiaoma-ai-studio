"""ComfyUI 工作流存档表：上传 workflow_api.json 后解析参数映射存库。

Revision ID: 0007_comfy_workflows
Revises: 0006_canvas_task_link
Create Date: 2026-09-15
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_comfy_workflows"
down_revision = "0006_canvas_task_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "comfy_workflows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("graph_json", sa.Text(), nullable=False),
        sa.Column("param_map_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("output_kind", sa.String(20), nullable=False, server_default="image"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_comfy_workflows_provider", "comfy_workflows", ["provider_id"])


def downgrade() -> None:
    op.drop_index("ix_comfy_workflows_provider", table_name="comfy_workflows")
    op.drop_table("comfy_workflows")
