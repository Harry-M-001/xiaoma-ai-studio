"""上游错误文案的测试（直接 python 运行）。

运行：venv/Scripts/python tests/test_provider_errors.py

为什么单独立一条：这些文案是用户唯一能看到的东西。上游给的是 `HTTP 401`
和一段英文 JSON，用户需要的是「我该去改哪里」——而这层映射一旦写漏，
表现只是「错误提示看了也不知道怎么办」，不会有任何测试或日志报错。
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.providers.base import network_error_message, summarize_upstream_error  # noqa: E402


def _resp(status: int, body: dict | str) -> httpx.Response:
    if isinstance(body, str):
        return httpx.Response(status, text=body)
    return httpx.Response(status, json=body)


def test_each_status_gets_an_actionable_hint():
    cases = {
        400: "模型名",
        401: "Key",
        403: "权限",
        404: "/v1",
        413: "过大",
        429: "限流",
        502: "上游",
        503: "上游",
    }
    for status, needle in cases.items():
        message, _ = summarize_upstream_error(_resp(status, {"error": {"message": "boom"}}))
        assert needle in message, f"HTTP {status} 的提示里看不出该做什么：{message}"
        assert f"HTTP {status}" in message


def test_unknown_5xx_still_says_retry():
    message, _ = summarize_upstream_error(_resp(599, {"error": "boom"}))
    assert "稍后重试" in message


def test_log_detail_never_contains_upstream_body():
    """日志摘要里不能出现上游正文（400 时上游常把请求体回显回来，那里是用户提示词）。"""
    message, detail = summarize_upstream_error(
        _resp(400, {"error": {"type": "invalid_request_error", "code": "x", "message": "提示词原文在这里"}})
    )
    assert "提示词原文在这里" in message  # 给用户看的那份保留
    assert "提示词原文在这里" not in detail  # 日志那份不能有
    assert detail.startswith("HTTP 400")
    assert "type=invalid_request_error" in detail
    assert "body=" in detail


def test_empty_body_does_not_leave_a_dangling_colon():
    """上游返回空体时不能留下 `HTTP 502 ：` 这种像 bug 的尾巴。"""
    message, _ = summarize_upstream_error(_resp(502, ""))
    assert "：\n" not in message
    assert not message.endswith("：")
    assert "响应体为空" in message


def test_connect_error_tells_where_to_look():
    message = network_error_message(httpx.ConnectError("connection refused"))
    assert "连不上" in message
    assert "端口" in message  # 本机服务的排查动作
    assert "ConnectError" in message  # 保留类型名，方便搜索


def test_connect_timeout_is_reported_as_timeout_not_refusal():
    """ConnectTimeout 同时属于「超时」和「连接类」错误，顺序判错会把建议说反。"""
    message = network_error_message(httpx.ConnectTimeout("timed out"))
    assert "超时" in message
    assert "连不上" not in message


def test_read_timeout_gets_retry_advice():
    message = network_error_message(httpx.ReadTimeout("read timed out"))
    assert "超时" in message
    assert "稍后重试" in message


def test_proxy_error_is_named():
    message = network_error_message(httpx.ProxyError("proxy failed"))
    assert "代理" in message


def test_unknown_exception_still_renders():
    class Weird(Exception):
        pass

    message = network_error_message(Weird("x"))
    assert "Weird" in message


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
