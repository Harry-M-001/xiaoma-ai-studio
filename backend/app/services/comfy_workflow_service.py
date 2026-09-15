"""ComfyUI 工作流解析与执行准备。

核心思路（与 Toonflow 同路线）：workflow_api.json 是静态模板，灵活性来自
「参数映射表」——上传时后端遍历图，按节点 class_type 自动发现可调参数
（模型/步数/CFG/采样器/宽高/帧数/提示词/seed/参考图），浮框按映射表渲染表单；
执行时把表单值写回图再提交。

参数发现规则（按 class_type 优先，通用字段名兜底）：
- CheckpointLoaderSimple / VAELoader / LoraLoader 等：模型名 → select（options 来自 /object_info）
- CLIPTextEncode：text → 提示词（第一个正向为 prompt 类型，上游文本注入目标）
- KSampler / KSamplerAdvanced：steps/cfg/sampler_name/scheduler/seed/denoise
- EmptyLatentImage：width/height/batch_size
- LoadImage：image → 参考图槽位（按 refImages 顺序分配）
- 视频类（VHS_VideoCombine 等）：frame_rate / length / num_frames / frames
"""

from __future__ import annotations

import json
import random
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ComfyWorkflow, ProviderService

# ---- 参数类型与中文标签 ----

_FIELD_LABELS: dict[str, str] = {
    "text": "提示词",
    "ckpt_name": "模型",
    "lora_name": "LoRA",
    "vae_name": "VAE",
    "steps": "步数",
    "cfg": "CFG 引导系数",
    "sampler_name": "采样器",
    "scheduler": "调度器",
    "seed": "种子",
    "denoise": "降噪强度",
    "width": "宽度",
    "height": "高度",
    "batch_size": "张数",
    "frame_rate": "帧率",
    "fps": "帧率",
    "length": "帧数/长度",
    "num_frames": "帧数",
    "frames": "帧数",
    "image": "参考图",
    "format": "输出格式",
}

_NUMBER_FIELDS = {"steps", "cfg", "seed", "denoise", "width", "height", "batch_size",
                  "frame_rate", "fps", "length", "num_frames", "frames"}
_SELECT_MODEL_FIELDS = {"ckpt_name", "lora_name", "vae_name", "model_name", "unet_name"}
_SELECT_ENUM_FIELDS = {"sampler_name", "scheduler", "format"}

# ---- 输出节点分类（仅用于 output_kind 预判）----
# 注意：真正落地时的产物类型以**文件扩展名**为准。核心 ComfyUI 的 SaveVideo / SaveWEBM
# 以及 SaveAnimatedWEBP / SaveAnimatedPNG 都返回 {"images": [...]} 同一个 ui 键，
# 只看键名会把 mp4/webm 当成图片归档（详见 docs/comfyui.md）。
_IMAGE_OUTPUT_CLASSES = {
    "SaveImage", "SaveImageWithAlpha", "SaveImageAdvanced", "Image Save",
    "SaveAnimatedWEBP", "SaveAnimatedPNG", "PreviewImage",
}
_VIDEO_OUTPUT_CLASSES = {"VHS_VideoCombine", "SaveVideo", "SaveWEBM"}

# 扩展名 → 产物类型（单一事实来源，适配器与解析器共用）
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif")
VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".avi", ".mov", ".m4v")

_TEXT_INPUT_CLASSES = {"CLIPTextEncode", "CLIPTextEncodeSDXL", "String", "ShowText|pysssss"}
_IMAGE_INPUT_CLASSES = {"LoadImage", "LoadImageMask", "Image Loader", "ImpactWildcardImage", "LoadImageOutput"}
_VIDEO_LATENT_CLASSES = {"EmptyHunyuanLatentVideo", "EmptySD3LatentVideo", "SVD_img2vid_Conditioning",
                         "EmptyLatent", "EmptyMochiLatentVideo", "EmptyLTXVLatentVideo", "CreateEmptyLatent"}

