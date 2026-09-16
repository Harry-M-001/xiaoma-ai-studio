"""脱敏与安全日志的回归测试（直接 python 运行）。

运行：venv/Scripts/python tests/test_redact.py

这个文件是整条「日志可以外发」承诺的守门人。它检查的都不是「看起来对不对」，
而是「某段用户内容有没有可能出现在日志里」。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.providers.base import AdapterError, summarize_upstream_error  # noqa: E402
from app.providers.comfyui import _describe_submit_error  # noqa: E402
from app.services import redact  # noqa: E402
from app.services.log_service import _exception_text  # noqa: E402

REPO = r"D:\ai-canvas-github\xiaoma-ai-studio"


def _red(text: str, limit: int = 0) -> str:
    return redact.redact_text(text, root=REPO, limit=limit)


# ---------- URL ----------


def test_url_query_is_dropped_path_is_kept():
    """query 里最常见的是 key，必须去掉；path 是排查 401/404 的关键，要留下。"""
    out = _red("请求 https://api.example.com/v1/chat/completions?key=sk-abcdef123456 失败")
    assert "sk-abcdef123456" not in out
    assert "key=" not in out
    assert "https://api.example.com/v1/chat/completions" in out


def test_url_userinfo_is_dropped():
    out = _red("https://user:pass@relay.example.com/v1/models")
    assert "user:pass" not in out
    assert "relay.example.com" in out


def test_url_fragment_dropped():
    out = _red("见 https://example.com/docs#token=abcdef123456")
    assert "abcdef123456" not in out


# ---------- 密钥 ----------


def test_sk_key_masked():
    out = _red("Authorization 用了 sk-proj-ABCdef0123456789 这个")
    assert "sk-proj-ABCdef0123456789" not in out
    assert redact.MASK_SECRET in out


def test_bearer_masked():
    out = _red("headers={'Authorization': 'Bearer eyJhbGciOiJIUzI1NiJ9'}")
    assert "eyJhbGciOiJIUzI1NiJ9" not in out


def test_named_secret_masked():
    out = _red("api_key=9f8e7d6c5b4a3f2e1d0c")
    assert "9f8e7d6c5b4a3f2e1d0c" not in out


def test_long_opaque_token_masked():
    token = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    out = _red(f"token {token} 无效")
    assert token not in out


# ---------- 路径 ----------


def test_windows_home_masked():
    out = _red(r"FileNotFoundError: [Errno 2] C:\Users\Lenovo\Documents\我的小说\分镜.md")
    assert "Lenovo" not in out
    assert redact.MASK_USER in out


def test_posix_home_masked():
    out = _red("Permission denied: /home/alice/projects/script.md")
    assert "alice" not in out
    assert redact.MASK_USER in out


def test_macos_home_masked():
    out = _red("open /Users/bob/work/story.md failed")
    assert "bob" not in out


def test_repo_root_masked():
    out = _red(r"Traceback: D:\ai-canvas-github\xiaoma-ai-studio\backend\app\main.py")
    assert "xiaoma-ai-studio" not in out.split("backend")[0] or redact.MASK_REPO in out
    assert redact.MASK_REPO in out


def test_other_absolute_path_shrunk_to_tail():
    """非家目录、非仓库的绝对路径只留最后两段。

    用户其它位置的绝对路径同样带着目录结构（可能就是他作品的组织方式），
    但完全抹掉会让日志没法用，所以折中成「最后两段」。
    """
    out = _red(r"写入失败：E:\私人\创作素材\第三章\分镜表.csv")
    assert "私人" not in out
    assert "分镜表.csv" in out


# ---------- 其它 ----------


def test_email_masked():
    out = _red("账号 zhangsan@example.com 校验失败")
    assert "zhangsan@example.com" not in out
    assert redact.MASK_EMAIL in out


def test_newlines_are_flattened():
    """换行必须压平，否则「把 key 折成两行」就能绕过上面所有规则。"""
    out = _red(redact.flatten("sk-abc\ndef123456\n尝试失败"))
    assert "\n" not in out
    assert out.count(" ") >= 1


def test_limit_truncates():
    out = _red("啊" * 500, limit=100)
    assert len(out) <= 100


def test_count_is_reported():
    text, n = redact.redact("a@b.com 与 c@d.com")
    assert n >= 2


def test_clean_text_is_left_alone():
    """不能把正常内容也洗掉——脱敏过头会让日志失去意义。"""
    src = "任务 12 状态更新为 completed，耗时 842ms"
    assert _red(src) == src


# ---------- 上游正文不得进日志 ----------


class _FakeResponse:
    def __init__(self, status: int, payload: dict | None, raw: bytes = b"") -> None:
        self.status_code = status
        self._payload = payload
        self.content = raw or b"x" * 40
        self.text = raw.decode("utf-8", "replace") if raw else ""

    def json(self):  # noqa: ANN201
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_upstream_body_goes_to_user_message_only():
    """上游回显了用户提示词：用户能看到（那是他自己的内容），日志摘要里不能有。"""
    secret_prompt = "暮光闪闪站在永恒花园的门口"
    resp = _FakeResponse(
        400,
        {"error": {"type": "invalid_request_error", "code": "bad_prompt", "message": secret_prompt}},
    )
    message, log_detail = summarize_upstream_error(resp)
    assert secret_prompt in message
    assert secret_prompt not in log_detail
    assert "invalid_request_error" in log_detail
    assert "HTTP 400" in log_detail


def test_adapter_error_traceback_is_not_logged():
    """AdapterError 的堆栈里带着上游正文，写日志时必须被换成安全摘要。"""
    exc = AdapterError("HTTP 400：暮光闪闪站在永恒花园的门口", log_detail="HTTP 400 type=bad")
    record = logging.LogRecord("t", logging.ERROR, "f.py", 1, "boom", None, (type(exc), exc, None))
    text = _exception_text(record)
    assert "暮光闪闪" not in text
    assert "HTTP 400 type=bad" in text


def test_adapter_error_without_detail_fails_safe():
    """没给 log_detail 时也要 fail-safe：宁可少记，也不能漏正文。"""
    exc = AdapterError("上游返回：一只猫坐在窗台上")
    record = logging.LogRecord("t", logging.ERROR, "f.py", 1, "boom", None, (type(exc), exc, None))
    text = _exception_text(record)
    assert "一只猫" not in text
    assert "省略" in text


def test_normal_bug_still_keeps_traceback():
    """本程序自己的 bug 要保留堆栈，否则没法查。"""
    try:
        raise KeyError("missing_field")
    except KeyError:
        exc_info = sys.exc_info()
    record = logging.LogRecord("t", logging.ERROR, "f.py", 1, "boom", None, exc_info)
    text = _exception_text(record)
    assert "KeyError" in text
    assert "missing_field" in text


def test_comfy_submit_error_detail_has_no_node_text():
    """ComfyUI 的 node_errors 里可能带着节点输入（也就是用户的提示词）。"""
    prompt = "一只戴帽子的猫"
    resp = _FakeResponse(
        400,
        {
            "error": {"type": "prompt_outputs_failed_validation", "message": "Prompt outputs failed"},
            "node_errors": {
                "6": {
                    "class_type": "CLIPTextEncode",
                    "errors": [{"type": "value_not_in_list", "message": f"bad value: {prompt}"}],
                }
            },
        },
        raw=b'{"error":{}}',
    )
    message, log_detail = _describe_submit_error(resp)
    assert prompt in message  # 用户自己要看到
    assert prompt not in log_detail
    assert "value_not_in_list" in log_detail
    assert "nodes=1" in log_detail


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
