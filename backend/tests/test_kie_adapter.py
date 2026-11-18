"""KIE 适配器（图片）的离线测试：画幅映射、协议注册、未接能力的报错。

真生成要花钱，所以真跑验证不在单测里做（已用 1 张图验证过：Seedream 4.0 文生图）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.providers.base import AdapterError  # noqa: E402
from app.providers.kie import (  # noqa: E402
    KieAdapter,
    image_input_payload,
    image_size_enum,
    video_input_payload,
)
from app.registry import adapters  # noqa: E402


def test_image_size_enum_covers_common_ratios():
    assert image_size_enum("1024x1024") == "square_hd"
    assert image_size_enum("1024x1536") == "portrait_4_3"
    assert image_size_enum("1536x1024") == "landscape_4_3"
    assert image_size_enum("720x1280") == "portrait_16_9"
    assert image_size_enum("1280x720") == "landscape_16_9"
    # 认不出来就按方图，绝不抛异常：画幅只是「尽量对上」，不该让整次生成失败
    assert image_size_enum("") == "square_hd"
    assert image_size_enum("abc") == "square_hd"


def test_kie_is_registered_as_an_adapter_kind():
    kinds = {k["kind"] for k in adapters.list_kinds()}
    assert "kie" in kinds
    adapter = adapters.build_adapter("kie", "https://api.kie.ai", "x")
    assert isinstance(adapter, KieAdapter)


def test_unimplemented_capabilities_say_so_instead_of_pretending():
    """语音、以及视频的「全能参考」没接就明确报错——静默给个空结果是最坏的一种「成功」。"""
    a = KieAdapter("https://api.kie.ai", "x")
    try:
        asyncio.run(a.synthesize_speech(model="x", text="你好"))
        raise AssertionError("未接入的语音居然没报错")
    except AdapterError as e:
        assert str(e), e
    try:
        asyncio.run(a.submit_video(model="kling/v2-5-turbo-image-to-video-pro", prompt="x",
                                   ref_images=[b"x"]))
        raise AssertionError("参考图没接却静默通过了")
    except AdapterError as e:
        assert "参考" in str(e), e
    try:
        a.chat_stream("m", [{"role": "user", "content": "hi"}])
        raise AssertionError("文本通道不该被当成可用")
    except AdapterError as e:
        assert "OpenAI 兼容" in str(e), e


# ================================================================ 入参形状


def test_image_input_payload_shapes():
    plain = image_input_payload("bytedance/seedream-v4-text-to-image", "猫", "1024x1024", 2, [])
    assert plain == {
        "prompt": "猫", "image_size": "square_hd", "image_resolution": "1K", "max_images": 2,
    }
    assert "image_urls" not in plain

    edit = image_input_payload(
        "bytedance/seedream-v4-edit", "改成水彩", "1024x1536", 1, ["https://x/a.png"]
    )
    assert edit["image_urls"] == ["https://x/a.png"]
    assert edit["image_size"] == "portrait_4_3"


def test_video_input_payload_routes_by_model_name():
    i2v = video_input_payload(
        "kling/v2-5-turbo-image-to-video-pro", "走两步", 5, "16:9", "https://x/f.png"
    )
    assert i2v["image_url"] == "https://x/f.png"
    assert i2v["duration"] == "5"
    assert "aspect_ratio" not in i2v, "图生视频的画幅由首帧决定，不该再传画幅"

    t2v = video_input_payload(
        "kling/v2-5-turbo-text-to-video-pro", "一只猫", 12, "9:16", ""
    )
    assert t2v["aspect_ratio"] == "9:16"
    assert t2v["duration"] == "10", "超过 10 秒按 10 秒的档位（Kling 只有 5/10）"
    assert "image_url" not in t2v

    # 认不出的模型不猜参数
    try:
        video_input_payload("some/new-video-model", "x", 5, "16:9", "")
        raise AssertionError("未知视频模型居然被猜了参数")
    except AdapterError as e:
        assert "还没适配" in str(e), e

    # 张冠李戴也要拦：图生视频没首帧 / 文生视频硬塞首帧
    for model, url in (
        ("kling/v2-5-turbo-image-to-video-pro", ""),
        ("kling/v2-5-turbo-text-to-video-pro", "https://x/f.png"),
    ):
        try:
            video_input_payload(model, "x", 5, "16:9", url)
            raise AssertionError(f"{model} 的参数矛盾没被拦住")
        except AdapterError:
            pass


def test_reference_on_a_non_edit_model_is_rejected_before_uploading():
    """文生图模型给参考图要当场拒绝（还没走到上传，所以不联网也能测）。"""
    a = KieAdapter("https://api.kie.ai", "x")
    try:
        asyncio.run(a.generate_image(
            model="bytedance/seedream-v4-text-to-image", prompt="x", ref_images=[b"png"]
        ))
        raise AssertionError("文生图模型收了参考图")
    except AdapterError as e:
        assert "edit" in str(e), e


def test_presets_mention_both_kie_entries():
    """聊天（openai 兼容）与图片（异步）是两条服务，预设里都要有，否则用户只会配对一半。"""
    registry = (BACKEND / "app" / "registry" / "schema_registry.py").read_text(encoding="utf-8")
    assert '"key": "kie"' in registry
    assert '"key": "kie_image"' in registry
    assert "api.kie.ai" in registry


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
