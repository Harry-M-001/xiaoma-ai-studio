"""#56 导航改造的前后端一致性检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_frontend_nav.py

这次动的是「用户每天看见的那一栏」，而它由**三个地方**共同决定，任何一处漏改都不是报错、
而是**静静地不一致**：

1. **后端种子**（`registry/schema_registry.py`）——新装的库照它建；
2. **数据迁移**（`alembic/versions/0017_*.py`）——已经在跑的库照它改。
   `ensure_seed` 只插缺行、**从不覆盖**，所以种子改了等于没改；
3. **前端兜底导航**（`App.tsx` 的 `DEFAULT_NAV`）——拿不到 nav 接口时照它渲染。

所以这里守四件事：

1. **折叠组三处一致**：`create` 组在后端种子、前端兜底、侧边栏渲染里说的是同一件事。
2. **改名三处一致**：「项目」→「自由画布」、「配音」→「音频生成」，种子 / 前端 / 迁移
   三处的旧值与新值必须对得上，否则老库升级后看到的名称与新装用户不一样。
3. **改动要尊重用户**：迁移里的改名与挪组都带条件（只在仍是旧默认值时动手）。
   这是最容易在后来被「顺手简化」掉的一条——省掉条件就是**抹掉用户自己改过的名字**。
4. **Agent 入口是完整的**：种子 / 兜底 / 路由白名单 / 页面分支 / 图标五处齐全，
   少一处就是点进去空白或者图标变成一个圆圈。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
BACKEND = ROOT / "backend"
MIGRATION = BACKEND / "alembic" / "versions" / "0017_nav_agent_group.py"

sys.path.insert(0, str(BACKEND))

from app.registry import schema_registry as registry  # noqa: E402

GROUP = "create"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _nav_seed() -> list[dict]:
    return next(s for s in registry.all_specs() if s.name == "nav_items").seed


def _seed_by_key() -> dict[str, dict]:
    return {item["key"]: item for item in _nav_seed()}


def _frontend_nav() -> dict[str, dict]:
    """把 `App.tsx` 的 `DEFAULT_NAV` 解析成 key → 行。"""
    app = _text(SRC / "App.tsx")
    block = re.search(r"const DEFAULT_NAV: NavMeta\[\] = \[(.*?)\n\];", app, re.S)
    assert block, "App.tsx 里找不到 DEFAULT_NAV"
    items: dict[str, dict] = {}
    for line in block.group(1).splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        row = dict(re.findall(r'(\w+):\s*"([^"]*)"', line))
        items[row["key"]] = row
    return items


# ---------- 1. 折叠组 ----------


def test_the_create_group_is_the_same_on_both_sides():
    """四个创作页在后端种子与前端兜底里都挂在 `create` 组下。"""
    seed = _seed_by_key()
    front = _frontend_nav()
    for key in ("chat", "image", "video", "speech"):
        assert seed[key]["group_name"] == GROUP, f"后端种子里 {key} 不在 {GROUP} 组：{seed[key]}"
        assert front[key]["group"] == GROUP, f"前端兜底里 {key} 不在 {GROUP} 组：{front[key]}"


def test_the_group_has_a_label_and_a_toggle():
    """组要有中文名、要能被收起、要有无障碍标记。"""
    sidebar = _text(SRC / "components" / "Sidebar.tsx")
    assert 'create: "创作台"' in sidebar, "折叠组没有中文名"
    assert "nav-group-toggle" in sidebar, "折叠组没有开关"
    assert "aria-expanded" in sidebar, "折叠组开关没有 aria-expanded"
    assert "prefs.navGroupOpen" in sidebar, "展开状态没有走 prefs"


def test_the_group_collapses_only_when_the_sidebar_has_labels():
    """图标态不做二次折叠。

    结构断言（`if (collapsed)` 必须早于渲染 `nav-group-toggle`）——这条最容易被
    「顺手统一」删掉，而后果是图标态的用户要为一个看不见的组名多点一次。
    """
    sidebar = _text(SRC / "components" / "Sidebar.tsx")
    collapsed_at = sidebar.find("if (collapsed) {")
    toggle_at = sidebar.find("nav-group-toggle")
    assert collapsed_at != -1, "Sidebar 里没有图标态的分支"
    assert toggle_at != -1, "Sidebar 里没有折叠组的开关"
    assert collapsed_at < toggle_at, "图标态分支必须早于折叠组开关"


def test_the_sidebar_can_scroll_to_its_last_item():
    """侧边栏必须能滚——这是「够不着」而不是「不好看」。

    实测：658px 高的窗口里，侧边栏内容高 811px，而 `overflow-y` 是 `visible`、
    外层 `.app-shell` 是 `hidden`，于是「系统设置」的顶边落在 721px、**点都点不到**。
    折叠组只是让这件事少发生几次，真正要治的是不给滚。
    """
    css = _text(SRC / "styles.css")
    rule = re.search(r"^\.sidebar \{(.*?)^\}", css, re.S | re.M)
    assert rule, "styles.css 里找不到 .sidebar 规则"
    assert "overflow-y: auto" in rule.group(1), "侧边栏不能滚动（底部入口会被裁掉）"


def test_the_group_sits_right_under_home_and_is_collapsed_by_default():
    """折叠组固定在**首页下面**（用户 2026-09-25 指定），且**默认收起**。

    结构断言：渲染折叠组那一段必须夹在「首页」与「其余项」之间——用「主分组拆成
    `headItem` / `tailItems`」来表达，而不是给 `home` 这个 key 写死一个特例，
    这样首页被关掉时它自然上移到第一位。

    「默认收起」靠 `?? false`：去掉它，侧边栏一进来又是十几行，这一组就白做了。
    """
    sidebar = _text(SRC / "components" / "Sidebar.tsx")
    assert "const [headItem, ...tailItems] = mainItems;" in sidebar, (
        "没有把主分组拆成「首页」与「其余」"
    )
    head_at = sidebar.index("{headItem &&")
    group_at = sidebar.index('groupKey="create"')
    tail_at = sidebar.index("{tailItems.map(")
    assert head_at < group_at < tail_at, "折叠组没有排在首页下面"
    assert "prefs.navGroupOpen.get(groupKey) ?? false" in sidebar, "折叠组默认不是收起"


# ---------- 2. 改名三处一致 ----------


def test_the_rename_matches_across_seed_frontend_and_migration():
    """「项目」→「自由画布」、「配音」→「音频生成」：三处说的必须是同一件事。"""
    seed = _seed_by_key()
    front = _frontend_nav()
    migration = _text(MIGRATION)

    # 迁移里那张 key → (旧, 新) 的表
    pairs = {
        key: (old, new)
        for key, old, new in re.findall(r'"(\w+)": \("([^"]*)", "([^"]*)"\)', migration)
    }
    assert pairs, "迁移里没有改名表"

    for key, (old, new) in pairs.items():
        assert seed[key]["label"] == new, f"种子里 {key} 是「{seed[key]['label']}」，迁移说是「{new}」"
        assert front[key]["label"] == new, f"前端兜底里 {key} 是「{front[key]['label']}」"
        assert old != new, f"{key} 的旧值与新值一样，这条迁移是空转"

    # 两处改名都必须在表里，少一个就是只改了一半
    assert pairs.get("projects") == ("项目", "自由画布"), pairs
    assert pairs.get("speech") == ("配音", "音频生成"), pairs


def test_the_canvas_pages_no_longer_call_it_a_project():
    """画布相关的界面文案不许再出现「项目」——改一半比不改更糟（两套说法并存）。

    `EnginesPage` 里的「项目主页」指的是上游仓库主页，是另一个意思，所以不在检查范围。
    """
    stale = [
        "项目列表加载失败",
        "请输入项目名称",
        "项目已创建",
        "新建项目",
        "还没有项目",
        "项目名称",
        "重命名项目",
        "最近项目",
        "全部项目",
        '删除项目「',
        "每个项目内置一张可视化画布",
    ]
    for name in ("pages/ProjectsPage.tsx", "pages/HomePage.tsx"):
        text = _text(SRC / name)
        left = [s for s in stale if s in text]
        assert not left, f"{name} 里还留着旧文案：{left}"

    projects = _text(SRC / "pages" / "ProjectsPage.tsx")
    assert "自由画布" in projects and "新建画布" in projects, "画布页没改成新说法"
    home = _text(SRC / "pages" / "HomePage.tsx")
    assert "最近画布" in home and "新建画布" in home, "首页没改成新说法"


# ---------- 3. 迁移要尊重用户 ----------


def test_the_migration_respects_what_the_user_changed():
    """改名与挪组都必须带条件：只在仍是旧默认值时动手。

    没有这两个条件，一次升级就会把用户自己改过的名称与位置全部抹掉。
    """
    migration = _text(MIGRATION)
    assert "AND label = :old" in migration, "改名没有加条件（会覆盖用户改过的名称）"
    assert "group_name = 'main'" in migration, "挪组没有加条件（会覆盖用户调过的分组）"
    assert "WHERE key = :key" in migration, "改名/挪组没有限定到具体某一行"


def test_the_migration_only_inserts_the_agent_row_once():
    """插入必须幂等：重复跑不该插出第二行 Agent。"""
    migration = _text(MIGRATION)
    assert "SELECT COUNT(*) FROM nav_items WHERE key = :key" in migration, "插入前没有查重"
    assert "INSERT INTO nav_items" in migration, "迁移里没有插入 Agent 行"
    assert "def downgrade" in migration and "DELETE FROM nav_items" in migration, "缺少可回退的 downgrade"
    assert 'down_revision = "0016_engines_nav"' in migration, "迁移链接错了"


def test_the_migration_does_not_touch_sort_order():
    """`sort_order` 刻意不动：组内只要相对顺序对就够，重排会牵动别的用例却没有收益。"""
    migration = _text(MIGRATION)
    assert "sort_order = " not in migration, "迁移里改了 sort_order（本次不该动它）"


# ---------- 4. Agent 入口的完整性 ----------


def test_the_agent_entry_is_wired_everywhere():
    """种子 / 兜底 / 路由白名单 / 页面分支 / 页面文件 / 图标，六处缺一不可。"""
    seed = _seed_by_key()
    assert "agent" in seed, "后端种子里没有 Agent"
    assert seed["agent"]["route"] == "agent", seed["agent"]
    assert seed["agent"]["group_name"] == "main", "Agent 入口不该被塞进折叠组"

    front = _frontend_nav()
    assert "agent" in front, "前端兜底导航里没有 Agent"

    app = _text(SRC / "App.tsx")
    known = re.search(r"const KNOWN_ROUTES = new Set\(\[(.*?)\]\)", app, re.S)
    assert known and '"agent"' in known.group(1), "路由白名单里没有 agent（会被当成未知路由）"
    assert 'import AgentPage from "./pages/AgentPage"' in app, "没有引入 Agent 页"
    assert re.search(r'case "agent":\s*\n\s*return <AgentPage', app), "路由没有分支到 Agent 页"

    page = SRC / "pages" / "AgentPage.tsx"
    assert page.exists(), "AgentPage.tsx 不存在"


def test_every_nav_icon_name_can_be_resolved():
    """种子里的图标名必须都在 `IconMap` 里——写错一个名是**静默**的（回落成圆圈）。"""
    icon_map = _text(SRC / "components" / "IconMap.tsx")
    known = set(re.findall(r"^\s*(\w+):\s*\w+,", icon_map, re.M))
    used = {item.get("icon", "") for item in _nav_seed()}
    missing = sorted(i for i in used if i and i not in known)
    assert not missing, f"这些图标名在 IconMap 里没有：{missing}"


def test_the_group_field_offers_the_new_value():
    """分组是 select：不给 `create` 这个选项，后台就改不回这一组。"""
    spec = next(s for s in registry.all_specs() if s.name == "nav_items")
    field = next(f for f in spec.fields if f.name == "group_name")
    assert GROUP in field.options, f"group_name 的可选值里没有 {GROUP}：{field.options}"


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
