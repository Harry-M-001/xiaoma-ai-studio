"""#26 配音（TTS 试听 + 样片旁白）的回归测试（直接 python 运行）。

运行：venv/Scripts/python tests/test_speech.py

分三层，越往下越"真"：

1. **纯逻辑**（`services/speech.py`）：什么算合法文本、哪些行不是台词、
   语速怎么兜、旁白怎么拼。没有 IO，规则改坏了当场就红。
2. **接线**：`speech_service.synthesize_to_asset`（适配器/落盘/量时长都是替身）
   与 `canvas_runner._animatic_narration`（旁白接进样片那条路）。
   断言的是「交出去的参数」与「落库的字段」。
3. **真跑 ffmpeg**：把一条真音轨封进 mp4，用**视频流的 md5**证明画面是
   原样复制而不是重编码。没有 ffmpeg 就跳过。

为什么要单独立这一层：这一版有两个入口（配音页、样片旁白）用同一套判据，
它们对「什么算台词」「文本多长算长」的口径必须完全一致。测试把口径钉在
`services/speech.py` 一处，改的时候两边一起动。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Iterator

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import Asset, ProviderService  # noqa: E402
from app.providers import comfyui  # noqa: E402
from app.providers.ark import ArkAdapter  # noqa: E402
from app.providers.base import AdapterError, SpeechResult  # noqa: E402
from app.providers.dashscope import DashScopeAdapter  # noqa: E402
from app.providers.openai_compat import OpenAICompatAdapter  # noqa: E402
from app.services import (  # noqa: E402
    animatic,
    canvas_runner,
    config_center_service,
    ffmpeg_service,
    provider_store,
    speech,
    speech_service,
    storage,
    storyboard_sheet,
)

SHEET = """## 场景1 | 黄昏的站台

### 镜头1 | 大远景 | 缓慢推近 | 4s
- 画面：站台全景，列车驶入
- 台词：黄昏的站台，最后一班车还没来

### 镜头2 | 中景 | 向右横移 | 3s
- 画面：他抬手看表
- 台词：小焰：你终于来了。