_MAX_PARAMS = 40


def classify_artifact(filename: str) -> str:
    """按文件扩展名判定产物类型，返回 image | video | other。"""
    name = (filename or "").lower()
    if "." not in name:
        return "other"
    ext = "." + name.rsplit(".", 1)[-1]
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return "other"


def _is_api_format(graph: Any) -> bool:
    """校验是否 ComfyUI API 格式：{nodeId: {class_type, inputs}}。"""
    if not isinstance(graph, dict) or not graph:
        return False
    sample = 0
    for v in graph.values():
        if isinstance(v, dict) and "class_type" in v and "inputs" in v:
            sample += 1
    return sample == len(graph)


def _node_label(i: int, class_type: str) -> str:
    return f"{class_type}" if i == 0 else f"{class_type}·{i + 1}"


def parse_workflow(graph: dict[str, Any], object_info: dict | None = None) -> dict:
    """解析图，生成参数映射表与输出类型预判。

    返回 {"paramMap": [...], "outputKind": "image|video|mixed", "nodeCount": N}
    每个参数项：{key, label, type(prompt|text|number|select|image|seed), nodeId, field, default, options?, min?, max?, step?}
    """
    if not _is_api_format(graph):
        raise ValueError("不是 ComfyUI API 格式的工作流（应为 {节点id: {class_type, inputs}}，请用 ComfyUI 界面「导出（API）」）")

    params: list[dict[str, Any]] = []
    text_seen = 0
    output_kinds: set[str] = set()

    oi = object_info or {}

    def _oi_options(class_type: str, field: str) -> list[str]:
        node_def = oi.get(class_type)
        if not isinstance(node_def, dict):
            return []
        inputs = node_def.get("input") or {}
        for group in ("required", "optional"):
            spec = (inputs.get(group) or {}).get(field)
            if spec and isinstance(spec, list):
                cfg = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
                opts = cfg.get("options")
                if isinstance(opts, list) and opts:
                    return [str(o) for o in opts]
        return []

    # 按节点 id 排序保证稳定
    for node_id in sorted(graph.keys(), key=lambda x: (len(x), x)):
        node = graph[node_id]
        class_type = str(node.get("class_type") or "")
        inputs = node.get("inputs") or {}
        if not isinstance(inputs, dict):
            continue

        if class_type in _IMAGE_OUTPUT_CLASSES:
            output_kinds.add("image")
        if class_type in _VIDEO_OUTPUT_CLASSES:
            output_kinds.add("video")

        def _add(field: str, ptype: str, label: str, **extra: Any) -> None:
            if len(params) >= _MAX_PARAMS:
                return
            params.append({
                "key": f"{node_id}_{field}",
                "label": label,
                "type": ptype,
                "nodeId": str(node_id),
                "field": field,
                "default": inputs.get(field),
                **extra,
            })

        # 提示词：全部 CLIPTextEncode 暴露；第一个标记为 prompt（上游文本注入目标）
        if class_type in _TEXT_INPUT_CLASSES:
            text_seen += 1
            label = "正向提示词" if text_seen == 1 else f"提示词（节点 {node_id}）"
            _add("text", "prompt" if text_seen == 1 else "text", label)

        # 参考图槽位
        if class_type in _IMAGE_INPUT_CLASSES:
            _add("image", "image", f"参考图（节点 {node_id}）")

        # 采样器参数
        if class_type in ("KSampler", "KSamplerAdvanced", "SamplerCustom", "KSampler (Efficient)"):
            for f in ("steps", "cfg", "sampler_name", "scheduler", "denoise"):
                if f in inputs:
                    if f in ("sampler_name", "scheduler"):
                        _add(f, "select", _FIELD_LABELS.get(f, f), options=_oi_options(class_type, f))
                    else:
                        _add(f, "number", _FIELD_LABELS.get(f, f), min=1 if f == "steps" else 0, step=0.5 if f == "cfg" else 1)
            if "seed" in inputs:
                _add("seed", "seed", "种子（留空随机）")
            if "noise_seed" in inputs:
                _add("noise_seed", "seed", "种子（留空随机）")

        # 模型加载器
        if class_type in ("CheckpointLoaderSimple", "CheckpointLoader", "unCLIPCheckpointLoader",
                          "VAELoader", "LoraLoader", "UNETLoader", "LoraLoaderModelOnly"):
            for f in _SELECT_MODEL_FIELDS:
                if f in inputs:
                    opts = _oi_options(class_type, f)
                    _add(f, "select" if opts else "text", _FIELD_LABELS.get(f, f), options=opts)
            if class_type in ("LoraLoader", "LoraLoaderModelOnly") and "strength_model" in inputs:
                _add("strength_model", "number", "LoRA 强度", min=0, max=3, step=0.05)

        # 潜空间尺寸（图片）
        if class_type == "EmptyLatentImage":
            for f in ("width", "height"):
                if f in inputs:
                    _add(f, "number", _FIELD_LABELS.get(f, f), min=64, step=64)
            if "batch_size" in inputs:
                _add("batch_size", "number", "张数", min=1, max=4, step=1)

        # 视频潜空间（帧数）
        if class_type in _VIDEO_LATENT_CLASSES:
            for f in ("length", "num_frames", "frames", "width", "height"):
                if f in inputs:
                    _add(f, "number", _FIELD_LABELS.get(f, f), min=1)

        # 视频/动图输出节点的帧率与格式
        # 核心节点用 fps（SaveVideo / SaveWEBM / SaveAnimatedWEBP），
        # 第三方 VideoHelperSuite 用 frame_rate —— 两个都要认
        if class_type in _VIDEO_OUTPUT_CLASSES or class_type in (
            "SaveAnimatedWEBP", "SaveAnimatedPNG", "CreateVideo"
        ):
            for f in ("frame_rate", "fps"):
                if f in inputs:
                    _add(f, "number", "帧率", min=1, max=120)
            if "format" in inputs:
                _add("format", "select", "输出格式", options=_oi_options(class_type, "format"))

        # 通用兜底：常见数值字段（自定义节点）
        if not any(class_type.startswith(prefix) for prefix in
                   ("KSampler", "Checkpoint", "Lora", "VAE", "LoadImage", "Save", "VHS_", "Empty")):
            for f in sorted(inputs.keys()):
                v = inputs[f]
                if f in _NUMBER_FIELDS and isinstance(v, (int, float)) and not any(p["key"] == f"{node_id}_{f}" for p in params):
                    _add(f, "number", _FIELD_LABELS.get(f, f), min=1)
                elif f in _SELECT_MODEL_FIELDS and isinstance(v, str):
                    opts = _oi_options(class_type, f)
                    if opts:
                        _add(f, "select", _FIELD_LABELS.get(f, f), options=opts)

    output_kind = "mixed" if len(output_kinds) > 1 else (next(iter(output_kinds)) if output_kinds else "image")
    return {"paramMap": params, "outputKind": output_kind, "nodeCount": len(graph)}


