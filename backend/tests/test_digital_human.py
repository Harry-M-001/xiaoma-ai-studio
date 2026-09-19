"""#39 数字人（对白 / 口播）的回归测试（直接 python 运行）。

运行：venv/Scripts/python tests/test_digital_human.py

这一条路的每一步都连着钱：**上游按秒计费、一次出片又慢又贵**。所以这里守四件事：

1. **时长口径**（`services/digital_human.py`）：配音比所选时长长时**拦住**，
   不替用户把 5 秒改成 10 秒——改时长就是改钱，得他自己点那一下。
2. **请求体形状**：参考音频要真的进了 `content[]`（`role=reference_audio`），
   且没勾的时候**一个字节都不许多发**。
3. **不许静默降级**：适配器实现不了、音频文件丢了、选错了资产，全都要报错。
   悄悄出一段没人说话的片子，用户只会以为功能坏了，而钱已经花了。
4. **本地能判的先判**：格式不是 wav/mp3、单个超过 15 MB，这些在本地就拦住，
   省一次必然失败的上游往返。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import Asset, Project, Task  # noqa: E402
from app.providers import ark as ark_mod  # noqa: E402
from app.providers.ark import ArkAdapter  # noqa: E402
from app.providers.base import AdapterError, AudioRef  # noqa: E402
from app.providers.dashscope import DashScopeAdapter  # noqa: E402
from app.providers.openai_compat import OpenAICompatAdapter  # noqa: E402
from app.services import (  # noqa: E402
    canvas_runner,
    digital_human,
    provider_store,
    runner,
    speech_service,
)
from app.services.runner import runner as task_runner  # noqa: E402

SRC = BACKEND.parent / "frontend" / "src"
WAV = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + bytes(64)
MP3 = b"ID3\x03\x00\x00\x00" + bytes(64)


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ================================================================ 1. 时长口径


def test_audio_seconds_round_up_with_a_tail_tolerance():
    """配音要几秒：向上取整；结尾那零点零几秒是编码器尾巴，不算台词。"""
    assert digital_human.dialogue_seconds(None) is None
    assert digital_human.dialogue_seconds(0) is None
    assert digital_human.dialogue_seconds(-1) is None
    assert digital_human.dialogue_seconds("abc") is None
    assert digital_human.dialogue_seconds(3.0) == 3
    assert digital_human.dialogue_seconds(3.02) == 3, "0.02 秒的尾巴不该逼用户多花一秒钱"
    assert digital_human.dialogue_seconds(3.2) == 4
    assert digital_human.dialogue_seconds(4.62) == 5
    assert digital_human.dialogue_seconds(9.4) == 10


def test_the_line_fits_the_chosen_duration():
    assert digital_human.check_duration(requested=5, audio_seconds=4.62, name="配音 · 甲") == ""
    # 恰好齐平也算读得完（留了容差）
    assert digital_human.check_duration(requested=5, audio_seconds=5.0, name="配音 · 甲") == ""
    assert digital_human.check_duration(requested=5, audio_seconds=5.04, name="配音 · 甲") == ""


def test_a_longer_line_is_refused_with_something_to_do():
    """核心口径：**不自动改时长**，拦住并给两条可照做的路。"""
    msg = digital_human.check_duration(requested=5, audio_seconds=8.4, name="配音 · 黄昏")
    assert "配音 · 黄昏" in msg and "8.4 秒" in msg and "5 秒" in msg, msg
    assert "调到 9 秒以上" in msg, "没告诉用户该把时长调到几秒"
    assert "拆到下一镜" in msg, "只说了不行，没给第二条路"


def test_a_line_longer_than_any_duration_gets_a_different_sentence():
    """配音本身就超过单段视频上限时，让他「调到 N 秒」是句废话——换一句说。"""
    msg = digital_human.check_duration(requested=5, audio_seconds=40.0, name="配音 · 长文")
    assert "40 秒" in msg, msg
    assert "调到" not in msg, f"这句建议在物理上做不到：{msg}"
    assert "拆到下一镜" in msg, msg


def test_an_unknown_length_does_not_invent_a_reason_to_block():
    """量不出时长就不装懂：按用户选的时长走，别拦住他。"""
    assert digital_human.check_duration(requested=5, audio_seconds=None, name="配音 · 甲") == ""


def test_attach_note_says_which_voice_and_how_long():
    assert digital_human.attach_note(name="配音 · 黄昏", seconds=4.62) == "对白音轨：配音 · 黄昏（4.6 秒）"
    # 时长读不出来要说出来，不然「时长为什么是这么多」事后无从查起
    assert "没量出来" in digital_human.attach_note(name="配音 · 黄昏", seconds=None)
    assert digital_human.attach_note(name="", seconds=3.0) == "对白音轨：未命名配音（3.0 秒）"


# ================================================================ 2. 方舟适配器


class _FakeResp:
    def __init__(self, status_code: int = 200, payload=None, content: bytes = b"") -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {"id": "task-123"}
        self.content = content
        self.text = json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, resp: _FakeResp) -> None:
        self.resp = resp
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, **kwargs) -> _FakeResp:
        self.posts.append((url, kwargs))
        return self.resp


def _ark(resp: _FakeResp | None = None):
    adapter = ArkAdapter("https://ark.example.com/api/v3", "k")
    client = _FakeClient(resp or _FakeResp())

    async def fake_client():
        return client

    adapter.client = fake_client  # type: ignore[method-assign]
    return adapter, client


def _submit(adapter, **kwargs):
    return asyncio.run(adapter.submit_video(prompt="一个人在说话", **kwargs))


def test_the_reference_audio_really_goes_into_the_request():
    """这是这一版的核心契约：`audio_url` + `role=reference_audio` 必须真发出去。"""
    adapter, client = _ark()
    rid = _submit(
        adapter, model="doubao-seedance-2-0-pro", first_frame=b"\x89PNG\r\n\x1a\n" + bytes(8),
        duration=5, ref_audio=AudioRef(data=WAV, content_type="audio/wav"),
    )
    assert rid == "task-123"
    url, kwargs = client.posts[0]
    assert url.endswith("/contents/generations/tasks"), url
    content = kwargs["json"]["content"]
    audio = [c for c in content if c["type"] == "audio_url"]
    assert len(audio) == 1, content
    assert audio[0]["role"] == "reference_audio", audio[0]
    assert audio[0]["audio_url"]["url"].startswith("data:audio/wav;base64,"), audio[0]
    # 顺序：文本在最前，音频在最后（与官方示例一致，别让人猜）
    assert content[0]["type"] == "text" and content[-1]["type"] == "audio_url", content


def test_no_reference_audio_means_not_a_single_extra_byte():
    """没勾对白时，请求体里不许出现任何 audio 字段——多发一个字节都是风险。"""
    adapter, client = _ark()
    _submit(adapter, model="doubao-seedance-1-0-pro", first_frame=b"\x89PNG\r\n\x1a\n" + bytes(8))
    body = client.posts[0][1]["json"]
    assert "audio_url" not in json.dumps(body), body


def test_wav_and_mp3_are_the_only_accepted_containers():
    """上游只吃 wav / mp3；其它格式本地就拦住（省一次必然失败的上游往返）。"""
    assert ark_mod._audio_mime(AudioRef(data=WAV, content_type="audio/wav")) == "audio/wav"
    assert ark_mod._audio_mime(AudioRef(data=MP3, content_type="audio/mpeg")) == "audio/mpeg"
    # 库里记的类型不准时，用魔数兜一下（TTS 存成 audio/mp3 这类写法也认）
    assert ark_mod._audio_mime(AudioRef(data=WAV, content_type="application/octet-stream")) == "audio/wav"
    assert ark_mod._audio_mime(AudioRef(data=MP3, content_type="")) == "audio/mpeg"
    assert ark_mod._audio_mime(AudioRef(data=b"\x00\x01\x02\x03", content_type="audio/flac")) == ""


def test_a_bad_container_is_refused_with_a_way_out():
    adapter, client = _ark()
    try:
        _submit(adapter, model="doubao-seedance-2-0-pro", ref_audio=AudioRef(data=b"\x00\x01\x02", content_type="audio/flac"))
    except AdapterError as e:
        assert "只收 wav 与 mp3" in str(e), str(e)
        assert "配音" in str(e), "没告诉用户去哪儿弄一条合规的"
        assert not client.posts, "本地就能判的错，不该还往上打一次"
        return
    raise AssertionError("flac 竟然发出去了")


def test_an_oversize_audio_is_refused_before_it_leaves_the_machine():
    adapter, client = _ark()
    big = WAV + bytes(16 * 1024 * 1024)
    try:
        _submit(adapter, model="doubao-seedance-2-0-pro", ref_audio=AudioRef(data=big, content_type="audio/wav"))
    except AdapterError as e:
        assert "15 MB" in str(e), str(e)
        assert not client.posts, "超限也往上打了一次，白等一轮"
        return
    raise AssertionError("超 15 MB 竟然发出去了")


def test_a_400_while_carrying_audio_names_the_models_that_work():
    """上游拒绝了要能看懂：**把「哪几档模型认参考音频」补上去**。

    不然用户只看到一句「上游拒绝了这次请求」，而这一句其实是有解的。
    """
    adapter, _client = _ark(_FakeResp(400, {"error": {"message": "unsupported parameter: audio_url"}}))
    try:
        _submit(adapter, model="doubao-seedance-1-0-pro", ref_audio=AudioRef(data=WAV, content_type="audio/wav"))
    except AdapterError as e:
        assert "Seedance 1.5 pro / 2.0 / 2.5" in str(e), str(e)
        assert "seedance" in str(e).lower(), str(e)
        return
    raise AssertionError("400 竟然成功了")


def test_an_image_400_is_not_touched_by_the_audio_feature():
    """参考音频那段提示只属于视频那条路。

    这条是**真实踩过的**：那句 `ref_audio is not None` 一开始被写进了生图分支，
    而 `ref_audio` 在那个作用域里根本不存在——图片的 400 会变成 NameError（500），
    而 NameError 只在真的失败时才出现，正常跑测试根本看不见。
    """
    adapter, _client = _ark(_FakeResp(400, {"error": {"message": "bad size"}}))
    try:
        asyncio.run(adapter.generate_image(model="m", prompt="p", size="1024x1024"))
    except NameError as e:  # pragma: no cover - 回归用
        raise AssertionError(f"生图那条路被音频提示污染了：{e}") from e
    except AdapterError as e:
        assert "400" in str(e) and "bad size" in str(e), str(e)
        assert "参考音频" not in str(e), "音频的提示跑到生图的报错里去了"
        return
    raise AssertionError("400 竟然成功了")


def test_the_capability_list_is_a_hint_not_a_gate():
    """名单只用来写提示，**不用来拦人**——新模型一直在出，拿名单拦必然拦错。"""
    assert ark_mod.audio_ref_support("doubao-seedance-1-0-pro-250528") is False
    assert ark_mod.audio_ref_support("doubao-seedance-2-0-pro-261111") is True
    assert ark_mod.audio_ref_support("doubao-seedance-1-5-pro-251015") is True
    # 认不出来就是「不知道」，不是「不支持」
    assert ark_mod.audio_ref_support("some-model-from-tomorrow") is None
    assert ark_mod.audio_ref_support("") is None


def test_adapters_that_cannot_do_it_say_so_instead_of_dropping_the_audio():
    """实现不了就说实现不了。**静默丢掉音频**＝用户花了视频钱拿到一段没对白的片子。"""
    async def go(adapter):  # noqa: ANN001
        try:
            await adapter.submit_video(
                model="m", prompt="p", ref_audio=AudioRef(data=WAV, content_type="audio/wav")
            )
        except AdapterError as e:
            return str(e)
        raise AssertionError(f"{type(adapter).__name__} 竟然把带音频的任务提交出去了")

    dash = asyncio.run(go(DashScopeAdapter("https://dashscope.aliyuncs.com", "k")))
    assert "不支持" in dash and "公网" in dash, dash
    assert "火山方舟" in dash, "没告诉用户该换哪种类型接入"

    for adapter in (OpenAICompatAdapter("https://api.example.com/v1", "k"),):
        msg = asyncio.run(go(adapter))
        assert "不支持" in msg, msg


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


def _audio_asset(db, *, duration: int | None = 4, filename: str = "2026-09/voice.wav", kind: str = "audio"):
    a = Asset(
        kind=kind, filename=filename, original_name="voice.wav", content_type="audio/wav",
        size=len(WAV), source="tts", name="配音 · 黄昏", prompt="黄昏的站台", duration=duration,
    )
    db.add(a)
    return a


@contextlib.contextmanager
def _stub_storage(files: dict[str, bytes], missing: bool = False):
    """把 storage.abs_path 指到一片虚拟文件；missing=True 时指向不存在的路径。"""
    real = runner.storage.abs_path

    class _P:
        def __init__(self, payload: bytes | None) -> None:
            self._payload = payload

        def exists(self) -> bool:
            return self._payload is not None

        def read_bytes(self) -> bytes:
            assert self._payload is not None
            return self._payload

    runner.storage.abs_path = lambda name: _P(None if missing else files.get(Path(name).name))  # type: ignore[assignment]
    try:
        yield
    finally:
        runner.storage.abs_path = real  # type: ignore[assignment]


def test_runner_reads_the_audio_bytes_and_keeps_the_recorded_type():
    async def scenario(db):  # noqa: ANN001
        asset = _audio_asset(db)
        await db.commit()
        with _stub_storage({"voice.wav": WAV}):
            ref = await runner._load_audio_ref(db, {"audio_ref_asset_id": asset.id})
        assert ref is not None and ref.data == WAV
        assert ref.content_type == "audio/wav", "没把库里记的类型带上（适配器就得去猜）"
        assert await runner._load_audio_ref(db, {}) is None

    _db_run(scenario)


def test_runner_refuses_loudly_when_the_audio_is_unusable():
    """三种「音频用不了」都要报错：删了 / 选错资产 / 文件丢了。

    注意这条路径**不会发生任何上游调用**（在提交之前就失败了），所以用户不会被扣钱，
    但任务会如实标失败——比悄悄出一段无声片子强。
    """
    async def scenario(db):  # noqa: ANN001
        asset = _audio_asset(db)
        img = _audio_asset(db, filename="2026-09/photo.png", kind="image")
        await db.commit()

        with _stub_storage({}):
            with _stub_storage({"voice.wav": WAV}, missing=True):
                try:
                    await runner._load_audio_ref(db, {"audio_ref_asset_id": asset.id})
                except AdapterError as e:
                    assert "文件不在了" in str(e), str(e)
                else:
                    raise AssertionError("文件丢了却当成没挂音频")

        try:
            await runner._load_audio_ref(db, {"audio_ref_asset_id": 999999})
        except AdapterError as e:
            assert "重新选一条" in str(e), str(e)
        else:
            raise AssertionError("资产不存在却当成没挂音频")

        try:
            await runner._load_audio_ref(db, {"audio_ref_asset_id": img.id})
        except AdapterError as e:
            assert "不是音频" in str(e), str(e)
        else:
            raise AssertionError("选了张图却当成音频用")

    _db_run(scenario)


# ---- 画布视频节点 ----


@contextlib.contextmanager
def _stub_video_model():
    class _Service:
        id = 9

    class _Resolved:
        service = _Service()
        model_name = "doubao-seedance-2-0-pro"
        modality = "video"
        label = "假视频"

    async def fake_resolve(db, model_key, modality):  # noqa: ANN001, ANN202
        assert modality == "video", modality
        return _Resolved()

    real = provider_store.resolve_model
    provider_store.resolve_model = fake_resolve  # type: ignore[assignment]
    try:
        yield
    finally:
        provider_store.resolve_model = real  # type: ignore[assignment]


def _video_node(**data) -> dict:
    return {"id": "v1", "type": "video", "data": {"model_key": "9:doubao-seedance-2-0-pro", **data}}


def _make_video_node(db, node: dict, images: list[Asset]):
    return canvas_runner._create_node_task(
        db, 1, node, "一个人在说话", images, [], dry_run=True
    )


def test_video_node_attaches_the_audio_and_leaves_a_note():
    async def scenario(db):  # noqa: ANN001
        audio = _audio_asset(db, duration=4)
        img = _audio_asset(db, filename="2026-09/photo.png", kind="image")
        await db.commit()
        with _stub_video_model():
            task = await _make_video_node(
                db, _video_node(mode="first_last", duration=5, audioRefAssetId=audio.id), [img]
            )
        params = json.loads(task.params_json)
        assert params["audio_ref_asset_id"] == audio.id, params
        assert params["duration"] == 5, "时长被悄悄改了——改时长就是改钱"
        assert "配音 · 黄昏" in params["dialogue_note"] and "4.0 秒" in params["dialogue_note"], params

    _db_run(scenario)


def test_video_node_refuses_when_the_line_is_longer_than_the_shot():
    """核心口径的接线版：不够读完整句 → 报错，且**一个任务都不落**。"""
    async def scenario(db):  # noqa: ANN001
        audio = _audio_asset(db, duration=9)
        img = _audio_asset(db, filename="2026-09/photo.png", kind="image")
        await db.commit()
        with _stub_video_model():
            try:
                await _make_video_node(
                    db, _video_node(mode="first_last", duration=5, audioRefAssetId=audio.id), [img]
                )
            except ValueError as e:
                assert "调到 9 秒以上" in str(e), str(e)
            else:
                raise AssertionError("配音比时长长却放过去了（台词会被截，钱照花）")

    _db_run(scenario)


def test_video_node_refuses_a_pick_that_is_not_an_audio():
    async def scenario(db):  # noqa: ANN001
        img = _audio_asset(db, filename="2026-09/photo.png", kind="image")
        await db.commit()
        with _stub_video_model():
            try:
                await _make_video_node(
                    db, _video_node(mode="first_last", duration=5, audioRefAssetId=img.id), [img]
                )
            except ValueError as e:
                assert "音频" in str(e) and "配音" in str(e), str(e)
            else:
                raise AssertionError("选了张图当对白音轨却放过去了")

    _db_run(scenario)


def test_a_stale_audio_pick_says_so_instead_of_silently_going_silent():
    async def scenario(db):  # noqa: ANN001
        img = _audio_asset(db, filename="2026-09/photo.png", kind="image")
        await db.commit()
        with _stub_video_model():
            try:
                await _make_video_node(db, _video_node(duration=5, audioRefAssetId=424242), [img])
            except ValueError as e:
                assert "已经被删掉" in str(e), str(e)
            else:
                raise AssertionError("对白音轨指向一条不存在的资产却照常出片")

    _db_run(scenario)


# ================================================================ 4. 视频接口


@contextlib.contextmanager
def _stub_task_start():
    """别让接口测试真的去起后台任务（那会连到真实数据库）。

    注意打的是**实例**上的方法：`generation.py` 里 `from ... import runner` 拿到的是
    `TaskRunner()` 那个对象，不是模块。
    """
    real = task_runner.start_or_fail

    async def fake(kind, task_id):  # noqa: ANN001, ANN202
        return True

    task_runner.start_or_fail = fake  # type: ignore[assignment]
    try:
        yield
    finally:
        task_runner.start_or_fail = real  # type: ignore[assignment]


def _video_payload(**over):
    from app.schemas import VideoGenerateIn

    payload = {"model_key": "9:doubao-seedance-2-0-pro", "prompt": "一个人在说话", "duration": 5}
    payload.update(over)
    return VideoGenerateIn(**payload)


async def _call_video_api(db, payload):  # noqa: ANN001
    from app.routers.generation import generate_videos

    return await generate_videos(payload, db)


def test_video_api_attaches_the_audio_and_keeps_the_duration():
    async def scenario(db):  # noqa: ANN001
        audio = _audio_asset(db, duration=4)
        await db.commit()
        with _stub_video_model(), _stub_task_start():
            out = await _call_video_api(db, _video_payload(audio_ref_asset_id=audio.id))
        row = await db.get(Task, out.id)
        params = json.loads(row.params_json)
        assert params["audio_ref_asset_id"] == audio.id, params
        assert params["duration"] == 5, params
        assert "对白音轨" in params["dialogue_note"], params

    _db_run(scenario)


def test_video_api_refuses_a_line_that_does_not_fit():
    async def scenario(db):  # noqa: ANN001
        audio = _audio_asset(db, duration=8)
        await db.commit()
        with _stub_video_model(), _stub_task_start():
            try:
                await _call_video_api(db, _video_payload(audio_ref_asset_id=audio.id))
            except Exception as e:  # HTTPException
                detail = str(getattr(e, "detail", e))
                assert "调到 8 秒以上" in detail, detail
                assert "拆到下一镜" in detail, detail
                return
            raise AssertionError("配音比时长长却建了任务")

    _db_run(scenario)


def test_video_api_refuses_a_pick_that_is_not_an_audio():
    async def scenario(db):  # noqa: ANN001
        img = _audio_asset(db, filename="2026-09/photo.png", kind="image")
        await db.commit()
        with _stub_video_model(), _stub_task_start():
            try:
                await _call_video_api(db, _video_payload(audio_ref_asset_id=img.id))
            except Exception as e:  # HTTPException
                assert "不是音频" in str(getattr(e, "detail", e)), str(e)
                return
            raise AssertionError("选了张图当对白音轨却建了任务")

    _db_run(scenario)


def test_the_hint_has_one_home_and_the_api_serves_it():
    """提示文案的唯一副本在后端，两个前端入口都从接口拿（抄两份必然漂移）。"""
    from app.routers import audio as audio_router

    out = asyncio.run(audio_router.list_voices())
    assert out["videoRefHint"] == ark_mod.AUDIO_REF_NO_HINT
    assert "1.0" in out["videoRefHint"] and "2.0" in out["videoRefHint"], out["videoRefHint"]
    # 画布的浮框也走同一句：它由注册表驱动，不许在前端再写一份
    assert "videoRefHint" in _text(SRC / "components" / "AudioRefPicker.tsx")
    assert "speechVoices" in _text(SRC / "components" / "AudioRefPicker.tsx")


# ================================================================ 5. 逐镜对白（数字人）


SHEET = """## 场景1 | 黄昏的站台

