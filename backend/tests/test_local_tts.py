"""本机配音（#41）的行为测试：命令行拼装、音色表、子进程那条路、以及「接成一条模型服务」。

运行：venv/Scripts/python tests/test_local_tts.py

**不需要真的装引擎**：目录用临时目录造，命令行用真的 `sys.executable` 当「引擎」跑
（这样退出码、stderr、超时、产物读取这几条都是真跑的），只有「接成服务」那几条
把「装好了没有」换成桩。真的合成一次属于真跑验证，不进单测（要几百 MB 的模型）。

这一版真正要钉住的四件事：
1. **语速是反比**：`--vits-length-scale` 是时长倍率，写错的表现是「调快反而更慢」；
2. **文本必须最后传**（位置参数）：传错位置它会去解析 flag；
3. **音色表来自模型包自己的 README**，而且只认「数字->名字」那一半
   （同一份 README 里还有反方向的表）；
4. **报错不能是空的**，而且「认不出音色」要说清有哪些可用。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import ProviderService  # noqa: E402
from app.providers.base import AdapterError  # noqa: E402
from app.providers.local_tts import LocalTTSAdapter  # noqa: E402
from app.registry import adapters  # noqa: E402
from app.services import engine_install as ei  # noqa: E402
from app.services import local_tts as lt  # noqa: E402

README_SAMPLE = """# Kokoro multi-lang

## Map between speaker ID and speaker name

- **ID to Speaker**
```
0->af_alloy, 1->af_aoise, 45->zf_xiaobei, 46->zf_xiaoni, 47->zf_xiaoxiao
```

- **Speaker to ID**
```
af_alloy->0, af_aoise->1, zf_xiaobei->45, zf_xiaoni->46, zf_xiaoxiao->47
```
"""


# ================================================================ 工具


def _make_install(root: Path, *, skip: tuple[str, ...] = ()) -> None:
    """造一份「装好了」的目录（可以故意少几样，用来验缺口报得对不对）。"""
    bin_dir = root / "engines" / lt.RUNTIME_KEY / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if "exe" not in skip:
        (bin_dir / lt.RUNTIME_EXE).write_bytes(b"MZ")
    md = root / "engines" / lt.MODEL_KEY
    md.mkdir(parents=True, exist_ok=True)
    for name in ("model.int8.onnx", "voices.bin", "tokens.txt", "lexicon-zh.txt",
                 "lexicon-us-en.txt", "date-zh.fst", "phone-zh.fst", "number-zh.fst",
                 "README.md"):
        if name in skip:
            continue
        if name == "README.md":
            (md / name).write_text(README_SAMPLE, encoding="utf-8")
        else:
            (md / name).write_bytes(b"x")
    if "espeak-ng-data" not in skip:
        (md / "espeak-ng-data").mkdir(exist_ok=True)


@contextlib.contextmanager
def _data_dir(root: Path):
    """把「数据目录」指到临时目录（引擎就装在它下面）。"""
    real = lt.settings

    class _S:
        data_dir = root

    lt.settings = _S  # type: ignore[assignment]
    try:
        yield
    finally:
        lt.settings = real  # type: ignore[assignment]


@contextlib.contextmanager
def _fake_engine(command: list[str], *, ready: bool = True):
    """把「引擎」换成一条我们指定的命令，用来真跑子进程那一段。

    `plan_args` 会被换掉，所以这里自己拼命令、并把 `--output-filename` 换成
    「把产物写到那个路径」——适配器读的是那个文件。
    """
    real_args = lt.plan_args
    real_ready = lt.check_ready
    real_layout = lt.resolve_layout

    def fake_plan(layout, *, text, out_path, sid=0, speed=1.0, threads=2):  # noqa: ANN001
        return [c.replace("{OUT}", str(out_path)) for c in command]

    fake_layout = lt.Layout(
        exe=Path(command[0]), model=Path("model.onnx"), voices=Path("voices.bin"),
        tokens=Path("tokens.txt"), data_dir=Path("data"), lexicon=(), rule_fsts=(),
    )
    lt.plan_args = fake_plan  # type: ignore[assignment]
    lt.check_ready = lambda *a, **k: ((True, "") if ready else (  # type: ignore[assignment]
        False, "还没装「Kokoro 中文模型（int8 量化）」——到「本机引擎」页下载它"))
    lt.resolve_layout = lambda *a, **k: fake_layout  # type: ignore[assignment]
    try:
        yield
    finally:
        lt.plan_args = real_args  # type: ignore[assignment]
        lt.check_ready = real_ready  # type: ignore[assignment]
        lt.resolve_layout = real_layout  # type: ignore[assignment]


WAV_BYTES = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 2000


# ================================================================ 1. 命令行拼装


def test_plan_args_points_only_at_files_that_exist():
    """每个 `--kokoro-*` 都要指向**真实存在**的文件：写死文件名的话一换命名就点不动。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_install(root)
        with _data_dir(root):
            layout = lt.resolve_layout()
            assert layout is not None, "造好的目录应当解析得出来"
            args = lt.plan_args(layout, text="你好", out_path=root / "o.wav")
        joined = " ".join(args)
        assert f"--kokoro-model={layout.model}" in joined
        for flag in ("--kokoro-model", "--kokoro-voices", "--kokoro-tokens"):
            path = Path(joined.split(f"{flag}=")[1].split(" ")[0])
            assert path.is_file(), f"{flag} 指向的文件不存在：{path}"
        # 音素数据是个目录
        assert layout.data_dir.is_dir()


