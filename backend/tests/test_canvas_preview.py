"""#21 整图预估（扣费前二次确认）的回归测试（直接 python 运行）。

运行：venv/Scripts/python tests/test_canvas_preview.py

覆盖三块：
1. `dry_run=True` 只算数不落库 —— 四条建任务路径（资产设定图 / 分镜图 / 逐镜视频 / 单任务）都验一遍；
2. `preview_graph` 的回答：条数、必挂、待定、重跑、素材复用，且全程不写库；
3. 静态守卫 —— 预览与真跑必须共用同一段建任务代码，以后新增批量节点别绕过去。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import Asset, ComfyWorkflow, Project, ProviderService, Task  # noqa: E402
from app.services import canvas_runner, provider_store  # noqa: E402

RUNNER_SRC = (BACKEND / "app" / "services" / "canvas_runner.py").read_text(encoding="utf-8")

# 资产表与分镜表刻意用不同的名字：分镜文本里不提任何资产名，
# 免掉「角色提及注入」带来的额外参考图，统计口径才干净。
TABLE = """## 资产表

| 中文名 | 类型 | 英文视觉描述 | 出现场次 |
| --- | --- | --- | --- |
| 暮光闪闪 | 角色 | Twilight Sparkle, purple unicorn, blue mane with pink stripes | S01,S02 |
| 永恒花园 | 场景 | crystal castle garden, white marble, blooming flowers | S01 |
| 星光罗盘 | 道具 | golden compass, cracked glass, glowing needle | S02,S04 |
"""

SHEET = """## 场景1 | 黄昏的花园

### 镜头1 | 全景 | 缓慢推近 | 4s
- 画面：小焰站在花园中央，缓缓抬头
- 首帧提示词：A lone figure standing in a crystal garden

### 镜头2 | 中景 | 固定 | 3s
- 画面：阿蓝从树后探出头
- 首帧提示词：A second figure peeking from behind a tree
"""


# ---------- 跑测脚手架 ----------


class _FakeResolved:
    class _Svc:
        id = 1

    service = _Svc()


def _run(fn):
    """在内存库上跑一段异步场景：把 SessionLocal 与模型解析换成假的。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    real_session = canvas_runner.SessionLocal
    real_resolve = provider_store.resolve_model

    async def fake_resolve(db, model_key, modality):  # noqa: ANN001
        return _FakeResolved()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        canvas_runner.SessionLocal = maker
        provider_store.resolve_model = fake_resolve
        try:
            await fn(maker)
        finally:
            canvas_runner.SessionLocal = real_session
            provider_store.resolve_model = real_resolve
            await engine.dispose()

    asyncio.run(scenario())


async def _task_count(db) -> int:  # noqa: ANN001
    row = await db.execute(select(func.count()).select_from(Task))
    return int(row.scalar_one())


async def _new_project(maker, doc: dict) -> int:  # noqa: ANN001
    async with maker() as db:
        p = Project(name="预览测试", canvas_json=json.dumps(doc, ensure_ascii=False))
        db.add(p)
        await db.commit()
        await db.refresh(p)
        return p.id


async def _save_canvas(maker, project_id: int, doc: dict) -> None:  # noqa: ANN001
    async with maker() as db:
        p = await db.get(Project, project_id)
        p.canvas_json = json.dumps(doc, ensure_ascii=False)
        await db.commit()


