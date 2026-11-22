"""火山方舟 Ark / 阿里百炼 DashScope 最新模型的适配契约。

守三件事：

1. **方舟图片数量**：Seedream 5.x 一次只出一张，界面选 N 张必须发 N 次，
   不能悄悄只回一张（也不能把 4.0 的 `n` 参数硬塞给 5.x）。
2. **百炼视频素材**：参考图收 base64；参考视频 / 音频只收公网 URL，本地必须报错，
   不许静默丢掉素材。
3. **参数映射**：尺寸 `宽*高`、可灵画幅回落、各模型时长夹取，都有确定口径。

运行：backend/venv/Scripts/python tests/test_ark_bailian.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.providers.ark import ArkAdapter  # noqa: E402
from app.providers.base import AdapterError  # noqa: E402
from app.providers.dashscope import (  # noqa: E402
    DashScopeAdapter,
    _clamp_duration,
    _kling_ratio,
    wan_size,
)
from app.registry.schema_registry import SCHEMA_REGISTRY  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + bytes(8)
ONE_B64 = base64.b64encode(b"image-bytes").decode("ascii")


class _Resp:
    def __init__(self, payload=None, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {"id": "task-1"}
        self.content = json.dumps(self._payload).encode("utf-8")
        self.text = json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _Client:
    def __init__(self, resp: _Resp) -> None:
        self.resp = resp
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, **kwargs) -> _Resp:
        self.posts.append((url, kwargs))
        return self.resp

    async def get(self, url: str, **kwargs) -> _Resp:
        return self.resp


def _with_client(adapter, client):  # noqa: ANN001
    async def fake_client():
        return client

    adapter.client = fake_client  # type: ignore[method-assign]
    return adapter


# ================================================================ 方舟：图片数量


def test_seedream_5_issues_one_request_per_image():
    """选 3 张就发 3 次；请求体里不许出现 5.x 不认识的 `n`。"""
    client = _Client(_Resp({"data": [{"b64_json": ONE_B64}]}))
    adapter = _with_client(ArkAdapter("https://ark.example.com/api/v3", "k"), client)

    images = asyncio.run(
        adapter.generate_image(model="doubao-seedream-5-5-261028", prompt="猫", n=3)
    )

    assert len(client.posts) == 3, f"只发了 {len(client.posts)} 次"
    assert len(images) == 3
    for url, kwargs in client.posts:
        assert url.endswith("/images/generations"), url
        assert "n" not in kwargs["json"], kwargs["json"]
        assert kwargs["json"]["size"] == "1024x1024", kwargs["json"]


def test_seedream_size_is_lifted_to_the_model_minimum():
    """默认 1024x1024 在 5.0 Lite 上不够（最低 2K），必须按比例抬到合规像素。"""
    from app.providers.ark import ark_image_size

    # 5.5 / 5.0 pro：最低 921600 像素，1024x1024 已经够
    assert ark_image_size("doubao-seedream-5-5-261028", "1024x1024") == "1024x1024"
    # 5.0 Lite：最低 3686400，按比例放大，比例不变
    lifted = ark_image_size("doubao-seedream-5-0-lite-260128", "1024x1024")
    w, h = (int(x) for x in lifted.split("x"))
    assert w == h, lifted
    assert w * h >= 3_686_400, lifted
    # 档位写法原样透传
    assert ark_image_size("doubao-seedream-5-0-lite-260128", "2K") == "2K"


def test_seedream_reference_images_still_go_as_base64():
    client = _Client(_Resp({"data": [{"b64_json": ONE_B64}]}))
    adapter = _with_client(ArkAdapter("https://ark.example.com/api/v3", "k"), client)

    asyncio.run(adapter.generate_image(model="doubao-seedream-5-5-261028", prompt="改水彩", ref_images=[PNG]))

    body = client.posts[0][1]["json"]
    assert body["image"][0].startswith("data:image/png;base64,"), body


# ================================================================ 百炼：参数映射


def test_wan_size_uses_pixel_star_and_keeps_tiers():
    assert wan_size("1024x1536") == "1024*1536"
    assert wan_size("2K") == "2K"
    assert wan_size("") == "2K"
    # 认不出来就透传，让上游说清楚，不擅自换成「差不多」的尺寸
    assert wan_size("big") == "big"


def test_duration_is_clamped_per_model():
    assert _clamp_duration("kling/kling-v3-video-generation", 30) == 15
    assert _clamp_duration("kling/kling-v4-video-generation", 30) == 30
    assert _clamp_duration("wan3.0-video", 40) == 30
    assert _clamp_duration("wan2.7-i2v", 40) == 15
    assert _clamp_duration("wan2.7-i2v", 1) == 2


def test_kling_ratio_falls_back_to_what_the_model_accepts():
    assert _kling_ratio("4:3", v4=False) == "16:9"
    assert _kling_ratio("9:16", v4=False) == "9:16"
    assert _kling_ratio("21:9", v4=True) == "21:9"
    assert _kling_ratio("", v4=True) == "16:9"


def test_wan_reference_video_is_refused_before_it_leaves_the_machine():
    client = _Client(_Resp({"output": {"task_id": "t1"}}))
    adapter = _with_client(DashScopeAdapter("https://dashscope.example.com/api/v1", "k"), client)

    try:
        asyncio.run(
            adapter.submit_video(model="wan3.0-video", prompt="海边", ref_videos=[b"video"])
        )
    except AdapterError as e:
        assert "公网" in str(e) and "OSS" in str(e), str(e)
        assert not client.posts, "本地就能判的错，不该还往上打一次"
        return
    raise AssertionError("参考视频竟然发出去了")


def test_kling_v4_gets_v4_parameters():
    client = _Client(_Resp({"output": {"task_id": "t1"}}))
    adapter = _with_client(DashScopeAdapter("https://dashscope.example.com/api/v1", "k"), client)

    rid = asyncio.run(
        adapter.submit_video(
            model="kling/kling-v4-video-generation",
            prompt="海边",
            duration=30,
            ratio="21:9",
            resolution="4k",
            first_frame=PNG,
        )
    )

    assert rid == "t1"
    body = client.posts[0][1]["json"]
    assert body["parameters"]["duration"] == 30, body
    assert body["parameters"]["aspect_ratio"] == "21:9", body
    assert body["parameters"]["mode"] == "pro", "4.0 不支持 4k，应回落到 pro"
    assert body["input"]["media"][0]["type"] == "first_frame", body


# ================================================================ 时长区间


def test_video_models_expose_their_own_duration_range():
    """同一个下拉里既有 5/10 的老模型，也有 30 秒的 Seedance 2.5 / Wan 3.0。"""
    from app.services.provider_store import video_duration_limit

    assert video_duration_limit("ark", "doubao-seedance-2-5-260628") == (4, 30)
    assert video_duration_limit("ark", "doubao-seedance-2-0-260128") == (4, 15)
    assert video_duration_limit("ark", "doubao-seedance-1-0-pro-250528") == (5, 10)
    assert video_duration_limit("dashscope", "wan3.0-video") == (2, 30)
    assert video_duration_limit("dashscope", "wan2.7-i2v") == (2, 15)
    assert video_duration_limit("dashscope", "kling/kling-v4-video-generation") == (3, 30)
    assert video_duration_limit("kie", "bytedance/seedance-2-5") == (4, 30)
    # 认不出厂商：给常用区间，不装懂
    assert video_duration_limit("openai", "some-video-model") == (3, 15)


def test_duration_validation_is_a_range_not_a_fixed_list():
    from app.services.option_service import validate_duration

    assert validate_duration(3, 3, 15) == 3
    assert validate_duration("7", 4, 30) == 7, "字符串数字也要认"
    try:
        validate_duration(31, 4, 30)
    except AdapterError as e:
        assert "4–30" in str(e), str(e)
    else:  # pragma: no cover
        raise AssertionError("31 秒竟然通过了")


def test_ark_duration_is_clamped_per_model():
    from app.providers.ark import ark_video_duration

    assert ark_video_duration("doubao-seedance-2-5-260628", 30) == 30
    assert ark_video_duration("doubao-seedance-2-5-260628", 40) == 30
    assert ark_video_duration("doubao-seedance-2-0-260128", 20) == 15
    # 1.0 只认 5 / 10，就近归到一档
    assert ark_video_duration("doubao-seedance-1-0-pro-250528", 7) == 5
    assert ark_video_duration("doubao-seedance-1-0-pro-250528", 9) == 10


# ================================================================ 预设


def test_presets_carry_the_latest_models():
    presets = {p["key"]: p for p in SCHEMA_REGISTRY["provider_presets"].seed}
    ark = {m["name"] for m in presets["ark"]["models_json"]}
    assert "doubao-seedream-5-5-261028" in ark
    assert "doubao-seedance-2-5-260628" in ark
    assert "doubao-seed-2-1-pro-260915" in ark

    bailian = {m["name"] for m in presets["bailian"]["models_json"]}
    assert presets["bailian"]["kind"] == "dashscope"
    assert "wan3.0-video" in bailian
    assert "wan2.7-image-pro" in bailian
    assert "kling/kling-v4-omni-reference-video-generation" in bailian
