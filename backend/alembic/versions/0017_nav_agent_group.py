"""导航改造：加「Agent」入口、「项目」改名「自由画布」、四个创作页收进「创作台」组。

为什么需要这个迁移：`ensure_seed` 只**插缺行**、从不覆盖（见 `registry/schema_registry.py`），
所以新库会直接带上改好的名称与分组，而**已经在跑的库**一个都不会变——
用户升级上来会看到「项目」还在、侧边栏还是十几个入口、也没有 Agent。
这里按新默认值逐项补上。

四条分寸（与 `0016_engines_nav` 同口径）：

1. **用户改过的名称一律不碰**：只在仍是旧默认值（`项目` / `配音`）时才改名。
   有人把「项目」改成了「我的片子」，那是他的选择，不该被一次升级抹掉。
2. **分组同理**：只在 `group_name` 仍是 `main` 时才挪进 `create`。
3. **插入是幂等的**：已经有 `agent` 这一行时什么都不做（重复跑不会插出第二行）。
4. **`sort_order` 刻意不动**：四个创作页保留各自的老值（2–5）——组内只要相对顺序对就够，
   而重排会牵动 `test_frontend_speech.py` 里那条「种子顺序必须与迁移说的同一件事」的用例，
   收益却是零。`agent` 用 2：它在 `main` 组里落在 `projects`(1) 与 `assets`(6) 之间，
   与 `chat` 的 2 不冲突（两者分组不同，前端分组后才渲染）。

Revision ID: 0017_nav_agent_group
Revises: 0016_engines_nav
Create Date: 2026-09-25
"""

from alembic import op
import sqlalchemy as sa

revision = "0017_nav_agent_group"
down_revision = "0016_engines_nav"
branch_labels = None
depends_on = None

# key → (旧名称, 新名称)。只在仍是旧名称时才改。
RENAME = {
    "projects": ("项目", "自由画布"),
    "speech": ("配音", "音频生成"),
}

# 要挪进「创作台」折叠组的项
MOVE_TO_CREATE = ("chat", "image", "video", "speech")

AGENT_ROW = {
    "key": "agent",
    "label": "Agent",
    "icon": "bot",
    "route": "agent",
    "group_name": "main",
    "sort_order": 2,
}


def upgrade() -> None:
    conn = op.get_bind()

    # 1) 改名：只在仍是旧默认值时动手
    for key, (old, new) in RENAME.items():
        conn.execute(
            sa.text(
                "UPDATE nav_items SET label = :new WHERE key = :key AND label = :old"
            ),
            {"key": key, "old": old, "new": new},
        )

    # 2) 收进折叠组：只在还挂在 main 时才挪
    for key in MOVE_TO_CREATE:
        conn.execute(
            sa.text(
                "UPDATE nav_items SET group_name = 'create' "
                "WHERE key = :key AND group_name = 'main'"
            ),
            {"key": key},
        )

    # 3) 新增 Agent 入口：幂等
    exists = conn.execute(
        sa.text("SELECT COUNT(*) FROM nav_items WHERE key = :key"),
        {"key": AGENT_ROW["key"]},
    ).scalar()
    if not exists:
        conn.execute(
            sa.text(
                "INSERT INTO nav_items "
                "(key, label, icon, route, group_name, sort_order, enabled, requires_auth, version) "
                "VALUES (:key, :label, :icon, :route, :group_name, :sort_order, 1, 0, 1)"
            ),
            AGENT_ROW,
        )


def downgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text("DELETE FROM nav_items WHERE key = :key"), {"key": AGENT_ROW["key"]}
    )

    for key, (old, new) in RENAME.items():
        conn.execute(
            sa.text(
                "UPDATE nav_items SET label = :old WHERE key = :key AND label = :new"
            ),
            {"key": key, "old": old, "new": new},
        )

    for key in MOVE_TO_CREATE:
        conn.execute(
            sa.text(
                "UPDATE nav_items SET group_name = 'main' "
                "WHERE key = :key AND group_name = 'create'"
            ),
            {"key": key},
        )
