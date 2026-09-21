"""#26 配音（TTS 试听）的前端接线检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_frontend_speech.py

后端把音频链修得再全，前端接错一处也白做。这里守五类最容易错的地方：

1. **导航与路由要成对**：后端注册表加了「配音」，前端 `App.tsx` 也得认这个 route，
   否则点进去是空白页（或落回首页，用户以为功能没做）。
2. **老库要靠迁移**：`ensure_seed` 只插缺行、从不覆盖。启用已有的 `audio` 模态行、
   重排已有的导航顺序，都必须写数据迁移——种子改了等于没改。
3. **字段名是前后端的契约**：`synthesize_to_asset` 返回的键与 `SpeechResult`
   必须对得上，否则界面上只是少一个数字，没人会知道是漏了。
4. **音频在浏览器侧有它自己的坑**：没有画面可显示，资产库与灯箱都要给分支，
   不然就是一张碎图（`<img>` 拿音频当图片渲染）。
5. **一次只放一个播放器**：连点几条来回对比是常见用法，每次 new 一个 `<audio>`
   就会叠着放，用户听到两段混在一起。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
BACKEND = ROOT / "backend"

sys.path.insert(0, str(BACKEND))

from app.registry import schema_registry as registry  # noqa: E402


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _spec(name: str):
    return next(s for s in registry.all_specs() if s.name == name)


def _nav_seed() -> list[dict]:
    return _spec("nav_items").seed


def test_the_new_nav_entry_exists_on_both_sides():
    """后端种子与前端兜底导航都要有「配音」，且路由名一致。"""
    seed = next((item for item in _nav_seed() if item["route"] == "speech"), None)
    assert seed is not None, "后端导航种子里没有「配音」"
    assert seed["label"] == "配音" and seed["group_name"] == "main", seed

    app = _text(SRC / "App.tsx")
    assert 'route: "speech"' in app, "前端兜底导航里没有配音"
    assert 'import SpeechPage from "./pages/SpeechPage"' in app, "没引入配音页"
    known = re.search(r"const KNOWN_ROUTES = new Set\(\[(.*?)\]\)", app, re.S)
    assert known and '"speech"' in known.group(1), "路由白名单里没有 speech（会被当成未知路由）"
    assert re.search(r'case "speech":\s*\n\s*return <SpeechPage', app), "路由没有分支到配音页"


def test_the_nav_order_matches_the_migration():
    """导航顺序的重排与迁移文件必须说同一件事。

    迁移按「旧默认值 → 新默认值」逐行搬；种子里那几个 key 的现值就是目标值。
    两边对不上，老库升级后顺序会与新装用户看到的不一样。
    """
    migration = _text(BACKEND / "alembic" / "versions" / "0015_audio_modality.py")
    order = {
        key: (int(old), int(new))
        for key, old, new in re.findall(r'"(\w+)": \((\d+), (\d+)\)', migration)
    }
    assert order, "迁移里没有导航重排表"
    current = {item["key"]: item["sort_order"] for item in _nav_seed()}
    for key, (_old, new) in order.items():
        assert current.get(key) == new, (
            f"导航 {key} 在种子里的顺序是 {current.get(key)}，迁移说是 {new}"
        )
    # 旧默认值与新默认值必须真的不同，否则这条迁移是空转
    assert all(o < n for o, n in order.values()), order
    assert 'UPDATE modalities SET enabled = 1 WHERE key = \'audio\'' in migration, (
        "迁移没有打开老库里那一行关着的音频模态"
    )


def test_the_audio_modality_is_on_in_the_seed():
    """种子里的 audio 必须开着（新装用户不该看到一个关着的能力）。"""
    row = next((m for m in _spec("modalities").seed if m["key"] == "audio"), None)
    assert row is not None, "能力表种子里没有 audio"
    assert row.get("enabled", True) is not False, "audio 在种子里还是关着的"


def test_api_paths_match_the_backend_routes():
    api = _text(SRC / "api.ts")
    assert 'request<SpeechVoices>("/api/audio/voices")' in api, "音色清单的路径不对"
    assert '"/api/audio/speech"' in api, "配音合成的路径不对"
    assert "speechVoices:" in api and "speech:" in api

    router = _text(BACKEND / "app" / "routers" / "audio.py")
    assert '@router.get("/voices")' in router and '@router.post("/speech")' in router
    assert 'prefix="/api/audio"' in router, "路由前缀与前端拼的路径不一致"
    main = _text(BACKEND / "app" / "main.py")
    assert "audio.router" in main, "配音路由没有挂到应用上（前端会拿到 404）"


def test_the_result_shape_matches_the_frontend_type():
    """`synthesize_to_asset` 返回的键 → 前端 `SpeechResult` 的字段，一个都不能少。

    这一版把「合成」与「落库」拆成了两层（一镜多人要按句各合成一次，而中间那几句
    **不登记资产**），所以契约分在两处：`synthesize_bytes` 给哪几个键、
    `synthesize_to_asset` 再补什么。只看其中一处会漏掉真正回给前端的东西。
    """
    service = _text(BACKEND / "app" / "services" / "speech_service.py")
    made = service[service.index("return result.audio, result.content_type, {") :]
    keys = set(re.findall(r'"(\w+)":', made[: made.index("\n    }")]))
    assert keys == {"chars", "text", "modelKey", "model", "voice", "speed"}, keys

    saved = service[
        service.index("async def synthesize_to_asset") : service.index("async def synthesize_to_temp")
    ]
    assert 'return asset, {**info, "seconds": seconds}' in saved, (
        "「落库」那一层没把合成给的键原样带上（前端会拿到一个空壳）"
    )

    types = _text(SRC / "types.ts")
    result = types[types.index("export interface SpeechResult") :]
    result = result[: result.index("}")]
    for key in sorted(keys - {"text"}):
        # `text` 是原样回显（前端不声明也用不到），其余每个键界面都要读
        assert key in result, f"前端 SpeechResult 里没有 {key}"
    assert "seconds" in result, "前端 SpeechResult 里没有 seconds"
    assert "asset: Asset" in result, "返回里没带上资产（界面要靠它播放与下载）"
    # 音色清单的形状
    voices = _text(BACKEND / "app" / "routers" / "audio.py")
    for key in ("presets", "maxChars", "speedRange", "speedDefault"):
        assert f'"{key}"' in voices and key in types, f"音色清单缺 {key}"


def test_the_voice_preference_is_a_real_pref_and_empty_is_legal():
    """留空 = 用服务默认音色，是**合法值**：不该往盘上写一个空串。"""
    prefs = _text(SRC / "prefs.ts")
    assert "voice:" in prefs and "xm_speech_voice" in prefs, "没有这个偏好项"
    block = prefs[prefs.index("  voice: {"):]
    block = block[: block.index("\n  },")]
    assert "dropRaw(K.voice)" in block, "留空时没有清掉键（会留下一个空串）"
    assert '?? ""' in block, "读回来没有兜底成空串"

    page = _text(SRC / "pages" / "SpeechPage.tsx")
    assert "prefs.voice.get()" in page and "prefs.voice.set(voice)" in page, (
        "配音页没有记住上次用的音色"
    )


def test_only_one_audio_element_is_created():
    """连点几条时用一个 `<audio>` 换 src：每次 new 一个会叠着放。"""
    page = _text(SRC / "pages" / "SpeechPage.tsx")
    tags = re.findall(r"<audio\s", page)
    assert len(tags) == 1, f"配音页里有 {len(tags)} 个 <audio> 标签"
    assert "audioRef" in page and "el.src = asset.url" in page, "播放没有复用同一个元素"
    assert "void el.play()" in page, "没有真的播（只是换了 src）"


def test_the_page_guides_to_settings_when_no_speech_model():
    """没配语音模型时给「去哪儿加」而不是一句「生成失败」。"""
    page = _text(SRC / "pages" / "SpeechPage.tsx")
    empty = page[page.index("if (models.length === 0)") : page.index("const tooLong")]
    assert "还没有可用的语音模型" in empty and "能力 = 音频" in empty, "空状态的引导没说清楚"
    assert "onGoSettings" in empty, "空状态没有去配置的入口"


def test_oversize_text_is_blocked_before_spending():
    """超长时按钮直接禁用，而不是让用户点下去再被后端拒。"""
    page = _text(SRC / "pages" / "SpeechPage.tsx")
    assert "meta.maxChars" in page, "没有用后端给的上限"
    assert "disabled={busy || tooLong}" in page, "超长时按钮没禁用"
    assert '? " danger" : ""' in page and ".field-hint.danger" in _text(SRC / "styles.css"), (
        "超限没有视觉提示"
    )


def test_the_sample_panel_offers_narration_and_reports_it():
    """样片面板要能勾「加旁白」、并把结果（几句、几字、比画面长多少）摆出来。"""
    canvas = _text(SRC / "pages" / "CanvasPage.tsx")
    assert "sampleNarration: e.target.checked" in canvas, "勾选框没有写回节点（刷新就丢了）"
    assert "sampleVoice: e.target.value" in canvas, "音色没有写回节点"
    assert "本地渲染 + 1 次配音" in canvas, "开了旁白却还写着「零成本」（用户会被误导）"
    assert "不与镜头逐一对齐" in canvas, "没有说明旁白是整段一条"
    for field in ("narration?.enabled", "narration.lines", "narration.chars", "narration.note"):
        assert field in canvas, f"报告里的 {field} 没显示出来"

    types = _text(SRC / "types.ts")
    assert "narration: {" in types and "sampleNarration?: boolean" in types, (
        "节点数据或报告类型里缺旁白字段"
    )
    for field in ("sampleVoice", "sampleNarration"):
        assert field in types, f"节点数据类型里没有 {field}"


def test_assets_page_and_lightbox_handle_audio():
    """音频没有画面：资产库与灯箱都要单独给分支，否则就是一张碎图。"""
    assets = _text(SRC / "pages" / "AssetsPage.tsx")
    assert '{ label: "音频", value: "audio" }' in assets, "资产库没有音频筛选"
    assert 'a.kind === "audio"' in assets and "asset-audio-thumb" in assets, (
        "音频资产没有自己的缩略图（会走 <img> 变成碎图）"
    )

    lightbox = _text(SRC / "components" / "Lightbox.tsx")
    assert 'kind === "audio"' in lightbox and "lightbox-audio" in lightbox, "灯箱没有音频分支"
    assert "<audio src={url} controls" in lightbox, "灯箱里放不出声音"
    assert "音频试听" in lightbox, "灯箱的无障碍标签还写着图片"


def test_styles_exist():
    """新加的类名要有样式，不然区块会挤成一坨。"""
    css = _text(SRC / "styles.css")
    for cls in (".speech-head", ".speech-voices", ".speech-voice", ".speech-player",
                ".speech-empty", ".speech-list", ".speech-item", ".speech-item-actions",
                ".asset-audio-thumb", ".lightbox-audio", ".field-hint-inline"):
        assert cls in css, f"styles.css 里没有 {cls}"


def test_the_page_uses_classes_that_exist():
    """页面里用的 `.speech-*` 每一个都要有样式——写错一个类名是**静默**的。"""
    css = _text(SRC / "styles.css")
    page = _text(SRC / "pages" / "SpeechPage.tsx")
    # 模板字符串里的 `${...}` 先拿掉，剩下的才是真正的类名
    names = re.findall(r'className="([^"]+)"', page)
    names += [re.sub(r"\$\{[^}]*\}", " ", s) for s in re.findall(r"className=\{`([^`]+)`\}", page)]
    used = {cls for group in names for cls in group.split() if not cls.startswith("$")}
    unknown = sorted(c for c in used if c.startswith("speech-") and f".{c}" not in css)
    assert not unknown, f"这些类名在 styles.css 里没有定义：{unknown}"


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
