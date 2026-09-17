"""修正火山方舟预设的 Key 识别规则：provider_presets.key_pattern（ark）。

真实 Key 长这样：`ark-<8位>-<4位>-<4位>-<4位>-<12位十六进制>-<随机尾码>`，
而 0012 里写的是裸 UUID（`3f2c1b0a-1d2e-4f5a-8b9c-0d1e2f3a4b5c`）——那是老版本方舟的形态。
结果：用户把真 Key 粘进来，应用认不出来，还拿它去试了 OpenAI / DeepSeek / Kimi 三家（全是 401）。

0012 那种「种子带新字段」的迁移对老库无效（`ensure_seed` 只插缺行、从不覆盖），
所以修规则必须单独走一条迁移。这里只改 ark 一行，且只在它仍是旧值时改，不碰用户自己填过的规则。

Revision ID: 0013_ark_key_pattern
Revises: 0012_preset_key_pattern
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa

revision = "0013_ark_key_pattern"
down_revision = "0012_preset_key_pattern"
branch_labels = None
depends_on = None

_OLD_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

# 升级后应有的规则（与 registry/schema_registry.py 的种子一致）
KEY_PATTERNS = {
    "ark": (
        r"^ark-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}-[A-Za-z0-9]+$"
        r"|" + _OLD_UUID
    ),
}

# 升级前那条（只认裸 UUID）——只有当前值还是它时才覆盖
OLD_PATTERNS = {"ark": _OLD_UUID}


def upgrade() -> None:
    conn = op.get_bind()
    for key, new in KEY_PATTERNS.items():
        conn.execute(
            sa.text(
                "UPDATE provider_presets SET key_pattern = :new "
                "WHERE key = :key AND (key_pattern = :old OR key_pattern IS NULL OR key_pattern = '')"
            ),
            {"new": new, "old": OLD_PATTERNS.get(key, ""), "key": key},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for key, old in OLD_PATTERNS.items():
        conn.execute(
            sa.text(
                "UPDATE provider_presets SET key_pattern = :old WHERE key = :key AND key_pattern = :new"
            ),
            {"old": old, "new": KEY_PATTERNS[key], "key": key},
        )
