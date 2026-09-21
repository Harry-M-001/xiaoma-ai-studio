"""配音的接线层：解析模型 → 调适配器 → 存成音频资产。

与 `speech.py` 的分工：那边是纯判据与文案（什么算合法、怎么拼旁白、话怎么说），
这边只负责把「一次合成」真的做完并落库。

**为什么一定要落库**：用户为这一段付过钱。存成资产之后才能重听、才能被样片旁白与
导演台复用；只把字节流回给前端，就成了「这次听了，下次还得再花钱生成一遍」。
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Asset
from app.providers.base import AdapterError
from app.services import ffmpeg_service, provider_store, speech, storage

logger = logging.getLogger("xiaoma.speech")

# 正文只是给人看的，不必存全文（超长文本另有 2000 字上限把关）
MAX_PROMPT = 2400


async def default_model_key(db: AsyncSession) -> str:
    """这次该用哪个语音模型。

    优先级：配置里的 `defaults.speech_model` → **只有一个可选模型时自动用它**。
    只有一个还要用户先去设置里填一遍纯属为难人；有多个时才需要他明确选一个
    （否则只能瞎猜，而每次猜错都是在替用户花钱）。
    """
    from app.services.config_center_service import runtime_value

    configured = str(runtime_value("defaults.speech_model", "") or "").strip()
    if configured:
        return configured

    rows = await provider_store.list_services(db)
    options = provider_store.list_model_options(rows, "audio")
    if len(options) == 1:
        return str(options[0].key)
    if not options:
        raise AdapterError(
            "还没有可用的语音模型：去「模型服务」里给某个服务加一个能力为「音频」的模型"
            "（OpenAI 兼容的服务填 `tts-1` 这类模型名即可）",
            log_detail="config_invalid no_audio_model",
        )
    raise AdapterError(
        f"有 {len(options)} 个语音模型，请先到「系统设置 → 默认值 → 默认语音模型」里选定一个，"
        "或者直接去「配音」页选一个再生成",
        log_detail=f"config_invalid ambiguous_model count={len(options)}",
    )


async def synthesize_bytes(
    db: AsyncSession,
    *,
    model_key: str,
    text: str,
    voice: str = "",
    speed: float = speech.SPEED_DEFAULT,
) -> tuple[bytes, str, dict]:
    """合成一段语音，回 `(音频字节, content_type, 实情)`。**不落库。**

    一镜多人时每一句都要各合成一次，而那些中间产物**不该进资产库**（用户要的是这一镜
    拼好的那一条）。所以把「合成」与「落库」拆开，落库交给调用方决定。
    """
    if not model_key:
        raise AdapterError(
            "还没有可用的语音模型：去「模型服务」里给某个服务加一个能力为「音频」的模型，"
            "再回来选它",
            log_detail="config_invalid missing_model",
        )
    clean = speech.sanitize_text(text)
    problem = speech.text_error(clean)
    if problem:
        raise AdapterError(problem, log_detail="invalid_text")

    clean_voice = speech.sanitize_voice(voice)
    clean_speed = speech.sanitize_speed(speed)

    resolved = await provider_store.resolve_model(db, model_key, "audio")
    try:
        result = await resolved.adapter.synthesize_speech(
            model=resolved.model_name, text=clean, voice=clean_voice, speed=clean_speed
        )
    finally:
        # 适配器持有 httpx client，用完就关，别把连接攒到进程退出
        await resolved.adapter.close()
    logger.info(
        "配音合成：%s 字（模型 %s，音色 %s）",
        len(clean), resolved.model_name, clean_voice or "默认",
    )
    return result.audio, result.content_type, {
        "chars": len(clean),
        "text": clean,
        "modelKey": f"{resolved.service.id}:{resolved.model_name}",
        "model": resolved.model_name,
        "voice": clean_voice,
        "speed": clean_speed,
    }


async def synthesize_to_asset(
    db: AsyncSession,
    *,
    model_key: str,
    text: str,
    voice: str = "",
    speed: float = speech.SPEED_DEFAULT,
    source: str = "tts",
    name: str = "",
    note: str = "",
) -> tuple[Asset, dict]:
    """合成一段语音并存成 `kind="audio"` 的资产，返回 (资产, 这一段的实情)。

    实情（`chars` / `seconds` / `voice`）返回给调用方写进报告或回执，
    免得界面只能显示一句「成功」——用户想知道的是「念了多少字、多长」。
    """
    data, content_type, info = await synthesize_bytes(
        db, model_key=model_key, text=text, voice=voice, speed=speed
    )
    clean = info["text"]

    rel = storage.save_bytes(data, content_type, preferred_ext=storage.audio_ext(content_type))
    seconds = await ffmpeg_service.probe_audio_seconds(storage.abs_path(rel))
    asset = Asset(
        kind="audio",
        filename=rel,
        original_name=rel.split("/")[-1],
        content_type=content_type,
        size=len(data),
        source=source,
        name=name or speech.asset_name(clean),
        # 正文落进 prompt：资产库里要能看出这一段念的是什么，否则一堆「配音 · xxx」认不出
        prompt=(clean + (f"\n\n{note}" if note else ""))[:MAX_PROMPT],
        duration=speech.duration_seconds(seconds),
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return asset, {**info, "seconds": seconds}


async def synthesize_to_temp(
    db: AsyncSession,
    *,
    model_key: str,
    text: str,
    voice: str = "",
    speed: float = speech.SPEED_DEFAULT,
    work: Path,
    index: int,
) -> tuple[Path, dict]:
    """合成一句，**只落到临时目录**，不登记资产（一镜多人的中间产物走这条）。

    文件名带上真实的扩展名（`.wav` / `.mp3`）：拼接时 ffmpeg 靠**内容**也能认出来，
    所以漏掉那个点不会立刻报错——正因如此它才容易一直错下去，而「按扩展名判断」
    是后面任何一处都可能做的事（`seg0mp3` 这种名字谁都认不出来）。
    """
    data, content_type, info = await synthesize_bytes(
        db, model_key=model_key, text=text, voice=voice, speed=speed
    )
    path = work / f"seg{index}.{storage.audio_ext(content_type)}"
    path.write_bytes(data)
    return path, {**info, "seconds": await ffmpeg_service.probe_audio_seconds(path)}


async def register_audio_asset(
    db: AsyncSession,
    *,
    path: Path,
    text: str,
    name: str = "",
    note: str = "",
    source: str = "tts",
    content_type: str = "audio/wav",
) -> tuple[Asset, dict]:
    """把**已经在 storage 里**的一条音频登记成资产（拼好的多音色对白走这条）。

    与 `synthesize_to_asset` 的差别只在「音频是哪儿来的」：这里不合成，只登记。
    两处都要做的一件事实则是同一件——**量出秒数**。少了它，「读不读得完这一镜」
    就无从判断（而那是这一整条链最贵的一个判断）。
    """
    rel = ffmpeg_service._save_asset_file(path, path.suffix.lstrip("."))
    seconds = await ffmpeg_service.probe_audio_seconds(path)
    asset = Asset(
        kind="audio",
        filename=rel,
        original_name=rel.split("/")[-1],
        content_type=content_type,
        size=path.stat().st_size,
        source=source,
        name=name or speech.asset_name(text),
        prompt=(text + (f"\n\n{note}" if note else ""))[:MAX_PROMPT],
        duration=speech.duration_seconds(seconds),
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    logger.info("配音登记：%s（%s 秒）", asset.name, f"{seconds:.1f}" if seconds else "?")
    return asset, {"chars": len(text), "seconds": seconds, "text": text}
