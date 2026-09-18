"""#29 静图样片（前端接线）的静态检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_frontend_animatic.py

后端把缓动算得再准，前端接错一处就白做。这里守四类最容易错的地方：

1. **先存再出片**：出片读的是库里的画布，不先保存就会拿旧设置、旧分镜表去渲染；
2. **样片要活过一次刷新**：产物地址写回节点（不是只放临时 state），否则用户看一眼
   刷新一下就没了，只能重新渲染一条；
3. **少了哪几镜要说**：`skipped` / `truncated` 不显示，用户会拿一条缺镜的样片判断节奏；
4. **产物被删要给一句人话**：`<video>` 加载失败时不处理，只会留下一个点不动的播放器。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
CANVAS = SRC / "pages" / "CanvasPage.tsx"
API = SRC / "api.ts"
TYPES = SRC / "types.ts"
STYLES = SRC / "styles.css"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.registry.canvas_nodes import NODE_SCHEMAS  # noqa: E402


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_sample_saves_the_canvas_first():
    """出片前必须先保存：后端读库，不存就会拿旧分镜表/旧设置去渲染。"""
    src = _text(CANVAS)
    start = src.index("const renderAnimatic = useCallback")
    body = src[start : src.index("\n  );", start)]
    assert "await saveDoc()" in body, "出样片没有先保存画布"
    assert "if (dirty && !(await saveDoc())) return null;" in body, (
        "保存失败还继续出片（会拿一份和屏幕上不一致的分镜表去渲染）"
    )
    assert "api.renderAnimatic(projectId, nodeId)" in body, "没有真的调接口"


def test_the_sample_reference_is_written_back_to_the_node():
    """产物地址要落在节点数据上，刷新页面后那条片子还在。"""
    src = _text(CANVAS)
    assert "sampleAssetId: asset.id, sampleAssetUrl: asset.url" in src, (
        "样片地址没有写回节点——刷新一下就得重新渲染一条"
    )
    # 面板必须从节点数据取地址，而不是只认本地 state
    section = src[src.index("function AnimaticSection") : src.index("function RunConfirmDialog")]
    assert 'String(data.sampleAssetUrl ?? "")' in section, "播放器没有从节点数据取地址"

    types = _text(TYPES)
    for field in ("sampleAssetId", "sampleAssetUrl", "sampleRatio", "sampleRes",
                  "sampleShotSeconds", "sampleDefaultMove"):
        assert field in types, f"节点数据类型里没有 {field}"


def test_every_setting_goes_through_update_node():
    """四个设置都要写回节点（不然调完一刷新就回去了）。"""
    src = _text(CANVAS)
    section = src[src.index("function AnimaticSection") : src.index("function RunConfirmDialog")]
    for key in ("sampleRatio", "sampleRes", "sampleShotSeconds", "sampleDefaultMove"):
        assert f"{key}:" in section, f"面板里没有 {key} 这个设置"
    assert section.count("ctx?.updateNode(id,") >= 4, "设置没有通过 updateNode 落到节点上"


def test_skipped_and_truncated_shots_are_shown():
    """缺了哪几镜、为什么缺，必须摆在面板上。"""
    src = _text(CANVAS)
    section = src[src.index("function AnimaticSection") : src.index("function RunConfirmDialog")]
    assert "report?.skipped" in section and "report?.truncated" in section, (
        "报告里缺镜的信息没读到"
    )
    assert "skipped.length > 0" in section and "truncated.length > 0" in section, (
        "缺镜信息有值却没渲染出来"
    )
    assert "跳过：" in section, "没有把跳过的镜头列出来"
    assert "没收录" in section, "没有把被截断的镜头列出来"
    assert "report.maxSeconds" in section, (
        "截断说明里用错了上限值——上限来自报告，不该在前端另写一个数"
    )
    assert "const MAX_TOTAL" not in section and "120" not in section, (
        "前端自己硬编码了时长上限（那是后端的常量，改一处就得改两处）"
    )


def test_a_deleted_asset_gives_a_sentence_not_a_dead_player():
    """产物被清理掉时给一句人话，而不是一个点不动的播放器。"""
    src = _text(CANVAS)
    section = src[src.index("function AnimaticSection") : src.index("function RunConfirmDialog")]
    assert "onError={() => setBroken(true)}" in section, "播放器没有处理加载失败"
    assert "不在了" in section, "加载失败时没有给一句解释"


def test_only_the_storyboard_image_node_offers_it():
    """只有分镜图节点该有这个入口：它是唯一「一镜一张图」的节点。

    后端注册表与前端 `features` 必须说的是同一件事——注册表里没开，
    前端多画一个按钮就会点出一个 400。
    """
    owners = [
        kind
        for kind, schema in NODE_SCHEMAS.items()
        if "animatic" in (schema.get("features") or [])
    ]
    assert owners == ["storyboardImage"], f"开了样片入口的节点是 {owners}，应当只有分镜图"

    src = _text(CANVAS)
    assert 'features.includes("animatic") && <AnimaticSection' in src, (
        "面板没有按 features 开关渲染样片区块"
    )


def test_api_path_matches_the_backend_route():
    """接口路径与后端路由要一致（前端拼错只有点下去才知道）。"""
    api = _text(API)
    assert "`/api/canvas/${projectId}/nodes/${encodeURIComponent(nodeId)}/animatic`" in api, (
        "api.ts 里的样片路径不对"
    )
    assert "renderAnimatic: (projectId: number, nodeId: string)" in api
    router = _text(Path(__file__).resolve().parent.parent / "app" / "routers" / "canvas.py")
    assert '@router.post("/{project_id}/nodes/{node_id}/animatic")' in router, (
        "后端没有这个路由"
    )
    assert "canvas_runner.AnimaticBusy" in router, (
        "没有把「正在渲染上一条」映射成一个明确的状态码（会变成 500）"
    )


def test_frontend_type_matches_the_report_shape():
    """报告字段名是前后端的契约：后端怎么给，前端怎么读。"""
    types = _text(TYPES)
    for field in ("shots", "seconds", "skipped", "truncated", "maxSeconds", "size", "fps", "bytes"):
        assert field in types, f"前端类型里没有 {field}"

    animatic = _text(Path(__file__).resolve().parent.parent / "app" / "services" / "animatic.py")
    keys = set(re.findall(r'"(\w+)":', animatic[animatic.index("def to_report") :]))
    keys |= {"size", "fps", "bytes"}  # 这三个由 canvas_runner 补上
    missing = {"shots", "seconds", "skipped", "truncated", "maxSeconds"} - keys
    assert not missing, f"后端报告里少了 {missing}"


def test_styles_exist():
    """新加的类名要有样式，不然区块会挤成一坨。"""
    css = _text(STYLES)
    for cls in (".canvas-sample", ".canvas-sample-opts", ".canvas-sample-report",
                ".canvas-sample-video", ".canvas-sample-tag"):
        assert cls in css, f"styles.css 里没有 {cls}"


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
