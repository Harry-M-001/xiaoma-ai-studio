"""数据库迁移入口（Alembic）。

- 全新数据库：直接 `upgrade head`，自动建出全部表。
- 旧版数据库（此前用 create_all 建过表、没有 alembic_version 表）：
  先 `stamp` 到基线版本，再 `upgrade head`，避免重复建表。
- 迁移在独立线程中同步执行，不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.config import settings

logger = logging.getLogger("xiaoma.migrations")

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_BASELINE = "0001_initial"
_LEGACY_TABLES = {"provider_services", "chat_sessions", "chat_messages", "tasks", "assets"}


def _sync_url() -> str:
    return f"sqlite:///{settings.database_path.as_posix()}"


def _alembic_config() -> Config:
    cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", _sync_url())
    return cfg


def run_migrations_sync() -> None:
    cfg = _alembic_config()
    engine = create_engine(_sync_url())
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    if "alembic_version" not in tables and (tables & _LEGACY_TABLES):
        logger.info("检测到旧版数据库，标记基线版本 %s", _BASELINE)
        command.stamp(cfg, _BASELINE)

    command.upgrade(cfg, "head")


async def run_migrations() -> None:
    await asyncio.to_thread(run_migrations_sync)
