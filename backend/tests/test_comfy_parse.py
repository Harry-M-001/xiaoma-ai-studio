"""ComfyUI 工作流解析与参数注入验证（无需 pytest：直接 python 运行；装了 pytest 也可用）。

运行：venv/Scripts/python tests/test_comfy_parse.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.comfy_workflow_service import apply_params, parse_workflow

SD_GRAPH = {
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 123, "steps": 20, "cfg": 8.0, "sampler_name": "euler",
            "scheduler": "normal", "denoise": 1.0,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
    },
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd_xl.safetensors"}},
    "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "正向默认", "clip": ["4", 1]}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "负向默认", "clip": ["4", 1]}},
    "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI", "images": ["8", 0]}},
}


def test_parse_image_workflow():
    out = parse_workflow(SD_GRAPH)
    keys = {p["key"] for p in out["paramMap"]}
    assert out["outputKind"] == "image", out["outputKind"]
    assert out["nodeCount"] == 7
    # 采样器 + 模型 + 尺寸 + 两个提示词
    for k in ("3_steps", "3_cfg", "3_sampler_name", "3_seed", "4_ckpt_name",
              "5_width", "5_height", "5_batch_size", "6_text", "7_text"):
        assert k in keys, f"缺少参数 {k}"
    # 第一个 CLIPTextEncode 标记为 prompt 类型（上游注入目标），第二个是普通文本
    types = {p["key"]: p["type"] for p in out["paramMap"]}
    assert types["6_text"] == "prompt"
    assert types["7_text"] == "text"


def test_parse_rejects_ui_format():
    try:
        parse_workflow({"nodes": [], "links": [], "version": 0.4})
    except ValueError as e:
        assert "API 格式" in str(e)
    else:
        raise AssertionError("UI 格式应被拒绝")


def test_parse_video_workflow():
    graph = {
        "1": {"class_type": "EmptySD3LatentVideo", "inputs": {"width": 1280, "height": 720, "length": 121, "batch_size": 1}},
        "2": {"class_type": "VHS_VideoCombine", "inputs": {"frame_rate": 24, "format": "video/h264-mp4", "images": ["1", 0]}},
    }
    out = parse_workflow(graph)
    assert out["outputKind"] == "video"
    keys = {p["key"] for p in out["paramMap"]}
    assert "1_length" in keys and "2_frame_rate" in keys


def test_parse_core_video_output_and_fps():
    """核心 ComfyUI 用 SaveVideo/SaveWEBM + fps（不是第三方 VHS 的 frame_rate）。"""
    graph = {
        "1": {"class_type": "EmptyLatentImage", "inputs": {"width": 832, "height": 480, "batch_size": 1}},
        "2": {"class_type": "CreateVideo", "inputs": {"images": ["1", 0], "fps": 24}},
        "3": {"class_type": "SaveVideo", "inputs": {"video": ["2", 0], "format": "mp4", "codec": "auto"}},
    }
    out = parse_workflow(graph)
    keys = {p["key"] for p in out["paramMap"]}
    # 之前只认 frame_rate，核心节点的 fps 会被漏掉
    assert "2_fps" in keys, keys
    labels = {p["key"]: p["label"] for p in out["paramMap"]}
    assert labels["2_fps"] == "帧率"
    assert out["outputKind"] == "video", out["outputKind"]
    # format 作为下拉暴露（有 options 时是 select）
    fmt = next(p for p in out["paramMap"] if p["key"] == "3_format")
    assert fmt["type"] == "select"


def test_save_webm_is_video_not_image():
    """SaveWEBM 曾被误放进图片输出集合，会把纯 webm 工作流预判成图片。"""
    graph = {
        "1": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
        "2": {"class_type": "SaveWEBM", "inputs": {"images": ["1", 0], "codec": "vp9", "fps": 16, "crf": 32}},
    }
    out = parse_workflow(graph)
    assert out["outputKind"] == "video", out["outputKind"]
    assert "2_fps" in {p["key"] for p in out["paramMap"]}


def test_parse_animated_webp_is_image():
    """SaveAnimatedWEBP 产出 .webp，按扩展名归档为图片（kinds 与 outputKind 保持一致）。"""
    graph = {
        "1": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
        "2": {"class_type": "SaveAnimatedWEBP", "inputs": {"images": ["1", 0], "fps": 8, "lossless": True, "quality": 80, "method": "default"}},
    }
    out = parse_workflow(graph)
    assert out["outputKind"] == "image", out["outputKind"]
    assert "2_fps" in {p["key"] for p in out["paramMap"]}


def test_apply_params_injection():
    pm = parse_workflow(SD_GRAPH)["paramMap"]
    values = {"3_steps": 30, "5_width": 768, "5_height": 768}
    g = apply_params(SD_GRAPH, pm, values, prompt_text="上游注入的提示词")
    assert g["3"]["inputs"]["steps"] == 30
    assert g["5"]["inputs"]["width"] == 768
    # 注入文本覆盖工作流默认正向提示词；负向提示词不受影响
    assert g["6"]["inputs"]["text"] == "上游注入的提示词"
    assert g["7"]["inputs"]["text"] == "负向默认"
    # seed 未填 → 随机整数
    assert isinstance(g["3"]["inputs"]["seed"], int) and g["3"]["inputs"]["seed"] >= 0
    # 未动的字段保持原值；原对象未被修改
    assert g["3"]["inputs"]["cfg"] == 8.0
    assert SD_GRAPH["3"]["inputs"]["steps"] == 20
    # 固定 seed
    g3 = apply_params(SD_GRAPH, pm, {"3_seed": 42}, prompt_text="")
    assert g3["3"]["inputs"]["seed"] == 42


def test_apply_params_empty_prompt_keeps_default():
    pm = parse_workflow(SD_GRAPH)["paramMap"]
    g = apply_params(SD_GRAPH, pm, {}, prompt_text="")
    # 无注入且无表单值 → 保留工作流原提示词
    assert g["6"]["inputs"]["text"] == "正向默认"


def test_apply_params_image_slots():
    pm = parse_workflow(SD_GRAPH)["paramMap"]
    graph = {**SD_GRAPH, "10": {"class_type": "LoadImage", "inputs": {"image": "example.png"}}}
    pm2 = parse_workflow(graph)["paramMap"]
    g = apply_params(graph, pm2, {}, image_files={"10_image": "canvas_ref_1_0.png"}, prompt_text="")
    assert g["10"]["inputs"]["image"] == "canvas_ref_1_0.png"


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
