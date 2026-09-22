"""超分（#42）的行为测试：路线/模型/命令行/设备/上限/预估，以及视频那条三段式。

运行：venv/Scripts/python tests/test_upscale.py

**不需要真的装引擎，也不需要显卡**：
- 纯口径那一半（`services/upscale.py`）造一份临时目录就能测，盘上有哪些模型文件
  就决定界面能给哪些模型——这正好是这一版最重要的一条口径；
- 执行那一半（`services/upscale_run.py`）把「引擎」换成一段真的子进程脚本
  （用 `sys.executable` 跑），于是三段式里的拆帧、目录批量、合帧、音频拷回、
  产物尺寸对帐、取消杀进程**全是真的**，只有「放大」那一步是假的（照抄输入）。

真跑一次超分（要几百 MB 的引擎包）属于真跑验证，不进单测。
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
from app.services import upscale as up  # noqa: E402
from app.services import upscale_run as urun  # noqa: E402

FFMPEG_CACHE: list[str] = []


# ================================================================ 工具


def _fake_realesrgan(root: Path, *, stems: tuple[str, ...] | None = None) -> Path:
    """造一份 realesrgan 的安装目录（模型是 `models/*.param`）。"""
    d = root / "realesrgan"
    (d / "models").mkdir(parents=True, exist_ok=True)
    (d / "realesrgan-ncnn-vulkan.exe").write_bytes(b"MZ")
    for stem in (stems if stems is not None else
                 ("realesrgan-x4plus", "realesrgan-x4plus-anime",
                  "realesr-animevideov3-x2", "realesr-animevideov3-x3",
                  "realesr-animevideov3-x4")):
        (d / "models" / f"{stem}.param").write_bytes(b"x")
        (d / "models" / f"{stem}.bin").write_bytes(b"x")
    return d


def _fake_waifu2x(root: Path, *, with_noise0: bool = True) -> Path:
    """造一份 waifu2x 的安装目录（模型是**目录**，不是文件名）。"""
    d = root / "waifu2x"
    d.mkdir(parents=True, exist_ok=True)
    (d / "waifu2x-ncnn-vulkan.exe").write_bytes(b"MZ")
    for name in ("models-cunet", "models-upconv_7_photo"):
        sub = d / name
        sub.mkdir(exist_ok=True)
        # upconv 那两个目录**没有** noise0_model.param，所以 1 倍跑不起来
        if with_noise0 and name == "models-cunet":
            (sub / "noise0_model.param").write_bytes(b"x")
        (sub / "scale2.0x_model.param").write_bytes(b"x")
    return d


class _FakeEngineRoot:
    """把 `up.le_install_dir` 指到临时目录，测完恢复。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def __enter__(self):
        self.old = up.le_install_dir
        up.le_install_dir = lambda key: self.root / key  # type: ignore[assignment]
        self.old_cache = dict(urun._devices)
        urun._devices.clear()
        return self

    def __exit__(self, *exc):
        up.le_install_dir = self.old  # type: ignore[assignment]
        urun._devices.clear()
        urun._devices.update(self.old_cache)
        return False


class _TempData:
    """把数据目录换到临时目录（`_out_path` 与临时工作目录都落在 storage 里）。"""

    def __enter__(self):
        self.old = settings.DATA_DIR
        self.dir = tempfile.mkdtemp(prefix="upscale-test-")
        settings.DATA_DIR = self.dir
        return Path(self.dir)

    def __exit__(self, *exc):
        settings.DATA_DIR = self.old
        shutil.rmtree(self.dir, ignore_errors=True)
        return False


def _bins() -> tuple[str, str]:
    """用**项目自己的**解析拿 ffmpeg/ffprobe。

    不要用 `shutil.which("ffmpeg")`：这台机器上 PATH 里靠前的那个是 IDE 自带的
    精简版，跑 `lavfi` 会直接失败（实测退出码 4294967274）——而项目自己会挑到
    真正装在系统里的那个（导演台启动时就打印过它用的是哪个）。
    """
    if FFMPEG_CACHE:
        return FFMPEG_CACHE[0], FFMPEG_CACHE[1]
    ff, fp = asyncio.run(ffmpeg_service._resolve_binaries())
    if not ff or not fp:
        raise RuntimeError("这台机器上没找到可用的 ffmpeg/ffprobe，超分那条链测不了")
    FFMPEG_CACHE.extend([ff, fp])
    return ff, fp


def _ffmpeg() -> str:
    return _bins()[0]


def _make_image(path: Path, w: int, h: int) -> None:
    subprocess.run([_ffmpeg(), "-y", "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=1",
                    "-frames:v", "1", str(path)], capture_output=True, check=True)


def _make_video(path: Path, w: int, h: int, frames: int, fps: int = 8,
                *, audio: bool = True) -> None:
    cmd = [_ffmpeg(), "-y", "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={frames / fps:g}"]
    cmd += ["-frames:v", str(frames), "-pix_fmt", "yuv420p", "-c:v", "libx264"]
    if audio:
        cmd += ["-c:a", "aac"]
    cmd += [str(path)]
    subprocess.run(cmd, capture_output=True, check=True)