def apply_params(
    graph: dict[str, Any],
    param_map: list[dict[str, Any]],
    values: dict[str, Any],
    image_files: dict[str, str] | None = None,
    prompt_text: str | None = None,
) -> dict[str, Any]:
    """把表单值写回图（深拷贝，不改原对象）。

    - values：param key → 表单值；未填的保持图原值
    - image_files：param key → 已上传到 ComfyUI 的文件名（LoadImage 引用）
    - prompt_text：上游/节点文本，注入到第一个 prompt 类型参数（用户已填值时拼在其后）
    - seed 类型：值为空/None → 随机整数
    """
    import copy

    g = copy.deepcopy(graph)
    img = image_files or {}
    prompt_injected = False

    for p in param_map:
        key = p.get("key") or ""
        node_id = str(p.get("nodeId") or "")
        field = p.get("field") or ""
        node = g.get(node_id)
        if not isinstance(node, dict):
            continue
        inputs = node.setdefault("inputs", {})

        if p.get("type") == "image":
            fname = img.get(key)
            if fname:
                inputs[field] = fname
            continue

        # seed：空/未填 → 随机（否则每次跑出同一张图）；填数字 → 固定
        if p.get("type") == "seed":
            v = values.get(key)
            if v is None or str(v).strip() == "" or str(v).strip().lower() == "random":
                inputs[field] = random.randint(0, 2**32 - 1)
            else:
                try:
                    inputs[field] = int(v)
                except (TypeError, ValueError):
                    inputs[field] = random.randint(0, 2**32 - 1)
            continue

        if p.get("type") == "prompt":
            own = str(values.get(key) or "").strip()
            incoming = (prompt_text or "").strip()
            merged = "\n".join(x for x in (incoming, own) if x)
            if merged:
                inputs[field] = merged
            prompt_injected = True
            continue

        if key not in values or values[key] is None or values[key] == "":
            continue
        v = values[key]
        if p.get("type") == "number":
            try:
                inputs[field] = int(v) if float(v) == int(float(v)) else float(v)
            except (TypeError, ValueError):
                pass
        else:
            inputs[field] = v

    # 没有 prompt 类型参数的工作流（如放大/转格式），把文本塞进第一个 text 字段兜底
    if prompt_text and not prompt_injected:
        for p in param_map:
            if p.get("type") == "text":
                g[str(p.get("nodeId"))]["inputs"][p.get("field") or ""] = (
                    f"{prompt_text}\n{g[str(p.get('nodeId'))]['inputs'].get(p.get('field') or '', '')}".strip()
                )
                break
    return g


