"""#39 数字人（对白 / 口播）的前端接线检查（直接 python 运行）。

运行：venv/Scripts/python tests/test_frontend_digital_human.py

守四件容易出错、而且**出错时界面看起来是正常的**事：

1. **两个入口都要有**：视频生成页与画布的视频节点。只做一处，用户会在另一处找不到。
2. **提示文案只有一份**（后端 `/api/audio/voices` 的 `videoRefHint`）。前端各写一份
   必然漂移，漂移的表现是「同一个能力两个说法」。
3. **前端不判「配音够不够长」**：那是后端唯一的口径。前端跟着算，两套规则迟早
   长出两个答案，而这类不一致最难向用户解释。
4. **空选项是「不用对白」**：它是可选件，措辞上不该像个没填的必填项。

逐镜对白（每位角色一个音色 + 逐镜台词自动配音）那一块也在这里守：它把「一次点击」
变成了「N 次付费调用」，所以「三个设置有没有真写回节点」「额外那几次合成有没有
单独说出来」这两件事必须盯住。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
BACKEND = ROOT / "backend"

sys.path.insert(0, str(BACKEND))

from app.registry.canvas_nodes import NODE_SCHEMAS  # noqa: E402


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_the_video_node_offers_the_audio_slot():
    """注册表开了 `audioRef`，前端才会画出那个选择器——两边说的是同一件事。"""
    features = NODE_SCHEMAS["video"]["features"]
    assert "audioRef" in features, f"视频节点没开对白入口：{features}"
    owners = [k for k, s in NODE_SCHEMAS.items() if "audioRef" in (s.get("features") or [])]
    assert owners == ["video"], f"对白入口只该开在视频节点上，现在开在：{owners}"

    canvas = _text(SRC / "pages" / "CanvasPage.tsx")
    assert 'features.includes("audioRef")' in canvas, "面板没有按 features 画对白区块"
    assert "AudioRefPicker" in canvas, "画布面板里没有用那个选择器"
    assert "audioRefAssetId: asset ? asset.id : null" in canvas, (
        "选了音频没写回节点（刷新就丢）"
    )


def test_the_video_page_offers_it_too_and_sends_it():
    page = _text(SRC / "pages" / "VideoPage.tsx")
    assert "AudioRefPicker" in page, "视频生成页没有对白入口"
    assert "<AudioRefPicker value={audioRef?.id ?? null} onChange={setAudioRef} />" in page
    assert "audio_ref_asset_id: audioRef?.id ?? null," in page, "提交时没把对白音轨带上"
    # 掏钱前那一次确认必须说出「这一笔会带配音」——它决定买到的是有声还是无声的片子
    confirm = page[page.index("confirm-summary") : page.index("视频生成通常消耗较多额度")]
    assert "<span>对白</span>" in confirm, "确认弹窗里没提对白音轨"

    api = _text(SRC / "api.ts")
    assert "audio_ref_asset_id?: number | null;" in api, "接口类型里没有这个字段"


def test_the_picker_only_offers_audio_and_says_what_empty_means():
    picker = _text(SRC / "components" / "AudioRefPicker.tsx")
    assert 'kind: "audio"' in picker, "选择器把图片视频也列进来了（用户会挑错）"
    assert "不用对白（出无声片子）" in picker, "空选项的措辞像个没填的必填项"
    # 秒数要显示出来：视频时长够不够读完整句，全看这个数字
    assert "a.duration" in picker, "没显示配音时长"
    # 没有配音时说清楚去哪儿弄
    assert "配音" in picker and "还没有配音" in picker


def test_the_hint_comes_from_the_backend_not_from_a_second_copy():
    """提示只有一份（后端），前端两处都从接口拿。"""
    picker = _text(SRC / "components" / "AudioRefPicker.tsx")
    assert "videoRefHint" in picker and "speechVoices" in picker, "提示没有从接口拿"

    types = _text(SRC / "types.ts")
    assert "videoRefHint: string;" in types, "前端类型里没有这个字段"

    # 前端源码里不许再抄一份「哪几档模型认参考音频」的名单
    # （只看这句名单的特征片段——空状态里提一句「如豆包·Seedance…」是另一回事）
    marks = ("1.5 pro / 2.0 / 2.5", "1.0 系列不支持", "audio_ref_support")
    offenders = []
    for path in SRC.rglob("*.ts*"):
        text = _text(path)
        if any(m in text for m in marks):
            offenders.append(path.name)
    assert not offenders, f"这些文件里又抄了一份模型名单：{offenders}"


def test_the_frontend_does_not_second_guess_the_backend_duration_rule():
    """前端不许自己判「配音够不够长」——口径只在 `services/digital_human.py`。"""
    page = _text(SRC / "pages" / "VideoPage.tsx") + _text(SRC / "pages" / "CanvasPage.tsx")
    picker = _text(SRC / "components" / "AudioRefPicker.tsx")
    for text in (page, picker):
        assert "拆到下一镜" not in text, "后端那句建议被抄到前端来了"
        assert "Math.ceil" not in text, "前端在算「配音要几秒」——那套口径属于后端"


def test_the_new_types_and_styles_are_in_place():
    types = _text(SRC / "types.ts")
    assert "audioRefAssetId?: number | null;" in types, "节点数据类型里没有对白字段"
    css = _text(SRC / "styles.css")
    assert ".audio-ref-empty" in css, "没有配音时的空状态没有样式"


def test_the_api_serves_the_hint_the_frontend_reads():
    """字段名是前后端的契约：后端给 `videoRefHint`，前端就得读这个名字。"""
    from app.services import speech_service  # noqa: F401  确保服务层能导入
    from app.providers import ark

    assert ark.AUDIO_REF_NO_HINT, "后端那句提示是空的"
    router = _text(BACKEND / "app" / "routers" / "audio.py")
    assert '"videoRefHint": ark.AUDIO_REF_NO_HINT' in router, "接口没有把这个字段带出来"


def test_the_shot_dialogue_panel_is_declared_by_the_registry_and_drawn_by_it():
    """注册表声明 `shotDialogue`，面板就得按它画——声明了却不读，那份声明就是摆设。"""
    features = NODE_SCHEMAS["video"]["features"]
    assert "shotDialogue" in features, f"视频节点没声明逐镜对白：{features}"
    owners = [k for k, s in NODE_SCHEMAS.items() if "shotDialogue" in (s.get("features") or [])]
    assert owners == ["video"], f"逐镜对白只该开在视频节点上，现在开在：{owners}"

    canvas = _text(SRC / "pages" / "CanvasPage.tsx")
    assert 'features.includes("shotDialogue") && isShotVideoNode' in canvas, (
        "面板没有按 features 画逐镜对白区块（或漏了「整段一个视频时不该出现」）"
    )


def test_the_three_shot_dialogue_settings_are_written_back_to_the_node():
    """三个设置都得写回节点：不写回就是「改了、刷新就丢」，而它决定花多少钱。"""
    canvas = _text(SRC / "pages" / "CanvasPage.tsx")
    for snippet in (
        "patch({ shotDialogue: e.target.checked })",
        "patch({ shotVoice: e.target.value })",
        "patch({ shotVoices: e.target.value })",
    ):
        assert snippet in canvas, f"节点面板里没有写回：{snippet}"

    types = _text(SRC / "types.ts")
    for field in ("shotDialogue?: boolean;", "shotVoice?: string;", "shotVoices?: string;"):
        assert field in types, f"节点数据类型里没有 {field}"


def test_the_shot_dialogue_block_does_not_second_guess_the_money():
    """逐镜对白那一块不许自己算账、也不许另抄一句「会额外花多少」。

    次数与时长的口径都在后端（`services/digital_human.py` 与 `preview_graph`），
    前端跟着算就有两套答案，而这两套答案说的是「你要付多少钱」。
    """
    canvas = _text(SRC / "pages" / "CanvasPage.tsx")
    block = canvas[
        canvas.index('features.includes("shotDialogue")') : canvas.index("同场景串联")
    ]
    assert "Math.ceil" not in block, "前端在算「这镜的台词要几秒」——那套口径属于后端"
    assert "还会额外做" not in block, "那条「额外 N 次语音合成」的提示被抄到前端来了"
    # 那一块要说清「读不完的镜头会怎样」——用户看到成片少一段，最想知道的就是这个
    assert "读不完这一镜的台词不会出片" in block, "没告诉用户读不完时会跳过这一镜"
    # 音色表要给出写法示例，否则「每行一条」是一句空话
    assert "小焰=nova" in block, "音色表没有写法示例"
    assert "小焰=nova\\n阿澈=echo" in block, "音色表的 placeholder 不是多行示例"


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
