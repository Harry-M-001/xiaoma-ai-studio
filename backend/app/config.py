"""应用配置（从环境变量 / .env 读取）。"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_DIST = _BACKEND_DIR.parent / "frontend" / "webroot"


class Settings(BaseSettings):
    """全局配置。所有项都有默认值，开箱即可启动。"""

    # 数据目录：SQLite 数据库、本地产物、密钥都放在这里
    DATA_DIR: str = str(_BACKEND_DIR / "data")

    # 服务监听
    HOST: str = "127.0.0.1"
    PORT: int = 8787

    # 可选访问口令；留空表示免登录（纯本机使用）
    APP_PASSWORD: str = ""

    # 前端构建产物目录（生产模式由后端托管）
    FRONTEND_DIST: str = str(_DEFAULT_DIST)

    # 调用上游模型接口的超时（秒）
    REQUEST_TIMEOUT: int = 300

    # 开发模式允许的前端来源
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def data_dir(self) -> Path:
        p = Path(self.DATA_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def storage_dir(self) -> Path:
        p = self.data_dir / "storage"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def database_path(self) -> Path:
        return self.data_dir / "xiaoma.db"

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.database_path.as_posix()}"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def frontend_dist(self) -> Path:
        return Path(self.FRONTEND_DIST)


settings = Settings()
