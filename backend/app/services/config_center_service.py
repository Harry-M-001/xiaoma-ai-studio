"""配置中心服务：驱动「schema 注册制」的全部能力。

对任意一张已注册的配置表，统一提供：
- 列表 / 新增 / 修改 / 删除
- 乐观锁（version 不匹配 → 409）
- 审计留痕（before / after）
- 按审计记录一键回滚
- 幂等种子数据（存在即跳过，绝不覆盖用户修改）

另外提供运行期配置读取（同步可用）与公开配置读取。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ConfigAuditLog
from app.registry.schema_registry import SCHEMA_REGISTRY, FieldSpec, TableSpec

logger = logging.getLogger("xiaoma.config")


class ConfigError(Exception):
    """配置域的可读错误。status_code 供路由层直接使用。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# 值统一以 JSON 字符串存放于 value_json 的配置表
_KV_VALUE_TABLES = {"config_items"}
_KV_FIELD = "value_json"
_KV_API_FIELD = "value"


# ============================================================
# 序列化 / 反序列化
# ============================================================


def _load_json(raw: Any, fallback: Any = None) -> Any:
    if raw is None:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def serialize_row(spec: TableSpec, obj: Any) -> dict[str, Any]:
    """ORM 行 → API 结构。config_items 的 value_json 暴露为 value。"""
    data: dict[str, Any] = {"id": obj.id}
    if spec.has_version:
        data["version"] = getattr(obj, "version", 1)
    for f in spec.fields:
        if spec.name in _KV_VALUE_TABLES and f.name == _KV_FIELD:
            data[_KV_API_FIELD] = _load_json(getattr(obj, f.name, None))
            continue
        value = getattr(obj, f.name, None)
        if f.type == "json":
            value = _load_json(value, {})
        data[f.name] = value
    return data


def _coerce(field: FieldSpec, value: Any) -> Any:
    if field.type == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if field.type == "int":
        try:
            return int(value)
        except (TypeError, ValueError) as e:
            raise ConfigError(f"「{field.label or field.name}」需要是整数") from e
    if field.type == "float":
        try:
            return float(value)
        except (TypeError, ValueError) as e:
            raise ConfigError(f"「{field.label or field.name}」需要是数字") from e
    if field.type == "json":
        return value  # 原样，写库时再序列化
    return "" if value is None else str(value)


def apply_payload(spec: TableSpec, obj: Any, payload: dict[str, Any]) -> None:
    """把请求体写回 ORM 对象（只写声明过的字段）。"""
    for f in spec.fields:
        if f.readonly:
            continue
        if spec.name in _KV_VALUE_TABLES and f.name == _KV_FIELD:
            if _KV_API_FIELD in payload:
                setattr(obj, _KV_FIELD, json.dumps(payload[_KV_API_FIELD], ensure_ascii=False))
            continue
        if f.name not in payload:
            continue
        value = _coerce(f, payload[f.name])
        if f.type == "json":
            setattr(obj, f.name, json.dumps(value if value is not None else {}, ensure_ascii=False))
        elif f.type == "bool":
            setattr(obj, f.name, bool(value))
        else:
            setattr(obj, f.name, value)


