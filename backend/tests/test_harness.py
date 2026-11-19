"""DeepSeek Harness 可选组件：路径、版本、MCP 补丁、Key 选择与状态（直接 python 运行）。

不真的下载 Node / 安装 dsh（那要几十 MB 与网络），只钉住**可离线验证的边界**：
配置写对没有、Key 挑对没有、没装时状态是否如实、前端有没有挂上入口。
"""

from __future__ import annotations

import asyncio
import tempfile
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app import security  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import ProviderService  # noqa: E402
from app.services import harness  # noqa: E402

ROOT = BACKEND.parent
SRC = ROOT / "frontend" / "src"


def test_parse_version():
    assert harness._parse_version("v24.18.1") == (24, 18, 1)
    assert harness._parse_version("24.9.0") == (24, 9, 0)
    assert harness._parse_version("") == ()


def test_profile_patch_uses_insert_and_points_at_mcp():
    """MCP 必须写在 `- insert:` 下：顶层同 id 覆盖会静默不生效（试点踩过）。"""
    patch = harness.profile_patch()
    assert "- insert:" in patch
    assert "serverName: xms" in patch
    assert "transport: streamable-http" in patch
    assert "/api/mcp" in patch
    assert "toolCallTimeoutMs: 120000" in patch


def test_write_profile_patch_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        old_home = harness.home
        harness.home = lambda: Path(tmp)  # type: ignore[assignment]
        try:
            harness._write_profile_patch()
            harness._write_profile_patch()
        finally:
            harness.home = old_home  # type: ignore[assignment]
        text = (Path(tmp) / "profiles" / "web" / "cordis.patch.yml").read_text(encoding="utf-8")
        assert text.count("serverName: xms") == 1, "重复安装会把补丁叠加两遍"


def test_only_the_official_deepseek_service_provides_the_key():
    async def scenario(maker):
        async with maker() as db:
            db.add(ProviderService(
                name="中转站", kind="openai", base_url="https://example.com/v1",
                api_key_enc=security.encrypt("sk-wrong"), models_json="[]", enabled=True,
            ))
            db.add(ProviderService(
                name="DeepSeek 官方", kind="deepseek", base_url="https://api.deepseek.com",
                api_key_enc=security.encrypt("sk-right"), models_json="[]", enabled=True,
            ))
            await db.commit()
            assert await harness.deepseek_key(db) == "sk-right"

            row = (await db.execute(
                select(ProviderService).where(ProviderService.name == "DeepSeek 官方")
            )).scalars().one()
            row.enabled = False
            await db.commit()
            assert await harness.deepseek_key(db) == "", "停用的服务不能继续供 Key"

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            await scenario(maker)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_status_is_honest_when_nothing_is_installed():
    async def scenario(maker):
        old = (harness.find_node, harness.dsh_bin, harness._probe, harness.get_workspace)
        missing = Path(tempfile.gettempdir()) / "xms-no-dsh" / "bin.js"

        async def no_node():
            return None, ""

        async def down(_url, timeout=1.5):
            return False

        harness.find_node = no_node  # type: ignore[assignment]
        harness.dsh_bin = lambda: missing  # type: ignore[assignment]
        harness._probe = down  # type: ignore[assignment]
        harness.get_workspace = lambda: ""  # type: ignore[assignment]
        try:
            async with maker() as db:
                st = await harness.status(db)
        finally:
            (harness.find_node, harness.dsh_bin,
             harness._probe, harness.get_workspace) = old  # type: ignore[assignment]
        assert st["node"]["ready"] is False
        assert st["dsh"]["installed"] is False
        assert st["running"] is False
        assert st["modelReady"] is False
        assert st["workspace"]["ready"] is False
        assert "工作区" in st["workspace"]["reason"]
        assert "只写工作区" in st["permission"]
        assert st["dsh"]["pinned"] == harness.DSH_VERSION

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def run():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            await scenario(maker)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_workspace_validation_and_roundtrip():
    from app.config import settings

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "my-project"
        target.mkdir()
        assert harness.validate_workspace(str(target)) == target.resolve()

        # 相对路径、不存在、数据目录、仓库根、磁盘根都不行
        for bad in ("relative/path", str(Path(tmp) / "missing"),
                    str(settings.data_dir), str(harness.repo_root()), str(Path(tmp).anchor)):
            try:
                harness.validate_workspace(bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"应当拒绝工作区：{bad}")

        # 保存的值能读回来，且写的是解析后的绝对路径
        old_root = harness.root
        harness.root = lambda: Path(tmp) / "harness"  # type: ignore[assignment]
        try:
            (Path(tmp) / "harness").mkdir()
            saved = harness.set_workspace(str(target))
            assert saved == str(target.resolve())
            assert harness.get_workspace() == saved
        finally:
            harness.root = old_root  # type: ignore[assignment]


def test_dir_browser_lists_only_visible_subdirectories():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "work").mkdir()
        (root / "B-roll").mkdir()
        (root / "note.txt").write_text("x", encoding="utf-8")
        (root / ".hidden").mkdir()

        data = harness.list_dirs(str(root))
        assert data["path"] == str(root.resolve())
        assert data["roots"] is False
        assert [e["name"] for e in data["entries"]] == ["B-roll", "work"]
        assert data["parent"] == str(root.resolve().parent)

        try:
            harness.list_dirs(str(root / "missing"))
        except ValueError as e:
            assert "不存在" in str(e)
        else:
            raise AssertionError("不存在的目录应当报错")

    roots = harness.list_dirs("")
    assert roots["roots"] is True
    if sys.platform == "win32":
        assert roots["entries"], "Windows 下应当能列出盘符"
        assert all(e["path"].endswith(":\\") for e in roots["entries"])


def test_frontend_has_the_optional_component_card():
    page = (SRC / "pages" / "EnginesPage.tsx").read_text(encoding="utf-8")
    api = (SRC / "api.ts").read_text(encoding="utf-8")
    picker = (SRC / "components" / "HarnessDirPicker.tsx").read_text(encoding="utf-8")
    assert "function HarnessCard" in page and "<HarnessCard />" in page
    assert "harnessStatus:" in api and "harnessStart:" in api and "harnessStop:" in api
    assert "harnessSetWorkspace:" in api and "harnessDirs:" in api
    assert "工作区（dsh 只能写这个目录）" in page, "界面没有让用户选工作区"
    assert "浏览…" in page, "工作区没有浏览入口"
    assert "<HarnessDirPicker" in page and "api.harnessDirs(" in picker
    assert "不装、不启动时，原 Agent 完全不受影响" in page, "界面要说清它与原 Agent 的边界"

    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "MIT License" in notices and "Copyright (c) 2026 DeepSeek" in notices
    assert "Node.js" in notices


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