def test_resolve_layout_returns_none_when_a_file_is_missing():
    """缺件时**不拼命令行**（宁可不给参数，也不要给一个指向空气的路径）。"""
    for missing in ("exe", "model.int8.onnx", "voices.bin", "tokens.txt"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_install(root, skip=(missing,))
            with _data_dir(root):
                assert lt.resolve_layout() is None, f"少了 {missing} 却还给了 layout"
                ready, why = lt.check_ready()
            assert ready is False and "本机引擎" in why, why


def test_length_scale_is_the_inverse_of_speed():
    """`--kokoro-length-scale` 是**时长倍率**：语速 2 倍 = 时长 0.5 倍。

    两个坑一起钉住：
    - 这个反比写错的表现是「把语速调快，声音反而更慢」；
    - **参数名不能用 `--vits-length-scale`**：那个名字这个二进制**认**（不报错），
      但对 Kokoro 完全不起作用——实测 1 倍速与 2 倍速都是 6.17 秒。
      正确的名字是从 exe 自己的用法表里读到的（见 `plan_args` 的注释）。
    """
    layout = lt.Layout(
        exe=Path("x.exe"), model=Path("m.onnx"), voices=Path("v.bin"),
        tokens=Path("t.txt"), data_dir=Path("d"), lexicon=(), rule_fsts=(),
    )
    fast = lt.plan_args(layout, text="a", out_path=Path("o.wav"), speed=2.0)
    slow = lt.plan_args(layout, text="a", out_path=Path("o.wav"), speed=0.5)
    normal = lt.plan_args(layout, text="a", out_path=Path("o.wav"), speed=1.0)
    assert any(a.endswith("=0.500") for a in fast), fast
    assert any(a.endswith("=2.000") for a in slow), slow
    assert not any("length-scale" in a for a in normal), "语速没变就别传这个参数"
    assert not any(a.startswith("--vits-length-scale") for a in fast + slow), (
        "用了 --vits-length-scale：它对这个模型静默不生效（实测）"
    )


def test_the_text_is_the_last_argument():
    """文本是**位置参数**，必须最后传：放前面它会去当成 flag 解析。"""
    layout = lt.Layout(
        exe=Path("x.exe"), model=Path("m.onnx"), voices=Path("v.bin"),
        tokens=Path("t.txt"), data_dir=Path("d"), lexicon=(), rule_fsts=(),
    )
    args = lt.plan_args(layout, text="你好，世界。", out_path=Path("o.wav"))
    assert args[-1] == "你好，世界。", args
    assert all(a.startswith("--") for a in args[1:-1]), args


def test_rule_fsts_and_lexicon_are_joined_the_documented_way():
    """中文要念对数字/电话/日期，靠的是那几个 fst；多个文件用逗号连在一起。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_install(root)
        with _data_dir(root):
            layout = lt.resolve_layout()
            args = lt.plan_args(layout, text="a", out_path=root / "o.wav")
    joined = " ".join(args)
    rules = joined.split("--tts-rule-fsts=")[1].split(" ")[0].split(",")
    assert [Path(r).name for r in rules] == ["date-zh.fst", "phone-zh.fst", "number-zh.fst"], rules
    lex = joined.split("--kokoro-lexicon=")[1].split(" ")[0]
    assert "," in lex and lex.count(".txt") == 2, lex


def test_threads_stay_in_a_sane_range():
    """线程给多了反而抢核：82M 的小模型用 4 个线程就到头了。"""
    import os

    real = os.cpu_count
    try:
        for cores, want in ((1, 1), (4, 2), (8, 4), (24, 4)):
            os.cpu_count = lambda c=cores: c  # type: ignore[assignment]
            assert lt.default_threads() == want, (cores, lt.default_threads())
    finally:
        os.cpu_count = real  # type: ignore[assignment]


# ================================================================ 2. 音色表


def test_voices_come_from_the_model_readme():
    """音色表读模型包自己的 README——不在代码里抄一份（抄的必然过时）。"""
    voices = lt.parse_voices(README_SAMPLE)
    assert [(v.sid, v.name) for v in voices] == [
        (0, "af_alloy"), (1, "af_aoise"), (45, "zf_xiaobei"),
        (46, "zf_xiaoni"), (47, "zf_xiaoxiao"),
    ], voices


def test_the_reverse_table_is_not_mistaken_for_the_voice_table():
    """同一份 README 里还有「名字->号码」那张表，一把抓会把音色表搞成两倍长。"""
    reverse_only = "af_alloy->0, zf_xiaobei->45\n"
    assert lt.parse_voices(reverse_only) == []
    mixed = "0->af_alloy\n" + reverse_only
    assert [(v.sid, v.name) for v in lt.parse_voices(mixed)] == [(0, "af_alloy")]


def test_voice_list_falls_back_to_the_default_when_there_is_no_readme():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_install(root, skip=("README.md",))
        with _data_dir(root):
            # 这个包只有 1 字节的 voices.bin（`_make_install` 造的），算不出个数
            assert lt.voice_list() == [lt.Voice(sid=0, name="")]


def test_the_speaker_count_comes_from_the_voices_file_size():
    """包里**没有**音色表时（实测 Kokoro 的 int8 包的 README 只有 114 字节），
    音色个数仍然可以从 `voices.bin` 的体积算出来。

    一个音色占 510×256×4 字节；实测 53,790,720 ÷ 522,240 = **103**，
    与官方文档写的 103 speakers 一致——所以这个数不是编的。
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_install(root, skip=("README.md",))
        packed = root / "engines" / lt.MODEL_KEY / "voices.bin"
        packed.write_bytes(b"\x00" * (lt._VOICE_BLOCK * 3))
        with _data_dir(root):
            voices = lt.voice_list()
            assert [v.sid for v in voices] == [0, 1, 2], voices
            assert all(not v.name for v in voices), voices
            hint = lt.voice_hint(len(voices), named=False)
            assert "0–2" in hint and "填数字" in hint, hint
        # 除不尽就**不猜**：只给一个默认音色，而不是列出一堆不存在的号
        packed.write_bytes(b"\x00" * (lt._VOICE_BLOCK * 3 + 1))
        with _data_dir(root):
            assert lt.voice_list() == [lt.Voice(sid=0, name="")]
            assert lt.speaker_count(packed) == 0


def test_the_voice_hint_says_whether_names_exist():
    named = lt.voice_hint(103, named=True)
    assert "103" in named and "名字" in named, named
    plain = lt.voice_hint(103, named=False)
    assert "103" in plain and "按号" in plain and "github.io" in plain, plain
    assert "一个默认音色" in lt.voice_hint(1, named=False)


def test_sid_for_accepts_a_name_a_number_or_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_install(root)
        with _data_dir(root):
            assert lt.sid_for("") == 0
            assert lt.sid_for("45") == 45
            assert lt.sid_for("zf_xiaoxiao") == 47
            assert lt.sid_for("ZF_XIAOXIAO") == 47, "名字比对不该区分大小写"
            try:
                lt.sid_for("nova")
            except ValueError as e:
                assert "zf_xiaoxiao" in str(e), str(e)
                assert "留空" in str(e), str(e)
            else:
                raise AssertionError("认不出的音色竟然没报错（会悄悄用别人的嗓子）")


# ================================================================ 3. 适配器（真跑子进程）


def test_the_adapter_refuses_other_modalities_loudly():
    """它不是对话/出图/视频服务：那几条路要明确报错，不许静默失败。"""
    ad = LocalTTSAdapter("local://x", "")
    try:
        asyncio.run(ad.generate_image(model="m", prompt="p"))
    except AdapterError as e:
        assert "图片" in str(e), str(e)
    else:
        raise AssertionError("本机配音不该能出图")

    async def drain():
        async for _ in ad.chat_stream("m", []):
            pass

    try:
        asyncio.run(drain())
    except AdapterError as e:
        assert "文本" in str(e), str(e)
    else:
        raise AssertionError("本机配音不该能对话")


def test_synthesize_returns_wav_bytes_from_the_real_subprocess():
    """真起一个子进程、真读它写出来的文件——这条链是这一版的核心。

    `content_type` 必须是 `audio/wav`：落库时扩展名跟着它走，写成 mp3 就是
    「一个 .mp3 文件里装着 wav 字节」，浏览器的进度条与时长都会不对。
    """
    code = f"import sys; open(sys.argv[1],'wb').write({WAV_BYTES!r})"
    with _fake_engine([sys.executable, "-c", code, "{OUT}"]):
        ad = LocalTTSAdapter("local://sherpa-onnx", "")
        result = asyncio.run(ad.synthesize_speech(model="kokoro", text="你好", voice="45"))
    assert result.content_type == "audio/wav", result.content_type
    assert result.audio.startswith(b"RIFF"), result.audio[:8]
    assert len(result.audio) > 1000, len(result.audio)


def test_a_failed_run_says_the_exit_code_and_the_stderr_tail():
    """跑失败要给「退出码 + 它说的那句话」——用户/我们排查全靠这个。"""
    code = "import sys; sys.stderr.write('cannot find model.onnx\\n'); raise SystemExit(3)"
    with _fake_engine([sys.executable, "-c", code, "{OUT}"]):
        ad = LocalTTSAdapter("local://sherpa-onnx", "")
        try:
            asyncio.run(ad.synthesize_speech(model="kokoro", text="你好"))
        except AdapterError as e:
            text = str(e)
            assert "退出码 3" in text, text
            assert "cannot find model.onnx" in text, text
            assert "本机引擎" in text, text
        else:
            raise AssertionError("子进程失败了却没报错")


def test_a_hung_run_is_stopped_with_a_readable_reason():
    """卡住要能停掉并说清超时——否则界面上会一直转圈。"""
    import app.providers.local_tts as provider

    real = provider.SYNTH_TIMEOUT
    provider.SYNTH_TIMEOUT = 0.6  # type: ignore[assignment]
    try:
        code = "import time; time.sleep(10)"
        with _fake_engine([sys.executable, "-c", code, "{OUT}"]):
            ad = LocalTTSAdapter("local://sherpa-onnx", "")
            try:
                asyncio.run(ad.synthesize_speech(model="kokoro", text="你好"))
            except AdapterError as e:
                assert "超时" in str(e) or "超过" in str(e), str(e)
            else:
                raise AssertionError("卡住的合成没被停掉")
    finally:
        provider.SYNTH_TIMEOUT = real  # type: ignore[assignment]


def test_no_output_means_failure_even_with_exit_code_zero():
    """退出码 0 但没产出文件：也要当失败（静默成功是最难查的一种）。"""
    code = "print('i did nothing')"
    with _fake_engine([sys.executable, "-c", code, "{OUT}"]):
        ad = LocalTTSAdapter("local://sherpa-onnx", "")
        try:
            asyncio.run(ad.synthesize_speech(model="kokoro", text="你好"))
        except AdapterError as e:
            assert "退出码 0" in str(e), str(e)
        else:
            raise AssertionError("没产出音频却当成成功了")


def test_not_installed_says_where_to_go():
    with _fake_engine([sys.executable, "-c", "pass"], ready=False):
        ad = LocalTTSAdapter("local://sherpa-onnx", "")
        try:
            asyncio.run(ad.synthesize_speech(model="kokoro", text="你好"))
        except AdapterError as e:
            assert "本机引擎" in str(e), str(e)
        else:
            raise AssertionError("引擎没装却跑成功了")


def test_test_connection_really_synthesizes_and_refuses_tiny_output():
    """「测试连接」要**真的合成一句**：只查文件在不在，测不出坏 exe 与版本不匹配。"""
    tiny = b"RIFF" + b"\x00" * 10
    code = f"import sys; open(sys.argv[1],'wb').write({tiny!r})"
    with _fake_engine([sys.executable, "-c", code, "{OUT}"]):
        ad = LocalTTSAdapter("local://sherpa-onnx", "")
        try:
            asyncio.run(ad.test_connection("kokoro"))
        except AdapterError as e:
            assert "字节" in str(e), str(e)
        else:
            raise AssertionError("只有 14 字节的产物也该被判成不对")

    code_ok = f"import sys; open(sys.argv[1],'wb').write({WAV_BYTES!r})"
    with _fake_engine([sys.executable, "-c", code_ok, "{OUT}"]):
        ad = LocalTTSAdapter("local://sherpa-onnx", "")
        asyncio.run(ad.test_connection("kokoro"))


# ================================================================ 4. 接成一条模型服务


def test_the_kind_is_registered_and_buildable():
    kinds = [k["kind"] for k in adapters.list_kinds()]
    assert "local_tts" in kinds, kinds
    built = adapters.build_adapter("local_tts", "local://sherpa-onnx", "")
    assert isinstance(built, LocalTTSAdapter), built


@contextlib.contextmanager
def _pretend_installed(*, ready: bool, voices: int = 2):
    """把「引擎装好了没有」换成桩（真装要下几百 MB，单测里不干这事）。"""
    real_installed = ei.installed
    real_ready = lt.check_ready
    real_voices = lt.voice_list
    ei.installed = lambda engine: ready  # type: ignore[assignment]
    lt.check_ready = lambda *a, **k: ((True, "") if ready else (False, "还没装引擎"))  # type: ignore[assignment]
    lt.voice_list = lambda *a, **k: (  # type: ignore[assignment]
        [lt.Voice(i, f"v{i}") for i in range(voices)] if ready else [])
    try:
        yield
    finally:
        ei.installed = real_installed  # type: ignore[assignment]
        lt.check_ready = real_ready  # type: ignore[assignment]
        lt.voice_list = real_voices  # type: ignore[assignment]


def _db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    return engine, maker


def test_connect_creates_one_audio_model_and_repeating_it_does_not_duplicate():
    engine, maker = _db()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with maker() as db:
            with _pretend_installed(ready=True):
                info = await ei.connect_local_tts(db)
                assert info["connected"] is True, info
                assert info["modelKey"].endswith(f":{ei.LOCAL_TTS_MODEL}"), info
                assert info["voiceCount"] == 2, info
                again = await ei.connect_local_tts(db)
                assert again["serviceId"] == info["serviceId"], "重复接入加出了第二份"
            rows = (await db.execute(select(ProviderService))).scalars().all()
            assert len(rows) == 1, [r.name for r in rows]
            row = rows[0]
            assert row.kind == "local_tts" and row.enabled is True
            # 模型必须是 **audio** 模态：配音页与逐镜对白都是按模态取模型的
            models = json.loads(row.models_json)
            assert models and models[0]["modality"] == "audio", models

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(engine.dispose())


def test_connect_refuses_when_the_engines_are_missing():
    engine, maker = _db()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with maker() as db:
            with _pretend_installed(ready=False):
                try:
                    await ei.connect_local_tts(db)
                except ValueError as e:
                    assert "引擎" in str(e) or "装" in str(e), str(e)
                    return
                raise AssertionError("引擎没装却接上了")

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(engine.dispose())


def test_disconnect_disables_and_keeps_the_row():
    """停用而不是删除：已经生成过的配音资产还挂在它名下，而用户常常只是想换一个。"""
    engine, maker = _db()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with maker() as db:
            with _pretend_installed(ready=True):
                await ei.connect_local_tts(db)
                info = await ei.disconnect_local_tts(db)
                assert info["connected"] is False, info
                assert info["disabled"] is True, info
            from sqlalchemy import select
            rows = (await db.execute(select(ProviderService))).scalars().all()
            assert len(rows) == 1 and rows[0].enabled is False, "停用把行删了"

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(engine.dispose())


def test_status_tells_the_three_states_apart():
    """「没装 / 装了没接 / 接好了」是三件事，因为用户要做的动作不同。"""
    engine, maker = _db()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with maker() as db:
            with _pretend_installed(ready=False):
                info = await ei.local_tts_status(db)
                assert info["ready"] is False and info["connected"] is False
                assert info["missingEngines"], "没说缺哪个引擎"
            with _pretend_installed(ready=True):
                info = await ei.local_tts_status(db)
                assert info["ready"] is True and info["connected"] is False
                assert info["missingEngines"] == [], info
                info = await ei.connect_local_tts(db)
                assert info["ready"] and info["connected"], info
                assert len(info["voices"]) == 2, info

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(engine.dispose())


def test_the_voices_endpoint_switches_on_the_selected_model():
    """音色清单跟着**选中的模型**走：云端那六个通用名字与本机那一百多个不是一回事。"""
    from app.routers.audio import list_voices

    engine, maker = _db()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with maker() as db:
            cloud = ProviderService(name="某云", kind="openai", base_url="https://x/v1",
                                    enabled=True)
            db.add(cloud)
            await db.commit()
            await db.refresh(cloud)

            plain = await list_voices(db=db)
            assert plain["local"] is False
            assert {p["id"] for p in plain["presets"]} >= {"alloy", "nova"}

            by_cloud = await list_voices(model_key=f"{cloud.id}:tts-1", db=db)
            assert by_cloud["local"] is False, "云端服务不该被当成本机"

            with _pretend_installed(ready=True):
                await ei.connect_local_tts(db)
                info = await ei.local_tts_status(db)
                local = await list_voices(model_key=info["modelKey"], db=db)
            assert local["local"] is True, local
            assert [p["id"] for p in local["presets"]] == ["v0", "v1"], local
            assert "2 个音色" in local["voiceHint"], local["voiceHint"]

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(engine.dispose())


def test_the_voice_chips_are_capped_but_the_count_is_not():
    """一百多个音色铺成一排按钮就没法用了：只列前 12 个，但总数要说出来。"""
    from app.routers.audio import list_voices

    engine, maker = _db()

    async def scenario():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with maker() as db:
            with _pretend_installed(ready=True, voices=103):
                await ei.connect_local_tts(db)
                info = await ei.local_tts_status(db)
                local = await list_voices(model_key=info["modelKey"], db=db)
            assert len(local["presets"]) == 16, len(local["presets"])
            assert local["voiceCount"] == 103, local

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(engine.dispose())


# ================================================================ 5. 前后端契约


def test_the_page_types_and_api_agree_on_the_local_tts_shape():
    types = (BACKEND.parent / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")
    page = (BACKEND.parent / "frontend" / "src" / "pages" / "EnginesPage.tsx").read_text(
        encoding="utf-8")
    api = (BACKEND.parent / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
    for field in ("localTts", "voiceCount", "missingEngines", "modelKey"):
        assert field in types, f"类型里没有 {field}"
    for field in ("ready", "connected", "disabled", "problem", "voices"):
        assert field in types and field in page, f"{field} 在类型或页面上缺了"
    assert "connectLocalTts" in api and "disconnectLocalTts" in api, "接口没接上"
    assert 'onNavigate?.("speech")' in page, "接好之后没有去配音页的入口"


def test_the_speech_page_asks_for_voices_of_the_selected_model():
    """换了模型要重新问音色（不然会拿云端的 alloy 去喂本机模型，报一句看不懂的错）。"""
    page = (BACKEND.parent / "frontend" / "src" / "pages" / "SpeechPage.tsx").read_text(
        encoding="utf-8")
    assert "api.speechVoices(modelKey)" in page, "没有按模型问音色"
    assert "res.presets.some((p) => p.id === saved)" in page, "换模型时没把不认识的音色清掉"
    assert "meta?.local" in page, "界面上没区分本机模型"


def test_user_facing_text_of_this_version_has_no_markdown_markers():
    """后端给的文案是纯文本（React 不渲染 markdown）——上一版踩过一次。"""
    texts = [ei.LOCAL_TTS_LABEL, lt.check_ready()[1]]
    for key in ("sherpa_tts_runtime", "kokoro_zh"):
        from app.services import local_engines as le
        engine = le.by_key(key)
        texts += [engine.note, engine.why]
    for text in texts:
        for bad in ("**", "`", "##"):
            assert bad not in text, f"给用户看的文字里有 {bad!r}：{text}"


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
