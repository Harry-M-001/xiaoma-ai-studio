"""提示词库：新增 prompts 配置表，并启用导航中的「提示词库」入口。

Revision ID: 0004_prompts
Revises: 0003_provider_presets
Create Date: 2026-09-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_prompts"
down_revision: Union[str, None] = "0003_provider_presets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "prompts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("modality", sa.String(length=30), nullable=False, server_default="image"),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("tags", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_prompts_key", "prompts", ["key"], unique=True)
    op.create_index("ix_prompts_modality", "prompts", ["modality"])

    # 提示词库模块随本版本交付，启用导航入口（此前为预留隐藏项，无用户偏好可覆盖）
    op.execute("UPDATE nav_items SET enabled = 1 WHERE key = 'prompts'")


def downgrade() -> None:
    op.execute("UPDATE nav_items SET enabled = 0 WHERE key = 'prompts'")
    op.drop_index("ix_prompts_modality", table_name="prompts")
    op.drop_index("ix_prompts_key", table_name="prompts")
    op.drop_table("prompts")
