"""画布节点契约（对齐星云创 TapCanvas 的声明式设计）。

每个节点类型只声明三件事：
- features：声明该节点支持哪些能力，属性面板按此动态渲染
  （prompt / modelSelect / videoMode / imageUpload / imageSize / sampleCount / duration / ratio）
- handles：端口契约，type ∈ text|image|video|any；any 兼容一切
- kind/category/label：分组与展示

节点语义约定（2026-09-15 契约 v2）：
- image = 生成 + 编辑合一：无参考图时文生图；接上游图片/选参考图后自动图生图（编辑）
- video 按模式工作（node.data.mode）：
  - text2video 文生视频：只消费文本（上游媒体连线被忽略）
  - first_last 首尾帧：首帧+尾帧两个图片位（refImages[0]=首帧, [1]=尾帧；上游图片依次填充）
  - omni_ref 全能参考：上游/参考的图片、视频全部传入（视频编辑、多模态参考类模型）
- 视频产物原样传下游（不做抽帧转换）

前端属性面板、连线校验全部由这份契约驱动；新增节点类型 = 在此注册一条。
"""

from __future__ import annotations

HANDLE_TYPES = ("text", "image", "video", "any")

VIDEO_MODES = {
    "text2video": "文生视频",
    "first_last": "首尾帧",
    "omni_ref": "全能参考",
}

# 旧契约类型 → 新类型的归一映射（存量画布文档兼容）
LEGACY_TYPE_ALIASES = {"imageEdit": "image"}

