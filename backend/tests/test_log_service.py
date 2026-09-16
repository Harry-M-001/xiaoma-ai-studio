"""日志落盘 / 脱敏 / 任务级捕获 / 诊断导出的回归测试（直接 python 运行）。

运行：venv/Scripts/python tests/test_log_service.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.services import log_service  # noqa: E402


class _Recorder(logging.Handler):
    """记下原始 LogRecord，用来证明脱敏没有就地改记录（否则控制台也被洗了）。"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _sandbox:
    """把数据目录临时指到临时目录，并重置日志装配状态。"""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir = settings.DATA_DIR
        self.root = logging.getLogger()
        self.saved_handlers = list(self.root.handlers)
        self.saved_level = self.root.level

    def __enter__(self) -> Path:
        settings.DATA_DIR = self.tmp.name
        log_service._configured = False
        for h in list(self.root.handlers):
            self.root.removeHandler(h)
        return Path(self.tmp.name)

    def __exit__(self, *exc: object) -> bool:
        log_service.setup_logging()
        self.root.handlers.clear()
        for h in self.saved_handlers:
            self.root.addHandler(h)
        self.root.setLevel(self.saved_level)
        settings.DATA_DIR = self.old_data_dir
        log_service._configured = False
        log_service._task_buffers.clear()
        self.tmp.cleanup()
        return False


def _flush() -> None:
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:  # noqa: BLE001
            pass


def test_file_log_is_actually_written():
    """回归背景：文件 handler 用了 delay=True，但重写的 emit 漏了补 stream，
    结果一条日志都写不出去，而 handleError 默认不吭声——静默失效，最难查。
    """
    with _sandbox() as tmp:
        log_service.setup_logging()
        logging.getLogger("xiaoma.test").info("这条必须落盘")
        _flush()
        path = tmp / "logs" / "app.log"
        assert path.exists(), "日志文件没被创建"
        text = path.read_text(encoding="utf-8")
        assert "这条必须落盘" in text
        assert "xiaoma.test" in text


def test_file_log_is_redacted_console_is_not():
    with _sandbox() as tmp:
        log_service.setup_logging()
        recorder = _Recorder()
        logging.getLogger().addHandler(recorder)

        secret = "sk-abcdef1234567890abcdef"
        logging.getLogger("xiaoma.test").error(
            "上游失败 url=%s key=%s path=%s",
            "https://api.example.com/v1/chat?key=" + secret,
            secret,
            r"C:\Users\Lenovo\Documents\我的小说\分镜.md",
        )
        _flush()

        text = (tmp / "logs" / "app.log").read_text(encoding="utf-8")
        assert secret not in text, "密钥进了文件日志"
        # 注意断言的是「?key=」这个查询参数形态，不是「key=」——
        # 后者是日志格式串里的普通标签，本来就应该留着。
        assert "?key=" not in text, "URL 查询参数进了文件日志"
        assert "Lenovo" not in text, "用户名进了文件日志"
        # path 本身要留着，否则日志没法用
        assert "https://api.example.com/v1/chat" in text

        # 但原始记录不能被改：控制台/其它 handler 仍要看到完整信息
        assert recorder.records, "recorder 没拿到记录"
        raw = str(recorder.records[-1].msg) % recorder.records[-1].args
        assert secret in raw, "脱敏错误地影响了原始记录"


def test_setup_logging_is_idempotent():
    with _sandbox() as tmp:
        log_service.setup_logging()
        before = len(logging.getLogger().handlers)
        log_service.setup_logging()
        log_service.setup_logging()
        assert len(logging.getLogger().handlers) == before
        assert tmp.exists()


def test_task_scope_captures_only_inside():
    with _sandbox() as tmp:
        log_service.setup_logging()

        async def run() -> list[str]:
            async with log_service.task_log_scope(7):
                logging.getLogger("xiaoma.task").warning("任务内部的一条")
            logging.getLogger("xiaoma.task").warning("任务外面的一条")
            return log_service.task_logs(7)

        lines = asyncio.run(run())
        assert any("任务内部的一条" in ln for ln in lines), lines
        assert all("任务外面的一条" not in ln for ln in lines), lines
        assert tmp.exists()


def test_task_scope_isolates_ids():
    with _sandbox():
        log_service.setup_logging()

        async def run() -> None:
            async with log_service.task_log_scope("a"):
                logging.getLogger("t").warning("给 a 的")
            async with log_service.task_log_scope("b"):
                logging.getLogger("t").warning("给 b 的")

        asyncio.run(run())
        assert any("给 a 的" in ln for ln in log_service.task_logs("a"))
        assert all("给 b 的" not in ln for ln in log_service.task_logs("a"))
        assert any("给 b 的" in ln for ln in log_service.task_logs("b"))


