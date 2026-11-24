"""远程视频落盘：边下边写、成功后原子替换、失败不留半成品。"""

from __future__ import annotations

import asyncio
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.providers.base import AdapterError  # noqa: E402
from app.services.runner import _download_video  # noqa: E402

PAYLOAD = b"x" * (3 * 1024 * 1024 + 7)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server 的约定
        if self.path == "/missing":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        # 分块写：客户端要是先整段缓冲也能过，但这条用例的意思是「边收边落盘」
        for i in range(0, len(PAYLOAD), 512 * 1024):
            self.wfile.write(PAYLOAD[i : i + 512 * 1024])

    def log_message(self, *args) -> None:  # noqa: ANN002 - 静音测试输出
        return


def _serve() -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_download_streams_to_a_final_file_without_leftovers():
    srv = _serve()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/clip.mp4"
        rel, size = asyncio.run(_download_video(url))
    finally:
        srv.shutdown()
        srv.server_close()

    assert size == len(PAYLOAD)
    assert (settings.storage_dir / rel).read_bytes() == PAYLOAD
    assert list(settings.storage_dir.rglob("*.part")) == [], "落盘后还留着临时文件"


def test_failed_download_leaves_no_partial_file():
    srv = _serve()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/missing"
        try:
            asyncio.run(_download_video(url))
        except AdapterError as e:
            assert "HTTP 404" in str(e)
        else:
            raise AssertionError("404 应当让下载失败")
    finally:
        srv.shutdown()
        srv.server_close()

    left = [p for p in settings.storage_dir.rglob("*") if p.is_file()]
    assert left == [], f"下载失败留下了文件：{left}"
