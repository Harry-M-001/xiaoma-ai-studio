"""资产链（B 期）：资产表解析 / 设定图提示词 / 角色提及注入。

运行：venv/Scripts/python tests/test_asset_chain.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import models  # noqa: F401  注册全部 ORM 模型
from app.database import Base
from app.models import Asset, Task
from app.registry.canvas_nodes import DOC_NODE_KINDS, NODE_SCHEMAS, is_asset_image, is_doc_kind
from app.registry.schema_registry import SCHEMA_REGISTRY
from app.services import asset_sheet, canvas_runner

TABLE = """## 资产表

| 中文名 | 类型 | 英文视觉描述 | 出现场次 |
| --- | --- | --- | --- |
| 暮光闪闪 | 角色 | Twilight Sparkle, purple unicorn, blue mane with pink stripes | S01,S02 |
| 永恒花园 | 场景 | crystal castle garden, white marble, blooming flowers | S01 |
| 星光罗盘 | 道具 | golden compass, cracked glass, glowing needle | S02,S04 |
"""


# ---------- 资产表解析 ----------


def test_parse_basic_table():
    rows = asset_sheet.parse_asset_table(TABLE)
    assert [r.name for r in rows] == ["暮光闪闪", "永恒花园", "星光罗盘"], rows
    assert [r.category for r in rows] == ["角色", "场景", "道具"]
    assert rows[0].scenes == "S01,S02"
    assert rows[0].visual.startswith("Twilight Sparkle")


def test_parse_tolerates_header_absence_and_bold():
    text = "\n".join(
        [
            "| **雲宝** | **角色** | Rainbow Dash, cyan pegasus, rainbow mane | **全片** |",
            "| :--: | :--: | :--: | :--: |",
            "| `小蝶` | `角色` | Fluttershy, pale yellow pegasus, long pink mane | S03 |",
        ]
    )
    rows = asset_sheet.parse_asset_table(text)
    assert [r.name for r in rows] == ["雲宝", "小蝶"], rows
    assert rows[0].scenes == "全片"


def test_parse_skips_broken_rows_without_raising():
    text = "\n".join(
        [
            "| 中文名 | 类型 | 英文视觉描述 | 出现场次 |",
            "| --- | --- | --- | --- |",
            "| 暮光闪闪 | 角色 | | S01 |",  # 视觉描述为空 → 丢掉
            "|  | 角色 | somebody | S01 |",  # 名字为空 → 丢掉
            "随便一句模型跑题的废话",
            "| 云宝 | 角色 | Rainbow Dash, cyan pegasus | S02 |",
        ]
    )
    rows = asset_sheet.parse_asset_table(text)
    assert [r.name for r in rows] == ["云宝"], rows


def test_parse_dedups_same_name():
    text = "| 云宝 | 角色 | Rainbow Dash | S01 |\n| 云宝 | 道具 | wrong row | S02 |"
    rows = asset_sheet.parse_asset_table(text)
    assert len(rows) == 1 and rows[0].category == "角色"


def test_parse_three_column_table():
    """模型少写一列也不能全军覆没。"""
    text = "| 中文名 | 类型 | 英文视觉描述 |\n| --- | --- | --- |\n| 云宝 | 角色 | Rainbow Dash, cyan pegasus |"
    rows = asset_sheet.parse_asset_table(text)
    assert len(rows) == 1 and rows[0].scenes == ""


def test_parse_bullet_fallback():
    """模型不肯写表格时，退到 `- 名称（类型）：描述` 列表行。"""
    text = "\n".join(
        [
            "资产清单：",
            "- 暮光闪闪（角色）：Twilight Sparkle, purple unicorn",
            "- 永恒花园（场景）：crystal castle garden, white marble",
        ]
    )
    rows = asset_sheet.parse_asset_table(text)
    assert [r.name for r in rows] == ["暮光闪闪", "永恒花园"], rows
    assert rows[1].category == "场景"


def test_parse_empty_returns_empty():
    assert asset_sheet.parse_asset_table("") == []
    assert asset_sheet.parse_asset_table("没有表格，只有一段散文。") == []


def test_parse_caps_row_count():
    """模型写超长表格时截断，避免一次建几十个生图任务把额度烧光。"""
    lines = ["| 中文名 | 类型 | 英文视觉描述 | 出现场次 |", "| --- | --- | --- | --- |"]
    for i in range(asset_sheet.MAX_ASSET_ROWS + 10):
        lines.append(f"| 角色{i} | 角色 | character number {i}, blue coat | S01 |")
    rows = asset_sheet.parse_asset_table("\n".join(lines))
    assert len(rows) == asset_sheet.MAX_ASSET_ROWS


def test_normalize_category_aliases():
    assert asset_sheet.normalize_category("人物") == "角色"
    assert asset_sheet.normalize_category("character") == "角色"
    assert asset_sheet.normalize_category("地点") == "场景"
    assert asset_sheet.normalize_category("物品") == "道具"
    # 认不出来时保留原词，不丢信息
    assert asset_sheet.normalize_category("载具") == "载具"


def test_filter_rows_by_scope():
    rows = asset_sheet.parse_asset_table(TABLE)
    assert len(asset_sheet.filter_rows(rows, "")) == 3
    assert len(asset_sheet.filter_rows(rows, "all")) == 3
    assert [r.name for r in asset_sheet.filter_rows(rows, "character")] == ["暮光闪闪"]
    assert [r.name for r in asset_sheet.filter_rows(rows, "prop")] == ["星光罗盘"]
    assert asset_sheet.filter_rows(rows, "scene")[0].scope == "scene"


# ---------- 设定图提示词 ----------


def _row(name: str, category: str) -> asset_sheet.AssetRow:
    return asset_sheet.AssetRow(name=name, category=category, visual="X, blue coat", scenes="S01")


def test_character_prompt_asks_for_three_views():
    p = asset_sheet.build_reference_prompt(_row("暮光闪闪", "角色"))
    assert "three views" in p and "face close-up" in p
    assert p.startswith("character reference sheet of X, blue coat")


def test_scene_prompt_excludes_people():
    p = asset_sheet.build_reference_prompt(_row("永恒花园", "场景"))
    assert "no people" in p and "establishing wide shot" in p


def test_prop_prompt_isolates_object():
    p = asset_sheet.build_reference_prompt(_row("星光罗盘", "道具"))
    assert "product reference photo" in p and "neutral background" in p


def test_unknown_category_falls_back_to_generic():
    p = asset_sheet.build_reference_prompt(_row("飞艇", "载具"))
    assert "reference sheet" in p and "X, blue coat" in p


def test_style_extra_appended_and_negative_kept():
    p = asset_sheet.build_reference_prompt(_row("暮光闪闪", "角色"), "anime style, cel shading")
    assert "anime style, cel shading" in p
    assert asset_sheet.NEGATIVE_SUFFIX in p
    assert "no watermark" in p


# ---------- 产物展示视图（画布网格里的标签） ----------


def _asset(name: str = "", category: str = "", filename: str = "a.png") -> Asset:
    return Asset(
        kind="image", filename=filename, original_name=filename,
        content_type="image/png", size=1, name=name, category=category,
    )


def test_asset_view_labels_shot_number():
    """分镜图：标签是镜号，悬停说明补景别运镜与自动挂的参考图。"""
    view = canvas_runner.asset_view(
        _asset(),
        {"shot_no": "3", "shot_label": "镜头3（中景 · 缓慢推近 · 4s）", "injected_names": ["暮光闪闪", "云宝"]},
    )
    assert view["label"] == "镜头3"
    assert view["title"] == "镜头3（中景 · 缓慢推近 · 4s） · 参考：暮光闪闪、云宝"
    assert view["url"] == "/media/a.png"


def test_asset_view_labels_asset_name_and_category():
    """资产设定图：标签是资产名，悬停说明补类型。"""
    view = canvas_runner.asset_view(_asset("暮光闪闪", "角色"))
    assert view["label"] == "暮光闪闪"
    assert view["title"] == "暮光闪闪 · 角色"
    assert view["name"] == "暮光闪闪"


def test_asset_view_falls_back_to_filename():
    """文档 / 视频 / 单图等没有业务名的产物退回文件名，标签留空。"""
    view = canvas_runner.asset_view(_asset(filename="2026-09/abc.md"))
    assert view["label"] == ""
    assert view["title"] == "2026-09/abc.md"
    assert view["name"] == "2026-09/abc.md"


def test_asset_view_shot_without_extra_params():
    """只有镜号、没有景别说明时，标签与说明都用镜号，不能出现空串说明。"""
    view = canvas_runner.asset_view(_asset(), {"shot_no": "1A"})
    assert view["label"] == "镜头1A"
    assert view["title"] == "镜头1A"


# ---------- 节点契约与 Agent 种子 ----------


def test_asset_sheet_is_doc_node():
    assert is_doc_kind("assetSheet") is True
    assert "assetSheet" in DOC_NODE_KINDS
    schema = NODE_SCHEMAS["assetSheet"]
    assert schema["category"] == "document"
    assert schema["features"] == ["prompt", "modelSelect"]
    assert schema["handles"]["targets"][0]["type"] == "text"


def test_asset_image_node_contract():
    assert is_asset_image("assetImage") is True
    schema = NODE_SCHEMAS["assetImage"]
    assert schema["category"] == "image"
    assert "assetScope" in schema["features"]
    # 输入是资产表（文本），输出是图片
    assert schema["handles"]["targets"][0]["type"] == "text"
    assert schema["handles"]["sources"][0]["type"] == "image"


def test_asset_sheet_seed_forbids_chunking():
    agents = {row["key"]: row for row in SCHEMA_REGISTRY["agent_prompts"].seed}
    assert "assetSheet" in agents, list(agents)
    row = agents["assetSheet"]
    # 表格被拆成两半既难读也难解析，所以资产表固定单次成文
    assert not row["chunk_prompt"].strip()
    assert not row["chunk_param"].strip()
    assert not row["plan_prompt"].strip()
    assert "{content}" in row["user_template"] and "{params}" in row["user_template"]
    # system_prompt 必须把表格结构写死，否则解析不出来
    for token in ("中文名", "类型", "英文视觉描述", "出现场次"):
        assert token in row["system_prompt"], token


# ---------- 角色提及注入（需要库） ----------


async def _with_db(fn):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        await fn(db)
    await engine.dispose()


async def _add_asset(db, project_id: int, node_id: str, name: str, category: str, filename: str) -> Asset:
    task = Task(
        kind="image",
        status="completed",
        model="m",
        prompt="p",
        params_json="{}",
        canvas_project_id=project_id,
        canvas_node_id=node_id,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    asset = Asset(
        kind="image",
        filename=filename,
        original_name=filename,
        content_type="image/png",
        size=1,
        task_id=task.id,
        name=name,
        category=category,
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return asset


def test_mention_injection_by_exact_name():
    async def run(db):
        await _add_asset(db, 7, "n1", "暮光闪闪", "角色", "a.png")
        await _add_asset(db, 7, "n1", "云宝", "角色", "b.png")
        images, names = await canvas_runner._inject_asset_refs(db, 7, "暮光闪闪抬头望向天空", [])
        assert names == ["暮光闪闪"], names
        assert [a.filename for a in images] == ["a.png"]

    asyncio.run(_with_db(run))


def test_mention_injection_does_not_cross_projects():
    async def run(db):
        await _add_asset(db, 8, "n1", "暮光闪闪", "角色", "a.png")
        images, names = await canvas_runner._inject_asset_refs(db, 7, "暮光闪闪登场", [])
        assert images == [] and names == []

    asyncio.run(_with_db(run))


def test_mention_injection_prefers_longer_name():
    async def run(db):
        await _add_asset(db, 7, "n1", "小马", "角色", "short.png")
        await _add_asset(db, 7, "n1", "小马宝莉", "角色", "long.png")
        images, names = await canvas_runner._inject_asset_refs(db, 7, "小马宝莉站在门口", [])
        # 两个名字都能子串命中，同一个起点上长名优先
        assert names[0] == "小马宝莉", names
        assert images[0].filename == "long.png"

    asyncio.run(_with_db(run))


def test_mention_injection_caps_and_keeps_order():
    async def run(db):
        for i, n in enumerate(["甲", "乙", "丙", "丁"]):
            await _add_asset(db, 7, "n1", n, "角色", f"{i}.png")
        images, names = await canvas_runner._inject_asset_refs(db, 7, "丁 丙 乙 甲", [])
        assert len(images) == canvas_runner.MAX_MENTION_REFS
        # 按出现位置排序（不是按资产 id）
        assert names == ["丁", "丙", "乙"], names

    asyncio.run(_with_db(run))


def test_mention_injection_skips_existing_refs():
    async def run(db):
        first = await _add_asset(db, 7, "n1", "暮光闪闪", "角色", "a.png")
        await _add_asset(db, 7, "n1", "云宝", "角色", "b.png")
        images, names = await canvas_runner._inject_asset_refs(db, 7, "暮光闪闪与云宝", [first])
        assert names == ["云宝"], names
        assert [a.filename for a in images] == ["a.png", "b.png"]

    asyncio.run(_with_db(run))


def test_mention_injection_ignores_unnamed_and_non_image_assets():
    async def run(db):
        await _add_asset(db, 7, "n1", "", "角色", "unnamed.png")
        # 非图片产物即使有名字也不该被当参考图
        task = Task(kind="text", status="completed", model="m", prompt="p", params_json="{}",
                    canvas_project_id=7, canvas_node_id="n2")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        db.add(Asset(kind="document", filename="d.md", original_name="d.md", content_type="text/markdown",
                     size=1, task_id=task.id, name="剧本", category="文档"))
        await db.commit()
        images, names = await canvas_runner._inject_asset_refs(db, 7, "剧本 角色 文档", [])
        assert images == [] and names == [], (images, names)

    asyncio.run(_with_db(run))


def test_mention_injection_empty_text_is_noop():
    async def run(db):
        await _add_asset(db, 7, "n1", "暮光闪闪", "角色", "a.png")
        images, names = await canvas_runner._inject_asset_refs(db, 7, "   ", [])
        assert images == [] and names == []

    asyncio.run(_with_db(run))


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
