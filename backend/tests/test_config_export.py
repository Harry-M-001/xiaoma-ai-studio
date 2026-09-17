"""配置导入导出（直接 python 运行）。

运行：venv/Scripts/python tests/test_config_export.py

这里钉的是四条产品底线，不是「代码能跑」：

1. **密钥永不外流**：provider_services 的 api_key_enc 与明文 api_key
   既不出现在导出内容里，导入侧也拒收（连密文都不碰）。
2. **本机参数不跟着走**：排除表里那几张表写死的键（绝对路径 / 本机地址 /
   本机自增编号）都不进快照，并且导出响应里能看出「丢了什么、为什么」。
3. **预览就是预览，幂等就是幂等**：dry-run 一行都不写；同一份快照导两次
   不产生重复行、不刷版本号、不再长审计。
4. **脏数据只脏它自己**：结构错了直接拒绝并说清原因，单行脏了只拒那一行，
   任何情况下都不让接口 500。

每个用例跑在一个全新的内存 SQLite 上（先灌种子，再按需加数据），
互不干扰，也不会碰到本机的 backend/data。
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
from app.models import ConfigAuditLog, DirectorStyle, Prompt, ProviderService  # noqa: E402
from app.registry.schema_registry import SCHEMA_REGISTRY  # noqa: E402
from app.schemas import ModelSpec, ProviderIn  # noqa: E402
from app.services import config_center_service as cc  # noqa: E402
from app.services import config_transfer_service as ct  # noqa: E402
from app.services import provider_store  # noqa: E402

# 只在代码里读、种子里没有的键（排除表里也收了它，理由写在那边）
CODE_ONLY_CONFIG_KEYS = {"update.in_docker"}


# ============================================================
# 测试脚手架
# ============================================================


def _run(fn):
    """在全新的空库（内存 SQLite）上跑一个用例。"""

    async def main():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as db:
                await cc.ensure_seed(db)  # 新装机器也是这个状态：只有种子数据
                return await fn(db)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _run_empty(fn):
    """连种子都不灌的库（用来验证「导入确实写进了空库」）。"""

    async def main():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as db:
                return await fn(db)
        finally:
            await engine.dispose()

    return asyncio.run(main())


async def _count(db, model) -> int:
    return int((await db.execute(select(func.count()).select_from(model))).scalar() or 0)


async def _audit_count(db, table: str) -> int:
    return int(
        (
            await db.execute(
                select(func.count()).select_from(ConfigAuditLog).where(ConfigAuditLog.table_name == table)
            )
        ).scalar()
        or 0
    )


async def _add_remote_service(db, name: str = "OpenAI 官方", key: str = "sk-proj-test1234567890") -> ProviderService:
    """加一个「公网地址」的服务（导出时应当保留，但不带 Key）。"""
    await provider_store.create(
        db,
        ProviderIn(
            name=name,
            kind="openai",
            base_url="https://api.openai.com/v1",
            api_key=key,
            models=[ModelSpec(name="gpt-4o", modality="text", label="GPT-4o")],
        ),
    )
    rows = [r for r in await provider_store.list_services(db) if r.base_url == "https://api.openai.com/v1"]
    return rows[0]


async def _add_local_service(db, name: str = "本地 Ollama") -> ProviderService:
    """加一个「本机地址」的服务（导出时应当整行跳过）。"""
    await provider_store.create(
        db,
        ProviderIn(
            name=name,
            kind="openai",
            base_url="http://127.0.0.1:11434/v1",
            api_key="",
            models=[ModelSpec(name="qwen2.5", modality="text", label="")],
        ),
    )
    rows = [r for r in await provider_store.list_services(db) if "11434" in r.base_url]
    return rows[0]


async def _expect_import_error(snapshot, db, *fragments):
    """断言这次导入被拒绝，且理由里提到了关键信息。"""
    try:
        await ct.import_snapshot(db, snapshot, dry_run=True)
    except cc.ConfigError as e:
        for frag in fragments:
            assert frag in e.message, f"拒绝理由里应提到「{frag}」，实际：{e.message}"
        return e
    raise AssertionError(f"这个快照应当被拒绝（期望提到 {fragments}），却通过了校验")


def _keys_of(snapshot: dict, table: str) -> list[str]:
    return [str(row.get("key")) for row in snapshot["tables"].get(table, [])]


# ============================================================
# 一、绝不导出密钥
# ============================================================


def test_export_never_contains_api_key():
    async def case(db):
        row = await _add_remote_service(db, key="sk-proj-SUPERSECRET-1234567890")
        assert row.api_key_enc, "前置条件：服务应当存了加密后的 Key"

        snapshot = await ct.export_snapshot(db, "services")
        text = json.dumps(snapshot, ensure_ascii=False)
        rows_text = json.dumps(snapshot["tables"], ensure_ascii=False)

        # 密文与明文：整份文件里都不许出现（连说明文字里都不许有）
        assert row.api_key_enc not in text, "导出内容里出现了 API Key 的密文"
        assert "sk-proj-SUPERSECRET" not in text, "导出内容里出现了明文 API Key"
        # 数据行里不许出现密钥相关字段（excluded 里提到字段名是「说明」，不是数据）
        assert "api_key_enc" not in rows_text, "数据行里出现了 api_key_enc"
        assert "api_key" not in rows_text, "数据行里出现了 api_key"
        assert "has_api_key" not in rows_text, "数据行里出现了 has_api_key"

        assert snapshot["containsSecrets"] is False, snapshot["containsSecrets"]
        services = snapshot["tables"]["provider_services"]
        assert len(services) == 1, services
        assert set(services[0]) == {"name", "kind", "base_url", "enabled", "sort_order", "models"}, services[0]

        # 排除说明里写清了「为什么不带 Key」，用户看得见
        # （说明里点名 api_key_enc / api_key 是刻意的：要让人知道这两个字段被丢了）
        documented = " ".join(f"{e['field']} {e['reason']}" for e in snapshot["excluded"])
        assert "api_key_enc" in documented and "密钥" in documented, documented

    _run(case)


def test_import_rejects_rows_carrying_a_key():
    async def case(db):
        await _add_remote_service(db, key="sk-proj-KEEPME-1234567890")
        before = (await provider_store.list_services(db))[0]

        # 手工拼一份「带 Key 的快照」——就像有人想把自己的 Key 分享给朋友
        snapshot = await ct.export_snapshot(db, "services")
        snapshot["tables"]["provider_services"][0]["api_key_enc"] = "gAAAAA-fake-ciphertext"
        snapshot["tables"]["provider_services"][0]["name"] = "被改名的服务"

        preview = await ct.import_snapshot(db, snapshot, dry_run=True)
        assert preview["totals"]["created"] == 0 and preview["totals"]["updated"] == 0, preview["totals"]
        assert len(preview["conflicts"]) == 1, preview["conflicts"]
        reason = preview["conflicts"][0]["reason"]
        assert "api_key_enc" in reason, reason
        assert "拒收" in reason, reason

        after = (await provider_store.list_services(db))[0]
        assert after.name == before.name, "带了 Key 的行不该被写入（连名字都不该改）"
        assert after.api_key_enc == before.api_key_enc, "已存好的 Key 不该被动过"

    _run(case)


def test_import_keeps_the_local_machines_own_key():
    """同地址的服务：导入只更新清单，绝不清空本机已有的 Key。"""

    async def case(db):
        row = await _add_remote_service(db, key="sk-proj-LOCALKEY-1234567890")
        cipher = row.api_key_enc

        snapshot = await ct.export_snapshot(db, "services")
        snapshot["tables"]["provider_services"][0]["name"] = "改名后的服务"
        snapshot["tables"]["provider_services"][0]["models"] = [
            {"name": "gpt-4o", "modality": "text", "label": "GPT-4o"},
            {"name": "gpt-image-1", "modality": "image", "label": "GPT Image"},
        ]

        result = await ct.import_snapshot(db, snapshot, dry_run=False)
        assert result["totals"] == {"created": 0, "updated": 1, "skipped": 0}, result["totals"]

        db.expire_all()
        after = (await provider_store.list_services(db))[0]
        assert after.name == "改名后的服务", after.name
        assert after.api_key_enc == cipher, "导入把本机已保存的 Key 弄丢了"
        assert len(provider_store.parse_models(after.models_json)) == 2, after.models_json

    _run(case)


def test_service_audit_never_records_key_material():
    async def case(db):
        row = await _add_remote_service(db, key="sk-proj-AUDITKEY-1234567890")
        cipher = row.api_key_enc

        snapshot = await ct.export_snapshot(db, "services")
        snapshot["tables"]["provider_services"].append(
            {"name": "新服务", "kind": "openai", "base_url": "https://api.example.com/v1",
             "enabled": True, "sort_order": 9, "models": []}
        )
        await ct.import_snapshot(db, snapshot, dry_run=False)

        logs = list((await db.execute(select(ConfigAuditLog))).scalars().all())
        blob = json.dumps([{"b": log.before_json, "a": log.after_json} for log in logs], ensure_ascii=False)
        assert cipher not in blob, "审计记录里出现了密钥密文"
        assert "sk-proj-AUDITKEY" not in blob, "审计记录里出现了明文 Key"
        assert any(log.table_name == "provider_services" and log.actor == "import" for log in logs), logs

    _run(case)


# ============================================================
# 二、本机相关参数不跟着走
# ============================================================


def test_export_excludes_machine_bound_config_keys():
    async def case(db):
        snapshot = await ct.export_snapshot(db, "settings")
        exported = _keys_of(snapshot, "config_items")

        for key in ct.MACHINE_BOUND_CONFIG_KEYS:
            assert key not in exported, f"本机参数「{key}」被导出了"

        reasons = " ".join(e["reason"] for e in snapshot["excluded"])
        for key in ct.MACHINE_BOUND_CONFIG_KEYS:
            if key in {row["key"] for row in SCHEMA_REGISTRY["config_items"].seed}:
                assert key in reasons, f"排除说明里没交代「{key}」为什么被丢下"
        assert "ffmpeg" in reasons, reasons

        # 可以搬走的偏好照常导出：站点名、模块开关、更新源
        assert "app.name" in exported, exported
        assert "modules.chat" in exported, exported
        assert "update.repo" in exported, exported

        # 导出响应自己说明「本机参数排除了哪些」
        notes = " ".join(snapshot["notes"])
        assert "limits.ffmpeg_path" in notes, notes
        assert "API Key" in notes, notes

    _run(case)


def test_exclusion_table_only_lists_real_machine_bound_keys():
    """排除表是「查过一遍」的结论，不是随手写的：这条盯住它的依据。"""
    seed_keys = {row["key"] for row in SCHEMA_REGISTRY["config_items"].seed}
    for key in ct.MACHINE_BOUND_CONFIG_KEYS:
        assert key in seed_keys or key in CODE_ONLY_CONFIG_KEYS, (
            f"排除表里的「{key}」既不在种子键里、也不是代码内读写的键，"
            "要么是写错了，要么该补进 CODE_ONLY_CONFIG_KEYS 并写清依据"
        )
    # 每一个排除项都得写清理由，且理由不能是空话
    for key, reason in ct.MACHINE_BOUND_CONFIG_KEYS.items():
        assert len(reason) >= 15, f"「{key}」的排除理由太含糊：{reason}"


def test_no_other_seed_value_looks_machine_bound():
    """查证的结论要能被复现：剩下的种子值里没有本机路径 / 本机地址。

    以后有人往种子里加一个绝对路径或 127.0.0.1 地址，这条会红——
    逼着他决定「这是本机参数吗」，而不是让默认值悄悄跟着快照跑到别人机器上。
    """
    for row in SCHEMA_REGISTRY["config_items"].seed:
        if row["key"] in ct.MACHINE_BOUND_CONFIG_KEYS:
            continue
        hint = ct._machine_hint(str(row.get("value") or ""))
        assert not hint, (
            f"「{row['key']}」的默认值看起来是{hint}：{row.get('value')!r}；"
            "要么加进 MACHINE_BOUND_CONFIG_KEYS，要么改掉这个默认值"
        )


def test_export_skips_services_pointing_at_this_machine():
    async def case(db):
        await _add_remote_service(db)
        local = await _add_local_service(db)

        snapshot = await ct.export_snapshot(db, "services")
        text = json.dumps(snapshot, ensure_ascii=False)
        rows_text = json.dumps(snapshot["tables"], ensure_ascii=False)

        # 数据行里不许出现这个地址（排除说明里点名它是刻意的：要让人知道哪一个被跳过了）
        assert local.base_url not in rows_text, "本机地址被写进了数据行"
        assert local.base_url in text, "被跳过的本机服务应当在 excluded 里点名，用户才知道少了什么"
        assert len(snapshot["tables"]["provider_services"]) == 1, snapshot["tables"]["provider_services"]
        assert snapshot["tables"]["provider_services"][0]["base_url"] == "https://api.openai.com/v1"
        assert any("指向本机" in w for w in snapshot["warnings"]), snapshot["warnings"]
        assert any(e["table"] == "provider_services" and e["field"] == "*" for e in snapshot["excluded"])

        # 没有 scheme 的写法也要认出来
        assert ct._is_local_url("127.0.0.1:11434/v1") is True
        assert ct._is_local_url("http://localhost:8080/v1") is True
        assert ct._is_local_url("https://api.openai.com/v1") is False

    _run(case)


def test_secret_config_rows_stay_home():
    async def case(db):
        await cc.create_row(
            db,
            SCHEMA_REGISTRY["config_items"],
            {"key": "my.private_token", "label": "自建令牌", "value": "tok-SUPER-SECRET",
             "group_name": "advanced", "value_type": "string", "is_secret": True},
        )
        snapshot = await ct.export_snapshot(db, "settings")
        text = json.dumps(snapshot, ensure_ascii=False)
        rows_text = json.dumps(snapshot["tables"], ensure_ascii=False)

        assert "tok-SUPER-SECRET" not in text, "敏感配置项的值被导出了（连排除说明里都不该有）"
        assert "my.private_token" not in rows_text, "敏感配置项被导出了"
        assert any("敏感" in e["reason"] for e in snapshot["excluded"]), snapshot["excluded"]

    _run(case)


# ============================================================
# 三、结构校验：版本 / 格式 / 表名 / 模式
# ============================================================


def test_import_rejects_wrong_format():
    async def case(db):
        snapshot = await ct.export_snapshot(db, "prompts")
        snapshot["format"] = "some-other-tool"
        e = await _expect_import_error(snapshot, db, "format", ct.FORMAT)
        assert "some-other-tool" in e.message, e.message

        await _expect_import_error(["not", "an", "object"], db, "JSON 对象")

    _run(case)


def test_import_rejects_unknown_schema_version():
    async def case(db):
        snapshot = await ct.export_snapshot(db, "prompts")

        newer = json.loads(json.dumps(snapshot))
        newer["schemaVersion"] = ct.SCHEMA_VERSION + 1
        e = await _expect_import_error(newer, db, "schemaVersion", "升级")
        assert str(ct.SCHEMA_VERSION + 1) in e.message, e.message

        missing = json.loads(json.dumps(snapshot))
        missing.pop("schemaVersion")
        await _expect_import_error(missing, db, "schemaVersion")

        broken = json.loads(json.dumps(snapshot))
        broken["schemaVersion"] = "第一版"
        await _expect_import_error(broken, db, "schemaVersion")

    _run(case)


def test_import_rejects_unknown_table_and_structure():
    async def case(db):
        snapshot = await ct.export_snapshot(db, "prompts")

        unknown = json.loads(json.dumps(snapshot))
        unknown["tables"]["comfy_workflows"] = []
        e = await _expect_import_error(unknown, db, "comfy_workflows", "不认识")
        assert "prompts" in e.message, e.message  # 说清「我认识哪些」

        not_object = json.loads(json.dumps(snapshot))
        not_object["tables"] = []
        await _expect_import_error(not_object, db, "tables")

        empty = json.loads(json.dumps(snapshot))
        empty["tables"] = {}
        await _expect_import_error(empty, db, "空")

        not_list = json.loads(json.dumps(snapshot))
        not_list["tables"]["prompts"] = {"key": "x"}
        await _expect_import_error(not_list, db, "不是数组")

    _run(case)


def test_import_rejects_unknown_mode_and_scope():
    async def case(db):
        snapshot = await ct.export_snapshot(db, "prompts")

        replace = json.loads(json.dumps(snapshot))
        replace["mode"] = "replace"
        await _expect_import_error(replace, db, "replace", ct.MODE_MERGE)

        bad_scope = json.loads(json.dumps(snapshot))
        bad_scope["scopes"] = ["prompts", "everything"]
        await _expect_import_error(bad_scope, db, "everything")

        try:
            ct.resolve_scopes("prompts,nope")
        except cc.ConfigError as e:
            assert "nope" in e.message and "settings" in e.message, e.message
        else:
            raise AssertionError("未知范围应当被拒绝")

    _run(case)


def test_import_accepts_only_declared_fields():
    async def case(db):
        snapshot = await ct.export_snapshot(db, "prompts")
        snapshot["tables"]["prompts"][0]["native_language"] = "中文"  # 注册表里没这个字段
        snapshot["tables"]["prompts"][0]["title"] = "改了标题"

        preview = await ct.import_snapshot(db, snapshot, dry_run=True)
        assert preview["totals"]["updated"] == 1, preview["totals"]
        assert any("native_language" in w for w in preview["warnings"]), preview["warnings"]

        await ct.import_snapshot(db, snapshot, dry_run=False)
        db.expire_all()
        row = (await db.execute(select(Prompt).where(Prompt.key == snapshot["tables"]["prompts"][0]["key"]))).scalars().one()
        assert row.title == "改了标题", row.title
        assert not hasattr(row, "native_language") or not row.__dict__.get("native_language"), "陌生字段被写进了模型"

    _run(case)


# ============================================================
# 四、dry-run 不写库 + 幂等
# ============================================================


def test_dry_run_writes_nothing():
    async def case(db):
        snapshot = await ct.export_snapshot(db)  # 默认范围（含 services）
        snapshot["tables"]["prompts"].append(
            {"key": "brand-new-prompt", "title": "新提示词", "modality": "image",
             "content": "一段全新的提示词", "description": "", "tags": "", "sort_order": 99, "enabled": True}
        )
        snapshot["tables"]["prompts"][0]["title"] = "被改过的标题"

        prompts_before = await _count(db, Prompt)
        audits_before = await _audit_count(db, "prompts")
        total_rows = sum(len(rows) for rows in snapshot["tables"].values())

        preview = await ct.import_snapshot(db, snapshot, dry_run=True)
        assert preview["dryRun"] is True
        assert preview["totals"] == {"created": 1, "updated": 1, "skipped": total_rows - 2}, preview["totals"]

        assert await _count(db, Prompt) == prompts_before, "dry-run 竟然写了库"
        assert await _audit_count(db, "prompts") == audits_before, "dry-run 竟然留了审计"
        db.expire_all()
        first = (await db.execute(select(Prompt).where(Prompt.key == snapshot["tables"]["prompts"][0]["key"]))).scalars().one()
        assert first.title != "被改过的标题", "dry-run 竟然改了内容"

    _run(case)


def test_dry_run_then_confirm_then_reimport_is_idempotent():
    async def case(db):
        snapshot = await ct.export_snapshot(db)
        snapshot["tables"]["prompts"].append(
            {"key": "shared-prompt", "title": "朋友分享的提示词", "modality": "image",
             "content": "极简线条插画：{主体}，单色背景", "description": "来自快照", "tags": "插画",
             "sort_order": 50, "enabled": True}
        )

        preview = await ct.import_snapshot(db, snapshot, dry_run=True)
        assert preview["totals"]["created"] == 1, preview["totals"]
        assert await _count(db, Prompt) == 10, "dry-run 之后提示词数量不该变"

        first = await ct.import_snapshot(db, snapshot, dry_run=False)
        assert first["totals"]["created"] == 1, first["totals"]
        assert await _count(db, Prompt) == 11, "确认写入后应当多一行"

        audits_after_first = await _audit_count(db, "prompts")

        second = await ct.import_snapshot(db, snapshot, dry_run=False)
        assert second["totals"]["created"] == 0, "重复导入竟然又新增了行"
        assert second["totals"]["updated"] == 0, "内容没变却报了更新"
        assert await _count(db, Prompt) == 11, "重复导入产生了重复行"
        assert await _audit_count(db, "prompts") == audits_after_first, "没改动却还在刷审计"

        third = await ct.import_snapshot(db, snapshot, dry_run=True)
        assert third["totals"]["created"] == 0 and third["totals"]["updated"] == 0, third["totals"]

        # 唯一约束的列：重复导入没有撞 key
        keys = list((await db.execute(select(Prompt.key))).scalars().all())
        assert len(set(keys)) == len(keys), "出现了重复 key"

    _run(case)


def test_roundtrip_between_two_installs():
    """真正想支持的那件事：A 机器导出，B 机器导入。"""

    async def build_source(db):
        await _add_remote_service(db)
        await _add_local_service(db)
        await cc.create_row(
            db,
            SCHEMA_REGISTRY["prompts"],
            {"key": "my-own-prompt", "title": "我自己写的提示词", "modality": "video",
             "content": "镜头缓缓推近：{主体}", "description": "自用", "tags": "运镜",
             "sort_order": 30, "enabled": True},
        )
        await cc.create_row(
            db,
            SCHEMA_REGISTRY["director_styles"],
            {"key": "my_style", "name": "我的风格", "agent_prompt": "偏冷调",
             "image_prompt": "cool tones", "video_prompt": "slow dolly", "negative_prompt": "",
             "sort_order": 20, "enabled": True},
        )
        return await ct.export_snapshot(db, "prompts,styles,services")

    def case():
        payload = _run(build_source)
        text = json.dumps(payload, ensure_ascii=False)
        rows_text = json.dumps(payload["tables"], ensure_ascii=False)
        assert "127.0.0.1" not in rows_text, "本机服务不该跟着快照走"
        assert "api_key" not in rows_text, "密钥字段不该跟着快照走"
        total_rows = sum(len(rows) for rows in payload["tables"].values())

        async def target(db):
            # B 机器：全新安装（只有种子）
            preview = await ct.import_snapshot(db, payload, dry_run=True)
            # 新增三样：我的提示词 + 我的风格 + 那个公网服务
            assert preview["totals"]["created"] == 3, preview["totals"]
            assert preview["totals"]["updated"] == 0, preview["totals"]
            assert preview["totals"]["skipped"] == total_rows - 3, preview["totals"]
            assert await _count(db, Prompt) == 10, "预览阶段不该写库"

            done = await ct.import_snapshot(db, payload, dry_run=False)
            assert done["totals"]["created"] == 3, done["totals"]
            assert done["conflicts"] == [], done["conflicts"]
            db.expire_all()

            prompt = (
                await db.execute(select(Prompt).where(Prompt.key == "my-own-prompt"))
            ).scalars().one()
            assert prompt.content == "镜头缓缓推近：{主体}", prompt.content
            assert await _count(db, DirectorStyle) == 13, "我的风格没被导入"

            service = (await provider_store.list_services(db))[0]
            assert service.base_url == "https://api.openai.com/v1", service.base_url
            assert service.api_key_enc == "", "导入的服务不该带 Key（要用户自己填）"
            assert len(provider_store.parse_models(service.models_json)) == 1, service.models_json

        _run(target)

    case()


def test_import_scope_subset_leaves_other_tables_alone():
    async def case(db):
        snapshot = await ct.export_snapshot(db)
        snapshot["tables"]["config_items"][0]["value"] = "别人的站点名"
        snapshot["scopes"] = ["prompts"]  # 前端勾掉了「系统配置」

        preview = await ct.import_snapshot(db, snapshot, dry_run=True)
        assert "config_items" not in preview["summary"], preview["summary"]
        assert any("系统配置" in w and "不在本次导入范围" in w for w in preview["warnings"]), preview["warnings"]

        await ct.import_snapshot(db, snapshot, dry_run=False)
        value = (
            await db.execute(
                select(SCHEMA_REGISTRY["config_items"].model).where(
                    SCHEMA_REGISTRY["config_items"].model.key == "app.name"
                )
            )
        ).scalars().one()
        assert json.loads(value.value_json) != "别人的站点名", "被排除的范围竟然被写进去了"

    _run(case)


# ============================================================
# 五、脏数据只脏它自己
# ============================================================


def test_dirty_rows_are_rejected_one_by_one():
    async def case(db):
        snapshot = await ct.export_snapshot(db)
        rows = snapshot["tables"]["prompts"]
        good_key = rows[0]["key"]
        rows[0]["title"] = "被改过的标题"  # 这一行是干净的「更新」

        rows.append({"key": "bad-type", "title": "类型错", "content": "c", "sort_order": "abc"})
        rows.append({"key": "missing-title", "content": "c"})  # 缺必填「标题」
        rows.append({"key": "", "title": "空标识", "content": "c"})
        rows.append("不是对象")
        rows.append(dict(rows[0]))  # 与第一行标识重复

        preview = await ct.import_snapshot(db, snapshot, dry_run=True)
        reasons = " ".join(c["reason"] for c in preview["conflicts"])
        assert len(preview["conflicts"]) == 5, preview["conflicts"]
        assert "整数" in reasons, reasons
        assert "标题" in reasons, reasons
        assert "标识" in reasons, reasons
        assert "不是一个对象" in reasons, reasons
        assert "重复" in reasons, reasons
        assert preview["dryRun"] is True

        # 正常行照样统计，不因为旁边有脏行就整批不干
        assert preview["totals"]["updated"] == 1, preview["totals"]

        done = await ct.import_snapshot(db, snapshot, dry_run=False)
        assert len(done["conflicts"]) == 5, done["conflicts"]
        assert done["ok"] is True

        db.expire_all()
        assert await _count(db, Prompt) == 10, "脏行被写进库了"
        row = (await db.execute(select(Prompt).where(Prompt.key == good_key))).scalars().one()
        assert row.title == "被改过的标题", row.title

    _run(case)


def test_bad_service_rows_are_rejected_with_readable_reasons():
    async def case(db):
        snapshot = await ct.export_snapshot(db, "services")
        snapshot["tables"]["provider_services"] = [
            {"name": "缺地址", "kind": "openai", "base_url": "", "models": []},
            {"name": "野协议", "kind": "dashscope-v2", "base_url": "https://api.example.com/v1", "models": []},
            {"name": "", "kind": "openai", "base_url": "https://api.example.com/v2", "models": []},
            {"name": "模型清单不是数组", "kind": "openai", "base_url": "https://api.example.com/v3", "models": {}},
            {"name": "好服务", "kind": "openai", "base_url": "https://api.example.com/v4", "models": [
                {"name": "gpt-4o", "modality": "text", "label": "GPT-4o"}]},
        ]

        preview = await ct.import_snapshot(db, snapshot, dry_run=True)
        assert preview["totals"]["created"] == 1, preview["totals"]
        assert len(preview["conflicts"]) == 4, preview["conflicts"]
        reasons = " ".join(c["reason"] for c in preview["conflicts"])
        assert "接口地址" in reasons, reasons
        assert "协议" in reasons and "openai" in reasons, reasons
        assert "服务名称" in reasons, reasons
        assert "数组" in reasons, reasons
        assert await _count(db, ProviderService) == 0, "dry-run 不该写库"

        await ct.import_snapshot(db, snapshot, dry_run=False)
        assert await _count(db, ProviderService) == 1, "只有那一行好数据该被写入"

    _run(case)


def test_import_survives_into_an_empty_database():
    """空库（没跑过种子）也不能 500：该建的建、该报的以冲突形式报出来。"""

    async def build(db):
        return await ct.export_snapshot(db)

    def case():
        snapshot = _run(build)

        async def target(db):
            preview = await ct.import_snapshot(db, snapshot, dry_run=True)
            expected = sum(len(rows) for rows in snapshot["tables"].values())
            assert preview["ok"] is True
            assert preview["totals"]["created"] == expected, preview["totals"]
            assert preview["conflicts"] == [], preview["conflicts"]

            done = await ct.import_snapshot(db, snapshot, dry_run=False)
            assert done["conflicts"] == [], done["conflicts"]
            assert await _count(db, Prompt) == len(snapshot["tables"]["prompts"])
            return True

        assert _run_empty(target) is True

    case()


def test_audit_trail_can_be_rolled_back():
    """写库要留痕，且这条留痕能用来回滚（复用现有 rollback）。"""

    async def case(db):
        snapshot = await ct.export_snapshot(db, "prompts")
        snapshot["tables"]["prompts"].append(
            {"key": "rollback-me", "title": "待回滚", "modality": "image", "content": "x",
             "description": "", "tags": "", "sort_order": 0, "enabled": True}
        )
        await ct.import_snapshot(db, snapshot, dry_run=False)

        log = (
            await db.execute(
                select(ConfigAuditLog)
                .where(ConfigAuditLog.table_name == "prompts")
                .where(ConfigAuditLog.action == "create")
                .order_by(ConfigAuditLog.id.desc())
            )
        ).scalars().first()
        assert log is not None and log.actor == "import", log
        assert json.loads(log.after_json or "{}").get("key") == "rollback-me", log.after_json

        await cc.rollback_row(db, SCHEMA_REGISTRY["prompts"], log.id)
        assert await _count(db, Prompt) == 10, "回滚没有把导入新增的行删掉"

    _run(case)


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
