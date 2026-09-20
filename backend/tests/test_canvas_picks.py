"""#33 候选选优（定稿）的回归测试（直接 python 运行）。

运行：venv/Scripts/python tests/test_canvas_picks.py

要守的核心是**「一镜多张时，下游到底用哪一张」**。以前这件事用户说了不算：

- 逐镜出视频遇到「一镜多张」，`_assets_by_shot` 只能取「最后生成的那张」；
- 普通视频节点首尾帧模式按生成顺序取前两张。

现在用户在产物网格里点一张「定稿」，下游就用那一张。而「用哪一张」与「用哪一版」
是同一个层级的事，所以两者共用**同一个出口**（`_latest_task_assets`）——
这也是这一项最容易做错的地方：只要有一处不认，就会出现
「画布上显示的是这一张、下游用的是另一张」，而界面上看起来完全正常。

五块：
1. 纯口径（分组依据、脏值、定稿失效怎么办）；
2. 节点实际交付哪几张（图片节点整组一组 / 分镜图每镜一组）；
3. 定稿跟着**镜号**走，所以生成新版之后仍然生效；回滚到定稿之前则整组交付；
4. 下游：视频节点与逐镜出片用的是定稿那张；
5. 接线：整图忽略两种选择、节点面板必须看到全部候选（不然没法挑）。
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


def test_the_candidate_group_is_keyed_by_the_stable_identity():
    """分组只认「这个位置是什么」：镜号 > 资产名 > 整节点一组。"""
    assert canvas_runner.candidate_key({"shot_no": "3"}) == "shot:3"
    assert canvas_runner.candidate_key({"asset_name": "暮光闪闪"}) == "asset:暮光闪闪"
    assert canvas_runner.candidate_key({}) == ""
    # 镜号优先：分镜图节点上不会同时有资产名，但真出现了要按镜号分（那才是这一镜）
    assert canvas_runner.candidate_key({"shot_no": "1", "asset_name": "X"}) == "shot:1"


def test_a_group_key_never_uses_the_task_id():
    """**不能用任务 id 分组**：任务 id 每生成一次就变，用户在上一版里定稿的那张
    会立刻变成指向不存在的键——而他想表达的是「这个镜位用这张」，与第几次生成无关。"""
    key = canvas_runner.candidate_key({"shot_no": "2", "asset_batch": "abc-node1"})
    assert "abc" not in key and "node1" not in key


def test_a_pick_must_be_a_usable_id():
    """手改 JSON / 老数据里的脏值要丢掉，一个脏值不该让整张图打不开。"""
    assert canvas_runner.picks_of({"data": {"picks": {"shot:1": 12}}}) == {"shot:1": 12}
    assert canvas_runner.picks_of({"data": {"picks": {"shot:1": "12"}}}) == {"shot:1": 12}
    assert canvas_runner.picks_of({"data": {"picks": {"a": "x", "b": 0, "c": -3, "d": None}}}) == {}
    assert canvas_runner.picks_of({"data": {"picks": "本来就不是对象"}}) == {}
    assert canvas_runner.picks_of({"data": {}}) == {}


def _asset(aid: int) -> Asset:
    a = Asset(kind="image", filename=f"{aid}.png", original_name=f"{aid}.png",
              content_type="image/png", size=1)
    a.id = aid
    return a


def test_picking_one_leaves_only_that_one_in_the_group():
    assets = [_asset(1), _asset(2), _asset(3)]
    keys = {1: "", 2: "", 3: ""}
    assert [a.id for a in canvas_runner.apply_picks(assets, keys, {"": 2})] == [2]


def test_a_group_without_a_pick_is_delivered_whole():
    """只定了一镜的稿，不该把别的镜也收缩成一张。"""
    assets = [_asset(1), _asset(2), _asset(3), _asset(4)]
    keys = {1: "shot:1", 2: "shot:1", 3: "shot:2", 4: "shot:2"}
    picked = canvas_runner.apply_picks(assets, keys, {"shot:1": 2})
    assert [a.id for a in picked] == [2, 3, 4]


def test_a_pick_that_points_nowhere_keeps_the_whole_group():
    """定稿失效（那一版里没有它）时整组交付，不是丢空——
    与「回滚指不到版本时退回最新」同一个口径：不因为一次选择失效就让下游读不到东西。"""
    assets = [_asset(1), _asset(2)]
    keys = {1: "", 2: ""}
    assert [a.id for a in canvas_runner.apply_picks(assets, keys, {"": 999})] == [1, 2]
    assert [a.id for a in canvas_runner.apply_picks(assets, keys, {"别的组": 1})] == [1, 2]


def test_no_picks_at_all_changes_nothing():
    assets = [_asset(1), _asset(2)]
    assert [a.id for a in canvas_runner.apply_picks(assets, {1: "", 2: ""}, {})] == [1, 2]


# ---------- 2. 节点实际交付哪几张 ----------


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


async def _add_group(
    db, batch: str, names: list[str], *, shot: str = "", asset_name: str = ""
) -> list[Asset]:
    """造一组候选：一个任务 + 它名下的 N 张图（同一镜 / 同一资产的备选）。"""
    params: dict = {"asset_batch": batch}
    if shot:
        params["shot_no"] = shot
    if asset_name:
        params["asset_name"] = asset_name
    task = Task(
        kind="image", status="completed", service_id=1, model="m", prompt="p",
        params_json=json.dumps(params), canvas_project_id=1, canvas_node_id=NODE,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    out: list[Asset] = []
    for name in names:
        a = Asset(
            kind="image", filename=f"{batch}-{name}.png", original_name=f"{name}.png",
            content_type="image/png", size=1, task_id=task.id, name=name,
        )
        db.add(a)
        await db.commit()
        await db.refresh(a)
        out.append(a)
    return out


def test_the_node_delivers_only_the_picked_candidate():
    """普通图片节点（没有镜号）：整节点一组，定稿一张。"""

    async def body(db) -> None:
        group = await _add_group(db, "a", ["c1", "c2", "c3"])
        assets = await canvas_runner._latest_task_assets(db, 1, NODE, None, {"": group[1].id})
        assert [a.name for a in assets] == ["c2"]

    _run(body)


def test_the_pick_is_per_shot_for_storyboard_images():
    """分镜图节点：**每一镜各一组**，各挑各的，不是整节点只留一张。"""

    async def body(db) -> None:
        s1 = await _add_group(db, "b", ["s1a", "s1b"], shot="1")
        s2 = await _add_group(db, "b", ["s2a", "s2b"], shot="2")
        picks = {"shot:1": s1[1].id, "shot:2": s2[1].id}
        assets = await canvas_runner._latest_task_assets(db, 1, NODE, None, picks)
        assert [a.name for a in assets] == ["s1b", "s2b"]

    _run(body)


def test_an_unpicked_shot_still_delivers_all_its_candidates():
    async def body(db) -> None:
        s1 = await _add_group(db, "b", ["s1a", "s1b"], shot="1")
        await _add_group(db, "b", ["s2a", "s2b"], shot="2")
        assets = await canvas_runner._latest_task_assets(db, 1, NODE, None, {"shot:1": s1[1].id})
        assert [a.name for a in assets] == ["s1b", "s2a", "s2b"]

    _run(body)


def test_the_picks_survive_a_new_version():
    """定稿跟着**镜号**走，所以「定稿 → 又生成一版」之后它仍然生效。

    这是「不用任务 id 分组」的实际回报：新版的任务 id 全变了，但镜号还是那个镜号。
    """

    async def body(db) -> None:
        old = await _add_group(db, "v1", ["旧1", "旧2"], shot="1")
        new = await _add_group(db, "v2", ["新1", "新2"], shot="1")
        # 定稿的是「第 1 镜」这张旧图，但当前版本已经是 v2
        assets = await canvas_runner._latest_task_assets(db, 1, NODE, None, {"shot:1": old[1].id})
        # 旧那张不在当前版本里 → 这一组整组交付（定稿失效的兜底）
        assert [a.name for a in assets] == ["新1", "新2"]
        # 在新版里重新定稿 → 只交付那一张
        again = await canvas_runner._latest_task_assets(db, 1, NODE, None, {"shot:1": new[1].id})
        assert [a.name for a in again] == ["新2"]

    _run(body)


def test_a_rollback_to_before_the_pick_keeps_the_whole_group():
    """回滚到「定稿之前」的那一版：那一版里没有定稿那张，于是整组交付。"""

    async def body(db) -> None:
        await _add_group(db, "v1", ["旧1", "旧2"], shot="1")
        new = await _add_group(db, "v2", ["新1", "新2"], shot="1")
        picks = {"shot:1": new[1].id}
        latest = await canvas_runner._latest_task_assets(db, 1, NODE, None, picks)
        assert [a.name for a in latest] == ["新2"]
        rolled = await canvas_runner._latest_task_assets(db, 1, NODE, "v1", picks)
        assert [a.name for a in rolled] == ["旧1", "旧2"]

    _run(body)


# ---------- 3. 下游用的是定稿那张 ----------


def test_the_downstream_video_uses_the_picked_frame():
    """这是这一项的核心断言：定稿之后，下游首帧必须是定稿那张。

    没定稿时的老行为是「按顺序取前两张」——四张候选里前两张，与用户想用哪张无关。
    """

    async def body(db) -> None:
        group = await _add_group(db, "a", ["c1", "c2", "c3", "c4"])
        # 先按「没定稿」交付：四张都在，下游会取前两张
        plain = await canvas_runner._latest_task_assets(db, 1, NODE)
        assert [a.name for a in plain] == ["c1", "c2", "c3", "c4"]
        # 定稿第 3 张 → 下游只会拿到它
        picked = await canvas_runner._latest_task_assets(db, 1, NODE, None, {"": group[2].id})
        assert [a.name for a in picked] == ["c3"]

    _run(body)


def test_the_shot_video_uses_the_picked_image_for_that_shot():
    """逐镜出片：镜 1 定稿第 2 张、镜 2 不定稿 → 镜 1 用它，镜 2 用最新的那张。"""

    async def body(db) -> None:
        s1 = await _add_group(db, "b", ["s1a", "s1b"], shot="1")
        s2 = await _add_group(db, "b", ["s2a", "s2b"], shot="2")
        assets = await canvas_runner._latest_task_assets(db, 1, NODE, None, {"shot:1": s1[0].id})
        by_shot = await canvas_runner._assets_by_shot(db, assets)
        assert by_shot["1"].name == "s1a", "镜 1 应当用定稿的那张"
        assert by_shot["2"].name == "s2b", "镜 2 没定稿，仍旧行为（最新那张）"

    _run(body)


# ---------- 4. 接线 ----------


def _doc(picks: dict | None = None, pin: str = "") -> dict:
    return {
        "nodes": [
            {"id": NODE, "type": "image", "data": {"versionKey": pin, "picks": picks or {}}},
            {"id": "dst", "type": "video", "data": {}},
        ],
        "edges": [{"id": "e", "source": NODE, "target": "dst"}],
    }


def test_a_single_node_run_respects_the_pick():
    async def body(db) -> None:
        group = await _add_group(db, "a", ["c1", "c2", "c3"])
        doc = _doc({"": group[2].id})
        upstream = await canvas_runner._resolve_upstream(db, 1, doc, "dst")
        _sid, _node, assets = upstream[0]
        assert [a.name for a in assets] == ["c3"]

    _run(body)


def test_a_whole_graph_run_ignores_the_pick_too():
    """整图与回滚同一个口径：那一跑节点会被重跑，两种显式选择都不生效。"""

    async def body(db) -> None:
        group = await _add_group(db, "a", ["c1", "c2", "c3"])
        doc = _doc({"": group[2].id})
        upstream = await canvas_runner._resolve_upstream(db, 1, doc, "dst", use_pins=False)
        _sid, _node, assets = upstream[0]
        assert [a.name for a in assets] == ["c1", "c2", "c3"]

    _run(body)


def test_the_status_endpoint_does_not_filter_so_you_can_still_pick():
    """节点面板必须看到**全部候选**，否则挑都没得挑。

    面板走 `/status`（按版本列全量产物），交付走 `_latest_task_assets`（再按定稿收缩）。
    这两处**必须**不一样，所以这里钉住它：状态接口不许去调 apply_picks。
    """
    text = (BACKEND / "app" / "routers" / "canvas.py").read_text(encoding="utf-8")
    start = text.index("async def canvas_status(")
    block = text[start : text.index("async def node_versions(", start)]
    assert "apply_picks" not in block, "状态接口按定稿过滤了：用户再也看不到别的候选"
    assert "picks_of" not in block, "状态接口读了定稿：面板会把别的候选藏起来"


def test_the_frontend_picks_are_written_back_to_the_node():
    page = (BACKEND.parent / "frontend" / "src" / "pages" / "CanvasPage.tsx").read_text(
        encoding="utf-8"
    )
    assert "ctx.pickCandidate(" in page, "产物网格里没有定稿入口"
    types = (BACKEND.parent / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")
    assert "picks?: Record<string, number> | null;" in types, "节点数据类型里没有 picks"


def test_the_product_view_carries_the_group_key():
    """分组键由**后端**给（`asset_view` 的 `candidateKey`），前端只按它数个数。

    前端重写一遍分组规则的话，会出现「界面上分在一组的、后端其实不是一组」——
    用户挑的那张未必是下游认的那张，而界面上看起来完全正常。
    """
    text = (BACKEND / "app" / "services" / "canvas_runner.py").read_text(encoding="utf-8")
    start = text.index("def asset_view(")
    block = text[start : text.index("async def _latest_task_assets(", start)]
    assert '"candidateKey": candidate_key(data)' in block, "产物视图没带分组键"
    types = (BACKEND.parent / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")
    assert "candidateKey?: string;" in types, "前端产物类型里没有分组键"


def test_the_pick_button_only_shows_when_there_is_a_choice():
    """一组只有一张时不给定稿按钮：那是个没意义的动作，只会让面板变吵。"""
    page = (BACKEND.parent / "frontend" / "src" / "pages" / "CanvasPage.tsx").read_text(
        encoding="utf-8"
    )
    assert "const many = (groupSize.get(key) ?? 0) > 1;" in page, "没有按「这一组有多张」判"
    assert "{many && (" in page, "定稿按钮没有按「这一组有多张」把住"
    css = (BACKEND.parent / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")
    assert ".canvas-pick" in css, "定稿按钮没有样式"
    assert ".canvas-prodcell.is-picked" in css, "已定稿那张没有标记样式"


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
