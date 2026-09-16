"""Alembic 运行环境。

- 连接串由 app/migrations.py 动态注入（指向数据目录里的 SQLite 文件）。
- render_as_batch=True：SQLite 修改列需要批处理模式，保证后续迁移可写。
"""

from __future__ import annotations

import os
import sys
import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# 让 alembic 能 import 到 app.*
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from app.database import Base  # noqa: E402
from app import models  # noqa: E402,F401  确保所有模型完成注册

config = context.config

# 只在「直接跑 alembic CLI」时接管日志配置。
#
# alembic 的 fileConfig 会**替换掉根 logger 的全部 handler**。而应用启动时
# （lifespan → init_db → 这里）根 logger 已经装配好了控制台与脱敏文件 handler，
# 再调一次 fileConfig 会把它们全冲掉——症状是应用日志一条都不落盘、
# 连「启动完成」那行都看不到，而且完全不报错。
# 所以已经有 handler 时就不动它。
if config.config_file_name is not None and not logging.getLogger().handlers:
    try:
        fileConfig(config.config_file_name)
    except Exception:  # noqa: BLE001  日志配置缺失不应中断迁移
        pass

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {}) or {}
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