async def _add_image(
    maker, project_id: int, node_id: str, name: str, shot_no: str = "", batch: str = ""
) -> Asset:  # noqa: ANN001
    """造一张「某节点已产出的图」：任务是 completed，产物带名字（可带镜号与批次）。

    同一批的图要带同一个 asset_batch —— 真跑时批量节点就是这么标记的，
    `_latest_task_assets` 靠它把整批产物一次收齐（否则只会看到最后一张）。
    """
    params: dict = {}
    if shot_no:
        params["shot_no"] = shot_no
    if batch:
        params["asset_batch"] = batch
    async with maker() as db:
        task = Task(
            kind="image",
            status="completed",
            service_id=1,
            model="1:i",
            prompt="p",
            params_json=json.dumps(params, ensure_ascii=False),
            canvas_project_id=project_id,
            canvas_node_id=node_id,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        asset = Asset(
            kind="image",
            filename=f"{name}.png",
            original_name=f"{name}.png",
            content_type="image/png",
            size=1,
            task_id=task.id,
            name=name,
        )
        db.add(asset)
        await db.commit()
        await db.refresh(asset)
        return asset


async def _seed_storyboard_images(maker, project_id: int) -> None:  # noqa: ANN001
    """给分镜图节点造出两镜产物（同一批次）：逐镜出片、复用统计都靠它。"""
    for no in ("1", "2"):
        await _add_image(
            maker, project_id, "n-shots-img", f"镜头{no}", shot_no=no, batch="b1"
        )


def _doc_node(nid: str, ntype: str, text: str, **data) -> dict:
    """文档节点：正文直接写在 docText 上（等价于用户手改过的正文，无需落盘产物）。"""
    return {"id": nid, "type": ntype, "data": {"model_key": "1:t", "docText": text, **data}}


def _graph(*, video: bool = False, image_data: dict | None = None) -> dict:
    """一张小画布：资产表 → 设定图；分镜 → 分镜图；（可选）分镜图 → 逐镜视频。"""
    extra = image_data or {}
    nodes = [
        _doc_node("n-sheet", "assetSheet", TABLE, prompt="按上面的设定整理一份资产表"),
        _doc_node("n-shots", "storyboard", SHEET, prompt="把这份资产表写成两镜分镜"),
        {
            "id": "n-assets",
            "type": "assetImage",
            "data": {"model_key": "1:i", "n": 1, **extra},
        },
        {
            "id": "n-shots-img",
            "type": "storyboardImage",
            "data": {"model_key": "1:i", "n": 1, **extra},
        },
    ]
    edges = [
        {"id": "e1", "source": "n-sheet", "target": "n-assets"},
        {"id": "e2", "source": "n-shots", "target": "n-shots-img"},
    ]
    if video:
        nodes.append(
            {
                "id": "n-video",
                "type": "video",
                "data": {"model_key": "1:v", "mode": "first_last", "shotVideo": "each"},
            }
        )
        edges.append({"id": "e3", "source": "n-shots-img", "target": "n-video"})
        # 逐镜出片同时要「分镜表」（逐镜画面与时长）与「分镜图」（逐镜首帧）：
        # 只接分镜图的话取不到镜头表，节点会明确要求把「分镜」也接进来
        edges.append({"id": "e4", "source": "n-shots", "target": "n-video"})
    return {"schemaVersion": 1, "nodes": nodes, "edges": edges, "viewport": {}}


def _node(doc: dict, nid: str) -> dict:
    return next(n for n in doc["nodes"] if n["id"] == nid)


def _item(result: dict, nid: str) -> dict:
    return next(n for n in result["nodes"] if n["id"] == nid)


def _notes(result: dict) -> str:
    return " ".join(n["text"] for n in result["notes"])


# ---------- dry_run：只算数，不落库 ----------


def test_asset_image_dry_run_writes_nothing():
    async def scenario(maker):
        doc = _graph()
        pid = await _new_project(maker, doc)
        async with maker() as db:
            tasks = await canvas_runner._create_asset_image_tasks(
                db, pid, _node(doc, "n-assets"), TABLE, [], dry_run=True
            )
            assert len(tasks) == 3  # 三行资产
            assert await _task_count(db) == 0

    _run(scenario)


def test_storyboard_image_dry_run_writes_nothing():
    async def scenario(maker):
        doc = _graph()
        pid = await _new_project(maker, doc)
        async with maker() as db:
            tasks = await canvas_runner._create_storyboard_image_tasks(
                db, pid, _node(doc, "n-shots-img"), SHEET, [], None, dry_run=True
            )
            assert len(tasks) == 2  # 两镜
            assert await _task_count(db) == 0

    _run(scenario)


def test_shot_video_dry_run_writes_nothing():
    """逐镜视频的预估：段数算得出来，但一条任务都不落库。"""
    async def scenario(maker):
        doc = _graph(video=True)
        pid = await _new_project(maker, doc)
        await _seed_storyboard_images(maker, pid)
        async with maker() as db:
            before = await _task_count(db)
            upstream = await canvas_runner._resolve_upstream(db, pid, doc, "n-video")
            tasks = await canvas_runner._build_node_tasks(
                db, pid, doc, _node(doc, "n-video"), upstream, dry_run=True
            )
            assert len(tasks) == 2  # 两镜各一段
            assert await _task_count(db) == before

    _run(scenario)


def test_single_node_dry_run_writes_nothing():
    async def scenario(maker):
        doc = _graph()
        doc["nodes"].append(
            {"id": "n-img", "type": "image", "data": {"model_key": "1:i", "prompt": "一只猫"}}
        )
        pid = await _new_project(maker, doc)
        async with maker() as db:
            task = await canvas_runner._create_node_task(
                db,
                pid,
                _node(doc, "n-img"),
                "一只猫",
                [],
                [],
                dry_run=True,
            )
            assert task.kind == "image"
            assert await _task_count(db) == 0

    _run(scenario)


def test_dry_run_count_matches_real_run():
    """同为一段代码：预估条数必须与真跑落库的条数一致（这是弹窗数字敢给人看的唯一理由）。"""
    async def scenario(maker):
        doc = _graph()
        pid = await _new_project(maker, doc)
        node = _node(doc, "n-assets")
        async with maker() as db:
            preview = await canvas_runner._create_asset_image_tasks(
                db, pid, node, TABLE, [], dry_run=True
            )
        async with maker() as db:
            real = await canvas_runner._create_asset_image_tasks(db, pid, node, TABLE, [])
        async with maker() as db:
            assert len(real) == len(preview) == 3
            assert await _task_count(db) == 3

    _run(scenario)


# ---------- preview_graph：这一跑会花多少 ----------


def test_preview_counts_and_writes_nothing():
    async def scenario(maker):
        doc = _graph()
        pid = await _new_project(maker, doc)
        result = await canvas_runner.preview_graph(pid)
        counts = {n["id"]: n["count"] for n in result["nodes"]}
        assert counts["n-sheet"] == 1  # 文档节点：1 条文本任务
        assert counts["n-assets"] == 3  # 三行资产
        assert counts["n-shots"] == 1
        assert counts["n-shots-img"] == 2  # 两镜
        assert result["totals"]["tasks"] == 7
        assert result["totals"]["steps"] == 4
        assert result["totals"]["pendingNodes"] == 0
        assert result["totals"]["blockedNodes"] == 0
        assert result["billable"] is True
        async with maker() as db:
            assert await _task_count(db) == 0  # 预览是纯读的

    _run(scenario)


def test_preview_lists_fanout_kinds():
    async def scenario(maker):
        doc = _graph()
        pid = await _new_project(maker, doc)
        result = await canvas_runner.preview_graph(pid)
        assert _item(result, "n-assets")["kinds"] == {"image": 3}
        assert _item(result, "n-sheet")["kinds"] == {"text": 1}
        assert result["totals"]["byKind"] == {"text": 2, "image": 5}

    _run(scenario)


def test_preview_says_unknown_instead_of_guessing():
    """上游这次才会产出：条数现在无从得知，就如实说不知道，不能拍脑袋写个 1。"""
    async def scenario(maker):
        doc = _graph(video=True)
        pid = await _new_project(maker, doc)
        result = await canvas_runner.preview_graph(pid)
        item = _item(result, "n-video")
        assert item["count"] is None
        assert item["error"] == ""  # 不是错，只是还没到能算的时候
        assert item["waiting"] == ["分镜图"]
        assert result["totals"]["pendingNodes"] == 1
        assert "等上游跑完才知道" in _notes(result)

    _run(scenario)


def test_preview_counts_after_upstream_ran():
    async def scenario(maker):
        doc = _graph(video=True)
        pid = await _new_project(maker, doc)
        await _seed_storyboard_images(maker, pid)
        result = await canvas_runner.preview_graph(pid)
        assert _item(result, "n-video")["count"] == 2  # 逐镜两段
        assert result["totals"]["byKind"]["video"] == 2

    _run(scenario)


def test_preview_flags_nodes_that_will_be_redone():
    """已经有产物的节点会被整图重做一遍 —— 这是二次确认最该讲清楚的钱。"""
    async def scenario(maker):
        doc = _graph()
        pid = await _new_project(maker, doc)
        await _add_image(maker, pid, "n-assets", "暮光闪闪")
        result = await canvas_runner.preview_graph(pid)
        item = _item(result, "n-assets")
        assert item["hasOutput"] is True
        assert item["outputCount"] == 1
        assert result["totals"]["rerunNodes"] == 1
        assert "已经有产物了" in _notes(result)

    _run(scenario)


def test_preview_flags_blockers():
    """已经能判定必挂的节点要提前说，别让用户等到整图跑一半才断在那里。"""
    async def scenario(maker):
        doc = _graph()
        _node(doc, "n-assets")["data"].pop("model_key")  # 没选模型 → 必挂
        pid = await _new_project(maker, doc)
        result = await canvas_runner.preview_graph(pid)
        item = _item(result, "n-assets")
        assert item["count"] is None
        assert "未选择模型" in item["error"]
        assert result["totals"]["blockedNodes"] == 1
        assert "会直接失败" in _notes(result)
        # 必挂不等于整图不能跑：其余节点照常报数
        assert _item(result, "n-shots-img")["count"] == 2

    _run(scenario)


def test_preview_reports_reused_assets():
    """同一张素材被这一跑引用了多次 → 说清楚是哪些、各用了几次。"""
    async def scenario(maker):
        doc = _graph()
        pid = await _new_project(maker, doc)
        star = await _add_image(maker, pid, "n-elsewhere", "主角设定图")
        _node(doc, "n-shots-img")["data"]["refImages"] = [{"id": star.id, "name": "主角设定图"}]
        await _save_canvas(maker, pid, doc)  # 预览读的是库里那份画布
        result = await canvas_runner.preview_graph(pid)
        reused = result["reused"]
        assert [r["name"] for r in reused] == ["主角设定图"]
        assert reused[0]["count"] == 2  # 两镜都挂着它
        assert "复用已有素材" in _notes(result)

    _run(scenario)


def test_preview_rejects_missing_canvas():
    async def scenario(maker):
        try:
            await canvas_runner.preview_graph(9999)
        except ValueError as e:
            assert "画布不存在" in str(e)
        else:
            raise AssertionError("画布不存在时应当直接报错")

    _run(scenario)


def test_preview_note_skips_billing_for_local_workflow():
    """整图只有本机 ComfyUI 工作流时不提扣费 —— 那条提醒对用户就是噪音。"""
    async def scenario(maker):
        doc = {
            "schemaVersion": 1,
            "nodes": [{"id": "n-wf", "type": "workflow", "data": {"workflowId": 1}}],
            "edges": [],
            "viewport": {},
        }
        pid = await _new_project(maker, doc)
        async with maker() as db:
            svc = ProviderService(
                name="本机 ComfyUI", kind="comfy", base_url="http://127.0.0.1:8188"
            )
            db.add(svc)
            await db.commit()
            await db.refresh(svc)
            db.add(ComfyWorkflow(name="放大", provider_id=svc.id, graph_json="{}"))
            await db.commit()
        result = await canvas_runner.preview_graph(pid)
        assert result["billable"] is False
        assert _item(result, "n-wf")["count"] == 1  # 本机工作流一样算得出条数
        assert "消耗" not in _notes(result)

    _run(scenario)


# ---------- 静态守卫 ----------


def _body(name: str) -> str:
    """按函数名切出源码正文（到下一个顶层 def / async def 之前）。"""
    start = RUNNER_SRC.index(f"async def {name}(")
    ends = [
        p
        for p in (
            RUNNER_SRC.find("\nasync def ", start + 1),
            RUNNER_SRC.find("\ndef ", start + 1),
        )
        if p != -1
    ]
    return RUNNER_SRC[start : min(ends) if ends else len(RUNNER_SRC)]


def test_guard_preview_shares_the_same_builder():
    """预览必须走 dry_run 的建任务代码，不能自己另写一套估算法。"""
    body = _body("preview_graph")
    assert "_build_node_tasks(" in body
    assert "dry_run=True" in body


def test_guard_fanout_builders_accept_dry_run():
    """会一次建出多个任务的函数都必须能收 dry_run（以后新增批量节点别漏）。"""
    for name in (
        "_create_asset_image_tasks",
        "_create_storyboard_image_tasks",
        "_create_shot_video_tasks",
        "_create_node_task",
        "_build_node_tasks",
    ):
        body = _body(name)
        head = body[: body.index(") -> ")]
        assert "dry_run" in head, f"{name} 缺少 dry_run 参数"


def test_guard_build_node_tasks_forwards_dry_run():
    """节点级分发要把 dry_run 传给每一个批量分支。"""
    body = _body("_build_node_tasks")
    assert body.count("dry_run=dry_run") == 4  # 资产设定图 / 分镜图 / 逐镜视频 / 单任务
    assert body.count("await _create_") == 4
    assert body.count("await _create_node_task(") == 1


def test_guard_dry_run_returns_before_commit():
    """三个批量建任务函数都要在 commit 之前就为 dry_run 返回。"""
    for name in (
        "_create_asset_image_tasks",
        "_create_storyboard_image_tasks",
        "_create_shot_video_tasks",
    ):
        body = _body(name)
        assert body.index("if dry_run:") < body.index("await db.commit()"), name


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    sys.exit(1 if failed else 0)