# ---------- 数据库 CRUD ----------


async def create_workflow(
    db: AsyncSession,
    *,
    name: str,
    provider_id: int,
    graph: dict[str, Any],
    object_info: dict | None = None,
) -> ComfyWorkflow:
    parsed = parse_workflow(graph, object_info)
    row = ComfyWorkflow(
        name=name.strip() or "未命名工作流",
        provider_id=provider_id,
        graph_json=json.dumps(graph, ensure_ascii=False),
        param_map_json=json.dumps(parsed["paramMap"], ensure_ascii=False),
        output_kind=parsed["outputKind"],
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def list_workflows(db: AsyncSession, provider_id: int | None = None) -> list[ComfyWorkflow]:
    q = select(ComfyWorkflow).order_by(ComfyWorkflow.id.desc())
    if provider_id:
        q = q.where(ComfyWorkflow.provider_id == provider_id)
    rows = await db.execute(q)
    return list(rows.scalars().all())


async def refresh_workflow(db: AsyncSession, row: ComfyWorkflow, object_info: dict | None = None) -> ComfyWorkflow:
    """重新解析（ComfyUI 上线后补 select options）。"""
    graph = json.loads(row.graph_json)
    parsed = parse_workflow(graph, object_info)
    row.param_map_json = json.dumps(parsed["paramMap"], ensure_ascii=False)
    row.output_kind = parsed["outputKind"]
    await db.commit()
    await db.refresh(row)
    return row


def workflow_out(row: ComfyWorkflow) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "providerId": row.provider_id,
        "outputKind": row.output_kind,
        "paramMap": json.loads(row.param_map_json or "[]"),
        "nodeCount": len(json.loads(row.graph_json or "{}")),
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


async def find_comfy_provider(db: AsyncSession, provider_id: int) -> ProviderService:
    row = await db.get(ProviderService, provider_id)
    if row is None:
        raise ValueError("ComfyUI 服务不存在，可能已被删除")
    if row.kind != "comfyui":
        raise ValueError("所选服务不是 ComfyUI 类型")
    return row
