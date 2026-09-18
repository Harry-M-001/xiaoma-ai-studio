"""启用音频能力，并把导航里新增的「配音」插到「视频生成」之后。

两件事都属于「老库缺、新库不受影响」的那一类——`ensure_seed` 只插缺行、**从不覆盖**
已有行，所以改了种子对已经跑起来的库等于没改：

1. `modalities` 里的 `audio` 行是当初设计时就留下的预留位（`enabled=False`，注释写着
   「把 enabled 打开即可启用音频能力」）。现在真要用它，老库里那一行还是关着的，
   必须显式打开；新库由种子直接开启。
2. 导航顺序：新库里「配音」排在「视频生成」之后（种子重排过 `sort_order`），
   老库只会多出一行、顺序落到「资产库」后面。这里按新默认值重排——**但只动仍是
   旧默认值的那几行**：用户自己调过顺序的行一律不碰，那是他的设置，不是我们的兜底。

Revision ID: 0015_audio_modality
Revises: 0014_task_run_id
Create Date: 2026-09-18
"""

from alembic import op
import sqlalchemy as sa

revision = "0015_audio_modality"
down_revision = "0014_task_run_id"
branch_labels = None
depends_on = None

# 导航：key → (旧默认 sort_order, 新默认 sort_order)。
# 只在这几行仍是旧默认值时重排，用户改过的顺序不会被覆盖。
NAV_ORDER = {
    "assets": (5, 6),
    "prompts": (6, 7),
    "tasks": (7, 8),
    "director": (8, 9),
    "workflow": (9, 10),
    "community": (10, 11),
}


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE modalities SET enabled = 1 WHERE key = 'audio'"))
    for key, (old, new) in NAV_ORDER.items():
        conn.execute(
            sa.text(
                "UPDATE nav_items SET sort_order = :new WHERE key = :key AND sort_order = :old"
            ),
            {"new": new, "old": old, "key": key},
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE modalities SET enabled = 0 WHERE key = 'audio'"))
    for key, (old, new) in NAV_ORDER.items():
        conn.execute(
            sa.text(
                "UPDATE nav_items SET sort_order = :old WHERE key = :key AND sort_order = :new"
            ),
            {"new": new, "old": old, "key": key},
        )
