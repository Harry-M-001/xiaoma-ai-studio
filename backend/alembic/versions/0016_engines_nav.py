"""导航里新增「本机引擎」，并把它插到「模型服务」之后。

为什么需要这个迁移：`ensure_seed` 只**插缺行**、从不覆盖，所以新库会直接带上这一行
（见 `registry/schema_registry.py` 里的 `seed`），而**已经在跑的库**不会——
它只会多出一行、顺序还落到「系统设置」后面。这里按新默认值补上。

两条分寸（与 `0015_audio_modality` 同口径）：
1. **用户自己调过顺序的行一律不碰**——只把仍是旧默认值（`settings` = 2）的那一行挪到 3。
2. 插入是**幂等**的：已经有 `engines` 这一行时什么都不做（重复跑迁移不会插出第二行）。

Revision ID: 0016_engines_nav
Revises: 0015_audio_modality
Create Date: 2026-09-21
"""

from alembic import op
import sqlalchemy as sa

revision = "0016_engines_nav"
down_revision = "0015_audio_modality"
branch_labels = None
depends_on = None

NAV_KEY = "engines"


def upgrade() -> None:
    conn = op.get_bind()
    exists = conn.execute(
        sa.text("SELECT COUNT(*) FROM nav_items WHERE key = :key"), {"key": NAV_KEY}
    ).scalar()
    if not exists:
        conn.execute(
            sa.text(
                "INSERT INTO nav_items "
                "(key, label, icon, route, group_name, sort_order, enabled, requires_auth, version) "
                "VALUES (:key, '本机引擎', 'cpu', 'engines', 'settings', 2, 1, 0, 1)"
            ),
            {"key": NAV_KEY},
        )
    # 「系统设置」仍停在旧默认值 2 时才挪到 3；用户改过就不动
    conn.execute(
        sa.text("UPDATE nav_items SET sort_order = 3 WHERE key = 'settings' AND sort_order = 2")
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM nav_items WHERE key = :key"), {"key": NAV_KEY})
    conn.execute(
        sa.text("UPDATE nav_items SET sort_order = 2 WHERE key = 'settings' AND sort_order = 3")
    )
