"""资产身份：给资产补「资产名 + 类型」两列，供资产链按名引用。

背景：资产链要求「下游提示词里提到某个角色名，就自动挂上这个角色的设定图」。
原 assets 表只有 filename / prompt / task_id，没有稳定的业务名可匹配，
所以这里补两列（老数据留空即可，不影响任何既有逻辑）。

Revision ID: 0009_asset_identity
Revises: 0008_agent_prompts
Create Date: 2026-09-16
"""

from alembic import op
import sqlalchemy as sa

revision = "0009_asset_identity"
down_revision = "0008_agent_prompts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("assets") as batch:
        batch.add_column(sa.Column("name", sa.String(80), nullable=False, server_default=""))
        batch.add_column(sa.Column("category", sa.String(20), nullable=False, server_default=""))
    op.create_index("ix_assets_name", "assets", ["name"])


def downgrade() -> None:
    op.drop_index("ix_assets_name", table_name="assets")
    with op.batch_alter_table("assets") as batch:
        batch.drop_column("category")
        batch.drop_column("name")
