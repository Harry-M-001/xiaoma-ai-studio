"""#28 分镜静态体检（接线部分）的回归测试（直接 python 运行）。

运行：venv/Scripts/python tests/test_canvas_lint.py

纯规则的用例在 `test_storyboard_lint.py`；这里只测**接线**——它才是容易出错的地方：

- 体检该看哪些节点、内容从哪来（只要 手改正文 / 已生成正文；`prompt` 是指令不算内容）；
- 分镜节点解析不出镜头表要**如实报**（下游逐镜出图会直接失败），
  而小说/剧本节点解析不出镜头表是正常的，要**静默跳过**（否则满屏噪音）；
- 全程**只读**：不建任务、不改画布；
- 输出形状与生成前的 preflight 一致（`blocking` 恒 false），前端才能复用同一个组件。
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
from app.models import Project, Task  # noqa: E402
from app.services import canvas_runner  # noqa: E402

# 六个「中景 + 固定」且全是套话词的分镜：景别、运镜、AI 腔三条规则都会命中
BAD_SHEET = """### 镜头1 | 中景 | 固定 | 3s
- 画面：他仿佛在等谁
### 镜头2 | 中景 | 固定 | 3s
- 画面：他顿时愣住
### 镜头3 | 中景 | 固定 | 3s
- 画面：他宛如听见了什么
### 镜头4 | 中景 | 固定 | 3s
- 画面：他仿佛又不确定
### 镜头5 | 中景 | 固定 | 3s
- 画面：他顿时明白
### 镜头6 | 中景 | 固定 | 3s
- 画面：他宛如回到从前
"""

SHEET = """## 场景1 | 黄昏的花园

### 镜头1 | 全景 | 缓慢推近 | 4s
- 画面：小焰站在花园中央，缓缓抬头
- 首帧提示词：A lone figure standing in a crystal garden, warm sunset light

### 镜头2 | 中景 | 固定 | 3s
- 画面：阿蓝从树后探出头
- 首帧提示词：A second figure peeking from behind a tree, soft backlight

### 镜头3 | 近景 | 固定 | 3s
- 画面：小焰的眼睫轻轻一颤
- 首帧提示词：Close up of a figure blinking slowly, shallow depth of field