def test_task_buffer_drops_oldest_lines():
    """长跑之后内存不能越攒越多：超过上限丢最旧的，且留下最近的。"""
    with _sandbox():
        log_service.setup_logging()
        original = log_service.MAX_TASK_LINES
        log_service.MAX_TASK_LINES = 3
        try:

            async def run() -> None:
                async with log_service.task_log_scope(9):
                    for i in range(6):
                        logging.getLogger("t").warning("第 %s 条", i)

            asyncio.run(run())
        finally:
            log_service.MAX_TASK_LINES = original

        lines = log_service.task_logs(9)
        assert len(lines) == 3, lines
        assert any("第 5 条" in ln for ln in lines), lines
        assert all("第 0 条" not in ln for ln in lines), lines


def test_task_buffers_have_caps():
    assert log_service.MAX_TASKS >= 1
    assert log_service.MAX_TASK_LINES >= 100


def test_export_report_shape_and_safety():
    with _sandbox():
        log_service.setup_logging()
        logging.getLogger("xiaoma.test").error("导出用的一条错误 url=%s", "https://h.example.com/x?k=sk-secretsecretsecret")
        _flush()
        data = log_service.export_report(error_lines=50)

        text = data["text"]
        assert "小马AI工坊 · 诊断报告" in text
        assert "一、环境体检" in text
        assert "二、最近的错误与警告" in text
        assert "三、日志文件" in text
        assert "导出用的一条错误" in text
        assert "sk-secretsecretsecret" not in text
        assert data["bytes"] > 0
        assert data["errorLines"] >= 1
        assert data["files"], "报告里要列出日志文件"


def test_export_report_without_log_file_still_works():
    """全新装、一条日志都没有时不能报错。"""
    with _sandbox():
        log_service.setup_logging()
        data = log_service.export_report()
        assert "小马AI工坊 · 诊断报告" in data["text"]
        assert data["errorLines"] == 0


def test_redaction_does_not_break_format_placeholders():
    """回归背景：先洗格式串再插值，会把 `http://%s:%s` 洗成 `http://%s`，
    插值时参数比占位符多，直接抛 TypeError —— 整条日志丢失，而且 logging
    只在 stderr 打一行 "Logging error"，几乎不会被注意到。
    """
    with _sandbox() as tmp:
        log_service.setup_logging()
        logging.getLogger("xiaoma.test").info(
            "%s v%s 启动完成：http://%s:%s", "小马AI工坊", "1.1.2", "127.0.0.1", 8787
        )
        _flush()
        text = (tmp / "logs" / "app.log").read_text(encoding="utf-8")
        assert "启动完成" in text, "格式串被脱敏破坏，日志整条丢了"
        assert "8787" in text
        assert "1.1.2" in text


def test_percent_sign_in_message_survives():
    """消息里带字面量 % 时也不能炸（args 置空后 logging 不会再做插值）。"""
    with _sandbox() as tmp:
        log_service.setup_logging()
        logging.getLogger("xiaoma.test").warning("路径已替换为 %%USER%% 占位符")
        _flush()
        text = (tmp / "logs" / "app.log").read_text(encoding="utf-8")
        assert "占位符" in text


def test_task_logs_are_redacted_too():
    """任务中心里能看到的内容，用户很可能直接复制出来发出去，同样要脱敏。"""
    with _sandbox():
        log_service.setup_logging()
        secret = "sk-abcdef1234567890abcdef"

        async def run() -> list[str]:
            async with log_service.task_log_scope(11):
                logging.getLogger("t").error("失败 url=https://h.example.com/x?k=%s", secret)
            return log_service.task_logs(11)

        lines = asyncio.run(run())
        joined = "\n".join(lines)
        assert lines, "任务日志没被捕获"
        assert secret not in joined
        assert "?k=" not in joined
        assert "https://h.example.com/x" in joined


def test_export_report_redacts_system_username():
    """回归背景：体检结果里带着解释器 / Node / npm 的绝对路径，而这类路径常常长在
    `C:\\Users\\<用户名>\\AppData\\...` 下面。这段文本是拼进去的，不走日志 handler，
    所以必须单独过一次脱敏——第一版就是这么漏的。
    """
    with _sandbox():
        log_service.setup_logging()
        data = log_service.export_report()
        text = data["text"]
        user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        if len(user) >= 3 and user.lower() not in ("user", "admin", "runner"):
            assert user not in text, f"报告里出现了系统用户名 {user}"
        assert "环境体检" in text


def test_log_files_lists_rotations():
    with _sandbox() as tmp:
        log_service.setup_logging()
        logging.getLogger("t").info("x")
        _flush()
        (tmp / "logs" / "app.log.1").write_text("old\n", encoding="utf-8")
        names = [f["name"] for f in log_service.log_files()]
        assert "app.log" in names
        assert "app.log.1" in names


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