### 镜头3 | 特写 | 固定 | 2s
- 画面：指节敲了两下
- 台词：（无）
"""

def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ================================================================ 1. 纯逻辑


def _shot(no: str, dialogue: str):
    return type("S", (), {"no": no, "dialogue": dialogue})()


def test_only_real_dialogue_is_read():
    """旁白只念台词；「（无）」这类占位不能变成一串「无无无」。"""
    shots = [
        _shot("1", "黄昏的站台，最后一班车还没来。"),
        _shot("2", "（无）"),
        _shot("3", "无"),
        _shot("4", "—"),
        _shot("5", ""),
        _shot("6", "小焰：你终于来了。"),
    ]
    text, lines = speech.narration_from_shots(shots)
    assert lines == 2, f"只有两句真台词，却数出了 {lines} 句"
    # 已经在句末带了句号的那条，后面不该再补一个（念出来是一段多余的停顿）
    assert text == "黄昏的站台，最后一班车还没来。你终于来了。", text


def test_two_lines_without_punctuation_do_not_run_together():
    """两条都没有句末标点时中间要补一个，否则会粘成一句。"""
    text, lines = speech.narration_from_shots([_shot("1", "他在门口站住"), _shot("2", "她回过头")])
    assert lines == 2
    assert text == "他在门口站住。她回过头", text


def test_no_dialogue_at_all_is_an_empty_result_not_a_placeholder():
    """一句台词都没有时返回空文本，由调用方去引导——不能悄悄拿画面描述顶上。"""
    text, lines = speech.narration_from_shots([_shot("1", "（无）"), _shot("2", "   ")])
    assert (text, lines) == ("", 0)


def test_placeholder_lines_are_not_dialogue():
    for line in ("（无）", "(无)", "无", "无台词", "无对白", "静音", "none", "N/A", "-", "--",
                 "—", "……", "。。。", "  （无）  "):
        assert not speech.is_speakable(line), f"「{line}」被当成了台词"
    for line in ("无人的站台", "他说：没有了", "静静地看着"):
        assert speech.is_speakable(line), f"「{line}」被误当成占位（句子被吃掉了）"


def test_speaker_label_is_stripped_but_only_when_it_is_short():
    assert speech.strip_speaker("小焰：你终于来了。") == "你终于来了。"
    assert speech.strip_speaker("【小焰】你终于来了。") == "你终于来了。"
    assert speech.strip_speaker("Xiao Yan: hello") == "hello"
    # 不含标签的句子原样返回
    assert speech.strip_speaker("你终于来了。") == "你终于来了。"
    # 超过 12 个字的「标签」不当说话人（正文里的冒号很常见，不能跟着一起切）
    long_label = "这是一个非常非常长的标签名：内容"
    assert speech.strip_speaker(long_label) == long_label


def test_the_speaker_is_read_from_the_same_labels_that_get_stripped():
    """说话人与剥标签必须对同一批写法给出**同一个答案**。

    它们各写一个正则迟早会漂移，而漂移的表现是「标签剥掉了、说话人却没认出来」——
    于是配音悄悄用了默认音色，用户只会觉得「我明明给这个角色配了音色，怎么没生效」。
    """
    for line, want in (
        ("小焰：你终于来了。", "小焰"),
        ("【小焰】你终于来了。", "小焰"),
        ("Xiao Yan: hello", "Xiao Yan"),
        ("你终于来了。", ""),
        ("这是一个非常非常长的标签名：内容", ""),
    ):
        assert speech.speaker_of(line) == want, f"「{line}」的说话人认成了 {speech.speaker_of(line)!r}"
        # 认得出说话人 ⇔ 剥得掉标签，这两件事必须同时成立
        assert bool(speech.speaker_of(line)) == (speech.strip_speaker(line) != line.strip()), line


def test_dialogue_parts_reads_one_shots_line_and_speaker():
    """逐镜对白要的就是这一镜的台词与说话人（一镜一条音轨）。"""
    assert speech.dialogue_parts(_shot("1", "小焰：你终于来了。")) == ("你终于来了。", "小焰")
    assert speech.dialogue_parts(_shot("2", "你终于来了")) == ("你终于来了", "")
    # 一镜里两行台词拼成一条（中间按句末标点补句号）
    assert speech.dialogue_parts(_shot("3", "他在门口站住\n她回过头"))[0] == "他在门口站住。她回过头"
    # 占位与空行都不算台词
    for empty in ("（无）", "无", "—", "", "   "):
        assert speech.dialogue_parts(_shot("4", empty)) == ("", ""), empty


def test_narration_and_per_shot_dialogue_agree_on_what_a_line_is():
    """整段旁白与逐镜对白必须对「这一镜有没有台词」给出同一个答案。

    不然会出现「样片里有旁白、逐镜出片却说没台词」这种最难解释的不一致。
    """
    sheet = "### 镜头1 | 中景 | 固定 | 3s\n- 画面：他抬手看表\n- 台词：（无）\n"
    shots = storyboard_sheet.parse_storyboard(sheet)
    assert speech.narration_from_shots(shots) == ("", 0)
    assert speech.dialogue_parts(shots[0]) == ("", "")
    # 两边的条数也要对得上
    sheet2 = SHEET
    shots2 = storyboard_sheet.parse_storyboard(sheet2)
    _text_all, lines = speech.narration_from_shots(shots2)
    per_shot = sum(1 for s in shots2 if speech.dialogue_parts(s)[0])
    assert lines == per_shot, f"整段旁白数出 {lines} 句，逐镜数出 {per_shot} 句"


def test_text_is_cleaned_without_rewriting():
    """不改写内容：标点、语气词都按用户写的念；只收拾空白。"""
    got = speech.sanitize_text("  黄昏的站台，\r\n\r\n\r\n最后一班车还没来。  \r\n")
    assert got == "黄昏的站台，\n\n最后一班车还没来。", repr(got)
    # 句中的单个换行保留（分段停顿由它表达）
    assert speech.sanitize_text("甲\n乙") == "甲\n乙"
    assert speech.sanitize_text("   ") == ""
    assert speech.sanitize_text(None) == ""


def test_text_error_says_what_to_do():
    assert speech.text_error("") == "请先写下要念的内容"
    assert speech.text_error("你好") == ""
    assert speech.text_error("字" * speech.MAX_CHARS) == ""
    over = speech.text_error("字" * (speech.MAX_CHARS + 1))
    assert str(speech.MAX_CHARS) in over and "分段" in over, over


def test_voice_is_not_a_whitelist_but_must_be_clean():
    """音色只做「去空白 + 拒控制字符」：各家音色名不通用，写死白名单等于逼用户等发版。"""
    assert speech.sanitize_voice("  nova  ") == "nova"
    assert speech.sanitize_voice("zh-CN-XiaoxiaoNeural") == "zh-CN-XiaoxiaoNeural"
    assert speech.sanitize_voice("自建音色-01") == "自建音色-01"
    assert speech.sanitize_voice("a\tb\nc") == "abc"
    # 留空是合法的（用服务默认音色）
    assert speech.sanitize_voice(None) == ""
    assert speech.sanitize_voice("   ") == ""
    assert len(speech.sanitize_voice("x" * 500)) == 80


def test_speed_falls_back_and_clamps():
    assert speech.sanitize_speed(1) == 1.0
    assert speech.sanitize_speed("1.25") == 1.25
    assert speech.sanitize_speed("abc") == speech.SPEED_DEFAULT
    assert speech.sanitize_speed(None) == speech.SPEED_DEFAULT
    assert speech.sanitize_speed(float("nan")) == speech.SPEED_DEFAULT
    # 夹取而不是报错：用户把滑杆拖过头不该得到一次失败
    assert speech.sanitize_speed(0.1) == speech.SPEED_MIN
    assert speech.sanitize_speed(9) == speech.SPEED_MAX
    assert speech.sanitize_speed(1.234) == 1.23


def test_asset_name_fits_in_the_library():
    name = speech.asset_name("黄昏的站台，最后一班车还没来。")
    assert name.startswith("配音 · 黄昏的站台"), name
    assert len(name) <= len("配音 · ") + 16, "名字太长，资产库里会挤成一行"
    assert "\n" not in speech.asset_name("第一行\n第二行"), "换行没压成空格"
    assert speech.asset_name("") == "配音"


def test_truncation_note_only_when_the_voice_overruns():
    note = speech.truncation_note(audio_seconds=8.0, video_seconds=5)
    assert "8 秒" in note and "长 3 秒" in note and "截掉" in note, note
    assert "调大" in note, "没告诉用户怎么让旁白读完"
    # 短了或刚好齐平不用说：画面先结束是正常观感
    assert speech.truncation_note(audio_seconds=5.0, video_seconds=5) == ""
    assert speech.truncation_note(audio_seconds=5.4, video_seconds=5) == ""
    assert speech.truncation_note(audio_seconds=None, video_seconds=5) == ""
    assert speech.truncation_note(audio_seconds=8.0, video_seconds=None) == ""


def test_duration_seconds_rounds_and_rejects_junk():
    assert speech.duration_seconds(3.6) == 4
    assert speech.duration_seconds(0.2) == 1
    assert speech.duration_seconds(0) is None
    assert speech.duration_seconds(-3) is None
    assert speech.duration_seconds("abc") is None
    assert speech.duration_seconds(None) is None


def test_the_char_cap_has_a_single_source_of_truth():
    """前端显示的「2000 字」必须来自后端：两处各写一个数，迟早出现
    「界面说还能写、点下去后端说超了」。"""
    src = _text(BACKEND / "app" / "routers" / "audio.py")
    assert '"maxChars": speech.MAX_CHARS' in src, "上限没有从 speech 模块取"
    assert str(speech.MAX_CHARS) not in src, "路由里硬编码了字数上限"


# ================================================================ 2. 适配器


class _FakeResp:
    def __init__(self, status_code: int = 200, content: bytes = b"", content_type: str = "audio/mpeg"):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type} if content_type else {}
        self.text = self.content.decode("utf-8", "replace")[:200]

    def json(self):
        try:
            return json.loads(self.text)
        except json.JSONDecodeError:
            return None


class _FakeClient:
    def __init__(self, resp: _FakeResp) -> None:
        self.resp = resp
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, **kwargs) -> _FakeResp:
        self.posts.append((url, kwargs))
        return self.resp


def _openai(resp: _FakeResp) -> tuple[OpenAICompatAdapter, _FakeClient]:
    adapter = OpenAICompatAdapter("https://api.example.com/v1", "sk-test")
    client = _FakeClient(resp)

    async def fake_client():  # noqa: ANN202
        return client

    adapter.client = fake_client  # type: ignore[method-assign]
    return adapter, client


def test_openai_speech_posts_the_expected_body():
    """请求体是与上游的契约：路径、字段名，以及产物照响应头带回来。"""
    adapter, client = _openai(_FakeResp(200, b"\xff\xfb\x90\x00", "audio/mpeg"))
    result = asyncio.run(
        adapter.synthesize_speech(model="tts-1", text="你好", voice="nova", speed=1.5)
    )
    url, kwargs = client.posts[0]
    assert url == "https://api.example.com/v1/audio/speech", url
    body = kwargs["json"]
    assert body["model"] == "tts-1" and body["input"] == "你好", body
    assert body["response_format"] == "mp3", "没请求 mp3（兼容面最广的一档）"
    assert body["voice"] == "nova" and body["speed"] == 1.5
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert result.audio == b"\xff\xfb\x90\x00" and result.content_type == "audio/mpeg"


def test_default_voice_and_speed_are_left_out():
    """留空音色 / 默认语速时不带这两个字段：不是所有兼容服务都认它们。"""
    adapter, client = _openai(_FakeResp(200, b"x", "audio/mpeg"))
    asyncio.run(adapter.synthesize_speech(model="tts-1", text="你好", voice="", speed=1.0))
    body = client.posts[0][1]["json"]
    assert "voice" not in body, "空音色还是带出去了（会让不认这个字段的服务直接报错）"
    assert "speed" not in body, "默认语速还是带出去了"


def test_non_audio_content_type_is_stored_as_mp3():
    """非 audio/* 一律按 mp3：照 `application/octet-stream` 存只会得到 `.bin`，
    浏览器拿到 octet-stream 后 `<audio>` 直接不认。"""
    for ct in ("application/octet-stream", "binary/octet-stream", "text/plain", "application/json", ""):
        adapter, _c = _openai(_FakeResp(200, b"x", ct))
        assert asyncio.run(adapter.synthesize_speech(model="t", text="你好")).content_type == "audio/mpeg", ct
    for ct, want in (("audio/wav", "audio/wav"), ("audio/mpeg; charset=binary", "audio/mpeg"),
                     ("audio/ogg", "audio/ogg")):
        adapter, _c = _openai(_FakeResp(200, b"x", ct))
        assert asyncio.run(adapter.synthesize_speech(model="t", text="你好")).content_type == want, ct


def test_empty_audio_is_an_error_not_a_zero_byte_asset():
    """200 但零字节时要说人话，而不是往资产库里塞一条放不出声音的空文件。"""
    adapter, _c = _openai(_FakeResp(200, b"", "audio/mpeg"))
    try:
        asyncio.run(adapter.synthesize_speech(model="tts-1", text="你好"))
    except AdapterError as e:
        assert "空音频" in str(e) and "重试" in str(e), str(e)
        return
    raise AssertionError("空音频竟然成功了")


def test_upstream_400_becomes_a_readable_message_without_leaking_the_body():
    adapter, _c = _openai(_FakeResp(400, b'{"error": {"message": "bad model"}}', "application/json"))
    try:
        asyncio.run(adapter.synthesize_speech(model="tts-nope", text="你好"))
    except AdapterError as e:
        assert "400" in str(e), str(e)
        assert "bad model" not in (e.log_detail or ""), "上游正文进了日志摘要"
        return
    raise AssertionError("400 竟然成功了")


def test_adapters_without_speech_say_so_with_a_reason():
    """方舟/百炼/ComfyUI 的语音各是另一套口径，这一版都不接——但要说清楚为什么，
    并指出该怎么接（否则用户只会以为功能坏了）。"""
    async def go(adapter):  # noqa: ANN001
        try:
            await adapter.synthesize_speech(model="m", text="你好")
        except AdapterError as e:
            return str(e)
        raise AssertionError(f"{type(adapter).__name__} 竟然合成了语音")

    msgs = [
        asyncio.run(go(ArkAdapter("https://ark.cn-beijing.volces.com/api/v3", "k"))),
        asyncio.run(go(DashScopeAdapter("https://dashscope.aliyuncs.com", "k"))),
        asyncio.run(go(comfyui.ComfyUIAdapter("http://127.0.0.1:8188", ""))),
    ]
    for msg in msgs:
        assert "不支持" in msg and "语音" in msg, msg
    assert len(set(msgs)) == 3, "三个适配器给了同一句话，等于没说清楚各自的原因"
    assert "OpenAI 兼容" in msgs[0], "方舟没告诉用户该换哪种类型接入"


# ================================================================ 3. 接线


def _db_run(fn) -> None:  # noqa: ANN001
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            async with maker() as db:
                await fn(db)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


class _FakeAdapter:
    def __init__(self, captured: dict, audio: bytes, content_type: str) -> None:
        self._captured = captured
        self._audio = audio
        self._ct = content_type

    async def synthesize_speech(self, **kwargs):  # noqa: ANN003, ANN201
        self._captured["adapter"] = kwargs
        return SpeechResult(audio=self._audio, content_type=self._ct)

    async def close(self) -> None:
        self._captured["closed"] = True


@contextlib.contextmanager
def _stub_pipeline(
    *,
    audio: bytes = b"\xff\xfb\x90\x00" * 64,
    content_type: str = "audio/mpeg",
    seconds: float | None = 4.2,
    resolve_error: str = "",
) -> Iterator[dict]:
    """替身包住「解析模型 → 调上游 → 落盘 → 量时长」四步，只把中间参数暴露出来。"""
    captured: dict = {}

    class _Service:
        id = 7

    class _Resolved:
        service = _Service()
        adapter = _FakeAdapter(captured, audio, content_type)
        model_name = "tts-1"
        modality = "audio"
        label = "tts-1"

    async def fake_resolve(db, model_key, expected_modality):  # noqa: ANN001, ANN202
        captured["resolve"] = (model_key, expected_modality)
        if resolve_error:
            raise AdapterError(resolve_error)
        return _Resolved()

    def fake_save_bytes(data, ct, preferred_ext=""):  # noqa: ANN001, ANN202
        captured["saved"] = (data, ct, preferred_ext)
        return f"2026-09/voice-{uuid.uuid4().hex[:6]}.{preferred_ext or 'bin'}"

    async def fake_probe(path):  # noqa: ANN001, ANN202
        return seconds

    real = (
        provider_store.resolve_model,
        storage.save_bytes,
        storage.abs_path,
        ffmpeg_service.probe_audio_seconds,
    )
    provider_store.resolve_model = fake_resolve
    storage.save_bytes = fake_save_bytes  # type: ignore[assignment]
    storage.abs_path = lambda name: Path("C:/fake") / Path(name).name  # type: ignore[assignment]
    ffmpeg_service.probe_audio_seconds = fake_probe  # type: ignore[assignment]
    try:
        yield captured
    finally:
        (
            provider_store.resolve_model,
            storage.save_bytes,
            storage.abs_path,
            ffmpeg_service.probe_audio_seconds,
        ) = real  # type: ignore[assignment]


@contextlib.contextmanager
def _config(value: str) -> Iterator[None]:
    """把 `defaults.speech_model` 固定成一个值（不碰真实配置库）。"""
    real = config_center_service.runtime_value
    config_center_service.runtime_value = (  # type: ignore[assignment]
        lambda key, default=None: value if key == "defaults.speech_model" else default
    )
    try:
        yield
    finally:
        config_center_service.runtime_value = real  # type: ignore[assignment]


def test_synthesize_stores_an_audio_asset_with_the_upstream_bytes():
    """一次成功的合成：字节原样落库、扩展名跟着上游的内容类型、正文存进 prompt。"""
    raw = b"RIFFfakewav-bytes"
    with _stub_pipeline(audio=raw, content_type="audio/wav", seconds=4.2) as captured:
        async def scenario(db):  # noqa: ANN001
            asset, meta = await speech_service.synthesize_to_asset(
                db, model_key="7:tts-1", text="  黄昏的站台。  ", voice=" nova ",
                speed=1.5, source="tts", name="配音 · 测试",
            )
            assert asset.kind == "audio" and asset.source == "tts"
            assert asset.content_type == "audio/wav" and asset.filename.endswith(".wav"), asset.filename
            assert asset.size == len(raw), "落库的字节数和上游给的不一致"
            assert asset.duration == 4, f"时长应当由探测结果四舍五入，实际 {asset.duration}"
            assert asset.name == "配音 · 测试"
            assert asset.prompt == "黄昏的站台。", f"正文没存对：{asset.prompt!r}"
            assert meta["chars"] == 6 and meta["seconds"] == 4.2
            assert meta["voice"] == "nova" and meta["speed"] == 1.5
            assert meta["modelKey"] == "7:tts-1" and meta["model"] == "tts-1"

        _db_run(scenario)

    assert captured["adapter"] == {
        "model": "tts-1", "text": "黄昏的站台。", "voice": "nova", "speed": 1.5
    }, captured["adapter"]
    assert captured["closed"] is True, "适配器没关，httpx 连接会攒到进程退出"
    assert captured["resolve"] == ("7:tts-1", "audio"), "没有按 audio 能力解析模型"
    assert captured["saved"][1] == "audio/wav" and captured["saved"][2] == "wav", captured["saved"]


def test_bad_text_never_reaches_the_upstream():
    """文本不合规要在付钱之前挡住——这条路每走一次都是真金白银。"""
    with _stub_pipeline() as captured:
        async def scenario(db):  # noqa: ANN001
            for text, want in (("   ", "请先写下要念的内容"),
                               ("字" * (speech.MAX_CHARS + 1), "分段")):
                try:
                    await speech_service.synthesize_to_asset(db, model_key="7:tts-1", text=text)
                except AdapterError as e:
                    assert want in str(e), f"{want!r} 没出现在报错里：{e}"
                else:
                    raise AssertionError("不合规的文本竟然发出去了")

        _db_run(scenario)

    assert "adapter" not in captured, "文本被拒了却还是调了上游（白花一次钱）"


def test_missing_model_key_says_where_to_configure():
    with _stub_pipeline() as captured:
        async def scenario(db):  # noqa: ANN001
            try:
                await speech_service.synthesize_to_asset(db, model_key="", text="你好")
            except AdapterError as e:
                assert "模型服务" in str(e) and "音频" in str(e), str(e)
                return
            raise AssertionError("没有模型却成功了")

        _db_run(scenario)
    assert "adapter" not in captured


def test_a_probe_that_cannot_read_a_duration_does_not_fail_the_synthesis():
    """量不出时长只是界面上少一个数字，不该把一次已经付过钱的合成判失败。"""
    with _stub_pipeline(seconds=None) as _captured:
        async def scenario(db):  # noqa: ANN001
            asset, meta = await speech_service.synthesize_to_asset(
                db, model_key="7:tts-1", text="你好"
            )
            assert asset.duration is None and meta["seconds"] is None
            assert asset.prompt == "你好"

        _db_run(scenario)


def test_default_model_key_prefers_the_configured_one():
    """配置里填了就听配置的：不该再去猜（猜错一次就是替用户花一次钱）。"""
    with _config("9:tts-1"):
        async def scenario(db):  # noqa: ANN001
            assert await speech_service.default_model_key(db) == "9:tts-1"

        _db_run(scenario)


def _seed_services(db, specs: list[tuple[str, str]]) -> None:
    """建几个服务，specs 是 (服务名, 模型名的能力)。"""
    for idx, (name, modality) in enumerate(specs):
        db.add(ProviderService(
            name=name, kind="openai", base_url="https://api.example.com/v1",
            api_key_enc="x", enabled=True, sort_order=idx,
            models_json=json.dumps([{"name": f"tts-{idx}", "modality": modality}]),
        ))


def test_default_model_key_uses_the_only_candidate_without_asking():
    """只有一个语音模型时自动用它——只有一个还要用户先去设置里填一遍纯属为难人。"""
    with _config(""):
        async def scenario(db):  # noqa: ANN001
            _seed_services(db, [("甲", "text"), ("乙", "audio")])
            await db.commit()
            key = await speech_service.default_model_key(db)
            assert key.endswith(":tts-1"), f"没自动选中唯一的语音模型：{key}"

        _db_run(scenario)


def test_default_model_key_refuses_to_guess_between_several():
    with _config(""):
        async def scenario(db):  # noqa: ANN001
            _seed_services(db, [("甲", "audio"), ("乙", "audio")])
            await db.commit()
            try:
                await speech_service.default_model_key(db)
            except AdapterError as e:
                assert "语音模型" in str(e) and "默认语音模型" in str(e), str(e)
                return
            raise AssertionError("两个候选却自己挑了一个（猜错就是替用户花钱）")

        _db_run(scenario)


def test_default_model_key_without_any_candidate_gives_a_path():
    with _config(""):
        async def scenario(db):  # noqa: ANN001
            _seed_services(db, [("甲", "text")])
            await db.commit()
            try:
                await speech_service.default_model_key(db)
            except AdapterError as e:
                assert "模型服务" in str(e) and "音频" in str(e), str(e)
                return
            raise AssertionError("一个语音模型都没有却给出了一个 key")

        _db_run(scenario)


def _plan(sheet: str = SHEET, video_seconds: int | None = None):
    shots = storyboard_sheet.parse_storyboard(sheet)
    plan = animatic.plan(shots, {s.no: object() for s in shots})
    return shots, plan.clips, plan.seconds


def test_animatic_narration_is_off_by_default():
    """没勾选就一个字都不该合成（那是付费调用）。"""
    with _stub_pipeline() as captured:
        async def scenario(db):  # noqa: ANN001
            shots, clips, seconds = _plan()
            path, report = await canvas_runner._animatic_narration(
                db, data={}, shots=shots, clips=clips, video_seconds=seconds
            )
            assert path is None and report == {"enabled": False}

        _db_run(scenario)
    assert "adapter" not in captured, "没开旁白却调了上游"


def test_animatic_narration_reads_the_dialogue_of_the_shots_in_the_sample():
    """只念**进片的那几镜**的台词，而且是「整段一条」（报告里如实写明未逐句对齐）。"""
    with _stub_pipeline(seconds=4.0) as captured, _config("7:tts-1"):
        async def scenario(db):  # noqa: ANN001
            shots, clips, seconds = _plan()
            # 只留第 2 镜进片：第 1、3 镜的台词不该出现在旁白里
            one = [c for c in clips if c.shot_no == "2"]
            path, report = await canvas_runner._animatic_narration(
                db, data={"sampleNarration": True}, shots=shots, clips=one,
                video_seconds=sum(c.seconds for c in one),
            )
            assert path is not None
            assert report["enabled"] is True and report["lines"] == 1, report
            assert report["chars"] == len("你终于来了。"), report
            assert report["voice"] == "服务默认音色"
            assert report["note"] == "" or "截掉" in report["note"], report

            asset = (await db.execute(select(Asset).where(Asset.kind == "audio"))).scalars().one()
            assert asset.source == "animatic"
            assert asset.name == "样片旁白 · 1 句", asset.name
            assert "未按镜头逐句对齐" in (asset.prompt or ""), "没有在资产里说明这一段的来路"

        _db_run(scenario)
    assert captured["adapter"]["text"] == "你终于来了。", captured["adapter"]
    assert captured["adapter"]["voice"] == "", "节点上的音色没传下去"


def test_animatic_narration_says_when_it_overruns_the_picture():
    with _stub_pipeline(seconds=9.0) as _captured, _config("7:tts-1"):
        async def scenario(db):  # noqa: ANN001
            shots, clips, _seconds = _plan()
            _path, report = await canvas_runner._animatic_narration(
                db, data={"sampleNarration": True}, shots=shots, clips=clips, video_seconds=2
            )
            assert "长" in report["note"] and "截掉" in report["note"], report["note"]

        _db_run(scenario)


def test_animatic_narration_uses_the_voice_on_the_node():
    with _stub_pipeline() as captured, _config("7:tts-1"):
        async def scenario(db):  # noqa: ANN001
            shots, clips, seconds = _plan()
            await canvas_runner._animatic_narration(
                db, data={"sampleNarration": True, "sampleVoice": "  nova  "},
                shots=shots, clips=clips, video_seconds=seconds,
            )

        _db_run(scenario)
    assert captured["adapter"]["voice"] == "nova", "节点上填的音色没有去空白后用上"


def test_animatic_narration_without_dialogue_guides_to_the_storyboard():
    """一句台词都没有时要给出「去哪儿补」，而不是拿画面描述凑一段解说词。"""
    sheet = "### 镜头1 | 中景 | 固定 | 2s\n- 画面：他抬手看表\n- 台词：（无）\n"
    with _stub_pipeline() as captured:
        async def scenario(db):  # noqa: ANN001
            shots, clips, seconds = _plan(sheet)
            try:
                await canvas_runner._animatic_narration(
                    db, data={"sampleNarration": True}, shots=shots, clips=clips,
                    video_seconds=seconds,
                )
            except canvas_runner.AnimaticInputError as e:
                assert "台词" in str(e) and "分镜" in str(e), str(e)
                return
            raise AssertionError("一句台词都没有却配出了旁白")

        _db_run(scenario)
    assert "adapter" not in captured, "没有台词却还是调了上游"


def test_animatic_narration_reports_upstream_failure_as_an_input_error():
    """上游/配置类问题要按「输入不对」（400）上报，别让它变成一句 500。"""
    with _stub_pipeline(resolve_error="模型服务不存在，可能已被删除") as captured, _config("7:tts-1"):
        async def scenario(db):  # noqa: ANN001
            shots, clips, seconds = _plan()
            try:
                await canvas_runner._animatic_narration(
                    db, data={"sampleNarration": True}, shots=shots, clips=clips,
                    video_seconds=seconds,
                )
            except canvas_runner.AnimaticInputError as e:
                assert "配旁白失败" in str(e) and "模型服务不存在" in str(e), str(e)
                return
            raise AssertionError("解析模型失败却出片成功了")

        _db_run(scenario)
    assert "adapter" not in captured


def test_the_report_shape_matches_the_frontend_type():
    """报告字段名是前后端的契约：后端改名而前端没跟着改，界面上只是少一块。"""
    types = _text(BACKEND.parent / "frontend" / "src" / "types.ts")
    for field in ("enabled", "assetId", "url", "chars", "lines", "seconds", "voice", "note"):
        assert field in types, f"前端类型里没有 narration.{field}"


# ================================================================ 4. 真跑 ffmpeg


def _ffmpeg_or_skip() -> str | None:
    ok, _version = asyncio.run(ffmpeg_service.ffmpeg_available())
    if not ok:
        print("    跳过：本机没有可用的 ffmpeg", file=sys.stderr)
        return None
    ffmpeg, _probe = asyncio.run(ffmpeg_service._resolve_binaries())
    return ffmpeg


@contextlib.contextmanager
def _out_in(folder: Path) -> Iterator[None]:
    """把 ffmpeg 的产物目录指到临时目录：跑测试不该往用户的数据目录里写东西。"""
    real = ffmpeg_service._out_path
    ffmpeg_service._out_path = lambda ext: folder / f"{uuid.uuid4().hex[:8]}.{ext}"  # noqa: ARG005
    try:
        yield
    finally:
        ffmpeg_service._out_path = real


def _mk(ffmpeg: str, cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr[-400:]


def _stream_md5(ffmpeg: str, path: Path, selector: str) -> str:
    """取出某条流**原样复制**后的 md5。

    这是「画面有没有被重编码」最硬的证据：只要 `-c:v copy` 生效，
    封音轨前后的视频流字节完全一致，md5 必然相等；一旦改成重编码就会变。
    """
    r = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-map", selector, "-c", "copy", "-f", "md5", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr[-400:]
    return r.stdout.strip()


def _testsrc(ffmpeg: str, out: Path, seconds: str = "2") -> None:
    _mk(ffmpeg, [ffmpeg, "-v", "error", "-y", "-f", "lavfi",
                 "-i", "testsrc=size=320x180:rate=15", "-t", seconds,
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)])


def _voice(ffmpeg: str, out: Path, seconds: str = "2") -> None:
    _mk(ffmpeg, [ffmpeg, "-v", "error", "-y", "-f", "lavfi",
                 "-i", f"sine=frequency=440:duration={seconds}", str(out)])


def test_real_mux_copies_the_picture_and_adds_the_voice():
    """封音轨前后**视频流的 md5 必须一样**：这是「画面没被重编码」最硬的证据。"""
    ffmpeg = _ffmpeg_or_skip()
    if not ffmpeg:
        return

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        silent = root / "silent.mp4"
        voice = root / "voice.wav"
        _testsrc(ffmpeg, silent)
        _voice(ffmpeg, voice)

        with _out_in(root):
            muxed = asyncio.run(ffmpeg_service.mux_audio(silent, voice))
        assert _stream_md5(ffmpeg, silent, "0:v:0") == _stream_md5(ffmpeg, muxed, "0:v:0"), (
            "画面被重新编码了（`-c:v copy` 没生效）"
        )
        assert asyncio.run(ffmpeg_service.probe_audio_seconds(muxed)), "音轨没合进去"
        meta = asyncio.run(ffmpeg_service.probe(muxed))
        assert abs(meta["duration"] - 2.0) < 0.2, meta
        assert (meta["width"], meta["height"]) == (320, 180), meta


def test_real_mux_does_not_cut_the_picture_when_the_voice_is_short():
    """旁白比画面短时，成片必须还是画面的长度。

    这条是真跑出来的：`-shortest` 会在旁白读完那一刻把整个输出截断——
    用户要一条 2 秒的样片、配上一段 1 秒的旁白，结果只剩 1 秒画面。
    补静音（`-af apad`）之后画面才保得住。
    """
    ffmpeg = _ffmpeg_or_skip()
    if not ffmpeg:
        return

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        silent = root / "silent.mp4"
        voice = root / "voice.wav"
        _testsrc(ffmpeg, silent, "2")
        _voice(ffmpeg, voice, "1")

        with _out_in(root):
            muxed = asyncio.run(ffmpeg_service.mux_audio(silent, voice))
        meta = asyncio.run(ffmpeg_service.probe(muxed))
        assert abs(meta["duration"] - 2.0) < 0.2, f"画面被旁白截短了：{meta}"
        assert asyncio.run(ffmpeg_service.probe_audio_seconds(muxed)), "音轨没合进去"


def test_real_animatic_with_narration_keeps_the_voice_and_drops_the_silent_cut():
    """真实链：一镜样片 + 旁白 → 成片里有音轨，且无声那版中间产物是删掉的。"""
    ffmpeg = _ffmpeg_or_skip()
    if not ffmpeg:
        return

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "src.png"
        voice = root / "voice.wav"
        _mk(ffmpeg, [ffmpeg, "-v", "error", "-y", "-f", "lavfi",
                     "-i", "testsrc=size=640x360:rate=1", "-frames:v", "1", str(src)])
        _voice(ffmpeg, voice, "2")

        shots = storyboard_sheet.parse_storyboard("### 镜头1 | 中景 | 固定 | 2s\n- 画面：甲\n")
        clip = animatic.plan(shots, {"1": object()}).clips[0]

        out = root / "out"
        out.mkdir()
        with _out_in(out):
            video = asyncio.run(ffmpeg_service.render_animatic(
                [(src, clip, (640, 360))], out_size=(320, 180), audio=voice
            ))

        assert str(video).startswith(str(out)), f"产物没落在指定的输出目录：{video}"
        assert asyncio.run(ffmpeg_service.probe_audio_seconds(video)), "旁白没有合进成片"
        meta = asyncio.run(ffmpeg_service.probe(video))
        assert abs(meta["duration"] - 2.0) < 0.3, meta
        leftovers = [p for p in out.glob("*.mp4") if p != Path(video)]
        assert not leftovers, f"无声那版中间产物没删：{leftovers}"
        work = ffmpeg_service.settings_storage_dir() / "tmp"
        if work.exists():
            assert not list(work.glob("animatic-*")), "临时片段目录没清干净"


if __name__ == "__main__":
    _ORDER = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in _ORDER:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(_ORDER) - failed}/{len(_ORDER)} passed")
    sys.exit(1 if failed else 0)
