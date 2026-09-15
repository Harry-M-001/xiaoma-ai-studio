"""配置域：系统配置、参数选项、能力类型、导航、配置审计。

Revision ID: 0002_config_domain
Revises: 0001_initial
Create Date: 2026-09-14

这五张表由「schema 注册制」统一管理（见 app/registry/schema_registry.py），
注册后自动获得通用 CRUD / 乐观锁 / 审计 / 回滚 / 管理 API。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_config_domain"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "config_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("group_name", sa.String(length=50), nullable=False, server_default="basic"),
        sa.Column("label", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("value_type", sa.String(length=20), nullable=False, server_default="string"),
        sa.Column("value_json", sa.Text(), nullable=False, server_default='""'),
        sa.Column("is_secret", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_config_items_key", "config_items", ["key"], unique=True)

    op.create_table(
        "param_options",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("value", sa.String(length=100), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("meta_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_param_options_kind", "param_options", ["kind"])

    op.create_table(
        "modalities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=30), nullable=False),
        sa.Column("label", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("icon", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_modalities_key", "modalities", ["key"], unique=True)

    op.create_table(
        "nav_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=50), nullable=False),
        sa.Column("label", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("icon", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("route", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("group_name", sa.String(length=50), nullable=False, server_default="main"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("requires_auth", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_nav_items_key", "nav_items", ["key"], unique=True)

    op.create_table(
        "config_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("table_name", sa.String(length=50), nullable=False),
        sa.Column("row_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("before_json", sa.Text(), nullable=True),
        sa.Column("after_json", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(length=50), nullable=False, server_default="local"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_config_audit_logs_table_name", "config_audit_logs", ["table_name"])
    op.create_index("ix_config_audit_logs_created_at", "config_audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("config_audit_logs")
    op.drop_table("nav_items")
    op.drop_table("modalities")
    op.drop_table("param_options")
    op.drop_table("config_items")
