"""#32 产物版本栈的回归测试（直接 python 运行）。

运行：venv/Scripts/python tests/test_canvas_versions.py

守的核心是一件事：**「这个节点现在交付哪一批产物」只有一处判断**。
以前它在两个地方各写了一遍（`_latest_task_assets` 与画布 `/status`），
加版本时如果还那样，迟早出现「画布上显示的是新版、下游读的是旧版」。

四块：
1. 版本分组与选择的纯口径（`version_key_of` / `group_versions` / `pick_version`）；
2. 节点实际交付哪一版（`_latest_task_assets` + `version_pin_of`）；
3. 版本列表服务（`node_versions`）如实回哪些字段；
4. 单节点运行与整图运行对「回滚」的不同口径（`_resolve_upstream` 的 `use_pins`）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import Asset, Base, Task  # noqa: E402
from app.services import canvas_runner  # noqa: E402

logging.getLogger("app.services.canvas_runner").setLevel(logging.CRITICAL)

NODE = "img1"


# ---------- 1. 纯口径 ----------


def _task(tid: int, batch: str = "", node: str = NODE, project: int = 1) -> Task:
    """造一个「查出来的任务」（不落库）：只要 id / node / params，够分组用。"""
    t = Task(
        kind="image",
        status="completed",
        model="m",
        prompt="p",
        params_json=json.dumps({"asset_batch": batch} if batch else {}),
        canvas_project_id=project,
        canvas_node_id=node,
    )
    t.id = tid
    return t


def test_a_task_without_a_batch_is_its_own_version():
    """老数据没有批次标记：一个任务就是一版——那正是它们本来的样子。"""
    assert canvas_runner.version_key_of(_task(7, "")) == "t7"
    assert canvas_runner.version_key_of(_task(7, "b1")) == "b1"


def test_versions_are_numbered_from_the_oldest():
    """「第 1 版」必须是最早那次生成：用户嘴里说的第 2 版就是这个号。"""
    groups = canvas_runner.group_versions([_task(9, "c"), _task(5, "b"), _task(1, "a")])
    assert [g["key"] for g in groups] == ["c", "b", "a"]  # 新的在前
    assert [g["index"] for g in groups] == [3, 2, 1]
    assert [g["latest"] for g in groups] == [True, False, False]
    assert all(g["total"] == 3 for g in groups)


def test_one_run_of_a_batch_node_is_one_version():
    """一次运行派的 N 个任务（同一个批次）只是一版，不是 N 版。"""
    groups = canvas_runner.group_versions([_task(3, "a"), _task(2, "a"), _task(1, "b")])
    assert len(groups) == 2
    assert groups[0]["taskCount"] == 2
    assert groups[0]["index"] == 2


def test_a_retried_task_stays_in_the_same_version():
    """「重新生成」是把同一版里失败的那条补上，不该自成一版。

    重跑走的是「按参数快照新建一条任务」，`asset_batch` 会被一起带过去
    （见 `test_task_retry.py` 的参数快照断言），所以它落回原来那一版。
    """
    groups = canvas_runner.group_versions([_task(8, "a"), _task(4, "a")])
    assert len(groups) == 1 and groups[0]["taskCount"] == 2


def test_no_pin_means_the_latest_version():
    groups = canvas_runner.group_versions([_task(9, "c"), _task(5, "b")])
    assert canvas_runner.pick_version(groups, "")["key"] == "c"
    assert canvas_runner.pick_version(groups, None)["key"] == "c"


def test_a_pin_selects_that_version():
    groups = canvas_runner.group_versions([_task(9, "c"), _task(5, "b")])
    assert canvas_runner.pick_version(groups, "b")["key"] == "b"


def test_a_pin_that_points_nowhere_falls_back_to_the_latest():
    """指不到任何一版时退回最新，而不是报错。

    那多半是分享码导入到别人机器上、或历史任务被清理了——为这个让整条链失败没有好处。
    但节点上会标出「当前交付的是第 N 版」，用户不会以为拿到的是最新那版。
    """
    groups = canvas_runner.group_versions([_task(9, "c")])
    assert canvas_runner.pick_version(groups, "早就没了")["key"] == "c"
    assert canvas_runner.pick_version([], "c") is None


def test_the_pin_is_read_from_the_node_data():
    assert canvas_runner.version_pin_of({"data": {}}) == ""
    assert canvas_runner.version_pin_of({"data": {"versionKey": None}}) == ""
    assert canvas_runner.version_pin_of({"data": {"versionKey": "  b  "}}) == "b"


# ---------- 2. 节点实际交付哪一版 ----------


def _run(fn) -> None:
    async def main() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            await fn(db)
        await engine.dispose()

    asyncio.run(main())


async def _make_version(db, batch: str, names: list[str], *, status: str = "completed") -> Task:
    """造一版产物：一个任务 + 它名下的若干资产。"""
    task = Task(
        kind="image",
        status=status,
        service_id=1,
        model="m",
        prompt="p",
        params_json=json.dumps({"asset_batch": batch}),
        canvas_project_id=1,
        canvas_node_id=NODE,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    for name in names:
        db.add(
            Asset(
                kind="image",
                filename=f"{batch}-{name}.png",
                original_name=f"{name}.png",
                content_type="image/png",
                size=1,
                task_id=task.id,
                name=name,
            )
        )
    await db.commit()
    return task


def test_the_node_delivers_the_latest_version_by_default():
    async def body(db) -> None:
        await _make_version(db, "a", ["v1a", "v1b"])
        await _make_version(db, "b", ["v2a"])
        assets = await canvas_runner._latest_task_assets(db, 1, NODE)
        assert [a.name for a in assets] == ["v2a"], "默认应当给最新那一版"

    _run(body)


def test_a_rollback_makes_the_node_deliver_the_old_version():
    """这是「回滚」真正生效的地方：下游/预览/体检/样片都从这里取产物。"""

    async def body(db) -> None:
        await _make_version(db, "a", ["v1a", "v1b"])
        await _make_version(db, "b", ["v2a"])
        assets = await canvas_runner._latest_task_assets(db, 1, NODE, "a")
        assert [a.name for a in assets] == ["v1a", "v1b"], "回滚后应当给旧那一版的全部产物"
        # 指不到的一版退回最新
        again = await canvas_runner._latest_task_assets(db, 1, NODE, "没有这一版")
        assert [a.name for a in again] == ["v2a"]

    _run(body)


def test_a_failed_version_is_skipped_when_picking_the_latest():
    """只有成功的那几条才算产物，所以「最新一版全失败」时交付的是上一个能用的版本。"""
    async def body(db) -> None:
        await _make_version(db, "a", ["v1a"])
        await _make_version(db, "b", [], status="failed")
        assets = await canvas_runner._latest_task_assets(db, 1, NODE)
        assert [a.name for a in assets] == ["v1a"]

    _run(body)


def test_another_nodes_versions_do_not_leak_in():
    async def body(db) -> None:
        await _make_version(db, "a", ["v1a"])
        other = await _make_version(db, "z", ["zzz"])
        other.canvas_node_id = "other"
        await db.commit()
        assets = await canvas_runner._latest_task_assets(db, 1, NODE)
        assert [a.name for a in assets] == ["v1a"]

    _run(body)


# ---------- 3. 版本列表 ----------


def test_the_version_list_reports_both_ends_and_the_effective_one():
    """`pin` 与 `activeKey` 分开回：前者是节点上写着的，后者是实际生效的。"""

    async def body(db) -> None:
        await _make_version(db, "a", ["v1a", "v1b"])
        await _make_version(db, "b", ["v2a"])
        data = await canvas_runner.node_versions(db, 1, NODE, "a")
        assert [g["index"] for g in data["items"]] == [2, 1]
        assert [g["latest"] for g in data["items"]] == [True, False]
        assert data["pin"] == "a" and data["activeKey"] == "a"
        assert data["latestKey"] == "b"
        old = data["items"][1]
        assert old["assetCount"] == 2 and [a["name"] for a in old["assets"]] == ["v1a", "v1b"]

    _run(body)


def test_the_version_list_keeps_a_version_that_produced_nothing():
    """某一版全失败也要列出来——用户最想知道的恰恰是「这一版为什么不能用」。"""

    async def body(db) -> None:
        await _make_version(db, "a", ["v1a"])
        await _make_version(db, "b", [], status="failed")
        data = await canvas_runner.node_versions(db, 1, NODE)
        assert len(data["items"]) == 2
        bad = data["items"][0]
        assert bad["failedCount"] == 1 and bad["doneCount"] == 0 and bad["assetCount"] == 0

    _run(body)


def test_the_version_list_says_which_version_a_pin_effectively_got():
    """pin 指不到任何一版时，`activeKey` 要如实给成生效的那一版。"""

    async def body(db) -> None:
        await _make_version(db, "a", ["v1a"])
        data = await canvas_runner.node_versions(db, 1, NODE, "早就没了")
        assert data["pin"] == "早就没了"
        assert data["activeKey"] == "a" and data["latestKey"] == "a"

    _run(body)


# ---------- 4. 单节点运行 vs 整图运行 ----------


def _doc(pin: str = "") -> dict:
    """一条 src → dst 的连线；src 就是 `_make_version` 造产物用的那个节点 id。"""
    return {
        "nodes": [
            {"id": NODE, "type": "image", "data": {"versionKey": pin}},
            {"id": "dst", "type": "video", "data": {}},
        ],
        "edges": [{"id": "e", "source": NODE, "target": "dst"}],
    }


def test_a_single_node_run_respects_the_rollback():
    """单跑下游 = 回滚的用处所在：「这一版的分镜图更好，拿它去出视频」。"""

    async def body(db) -> None:
        await _make_version(db, "a", ["v1a"])
        await _make_version(db, "b", ["v2a"])
        upstream = await canvas_runner._resolve_upstream(db, 1, _doc("a"), "dst")
        _sid, _node, assets = upstream[0]
        assert [a.name for a in assets] == ["v1a"]

    _run(body)


def test_a_whole_graph_run_ignores_the_rollback():
    """整图会把源节点自己也重跑一遍，那时还按旧版给下游，
    用户会看到「我刚跑的图没被用上」——比「回滚没生效」更难解释。"""

    async def body(db) -> None:
        await _make_version(db, "a", ["v1a"])
        await _make_version(db, "b", ["v2a"])
        upstream = await canvas_runner._resolve_upstream(db, 1, _doc("a"), "dst", use_pins=False)
        _sid, _node, assets = upstream[0]
        assert [a.name for a in assets] == ["v2a"]

    _run(body)


def test_the_preview_warns_that_a_whole_graph_run_ignores_rollbacks():
    """这条不能在点下去之后才知道：确认弹窗里要说出来。"""
    text = (BACKEND / "app" / "services" / "canvas_runner.py").read_text(encoding="utf-8")
    start = text.index("if pinned:")
    block = text[start : text.index("if rerun:", start)]
    assert "回滚后的旧版本" in block, "预览没有把「回滚只在单跑时生效」说出来"
    assert "单跑下游那个节点" in block, "没给用户一条能照做的路"


# ---------- 5. 接线（前后端 + 接口）----------


def test_every_node_task_carries_its_version_mark():
    """单任务节点也要盖批次标记，否则「重新生成」出来的那条会自成一版。"""
    text = (BACKEND / "app" / "services" / "canvas_runner.py").read_text(encoding="utf-8")
    start = text.index("async def _create_node_task(")
    block = text[start : text.index("async def _resolve_upstream(", start)]
    assert block.count('"asset_batch": batch') >= 2, "文本 / 工作流 / 图片视频三条路都要盖"
    assert "batch = _new_batch_id(node)" in block


def test_the_status_endpoint_uses_the_same_grouping():
    """列表与交付不能各分一次组——那会出现「列表说 3 版、交付的是第 4 版」。"""
    text = (BACKEND / "app" / "routers" / "canvas.py").read_text(encoding="utf-8")
    start = text.index("async def canvas_status(")
    block = text[start : text.index("async def node_versions(", start)]
    assert "canvas_runner.group_versions(" in block, "状态接口没有复用同一套分组"
    assert "canvas_runner.pick_version(" in block, "状态接口没有认回滚"
    for field in ("versionIndex", "versionTotal", "versionKey", "versionLatest"):
        assert f'"{field}"' in block, f"状态里缺 {field}"


def test_the_versions_endpoint_is_read_only():
    """回滚走的是「保存画布」那条路，不另开一个写接口（否则有两条写画布的路径）。"""
    text = (BACKEND / "app" / "routers" / "canvas.py").read_text(encoding="utf-8")
    start = text.index("async def node_versions(")
    block = text[start : text.index("_DOC_PREVIEW_CHARS", start)]
    assert 'canvas_runner.node_versions(' in block
    assert "canvas_runner.version_pin_of(node)" in block, "没有把节点上回滚到的那一版读进来"
    for bad in ("db.add(", "await db.commit()", "HTTPException(status_code=400"):
        assert bad not in block, f"这个接口不该写任何东西，却出现了 {bad}"


def test_the_frontend_wires_the_version_picker_to_the_node_data():
    page = (BACKEND.parent / "frontend" / "src" / "pages" / "CanvasPage.tsx").read_text(
        encoding="utf-8"
    )
    assert ".nodeVersions(projectId, id)" in page, "浮框没有拉版本列表"
    assert "ctx.pickVersion(id, null)" in page, "没有「切到最新」"
    # 选「最新」必须清掉 pin（而不是把最新的 key 写进去）：否则下次生成出新版时
    # 节点还钉在旧的「最新」上，看起来像回滚没生效
    assert "ctx.pickVersion(id, picked?.latest ? null : e.target.value)" in page, (
        "选「最新」时没有改回「跟最新」"
    )
    types = (BACKEND.parent / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")
    assert "versionKey?: string | null;" in types
    assert "export interface CanvasNodeVersions" in types
    assert "/nodes/${encodeURIComponent(nodeId)}/versions" in (
        BACKEND.parent / "frontend" / "src" / "api.ts"
    ).read_text(encoding="utf-8")


def test_a_version_switch_refreshes_the_node_status_after_saving():
    """产物网格是后端按**已保存的**画布算出来的：切完版本要等落库再补刷一次状态。

    不补刷的表现是「标签已经是第 1 版、缩略图还是第 2 版」——浏览器走查时正是这样。
    """
    page = (BACKEND.parent / "frontend" / "src" / "pages" / "CanvasPage.tsx").read_text(
        encoding="utf-8"
    )
    assert "versionSwitchPending.current = true" in page, "切版本时没有打上待刷新的标记"
    start = page.index("void saveDoc().then((ok) => {")
    block = page[start : page.index("}, 1000);", start)]
    assert "versionSwitchPending.current" in block and "refreshStatus()" in block, (
        "自动保存落库后没有补刷节点状态"
    )


def test_the_panel_trusts_the_node_field_not_the_saved_answer():
    """刚在下拉里改完、画布还没保存时，接口回的 `activeKey` 仍是上一次保存的算法结果。

    界面若拿它当准，就会「选了半天没反应」——浏览器走查时正是这样，
    所以当前这一版一律以节点上的字段算。
    """
    page = (BACKEND.parent / "frontend" / "src" / "pages" / "CanvasPage.tsx").read_text(
        encoding="utf-8"
    )
    start = page.index("const activeVersion =")
    block = page[start : page.index("const pinMissing", start)]
    assert "versionPin ? versionItems.find((v) => v.key === versionPin)" in block, (
        "当前这一版没有以节点上的 versionKey 为准"
    )
    assert "versions?.activeKey" not in block, (
        "又拿接口回的 activeKey 当准了：保存之前它会滞后一版"
    )


def test_the_panel_never_hides_a_rollback_it_could_not_load():
    """版本列表拉不到时，也要如实说「节点上记着已回滚」——不能装作没回滚过。"""
    page = (BACKEND.parent / "frontend" / "src" / "pages" / "CanvasPage.tsx").read_text(
        encoding="utf-8"
    )
    start = page.index('{(versionItems.length > 1 || versionPin) && (')
    block = page[start : page.index("{imageProducts.length > 0 && (", start)]
    assert "版" in block
    assert "版本列表没取回来" in block, "列表拉不到时没有如实说明"
    assert "你回滚到的那一版已经不在了" in block, "pin 指不到任何一版时没有如实说明"
    assert "整图运行会把每个节点重跑一遍" in block, "没有把整图会让回滚失效说出来"


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
