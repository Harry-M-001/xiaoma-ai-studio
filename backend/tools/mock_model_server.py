"""本地模拟模型服务：按火山方舟 / 阿里百炼的公开 API 形状，假出图、假出片。

用途：在没有真实 Key、也不想花钱的情况下，把画布上「图片 → 视频」整条链路跑通，
用它做 9 组模型组合的回归测试。

覆盖：
- 方舟 Ark：Seedream 5.5 / 5.0 Pro 图片、Seedance 2.0 / 2.5 视频
- 百炼 DashScope：Qwen-Image 2.1 图片、Wan 3.0 视频

请求头按官方文档校验（缺了就回对应的 4xx，而不是默默放行）：
- 两家都要 `Authorization: Bearer <key>`
- 两家都要 `Content-Type: application/json`
- 百炼的异步任务还要 `X-DashScope-Async: enable`

启动：
    backend\\venv\\Scripts\\python.exe backend\\tools\\mock_model_server.py --port 8790

Base URL：
- 方舟：http://127.0.0.1:8790/ark/api/v3
- 百炼：http://127.0.0.1:8790/bailian/api/v1

每次请求都会追加到 `--log` 指定的 JSONL（默认 `mock_requests.jsonl`），
用来核对「我们发出去的请求头/参数确实符合文档」。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, ImageDraw, ImageFont

app = FastAPI(title="小马 AI 工坊 · 模拟模型服务")

MEDIA_DIR = Path(tempfile.gettempdir()) / "xiaoma-mock-media"
LOG_PATH = Path("mock_requests.jsonl")
PUBLIC_BASE = "http://127.0.0.1:8790"
ARK_KEY = "mock-ark-key"
BAILIAN_KEY = "mock-bailian-key"

ARK_MODELS = {
    "doubao-seedream-5-5-261028": "image",
    "doubao-seedream-5-0-pro-260628": "image",
    "doubao-seedance-2-0-260128": "video",
    "doubao-seedance-2-5-260628": "video",
}
BAILIAN_MODELS = {
    "qwen-image-2.1": "image",
    "wan3.0-video": "video",
}

# 任务表：task_id -> {provider, model, kind, duration, size, polls, created}
_TASKS: dict[str, dict[str, Any]] = {}


# --------------------------------------------------------------------------
# 日志与鉴权
# --------------------------------------------------------------------------


def _log(entry: dict[str, Any]) -> None:
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _bearer(request: Request) -> str:
    raw = request.headers.get("authorization", "")
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return ""


def _auth_error(provider: str, code: str = "InvalidApiKey", status: int = 401) -> JSONResponse:
    request_id = f"mock-{uuid.uuid4().hex[:12]}"
    if provider == "ark":
        body = {
            "error": {
                "code": code,
                "message": "The API key in the request is invalid or missing.",
                "type": "AuthenticationError",
            }
        }
    else:
        body = {"code": code, "message": "Invalid API-key provided.", "request_id": request_id}
    _log({"provider": provider, "event": "auth_failed", "code": code, "status": status})
    return JSONResponse(status_code=status, content=body)


def _check_json(request: Request, provider: str) -> JSONResponse | None:
    ct = (request.headers.get("content-type") or "").lower()
    if "application/json" not in ct:
        _log({"provider": provider, "event": "bad_content_type", "content_type": ct})
        return JSONResponse(
            status_code=415,
            content={"error": {"code": "UnsupportedMediaType", "message": "Content-Type must be application/json"}},
        )
    return None


# --------------------------------------------------------------------------
# 假素材生成
# --------------------------------------------------------------------------


def _color_for(name: str, salt: int = 0) -> tuple[int, int, int]:
    h = hashlib.sha256(f"{name}:{salt}".encode()).digest()
    return 40 + h[0] % 180, 40 + h[1] % 180, 40 + h[2] % 180


def _render_png(model: str, prompt: str, size: str) -> bytes:
    """按模型名生成一张可辨认的占位图（不同模型颜色不同）。"""
    width, height = 768, 432
    raw = (size or "").lower()
    if "x" in raw:
        try:
            w, h = (int(x) for x in raw.split("x", 1))
            if w > 0 and h > 0:
                scale = min(768 / w, 432 / h, 1.0)
                width, height = max(64, int(w * scale)), max(64, int(h * scale))
        except ValueError:
            pass
    base = _color_for(model)
    img = Image.new("RGB", (width, height), base)
    draw = ImageDraw.Draw(img)
    # 斜向渐变条纹：一眼能看出这不是真模型出的图
    for i in range(0, width + height, 24):
        c = tuple(min(255, v + 28) for v in base)
        draw.line([(i, 0), (i - height, height)], fill=c, width=6)
    font = ImageFont.load_default()
    lines = [
        "MOCK OUTPUT",
        model,
        (prompt or "").encode("ascii", "replace").decode("ascii")[:80],
        f"{width}x{height}",
    ]
    y = max(8, height // 2 - 40)
    for line in lines:
        draw.text((16, y), line, fill=(255, 255, 255), font=font)
        y += 18
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _render_mp4(model: str, prompt: str, duration: int) -> Path:
    """生成一段 2–4 秒的纯色测试片，交给真 ffmpeg，产物能被 ffprobe 正常识别。"""
    seconds = max(2, min(int(duration or 4), 4))
    name = f"mock-{hashlib.sha1(f'{model}:{seconds}'.encode()).hexdigest()[:12]}.mp4"
    dst = MEDIA_DIR / name
    if dst.is_file() and dst.stat().st_size > 0:
        return dst
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg 不可用，无法生成模拟视频")
    color = "0x%02x%02x%02x" % _color_for(model)
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c={color}:s=480x270:d={seconds}:r=24",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dst),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    if proc.returncode != 0 or not dst.is_file():
        raise RuntimeError(f"ffmpeg 生成失败：{(err or b'')[-200:].decode('utf-8', 'replace')}")
    return dst


def _file_url(path: Path) -> str:
    return f"{PUBLIC_BASE}/files/{path.name}"


def _new_task(provider: str, model: str, kind: str, **extra: Any) -> str:
    task_id = f"mock-{uuid.uuid4().hex[:16]}"
    _TASKS[task_id] = {
        "provider": provider,
        "model": model,
        "kind": kind,
        "polls": 0,
        "created": time.time(),
        **extra,
    }
    return task_id


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/files/{name}")
async def files(name: str):
    path = MEDIA_DIR / Path(name).name
    if not path.is_file():
        return JSONResponse(status_code=404, content={"detail": "not found"})
    media = "video/mp4" if path.suffix == ".mp4" else "image/png"
    return FileResponse(path, media_type=media)


# --------------------------------------------------------------------------
# 方舟 Ark
# --------------------------------------------------------------------------


@app.get("/ark/api/v3/models")
async def ark_models(request: Request):
    if not _bearer(request):
        return _auth_error("ark")
    _log({"provider": "ark", "event": "list_models"})
    return {"data": [{"id": name, "object": "model"} for name in ARK_MODELS]}


@app.post("/ark/api/v3/images/generations")
async def ark_image(request: Request):
    if not _bearer(request):
        return _auth_error("ark")
    bad = _check_json(request, "ark")
    if bad:
        return bad
    body = await request.json()
    model = str(body.get("model") or "")
    if ARK_MODELS.get(model) != "image":
        _log({"provider": "ark", "event": "unknown_image_model", "model": model})
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "ModelNotFound", "message": f"model {model} not found"}},
        )
    has_ref = bool(body.get("image"))
    _log({
        "provider": "ark", "event": "image", "model": model,
        "size": body.get("size"), "response_format": body.get("response_format"),
        "ref_images": len(body.get("image") or []), "has_ref": has_ref,
        "prompt_len": len(str(body.get("prompt") or "")),
        "auth_ok": _bearer(request) in (ARK_KEY, "mock-any"),
    })
    data = _render_png(model, str(body.get("prompt") or ""), str(body.get("size") or ""))
    await asyncio.sleep(0.15)
    return {
        "model": model,
        "created": int(time.time()),
        "data": [{"b64_json": base64.b64encode(data).decode("ascii"), "size": body.get("size")}],
        "usage": {"generated_images": 1},
    }


@app.post("/ark/api/v3/contents/generations/tasks")
async def ark_video_submit(request: Request):
    if not _bearer(request):
        return _auth_error("ark")
    bad = _check_json(request, "ark")
    if bad:
        return bad
    body = await request.json()
    model = str(body.get("model") or "")
    if ARK_MODELS.get(model) != "video":
        _log({"provider": "ark", "event": "unknown_video_model", "model": model})
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "ModelNotFound", "message": f"model {model} not found"}},
        )
    content = body.get("content") or []
    roles = [c.get("role") for c in content if isinstance(c, dict)]
    _log({
        "provider": "ark", "event": "video_submit", "model": model,
        "duration": body.get("duration"), "ratio": body.get("ratio"),
        "resolution": body.get("resolution"), "roles": roles,
        "content_types": [c.get("type") for c in content if isinstance(c, dict)],
        "auth_ok": _bearer(request) in (ARK_KEY, "mock-any"),
    })
    task_id = _new_task(
        "ark", model, "video",
        duration=int(body.get("duration") or 4),
        ratio=body.get("ratio"), resolution=body.get("resolution"),
    )
    return {"id": task_id}


@app.get("/ark/api/v3/contents/generations/tasks/{task_id}")
async def ark_video_poll(task_id: str, request: Request):
    if not _bearer(request):
        return _auth_error("ark")
    task = _TASKS.get(task_id)
    if task is None or task["kind"] != "video" or task["provider"] != "ark":
        return JSONResponse(status_code=404, content={"error": {"code": "TaskNotFound", "message": task_id}})
    task["polls"] += 1
    if task["polls"] <= 1:
        return {"id": task_id, "model": task["model"], "status": "running"}
    video = await _render_mp4(task["model"], "", task["duration"])
    return {
        "id": task_id,
        "model": task["model"],
        "status": "succeeded",
        "content": {"video_url": _file_url(video)},
        "usage": {"completion_tokens": 108900},
    }


# --------------------------------------------------------------------------
# 阿里百炼 DashScope
# --------------------------------------------------------------------------


def _async_header_missing(request: Request, provider: str = "bailian") -> JSONResponse | None:
    if (request.headers.get("x-dashscope-async") or "").lower() != "enable":
        _log({"provider": provider, "event": "missing_async_header"})
        return JSONResponse(
            status_code=400,
            content={
                "code": "InvalidParameter",
                "message": "X-DashScope-Async header is required for this asynchronous API.",
                "request_id": f"mock-{uuid.uuid4().hex[:12]}",
            },
        )
    return None


@app.get("/bailian/api/v1/tasks/0")
async def bailian_probe(request: Request):
    if not _bearer(request):
        return _auth_error("bailian")
    # 和真实服务一致：鉴权通过但任务不存在
    return JSONResponse(
        status_code=404,
        content={"code": "TaskNotFound", "message": "task not found", "request_id": "mock-probe"},
    )


@app.post("/bailian/api/v1/services/aigc/image-generation/generation")
async def bailian_image_submit(request: Request):
    if not _bearer(request):
        return _auth_error("bailian")
    bad = _check_json(request, "bailian") or _async_header_missing(request)
    if bad:
        return bad
    body = await request.json()
    model = str(body.get("model") or "")
    if BAILIAN_MODELS.get(model) != "image":
        _log({"provider": "bailian", "event": "unknown_image_model", "model": model})
        return JSONResponse(
            status_code=400,
            content={"code": "InvalidParameter", "message": f"Unsupported model: {model}", "request_id": "mock"},
        )
    params = body.get("parameters") or {}
    _log({
        "provider": "bailian", "event": "image_submit", "model": model,
        "size": params.get("size"), "n": params.get("n"),
        "auth_ok": _bearer(request) in (BAILIAN_KEY, "mock-any"),
    })
    task_id = _new_task("bailian", model, "image", size=params.get("size"), n=int(params.get("n") or 1))
    return {"output": {"task_id": task_id, "task_status": "PENDING"}, "request_id": f"mock-{task_id}"}


@app.post("/bailian/api/v1/services/aigc/video-generation/video-synthesis")
async def bailian_video_submit(request: Request):
    if not _bearer(request):
        return _auth_error("bailian")
    bad = _check_json(request, "bailian") or _async_header_missing(request)
    if bad:
        return bad
    body = await request.json()
    model = str(body.get("model") or "")
    if BAILIAN_MODELS.get(model) != "video":
        _log({"provider": "bailian", "event": "unknown_video_model", "model": model})
        return JSONResponse(
            status_code=400,
            content={"code": "InvalidParameter", "message": f"Unsupported model: {model}", "request_id": "mock"},
        )
    params = body.get("parameters") or {}
    media = (body.get("input") or {}).get("media") or []
    _log({
        "provider": "bailian", "event": "video_submit", "model": model,
        "duration": params.get("duration"), "ratio": params.get("ratio"),
        "resolution": params.get("resolution"), "media_types": [m.get("type") for m in media if isinstance(m, dict)],
        "auth_ok": _bearer(request) in (BAILIAN_KEY, "mock-any"),
    })
    task_id = _new_task(
        "bailian", model, "video",
        duration=int(params.get("duration") or 4),
        ratio=params.get("ratio"), resolution=params.get("resolution"),
    )
    return {"output": {"task_id": task_id, "task_status": "PENDING"}, "request_id": f"mock-{task_id}"}


@app.get("/bailian/api/v1/tasks/{task_id}")
async def bailian_task(task_id: str, request: Request):
    if not _bearer(request):
        return _auth_error("bailian")
    task = _TASKS.get(task_id)
    if task is None or task["provider"] != "bailian":
        return JSONResponse(
            status_code=404,
            content={"code": "TaskNotFound", "message": "task not found", "request_id": "mock"},
        )
    task["polls"] += 1
    if task["polls"] <= 1:
        return {"output": {"task_id": task_id, "task_status": "RUNNING"}, "request_id": f"mock-{task_id}"}
    if task["kind"] == "image":
        count = max(1, min(int(task.get("n") or 1), 9))
        results = []
        for i in range(count):
            png = _render_png(task["model"], "", str(task.get("size") or ""))
            name = f"mock-{uuid.uuid4().hex[:12]}-{i}.png"
            (MEDIA_DIR / name).write_bytes(png)
            results.append({"url": _file_url(MEDIA_DIR / name)})
        return {
            "output": {"task_id": task_id, "task_status": "SUCCEEDED", "results": results},
            "usage": {"image_count": count},
            "request_id": f"mock-{task_id}",
        }
    video = await _render_mp4(task["model"], "", task["duration"])
    return {
        "output": {"task_id": task_id, "task_status": "SUCCEEDED", "video_url": _file_url(video)},
        "usage": {"video_duration": task["duration"]},
        "request_id": f"mock-{task_id}",
    }


@app.post("/bailian/api/v1/services/aigc/multimodal-generation/generation")
async def bailian_qwen_image(request: Request):
    """Qwen-Image 的同步多模态出图端点（图片按 URL 回，不回 base64）。"""
    if not _bearer(request):
        return _auth_error("bailian")
    bad = _check_json(request, "bailian")
    if bad:
        return bad
    body = await request.json()
    model = str(body.get("model") or "")
    if BAILIAN_MODELS.get(model) != "image" or not model.startswith("qwen-image"):
        return JSONResponse(
            status_code=400,
            content={"code": "InvalidParameter", "message": f"Unsupported model: {model}", "request_id": "mock"},
        )
    params = body.get("parameters") or {}
    messages = (body.get("input") or {}).get("messages") or []
    content = (messages[0] or {}).get("content") if messages else []
    has_ref = any(isinstance(c, dict) and c.get("image") for c in (content or []))
    count = max(1, min(int(params.get("n") or 1), 4))
    _log({
        "provider": "bailian", "event": "qwen_image", "model": model,
        "size": params.get("size"), "n": count, "has_ref": has_ref,
        "auth_ok": _bearer(request) in (BAILIAN_KEY, "mock-any"),
    })
    out = []
    for _ in range(count):
        png = _render_png(model, "", str(params.get("size") or ""))
        name = f"mock-{uuid.uuid4().hex[:12]}.png"
        (MEDIA_DIR / name).write_bytes(png)
        out.append({"image": _file_url(MEDIA_DIR / name)})
    return {
        "output": {
            "choices": [
                {"finish_reason": "stop", "message": {"role": "assistant", "content": out}}
            ]
        },
        "usage": {"image_count": count},
        "request_id": f"mock-{uuid.uuid4().hex[:12]}",
    }


# --------------------------------------------------------------------------
# 辅助端点：给测试脚本读日志 / 清空任务
# --------------------------------------------------------------------------


@app.get("/__requests")
async def get_requests():
    if not LOG_PATH.is_file():
        return {"items": []}
    lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    return {"items": [json.loads(x) for x in lines if x.strip()]}


@app.post("/__reset")
async def reset():
    _TASKS.clear()
    if LOG_PATH.is_file():
        LOG_PATH.unlink()
    return {"ok": True}


def main() -> None:
    global MEDIA_DIR, LOG_PATH, PUBLIC_BASE
    parser = argparse.ArgumentParser(description="本地模拟模型服务")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--log", default=str(Path.cwd() / "mock_requests.jsonl"))
    parser.add_argument("--media-dir", default=str(Path(tempfile.gettempdir()) / "xiaoma-mock-media"))
    args = parser.parse_args()

    MEDIA_DIR = Path(args.media_dir)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH = Path(args.log)
    PUBLIC_BASE = f"http://{args.host}:{args.port}"

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
