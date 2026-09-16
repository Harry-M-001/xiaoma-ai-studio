"""日志与诊断导出。

这一层解决四个具体问题：

1. **日志本来会丢**。此前只 `logging.basicConfig` 输出到 stderr，控制台一关就没了，
   用户来报问题时手上什么都没有。现在同时写一份滚动文件到 `data/logs/app.log`。
2. **日志不该带隐私**。写进文件前统一过一遍 `redact`，所以「日志文件」本身就是
   可以外发的安全产物——导出功能不需要再临时想办法。
   代价是文件里看不到完整 URL 的 query，换来的是导出永远不泄漏；控制台仍保留完整内容。
3. **上游正文不该进日志**。错误消息里为了给用户看清楚，会带上游返回的错误正文，
   而上游收到 400 时经常回显请求体（里面是用户的提示词）。所以写文件时优先使用
   `AdapterError.log_detail`——那是枚举化的安全摘要。
4. **用户报问题时需要一份能直接发的东西**。`export_report()` 把环境体检 + 最近错误
   拼成一段可整段复制的文本。

另外提供任务级日志采集：执行任务期间产生的日志会同时留一份在内存缓冲区，
任务中心可以展示「这个任务到底发生了什么」。
"""

from __future__ import annotations

import datetime
import importlib.util
import logging
import threading
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator

from app.config import settings
from app.services import redact

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[3]

LOG_FILE_NAME = "app.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5

# 任务级日志：条数与同时保留的任务数都做上限，避免长跑之后内存里越攒越多
MAX_TASK_LINES = 1000
MAX_TASKS = 50

_configured = False
_current_task: ContextVar[str | None] = ContextVar("xiaoma_current_task", default=None)
_task_buffers: "OrderedDict[str, deque[str]]" = OrderedDict()
_lock = threading.Lock()

_FILE_FORMAT = "%(asctime)s %(levelname)-5s %(name)s [%(task)s] %(shortpath)s:%(lineno)d — %(message)s"
_CONSOLE_FORMAT = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"


# ============================================================
# 格式化辅助
# ============================================================


def _short_path(pathname: str) -> str:
    """把绝对路径压成相对 backend 的短路径。

    Windows 的映射盘（subst）、大小写、正反斜杠都要兜住：这里一旦抛异常，
    Formatter 会把整条日志吞掉，那比路径难看严重得多。
    """
    try:
        p = Path(pathname)
        try:
            return p.relative_to(BACKEND_ROOT).as_posix()
        except ValueError:
            try:
                return p.relative_to(REPO_ROOT).as_posix()
            except ValueError:
                return p.name or str(pathname)
    except Exception:  # noqa: BLE001
        return str(pathname)


def _exception_text(record: logging.LogRecord) -> str:
    """异常文本。

    带 `log_detail` 的异常（也就是 `AdapterError` 那一类「外部输入引起的错误」）
    只用安全摘要，不打印完整堆栈——堆栈里的异常消息就是上游返回的正文，
    而上游在 400 时经常把请求体回显回来，里面是用户的提示词。

    没给 `log_detail` 时**也不退回去打堆栈**，而是留一个显式标记：
    宁可少记一行，也不能因为「这段代码还没改造到」就把用户内容写进日志。
    这个标记同时也是提示——看到它就该去那里补 log_detail。
    """
    if not record.exc_info:
        return ""
    exc = record.exc_info[1]
    if hasattr(exc, "log_detail"):
        detail = str(getattr(exc, "log_detail", "") or "")
        if detail:
            return f"{type(exc).__name__}: {detail}"
        return f"{type(exc).__name__}: （未提供安全摘要，已省略消息）"

    # 其它异常是本程序自己的 bug，堆栈要留着
    import traceback

    return "".join(traceback.format_exception(*record.exc_info)).strip()


