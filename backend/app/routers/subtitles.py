"""字幕：版式与字体的选项、字体按需下载、版式真渲染预览。

这一版（v1.1.25）字幕落在**导演台**：片段排好之后给每段配一句，导出时烧进成片。
版式是**用户直接选的**——画风只提供默认值（`subtitles.resolve_style`），
一旦用户选过，换画风不再动它。这与 #32 回滚、#33 定稿是同一个口径。

三条边界写清楚：

1. **预览是真渲染一帧**，不是前端近似画。版式的效果取决于 libass 的字体选择、
   字形、描边、缩放、边距；前端再写一套必然对不上，而「预览看着挺好、导出来不一样」
   是最伤信任的一种不一致。
2. **字体只内置一款**（思源黑体），其余按需下载——**大文件不进仓库与便携包**
   （2026-09-20 用户定下的原则）。下载走我们自己的 Release 附件，国内优先 Gitee，
   并且**必须比对 sha256**。
3. **缺字体 / 字体损坏 / 没有 libass 一律在点之前拦住**，不留到导出时才报一句
   ffmpeg 的错。三种情况的文案各不相同——要用户做的事不一样。
"""

from __future__ import annotations

import base64
import logging

from fastapi import APIRouter, HTTPException

from app.schemas import SubtitlePreviewIn
from app.services import ffmpeg_service, subtitle_fonts, subtitles

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/subtitles", tags=["subtitles"])

# 预览用的示例文本：**要能一眼看出字体有没有生效、有没有缺字**。
# 所以刻意带上繁体（漢語）、一个扩展 A 区字（㸚）与英文数字——只写「示例」两个字，
# 缺字与字体回退都看不出来。
PREVIEW_TEXT = "小马工坊 · 漢語字幕 ABC 123"


@router.get("/options")
async def list_options() -> dict:
    """字幕的全部可选项：版式、字体（含能不能用）、以及本机能不能烧。

    一次给全，前端不另抄任何一份表——抄一份就会出现「界面能选出后端不认识的东西」，
    而报错要到导出时才知道（转场那一版立的规矩，这里照用）。
    """
    # **先预热滤镜探测**：`check_ready` 里的滤镜判据读的是缓存，没预热时会把
    # 「还没探过」当成「没有滤镜」，界面上就会说一句完全错误的理由。
    # 这里是异步函数，顺手探一次；已探过时它是个空操作。
    await ffmpeg_service.warm_subtitle_filter()
    return {
        "styles": subtitles.style_options(),
        "fonts": subtitle_fonts.list_fonts(),
        "defaultStyle": subtitles.DEFAULT_STYLE,
        "recommended": dict(subtitles.RECOMMENDED),
        "scale": {"min": subtitles.MIN_SCALE, "max": subtitles.MAX_SCALE, "default": 1.0},
        "filterAvailable": ffmpeg_service.subtitle_filter_available(),
        "usableFonts": subtitle_fonts.usable_keys(),
        # 一句可直接展示的话：能烧就是空串，不能烧就说清是滤镜还是字体的问题
        "problem": subtitle_fonts.check_ready([subtitle_fonts.BUILTIN_KEY]),
        "previewText": PREVIEW_TEXT,
    }


@router.post("/fonts/{key}/download")
async def download_font(key: str) -> dict:
    """下载并安装一款字体（下到数据目录，不进仓库）。

    **回的是整份选项，不只是字体列表**：界面上的下拉里每一项都带「这款要的字体在不在本机」
    （`fontReady`），只回字体列表的话那部分状态还是页面加载时那一次的老值，
    用户会看到「下完了但提示还在」。走查时踩到过这个。整份回传、前端整体替换，
    就不会再有「刷新了一半」这种状态。
    """
    if subtitle_fonts.font(key) is None:
        raise HTTPException(status_code=404, detail="没有这款字体")
    try:
        message = await subtitle_fonts.install_font(key)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "message": message, "options": await list_options()}


@router.post("/preview")
async def preview(payload: SubtitlePreviewIn) -> dict:
    """按当前版式真渲染一帧，回来一张 JPEG 的 data URI。

    `text` 留空就用示例文本（带繁体与生僻字，用来暴露缺字与字体回退）。
    """
    style_key = subtitles.resolve_style(payload.style)
    text = subtitles.ass_text(payload.text) or PREVIEW_TEXT
    ass = subtitles.build_ass(
        [{"start": 0.0, "end": 10.0, "text": text}],
        style_key=style_key,
        width=payload.width,
        height=payload.height,
        size_scale=payload.scale,
    )
    try:
        image = await ffmpeg_service.render_subtitle_preview(
            ass,
            font_keys=subtitles.fonts_used(style_key),
            width=payload.width,
            height=payload.height,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "style": style_key,
        "font": subtitles.fonts_used(style_key)[0],
        "image": "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii"),
    }
