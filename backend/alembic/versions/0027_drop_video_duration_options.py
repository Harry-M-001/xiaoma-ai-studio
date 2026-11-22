"""视频时长不再用档位表。

v2.2.0 把视频时长改成「按模型连续可调」：接口校验走
`provider_store.video_duration_limit`，前端是数字输入。`param_options` 里
5 / 10 / 15 这三行已经没有人读——但「系统设置 → 参数选项」还会把它们列出来，
用户改了也不会影响出片。这里把它们删掉，避免一份看起来能用、实际不生效的配置。

预算里的默认值仍由 `defaults.video_duration` 控制，不受影响。

Revision ID: 0027_drop_video_duration_options
Revises: 0026_ark_bailian_presets
Create Date: 2026-11-22
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0027_drop_video_duration_options"
down_revision = "0026_ark_bailian_presets"
branch_labels = None
depends_on = None

OLD = [
    ("5", "5 秒", 1, True),
    ("10", "10 秒", 2, True),
    ("15", "15 秒", 3, False),
]


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM param_options WHERE kind = 'video_duration'"))


def downgrade() -> None:
    conn = op.get_bind()
    for value, label, order, enabled in OLD:
        conn.execute(
            sa.text(
                "INSERT INTO param_options "
                "(kind, value, label, meta_json, sort_order, enabled, version) "
                "VALUES ('video_duration', :v, :l, '{}', :o, :e, 1)"
            ),
            {"v": value, "l": label, "o": order, "e": 1 if enabled else 0},
        )
