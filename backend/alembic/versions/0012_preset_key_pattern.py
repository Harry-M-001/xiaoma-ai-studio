"""服务商预设增加 Key 识别规则：provider_presets.key_pattern。

「粘贴一个 Key 自动接入」要靠它认出这个 Key 是哪家的。规则放在配置表里而不是写死在代码里，
用户自己加的服务商也能教应用认形状。

为什么这条迁移不能只靠种子：`ensure_seed` 只插缺失的行、**从不覆盖已有行**（这是有意的——
用户改过的配置优先）。所以老库里那 8 条内置预设不会因为 seed 里新增了 key_pattern 就自动拿到，
必须在这里补一次。补的范围严格限定在「内置的这几个 key」且「当前为空」的行上，
不会覆盖用户自己填过的规则。

Revision ID: 0012_preset_key_pattern
Revises: 0011_task_retry_link
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa

revision = "0012_preset_key_pattern"
down_revision = "0011_task_retry_link"
branch_labels = None
depends_on = None

# 与 registry/schema_registry.py 里的种子保持一致（那边是新装时的来源，这边管老库）
KEY_PATTERNS = {
    "openai": r"^sk-proj-[A-Za-z0-9_-]{20,}$",
    "deepseek": r"^sk-[0-9a-f]{32}$",
    "kimi": r"^sk-[A-Za-z0-9]{48,}$",
    "qwen": r"^sk-[0-9a-f]{32}$",
    "zhipu": r"^[0-9a-f]{32}\.[A-Za-z0-9]{8,}$",
    "ark": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    "openrouter": r"^sk-or-v1-[A-Za-z0-9]{16,}$",
}


def upgrade() -> None:
    op.add_column(
        "provider_presets",
        sa.Column("key_pattern", sa.String(length=300), nullable=False, server_default=""),
    )
    conn = op.get_bind()
    for key, pattern in KEY_PATTERNS.items():
        conn.execute(
            sa.text(
                "UPDATE provider_presets SET key_pattern = :pattern "
                "WHERE key = :key AND (key_pattern IS NULL OR key_pattern = '')"
            ),
            {"pattern": pattern, "key": key},
        )


def downgrade() -> None:
    op.drop_column("provider_presets", "key_pattern")
