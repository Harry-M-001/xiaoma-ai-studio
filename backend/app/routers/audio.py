"""配音接口：把一段文本合成成语音。

**为什么是同步接口，而不是像出图/出视频那样走任务队列**：这个功能的用法就是
「写下几句话 → 点一下 → 立刻听到」。走队列要在中间多插一次轮询（默认 3 秒一轮），
为了一个三五秒就回来的调用让用户盯着「生成中」，正好把「试听」的意义弄没了。

代价是没有任务中心那一行、也就没有现成的重试按钮——所以这里配了两条补偿：
产物**落成音频资产**（能重听、能复用、能在资产库里删），以及文本留在页面上
（点第二次就是重试）。真要重试按钮，说明它该变成节点，那是后面按角色配音时的事。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, require_auth
from app.providers import ark
from app.providers.base import AdapterError
from app.schemas import asset_to_out
from app.services import speech, speech_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/audio", tags=["audio"], dependencies=[Depends(require_auth)])


class SpeechIn(BaseModel):
    """一次配音请求。字段名与上游（OpenAI `/audio/speech`）的口径对齐，少一层翻译。"""

    text: str = ""
    # "服务ID:模型名"，与图片/视频用同一套选模型的形状
    model_key: str = ""
    voice: str = ""
    speed: float = speech.SPEED_DEFAULT


@router.get("/voices")
async def list_voices() -> dict:
    """可选音色与这一段的上限。

    `presets` 只是**快捷选项**，不是白名单：各家音色名不通用（`alloy` /
    `zh-CN-XiaoxiaoNeural` / 自建音色 id），界面上允许直接手填。

    `videoRefHint` 是「把配音挂到一次视频生成上」时那句提示，**唯一副本在后端**：
    它会同时出现在视频生成页与画布的视频节点面板上，两边各抄一份必然会漂移，
    而漂移的表现是「同一个能力两个说法」，用户只会谁都不信。
    """
    return {
        "presets": [dict(v) for v in speech.VOICE_PRESETS],
        "maxChars": speech.MAX_CHARS,
        "speedRange": [speech.SPEED_MIN, speech.SPEED_MAX],
        "speedDefault": speech.SPEED_DEFAULT,
        "videoRefHint": ark.AUDIO_REF_NO_HINT,
    }


@router.post("/speech")
async def synthesize(payload: SpeechIn, db: AsyncSession = Depends(get_db)) -> dict:
    """合成一段语音并落成音频资产，返回资产与这一段的实情（多少字、多长）。"""
    try:
        asset, meta = await speech_service.synthesize_to_asset(
            db,
            model_key=payload.model_key,
            text=payload.text,
            voice=payload.voice,
            speed=payload.speed,
            source="tts",
        )
    except AdapterError as e:
        # 上游/配置类问题都是 400：用户改一下就能过，不是服务端故障
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"asset": asset_to_out(asset), **meta}