def _validate_required(spec: TableSpec, obj: Any) -> None:
    for f in spec.fields:
        if not f.required:
            continue
        value = getattr(obj, f.name, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ConfigError(f"「{f.label or f.name}」不能为空")


# ============================================================
# 审计
# ============================================================


async def _audit(
    db: AsyncSession,
    table_name: str,
    row_id: int | None,
    action: str,
    before: dict | None,
    after: dict | None,
    actor: str = "local",
) -> ConfigAuditLog:
    log = ConfigAuditLog(
        table_name=table_name,
        row_id=row_id,
        action=action,
        before_json=json.dumps(before, ensure_ascii=False) if before is not None else None,
        after_json=json.dumps(after, ensure_ascii=False) if after is not None else None,
        actor=actor,
    )
    db.add(log)
    return log


async def list_audit(db: AsyncSession, table_name: str, limit: int = 50) -> list[dict]:
    rows = await db.execute(
        select(ConfigAuditLog)
        .where(ConfigAuditLog.table_name == table_name)
        .order_by(ConfigAuditLog.id.desc())
        .limit(min(limit, 200))
    )
    return [
        {
            "id": r.id,
            "table_name": r.table_name,
            "row_id": r.row_id,
            "action": r.action,
            "before": _load_json(r.before_json),
            "after": _load_json(r.after_json),
            "actor": r.actor,
            "created_at": r.created_at,
        }
        for r in rows.scalars().all()
    ]


# ============================================================
# 通用 CRUD
# ============================================================


def _order_clause(spec: TableSpec):
    order_col = getattr(spec.model, spec.order_by, None)
    id_col = getattr(spec.model, "id")
    return (order_col.asc(), id_col.asc()) if order_col is not None else (id_col.asc(),)


async def list_rows(db: AsyncSession, spec: TableSpec) -> list[dict]:
    rows = await db.execute(select(spec.model).order_by(*_order_clause(spec)))
    return [serialize_row(spec, r) for r in rows.scalars().all()]


async def get_row(db: AsyncSession, spec: TableSpec, row_id: int) -> Any:
    row = await db.get(spec.model, row_id)
    if row is None:
        raise ConfigError("配置项不存在", 404)
    return row


async def create_row(
    db: AsyncSession, spec: TableSpec, payload: dict[str, Any], actor: str = "local"
) -> dict:
    obj = spec.model()
    apply_payload(spec, obj, payload)
    _validate_required(spec, obj)
    if spec.has_version:
        obj.version = 1
    db.add(obj)
    try:
        await db.flush()
    except Exception as e:  # noqa: BLE001
        await db.rollback()
        raise ConfigError(f"保存失败：{_friendly_db_error(e)}") from e
    after = serialize_row(spec, obj)
    await _audit(db, spec.name, obj.id, "create", None, after, actor)
    await db.commit()
    await _refresh_cache(db)
    return after


async def update_row(
    db: AsyncSession,
    spec: TableSpec,
    row_id: int,
    payload: dict[str, Any],
    actor: str = "local",
) -> dict:
    obj = await get_row(db, spec, row_id)
    if spec.has_version and "version" in payload and payload["version"] is not None:
        try:
            incoming = int(payload["version"])
        except (TypeError, ValueError) as e:
            raise ConfigError("version 参数不正确") from e
        current = int(getattr(obj, "version", 1) or 1)
        if incoming != current:
            raise ConfigError(
                "该配置已被其他操作修改，请刷新后重试（版本不一致）", 409
            )
    before = serialize_row(spec, obj)
    apply_payload(spec, obj, payload)
    _validate_required(spec, obj)
    if spec.has_version:
        obj.version = int(getattr(obj, "version", 1) or 1) + 1
    try:
        await db.flush()
    except Exception as e:  # noqa: BLE001
        await db.rollback()
        raise ConfigError(f"保存失败：{_friendly_db_error(e)}") from e
    after = serialize_row(spec, obj)
    await _audit(db, spec.name, obj.id, "update", before, after, actor)
    await db.commit()
    await _refresh_cache(db)
    return after


async def delete_row(
    db: AsyncSession, spec: TableSpec, row_id: int, actor: str = "local"
) -> None:
    obj = await get_row(db, spec, row_id)
    before = serialize_row(spec, obj)
    await db.delete(obj)
    await _audit(db, spec.name, row_id, "delete", before, None, actor)
    await db.commit()
    await _refresh_cache(db)


async def rollback_row(
    db: AsyncSession, spec: TableSpec, log_id: int, actor: str = "local"
) -> dict:
    """按审计记录还原某个配置行。"""
    log = await db.get(ConfigAuditLog, log_id)
    if log is None or log.table_name != spec.name:
        raise ConfigError("审计记录不存在", 404)

    target = _load_json(log.before_json)
    existing = await db.get(spec.model, log.row_id) if log.row_id else None

    if target is None:
        # 原操作是新增 → 回滚即删除
        if existing is not None:
            await db.delete(existing)
            await _audit(db, spec.name, log.row_id, "rollback", None, {"deleted": True}, actor)
            await db.commit()
            await _refresh_cache(db)
        return {"ok": True, "action": "deleted"}

    if existing is None:
        obj = spec.model()
        apply_payload(spec, obj, target)
        if spec.has_version:
            obj.version = 1
        db.add(obj)
        await db.flush()
        restored = serialize_row(spec, obj)
        await _audit(db, spec.name, obj.id, "rollback", None, restored, actor)
        await db.commit()
        await _refresh_cache(db)
        return restored

    before = serialize_row(spec, existing)
    apply_payload(spec, existing, target)
    if spec.has_version:
        existing.version = int(getattr(existing, "version", 1) or 1) + 1
    await db.flush()
    restored = serialize_row(spec, existing)
    await _audit(db, spec.name, existing.id, "rollback", before, restored, actor)
    await db.commit()
    await _refresh_cache(db)
    return restored


def _friendly_db_error(exc: Exception) -> str:
    text = str(getattr(exc, "orig", exc))
    if "UNIQUE" in text.upper():
        return "存在重复的唯一标识，请修改后重试"
    return text or exc.__class__.__name__


# ============================================================
# 运行期配置
# ============================================================

_CACHE: dict[str, Any] = {}


def runtime_value(key: str, default: Any = None) -> Any:
    """同步读取运行期配置（供非请求上下文使用）。未加载时回落到默认值。"""
    return _CACHE.get(key, default)


async def _load_cache(db: AsyncSession) -> None:
    from app.models import ConfigItem

    rows = await db.execute(select(ConfigItem))
    _CACHE.clear()
    for row in rows.scalars().all():
        _CACHE[row.key] = _load_json(row.value_json)


async def _refresh_cache(db: AsyncSession) -> None:
    await _load_cache(db)


async def reload_runtime_config(db: AsyncSession) -> None:
    await _load_cache(db)


async def get_config_map(db: AsyncSession, public_only: bool = False) -> dict[str, Any]:
    from app.models import ConfigItem

    rows = await db.execute(select(ConfigItem).order_by(ConfigItem.sort_order, ConfigItem.id))
    result: dict[str, Any] = {}
    for row in rows.scalars().all():
        if public_only and row.is_secret:
            continue
        result[row.key] = _load_json(row.value_json)
    return result


async def set_config_value(
    db: AsyncSession, key: str, value: Any, actor: str = "local"
) -> dict:
    """按 key 写入单个配置项（不存在则创建），供快捷设置使用。"""
    from app.models import ConfigItem

    rows = await db.execute(select(ConfigItem).where(ConfigItem.key == key))
    row = rows.scalars().first()
    spec = SCHEMA_REGISTRY["config_items"]
    if row is None:
        return await create_row(
            db, spec, {"key": key, "value": value, "label": key, "group_name": "basic"}, actor
        )
    return await update_row(db, spec, row.id, {"value": value}, actor)


# ============================================================
# 幂等种子
# ============================================================


def _seed_identity(row: dict[str, Any]) -> tuple[str, ...]:
    """推断种子行的唯一标识：优先 key，其次 kind+value，最后第一个字段。"""
    if "key" in row:
        return ("key", str(row["key"]))
    if "kind" in row and "value" in row:
        return ("kind+value", f"{row['kind']}={row['value']}")
    first = next(iter(row))
    return (first, str(row[first]))


async def ensure_seed(db: AsyncSession) -> int:
    """为所有注册表补齐缺失的种子行；已存在的绝不覆盖。返回新增条数。"""
    inserted = 0
    for spec in SCHEMA_REGISTRY.values():
        if not spec.seed:
            continue
        existing = {_seed_identity(serialize_row(spec, r)) for r in await _all_rows(db, spec)}
        for row in spec.seed:
            identity = _seed_identity(row)
            if identity in existing:
                continue
            obj = spec.model()
            apply_payload(spec, obj, row)
            if spec.has_version:
                obj.version = 1
            db.add(obj)
            existing.add(identity)
            inserted += 1
        await db.flush()
    if inserted:
        await db.commit()
    await _load_cache(db)
    logger.info("配置种子数据已就绪，本次新增 %s 条", inserted)
    return inserted


async def _all_rows(db: AsyncSession, spec: TableSpec) -> list[Any]:
    rows = await db.execute(select(spec.model))
    return list(rows.scalars().all())


# ============================================================
# 元数据（供前端动态渲染）
# ============================================================


def spec_to_meta(spec: TableSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "label": spec.label,
        "description": spec.description,
        "group": spec.group,
        "has_version": spec.has_version,
        "fields": [
            {
                "name": f.name,
                "label": f.label or f.name,
                "type": f.type,
                "required": f.required,
                "options": f.options,
                "default": f.default,
                "help": f.help,
                "readonly": f.readonly,
            }
            for f in spec.fields
        ],
    }
