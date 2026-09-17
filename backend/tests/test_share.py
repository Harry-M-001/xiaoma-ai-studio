"""社区分享（#20，直接 python 运行）。

运行：venv/Scripts/python tests/test_share.py

分享这件事只有两个失败模式值得钉死，两条都是「在别人的机器上才暴露」的类型：

1. **别人的机器上读不了 / 跑不了**。所以 `schemaVersion`（能不能读）与 `requires`
   （缺不缺节点类型与模型）必须真的起作用，而且要**按导入方自己的实情**判断，
   不能照抄分享方的声明——对方声明得再全，也管不了你我版本不同这件事。
2. **把不该带的东西带出去**。快照里只该有「怎么做」：节点拓扑、节点要求与参数。
   本机的模型编号（`2:deepseek-chat` 在别人机器上指向另一个服务）、产出的图片视频、
   任务记录都不该在里面。

另外钉一条边界：**导入只读不写**——它只给结论，落不落由用户决定。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.registry.canvas_nodes import CANVAS_SCHEMA_VERSION, NODE_SCHEMAS  # noqa: E402
from app.services import share_service as ss  # noqa: E402


def _doc() -> dict:
    """一条 L4 形状的画布：两个来源汇到 video，最能体现「拓扑不是一条直线」。"""
    return {
        "schemaVersion": CANVAS_SCHEMA_VERSION,
        "nodes": [
            {"id": "a", "type": "idea", "position": {"x": 0, "y": 0},
             "data": {"prompt": "写个创意", "model_key": "2:deepseek-chat", "status": "completed",
                      "schema": {"kind": "idea"}}},
            {"id": "b", "type": "storyboard", "position": {"x": 300, "y": 0},
             "data": {"prompt": "做分镜", "shotCount": 8, "styleKey": "wkw"}},
            {"id": "c", "type": "storyboardImage", "position": {"x": 600, "y": 0},
             "data": {"prompt": "逐镜出图", "size": "864x1152", "n": 1,
                      "refImages": [{"id": 7, "url": "/media/x.png"}]}},
            {"id": "d", "type": "video", "position": {"x": 900, "y": 0},
             "data": {"prompt": "出片", "shotVideo": "chain", "mode": "first_last"}},
        ],
        "edges": [
            {"id": "e1", "source": "a", "target": "b", "sourceHandle": "out-text",
             "targetHandle": "in-text"},
            {"id": "e2", "source": "b", "target": "c", "sourceHandle": "out-text",
             "targetHandle": "in-any"},
            {"id": "e3", "source": "c", "target": "d", "sourceHandle": "out-image",
             "targetHandle": "in-any"},
        ],
    }


def _export(**kw) -> dict:
    base = dict(title="竖屏短剧流水线", description="三幕结构，逐镜出片", author="小明")
    base.update(kw)
    return ss.export_canvas(_doc(), app_version="1.1.7", **base)


# ============================================================
# 一、快照的内容：该有的都在，不该有的一律没有
# ============================================================


def test_snapshot_carries_the_three_declared_fields():
    snap = _export()
    assert snap["format"] == "xiaoma-share"
    assert snap["schemaVersion"] == ss.SHARE_SCHEMA_VERSION
    assert snap["kind"] == "canvas"
    assert snap["license"] == ss.DEFAULT_LICENSE
    assert snap["appVersion"] == "1.1.7"
    assert snap["title"] == "竖屏短剧流水线"
    assert snap["createdAt"]
    # requires 的三件事：节点类型、能力、画布版本
    assert set(snap["requires"]) == {"nodeKinds", "modalities", "canvasSchemaVersion"}
    assert snap["requires"]["nodeKinds"] == ["idea", "storyboard", "storyboardImage", "video"]
    assert snap["requires"]["modalities"] == ["text", "image", "video"]


def test_snapshot_keeps_the_node_requirements_and_params():
    """分享的核心价值是「怎么做」：节点要求与参数必须在。"""
    nodes = {n["id"]: n for n in _export()["payload"]["nodes"]}
    assert nodes["a"]["data"]["prompt"] == "写个创意"
    assert nodes["b"]["data"]["shotCount"] == 8
    assert nodes["b"]["data"]["styleKey"] == "wkw"
    assert nodes["d"]["data"]["shotVideo"] == "chain"


def test_snapshot_never_carries_local_model_ids():
    """`2:deepseek-chat` 在别人机器上指向的是另一个服务，带出去只会造成误解。"""
    for n in _export()["payload"]["nodes"]:
        assert "model_key" not in n["data"], n


def test_snapshot_never_carries_assets_tasks_or_runtime_caches():
    """节点 data 里只该留下「怎么做」：运行时缓存与本机资产都不带。

    按 key 判断而不是搜整段文本：`schemaVersion` 这种合法字段名里也含 "schema"，
    搜字符串会把正常字段一起误判掉。
    """
    snap = _export()
    forbidden = {"refImages", "status", "schema", "task_id", "assetId", "workflowId"}
    for n in snap["payload"]["nodes"]:
        hit = forbidden & set(n["data"])
        assert not hit, f"节点 {n['id']} 的 data 里带了 {hit}"

    body = json.dumps(snap, ensure_ascii=False)
    for leaked in ("/media/", "task_id", "assetId"):
        assert leaked not in body, f"快照里出现了 {leaked}"


def test_export_refuses_a_broken_canvas():
    """自己都打不开的东西不要分享出去。"""
    bad = {"nodes": [{"id": "a", "type": "imaginary", "position": {"x": 0, "y": 0}}], "edges": []}
    try:
        ss.export_canvas(bad, title="x")
    except ValueError as e:
        assert "未知节点类型" in str(e)
        return
    raise AssertionError("坏画布竟然可以导出")


def test_unknown_license_falls_back_to_the_default():
    snap = _export(license_key="我自己编的许可")
    assert snap["license"] == ss.DEFAULT_LICENSE


# ============================================================
# 二、分享码
# ============================================================


def test_share_code_round_trips():
    snap = _export()
    code = ss.encode_code(snap)
    assert code.startswith(ss.CODE_PREFIX)
    assert ss.parse_share(code) == snap


def test_share_code_is_short_enough_to_paste():
    """分享码存在的唯一理由就是能贴进聊天窗口，太长就没意义了。"""
    code = ss.encode_code(_export())
    assert len(code) < 2000, f"分享码 {len(code)} 字符，太长了"


def test_share_code_is_compressed_not_just_base64():
    """不压缩的话 base64 会把体积撑到 1.33 倍，中文画布很容易就贴不下了。"""
    snap = _export()
    raw = len(json.dumps(snap, ensure_ascii=False).encode("utf-8"))
    assert len(ss.encode_code(snap)) < raw, "分享码没有比原文更短，说明没在压缩"


def test_broken_code_says_what_is_wrong():
    for bad, keyword in (
        ("XMS1:!!!!", "损坏"),
        ("XMS1:", "损坏"),
        ("hello world", "不是分享码"),
        ("{'a':1}", "JSON"),
    ):
        try:
            ss.parse_share(bad)
        except ValueError as e:
            assert keyword in str(e), f"{bad!r} 的报错里应当提到「{keyword}」：{e}"
        else:
            raise AssertionError(f"{bad!r} 竟然解析成功了")


def test_parse_accepts_a_plain_json_file_body():
    """从文件导入时贴的是 JSON 原文，不能只认分享码。"""
    snap = _export()
    assert ss.parse_share(json.dumps(snap, ensure_ascii=False)) == snap


# ============================================================
# 三、导入：能不能读、缺什么
# ============================================================


def _import(snap, **kw) -> ss.Imported:
    return ss.import_share(json.dumps(snap, ensure_ascii=False), **kw)


def test_clean_import_reports_everything_the_receiver_needs_to_know():
    r = _import(_export())
    assert r.ok and not r.errors
    assert r.title == "竖屏短剧流水线"
    assert r.author == "小明"
    assert r.license == ss.DEFAULT_LICENSE
    assert r.missingNodeKinds == []
    assert [n["id"] for n in r.doc["nodes"]] == ["a", "b", "c", "d"]
    assert len(r.doc["edges"]) == 3


def test_unsupported_version_says_upgrade_instead_of_failing_obscurely():
    snap = _export()
    snap["schemaVersion"] = 99
    r = _import(snap)
    assert not r.ok
    assert any("99" in e and "升级" in e for e in r.errors), r.errors


def test_wrong_format_is_rejected_by_name():
    r = _import({"format": "someone-elses-thing", "schemaVersion": 1})
    assert not r.ok
    assert any("小马AI工坊" in e for e in r.errors), r.errors


def test_unsupported_kind_is_rejected_clearly():
    """给别人留的口子：将来加了 kind=prompt，老版本要能明说「不支持这个」而不是崩。"""
    snap = _export()
    snap["kind"] = "prompt"
    r = _import(snap)
    assert not r.ok
    assert any("画布" in e and "prompt" in e for e in r.errors), r.errors


def test_missing_node_kinds_are_reported_as_an_error():
    """对方版本旧、还没有这个节点类型：必须当错误说清，否则导入进来是跑不动的空壳。"""
    r = _import(_export(), local_node_kinds={"idea", "storyboard", "storyboardImage"})
    assert not r.ok
    assert r.missingNodeKinds == ["video"]
    assert any("video" in e for e in r.errors), r.errors


def test_missing_models_is_a_warning_not_an_error():
    """缺模型不该拦住导入：先导入、之后接入模型再跑，完全合理。"""
    r = _import(_export(), local_modalities={"text"})
    assert r.ok, r.errors
    assert r.missingModalities == ["image", "video"]
    assert any("图片" in w and "视频" in w for w in r.warnings), r.warnings


def test_missing_requirements_are_computed_from_the_local_side():
    """对方声明得再全也代替不了「我这边有什么」。"""
    snap = _export()
    snap["requires"] = {"nodeKinds": [], "modalities": []}   # 作者声明得干干净净
    r = _import(snap, local_node_kinds={"idea"}, local_modalities=set())
    assert r.missingNodeKinds, "不该相信分享方的声明"
    assert r.missingModalities == ["text", "image", "video"]


def test_undeclared_license_is_flagged():
    snap = _export(license_key="") or {}
    snap["license"] = ""
    r = _import(snap)
    assert r.ok
    assert any("没有声明授权范围" in w for w in r.warnings), r.warnings


def test_a_tampered_snapshot_fails_loudly_not_silently():
    """有人在分享码里手动改了连线（引用不存在的节点）：要报错，不要猜着修。"""
    snap = _export()
    snap["payload"]["edges"].append(
        {"id": "e9", "source": "a", "target": "不存在的节点"}
    )
    r = _import(snap)
    assert not r.ok
    assert any("不合法" in e for e in r.errors), r.errors


def test_import_returns_a_doc_that_can_be_saved_as_is():
    """导入结果的形状必须与画布保存接口认的一致，否则用户点「导入」之后还要再修一遍。"""
    r = _import(_export())
    doc = r.doc
    assert doc["schemaVersion"] == CANVAS_SCHEMA_VERSION
    from app.registry.canvas_nodes import validate_document

    nodes, edges = validate_document(doc)
    assert len(nodes) == 4 and len(edges) == 3


def test_import_is_read_only():
    """导入只给结论；这个模块不该有任何落库动作。"""
    src = (BACKEND / "app" / "services" / "share_service.py").read_text(encoding="utf-8")
    for forbidden in ("db.add", "commit(", "SessionLocal", "save_bytes"):
        assert forbidden not in src, f"share_service 里出现了 {forbidden}，它应当是纯函数"


def test_every_license_option_has_a_human_readable_label():
    """授权范围要用人话解释：只写一个 SPDX 标识，用户判断不了自己能拿它做什么。"""
    for key, label in ss.LICENSES.items():
        assert key and label and len(label) >= 6, (key, label)
    assert ss.DEFAULT_LICENSE in ss.LICENSES


def test_licenses_cover_the_common_intents():
    """四档要能覆盖「跟我一样 / 可改 / 随便用 / 只许看」这四种常见意图。"""
    joined = " ".join(ss.LICENSES.values())
    for intent in ("商用", "修改", "随便用", "仅供查看"):
        assert intent in joined, f"授权档位里没有覆盖「{intent}」"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001  一个用例炸了别把剩下的都带下去
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