def _probe(path: Path) -> dict:
    r = subprocess.run([_bins()[1], "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    import json
    data = json.loads(r.stdout or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    return {"w": v.get("width"), "h": v.get("height"), "audio": a is not None,
            "duration": round(float(data.get("format", {}).get("duration") or 0), 2)}


#: 假的「引擎」：目录进、目录出，把每一帧照抄一份（尺寸不变）。
#: 所以我们用 `-s 1` 跑，产物尺寸与输入一致，正好能验「尺寸对帐」那条。
FAKE_DIR_ENGINE = r'''
import shutil, sys
from pathlib import Path
args = sys.argv[1:]
def val(flag):
    return args[args.index(flag) + 1] if flag in args else ""
src, dst = Path(val("-i")), Path(val("-o"))
scale = int(val("-s") or 1)
wrong = "-o" in args and "WRONGSIZE" in " ".join(args)
for f in sorted(src.iterdir()):
    shutil.copyfile(f, dst / f.name)
if wrong:
    # 故意写一张尺寸不对的（用来验「产物对不上就判失败」）
    import zlib, struct
    def png(w, h):
        raw = b"\x00" + b"\x00\x00\x00" * w
        data = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
        def chunk(t, d):
            c = t + d
            return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c))
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(data)) + chunk(b"IEND", b""))
    for f in sorted(dst.iterdir()):
        f.write_bytes(png(8, 8))
print("0.00%")
print("done")
'''

#: 假的「引擎」：单图进、单图出（照抄）。用来验 run_image 的产物校验。
FAKE_IMAGE_ENGINE = r'''
import shutil, sys
from pathlib import Path
args = sys.argv[1:]
def val(flag):
    return args[args.index(flag) + 1] if flag in args else ""
dst = Path(val("-o"))
if "NOSIZE" in " ".join(args):
    dst.write_bytes(b"not an image")
else:
    shutil.copyfile(val("-i"), dst)
'''


def _fake_dir_plan(script: str, marker: str = ""):
    """把 `plan_dir_args` 换成「用真 python 跑一段假引擎」。

    **必须把真实参数原样带上**（`-i/-o/-s`）：只换掉「哪个可执行文件」这一步，
    三段式里其余的部分（拆帧目录、输出目录、倍数）才是真的。
    第一版这里图省事返回了一个固定列表，结果假引擎在 `cwd` 里找帧、
    报了一句 `SameFileError: 'activate'`——把「假引擎」写错了当成产品 bug 查会白费很久。
    """

    def _plan(engine_key, *, in_dir, out_dir, model, scale, gpu=-1):
        return [sys.executable, "-c", script, *([marker] if marker else []),
                "-i", str(in_dir), "-o", str(out_dir), "-s", str(scale), "-f", "png"]

    return _plan


def _fake_image_plan(script: str, marker: str = ""):
    def _plan(engine_key, *, src, out, model, scale, gpu=-1, tta=False):
        return [sys.executable, "-c", script, *([marker] if marker else []),
                "-i", str(src), "-o", str(out)]

    return _plan


# ================================================================ 1. 路线


def test_route_keys_round_trip():
    assert up.local_route("realesrgan") == "local:realesrgan"
    assert up.engine_of("local:realesrgan") == "realesrgan"
    assert up.engine_of("local:waifu2x") == "waifu2x"
    assert up.is_comfy("comfyui") is True


def test_a_route_we_do_not_know_is_refused_not_guessed():
    """认不出的路线必须回空串（后面会 400），**不许**退到某个默认引擎上跑。"""
    for bad in ("", "realesrgan", "local:", "local:nope", "comfy", "local:comfyui", "  "):
        assert up.engine_of(bad) == "", f"{bad!r} 被认成了引擎"
    assert up.is_comfy("local:realesrgan") is False


# ================================================================ 2. 模型清单


def test_realesrgan_models_come_from_the_models_folder():
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        _fake_realesrgan(Path(tmp))
        keys = [m.key for m in up.models("realesrgan")]
        # `-x2/-x3/-x4` 三份文件属于**同一个模型**，不能算三个
        assert keys == ["realesr-animevideov3", "realesrgan-x4plus",
                        "realesrgan-x4plus-anime"], keys


def test_a_model_the_package_does_not_ship_is_never_offered():
    """上游用法表里列了 `realesrnet-x4plus`，而这个包里没有它。

    照着用法表给用户就是「选了就报错」——所以清单必须来自盘上的文件。
    """
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        _fake_realesrgan(Path(tmp), stems=("realesrgan-x4plus",))
        assert [m.key for m in up.models("realesrgan")] == ["realesrgan-x4plus"]
        assert up.find_model("realesrgan", "realesrnet-x4plus") is None


def test_waifu2x_models_are_folders_not_file_names():
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        _fake_waifu2x(Path(tmp))
        assert [m.key for m in up.models("waifu2x")] == [
            "models-cunet", "models-upconv_7_photo"], "waifu2x 的模型是目录"


def test_one_times_is_only_offered_where_it_really_runs():
    """实测：upconv 那两个目录没有 `noise0_model.param`，1 倍直接报错（rc=3221226505）。

    所以 1 倍只能给 cunet。这条是「倍数菜单跟着模型走」的直接依据。
    """
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        _fake_waifu2x(Path(tmp))
        cunet = up.find_model("waifu2x", "models-cunet")
        upconv = up.find_model("waifu2x", "models-upconv_7_photo")
        assert 1 in cunet.scales
        assert 1 not in upconv.scales, "upconv 缺 noise0_model.param，不能给它 1 倍"
        assert upconv.scales == (2, 4), upconv.scales


def test_a_model_folder_without_any_usable_scale_is_not_listed():
    """一个倍数都算不出来的目录不列给用户（列了就是「选了就报错」）。"""
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        d = Path(tmp) / "waifu2x"
        (d / "models-empty").mkdir(parents=True)
        (d / "waifu2x-ncnn-vulkan.exe").write_bytes(b"MZ")
        # 只放一个用途不明的文件：既没有 noise0 也没有 scale2.0x
        (d / "models-empty" / "whatever.param").write_bytes(b"x")
        assert up.models("waifu2x") == []


def test_scales_are_the_measured_ones():
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        _fake_realesrgan(Path(tmp))
        _fake_waifu2x(Path(tmp))
        for m in up.models("realesrgan"):
            assert m.scales == (2, 3, 4), f"{m.key} 的倍数是实测过的 2/3/4"
        assert up.find_model("waifu2x", "models-cunet").scales == (1, 2, 4)


def test_no_engine_installed_means_no_models():
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        assert up.models("realesrgan") == []
        assert up.models("waifu2x") == []
        assert up.default_model("realesrgan") == ""


def test_per_scale_network_is_derived_from_the_shipped_params():
    """「倍数越高越贵」还是「三档同价」，由盘上有没有 -x2/-x3 决定，不按名字硬编。

    实测依据：animevideov3 带三份网络 → 4 倍是 2 倍的 3.4 倍；
    x4plus 只有一份 x4 网络、2/3/4 倍都跑它再缩放 → 三档每帧耗时几乎一样。
    """
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        _fake_realesrgan(Path(tmp))
        assert up.find_model("realesrgan", "realesr-animevideov3").per_scale_network is True
        assert up.find_model("realesrgan", "realesrgan-x4plus").per_scale_network is False


def test_defaults_never_name_a_model_that_is_not_there():
    """包换过之后，默认值必须落在**真有的**模型上（不能点开就报错）。"""
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        _fake_realesrgan(Path(tmp), stems=("realesrgan-x4plus-anime",))
        assert up.default_model("realesrgan") == "realesrgan-x4plus-anime"
        assert up.default_model("realesrgan", video=True) == "realesrgan-x4plus-anime"


def test_video_defaults_to_the_model_built_for_video():
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        _fake_realesrgan(Path(tmp))
        assert up.default_model("realesrgan", video=True) == "realesr-animevideov3"
        assert up.default_model("realesrgan", video=False) == "realesrgan-x4plus"


def test_default_scale_prefers_two_times():
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        _fake_realesrgan(Path(tmp))
        assert up.default_scale(up.find_model("realesrgan", "realesrgan-x4plus")) == 2


# ================================================================ 3. 命令行


def test_plan_image_args_points_only_at_files_that_exist():
    """每一条 `路径=` 都要指到真文件：写死文件名的话，上游改一次命名就全崩。"""
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        root = Path(tmp)
        _fake_realesrgan(root)
        exe = up.exe_path("realesrgan")
        assert exe is not None and exe.is_file()
        args = up.plan_image_args("realesrgan", src=root / "a.png", out=root / "b.png",
                                  model="realesrgan-x4plus", scale=2)
        assert args[0] == str(exe)
        assert "-n" in args and args[args.index("-n") + 1] == "realesrgan-x4plus"
        assert args[args.index("-s") + 1] == "2"
        assert args[args.index("-i") + 1] == str(root / "a.png")
        assert args[args.index("-o") + 1] == str(root / "b.png")
        # 单图模式不给 -f（它按输出名的后缀判格式），也不给 -g（默认自动）
        assert "-f" not in args and "-g" not in args


def test_waifu2x_uses_m_not_n():
    """两档的模型参数名不一样：`-n` 是名字、`-m` 是目录。给错那个它会去别处找。"""
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        root = Path(tmp)
        _fake_waifu2x(root)
        args = up.plan_image_args("waifu2x", src=root / "a.png", out=root / "b.png",
                                  model="models-cunet", scale=2)
        assert "-m" in args and "-n" not in args
        assert args[args.index("-m") + 1] == "models-cunet"


def test_gpu_is_only_passed_when_the_user_picked_one():
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        root = Path(tmp)
        _fake_realesrgan(root)
        auto = up.plan_image_args("realesrgan", src=root / "a.png", out=root / "b.png",
                                  model="realesrgan-x4plus", scale=2, gpu=-1)
        assert "-g" not in auto, "自动时不该给 -g（交给引擎自己挑）"
        picked = up.plan_image_args("realesrgan", src=root / "a.png", out=root / "b.png",
                                    model="realesrgan-x4plus", scale=2, gpu=1)
        assert picked[picked.index("-g") + 1] == "1"


def test_tta_is_off_unless_asked():
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        root = Path(tmp)
        _fake_realesrgan(root)
        plain = up.plan_image_args("realesrgan", src=root / "a.png", out=root / "b.png",
                                   model="realesrgan-x4plus", scale=2)
        assert "-x" not in plain
        tta = up.plan_image_args("realesrgan", src=root / "a.png", out=root / "b.png",
                                 model="realesrgan-x4plus", scale=2, tta=True)
        assert "-x" in tta


def test_directory_mode_must_pass_f():
    """目录输出**必须给 -f**：它按输出名的后缀判格式，而目录名没有后缀。

    实测：输出目录不存在 / 没给 -f 时直接报 `invalid outputpath extension type`。
    """
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        root = Path(tmp)
        _fake_realesrgan(root)
        args = up.plan_dir_args("realesrgan", in_dir=root / "in", out_dir=root / "out",
                                model="realesr-animevideov3", scale=2)
        assert args[args.index("-f") + 1] == "png"
        assert args[args.index("-s") + 1] == "2"


def test_planning_without_an_engine_says_which_one():
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        root = Path(tmp)
        try:
            up.plan_image_args("realesrgan", src=root / "a.png", out=root / "b.png",
                               model="realesrgan-x4plus", scale=2)
            raise AssertionError("没装也把命令行拼出来了")
        except ValueError as e:
            assert "realesrgan" in str(e)


# ================================================================ 4. 设备


#: waifu2x（新版 ncnn）的真实输出：4 行/台，多一行能力
WAIFU_DEVICE_TEXT = """
[0 NVIDIA GeForce RTX 5060 Laptop GPU]  queueC=2[8]  queueT=1[2]
[0 NVIDIA GeForce RTX 5060 Laptop GPU]  fp16-p/s/u/a=1/1/1/1  int8-p/s/u/a=1/1/1/1
[0 NVIDIA GeForce RTX 5060 Laptop GPU]  subgroup=32(32~32)  ops=1/1/1/1/1/1/1/1/1/1
[0 NVIDIA GeForce RTX 5060 Laptop GPU]  fp16-cm=16x16x16/16x8x16/16x8x8  int8-cm=16x16x32/16x8x32
[1 Intel(R) RaptorLake-S Mobile Graphics Controller]  queueC=0[1]  queueT=0[1]
[1 Intel(R) RaptorLake-S Mobile Graphics Controller]  fp16-p/s/u/a=1/1/1/1  int8-p/s/u/a=1/1/1/1
[1 Intel(R) RaptorLake-S Mobile Graphics Controller]  fp16-cm=0  int8-cm=0  bf16-cm=0  fp8-cm=0
[2 Intel(R) RaptorLake-S Mobile Graphics Controller]  queueC=0[1]  queueT=0[1]
[2 Intel(R) RaptorLake-S Mobile Graphics Controller]  fp16-cm=0  int8-cm=0
"""

#: realesrgan（2022 年的包）的真实输出：**没有 -cm 那一行**
REALESRGAN_DEVICE_TEXT = """
[0 NVIDIA GeForce RTX 5060 Laptop GPU]  queueC=2[8]  queueG=0[16]  queueT=1[2]
[0 NVIDIA GeForce RTX 5060 Laptop GPU]  bugsbn1=0  bugbilz=0  bugcopc=0  bugihfa=0
[0 NVIDIA GeForce RTX 5060 Laptop GPU]  fp16-p/s/a=1/1/1  int8-p/s/a=1/1/1
[0 NVIDIA GeForce RTX 5060 Laptop GPU]  subgroup=32  basic=1  vote=1  ballot=1  shuffle=1
[1 Intel(R) RaptorLake-S Mobile Graphics Controller]  queueC=0[1]  queueG=0[1]  queueT=0[1]
[1 Intel(R) RaptorLake-S Mobile Graphics Controller]  fp16-p/s/a=1/1/1  int8-p/s/a=1/1/1
"""


def test_devices_are_merged_by_id_from_several_lines():
    devs = up.parse_devices(WAIFU_DEVICE_TEXT)
    assert [d.id for d in devs] == [0, 1, 2]
    assert devs[0].name == "NVIDIA GeForce RTX 5060 Laptop GPU"
    assert devs[0].queues == 2 and devs[1].queues == 0


def test_the_recommendation_does_not_depend_on_the_cm_line():
    """实测：realesrgan 那一档**整行都没有** `fp16-cm=`。

    如果拿它当唯一判据，realesrgan 永远一个「推荐」都出不来——所以判据是设备名，
    `-cm` 只当加分项。
    """
    rs = up.parse_devices(REALESRGAN_DEVICE_TEXT)
    wf = up.parse_devices(WAIFU_DEVICE_TEXT)
    assert [d.recommended for d in rs] == [True, False], "realesrgan 也要能推荐独显"
    assert [d.recommended for d in wf] == [True, False, False]
    assert rs[0].coop == "", "老 ncnn 不打 -cm，这里本来就该是空的"
    assert wf[0].coop.startswith("fp16="), "新 ncnn 的能力位要读出来"


def test_the_integrated_gpu_is_not_recommended():
    for text in (WAIFU_DEVICE_TEXT, REALESRGAN_DEVICE_TEXT):
        devs = up.parse_devices(text)
        assert devs[1].discrete is False and devs[1].recommended is False
        assert "集成显卡" in devs[1].note or "独立显卡" not in devs[1].note


def test_the_same_card_reported_twice_is_kept_but_explained():
    """这台机器上集显被报了两次（两个入口）。多的要留着（那是它自己报的），
    但说明里要讲清「名字一样的就是同一块」——否则用户以为机器上有三块卡。"""
    devs = up.parse_devices(WAIFU_DEVICE_TEXT)
    assert len(devs) == 3 and len({d.name for d in devs}) == 2
    note = up.device_note(devs)
    assert "同一块" in note
    assert "22 倍" in note, "选错设备的代价必须写出来，否则这一栏没人会看"


def test_one_device_means_no_note():
    assert up.device_note(up.parse_devices(
        "[0 NVIDIA GeForce RTX 4090]  queueC=2[8]")) == ""


def test_garbage_device_output_is_simply_empty():
    """拿不到清单就回空（界面只显示「自动」）——**不要编一个出来**。"""
    for text in ("", "Usage: realesrgan-ncnn-vulkan -i infile", None):
        assert up.parse_devices(text) == []


# ================================================================ 5. 上限与尺寸


def test_target_size_is_an_exact_multiple():
    assert up.target_size(1024, 1024, 2) == (2048, 2048)
    assert up.target_size(1920, 1080, 4) == (7680, 4320)
    # **不取偶数**：这两档不做视频编码，奇数尺寸是合法的
    assert up.target_size(101, 51, 3) == (303, 153)


def test_the_eight_k_ceiling_is_explained():
    assert up.size_problem(1920, 1080, 4) == "", "1080p 的 4 倍正好是 8K，应当放行"
    problem = up.size_problem(3840, 2160, 4)
    assert "15360×8640" in problem and "降一档" in problem
    assert up.size_problem(1024, 1024, 0) != ""


def test_too_many_frames_is_refused_with_the_number():
    assert up.frames_problem(1800) == ""
    problem = up.frames_problem(1801)
    assert "1801 帧" in problem and "60 秒" in problem


def test_temp_bytes_counts_both_frame_sets():
    """输入帧与输出帧同时留在盘上，所以是「输入 + 输出」。估小了会跑到一半没盘。"""
    one = up.temp_bytes(1, 1000, 1000, 2)
    assert one > 1000 * 1000 * 0.3, "至少要有输入那一份"
    assert up.temp_bytes(10, 1000, 1000, 2) == one * 10
    bigger = up.temp_bytes(1, 1000, 1000, 4)
    assert bigger > one * 3, "4 倍那一份要大得多"


# ================================================================ 6. 时间预估


def test_the_estimate_is_per_model_not_per_engine():
    """这一版最重要的一个纠正：同一个包里两个模型差约 75 倍。

    按引擎记的话，给视频默认的那一档会被估成几十分钟，其实只要几十秒。
    """
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        _fake_realesrgan(Path(tmp))
        fast = up.find_model("realesrgan", "realesr-animevideov3")
        heavy = up.find_model("realesrgan", "realesrgan-x4plus")
        px = 1920 * 1080
        assert up.estimate_seconds(fast, 2, px, 150) < 60
        assert up.estimate_seconds(heavy, 2, px, 150) > 300
        assert up.estimate_seconds(heavy, 2, px, 150) > up.estimate_seconds(fast, 2, px, 150) * 20


def test_a_single_network_model_costs_the_same_at_every_scale():
    """`realesrgan-x4plus` 只有一份 x4 网络，2/3/4 倍都跑它再缩放 → 三档同价。"""
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        _fake_realesrgan(Path(tmp))
        flat = up.find_model("realesrgan", "realesrgan-x4plus")
        network = up.find_model("realesrgan", "realesr-animevideov3")
        assert flat is not None and network is not None
        two, four = up.estimate_seconds(flat, 2, 1000000), up.estimate_seconds(flat, 4, 1000000)
        assert two == four, f"三档同价的模型差了：2 倍={two} 4 倍={four}"
        # 快模型要用「大一点的活」才看得出差别：animevideov3 每百万像素只要 0.058 秒，
        # 一张一百万的图四舍五入都是 1 秒
        px, frames = 1920 * 1080, 30
        n2 = up.estimate_seconds(network, 2, px, frames)
        n4 = up.estimate_seconds(network, 4, px, frames)
        assert n4 > n2 * 2, f"按倍数换网络的模型 4 倍没有明显更贵：{n2} → {n4}"


def test_an_unmeasured_model_is_estimated_heavily():
    """上游换了包、出现没量过的模型时按偏重估——**估久比估短好**。"""
    unknown = up.Model(key="brand-new-model", label="", note="", scales=(2,))
    assert up.estimate_seconds(unknown, 2, 1000000, 10) > 10


def test_the_estimate_is_a_range_and_reads_as_one():
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        _fake_realesrgan(Path(tmp))
        m = up.find_model("realesrgan", "realesr-animevideov3")
        lo, hi = up.estimate_range(m, 2, 1920 * 1080, 150)
        assert lo < hi
        text = up.estimate_text(m, 2, 1920 * 1080, 150)
        assert "大约" in text and "帧" in text, text


def test_human_seconds_never_hands_back_raw_seconds():
    assert up.human_seconds(45) == "45 秒"
    assert "分钟" in up.human_seconds(600)
    assert "小时" in up.human_seconds(7200)
    assert "秒" not in up.human_seconds(7200).replace("小时", "")


def test_estimate_for_an_unknown_model_does_not_raise():
    """估时只决定超时与界面上一句话，为它把一件正经事判失败代价完全不成比例。"""
    assert up.estimate_for("realesrgan", "没有这个模型", 2, 1000000) > 0


# ================================================================ 7. 就绪判断


def test_a_missing_engine_says_where_to_go():
    """「还没装」这句要说清**缺什么**，并且给一个**能照做的去处**（这两个是不同字段）。"""
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        state = up.local_route_state("realesrgan", installed=set(), vulkan=True)
        assert state.available is False
        assert "还没装" in state.reason, state.reason
        assert "本机引擎" in state.action, state.action
        assert state.action_route == "engines", "要给一个能跳过去的入口"


def test_without_vulkan_it_refuses_and_says_why():
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        _fake_realesrgan(Path(tmp))
        state = up.local_route_state("realesrgan", installed={"realesrgan"}, vulkan=False)
        assert state.available is False and "Vulkan" in state.reason


def test_installed_but_the_models_are_missing_says_reinstall():
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        d = Path(tmp) / "realesrgan"
        d.mkdir(parents=True)
        (d / "realesrgan-ncnn-vulkan.exe").write_bytes(b"MZ")
        state = up.local_route_state("realesrgan", installed={"realesrgan"}, vulkan=True)
        assert state.available is False and "重新下一次" in state.action


def test_a_ready_route_says_nothing_extra():
    with tempfile.TemporaryDirectory() as tmp, _FakeEngineRoot(Path(tmp)):
        _fake_realesrgan(Path(tmp))
        state = up.local_route_state("realesrgan", installed={"realesrgan"}, vulkan=True)
        assert state.available is True and state.reason == ""


# ================================================================ 8. 引擎输出处理


def test_the_device_lines_are_filtered_out_of_error_text():
    """它一次打十几行设备清单。不滤掉的话，用户拿到的「尾部」全是显卡参数，
    真正的错被淹在最前面。"""
    text = (WAIFU_DEVICE_TEXT + "\ninvalid gpu device\n"
            + "0.00%\n0.00%\n_wfopen a.param failed\n")
    tail = urun._error_tail(text)
    assert "NVIDIA" not in tail and "queueC" not in tail
    assert "invalid gpu device" in tail and "_wfopen" in tail
    assert "0.00%" not in tail, "进度行也不是错"


def test_error_text_is_never_empty():
    assert urun._error_tail("").strip()
    assert urun._error_tail(WAIFU_DEVICE_TEXT).strip(), "只有设备清单时也要给出一句话"


def test_frames_are_renumbered_so_the_concat_pattern_matches():
    """引擎给产物起什么名我们不掌握，所以按排序重编一遍序号。"""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        for name in ("b.png", "a.png", "c.png"):
            (d / name).write_bytes(b"x")
        pattern = urun._number_frames(d)
        assert [p.name for p in sorted(d.iterdir())] == [
            "frame00000001.png", "frame00000002.png", "frame00000003.png"]
        assert pattern.endswith("frame%08d.png")


def test_renumbering_is_a_no_op_when_names_already_match():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "frame00000001.png").write_bytes(b"x")
        (d / "frame00000002.png").write_bytes(b"x")
        urun._number_frames(d)
        assert sorted(p.name for p in d.iterdir()) == [
            "frame00000001.png", "frame00000002.png"]


def test_the_pattern_keeps_the_real_suffix():
    """我们给 `-f png`，但它真给了什么后缀就以什么为准，否则合帧一个都匹配不到。"""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "a.jpg").write_bytes(b"x")
        (d / "b.jpg").write_bytes(b"x")
        assert urun._number_frames(d).endswith("frame%08d.jpg")


def test_the_device_probe_does_not_just_run_it_with_no_arguments():
    """实测：不带参数运行它**只打用法表**，一行设备都没有。

    设备清单是在它开始初始化 Vulkan 时才打的，所以要用一个能走到那一步、
    又被无效设备号挡回来的调用（0.2 秒，且不碰 GPU、不加载模型）。
    """
    assert urun._DEVICE_PROBE_ARGS[0] == "-g"
    assert urun._DEVICE_PROBE_ARGS[1] not in ("", "-1"), "要一个无效的号才会被挡回来"
    assert "-i" in urun._DEVICE_PROBE_ARGS


# ================================================================ 9. 单图那条路（真子进程）


def test_run_image_returns_dims_and_verifies_the_product():
    with _TempData() as data, tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "a.png"
        _make_image(src, 64, 32)
        old = up.plan_image_args
        up.plan_image_args = _fake_image_plan(FAKE_IMAGE_ENGINE)  # type: ignore[assignment]
        try:
            out, w, h = asyncio.run(urun.run_image(
                "realesrgan", src=src, model="realesrgan-x4plus", scale=1))
        finally:
            up.plan_image_args = old  # type: ignore[assignment]
        assert out.exists() and out.is_file()
        assert (w, h) == (64, 32)
        assert str(out).startswith(str(data)), "产物必须落在 storage 里"


def test_run_image_fails_when_the_product_is_not_an_image():
    """引擎说「好了」但产物读不出尺寸 —— 必须判失败，不许当成功收下。"""
    with _TempData() as data, tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "a.png"
        _make_image(src, 64, 32)
        old = up.plan_image_args
        up.plan_image_args = _fake_image_plan(FAKE_IMAGE_ENGINE, "NOSIZE")  # type: ignore[assignment]
        try:
            asyncio.run(urun.run_image("realesrgan", src=src,
                                       model="realesrgan-x4plus", scale=1))
            raise AssertionError("坏产物被当成了成功")
        except RuntimeError as e:
            assert "尺寸" in str(e)
        finally:
            up.plan_image_args = old  # type: ignore[assignment]
        # 坏产物要收掉，不能留在 storage 里
        left = [p for p in (data / "storage").rglob("*") if p.is_file()]
        assert left == [], left


# ================================================================ 10. 视频那条三段式（真跑）


def _run_video(engine_script: str, src: Path, *, scale: int = 1, marker: str = ""):
    old_dir, old_img = up.plan_dir_args, up.plan_image_args
    up.plan_dir_args = _fake_dir_plan(engine_script, marker)  # type: ignore[assignment]
    up.plan_image_args = _fake_image_plan(engine_script, marker)  # type: ignore[assignment]
    try:
        return asyncio.run(urun.run_video("realesrgan", src=src,
                                          model="realesr-animevideov3", scale=scale))
    finally:
        up.plan_dir_args = old_dir  # type: ignore[assignment]
        up.plan_image_args = old_img  # type: ignore[assignment]


def test_the_video_pipeline_keeps_the_frame_count_fps_and_audio():
    """三段式真跑：拆帧 → 目录批量 → 合帧并把**原音轨**拷回来。

    要钉住的三条：帧率按**真实值**回填（上游 README 里写死 23.98 是它自己 demo 的）、
    音轨用 `-map 1:a:0?` 拷回、产物尺寸要跟「原尺寸 × 倍数」对得上。
    """
    with _TempData() as data, tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "clip.mp4"
        _make_video(src, 64, 32, 8, fps=8)
        out, w, h, frames, seconds = _run_video(FAKE_DIR_ENGINE, src, scale=1)
        info = _probe(out)
        assert (w, h) == (64, 32), f"{w}x{h}"
        assert frames == 8, f"应该是 8 帧，实际 {frames}"
        assert info["w"] == 64 and info["h"] == 32, info
        assert info["audio"] is True, "原音轨没拷回来"
        assert abs(info["duration"] - 1.0) < 0.25, info
        assert str(out).startswith(str(data))
        # 临时目录要收干净
        work = data / "storage" / "tmp"
        assert not work.exists() or not any(work.iterdir()), "临时目录没清"


def test_a_video_without_audio_still_works():
    """`-map 1:a:0?` 里那个问号是必需的：没有音轨的片子（静图样片）走 `-map 1:a:0`
    会直接失败。"""
    with _TempData(), tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "silent.mp4"
        _make_video(src, 64, 32, 4, fps=8, audio=False)
        out, w, h, frames, _ = _run_video(FAKE_DIR_ENGINE, src, scale=1)
        assert frames == 4 and (w, h) == (64, 32)
        assert _probe(out)["audio"] is False


def test_a_wrong_sized_product_is_refused():
    """尺寸对不上就判失败：宁可让用户重跑，也不要给他一段看着好了的错片子。"""
    with _TempData(), tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "clip.mp4"
        _make_video(src, 64, 32, 4, fps=8)
        try:
            _run_video(FAKE_DIR_ENGINE, src, marker="WRONGSIZE")
            raise AssertionError("尺寸不对的产物被当成了成功")
        except RuntimeError as e:
            assert "对不上" in str(e), str(e)


def test_missing_frames_are_reported_instead_of_assembled():
    """少帧拼出来会缺帧或变速 —— 必须如实报，不假装成功。"""
    script = r'''
import shutil, sys
from pathlib import Path
args = sys.argv[1:]
def val(f):
    return args[args.index(f) + 1] if f in args else ""
src, dst = Path(val("-i")), Path(val("-o"))
for i, f in enumerate(sorted(src.iterdir())):
    if i >= 2:
        break
    shutil.copyfile(f, dst / f.name)
'''
    with _TempData(), tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "clip.mp4"
        _make_video(src, 64, 32, 6, fps=8)
        try:
            _run_video(script, src, scale=1)
            raise AssertionError("少帧被当成了成功")
        except RuntimeError as e:
            assert "只放大了" in str(e) and "变速" in str(e), str(e)


def test_a_video_that_is_too_large_is_refused_before_any_work():
    with _TempData(), tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "clip.mp4"
        _make_video(src, 64, 32, 4, fps=8)
        try:
            _run_video(FAKE_DIR_ENGINE, src, scale=4 * 4000)
            raise AssertionError("越界的倍数没被拦住")
        except ValueError as e:
            assert "倍" in str(e)


# ================================================================ 11. 取消（真子进程）


def test_a_cancelled_run_kills_the_process_and_cleans_up():
    """任务中心那个「取消」只改数据库状态，**不取消协程**。

    所以引擎那一层必须有心跳：发现用户不要了就 terminate 并抛 CancelledError，
    否则点了取消之后进程照跑——一段视频能白烧几十分钟显卡。
    """
    with _TempData() as data, tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "clip.mp4"
        _make_video(src, 64, 32, 4, fps=8)
        # 引擎先睡 30 秒：给它足够时间被取消
        slow = r'''
import time, sys
time.sleep(30)
'''
        old = up.plan_dir_args
        up.plan_dir_args = _fake_dir_plan(slow)  # type: ignore[assignment]
        seen: list[int] = []

        async def beat(pct: int) -> bool:
            seen.append(pct)
            return False  # 第一次心跳就说「不要了」

        try:
            asyncio.run(urun.run_video("realesrgan", src=src,
                                       model="realesr-animevideov3", scale=1,
                                       on_progress=beat))
            raise AssertionError("取消没有停下来")
        except asyncio.CancelledError:
            pass
        finally:
            up.plan_dir_args = old  # type: ignore[assignment]
        assert seen, "心跳没被调用（那取消就无从谈起）"
        work = data / "storage" / "tmp"
        assert not work.exists() or not any(work.iterdir()), "取消后临时目录没清"


def test_the_heartbeat_gets_a_percentage():
    """取消与进度共用一次心跳：返回 False 表示取消。"""
    with _TempData(), tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "clip.mp4"
        _make_video(src, 64, 32, 4, fps=8)
        seen: list[int] = []

        async def beat(pct: int) -> bool:
            seen.append(pct)
            return True

        _ = seen
        old = up.plan_dir_args
        up.plan_dir_args = _fake_dir_plan(FAKE_DIR_ENGINE)  # type: ignore[assignment]
        try:
            asyncio.run(urun.run_video("realesrgan", src=src,
                                       model="realesr-animevideov3", scale=1,
                                       on_progress=beat))
        finally:
            up.plan_dir_args = old  # type: ignore[assignment]
        assert seen and all(0 <= p <= 100 for p in seen), seen
        assert seen[-1] == 100, f"最后要报到 100，实际 {seen[-1]}"


# ================================================================ 12. 接线


def test_the_runner_dispatches_the_new_kind():
    """`start()` 的兜底分支是「当图片跑」——新类型不加分支就会被悄悄跑成生图。"""
    from app.services.runner import TaskRunner

    r = TaskRunner()
    called: dict[str, object] = {}

    def _fake_start(tid: int) -> bool:
        called["id"] = tid
        return True

    r.start_upscale = _fake_start  # type: ignore[method-assign]
    assert r.start("upscale", 7) is True
    assert called["id"] == 7
    assert r.upscale_busy() == 0, "没有在跑的时候应当是 0"


def test_the_router_and_the_frontend_agree_on_the_paths():
    from app.main import app

    paths = app.openapi()["paths"]
    for path, methods in (("/api/upscale/options", {"get"}),
                          ("/api/upscale/image", {"post"}),
                          ("/api/upscale/video", {"post"}),
                          ("/api/upscale/comfy", {"post"})):
        assert path in paths, f"后端少了 {path}"
        assert set(paths[path]) == methods, f"{path} 的方法不对：{sorted(paths[path])}"

    api = (BACKEND.parent / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
    for call in ("upscaleOptions", "upscaleImage", "upscaleVideo", "upscaleComfy"):
        assert call in api, f"api.ts 里少了 {call}"
    for path in ("/api/upscale/options", "/api/upscale/image",
                 "/api/upscale/video", "/api/upscale/comfy"):
        assert path in api, f"api.ts 里少了 {path}"


def test_the_upload_dialog_shows_the_reason_instead_of_hiding_the_route():
    """不能用的路线也要列出来（灰掉 + 写原因 + 能给入口就给）。
    静默消失会让人以为这个软件不会放大——与去字幕四种手法同一个口径。"""
    dialog = (BACKEND.parent / "frontend" / "src" / "components" /
              "UpscaleDialog.tsx").read_text(encoding="utf-8")
    assert "available" in dialog and "reason" in dialog
    assert "用不了：" in dialog, "不可用的路线没把原因摆出来"
    assert "onGoEngines" in dialog, "「去本机引擎」那个跳转没接上"
    for text in ("开始放大", "放大几倍", "用哪块显卡", "目标尺寸"):
        assert text in dialog, f"弹窗里少了「{text}」"


def test_the_dialog_covers_the_comfy_route():
    """走 ComfyUI 是**异步**的，所以要和视频一样给流向说明，不能写成同步出资产。"""
    dialog = (BACKEND.parent / "frontend" / "src" / "components" /
              "UpscaleDialog.tsx").read_text(encoding="utf-8")
    assert "upscaleComfy" in dialog, "ComfyUI 那条没接到接口上"
    assert "工作流" in dialog and "由工作流决定" in dialog


def test_the_shipped_comfy_template_really_parses():
    """随包下发的放大模板必须能被**我们自己的解析器**认下来。

    不验的话，用户导入之后在画布上看不到参数、或者产物类型判成别的，
    而那时他只会觉得「这个模板是坏的」。
    """
    import json

    from app.services import comfy_workflow_service as cws

    path = BACKEND.parent / "docs" / "comfyui-upscale-workflow.json"
    graph = json.loads(path.read_text(encoding="utf-8"))
    parsed = cws.parse_workflow(graph)
    assert parsed["outputKind"] == "image", parsed["outputKind"]
    assert parsed["nodeCount"] == 4, parsed["nodeCount"]
    kinds = [p["type"] for p in parsed["paramMap"]]
    assert "image" in kinds, f"模板里要有一个图片入口，实际 {kinds}"
    # 模型名必须是**核对过的那个文件**（x4plus 不在那个 release 里，写它会扑空）
    text = path.read_text(encoding="utf-8")
    assert "realesr-general-x4v3.pth" in text
    assert "x4plus.pth" not in text, "模板里又出现了那个不在 release 里的文件名"


def test_the_template_and_the_doc_agree_on_the_file_name():
    doc = (BACKEND.parent / "docs" / "comfyui.md").read_text(encoding="utf-8")
    assert "realesr-general-x4v3.pth" in doc
    assert "comfyui-upscale-workflow.json" in doc, "文档里要指向那份模板"


def test_user_facing_text_has_no_markdown_markers():
    """后端给的文案是纯文本（React 不渲染 markdown）——前两版各踩过一次。"""
    texts = [up.COMFY_LABEL, up.device_note(up.parse_devices(WAIFU_DEVICE_TEXT))]
    for key in up.LOCAL_ENGINE_ORDER:
        texts.append(up.local_route_state(key, installed=set(), vulkan=True).reason)
    for m in (up.Model(key="realesrgan-x4plus", label="a", note="b", scales=(2,)),
              up.Model(key="models-cunet", label="a", note="b", scales=(1, 2))):
        texts.append(up.estimate_text(m, 2, 1920 * 1080, 30))
        texts.append(up.estimate_text(m, 2, 1920 * 1080, 1))
    texts.append(up.size_problem(4000, 4000, 4))
    texts.append(up.frames_problem(9999))
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