### 镜头4 | 大远景 | 升降 | 8s
- 画面：整座花园在暮色里亮起灯
- 首帧提示词：Aerial rise over the whole garden, lanterns lighting up at dusk
"""

NOVEL = "第一章　风起\n\n" + "他站在门口，看着远方的云。\n" * 40


def _run(fn):  # noqa: ANN001
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    real = canvas_runner.SessionLocal

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        canvas_runner.SessionLocal = maker
        try:
            await fn(maker)
        finally:
            canvas_runner.SessionLocal = real
            await engine.dispose()

    asyncio.run(scenario())


async def _new_project(maker, nodes: list[dict]) -> int:  # noqa: ANN001
    doc = {"schemaVersion": 1, "nodes": nodes, "edges": [], "viewport": {}}
    async with maker() as db:
        p = Project(name="体检测试", canvas_json=json.dumps(doc, ensure_ascii=False))
        db.add(p)
        await db.commit()
        await db.refresh(p)
        return p.id


def _doc_node(nid: str, ntype: str, text: str = "", prompt: str = "") -> dict:
    return {"id": nid, "type": ntype, "data": {"docText": text, "prompt": prompt}}


async def _task_count(maker) -> int:  # noqa: ANN001
    async with maker() as db:
        row = await db.execute(select(func.count()).select_from(Task))
        return int(row.scalar_one())


async def _canvas_json(maker, project_id: int) -> str:  # noqa: ANN001
    async with maker() as db:
        return (await db.get(Project, project_id)).canvas_json


# ---------------------------------------------------------------- 用例


def test_lints_storyboard_node_body():
    async def main(maker):  # noqa: ANN001
        pid = await _new_project(maker, [_doc_node("sb1", "storyboard", SHEET)])
        report = await canvas_runner.lint_graph(pid)
        assert report["shotTotal"] == 4, f"镜头数不对：{report['shotTotal']}"
        assert len(report["nodes"]) == 1 and report["nodes"][0]["id"] == "sb1"
        summary = report["nodes"][0]["summary"]
        assert summary["shots"] == 4 and summary["sizes"]["全景"] == 1
        # 这四镜景别/运镜都有变化、字段齐全 → 不该有任何 warn
        assert not [w for w in report["warnings"] if w["level"] == "warn"], report["warnings"]

    _run(main)


def test_node_prompt_is_not_treated_as_content():
    """`prompt` 是「生成要求」，不是内容。

    真实项目上踩过的坑：一个还没跑过分镜节点，输入框里写着 126 字的生成要求，
    体检却对着这段指令报「解析不出镜头表，下游会直接失败」——纯属误报。
    """

    async def main(maker):  # noqa: ANN001
        instruction = "请按三幕结构把小说改写成分镜：每镜不超过 4 秒，运镜要有变化，避免连续固定镜头。"
        node = {"id": "sb1", "type": "storyboard", "data": {"docText": "", "prompt": instruction}}
        pid = await _new_project(maker, [node])
        report = await canvas_runner.lint_graph(pid)
        codes = {w["code"] for w in report["warnings"]}
        assert "unparsable_storyboard" not in codes, f"把生成要求当内容了：{report['warnings']}"
        assert report["warnings"] == [], report["warnings"]
        assert report["shotTotal"] == 0
        # 如实记成「还没内容」，而不是「没问题」
        assert [s["id"] for s in report["skipped"]] == ["sb1"], report["skipped"]

    _run(main)


def test_unparsable_storyboard_is_reported():
    """分镜节点解析不出镜头表 → 必须报（下游逐镜出图会直接失败）。"""

    async def main(maker):  # noqa: ANN001
        pid = await _new_project(maker, [_doc_node("sb1", "storyboard", "这就是一段散文，没有镜头表。")])
        report = await canvas_runner.lint_graph(pid)
        codes = {w["code"] for w in report["warnings"]}
        assert "unparsable_storyboard" in codes, report["warnings"]
        assert report["shotTotal"] == 0
        assert report["nodes"] == []

    _run(main)


def test_non_storyboard_doc_is_silently_skipped():
    """小说节点解析不出镜头表是正常的 → 不许报，也不许出现在 nodes 里。"""

    async def main(maker):  # noqa: ANN001
        pid = await _new_project(
            maker,
            [
                _doc_node("nv1", "novel", NOVEL),
                _doc_node("sb1", "storyboard", SHEET),
            ],
        )
        report = await canvas_runner.lint_graph(pid)
        assert [n["id"] for n in report["nodes"]] == ["sb1"], report["nodes"]
        assert all("novel" not in w["message"] and "小说" not in w["message"] for w in report["warnings"])
        assert report["skipped"] == [], "小说节点不该被记成「跳过」（它本来就不是镜头表）"

    _run(main)


def test_empty_storyboard_node_is_listed_as_skipped():
    """分镜节点还没内容 → 如实说明，而不是当成「没问题」。"""

    async def main(maker):  # noqa: ANN001
        pid = await _new_project(maker, [_doc_node("sb1", "storyboard")])
        report = await canvas_runner.lint_graph(pid)
        assert len(report["skipped"]) == 1
        assert report["skipped"][0]["id"] == "sb1"
        assert "还没有内容" in report["skipped"][0]["reason"]
        assert report["warnings"] == []

    _run(main)


def test_shot_table_inside_other_doc_kinds_is_still_linted():
    """镜头表放在剧本节点里也能体检（按内容自选，不按节点类型硬判）。"""

    async def main(maker):  # noqa: ANN001
        pid = await _new_project(maker, [_doc_node("sc1", "script", SHEET)])
        report = await canvas_runner.lint_graph(pid)
        assert report["shotTotal"] == 4, "剧本节点里的镜头表没被体检到"

    _run(main)


def test_lint_is_read_only():
    """只读：不建任务、不改画布。"""

    async def main(maker):  # noqa: ANN001
        pid = await _new_project(maker, [_doc_node("sb1", "storyboard", SHEET)])
        before_tasks = await _task_count(maker)
        before_doc = await _canvas_json(maker, pid)
        await canvas_runner.lint_graph(pid)
        await canvas_runner.lint_graph(pid)
        assert await _task_count(maker) == before_tasks, "体检建了任务（应当零成本）"
        assert await _canvas_json(maker, pid) == before_doc, "体检改了画布"

    _run(main)


def test_json_keys_are_a_stable_contract():
    """字段名是前后端的契约：前端按名字读，改名就等于弹窗空白。

    所以这里把键**写死**——真要改名时，这个用例会先红，提醒你前端也要跟着改。
    """

    async def main(maker):  # noqa: ANN001
        pid = await _new_project(maker, [_doc_node("sb1", "storyboard", BAD_SHEET)])
        report = await canvas_runner.lint_graph(pid)
        assert set(report) == {"blocking", "warnings", "nodes", "skipped", "shotTotal"}, set(report)
        node = report["nodes"][0]
        assert set(node) == {"id", "type", "label", "summary", "findings"}
        assert set(node["summary"]) == {"shots", "sizes", "moves", "scenes", "totalSeconds"}
        assert node["findings"], "这份分镜本该有 findings，键名契约就无从校验了"
        assert set(node["findings"][0]) == {"code", "level", "message", "suggestion", "shots"}

    _run(main)


def test_report_shape_matches_preflight():
    """形状与生成前的 preflight 一致：前端复用同一个组件。"""

    async def main(maker):  # noqa: ANN001
        pid = await _new_project(maker, [_doc_node("sb1", "storyboard", BAD_SHEET)])
        report = await canvas_runner.lint_graph(pid)
        assert report["blocking"] is False, "体检必须只告警不阻断"
        assert report["warnings"], "这份分镜应该至少报出景别单调"
        for w in report["warnings"]:
            assert set(w) == {"code", "level", "message", "suggestion"}, w
            assert w["level"] in ("warn", "info")
            assert w["message"].startswith("「分镜」"), w["message"]
        levels = [w["level"] for w in report["warnings"]]
        assert levels == sorted(levels, key=lambda x: x != "warn"), "warn 应该排在前面"
        codes = {w["code"] for w in report["warnings"]}
        assert "size_single_kind" in codes and "ai_slop" in codes, codes

    _run(main)


def test_missing_project_raises():
    async def main(maker):  # noqa: ANN001
        try:
            await canvas_runner.lint_graph(999999)
        except ValueError as e:
            assert "画布不存在" in str(e)
            return
        raise AssertionError("不存在的项目应该抛 ValueError")

    _run(main)


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