### 镜头1 | 大远景 | 缓慢推近 | 6s
- 画面：站台全景，列车驶入
- 台词：小焰：黄昏的站台，最后一班车还没来

### 镜头2 | 中景 | 向右横移 | 3s
- 画面：他抬手看表
- 台词：（无）

### 镜头3 | 特写 | 固定 | 2s
- 画面：指节敲了两下
- 台词：你终于来了
"""

# 这一份里镜头3 的台词（20 字 ≈ 4.4 秒）读不完 2 秒的镜
SHEET_LONG = SHEET.replace("台词：你终于来了", "台词：他抬起头看着站台的尽头，轻轻叹了口气")


def test_the_voice_map_forgives_colons_but_reports_junk():
    """「角色=音色」小表：中文冒号要认（用户十有八九这么打），认不出的行要报出来。

    静默丢掉一条映射的表现是「我明明配了音色却没生效」——而这一条错会跟着一次**付费**的
    配音一起发生，所以宁可报错。
    """
    long_role = "很长很长很长的角色名字超过十二个字"
    mapping, bad = digital_human.parse_voice_map(
        f"小焰=nova\n阿澈：echo\n小焰 : onyx\n# 这是注释\n\n坏行没有分隔符\n{long_role}=nova"
    )
    assert mapping == {"小焰": "onyx", "阿澈": "echo"}, mapping  # 后写的覆盖前写的
    assert bad == ["坏行没有分隔符", f"{long_role}=nova"], bad
    assert digital_human.parse_voice_map(None) == ({}, [])
    assert digital_human.parse_voice_map("a=") == ({}, ["a="]), "音色为空也算认不出"


def test_the_voice_falls_back_to_the_default_and_never_guesses():
    mapping = {"小焰": "nova"}
    assert digital_human.voice_for("小焰", mapping, "alloy") == "nova"
    # 认不出说话人 / 没配这个角色 → 用默认，**不猜一个角色出来**
    assert digital_human.voice_for("", mapping, "alloy") == "alloy"
    assert digital_human.voice_for("阿澈", mapping, "alloy") == "alloy"
    assert digital_human.voice_for("小焰", mapping, "") == "nova"


def test_the_skip_reason_gives_both_numbers_and_both_ways_out():
    msg = digital_human.skip_reason(shot_no="3", audio_seconds=4.4, shot_seconds=2)
    assert "镜头3" in msg and "5 秒" in msg and "2 秒" in msg, msg
    assert "台词改短" in msg and "时长写长" in msg, msg


def test_the_duration_advice_points_at_where_the_user_can_actually_change_it():
    """逐镜出片时时长来自分镜表，建议不能让他去改「视频时长」——那儿没有这个开关。"""
    page = digital_human.check_duration(requested=5, audio_seconds=8.4, name="配音")
    assert "请把视频时长调到" in page, page
    shot = digital_human.check_duration(
        requested=2, audio_seconds=4.4, name="配音", duration_hint="分镜表里这一镜的时长"
    )
    assert "请把分镜表里这一镜的时长调到 5 秒以上" in shot, shot


@contextlib.contextmanager
def _stub_tts(*, per_char: float = 0.22, fail_on: tuple[str, ...] = ()):
    """替身：接管「台词 → 配音资产」这一步（真跑的话这一步要花钱）。

    `fail_on` 里的台词会抛 AdapterError，用来验「某一镜配不上」那条路。
    时长按字数算，与本机假 TTS 的口径一致（0.22 秒/字）。
    """
    calls: list[dict] = []

    async def fake_default(db):  # noqa: ANN001, ANN202
        return "5:tts-1"

    async def fake_synth(db, *, model_key, text, voice="", speed=1.0, source="tts", name="", note=""):  # noqa: ANN001, ANN202
        calls.append({"text": text, "voice": voice, "name": name, "source": source})
        if text in fail_on:
            raise AdapterError("上游 HTTP 429（触发限流或额度不足）")
        seconds = round(len(text) * per_char, 2)
        asset = Asset(
            kind="audio", filename=f"2026-09/dialogue-{len(calls)}.wav", original_name="v.wav",
            content_type="audio/wav", size=100, source=source, name=name, prompt=text,
            duration=max(1, int(round(seconds))),
        )
        db.add(asset)
        await db.commit()
        await db.refresh(asset)
        return asset, {
            "chars": len(text), "seconds": seconds, "modelKey": model_key, "model": "tts-1",
            "voice": voice, "speed": speed,
        }

    real = (speech_service.synthesize_to_asset, speech_service.default_model_key)
    speech_service.synthesize_to_asset = fake_synth  # type: ignore[assignment]
    speech_service.default_model_key = fake_default  # type: ignore[assignment]
    try:
        yield calls
    finally:
        (speech_service.synthesize_to_asset, speech_service.default_model_key) = real  # type: ignore[assignment]


def _doc(nodes: list[dict]) -> dict:
    return {"schemaVersion": 1, "nodes": nodes, "edges": [], "viewport": {}}


async def _seed_shots(db, sheet: str = SHEET, shots: tuple[str, ...] = ("1", "2", "3")):
    """建一个「分镜 → 分镜图 → 视频」的小图，并给每镜造一张已出的分镜图。"""
    doc = _doc([
        {"id": "sb", "type": "storyboard", "data": {"docText": sheet}},
        {"id": "simg", "type": "storyboardImage", "data": {"model_key": "m1"}},
        {"id": "v1", "type": "video", "data": {}},
    ])
    project = Project(name="逐镜对白测试", canvas_json=json.dumps(doc, ensure_ascii=False))
    db.add(project)
    await db.commit()
    await db.refresh(project)
    images: list[Asset] = []
    for no in shots:
        task = Task(
            kind="image", status="completed", service_id=1, model="m1", prompt="",
            params_json=json.dumps({"shot_no": no, "shot_label": f"镜头{no}", "asset_batch": "b1"}),
            canvas_project_id=project.id, canvas_node_id="simg",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        asset = Asset(
            kind="image", filename=f"shot{no}.png", original_name=f"shot{no}.png",
            content_type="image/png", size=100, task_id=task.id,
        )
        db.add(asset)
        await db.commit()
        await db.refresh(asset)
        images.append(asset)
    return project.id, images


def _shot_video_node(**data) -> dict:
    return {
        "id": "v1",
        "type": "video",
        "data": {
            "model_key": "9:doubao-seedance-2-0-pro",
            "mode": "first_last",
            "shotVideo": "each",
            "duration": 5,
            **data,
        },
    }


def test_each_shot_gets_its_own_dubbing_with_the_voice_of_its_speaker():
    """这是这一版的核心：每镜的台词各配一条，按说话人挑音色。"""
    async def scenario(db):  # noqa: ANN001
        pid, images = await _seed_shots(db)
        node = _shot_video_node(shotDialogue=True, shotVoices="小焰=nova")
        with _stub_video_model(), _stub_tts() as calls:
            tasks = await canvas_runner._create_shot_video_tasks(db, pid, node, SHEET, images)

        # 只有镜头1、镜头3 有台词 → 两次合成（镜头2 是「（无）」，不该花这个钱）
        assert [c["text"] for c in calls] == ["黄昏的站台，最后一班车还没来", "你终于来了"], calls
        assert calls[0]["voice"] == "nova", "说话人「小焰」配了 nova，没用上"
        assert calls[1]["voice"] == "", "没标说话人的那句该用默认音色（这里默认留空）"
        assert calls[0]["name"] == "对白 · 镜头1" and calls[0]["source"] == "shot"

        params = {json.loads(t.params_json)["shot_no"]: json.loads(t.params_json) for t in tasks}
        assert len(tasks) == 3, "没台词的镜头也该照常出片"
        assert params["1"]["audio_ref_asset_id"] and "对白音轨" in params["1"]["dialogue_note"]
        assert params["3"]["audio_ref_asset_id"]
        assert "audio_ref_asset_id" not in params["2"], "「（无）」的镜头不该挂音轨"
        # 时长**一个都没被改**（改时长就是改钱）
        assert [params[k]["duration"] for k in ("1", "2", "3")] == [6, 3, 2], params

    _db_run(scenario)


def test_a_line_that_does_not_fit_kills_only_that_shot():
    """读不完的那一镜**不派任务**（其余照常），而且日志里说明是哪儿的问题。"""
    async def scenario(db):  # noqa: ANN001
        pid, images = await _seed_shots(db, SHEET_LONG)
        node = _shot_video_node(shotDialogue=True)
        with _stub_video_model(), _stub_tts() as calls:
            tasks = await canvas_runner._create_shot_video_tasks(db, pid, node, SHEET_LONG, images)

        assert [c["text"] for c in calls] == [
            "黄昏的站台，最后一班车还没来",
            "他抬起头看着站台的尽头，轻轻叹了口气",
        ]
        shots = [json.loads(t.params_json)["shot_no"] for t in tasks]
        assert shots == ["1", "2"], f"镜头3 的台词读不完，它不该出片：{shots}"

    _db_run(scenario)


def test_a_shot_whose_dubbing_failed_is_skipped_not_silently_voiced():
    """配音没做成的那一镜也不出片——静默出一段没声音的片子等于白花视频钱。"""
    async def scenario(db):  # noqa: ANN001
        pid, images = await _seed_shots(db)
        node = _shot_video_node(shotDialogue=True, shotVoices="小焰=nova")
        with _stub_video_model(), _stub_tts(fail_on=("黄昏的站台，最后一班车还没来",)):
            tasks = await canvas_runner._create_shot_video_tasks(db, pid, node, SHEET, images)
        shots = [json.loads(t.params_json)["shot_no"] for t in tasks]
        assert shots == ["2", "3"], f"镜头1 的配音挂了，它不该出片：{shots}"

    _db_run(scenario)


def test_a_run_that_cannot_voice_a_single_shot_is_refused_outright():
    """一镜都配不上时**一条视频任务都不派**：那是拿视频钱换一串没声音的片子。"""
    async def scenario(db):  # noqa: ANN001
        pid, images = await _seed_shots(db, SHEET_LONG)
        node = _shot_video_node(shotDialogue=True)
        fail = ("黄昏的站台，最后一班车还没来", "他抬起头看着站台的尽头，轻轻叹了口气")
        with _stub_video_model(), _stub_tts(fail_on=fail):
            try:
                await canvas_runner._create_shot_video_tasks(db, pid, node, SHEET_LONG, images)
            except ValueError as e:
                assert "一镜都没配上" in str(e) and "没有派任何视频任务" in str(e), str(e)
                rows = (await db.execute(select(Task).where(Task.kind == "video"))).scalars().all()
                assert not rows, "说了不派，却落了任务"
                return
        raise AssertionError("全都配不上却照常派了视频")

    _db_run(scenario)


def test_no_line_at_all_is_refused_before_spending_anything():
    """一句台词都没有 → 在花任何钱之前就报错（这是最常见的配置错误）。"""
    sheet = SHEET.replace("台词：小焰：黄昏的站台，最后一班车还没来", "台词：（无）").replace(
        "台词：你终于来了", "台词：（无）"
    )
    async def scenario(db):  # noqa: ANN001
        pid, images = await _seed_shots(db, sheet)
        node = _shot_video_node(shotDialogue=True)
        with _stub_video_model(), _stub_tts() as calls:
            try:
                await canvas_runner._create_shot_video_tasks(db, pid, node, sheet, images)
            except ValueError as e:
                assert "一句台词都没有" in str(e), str(e)
                assert not calls, "都报错了还去合成配音（白花钱）"
                return
        raise AssertionError("一句台词都没有却照常出片")

    _db_run(scenario)


def test_a_broken_voice_map_is_refused_before_spending_anything():
    async def scenario(db):  # noqa: ANN001
        pid, images = await _seed_shots(db)
        node = _shot_video_node(shotDialogue=True, shotVoices="小焰 nova")
        with _stub_video_model(), _stub_tts() as calls:
            try:
                await canvas_runner._create_shot_video_tasks(db, pid, node, SHEET, images)
            except ValueError as e:
                assert "认不出的行" in str(e) and "小焰 nova" in str(e), str(e)
                assert not calls, "音色表坏了还往下走"
                return
        raise AssertionError("音色表里有认不出的行却照常开跑")

    _db_run(scenario)


def test_the_preview_never_synthesizes_but_counts_the_extra_calls():
    """预览绝不花钱：只标出「这一镜会多做一次配音」，并把这个次数摆出来。"""
    async def scenario(db):  # noqa: ANN001
        pid, images = await _seed_shots(db)
        node = _shot_video_node(shotDialogue=True)
        with _stub_video_model(), _stub_tts() as calls:
            tasks = await canvas_runner._create_shot_video_tasks(
                db, pid, node, SHEET, images, dry_run=True
            )
        assert not calls, "预览竟然真的去合成配音了（那是付费调用）"
        parsed = [json.loads(t.params_json) for t in tasks]
        marked = [p for p in parsed if p.get("shot_dialogue")]
        assert len(marked) == 2, f"预览该标出 2 镜会配对白：{parsed}"
        assert all("audio_ref_asset_id" not in p for p in marked), "预览里不该出现音轨 id"
        rows = (await db.execute(select(Task).where(Task.kind == "video"))).scalars().all()
        assert not rows, "预览落库了任务"

    _db_run(scenario)


def test_the_preview_note_tells_the_user_about_the_extra_billing():
    """确认弹窗里必须说「还会额外做 N 次语音合成」——不说的话这一跑的账就少算了。"""
    src = _text(BACKEND / "app" / "services" / "canvas_runner.py")
    assert '"dialogueCalls": dialogue_calls' in src, "预览没把额外调用次数给出去"
    assert "还会额外做" in src and "语音合成" in src, "没有那句提示"
    assert 'tp.get("shot_dialogue")' in src, "预览没有从任务上数这一镜会不会配对白"


def test_the_panel_says_what_happens_to_the_shots_that_do_not_fit():
    """前端要把「读不完的那一镜不会出片」说出来——用户看到成片少一段时的第一反应是找原因。"""
    canvas = _text(SRC / "pages" / "CanvasPage.tsx")
    block = canvas[canvas.index("逐镜对白（数字人）") : canvas.index("同场景串联")]
    assert "不会出片" in block, "没说读不完的镜头会怎样"
    assert "shotDialogue: e.target.checked" in block, "开关没写回节点"
    assert "shotVoice" in block and "shotVoices" in block, "音色设置没接上"


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
