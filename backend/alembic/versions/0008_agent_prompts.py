"""创作 Agent 提示词表：自动链每个阶段一份系统提示词 + 用户消息模板。

Revision ID: 0008_agent_prompts
Revises: 0007_comfy_workflows
Create Date: 2026-09-15
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_agent_prompts"
down_revision = "0007_comfy_workflows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_prompts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(50), nullable=False),
        sa.Column("label", sa.String(100), nullable=False, server_default=""),
        sa.Column("stage", sa.String(30), nullable=False, server_default="document"),
        sa.Column("system_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("user_template", sa.Text(), nullable=False, server_default=""),
        sa.Column("var_hint", sa.Text(), nullable=False, server_default=""),
        sa.Column("plan_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("chunk_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("chunk_param", sa.String(50), nullable=False, server_default=""),
        sa.Column("model_key", sa.String(200), nullable=False, server_default=""),
        sa.Column("temperature", sa.Float(), nullable=False, server_default="0.8"),
        sa.Column("max_tokens", sa.Integer(), nullable=False, server_default="2000"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_agent_prompts_key", "agent_prompts", ["key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_agent_prompts_key", table_name="agent_prompts")
    op.drop_table("agent_prompts")
