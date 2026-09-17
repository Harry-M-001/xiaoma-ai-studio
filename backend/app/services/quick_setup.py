"""粘贴一个 Key 就接入：认出归属 → 试一次真实请求 → 通了才落库。

为什么值得做：第一次用的人往往卡在「添加模型服务」那张表单上——协议、Base URL、模型名
三样都得填对，填错的表现还都是「请求失败」。而多数人手上只有一串 Key。
这里按预设里的 `key_pattern` 认归属，用预设的地址与常用模型发一次真请求，
通了才写库：既省掉填表，也避免「配了半天才发现这个 Key 是别家的」。

认不出来时**不猜死**：把候选列给用户挑（这也是 DeepSeek 与通义千问这类
`sk-` + 32 位十六进制 Key 无法从形状上区分的现实），而不是随便选一个存下去。
"""

from __future__ import annotations

import asyncio
import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProviderPreset
from app.providers.base import AdapterError
from app.registry import adapters
from app.schemas import ModelSpec
from app.services import provider_store

logger = logging.getLogger("xiaoma.setup")

# 一次最多试几个：前缀撞车时（sk- 开头的一大串）串行试完会很慢，
# 而且多数情况第一个就中了。剩下的交给用户手动挑。
MAX_PROBES = 3

# 单次连通测试的上限：上游卡住时不能让这个接口一直挂着
PROBE_TIMEOUT = 20.0


async def _presets(db: AsyncSession) -> list[ProviderPreset]:
    rows = await db.execute(
        select(ProviderPreset)
        .where(ProviderPreset.enabled.is_(True))
        .order_by(ProviderPreset.sort_order, ProviderPreset.id)
    )
    return list(rows.scalars().all())


def _matches(pattern: str, api_key: str) -> bool:
    """key_pattern 是「|」分隔的若干正则，命中任意一条即算认出来。"""
    for part in (pattern or "").split("|"):
        part = part.strip()
        if not part:
            continue
        try:
            if re.search(part, api_key, re.I):
                return True
        except re.error:
            # 配置写错不该让整个接入流程挂掉，只记一条日志
            logger.warning("服务商预设的 key_pattern 不是合法正则，已跳过：%s", part)
    return False


def _models_of(preset: ProviderPreset) -> list[ModelSpec]:
    return list(provider_store.parse_models(preset.models_json))


def _card(preset: ProviderPreset) -> dict:
    return {
        "key": preset.key,
        "name": preset.name,
        "kind": preset.kind,
        "baseUrl": preset.base_url,
        "hint": preset.hint,
        "models": [m.model_dump() for m in _models_of(preset)],
    }


async def _candidates(
    db: AsyncSession, api_key: str, provider_key: str
) -> tuple[list[ProviderPreset], bool]:
    """返回（按优先级排好的候选, 是否是「认出来的」）。

    用户显式指定服务商时只有它一个候选；否则先按形状认，
    认不出就把全部预设列出来（排除 Ollama——那个不吃 Key，走另一条接入路径）。
    """
    presets = await _presets(db)
    if provider_key:
        hit = [p for p in presets if p.key == provider_key]
        if not hit:
            raise AdapterError(f"没有名为「{provider_key}」的服务商预设")
        return hit, True
    matched = [p for p in presets if _matches(p.key_pattern, api_key)]
    if matched:
        return matched, True
    return [p for p in presets if p.key != "ollama"], False


async def _probe(preset: ProviderPreset, api_key: str) -> tuple[bool, str]:
    """发一次真实请求验证这个 Key 配这家服务能不能用。"""
    models = _models_of(preset)
    probe_model = next((m.name for m in models if m.modality == "text"), None)
    adapter = adapters.build_adapter(preset.kind, preset.base_url, api_key)
    try:
        await asyncio.wait_for(adapter.test_connection(probe_model), timeout=PROBE_TIMEOUT)
        return True, "连接正常"
    except AdapterError as e:
        return False, str(e)
    except asyncio.TimeoutError:
        return False, f"连接超时（超过 {int(PROBE_TIMEOUT)} 秒）"
    except Exception as e:  # noqa: BLE001
        logger.warning("快速接入探测出现未预期错误：%s", e)
        return False, f"{type(e).__name__}: {e}"
    finally:
        try:
            await adapter.close()
        except Exception:  # noqa: BLE001
            pass


async def setup(
    db: AsyncSession,
    *,
    api_key: str,
    provider_key: str = "",
    force: bool = False,
) -> dict:
    """接入。force=True 表示「连通测试没过也照样存」——给那些测试方式不适配的服务商留个出口。"""
    key = (api_key or "").strip()
    if not key:
        raise AdapterError("请先粘贴 API Key")

    candidates, recognized = await _candidates(db, key, provider_key)
    if not recognized:
        # 认不出归属就**一个都不试**，直接把候选交回给用户挑。
        # 两个理由，第二条更要紧：
        # 一是快——按形状猜三家要串行跑三个真实请求，白等五秒；
        # 二是**不能拿用户的凭据去试不相干的服务商**：这串 Key 可能属于任何一家，
        # 拿它去敲 OpenAI / DeepSeek / Kimi 的门，等于把他的密钥发给了三家他没用过的公司。
        logger.info("快速接入：认不出 Key 的归属，列出 %s 个候选等用户挑", len(candidates))
        return {
            "ok": False,
            "recognized": False,
            "forced": False,
            "service": None,
            "tried": [],
            "candidates": [_card(p) for p in candidates],
        }

    tried: list[dict] = []
    for preset in candidates[:MAX_PROBES]:
        ok, message = await _probe(preset, key)
        tried.append(
            {
                "key": preset.key,
                "name": preset.name,
                "baseUrl": preset.base_url,
                "ok": ok,
                "message": message,
            }
        )
        if ok:
            row = await provider_store.upsert_by_base_url(
                db,
                name=preset.name,
                kind=preset.kind,
                base_url=preset.base_url,
                api_key=key,
                models=_models_of(preset),
                sort_order=preset.sort_order,
            )
            logger.info("快速接入成功：%s（%s）", preset.name, preset.base_url)
            return {
                "ok": True,
                "recognized": recognized,
                "forced": False,
                "service": row.model_dump(mode="json"),
                "tried": tried,
                "candidates": [],
            }

    if force and candidates:
        preset = candidates[0]
        row = await provider_store.upsert_by_base_url(
            db,
            name=preset.name,
            kind=preset.kind,
            base_url=preset.base_url,
            api_key=key,
            models=_models_of(preset),
            sort_order=preset.sort_order,
        )
        return {
            "ok": True,
            "recognized": recognized,
            "forced": True,
            "service": row.model_dump(mode="json"),
            "tried": tried,
            "candidates": [],
        }

    return {
        "ok": False,
        "recognized": recognized,
        "forced": False,
        "service": None,
        "tried": tried,
        "candidates": [_card(p) for p in candidates],
    }
