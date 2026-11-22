"""KIE 适配器（图片/视频）的离线测试：画幅映射、两代入参形状、协议注册、未接能力的报错。

真生成要花钱，所以真跑验证不在单测里做（2026-11 已用最小额度实测：
Seedream 5 Flash 文生图/参考图、Nano Banana 2、Seedance 2.5 文生视频、Wan 3.0 首帧生视频）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.providers.base import AdapterError, AudioRef  # noqa: E402
from app.providers.kie import (  # noqa: E402
    KieAdapter,
    accepts_image_refs,
    audio_upload_mime,
    image_aspect_ratio,
    image_family,
    image_input_payload,
    image_size_enum,
    video_family,
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


# ================================================================ 新一代模型（2026-11）


def test_new_image_families_are_recognized():
    assert image_family("seedream/5-flash-text-to-image") == "seedream_new"
    assert image_family("seedream/4.5-edit") == "seedream_new"
    assert image_family("nano-banana-2") == "nano_banana_2"
    assert image_family("gpt-image-2-text-to-image") == "gpt_image_2"
    assert image_family("bytedance/seedream-v4-edit") == "legacy"


def test_aspect_ratio_mapping_matches_new_image_models():
    assert image_aspect_ratio("1024x1024") == "1:1"
    assert image_aspect_ratio("1024x1536") == "3:4"
    assert image_aspect_ratio("1152x864") == "4:3"
    assert image_aspect_ratio("1280x720") == "16:9"
    assert image_aspect_ratio("720x1280") == "9:16"
    assert image_aspect_ratio("abc") == "1:1"


def test_seedream5_payload_uses_ratio_and_tier():
    flash = image_input_payload(
        "seedream/5-flash-text-to-image", "猫", "1024x1536", 1, []
    )
    assert flash == {
        "prompt": "猫",
        "aspect_ratio": "3:4",
        "size": "1K",
        "output_format": "png",
        "nsfw_checker": False,
    }
    assert "image_size" not in flash and "max_images" not in flash

    pro = image_input_payload(
        "seedream/5-pro-image-to-image", "改成水彩", "1024x1024", 1, ["https://x/a.png"]
    )
    assert pro["quality"] == "basic"
    assert pro["image_urls"] == ["https://x/a.png"]
    assert pro["output_format"] == "png"

    # 4.5 的输出格式不固定，不能硬塞 output_format
    old45 = image_input_payload("seedream/4.5-text-to-image", "猫", "1024x1024", 1, [])
    assert "output_format" not in old45
    assert old45["quality"] == "basic"


def test_nano_banana_and_gpt_image_field_names_differ():
    nb = image_input_payload("nano-banana-2", "猫", "1024x1024", 1, ["https://x/a.png"])
    assert nb["image_input"] == ["https://x/a.png"]
    assert nb["resolution"] == "1K"

    lite = image_input_payload(
        "nano-banana-2-lite", "猫", "1024x1024", 1, ["https://x/a.png"]
    )
    assert lite["image_urls"] == ["https://x/a.png"]
    assert "resolution" not in lite

    gpt = image_input_payload(
        "gpt-image-2-image-to-image", "合影", "1024x1024", 1, ["https://x/a.png"]
    )
    assert gpt["input_urls"] == ["https://x/a.png"]


def test_ref_support_is_per_model_not_per_generation_name():
    assert accepts_image_refs("seedream/5-flash-image-to-image") is True
    assert accepts_image_refs("seedream/4.5-edit") is True
    assert accepts_image_refs("nano-banana-2") is True
    assert accepts_image_refs("gpt-image-2-image-to-image") is True
    # GPT Image 2 的 text 版不收参考图
    assert accepts_image_refs("gpt-image-2-text-to-image") is False
    assert accepts_image_refs("seedream/5-flash-text-to-image") is False


def test_new_video_families_use_first_frame_url_and_integer_duration():
    assert video_family("bytedance/seedance-2-5") == "seedance2"
    assert video_family("kling-4.0/keyframe-to-video") == "kling4_keyframe"
    assert video_family("wan/3-0-video") == "wan3"
    assert video_family("kling/v2-5-turbo-image-to-video-pro") == "legacy"

    t2v = video_input_payload("bytedance/seedance-2-5", "海边", 15, "16:9", "")
    assert t2v == {"prompt": "海边", "resolution": "720p", "duration": 15, "aspect_ratio": "16:9"}

    i2v = video_input_payload(
        "bytedance/seedance-2-5", "海边", 5, "16:9", "https://x/f.png"
    )
    assert i2v["first_frame_url"] == "https://x/f.png"
    assert "aspect_ratio" not in i2v

    kling = video_input_payload(
        "kling-4.0/keyframe-to-video", "走", 5, "16:9", "https://x/f.png", "480p"
    )
    assert kling["resolution"] == "720p", "Kling 4.0 没有 480p 档，抬到 720p"
    assert kling["first_frame_url"] == "https://x/f.png"

    # 纯文生视频不能塞给 Kling（它只有关键帧生视频）
    try:
        video_input_payload("kling-4.0/keyframe-to-video", "走", 5, "16:9", "")
        raise AssertionError("Kling 4.0 没首帧却没报错")
    except AdapterError as e:
        assert "必须给首帧" in str(e), e

    wan = video_input_payload("wan/3-0-video", "猫", 5, "16:9", "", "480p")
    assert wan["resolution"] == "480P", "Wan 3.0 的分辨率是大写枚举"
    assert wan["duration"] == 5

    wan_i2v = video_input_payload("wan/3-0-video", "猫", 5, "16:9", "https://x/f.png")
    assert wan_i2v["first_frame_url"] == "https://x/f.png"


def test_reference_on_a_non_edit_model_is_rejected_before_uploading():
    """文生图模型给参考图要当场拒绝（还没走到上传，所以不联网也能测）。"""
    a = KieAdapter("https://api.kie.ai", "x")
    try:
        asyncio.run(a.generate_image(
            model="bytedance/seedream-v4-text-to-image", prompt="x", ref_images=[b"png"]))
        raise AssertionError("文生图模型收了参考图")
    except AdapterError as e:
        assert "edit" in str(e), e


# ================================================================ 视频参考（2026-11 适配）


def test_seedance_accepts_first_and_last_frames():
    p = video_input_payload(
        "bytedance/seedance-2", "走两步", 5, "9:16", "https://x/f.png",
        last_frame_url="https://x/l.png",
    )
    assert p["first_frame_url"] == "https://x/f.png"
    assert p["last_frame_url"] == "https://x/l.png"
    assert "reference_image_urls" not in p
    assert "aspect_ratio" not in p, "有首帧时画幅由画面决定，不该再传"


def test_last_frame_alone_is_rejected():
    for model in ("bytedance/seedance-2", "wan/3-0-video"):
        try:
            video_input_payload(model, "x", 5, "16:9", "", last_frame_url="https://x/l.png")
            raise AssertionError(f"{model} 收了没有首帧的尾帧")
        except AdapterError as e:
            assert "尾帧不能单独" in str(e), e


def test_frames_and_references_are_mutually_exclusive():
    for model in ("bytedance/seedance-2", "wan/3-0-video"):
        try:
            video_input_payload(
                model, "x", 5, "16:9", "https://x/f.png",
                last_frame_url="https://x/l.png",
                ref_image_urls=["https://x/r.png"],
            )
            raise AssertionError(f"{model} 把首尾帧和参考图同时收了")
        except AdapterError as e:
            assert "不能同时用" in str(e), e


def test_seedance_reference_mode_uses_reference_fields():
    p = video_input_payload(
        "bytedance/seedance-2", "两只猫", 5, "9:16", "",
        ref_image_urls=["https://x/a.png", "https://x/b.png"],
        ref_video_urls=["https://x/v.mp4"],
    )
    assert p["reference_image_urls"] == ["https://x/a.png", "https://x/b.png"]
    assert p["reference_video_urls"] == ["https://x/v.mp4"]
    assert p["aspect_ratio"] == "9:16"
    assert "first_frame_url" not in p


def test_first_frame_plus_audio_becomes_reference_mode_with_a_hint():
    """数字人那条路：一张图 + 一条配音。两种模式互斥，所以图要并进参考图并说明用途。"""
    p = video_input_payload(
        "bytedance/seedance-2", "她在说话", 5, "9:16", "https://x/f.png",
        ref_audio_urls=["https://x/a.mp3"],
    )
    assert p["reference_image_urls"] == ["https://x/f.png"]
    assert p["reference_audio_urls"] == ["https://x/a.mp3"]
    assert "首帧" in p["prompt"] and "参考音频" in p["prompt"]
    assert "first_frame_url" not in p


def test_video_duration_is_clamped_per_family():
    assert video_input_payload("bytedance/seedance-2", "x", 20, "16:9", "")["duration"] == 15
    assert video_input_payload("bytedance/seedance-2-5", "x", 20, "16:9", "")["duration"] == 20
    assert video_input_payload("wan/3-0-video", "x", 45, "16:9", "")["duration"] == 30
    assert video_input_payload("kling-4.0/keyframe-to-video", "x", 2, "16:9", "https://x/f")["duration"] == 3


def test_reference_counts_are_capped_before_uploading():
    try:
        video_input_payload(
            "bytedance/seedance-2", "x", 5, "16:9", "",
            ref_image_urls=[f"https://x/{i}.png" for i in range(10)],
        )
        raise AssertionError("超过 9 张参考图居然通过了")
    except AdapterError as e:
        assert "最多收 9 个参考图" in str(e), e


def test_kling4_accepts_last_frame_but_refuses_reference_material():
    p = video_input_payload(
        "kling-4.0/keyframe-to-video", "走", 5, "16:9", "https://x/f.png",
        last_frame_url="https://x/l.png",
    )
    assert p["last_frame_url"] == "https://x/l.png"
    assert p["resolution"] == "720p"

    for kwargs in (
        {"ref_image_urls": ["https://x/r.png"]},
        {"ref_audio_urls": ["https://x/a.mp3"]},
    ):
        try:
            video_input_payload(
                "kling-4.0/keyframe-to-video", "走", 5, "16:9", "https://x/f.png", **kwargs
            )
            raise AssertionError("Kling 4 关键帧收了参考素材")
        except AdapterError as e:
            assert "不收参考" in str(e), e


def test_legacy_models_still_refuse_references():
    try:
        video_input_payload(
            "kling/v2-5-turbo-image-to-video-pro", "x", 5, "16:9", "https://x/f.png",
            ref_image_urls=["https://x/r.png"],
        )
        raise AssertionError("老模型收了参考图")
    except AdapterError as e:
        assert "只吃首帧" in str(e), e


def test_audio_container_sniffing_matches_what_upstream_accepts():
    wav = b"RIFF" + bytes(4) + b"WAVEfmt "
    mp3 = b"ID3" + bytes(8)
    assert audio_upload_mime(AudioRef(data=wav, content_type="application/octet-stream")) == ("audio/wav", "wav")
    assert audio_upload_mime(AudioRef(data=mp3, content_type="")) == ("audio/mpeg", "mp3")
    try:
        audio_upload_mime(AudioRef(data=b"\x00\x01\x02", content_type="audio/flac"))
        raise AssertionError("flac 居然被当成参考音频")
    except AdapterError as e:
        assert "只认 wav / mp3" in str(e), e


def test_submit_video_rejects_invalid_combination_before_uploading():
    """组合不合法时不许先上传几 MB：预检在第一个网络请求之前。"""
    a = KieAdapter("https://api.kie.ai", "x")
    try:
        asyncio.run(a.submit_video(
            model="kling-4.0/keyframe-to-video", prompt="x", first_frame=b"png",
            ref_images=[b"ref"],
        ))
        raise AssertionError("Kling 4 参考图组合没有被拦住")
    except AdapterError as e:
        assert "不收参考" in str(e), e


def test_presets_mention_both_kie_entries():
    """聊天（openai 兼容）与图片（异步）是两条服务，预设里都要有，否则用户只会配对一半。"""
    registry = (BACKEND / "app" / "registry" / "schema_registry.py").read_text(encoding="utf-8")
    assert '"key": "kie"' in registry
    assert '"key": "kie_image"' in registry
    assert "api.kie.ai" in registry
    # 新模型要真的进预设，否则界面上选不到（只有适配器支持等于没接）
    for name in (
        "seedream/5-flash-text-to-image",
        "nano-banana-2",
        "gpt-image-2-text-to-image",
        "bytedance/seedance-2-5",
        "kling-4.0/keyframe-to-video",
        "wan/3-0-video",
    ):
        assert name in registry, name


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
