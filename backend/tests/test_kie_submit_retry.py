"""KIE 提交任务：断网先自己重连，5 次都失败才交出「不确定」的结论。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.providers import kie as kie_module  # noqa: E402
from app.providers.base import AdapterError  # noqa: E402


def _adapter() -> kie_module.KieAdapter:
    return kie_module.KieAdapter("https://api.kie.ai", "test-key")


def test_submit_retries_until_network_recovers(monkeypatch) -> None:
    monkeypatch.setattr(kie_module, "_SUBMIT_BACKOFF", 0)
    adapter = _adapter()
    calls = {"n": 0}

    async def fake_call(client, method, path, **kw):  # noqa: ANN001, ANN202
        calls["n"] += 1
        if calls["n"] < 4:
            raise AdapterError("网络层出错，稍后重试（RemoteProtocolError）")
        return {"taskId": "task-1"}

    monkeypatch.setattr(adapter, "_call", fake_call)

    data = asyncio.run(adapter._submit(None, {"model": "m"}))  # type: ignore[arg-type]

    assert data["taskId"] == "task-1"
    assert calls["n"] == 4, "断网应当自己重连，而不是直接失败"


def test_submit_gives_up_after_five_attempts(monkeypatch) -> None:
    monkeypatch.setattr(kie_module, "_SUBMIT_BACKOFF", 0)
    adapter = _adapter()
    calls = {"n": 0}

    async def fake_call(client, method, path, **kw):  # noqa: ANN001, ANN202
        calls["n"] += 1
        raise AdapterError("连不上这个地址（ConnectError）")

    monkeypatch.setattr(adapter, "_call", fake_call)

    with pytest.raises(AdapterError) as err:
        asyncio.run(adapter._submit(None, {}))  # type: ignore[arg-type]

    message = str(err.value)
    assert calls["n"] == 5
    assert "5 次" in message
    # 上游没有任务列表接口，只能指到控制台 Logs 去核对
    assert "Logs" in message


def test_submit_does_not_retry_business_errors(monkeypatch) -> None:
    monkeypatch.setattr(kie_module, "_SUBMIT_BACKOFF", 0)
    adapter = _adapter()
    calls = {"n": 0}

    async def fake_call(client, method, path, **kw):  # noqa: ANN001, ANN202
        calls["n"] += 1
        raise AdapterError("KIE 返回错误（402）：Credits insufficient")

    monkeypatch.setattr(adapter, "_call", fake_call)

    with pytest.raises(AdapterError):
        asyncio.run(adapter._submit(None, {}))  # type: ignore[arg-type]

    assert calls["n"] == 1, "余额不足重试多少次都一样，不该白等"
