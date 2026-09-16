"""脱敏：把可能带隐私的文本洗成可以安全外发的样子。

三条原则，顺序不能反。

**一、能结构化就不要带正文。** 上游返回的错误体里经常把请求体回显回来，而请求体
里就是用户的提示词。所以 provider 层会把「错误类型 / HTTP 状态 / 主机名」这些枚举
信息单独带出来（见 `providers/base.py` 的 `AdapterError.log_detail`），日志只记这些，
正文根本不进日志。本模块只是第二道防线。

**二、本模块负责洗掉异常字符串里混进来的 URL、密钥、路径、邮箱、用户名。**
这些是「代码自己拼出来的字符串」，结构化解决不了。

**三、规则要同时在导出侧与接收侧跑。** 客户端跑在用户自己的机器上，随时可能被改，
所以服务端收到之后必须再用同一套规则跑一遍。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

MASK_SECRET = "«secret»"
MASK_EMAIL = "«email»"
MASK_USER = "%USER%"
MASK_REPO = "<repo>"
MASK_ABS = "…"

# URL 只留 scheme + host。中转站常把 key 放在 query 上，用户也可能填了内网网关地址。
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]，。；、]+", re.I)
# OpenAI 风格的 key
_SECRET_PREFIX_RE = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")
# 「Bearer xxx」「api_key=xxx」这类带名字的凭据
_NAMED_SECRET_RE = re.compile(
    r"(?i)\b(bearer|api[_-]?key|apikey|access[_-]?token|secret[_-]?key|password)\b"
    r"\s*[:=]?\s*[A-Za-z0-9._\-]{6,}"
)
# 没有名字的长随机串（32 位以上）。会连 git sha、md5 一起吃掉，那些本来也没必要外发。
_LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{32,}(?![A-Za-z0-9_\-])")
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+")
# 三种系统的家目录
_WIN_HOME_RE = re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"'<>|]+", re.I)
_POSIX_HOME_RE = re.compile(r"/(?:home|Users|root)/[^/\s\"'<>]+")
# 其余的 Windows 绝对路径（家目录与仓库根都替换完之后才轮到它）
_WIN_ABS_RE = re.compile(r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]+\\?)+")
_POSIX_ABS_RE = re.compile(r"/(?:tmp|var|opt|srv|mnt)/[^\s\"'<>]+")
_WS_RE = re.compile(r"\s+")

_MAX_LEN = 4000


def host_of(url: str) -> str:
    """只取 host，丢掉 path / query / 端口 / 用户信息。"""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return ""
    return parts.hostname or ""


def _mask_url(match: re.Match[str]) -> str:
    """URL 保留 scheme + host + port + path，丢掉 query、fragment 与用户信息。

    为什么要留 path 与 port：排查 401 / 404 时「打的是哪个端点」是最关键的信息，
    自建网关又常常跑在非默认端口上，整条砍掉会让日志失去价值。
    真正会漏 key 的是 query（中转站常见 `?key=xxx`）与用户信息，这两段必须去掉；
    把 key 藏在 path 里的写法由下面的密钥规则兜住。
    """
    raw = match.group(0)
    try:
        parts = urlsplit(raw)
    except ValueError:
        return MASK_ABS
    if not parts.hostname:
        return MASK_ABS
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}{parts.path or ''}"


def _mask_windows_abs(match: re.Match[str]) -> str:
    """绝对路径只留最后两段，既去掉了用户名与目录结构，又还能看出是哪个文件。"""
    raw = match.group(0)
    parts = [p for p in raw.replace("/", "\\").split("\\") if p]
    if len(parts) <= 2:
        return raw
    return f"{MASK_ABS}\\" + "\\".join(parts[-2:])


def _mask_posix_abs(match: re.Match[str]) -> str:
    parts = [p for p in match.group(0).split("/") if p]
    if len(parts) <= 2:
        return match.group(0)
    return f"{MASK_ABS}/" + "/".join(parts[-2:])


def _username() -> str:
    for key in ("USERNAME", "USER", "LOGNAME"):
        name = (os.environ.get(key) or "").strip()
        # 名字太短或就是通用词时不做全局替换，否则会误伤正常文本
        if len(name) >= 3 and name.lower() not in ("user", "admin", "root", "runner"):
            return name
    return ""


def redact(text: str, *, root: Path | str | None = None, limit: int = 0) -> tuple[str, int]:
    """按顺序清洗文本，返回 (清洗结果, 替换次数)。

    `root` 传仓库根目录时，本仓库的绝对路径会被压成 `<repo>`；
    `limit` 大于 0 时结果会被截断到该长度。
    """
    raw = str(text or "")
    if not raw:
        return "", 0

    total = 0
    out = raw

    # 1) URL 先于密钥：否则带 key 的 URL 会被当成普通长串，结果反而把 host 也吃掉
    out, n = _URL_RE.subn(_mask_url, out)
    total += n
    # 2) 有名字的凭据
    out, n = _NAMED_SECRET_RE.subn(lambda m: f"{m.group(1)}={MASK_SECRET}", out)
    total += n
    # 3) sk- 前缀
    out, n = _SECRET_PREFIX_RE.subn(MASK_SECRET, out)
    total += n
    # 4) 没名字的长随机串
    out, n = _LONG_TOKEN_RE.subn(MASK_SECRET, out)
    total += n
    # 5) 邮箱
    out, n = _EMAIL_RE.subn(MASK_EMAIL, out)
    total += n
    # 6) 家目录（含用户名）
    out, n = _WIN_HOME_RE.subn(MASK_USER, out)
    total += n
    out, n = _POSIX_HOME_RE.subn(MASK_USER, out)
    total += n
    # 7) 仓库根目录（路径里的目录名可能正是用户的创作项目名）
    if root:
        repo = str(root).rstrip("\\/")
        if repo:
            for variant in {repo, repo.replace("\\", "/"), repo.replace("/", "\\")}:
                pattern = re.compile(re.escape(variant), re.I)
                out, n = pattern.subn(MASK_REPO, out)
                total += n
    # 8) 用户名单独出现的情况（兜底）
    name = _username()
    if name:
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", re.I)
        out, n = pattern.subn(MASK_USER, out)
        total += n
    # 9) 剩下的绝对路径
    out, n = _WIN_ABS_RE.subn(_mask_windows_abs, out)
    total += n
    out, n = _POSIX_ABS_RE.subn(_mask_posix_abs, out)
    total += n

    if limit and len(out) > limit:
        out = out[: max(1, limit - 1)] + "…"
    return out, total


def redact_text(text: str, *, root: Path | str | None = None, limit: int = 0) -> str:
    """只要结果、不要计数的便捷写法。"""
    return redact(text, root=root, limit=limit)[0]


def flatten(text: str) -> str:
    """换行与连续空白压平。

    防的是「用换行拆开前面几条规则」——比如把 key 折成两行就躲过了正则。
    日志导出与分析都是按行处理的，压平也省得后续解析出错。
    """
    return _WS_RE.sub(" ", str(text or "")).strip()


def redact_line(text: str, *, root: Path | str | None = None, limit: int = 0) -> str:
    """压平 + 脱敏，日志行专用。"""
    flat = flatten(text)
    if len(flat) > _MAX_LEN:
        flat = flat[:_MAX_LEN] + "…"
    return redact_text(flat, root=root, limit=limit)