class _ContextFilter(logging.Filter):
    """给每条记录补上任务 id 与短路径。只新增字段，不改动原有字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.task = _current_task.get() or "-"
        record.shortpath = _short_path(record.pathname)
        return True


def _safe_record(record: logging.LogRecord) -> logging.LogRecord:
    """复制一条记录并把正文换成脱敏后的版本。

    **必须先插值再脱敏**。直接洗格式串会把 `%s` 这类占位符一起洗掉：
    实测 `"%s v%s 启动完成：http://%s:%s"` 被 URL 规则吃成 `http://%s`，
    于是插值时报「参数比占位符多」的 TypeError，整条日志丢失——而且
    logging 只会往 stderr 打一行 "Logging error"，非常容易被忽略。
    """
    safe = logging.makeLogRecord(dict(record.__dict__))
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001
        message = str(record.msg)
    safe.msg = redact.redact_line(message, root=REPO_ROOT)
    # args 置空才不会在 format 时被二次插值
    safe.args = ()
    if record.exc_info:
        safe.exc_info = None
        safe.exc_text = redact.redact_line(_exception_text(record), root=REPO_ROOT)
    return safe


class _RedactingFileHandler(RotatingFileHandler):
    """写文件前脱敏。

    复制一份 LogRecord 再改，而不是就地修改——就地在 Filter 里改会连带影响
    控制台 handler（它们共享同一条记录），控制台就看不到完整信息了。
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # delay=True 时 stream 还是 None，必须自己补上这一句；
            # 漏掉它会静默报错（日志一条都写不出去），因为 handleError 默认不吭声。
            if self.stream is None:
                self.stream = self._open()

            safe = _safe_record(record)
            if self.shouldRollover(safe):
                self.doRollover()
            self.stream.write(self.format(safe) + self.terminator)
            self.flush()
        except Exception:  # noqa: BLE001
            # 日志写不进去不能把业务打挂
            self.handleError(record)


class _TaskLogHandler(logging.Handler):
    """把任务执行期间的日志额外留一份到内存，供任务中心展示。

    同样先脱敏：用户在任务中心看到的内容，很可能被他自己复制出来发给作者，
    所以「凡是能看日志的入口，看到的都是脱敏后的版本」这条要一致。
    """

    def emit(self, record: logging.LogRecord) -> None:
        task_id = _current_task.get()
        if not task_id:
            return
        try:
            with _lock:
                buf = _task_buffers.get(task_id)
                if buf is None:
                    return
                buf.append(self.format(_safe_record(record)))
        except Exception:  # noqa: BLE001
            pass


# ============================================================
# 初始化
# ============================================================


def log_dir() -> Path:
    p = settings.data_dir / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_file() -> Path:
    return log_dir() / LOG_FILE_NAME


