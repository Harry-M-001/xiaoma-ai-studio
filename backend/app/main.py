"""小马AI工坊 后端入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8787
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import os
from contextlib import asynccontextmanager
from email.utils import formatdate
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.providers.base import AdapterError

from app import APP_NAME, __version__
from app.config import settings
from app.database import SessionLocal, init_db
from app.deps import has_valid_token, require_auth
from app.routers import (
    admin,
    agent,
    audio,
    canvas,
    chat,
    comfy,
    director,
    engines,
    interpolate,
    generation,
    harness,
    logs,
    mcp,
    meta,
    projects,
    providers,
    share,
    stats,
    subtitles,
    system,
    update,
    upscale,
    workflows,
)
from app.services import (
    canvas_runner,
    config_center_service,
    engine_install,
    ffmpeg_service,
    keyed_tasks,
    log_service,
    mcp_gate,
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
    # 任务接回来之后，再让中断的整图接着走：已经跑完的节点按 run_steps 复用，
    # 不再重新付一次钱；说不清是否已提交的节点标成 unknown，等人确认。
    await canvas_runner.resume_runs()
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
    # 挂着等确认的 MCP 调用随进程一起结束，不让它悬在 dsh 那一侧
    mcp_gate.cleanup_stale()
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
app.include_router(upscale.router)
app.include_router(interpolate.router)
app.include_router(projects.router)
app.include_router(canvas.router)
app.include_router(agent.router)
if settings.XMS_HARNESS:
    app.include_router(mcp.router)
    app.include_router(harness.router)
app.include_router(share.router)
app.include_router(comfy.router)
app.include_router(update.router)
app.include_router(logs.router)
app.include_router(admin.router)
app.include_router(workflows.router)
app.include_router(stats.router)


@app.exception_handler(AdapterError)
async def adapter_error_handler(request: Request, exc: AdapterError) -> JSONResponse:
    """模型服务配置/调用类错误统一返回 400，消息可直接展示。"""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ---------- 静态资源：媒体文件 + 前端构建产物 ----------

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".m4v", ".mkv"}


@app.get("/media-thumb/{rel:path}", dependencies=[Depends(require_auth)])
async def media_thumb(rel: str, w: int = 320):
    """列表用的缩略图（默认 320px）。图片缩放，视频抽第 1 秒的一帧当封面。

    原文件仍走 `/media/`，生成链路不受影响。资产页一屏几十张原图（实测平均 757 KB），
    视频还会各发一次 range 请求；缩略图/封面只有几十 KB。首次请求生成并落盘缓存。
    """
    root = settings.storage_dir.resolve()
    src = (root / rel).resolve()
    if (root != src and root not in src.parents) or not src.is_file():
        return JSONResponse(status_code=404, content={"detail": "not found"})
    suffix = src.suffix.lower()
    if suffix not in _IMAGE_SUFFIXES and suffix not in _VIDEO_SUFFIXES:
        return JSONResponse(status_code=404, content={"detail": "not found"})
    width = max(64, min(int(w or 320), 1024))
    cache = settings.data_dir / "thumbnails" / f"{rel}.{width}.jpg"
    fresh = cache.is_file() and cache.stat().st_mtime >= src.stat().st_mtime
    if not fresh:
        # single-flight：同一份原图 + 同一尺寸的并发请求只跑一次 ffmpeg，
        # 其余请求等这次的结果。生成成功就返回缓存，失败再退回原图。
        ok = await keyed_tasks.run_once(
            f"thumb:{rel}:{width}",
            lambda: ffmpeg_service.make_thumbnail(src, cache, width),
        )
        if not ok:
            # 缩不出来就退回原图：宁可多传一点，也不要给用户一堆碎图
            return FileResponse(src)
    return FileResponse(
        cache, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"}
    )


class AuthStaticFiles(StaticFiles):
    """带口令校验的媒体目录。

    `/media` 下的原片是 `<img>` / `<video>` 直接请求的，加不了自定义请求头，
    所以这里与 API 同源但不同通道：认登录时种下的 HttpOnly Cookie，也认
    `?access_token=`（复制直链的场景）。校验通过后仍走 StaticFiles，
    Range（拖动进度条）、ETag、304 都照旧，不自己实现一套。
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        if not has_valid_token(Request(scope)):
            return JSONResponse(status_code=401, content={"detail": "未授权，请先输入访问口令"})
        return await super().get_response(path, scope)


app.mount("/media", AuthStaticFiles(directory=settings.storage_dir), name="media")

_dist: Path = settings.frontend_dist
# 前端构建产物里这些后缀是文本，gzip 收益大；图片/字体/媒体不碰
_TEXT_ASSET_SUFFIXES = {".js", ".mjs", ".css", ".html", ".json", ".svg", ".map"}
# 压缩结果按「路径 + mtime」缓存：构建产物文件名带 hash，内容不会变；
# 不缓存的话每次刷新都要重压 200 KB，本地也能感觉到十几毫秒的额外延迟。
_GZIP_CACHE: dict[str, tuple[float, bytes]] = {}


def _gzip_asset(rel: str, scope) -> tuple[bytes, float] | None:
    """接受 gzip 且体积值得压时，返回 (压缩后的字节, mtime)。

    只在 SPA 静态资源上用，**不挂全局中间件**：全局 GZip 会把 `/media/` 下的
    mp4 也缓存进内存再压一遍，既浪费 CPU，又会破坏 Range 请求。
    """
    path = _dist / rel
    try:
        mtime = path.stat().st_mtime
        size = path.stat().st_size
    except OSError:
        return None
    if size < 1024:
        return None
    if "gzip" not in Headers(scope=scope).get("accept-encoding", "").lower():
        return None
    cached = _GZIP_CACHE.get(rel)
    if cached is not None and cached[0] == mtime:
        return cached[1], mtime
    try:
        packed = gzip.compress(path.read_bytes(), 6)
    except OSError:
        return None
    if len(_GZIP_CACHE) > 200:  # 老 hash 文件越积越多时整体丢掉，别无限长
        _GZIP_CACHE.clear()
    _GZIP_CACHE[rel] = (mtime, packed)
    return packed, mtime


if _dist.exists():
    class SPAStaticFiles(StaticFiles):
        """找不到文件时回退 index.html，交给前端处理路由。"""

        async def get_response(self, path: str, scope):  # type: ignore[override]
            # Windows 下 Mount 传来的 path 可能用反斜杠分隔
            rel = path.replace("\\", "/").lstrip("/")
            # API 不能回退到 index.html：没挂载的接口（比如默认关闭的 /api/mcp）
            # 应该如实 404，拿一段 HTML 回去会让调用方以为「调通了」。
            if rel.startswith("api/"):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
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
            # 带 hash 的构建产物可以永久缓存；文件名一变就是新请求
            if rel.startswith("assets/") and response.status_code == 200:
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            suffix = Path(rel).suffix.lower()
            if response.status_code == 200 and suffix in _TEXT_ASSET_SUFFIXES:
                packed = _gzip_asset(rel, scope)
                if packed is not None:
                    body, mtime = packed
                    out = Response(
                        body,
                        headers={
                            "Content-Encoding": "gzip",
                            "Vary": "Accept-Encoding",
                            "Cache-Control": response.headers.get("Cache-Control", ""),
                        },
                    )
                    for key in ("Content-Type", "ETag", "Last-Modified"):
                        if key in response.headers:
                            out.headers[key] = response.headers[key]
                    if "Last-Modified" not in out.headers and mtime:
                        out.headers["Last-Modified"] = formatdate(mtime, usegmt=True)
                    return out
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
