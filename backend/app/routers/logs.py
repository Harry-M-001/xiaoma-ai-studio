"""日志与诊断导出 API。

导出的文本会被用户手动发给作者，所以这里只做「拼装 + 暴露」：
脱敏在写日志时就完成了（`services/log_service.py`），日志文件本身就是安全产物，
不存在「导出时忘了脱敏」这条失败路径。
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse

from app.deps import require_auth
from app.services import log_service

router = APIRouter(prefix="/api/logs", tags=["logs"], dependencies=[Depends(require_auth)])


@router.get("/status")
async def logs_status() -> dict:
    """日志目录与文件清单，用于在设置页展示「日志占了多少、保留几份」。"""
    files = log_service.log_files()
    return {
        "dir": str(log_service.log_dir()),
        "files": files,
        "totalBytes": sum(int(f["size"]) for f in files),
        "maxBytesPerFile": log_service.MAX_BYTES,
        "backupCount": log_service.BACKUP_COUNT,
    }


@router.get("/export")
async def export_logs(errorLines: int = Query(200, ge=10, le=2000)) -> dict:
    """生成诊断报告文本（给界面预览用）。"""
    return log_service.export_report(error_lines=errorLines)


@router.get("/export.txt")
async def export_logs_file(errorLines: int = Query(200, ge=10, le=2000)) -> PlainTextResponse:
    """同一份内容，作为附件下载。"""
    data = log_service.export_report(error_lines=errorLines)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return PlainTextResponse(
        data["text"],
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="xiaoma-diagnostic-{stamp}.txt"'},
    )
