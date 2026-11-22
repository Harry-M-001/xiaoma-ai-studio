"""DeepSeek 模型目录更新到 V4.1 Flash（API 名 `deepseek-flash`）。

2026-09-10 官方发布 V4.1 Flash，2026-10-17 升级到 V4.1-Flash-1017；官方公告写明：

- 想用最新模型，模型名改成 **`deepseek-flash`**；
- `deepseek-v4-flash` 出于兼容会被**路由到 V4.1 Flash**（暂时）；
- `deepseek-v4-pro` 计划下线，期间的请求同样被路由到 V4.1 Flash 并按它的价格计费。

做法分三层：

1. **出厂清单**只留 `deepseek-flash`——新装的人不该再看到三个同义模型；
2. **已有服务**里旧名改成 `deepseek-flash`，同时把旧名留成 `hidden` 的兼容别名：
   下拉里看不到，但**解析仍认**——老任务重跑、老画布节点不至于因为改个目录就报
   「模型已从服务配置中移除」；
3. **引用旧名的地方跟着改**：`agent_sessions.model_key` 与 `projects.canvas_json`
   里的 `"<sid>:<旧名>"` 一律换成 `"<sid>:deepseek-flash"`。不改的话，Agent 页
   找不到这个 key，思考开关会被判成「当前模型不支持」而置灰——实际它背后就是
   DeepSeek（这也是这次用户报的问题）。

Revision ID: 0023_deepseek_v41_flash
Revises: 0022_agent_thinking
Create Date: 2026-11-20
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa

revision = "0023_deepseek_v41_flash"
down_revision = "0022_agent_thinking"
branch_labels = None
depends_on = None

OLD_NAMES = [
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-chat",
    "deepseek-reasoner",
]
NEW_NAME = "deepseek-flash"
NEW_ITEM = {"name": NEW_NAME, "modality": "text", "label": "DeepSeek V4.1 Flash"}
ALIAS_LABELS = {
    "deepseek-v4-flash": "DeepSeek V4 Flash（兼容别名）",
    "deepseek-v4-pro": "DeepSeek V4 Pro（兼容别名）",
    "deepseek-chat": "DeepSeek Chat（兼容别名）",
    "deepseek-reasoner": "DeepSeek Reasoner（兼容别名）",
}
PREV_ITEMS = [
    {"name": "deepseek-v4-flash", "modality": "text", "label": "DeepSeek V4 Flash"},
    {"name": "deepseek-v4-pro", "modality": "text", "label": "DeepSeek V4 Pro"},
]
NEW_HINT = (
    "最新是 DeepSeek V4.1 Flash（`deepseek-flash`）：原生多模态、1M 上下文、"
    "支持工具调用与思考模式；旧名 v4-flash / v4-pro 已被官方路由到它。"
)
OLD_HINT = (
    "V4 全系 1M 上下文；v4-flash 便宜、v4-pro 更强。"
    "旧名 deepseek-chat / deepseek-reasoner 已于 2026-07-24 弃用。"
)


def _dump(items: list[dict]) -> str:
    return json.dumps(items, ensure_ascii=False)


def _load(raw: str) -> list[dict]:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict)]


def _to_new(raw: str) -> str:
    """旧名收成 deepseek-flash，并把旧名留成隐藏别名（可解析、不进下拉）。"""
    data = _load(raw)
    out = [item for item in data if str(item.get("name") or "") not in OLD_NAMES]
    if not any(str(d.get("name") or "") == NEW_NAME for d in out):
        out.insert(0, dict(NEW_ITEM))
    have = {str(d.get("name") or "") for d in out}
    for name in OLD_NAMES:
        if name not in have:
            out.append({
                "name": name,
                "modality": "text",
                "label": ALIAS_LABELS[name],
                "hidden": True,
            })
    return _dump(out)


def _to_prev(raw: str) -> str:
    """回滚：去掉 deepseek-flash 与隐藏别名，恢复 v4-flash / v4-pro。"""
    data = _load(raw)
    out = [
        d for d in data
        if str(d.get("name") or "") != NEW_NAME and not bool(d.get("hidden"))
    ]
    have = {str(d.get("name") or "") for d in out}
    for item in PREV_ITEMS:
        if item["name"] not in have:
            out.append(dict(item))
    return _dump(out)


def _deepseek_rows(conn) -> list:
    rows = conn.execute(
        sa.text("SELECT id, base_url, models_json FROM provider_services")
    ).fetchall()
    return [r for r in rows if "api.deepseek.com" in (r.base_url or "").lower()]


def _rewrite_keys(conn, ids: list[int], table: str, column: str, olds: list[str], new: str) -> None:
    """把 `<sid>:<旧名>` 换成 `<sid>:<新名>`。

    `agent_sessions.model_key` 是裸字符串，`projects.canvas_json` 是整份 JSON——
    两种情况下 `sid:旧名` 这段字样是一样的，所以直接做精确字符串替换即可；
    带上服务 id 之后不会误伤提示词里的普通文字。
    """
    if not ids:
        return
    rows = conn.execute(sa.text(f"SELECT id, {column} AS val FROM {table}")).fetchall()
    for row in rows:
        val = row.val or ""
        if not val:
            continue
        out = val
        for sid in ids:
            for old in olds:
                out = out.replace(f"{sid}:{old}", f"{sid}:{new}")
        if out != val:
            conn.execute(
                sa.text(f"UPDATE {table} SET {column} = :v WHERE id = :i"),
                {"v": out, "i": row.id},
            )


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("UPDATE provider_presets SET models_json = :m, hint = :h WHERE key = 'deepseek'"),
        {"m": _dump([NEW_ITEM]), "h": NEW_HINT},
    )

    rows = _deepseek_rows(conn)
    ids = [int(r.id) for r in rows]
    for row in rows:
        conn.execute(
            sa.text("UPDATE provider_services SET models_json = :m WHERE id = :i"),
            {"m": _to_new(row.models_json), "i": row.id},
        )

    _rewrite_keys(conn, ids, "agent_sessions", "model_key", OLD_NAMES, NEW_NAME)
    _rewrite_keys(conn, ids, "projects", "canvas_json", OLD_NAMES, NEW_NAME)


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("UPDATE provider_presets SET models_json = :m, hint = :h WHERE key = 'deepseek'"),
        {"m": _dump(PREV_ITEMS), "h": OLD_HINT},
    )

    rows = _deepseek_rows(conn)
    ids = [int(r.id) for r in rows]
    for row in rows:
        conn.execute(
            sa.text("UPDATE provider_services SET models_json = :m WHERE id = :i"),
            {"m": _to_prev(row.models_json), "i": row.id},
        )

    _rewrite_keys(conn, ids, "agent_sessions", "model_key", [NEW_NAME], "deepseek-v4-flash")
    _rewrite_keys(conn, ids, "projects", "canvas_json", [NEW_NAME], "deepseek-v4-flash")
