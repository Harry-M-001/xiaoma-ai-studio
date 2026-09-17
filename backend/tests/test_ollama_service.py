"""本机 Ollama 检测的行为测试（直接 python 运行）。

运行：venv/Scripts/python tests/test_ollama_service.py

不起真的 Ollama：本机有没有装、装了什么模型都不该影响测试结果。
这里在 localhost 上起一个只回答 /api/tags 的小服务器来验证解析，
再指向一个没人监听的端口来验证「没装」这条路径。
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.providers.base import AdapterError  # noqa: E402
from app.services import ollama_service  # noqa: E402


def _detect(host: str, force: bool = False) -> dict:
    """detect 是 async 的（服务里就是这么用的），测试里逐次跑完即可。"""
    return asyncio.run(ollama_service.detect(host=host, force=force))


class _TagsHandler(BaseHTTPRequestHandler):
    payload: dict = {"models": []}

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/api/tags":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # 测试输出保持干净
        return


def _serve(payload: dict) -> tuple[HTTPServer, str]:
    handler = type("H", (_TagsHandler,), {"payload": payload})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_usable_filters_non_chat_models():
    """向量模型与重排模型不能对话，混进模型清单只会让人选了就报错。"""
    assert ollama_service._usable("qwen2.5:7b")
    assert ollama_service._usable("llama3.1:8b-instruct-q4_K_M")
    assert not ollama_service._usable("nomic-embed-text:latest")
    assert not ollama_service._usable("bge-m3-embed")
    assert not ollama_service._usable("sam860/qwen3-reranker:0.6b-Q8_0")


def test_detect_parses_tags_and_sorts():
    server, host = _serve(
        {
            "models": [
                {"name": "qwen2.5:7b"},
                {"name": "nomic-embed-text:latest"},  # 应被过滤
                {"name": "llama3.1:8b"},
                {"name": "llama3.1:8b"},  # 重复项应去重
            ]
        }
    )
    try:
        info = _detect(host, force=True)
    finally:
        server.shutdown()
    assert info["running"] is True
    assert info["models"] == ["llama3.1:8b", "qwen2.5:7b"]
    assert info["error"] == ""


def test_detect_caches_and_force_bypasses_it():
    """引导横幅每次进页面都会问一次状态，不缓存就变成高频端口探测。"""
    server, host = _serve({"models": [{"name": "a:1"}]})
    try:
        first = _detect(host, force=True)
        assert first["models"] == ["a:1"]
        # 换个 host 会重新探（缓存按 host 分开）
        assert _detect("http://127.0.0.1:1")["running"] is False
        # 回到原来的 host 仍走缓存
        assert _detect(host)["models"] == ["a:1"]
    finally:
        server.shutdown()
    # force 绕过缓存，且这一次服务器已经关了 → 必须变成「没在跑」
    info = _detect(host, force=True)
    assert info["running"] is False
    assert info["error"]


def test_detect_reports_not_running_without_raising():
    """没装 Ollama 是最常见的情况，必须安静地返回 running=False。"""
    info = _detect(f"http://127.0.0.1:{_free_port()}", force=True)
    assert info["running"] is False
    assert info["models"] == []


def test_connect_refuses_when_not_running():
    """没检测到就接入，必须给出可照做的提示，而不是落一个连不上的服务。"""
    try:
        asyncio.run(ollama_service.connect(None, host=f"http://127.0.0.1:{_free_port()}"))
    except AdapterError as e:
        assert "Ollama" in str(e)
        assert "11434" in str(e) or "启动" in str(e)
    else:
        raise AssertionError("没检测到 Ollama 时应该报错")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failed else 0)