def setup_logging(*, level: int = logging.INFO, force: bool = False) -> None:
    """装配控制台 + 文件两个 handler。

    默认只生效一次；`force=True` 会重新装配 —— 留这个口子是给启动流程用的：
    迁移阶段（alembic）历史上会用自己的 fileConfig 把 handler 全换掉，
    跑完迁移再强制装一次，能保证「之后一定是我们这套」。
    """
    global _configured
    if _configured and not force:
        return

    root = logging.getLogger()
    root.setLevel(level)

    ctx = _ContextFilter()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    console.addFilter(ctx)
    root.addHandler(console)

    try:
        handler = _RedactingFileHandler(
            log_file(),
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(logging.Formatter(_FILE_FORMAT))
        handler.addFilter(ctx)
        root.addHandler(handler)
    except Exception:  # noqa: BLE001
        # 磁盘只读之类的情况下也要能起来，只是没有文件日志
        logging.getLogger("xiaoma.logging").warning("文件日志初始化失败，仅输出到控制台", exc_info=True)

    task_handler = _TaskLogHandler()
    task_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
    task_handler.addFilter(ctx)
    root.addHandler(task_handler)

    _configured = True


# ============================================================
# 任务级日志
# ============================================================


@asynccontextmanager
async def task_log_scope(task_id: object) -> "Iterator[None]":
    """在任务执行期间把日志额外留一份下来。"""
    key = str(task_id)
    with _lock:
        _task_buffers[key] = deque(maxlen=MAX_TASK_LINES)
        _task_buffers.move_to_end(key)
        while len(_task_buffers) > MAX_TASKS:
            _task_buffers.popitem(last=False)
    token = _current_task.set(key)
    try:
        yield
    finally:
        _current_task.reset(token)


def task_logs(task_id: object) -> list[str]:
    with _lock:
        buf = _task_buffers.get(str(task_id))
        return list(buf) if buf else []


def drop_task_logs(task_id: object) -> None:
    with _lock:
        _task_buffers.pop(str(task_id), None)


# ============================================================
# 读取与导出
# ============================================================


def log_files() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    directory = log_dir()
    for f in sorted(directory.glob(f"{LOG_FILE_NAME}*")):
        try:
            st = f.stat()
        except OSError:
            continue
        out.append(
            {
                "name": f.name,
                "size": st.st_size,
                "modifiedAt": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(
                    timespec="seconds"
                ),
            }
        )
    return out


def _iter_lines(path: Path) -> Iterator[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                yield line.rstrip("\n")
    except OSError:
        return


def tail_lines(count: int, *, levels: tuple[str, ...] = ()) -> list[str]:
    """取最近若干行。文件本身就是脱敏过的，读出来可以直接外发。"""
    path = log_file()
    if not path.exists():
        return []
    # 文件上限 5MB，直接全读再切片，比逐行环形缓冲简单也不慢
    lines = list(_iter_lines(path))
    if levels:
        wanted = {f" {lv} " for lv in levels}
        lines = [ln for ln in lines if any(w in ln for w in wanted)]
    return lines[-count:]


def _env_report_text() -> str:
    """复用环境体检脚本的输出。

    该脚本刻意做到不依赖第三方包（它要在「虚拟环境还没建出来」时也能跑），
    所以这里是按文件路径加载，而不是当成普通模块 import。

    体检结果里有解释器、Node、npm 的**绝对路径**，而这类路径常常长在
    `C:\\Users\\<用户名>\\AppData\\...` 下面——所以整段再过一次脱敏。
    它走的是逐行输出，不会像日志那样被压平，多行结构得以保留。
    """
    script = BACKEND_ROOT / "tools" / "env_report.py"
    if not script.exists():
        return "（环境体检脚本缺失）"
    try:
        spec = importlib.util.spec_from_file_location("xiaoma_env_report", script)
        if not spec or not spec.loader:
            return "（环境体检脚本加载失败）"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        findings = module.collect_findings(REPO_ROOT, check_port=False)
        raw = module.build_report_text(findings, heading=False)
    except Exception as exc:  # noqa: BLE001
        return f"（环境体检执行失败：{type(exc).__name__}）"
    return redact.redact_text(raw, root=REPO_ROOT)


def export_report(*, error_lines: int = 200) -> dict[str, Any]:
    """生成一份可以整段复制发出去的诊断报告。"""
    setup_logging()
    generated = datetime.datetime.now().isoformat(timespec="seconds")

    errors = tail_lines(error_lines, levels=("ERROR", "WARNING"))

    parts: list[str] = []
    parts.append("小马AI工坊 · 诊断报告")
    parts.append(f"生成时间：{generated}")
    parts.append("版本：" + _version())
    parts.append("")
    parts.append("说明：本报告用于排查问题，只包含环境信息与错误的文字摘要。")
    parts.append("      日志中的密钥、URL 查询参数、系统用户名与家目录路径已做脱敏；")
    parts.append("      不包含提示词、作品正文、图片、数据库内容或任何接口密钥。")
    parts.append("")
    parts.append("=" * 56)
    parts.append("一、环境体检")
    parts.append("=" * 56)
    parts.append(_env_report_text().rstrip())
    parts.append("")
    parts.append("=" * 56)
    parts.append(f"二、最近的错误与警告（最多 {error_lines} 条，已脱敏）")
    parts.append("=" * 56)
    if errors:
        parts.extend(errors)
    else:
        parts.append("（日志里还没有 ERROR / WARNING）")
    parts.append("")

    files = log_files()
    total = sum(int(f["size"]) for f in files)
    parts.append("=" * 56)
    parts.append("三、日志文件")
    parts.append("=" * 56)
    if files:
        for f in files:
            parts.append(f"{f['name']}　{int(f['size']) / 1024:.1f} KB　{f['modifiedAt']}")
    else:
        parts.append("（暂无日志文件）")
    parts.append(f"合计 {total / 1024:.1f} KB，保留最近 {len(files)} 个文件（每个上限 5 MB）。")

    text = "\n".join(parts)
    return {
        "text": text,
        "generatedAt": generated,
        "errorLines": len(errors),
        "files": files,
        "bytes": len(text.encode("utf-8")),
    }


def _version() -> str:
    try:
        from app import __version__

        return __version__
    except Exception:  # noqa: BLE001
        return "unknown"
