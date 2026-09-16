"""导演风格库：风格卡表。

风格卡一行 = 一套镜头语言 + 光影色调 + 三组提示词片段，
可在「系统设置 → 导演风格」里改，改完画布上的下拉立即生效（注册制）。

Revision ID: 0010_director_styles
Revises: 0009_asset_identity
Create Date: 2026-09-16
"""

from alembic import op
import sqlalchemy as sa

revision = "0010_director_styles"
down_revision = "0009_asset_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "director_styles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(50), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        # 给 LLM 的风格要求（可含导演名）与给生图/生视频模型的技法片段（不含导演名）
        sa.Column("agent_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("image_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("video_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("negative_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_director_styles_key", "director_styles", ["key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_director_styles_key", table_name="director_styles")
    op.drop_table("director_styles")
