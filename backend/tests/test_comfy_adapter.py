"""ComfyUI 适配器：产物归类与错误可读性（直接 python 运行）。

这里守的是两个曾经真实出错的点（对照官方 server.py / execution.py 核实）：
1. 核心 ComfyUI 的 SaveVideo / SaveWEBM / SaveAnimatedWEBP 都把产物放在 ui 的
   **images** 键里，只看键名会把 mp4 当图片归档 → 必须按**文件扩展名**判定；
2. `/prompt` 的 400 是结构化 `error` + `node_errors`，直接抛原始 JSON 用户看不懂。

运行：venv/Scripts/python tests/test_comfy_adapter.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.providers import comfyui
from app.providers.comfyui import ComfyUIAdapter
from app.services.comfy_workflow_service import classify_artifact


class _FakeResp:
    def __init__(self, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)[:200]

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp

    async def get(self, url: str, **kwargs: Any) -> _FakeResp:  # noqa: ANN003
        return self._resp


def _adapter(history_payload: Any, status_code: int = 200) -> ComfyUIAdapter:
    adapter = ComfyUIAdapter("http://127.0.0.1:8188", "")

    async def fake_client() -> _FakeClient:
        return _FakeClient(_FakeResp(status_code, history_payload))

    adapter.client = fake_client  # type: ignore[method-assign]
    return adapter


def _entry(**outputs: Any) -> dict:
    return {
        "status": {"status_str": "success", "completed": True, "messages": []},
        "outputs": outputs,
    }


# ---------------- 产物类型：按扩展名判定 ----------------


def test_classify_artifact_by_extension():
    assert classify_artifact("ComfyUI_00001_.png") == "image"
    assert classify_artifact("ComfyUI_00001_.webp") == "image"
    assert classify_artifact("ComfyUI_00001_.mp4") == "video"
    assert classify_artifact("ComfyUI_00001_.WEBM") == "video"
    assert classify_artifact("clip.mov") == "video"
    assert classify_artifact("voice.flac") == "other"
    assert classify_artifact("noext") == "other"
    assert classify_artifact("") == "other"


def test_save_video_output_is_video_not_image():
    """SaveVideo 的产物走 images 键 + .mp4 扩展名 → 必须归档为 video。"""
    payload = {
        "pid": _entry(
            **{
                "12": {
                    "images": [
                        {"filename": "ComfyUI_00001_.mp4", "subfolder": "video", "type": "output"}
                    ],
                    "animated": [True],
                }
            }
        )
    }
    st = asyncio.run(_adapter(payload).poll_workflow("pid"))
    assert st.status == "succeeded", st
    assert len(st.files) == 1
    f = st.files[0]
    assert f.kind == "video", f"mp4 被归档成了 {f.kind}"
    assert f.filename == "ComfyUI_00001_.mp4"
    assert f.subfolder == "video"


def test_save_webm_output_is_video():
    payload = {"pid": _entry(**{"7": {"images": [{"filename": "out_00001_.webm", "type": "output"}]}})}
    st = asyncio.run(_adapter(payload).poll_workflow("pid"))
    assert [f.kind for f in st.files] == ["video"]


def test_vhs_gifs_slot_still_works():
    """第三方 VideoHelperSuite 用 gifs 槽位承载 mp4。"""
    payload = {"pid": _entry(**{"9": {"gifs": [{"filename": "anim_00001_.mp4", "type": "output"}]}})}
    st = asyncio.run(_adapter(payload).poll_workflow("pid"))
    assert [f.kind for f in st.files] == ["video"]


def test_mixed_outputs_are_all_collected():
    payload = {
        "pid": _entry(
            **{
                "1": {"images": [{"filename": "a_00001_.png", "type": "output"}]},
                "2": {"images": [{"filename": "b_00001_.mp4", "type": "output"}]},
            }
        )
    }
    st = asyncio.run(_adapter(payload).poll_workflow("pid"))
    kinds = sorted(f.kind for f in st.files)
    assert kinds == ["image", "video"], kinds


def test_folder_type_is_passed_through():
    """type 必须原样用回传值：PreviewImage 落在 temp，硬编码 output 会 404。"""
    payload = {
        "pid": _entry(
            **{"3": {"images": [{"filename": "ComfyUI_temp_x_00001_.png", "type": "temp"}]}}
        )
    }
    st = asyncio.run(_adapter(payload).poll_workflow("pid"))
    assert st.files[0].folder_type == "temp"


def test_unsupported_artifact_reports_readable_error():
    """跑完了但只有音频 → 明确告诉用户，而不是静默返回空成功。"""
    payload = {"pid": _entry(**{"4": {"audio": [{"filename": "voice_00001_.flac", "type": "output"}]}})}
    st = asyncio.run(_adapter(payload).poll_workflow("pid"))
    assert st.status == "failed"
    assert "voice_00001_.flac" in (st.error or "")
    assert "暂不支持" in (st.error or "")


# ---------------- 状态判定 ----------------


def test_unknown_prompt_id_means_processing():
    """官方 /history/{id} 查不到时返回 200 + {}（不是 404），应视为仍在执行。"""
    for payload in ({}, {"other-id": _entry()}):
        st = asyncio.run(_adapter(payload).poll_workflow("pid"))
        assert st.status == "processing", (payload, st)


def test_execution_error_is_surfaced_with_node():
    payload = {
        "pid": {
            "status": {
                "status_str": "error",
                "completed": False,
                "messages": [
                    ["execution_start", {"prompt_id": "pid"}],
                    [
                        "execution_error",
                        {
                            "prompt_id": "pid",
                            "node_id": "3",
                            "node_type": "KSampler",
                            "exception_message": "CUDA out of memory. Tried to allocate 2.00 GiB",
                            "exception_type": "torch.cuda.OutOfMemoryError",
                        },
                    ],
                ],
            },
            "outputs": {},
        }
    }
    st = asyncio.run(_adapter(payload).poll_workflow("pid"))
    assert st.status == "failed"
    assert "节点 3" in (st.error or "") and "KSampler" in (st.error or "")
    assert "CUDA out of memory" in (st.error or "")


def test_error_without_detail_still_gives_guidance():
    info = {"status_str": "error", "messages": []}
    msg = comfyui._describe_failure(info)
    assert "控制台日志" in msg


# ---------------- /prompt 提交失败的报错翻译 ----------------


def test_submit_error_is_readable():
    resp = _FakeResp(
        400,
        {
            "error": {
                "type": "prompt_outputs_failed_validation",
                "message": "Prompt outputs failed validation",
                "details": "",
                "extra_info": {},
            },
            "node_errors": {
                "5": {
                    "class_type": "KSampler",
                    "errors": [
                        {"type": "invalid_input_type", "details": "steps: 0 is smaller than min 1"}
                    ],
                    "dependent_outputs": ["9"],
                }
            },
        },
    )
    msg = comfyui._describe_submit_error(resp)
    assert "校验未通过" in msg
    assert "节点 5" in msg and "KSampler" in msg
    assert "steps" in msg


def test_submit_error_without_json_falls_back():
    class _Bad:
        status_code = 500
        text = "<html>gateway error</html>"

        def json(self):
            raise ValueError("not json")

    msg = comfyui._describe_submit_error(_Bad())  # type: ignore[arg-type]
    assert "HTTP 500" in msg


def test_submit_error_generic_when_only_type():
    resp = _FakeResp(400, {"error": {"type": "no_prompt"}, "node_errors": {}})
    msg = comfyui._describe_submit_error(resp)
    assert "no_prompt" in msg


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
