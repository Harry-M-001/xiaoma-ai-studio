"""视频轮询遇到网络抖动不能把「上游已经受理」的任务判死。

2.2.6 实测踩过：KIE 那边出片成功并扣了费，我们这边一次 RemoteProtocolError
就把任务标成失败，产物直接丢掉。这里把边界钉住。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.database import Base  # noqa: E402
from app.models import Task  # noqa: E402
from app.providers.base import AdapterError, VideoStatus, is_transient_network_error  # noqa: E402
from app.services import runner as runner_module  # noqa: E402


def test_network_markers_are_classified() -> None:
    transient = (
        "网络层出错，稍后重试（RemoteProtocolError）",
        "连不上这个地址：确认地址没写错（ConnectError）",
        "连接超时：地址可能不可达（ConnectTimeout）",
        "请求超时：服务没在超时时间内响应（ReadTimeout）",
    )
    for msg in transient:
        assert is_transient_network_error(msg), msg
    # 业务错误不是网络抖动：余额不足、参数错都不能靠等待解决
    for msg in ("KIE 返回错误（402）：Credits insufficient", "HTTP 400 模型名写错"):
        assert not is_transient_network_error(msg), msg


def _engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(maker) -> int:
    async with maker() as db:
        task = Task(
            kind="video",
            status="processing",
            service_id=1,
            model="1:bytedance/seedance-2",
            prompt="雨夜书店",
            params_json='{"duration": 5}',
            remote_job_id="remote-1",
        )
        db.add(task)
        await db.commit()
        return task.id


async def _status(maker, task_id: int) -> tuple[str, str]:
    async with maker() as db:
        task = await db.get(Task, task_id)
        return task.status or "", task.error or ""


def test_transient_poll_error_keeps_waiting(monkeypatch) -> None:
    engine, maker = _engine()

    class Adapter:
        def __init__(self) -> None:
            self.calls = 0

        async def poll_video(self, remote_id: str) -> VideoStatus:
            self.calls += 1
            if self.calls <= 2:
                raise AdapterError("网络层出错，稍后重试（RemoteProtocolError）")
            return VideoStatus(status="succeeded", video_url="https://example.com/a.mp4", progress=100)

        async def close(self) -> None:
            return None

    adapter = Adapter()

    class Resolved:
        pass

    async def resolve_model(db, key, modality):  # noqa: ANN001, ANN202
        resolved = Resolved()
        resolved.adapter = adapter
        return resolved

    async def fake_download(url: str) -> tuple[str, int]:  # noqa: ARG001
        return "2026-11/fake.mp4", 1024

    async def fake_probe(path) -> dict:  # noqa: ANN001, ARG001
        return {"width": 1280, "height": 720, "duration": 5.0}

    async def scenario() -> tuple[str, str]:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        task_id = await _seed(maker)
        await runner_module.TaskRunner()._poll_video(task_id)
        return await _status(maker, task_id)

    monkeypatch.setattr(runner_module, "SessionLocal", maker)
    monkeypatch.setattr(runner_module.provider_store, "resolve_model", resolve_model)
    monkeypatch.setattr(runner_module, "_download_video", fake_download)
    monkeypatch.setattr(runner_module.ffmpeg_service, "probe", fake_probe)
    monkeypatch.setattr(runner_module, "_video_poll_interval", lambda: 0)

    status, error = asyncio.run(scenario())

    assert status == "completed", f"网络抖动不该判失败：{status} {error}"
    assert adapter.calls == 3, "应当一直重试到上游有结果"


def test_business_error_still_fails(monkeypatch) -> None:
    """余额不足这类错误等多久都不会好，必须立刻失败并把原因写清楚。"""
    engine, maker = _engine()

    class Adapter:
        async def poll_video(self, remote_id: str) -> VideoStatus:  # noqa: ARG002
            raise AdapterError("KIE 返回错误（402）：Credits insufficient")

        async def close(self) -> None:
            return None

    class Resolved:
        pass

    async def resolve_model(db, key, modality):  # noqa: ANN001, ANN202
        resolved = Resolved()
        resolved.adapter = Adapter()
        return resolved

    async def scenario() -> tuple[str, str]:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        task_id = await _seed(maker)
        # 生产路径由 _poll_video_guarded 收住异常并标失败；直接调用裸轮询只会把异常抛出来
        await runner_module.TaskRunner()._poll_video_guarded(task_id)
        return await _status(maker, task_id)

    monkeypatch.setattr(runner_module, "SessionLocal", maker)
    monkeypatch.setattr(runner_module.provider_store, "resolve_model", resolve_model)
    monkeypatch.setattr(runner_module, "_video_poll_interval", lambda: 0)
    monkeypatch.setattr(runner_module, "_video_max_wait", lambda: 1)

    status, error = asyncio.run(scenario())

    assert status == "failed"
    assert "402" in error
