"""测试公共夹具：用例改了全局 `settings` 必须自动复原。

背景：好几个用例为了不写进仓库的 `data/`，直接写 `settings.DATA_DIR = tmp`
就开工，跑完 `TemporaryDirectory` 一关、目录没了，可 `settings.DATA_DIR`
还指着那个已删除的路径。于是同一个 pytest 进程里**后面的用例**（甚至别的文件）
都拿着一条失效的数据目录，表现为「本地单独跑绿、全量跑红」这类查不动的假失败。

这里做两件事，都按用例粒度：
1. 开工前把 `settings` 里会被用例改的字段全部记下来，收工后逐个写回——
   不管用例是用 monkeypatch、`with` 还是直接赋值，都兜得住。
2. 每个用例给一份**独立的空数据目录**：既不污染仓库 `data/`（里面是真实数据库和密钥），
   也让「全新空库」从第一个用例起就成立。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402

# 用例里真实出现过（或被显式夹带）的全局配置项。新增可写配置时往这里加一行。
_MUTABLE_FIELDS = (
    "DATA_DIR",
    "HOST",
    "PORT",
    "APP_PASSWORD",
    "XMS_HARNESS",
    "FRONTEND_DIST",
    "REQUEST_TIMEOUT",
    "CORS_ORIGINS",
)


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    """把全局 engine 指向的那个库先迁到 head。

    `app.database` 在 import 时就把连接串定死了，用例里的临时 `DATA_DIR` 改不动它。
    所以「换一台干净机器 / CI 空目录」时，凡是真正走接口的用例都会撞上「表不存在」——
    这正是审查里记的那两项失败。这里跑一次迁移（已是最新时是幂等的），
    让「全新空库全绿」从测试入口就成立，不必先手工启动一次应用。
    """
    from app.migrations import run_migrations_sync

    run_migrations_sync()


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path):
    saved = {name: getattr(settings, name) for name in _MUTABLE_FIELDS}
    settings.DATA_DIR = str(tmp_path / "data")
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(settings, name, value)