NODE_SCHEMAS: dict[str, dict] = {
    "text": {
        "kind": "text",
        "category": "document",
        "label": "文本",
        "description": "提示词/文案素材；自身为空时继承上游文本（链式改写）",
        "features": ["prompt"],
        "handles": {
            "targets": [{"id": "in-text", "type": "text"}],
            "sources": [{"id": "out-text", "type": "text"}],
        },
    },
    # ---- 自动链：文档生成节点（需要调用 LLM，产出 Markdown 正文） ----
    # 内容层全是 Markdown：LLM 不丢正文、用户可直接手改、天然按节分块
    "idea": {
        "kind": "idea",
        "category": "document",
        "label": "创意策划",
        "description": "一句话想法 → 创意简报（卖点/主角/冲突/三幕/视觉基调）",
        "features": ["prompt", "modelSelect"],
        "promptLabel": "你的想法",
        "promptPlaceholder": "一句话说清想拍什么，例如：外卖小哥其实是隐退的顶级保镖",
        "handles": {
            "targets": [{"id": "in-text", "type": "text"}],
            "sources": [{"id": "out-text", "type": "text"}],
        },
    },
    "novel": {
        "kind": "novel",
        "category": "document",
        "label": "小说",
        "description": "创意简报 → 小说正文（先出章节大纲，再逐章续写）",
        "features": ["prompt", "modelSelect", "chapterCount", "styleSelect"],
        "promptLabel": "补充要求",
        "promptPlaceholder": "可选：人称、文风、必须出现的情节",
        "handles": {
            "targets": [{"id": "in-text", "type": "text"}],
            "sources": [{"id": "out-text", "type": "text"}],
        },
    },
    "script": {
        "kind": "script",
        "category": "document",
        "label": "剧本",
        "description": "小说 → 场次剧本（只写戏不写镜头，镜头语言留给分镜）",
        "features": ["prompt", "modelSelect", "sceneCount", "styleSelect"],
        "promptLabel": "补充要求",
        "promptPlaceholder": "可选：目标集数、必须保留的台词、删减方向",
        "handles": {
            "targets": [{"id": "in-text", "type": "text"}],
            "sources": [{"id": "out-text", "type": "text"}],
        },
    },
    "storyboard": {
        "kind": "storyboard",
        "category": "document",
        "label": "分镜",
        "description": "剧本 → 镜头表（景别/运镜/情绪外化/首帧英文提示词）",
        "features": ["prompt", "modelSelect", "shotCount", "styleSelect"],
        "promptLabel": "补充要求",
        "promptPlaceholder": "可选：整体影调、必须出现的画面、参考片风格",
        "handles": {
            "targets": [{"id": "in-text", "type": "text"}],
            "sources": [{"id": "out-text", "type": "text"}],
        },
    },
    "assetSheet": {
        "kind": "assetSheet",
        "category": "document",
        "label": "资产表",
        "description": "剧本/分镜 → 资产表（角色/场景/道具 + 英文视觉描述 + 出现场次），供下游生成设定图",
        "features": ["prompt", "modelSelect"],
        "promptLabel": "补充要求",
        "promptPlaceholder": "可选：统一画风词、必须保留的角色、道具细节要求",
        "handles": {
            "targets": [{"id": "in-text", "type": "text"}],
            "sources": [{"id": "out-text", "type": "text"}],
        },
    },
    "image": {
        "kind": "image",
        "category": "image",
        "label": "图片生成",
        "description": "文生图；接上游图片或选参考图后自动变为图生图（编辑）",
        "features": ["prompt", "modelSelect", "imageUpload", "imageSize", "sampleCount"],
        "handles": {
            "targets": [{"id": "in-any", "type": "any"}],
            "sources": [{"id": "out-image", "type": "image"}],
        },
    },
    # ---- 资产链：资产表 → 资产设定图（B 期） ----
    # 资产名落进资产库后，下游节点提示词里提到名字就自动挂参考图（角色提及注入）
    "assetImage": {
        "kind": "assetImage",
        "category": "image",
        "label": "资产设定图",
        "description": "读上游资产表逐行生成设定图（角色=三视图+特写；场景=广角空镜），存入资产库供下游按名自动引用",
        "features": ["prompt", "modelSelect", "assetScope", "imageSize", "sampleCount"],
        "promptLabel": "统一风格（可选）",
        "promptPlaceholder": "可选：统一画风词，如 anime style, cel shading, bright colors",
        "handles": {
            "targets": [{"id": "in-text", "type": "text"}],
            "sources": [{"id": "out-image", "type": "image"}],
        },
    },
    "video": {
        "kind": "video",
        "category": "video",
        "label": "视频生成",
        "description": "按模式工作：文生视频 / 首尾帧 / 全能参考（视频编辑）",
        "features": ["prompt", "modelSelect", "videoMode", "imageUpload", "duration", "ratio", "styleSelect"],
        "handles": {
            "targets": [{"id": "in-any", "type": "any"}],
            "sources": [{"id": "out-video", "type": "video"}],
        },
    },
    # ---- 分镜图：读分镜表逐镜出图（C 期） ----
    # 每镜用它自己的首帧提示词，上游纯图片不参与（要的是"这一镜的角色"），
    # 参考图 = 节点上手动选的 + 该镜文本里提到的资产设定图。
    "storyboardImage": {
        "kind": "storyboardImage",
        "category": "image",
        "label": "分镜图",
        "description": "读上游分镜表逐镜出图（用每镜的首帧提示词，自动追加风格词）；提到资产名会自动挂设定图",
        "features": ["prompt", "modelSelect", "styleSelect", "imageSize", "sampleCount", "shotLimit"],
        "promptLabel": "补充要求",
        "promptPlaceholder": "可选：每镜都必须出现的元素、统一的画面要求（风格请用上面的风格卡）",
        "handles": {
            "targets": [{"id": "in-any", "type": "any"}],
            "sources": [{"id": "out-image", "type": "image"}],
        },
    },
    "workflow": {
        "kind": "workflow",
        "category": "tool",
        "label": "ComfyUI 工作流",
        "description": "上传 workflow_api.json 后按参数表调分辨率/帧数/步数等执行；产物类型由工作流决定",
        "features": ["prompt", "workflowSelect", "imageUpload"],
        "handles": {
            "targets": [{"id": "in-any", "type": "any"}],
            "sources": [{"id": "out-any", "type": "any"}],
        },
    },
}

CANVAS_SCHEMA_VERSION = 1

# 文档生成节点（自动链）：需要调用 LLM 写正文，产物是 Markdown 文档
DOC_NODE_KINDS = ("idea", "novel", "script", "storyboard", "assetSheet")

