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

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import Asset, Task  # noqa: E402
from app.providers import ark as ark_mod  # noqa: E402
from app.providers.ark import ArkAdapter  # noqa: E402
from app.providers.base import AdapterError, AudioRef  # noqa: E402
from app.providers.dashscope import DashScopeAdapter  # noqa: E402
from app.providers.openai_compat import OpenAICompatAdapter  # noqa: E402
from app.services import canvas_runner, digital_human, provider_store, runner  # noqa: E402
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
