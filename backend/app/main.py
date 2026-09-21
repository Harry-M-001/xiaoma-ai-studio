"""小马AI工坊 后端入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8787
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.providers.base import AdapterError

from app import APP_NAME, __version__
from app.config import settings
from app.database import SessionLocal, init_db
from app.routers import (
    admin,
    audio,
    canvas,
    chat,
    comfy,
    director,
    engines,
    generation,
    logs,
    meta,
    projects,
    providers,
    share,
    subtitles,
    system,
    update,
)
from app.services import (
    config_center_service,
    engine_install,
    ffmpeg_service,
    log_service,
    ollama_service,
)
from app.services.runner import runner

# 控制台保留完整信息（开发/本机排查用），文件那份会脱敏后再写，
# 因为文件是可能被导出、被发出去的那一份。详见 services/log_service.py。
log_service.setup_logging()
logger = logging.getLogger("xiaoma")


async def _seed_config() -> None:
    """补齐配置种子数据（幂等：已存在的不覆盖），并加载运行期配置缓存。"""
    async with SessionLocal() as db:
        await config_center_service.ensure_seed(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # 迁移（alembic）历史上会用自己的 fileConfig 重置根 logger 的 handler，
    # 所以跑完迁移再强制装一次日志，避免「启动之后什么都不落盘」。
    log_service.setup_logging(force=True)
    await _seed_config()
    await runner.recover()
    # 运行巡检：`recover()` 只在重启时兜一次，跑着的时候没人管「轮询协程静默结束、
    # 任务永远停在 processing」这个洞。循环跟着应用一起起、一起停。
    patrol_task = asyncio.create_task(runner.patrol_loop())
    # 先把「本机有没有 Ollama」探一次，让首屏那条引导横幅不用等探测：
    # 冷探测实测要 0.6–2.6 秒，而带缓存之后只要十几毫秒。
    asyncio.create_task(ollama_service.detect())
    # 字幕滤镜（libass）探一次：字幕设置页要拿它决定「能不能烧」，
    # 不预热的话那一次请求会同步等一个几十毫秒的外部进程，而且首屏可能先于预热拿到
    # 「还没探过 = 没有」这个错的结论。
    asyncio.create_task(ffmpeg_service.warm_subtitle_filter())
    name = config_center_service.runtime_value("app.name", APP_NAME)
    logger.info("%s v%s 启动完成：http://%s:%s", name, __version__, settings.HOST, settings.PORT)
    yield
    patrol_task.cancel()
    try:
        await patrol_task
    except asyncio.CancelledError:
        pass
    # 正在下载的本机引擎要停下来：不然进程关了、那条 httpx 连接还挂着写文件
    await engine_install.shutdown()
    runner.shutdown()


app = FastAPI(title=APP_NAME, version=__version__, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(system.router)
app.include_router(meta.router)
app.include_router(providers.router)
app.include_router(chat.router)
app.include_router(generation.router)
app.include_router(audio.router)
app.include_router(director.router)
app.include_router(subtitles.router)
app.include_router(engines.router)
app.include_router(projects.router)
app.include_router(canvas.router)
app.include_router(share.router)
app.include_router(comfy.router)
app.include_router(update.router)
app.include_router(logs.router)
app.include_router(admin.router)


@app.exception_handler(AdapterError)
async def adapter_error_handler(request: Request, exc: AdapterError) -> JSONResponse:
    """模型服务配置/调用类错误统一返回 400，消息可直接展示。"""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ---------- 静态资源：媒体文件 + 前端构建产物 ----------

app.mount("/media", StaticFiles(directory=settings.storage_dir), name="media")

_dist: Path = settings.frontend_dist
if _dist.exists():
    class SPAStaticFiles(StaticFiles):
        """找不到文件时回退 index.html，交给前端处理路由。"""

        async def get_response(self, path: str, scope):  # type: ignore[override]
            # Windows 下 Mount 传来的 path 可能用反斜杠分隔
            rel = path.replace("\\", "/").lstrip("/")
            try:
                response = await super().get_response(path, scope)
            except StarletteHTTPException:
                # StaticFiles 对缺失路径直接抛 404 异常
                if rel.startswith("assets/"):
                    # 旧 hash 产物 404 就让它 404，不能拿 HTML 冒充 JS
                    raise
                return FileResponse(_dist / "index.html", headers={"Cache-Control": "no-cache"})
            if response.status_code == 404:
                if rel.startswith("assets/"):
                    return response
                response = FileResponse(_dist / "index.html")
            # index.html 永远不走缓存，保证发版后浏览器立即拿到新 bundle 引用
            if str(getattr(response, "path", "")).endswith("index.html"):
                response.headers["Cache-Control"] = "no-cache"
            return response

    app.mount("/", SPAStaticFiles(directory=str(_dist), html=True), name="spa")
else:
    @app.get("/")
    async def no_frontend() -> dict:
        return {
            "app": APP_NAME,
            "hint": "前端尚未构建：开发模式请运行 frontend 的 npm run dev；或执行 start.bat 一键启动",
        }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", settings.HOST),
        port=int(os.getenv("PORT", settings.PORT)),
        reload=False,
    )