# 纯素材节点：不执行、不产生任务，只承载文本供下游取用
PASSIVE_NODE_KINDS = ("text",)

# 资产链节点：逐行批量产生图片任务（一个节点 → N 个任务）
ASSET_IMAGE_KINDS = ("assetImage",)

# 分镜图节点：逐镜批量产生图片任务（同样是 1 个节点 → N 个任务）
STORYBOARD_IMAGE_KINDS = ("storyboardImage",)


def is_doc_kind(ntype: str) -> bool:
    return normalize_type(ntype) in DOC_NODE_KINDS


def is_asset_image(ntype: str) -> bool:
    return normalize_type(ntype) in ASSET_IMAGE_KINDS


def is_storyboard_image(ntype: str) -> bool:
    return normalize_type(ntype) in STORYBOARD_IMAGE_KINDS


def is_batch_image(ntype: str) -> bool:
    """会「一个节点派发 N 个任务」的节点类型。"""
    return is_asset_image(ntype) or is_storyboard_image(ntype)


def is_runnable(ntype: str) -> bool:
    """需要执行（会产生任务）的节点类型。"""
    return normalize_type(ntype) not in PASSIVE_NODE_KINDS


def normalize_type(ntype: str) -> str:
    """旧类型归一（imageEdit → image）。"""
    return LEGACY_TYPE_ALIASES.get(ntype, ntype)


def validate_document(doc: dict) -> list[dict]:
    """校验画布文档结构，返回标准化后的 nodes/edges；不合法抛 ValueError。

    归一动作：旧类型（imageEdit）映射为新类型；video 缺省 mode 补 text2video。
    """
    if not isinstance(doc, dict):
        raise ValueError("画布文档必须是 JSON 对象")
    if doc.get("schemaVersion", 1) != CANVAS_SCHEMA_VERSION:
        raise ValueError(f"不支持的画布版本：{doc.get('schemaVersion')}")

    raw_nodes = doc.get("nodes", [])
    edges = doc.get("edges", [])
    if not isinstance(raw_nodes, list) or not isinstance(edges, list):
        raise ValueError("nodes / edges 必须是数组")

    nodes: list[dict] = []
    node_ids: set[str] = set()
    for n in raw_nodes:
        if not isinstance(n, dict):
            raise ValueError("节点必须是 JSON 对象")
        nid = n.get("id")
        if not nid or not isinstance(nid, str):
            raise ValueError("节点缺少 id")
        if nid in node_ids:
            raise ValueError(f"节点 id 重复：{nid}")
        ntype = normalize_type(n.get("type"))
        if ntype not in NODE_SCHEMAS:
            raise ValueError(f"未知节点类型：{n.get('type')}")
        pos = n.get("position") or {}
        if not isinstance(pos.get("x"), (int, float)) or not isinstance(pos.get("y"), (int, float)):
            raise ValueError(f"节点 {nid} 缺少 position")
        data = n.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        if ntype == "video" and data.get("mode") not in VIDEO_MODES:
            data = {**data, "mode": "text2video"}
        nodes.append({**n, "type": ntype, "data": data})
        node_ids.add(nid)

    edge_keys: set[tuple] = set()
    for e in edges:
        if e.get("source") not in node_ids or e.get("target") not in node_ids:
            raise ValueError(f"连线引用了不存在的节点：{e.get('id')}")
        if e.get("source") == e.get("target"):
            raise ValueError("不允许自连接")
        key = (e.get("source"), e.get("sourceHandle"), e.get("target"), e.get("targetHandle"))
        if key in edge_keys:
            raise ValueError("重复连线")
        edge_keys.add(key)

    # 环检测（DFS）
    graph: dict[str, list[str]] = {}
    for e in edges:
        graph.setdefault(e["source"], []).append(e["target"])
    state: dict[str, int] = {}

    def dfs(u: str) -> None:
        state[u] = 1
        for v in graph.get(u, []):
            if state.get(v) == 1:
                raise ValueError("画布中存在环，请检查连线")
            if state.get(v, 0) == 0:
                dfs(v)
        state[u] = 2

    for nid in node_ids:
        if state.get(nid, 0) == 0:
            dfs(nid)

    return nodes, edges
