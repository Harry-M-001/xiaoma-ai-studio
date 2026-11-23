"""工作流模板：内置三份、存自己的片段、插入时换 id / 让位 / 填输入、依赖预检。

模板的价值全在「能复用到别的画布」——所以这里钉的不是列表有多好看，而是：
插进去的节点不撞 id、不压在旧内容上、输入真被替换、缺模型时说得出缺什么。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import Project, ProviderService  # noqa: E402
from app.registry.canvas_nodes import validate_document  # noqa: E402
from app.services import workflow_template_service as templates  # noqa: E402


def _run(fn):  # noqa: ANN001
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario():  # noqa: ANN202
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            await fn(maker)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


async def _seed_project(maker, doc: dict | None = None) -> int:  # noqa: ANN001
    async with maker() as db:
        p = Project(
            name="模板测试",
            canvas_json=json.dumps(
                doc or {"schemaVersion": 1, "nodes": [], "edges": [], "viewport": {}},
                ensure_ascii=False,
            ),
        )
        db.add(p)
        await db.commit()
        await db.refresh(p)
        return p.id


async def _seed_models(maker, modalities: list[str]) -> None:  # noqa: ANN001
    async with maker() as db:
        models = [
            {"name": f"{m}-1", "modality": m, "label": f"{m.upper()} 1"} for m in modalities
        ]
        db.add(
            ProviderService(
                name="测试服务",
                kind="openai",
                base_url="https://example.invalid/v1",
                enabled=True,
                models_json=json.dumps(models),
            )
        )
        await db.commit()


async def _doc(maker, pid: int) -> dict:  # noqa: ANN001
    async with maker() as db:
        p = await db.get(Project, pid)
        return json.loads(p.canvas_json or "{}")


def test_builtin_templates_are_valid_and_cover_the_first_batch():
    async def scenario(maker):  # noqa: ANN001
        async with maker() as db:
            rows = await templates.list_templates(db)
        builtin = [r for r in rows if r["source"] == "builtin"]
        assert [r["name"] for r in builtin] == ["图文广告", "漫剧分镜", "逐镜成片"]
        for meta in builtin:
            async with maker() as db:
                tpl = await templates._load(db, meta["id"])
            nodes, edges = validate_document({**tpl["doc"], "schemaVersion": 1})
            assert nodes and edges, f"{meta['name']} 的拓扑不合法"
            assert "text" in meta["requirements"]["modalities"]

    _run(scenario)


def test_inserting_a_template_remaps_ids_and_moves_it_out_of_the_way():
    """插进来的节点不能和画布上已有的撞 id，也不能压在已有内容上。"""
    async def scenario(maker):  # noqa: ANN001
        existing = {
            "schemaVersion": 1,
            "nodes": [
                {
                    "id": "old",
                    "type": "image",
                    "position": {"x": 900, "y": 0},
                    "data": {"prompt": "已有节点", "model_key": ""},
                }
            ],
            "edges": [],
            "viewport": {},
        }
        pid = await _seed_project(maker, existing)
        async with maker() as db:
            out = await templates.instantiate(
                db, pid, "builtin:ad_image", {"prompt": "保温杯，通勤"}, base_revision=0
            )
        assert out["revision"] == 1

        doc = await _doc(maker, pid)
        ids = [n["id"] for n in doc["nodes"]]
        assert "old" in ids and len(ids) == 3
        assert len(set(ids)) == 3, "插入后节点 id 撞了"
        new_nodes = [n for n in doc["nodes"] if n["id"].startswith("tpl_")]
        assert all(n["position"]["x"] > 900 for n in new_nodes), "模板压在已有节点上了"
        idea = next(n for n in new_nodes if n["type"] == "idea")
        assert idea["data"]["prompt"] == "保温杯，通勤", "输入没有填进第一个节点"
        assert doc["edges"], "插入模板没带上连线"

    _run(scenario)


def test_inserting_respects_the_canvas_revision():
    """模板插入也是写画布，不能绕过修订号把别人的改动盖掉。"""
    async def scenario(maker):  # noqa: ANN001
        pid = await _seed_project(maker)
        async with maker() as db:
            await templates.instantiate(db, pid, "builtin:ad_image", {}, base_revision=0)
        async with maker() as db:
            with pytest.raises(ValueError) as e:
                await templates.instantiate(db, pid, "builtin:ad_image", {}, base_revision=0)
        assert "修改" in str(e.value)
        doc = await _doc(maker, pid)
        assert len(doc["nodes"]) == 2, "被拒绝的插入仍然改了画布"

    _run(scenario)


def test_custom_template_roundtrip_and_placeholder_replacement():
    """自己存一段 → 列表里能看到 → 插到别处，`{{prompt}}` 被替换成用户填的值。"""
    async def scenario(maker):  # noqa: ANN001
        src = {
            "nodes": [
                {
                    "id": "a",
                    "type": "idea",
                    "position": {"x": 0, "y": 0},
                    "data": {"prompt": "", "model_key": ""},
                },
                {
                    "id": "b",
                    "type": "image",
                    "position": {"x": 300, "y": 0},
                    "data": {"prompt": "风格：{{style}}", "model_key": ""},
                },
            ],
            "edges": [
                {
                    "id": "e",
                    "source": "a",
                    "sourceHandle": "out-text",
                    "target": "b",
                    "targetHandle": "in-any",
                }
            ],
        }
        async with maker() as db:
            meta = await templates.create_template(
                db,
                {
                    "name": "我的片段",
                    "description": "",
                    "nodes": src["nodes"],
                    "edges": src["edges"],
                    "inputs": [{"key": "style", "label": "风格"}],
                },
            )
        assert meta["id"].startswith("custom:") and meta["nodeCount"] == 2

        pid = await _seed_project(maker)
        async with maker() as db:
            await templates.instantiate(
                db, pid, meta["id"], {"prompt": "猫", "style": "水彩"}, base_revision=0
            )
        doc = await _doc(maker, pid)
        image = next(n for n in doc["nodes"] if n["type"] == "image")
        assert image["data"]["prompt"] == "风格：水彩"
        idea = next(n for n in doc["nodes"] if n["type"] == "idea")
        assert idea["data"]["prompt"] == "猫"

    _run(scenario)


def test_preflight_says_what_is_missing_then_turns_ok():
    """预检要在插入之前回答「这台机器能不能跑」：缺什么能力、哪个模型解析不了。"""
    async def scenario(maker):  # noqa: ANN001
        async with maker() as db:
            empty = await templates.preflight(db, "builtin:ad_image")
        assert empty["ok"] is False
        assert "text" in empty["missingModalities"] and "image" in empty["missingModalities"]

        await _seed_models(maker, ["text", "image"])
        async with maker() as db:
            ready = await templates.preflight(db, "builtin:ad_image")
        assert ready["ok"] is True, ready
        assert ready["missingNodes"] == []

    _run(scenario)


def test_inserting_autofills_models_from_what_is_available():
    """插入时按能力自动挑一个可用模型：不替用户留一堆「未选择模型」的节点。"""
    async def scenario(maker):  # noqa: ANN001
        await _seed_models(maker, ["text", "image"])
        pid = await _seed_project(maker)
        async with maker() as db:
            await templates.instantiate(db, pid, "builtin:storyboard_chain", {}, base_revision=0)
        doc = await _doc(maker, pid)
        assert doc["nodes"], "什么都没插进去"
        for n in doc["nodes"]:
            if n["type"] in ("idea", "novel", "script", "storyboard", "assetSheet"):
                assert n["data"]["model_key"].startswith("1:"), f"{n['type']} 没有自动补文本模型"
            if n["type"] in ("assetImage", "storyboardImage"):
                assert n["data"]["model_key"].startswith("1:"), f"{n['type']} 没有自动补图片模型"

    _run(scenario)


def test_unknown_template_ref_is_rejected():
    async def scenario(maker):  # noqa: ANN001
        pid = await _seed_project(maker)
        async with maker() as db:
            with pytest.raises(ValueError):
                await templates.instantiate(db, pid, "builtin:nope", {}, base_revision=0)
        doc = await _doc(maker, pid)
        assert doc["nodes"] == []

    _run(scenario)
