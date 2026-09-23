"""补帧（#43）的行为测试：模型清单、能补到多少帧、命令行、以及那条三段式。

运行：venv/Scripts/python tests/test_interpolate.py

不需要装引擎、不需要显卡：纯口径那一半造一份临时目录就能测；
执行那一半把「引擎」换成一段真子进程脚本（它按 `-n` 吐帧，与 RIFE 的行为同形），
于是拆帧、帧数对帐、按新帧率合帧、音轨拷回、取消杀进程**全是真的**。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.services import ffmpeg_service  # noqa: E402
from app.services import interpolate as ip  # noqa: E402
from app.services import interpolate_run as irun  # noqa: E402
from app.services import local_engines as le  # noqa: E402

BINS: list[str] = []


# ================================================================ 工具


def _bins() -> tuple[str, str]:
    """用**项目自己的**解析拿 ffmpeg/ffprobe（PATH 里靠前那个可能是 IDE 的精简版）。"""
    if BINS:
        return BINS[0], BINS[1]
    ff, fp = asyncio.run(ffmpeg_service._resolve_binaries())
    if not ff or not fp:
        raise RuntimeError("这台机器上没找到可用的 ffmpeg/ffprobe，补帧那条链测不了")
    BINS.extend([ff, fp])
    return ff, fp


def _make_video(path: Path, w: int, h: int, frames: int, fps: int = 8,
                *, audio: bool = True) -> None:
    cmd = [_bins()[0], "-y", "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={frames / fps:g}"]
    cmd += ["-frames:v", str(frames), "-pix_fmt", "yuv420p", "-c:v", "libx264"]
    if audio:
        cmd += ["-c:a", "aac"]
    subprocess.run(cmd + [str(path)], capture_output=True, check=True)


def _probe(path: Path) -> dict:
    import json
    r = subprocess.run([_bins()[1], "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    data = json.loads(r.stdout or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    num, _, den = str(v.get("avg_frame_rate") or "").partition("/")
    return {
        "w": v.get("width"), "h": v.get("height"),
        "fps": round(float(num) / float(den), 3) if den and float(den) else 0,
        # 帧数是唯一能证明「帧真的都在」的维度，必须一起报出来
        "frames": int(v.get("nb_frames") or 0),
        "audio": a is not None,
        "duration": round(float(data.get("format", {}).get("duration") or 0), 2),
    }


def _fake_rife_root(root: Path, models: tuple[str, ...] = ("rife-v4.6", "rife-v2.3")) -> Path:
    """造一份「装好了」的 rife 目录：每个模型目录下要有 flownet.param。"""
    d = root / "rife"
    d.mkdir(parents=True, exist_ok=True)
    (d / "rife-ncnn-vulkan.exe").write_bytes(b"MZ")
    for name in models:
        sub = d / name
        sub.mkdir(exist_ok=True)
        (sub / "flownet.param").write_bytes(b"x")
        (sub / "flownet.bin").write_bytes(b"x")
        if name not in ip.CUSTOM_FRAME_MODELS:
            # 老架构还有 contextnet / fusionnet
            (sub / "contextnet.param").write_bytes(b"x")
            (sub / "fusionnet.param").write_bytes(b"x")
    return d


class _Sandbox:
    """把「装到哪儿」指到临时目录；测完恢复。"""

    def __enter__(self):
        self.old = ip.le_install_dir
        import app.services.upscale as up_mod
        self.old_up = up_mod.le_install_dir
        return self

    def __exit__(self, *exc):
        import app.services.upscale as up_mod
        ip.le_install_dir = self.old  # type: ignore[assignment]
        up_mod.le_install_dir = self.old_up  # type: ignore[assignment]
        return False


def _point_at(root: Path) -> None:
    """让两边的 `le_install_dir` 都指向临时目录（`exe_path` 走的是 upscale 那一个）。"""
    import app.services.upscale as up_mod
    ip.le_install_dir = lambda key: root / key  # type: ignore[assignment]
    up_mod.le_install_dir = lambda key: root / key  # type: ignore[assignment]


class _TempData:
    def __enter__(self):
        self.old = settings.DATA_DIR
        self.dir = tempfile.mkdtemp(prefix="interp-test-")
        settings.DATA_DIR = self.dir
        return Path(self.dir)

    def __exit__(self, *exc):
        settings.DATA_DIR = self.old
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


#: 假引擎：按 `-n` 吐帧（不给就 2 倍），与 RIFE 的行为同形。
FAKE_ENGINE = r'''
import shutil, sys
from pathlib import Path
args = sys.argv[1:]
def val(f):
    return args[args.index(f) + 1] if f in args else ""
src, dst = Path(val("-i")), Path(val("-o"))
files = sorted(src.iterdir())
n = int(val("-n") or 0) or len(files) * 2
for i in range(n):
    shutil.copyfile(files[min(i * len(files) // max(1, n), len(files) - 1)],
                    dst / f"{i + 1:08d}.png")
'''


def _fake_plan(script: str = FAKE_ENGINE):
    """把 `ip.plan_dir_args` 换成「用真 python 跑假引擎」。

    参数照旧由**真口径**算出来（`-n` 走 `ip.expected_out_frames`），
    这样「帧数怎么算」这件事还是被测的那一份代码在算。
    """

    def _plan(*, in_dir, out_dir, model, src_frames, src_fps, target, gpu=-1):
        want = ip.expected_out_frames(model, src_frames, src_fps, target)
        return [sys.executable, "-c", script, "-i", str(in_dir), "-o", str(out_dir),
                "-n", str(want), "-f", "%08d.png"]

    return _plan


def _run(src: Path, *, model="rife-v4.6", src_fps=8.0, target=16, beat=None):
    old = ip.plan_dir_args
    ip.plan_dir_args = _fake_plan()  # type: ignore[assignment]
    try:
        return asyncio.run(irun.run_video(src=src, model=model, src_fps=src_fps,
                                          target=target, gpu=0, on_progress=beat))
    finally:
        ip.plan_dir_args = old  # type: ignore[assignment]


# ================================================================ 1. 清单


def test_the_interpolate_kind_now_has_an_engine():
    """`KIND_LABELS` 里早就写着「补帧」，但清单里一直没有这个引擎——
    这一版才把它补上（不然那是个空壳标签）。"""
    kinds = {e.kind for e in le.ENGINES}
    assert "interpolate" in kinds, "清单里没有补帧这一类"
    rife = le.by_key("rife")
    assert rife is not None and rife.kind == "interpolate"
    assert rife.size == 431540241, "体积要与上游 API 报的一致"
    le.assert_consistent()


def test_the_engine_entry_promises_what_was_measured():
    """清单里写给用户的话，必须与我们真量到的一致。"""
    rife = le.by_key("rife")
    assert "411" in rife.note, "要老实说清这个包为什么有 411MB"
    assert "12 个模型" in rife.note or "12 个" in rife.note
    assert rife.proof == "local-download", "上游没给摘要，只能是我们自己算的"


# ================================================================ 2. 模型


def test_models_come_from_the_folders_on_disk():
    with tempfile.TemporaryDirectory() as tmp, _Sandbox():
        root = Path(tmp)
        _point_at(root)
        _fake_rife_root(root, ("rife-v4.6", "rife-v2.3", "rife-anime"))
        assert [m.key for m in ip.models()] == ["rife-anime", "rife-v2.3", "rife-v4.6"]


def test_a_folder_without_flownet_is_not_a_model(tmp_path: Path | None = None):
    """判断依据是**目录里有没有 flownet.param**，不是目录名像不像。"""
    with tempfile.TemporaryDirectory() as tmp, _Sandbox():
        root = Path(tmp)
        _point_at(root)
        _fake_rife_root(root, ("rife-v4.6",))
        junk = root / "rife" / "not-a-model"
        junk.mkdir()
        (junk / "readme.txt").write_text("x")
        assert [m.key for m in ip.models()] == ["rife-v4.6"]


def test_only_v4_supports_an_arbitrary_target():
    """实测：其余模型给 `-n` 会被直接挡回来
    （`only rife-v4 model support custom numframe and timestep`）。"""
    with tempfile.TemporaryDirectory() as tmp, _Sandbox():
        root = Path(tmp)
        _point_at(root)
        _fake_rife_root(root, ("rife-v4.6", "rife-v4", "rife-v2.3", "rife-anime"))
        custom = sorted(m.key for m in ip.models() if m.custom)
        assert custom == ["rife-v4", "rife-v4.6"], custom
        for m in ip.models():
            if not m.custom:
                assert m.note_2x_only, f"{m.key} 明明只做 2 倍，却没有把这件事写出来"


def test_the_default_is_the_newest_one_and_it_must_exist():
    with tempfile.TemporaryDirectory() as tmp, _Sandbox():
        root = Path(tmp)
        _point_at(root)
        _fake_rife_root(root, ("rife-v2.3",))
        assert ip.default_model() == "rife-v2.3", "默认值要落在真有的模型上"
        shutil.rmtree(root / "rife")
        assert ip.default_model() == "", "一个都没有时不许编一个名字出来"


def test_target_choices_follow_the_model():
    """**不是所有模型都给 60**：给不出来却列在界面上，就是「选了就报错」。"""
    with tempfile.TemporaryDirectory() as tmp, _Sandbox():
        root = Path(tmp)
        _point_at(root)
        _fake_rife_root(root)
        v46 = ip.find_model("rife-v4.6")
        v23 = ip.find_model("rife-v2.3")
        assert ip.target_counts(24, model=v23) == [48], ip.target_counts(24, model=v23)
        assert 60 in ip.target_counts(24, model=v46)
        assert all(t >= 48 for t in ip.target_counts(24, model=v46)), \
            ip.target_counts(24, model=v46)


# ================================================================ 3. 帧数换算


def test_out_frames_is_computed_from_duration_not_from_a_multiplier():
    """`-n` 要的是**总帧数**，而用户想的是「把这段 24 帧的片子变成 60 帧的」。"""
    assert ip.out_frames(24, 24.0, 60) == 60
    assert ip.out_frames(24, 24.0, 48) == 48
    assert ip.out_frames(150, 30.0, 60) == 300
    assert ip.out_frames(0, 24.0, 60) == 0
    assert ip.out_frames(24, 0, 60) == 0


def test_a_two_times_only_model_is_accounted_for_at_two_times():
    """它实际只会给 2N 帧，**按它实际会给的算**——不然产物对帐会一直失败。"""
    with tempfile.TemporaryDirectory() as tmp, _Sandbox():
        root = Path(tmp)
        _point_at(root)
        _fake_rife_root(root)
        assert ip.expected_out_frames("rife-v2.3", 24, 24.0, 48) == 48
        # 就算用户（或界面）报了别的目标，它也只给 2 倍
        assert ip.expected_out_frames("rife-v2.3", 24, 24.0, 60) == 48
        assert ip.out_fps(60, model="rife-v2.3", src_fps=24.0) == 48.0


def test_the_target_problem_tells_the_user_what_to_do():
    with tempfile.TemporaryDirectory() as tmp, _Sandbox():
        root = Path(tmp)
        _point_at(root)
        _fake_rife_root(root)
        v46 = ip.find_model("rife-v4.6")
        v23 = ip.find_model("rife-v2.3")
        assert ip.target_problem(24, 24.0, 60, model=v46) == ""
        low = ip.target_problem(24, 24.0, 12, model=v46)
        assert "不高于" in low, low
        only2 = ip.target_problem(24, 24.0, 60, model=v23)
        assert "2 倍" in only2 and "v4" in only2, only2
        toobig = ip.target_problem(24, 24.0, 24 * 16, model=v46)
        assert toobig, "16 倍应当被拦住"


def test_readability_and_frame_caps():
    with tempfile.TemporaryDirectory() as tmp, _Sandbox():
        root = Path(tmp)
        _point_at(root)
        _fake_rife_root(root)
        v46 = ip.find_model("rife-v4.6")
        assert ip.target_problem(0, 24.0, 60, model=v46), "读不出帧数要说出来"
        # 帧数上限：一段很长的片子补到 120 帧会超
        assert ip.out_frames(1800, 24.0, 120) > ip.MAX_OUT_FRAMES
        assert ip.target_problem(1800, 24.0, 120, model=v46), "超过上限要拦住"


# ================================================================ 4. 命令行


def test_plan_dir_args_uses_m_for_the_model_and_n_for_the_total():
    """`-n` 是**目标总帧数**，`-m` 才是模型——读错这一个参数，24→60 会变成 24→1440。"""
    with tempfile.TemporaryDirectory() as tmp, _Sandbox():
        root = Path(tmp)
        _point_at(root)
        _fake_rife_root(root)
        args = ip.plan_dir_args(in_dir=root / "i", out_dir=root / "o",
                                model="rife-v4.6", src_frames=24, src_fps=24.0,
                                target=60)
        assert args[args.index("-m") + 1] == "rife-v4.6"
        assert args[args.index("-n") + 1] == "60", "`-n` 要给总帧数"
        assert args[args.index("-f") + 1] == "%08d.png"


def test_plan_dir_args_omits_n_for_the_two_times_only_models():
    """给它们 `-n` 会被直接挡回来（实测），所以对它们**不发这个参数**。"""
    with tempfile.TemporaryDirectory() as tmp, _Sandbox():
        root = Path(tmp)
        _point_at(root)
        _fake_rife_root(root)
        args = ip.plan_dir_args(in_dir=root / "i", out_dir=root / "o",
                                model="rife-v2.3", src_frames=24, src_fps=24.0,
                                target=48)
        assert "-n" not in args, "只做 2 倍的模型不该收到 -n"


def test_gpu_is_only_added_when_the_user_picked_one():
    with tempfile.TemporaryDirectory() as tmp, _Sandbox():
        root = Path(tmp)
        _point_at(root)
        _fake_rife_root(root)
        auto = ip.plan_dir_args(in_dir=root / "i", out_dir=root / "o",
                                model="rife-v4.6", src_frames=8, src_fps=8.0,
                                target=16, gpu=-1)
        assert "-g" not in auto, "自动时不该给 -g"
        picked = ip.plan_dir_args(in_dir=root / "i", out_dir=root / "o",
                                  model="rife-v4.6", src_frames=8, src_fps=8.0,
                                  target=16, gpu=1)
        assert picked[picked.index("-g") + 1] == "1"


def test_planning_without_the_engine_says_so():
    with tempfile.TemporaryDirectory() as tmp, _Sandbox():
        _point_at(Path(tmp))
        try:
            ip.plan_dir_args(in_dir=Path(tmp) / "i", out_dir=Path(tmp) / "o",
                             model="rife-v4.6", src_frames=8, src_fps=8.0, target=16)
            raise AssertionError("没装也把命令行拼出来了")
        except ValueError as e:
            assert "没装好" in str(e)


# ================================================================ 5. 预估与就绪


def test_estimate_grows_with_frames_and_size():
    small = ip.estimate_seconds("rife-v4.6", 640, 360, 30)
    big = ip.estimate_seconds("rife-v4.6", 1920, 1080, 30)
    more = ip.estimate_seconds("rife-v4.6", 640, 360, 300)
    assert big > small and more > small
    lo, hi = ip.estimate_range("rife-v4.6", 1920, 1080, 150)
    assert lo < hi


def test_an_unmeasured_model_is_estimated_heavily():
    assert ip.estimate_seconds("没有这个模型", 1920, 1080, 150) > \
        ip.estimate_seconds("rife-v4.6", 1920, 1080, 150)


def test_check_ready_states():
    with tempfile.TemporaryDirectory() as tmp, _Sandbox():
        root = Path(tmp)
        _point_at(root)
        ok, reason, action, where = ip.check_ready(installed=set(), vulkan=True)
        assert not ok and "还没装" in reason and where == "engines"
        _fake_rife_root(root)
        ok, reason, _a, _w = ip.check_ready(installed={"rife"}, vulkan=False)
        assert not ok and "Vulkan" in reason
        ok, reason, _a, _w = ip.check_ready(installed={"rife"}, vulkan=True)
        assert ok and reason == ""


# ================================================================ 6. 三段式（真子进程）


def test_the_interpolation_pipeline_keeps_duration_and_adds_frames():
    """24 帧 8fps（1 秒）→ 目标 16 帧：帧数翻倍、画幅不变、时长不变、音轨还在。"""
    with _TempData() as data, tempfile.TemporaryDirectory() as tmp:
        _point_at(Path(tmp))
        _fake_rife_root(Path(tmp))
        src = Path(tmp) / "clip.mp4"
        _make_video(src, 64, 32, 8, fps=8)
        out, w, h, frames, _ = _run(src, model="rife-v4.6", src_fps=8.0, target=16)
        info = _probe(out)
        assert (w, h) == (64, 32), f"{w}x{h}"
        assert frames == 16, f"应当补出 16 帧，实际 {frames}"
        assert info["frames"] == 16, (
            f"**产物里是 {info['frames']} 帧，应当是 16**："
            "合帧那一步必须把 PNG 序列的帧率告诉 ffmpeg（image2 默认按 25fps 读）"
        )
        assert abs(info["fps"] - 16) < 0.5, f"帧率应当是 16，实际 {info['fps']}"
        assert abs(info["duration"] - 1.0) < 0.2, f"时长应当还是 1 秒，实际 {info['duration']}"
        assert info["audio"] is True, "原音轨没拷回来"
        assert str(out).startswith(str(data))


def test_a_video_without_audio_still_works():
    with _TempData(), tempfile.TemporaryDirectory() as tmp:
        _point_at(Path(tmp))
        _fake_rife_root(Path(tmp))
        src = Path(tmp) / "silent.mp4"
        _make_video(src, 64, 32, 8, fps=8, audio=False)
        out, _w, _h, frames, _ = _run(src, target=16)
        assert frames == 16
        assert _probe(out)["audio"] is False


def test_a_wrong_frame_count_is_refused():
    """帧数不对拼出来一定变速 —— 必须如实报，不假装成功。"""
    script = r'''
import shutil, sys
from pathlib import Path
args = sys.argv[1:]
def val(f):
    return args[args.index(f) + 1] if f in args else ""
src, dst = Path(val("-i")), Path(val("-o"))
files = sorted(src.iterdir())
for i in range(len(files)):          # 只吐输入的份数，不补
    shutil.copyfile(files[i], dst / f"{i + 1:08d}.png")
'''
    with _TempData(), tempfile.TemporaryDirectory() as tmp:
        _point_at(Path(tmp))
        _fake_rife_root(Path(tmp))
        src = Path(tmp) / "clip.mp4"
        _make_video(src, 64, 32, 8, fps=8)
        old = ip.plan_dir_args
        ip.plan_dir_args = _fake_plan(script)  # type: ignore[assignment]
        try:
            asyncio.run(irun.run_video(src=src, model="rife-v4.6", src_fps=8.0,
                                       target=16, gpu=0))
            raise AssertionError("帧数不对却被当成了成功")
        except RuntimeError as e:
            assert "帧" in str(e) and "变速" in str(e), str(e)
        finally:
            ip.plan_dir_args = old  # type: ignore[assignment]


def test_a_two_times_only_model_makes_a_two_times_longer_clip():
    """只做 2 倍的模型：帧数与帧率都是 2 倍，但**时长不能变**。"""
    with _TempData(), tempfile.TemporaryDirectory() as tmp:
        _point_at(Path(tmp))
        _fake_rife_root(Path(tmp))
        src = Path(tmp) / "clip.mp4"
        _make_video(src, 64, 32, 8, fps=8)
        out, _w, _h, frames, _ = _run(src, model="rife-v2.3", src_fps=8.0, target=16)
        info = _probe(out)
        assert frames == 16, frames
        assert abs(info["fps"] - 16) < 0.5, info["fps"]
        assert abs(info["duration"] - 1.0) < 0.2, info


def test_the_heartbeat_reports_progress_and_can_stop():
    with _TempData(), tempfile.TemporaryDirectory() as tmp:
        _point_at(Path(tmp))
        _fake_rife_root(Path(tmp))
        src = Path(tmp) / "clip.mp4"
        _make_video(src, 64, 32, 8, fps=8)
        seen: list[int] = []

        async def beat(pct: int) -> bool:
            seen.append(pct)
            return True

        _run(src, target=16, beat=beat)
        assert seen and seen == sorted(seen) and seen[-1] == 100, seen

        async def stop_now(pct: int) -> bool:
            return False

        try:
            _run(src, target=32, beat=stop_now)
            raise AssertionError("取消没有停下来")
        except asyncio.CancelledError:
            pass


def test_a_cancelled_run_cleans_up_the_workdir():
    with _TempData() as data, tempfile.TemporaryDirectory() as tmp:
        _point_at(Path(tmp))
        _fake_rife_root(Path(tmp))
        src = Path(tmp) / "clip.mp4"
        _make_video(src, 64, 32, 8, fps=8)

        async def stop_now(pct: int) -> bool:
            return False

        try:
            _run(src, target=16, beat=stop_now)
        except asyncio.CancelledError:
            pass
        work = data / "storage" / "tmp"
        assert not work.exists() or not any(work.iterdir()), "取消后临时目录没清"


# ================================================================ 7. 接线与文案


def test_user_facing_text_has_no_markdown_markers():
    with tempfile.TemporaryDirectory() as tmp, _Sandbox():
        root = Path(tmp)
        _point_at(root)
        _fake_rife_root(root)
        v46 = ip.find_model("rife-v4.6")
        v23 = ip.find_model("rife-v2.3")
        texts = [
            ip.target_problem(24, 24.0, 60, model=v23),
            ip.target_problem(24, 24.0, 12, model=v46),
            ip.target_problem(0, 0, 60, model=v46),
            ip.estimate_text("rife-v4.6", 1920, 1080, 150),
            ip.factor_text(24, 60),
            v23.note_2x_only,
            ip.check_ready(installed=set(), vulkan=True)[1],
        ]
        for t in texts:
            for bad in ("**", "`", "##"):
                assert bad not in t, f"给用户看的文字里有 {bad!r}：{t}"


def test_factor_text_is_something_the_user_can_check():
    assert ip.factor_text(24, 60) == "24 → 60 帧（补 2.50 倍）"
    assert ip.factor_text(0, 60) == ""


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
