"""服务商预设：把原先写死在前端的预设迁到配置表。

Revision ID: 0003_provider_presets
Revises: 0002_config_domain
Create Date: 2026-09-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_provider_presets"
down_revision: Union[str, None] = "0002_config_domain"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "provider_presets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False, server_default="openai"),
        sa.Column("base_url", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("models_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("hint", sa.Text(), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_provider_presets_key", "provider_presets", ["key"], unique=True)


def downgrade() -> None:
    op.drop_table("provider_presets")
