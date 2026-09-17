"""图片宽高的读取（直接 python 运行）。

运行：venv/Scripts/python tests/test_image_size.py

为什么这个小事值得一个测试文件：宽高是**图生视频预检的输入**。
它读不出来，那条「首帧是 16:9 却选了 9:16」的告警就永远不触发——
功能看着做了，实际是死的。本项目之前就正是这个状态：31 条资产里 0 条有宽高。

三种格式的尺寸藏在文件头的不同位置，JPEG 更是要按段长一截截跳（EXIF 里还有
自己的缩略图 SOFn，只看开头会读错），所以每种格式都用一个手工拼出来的最小
文件头钉住偏移量。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.services.image_size import read_image_size  # noqa: E402


# ---------- 手工拼最小文件头 ----------


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")          # IHDR 长度
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
    )


def _gif(width: int, height: int) -> bytes:
    return b"GIF89a" + width.to_bytes(2, "little") + height.to_bytes(2, "little")


def _bmp(width: int, height: int) -> bytes:
    """偏移量：0 文件头(14) 之后是 DIB 头长度(4)，宽高紧随其后——宽在 18，高在 22。

    宽高按**有符号**写：自顶向下的位图高度是负数。
    """
    return (
        b"BM"
        + bytes(16)
        + width.to_bytes(4, "little", signed=True)
        + height.to_bytes(4, "little", signed=True)
    )


def _jpeg(width: int, height: int, *, with_app0: bool = True) -> bytes:
    """SOI + [APP0] + SOF0。APP0 让「按段长跳过」这条路径真的被走到。"""
    out = b"\xff\xd8"
    if with_app0:
        out += b"\xff\xe0" + (4).to_bytes(2, "big") + b"\x00\x10"
    out += (
        b"\xff\xc0"
        + (17).to_bytes(2, "big")            # 段长
        + b"\x08"                            # 采样精度
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + bytes(4)
    )
    return out


def _webp_vp8x(width: int, height: int) -> bytes:
    return (
        b"RIFF" + bytes(4) + b"WEBP" + b"VP8X" + bytes(4) + bytes(4)
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )


def _webp_vp8(width: int, height: int) -> bytes:
    return (
        b"RIFF" + bytes(4) + b"WEBP" + b"VP8 " + bytes(4)
        + b"\x00\x00\x00" + b"\x9d\x01\x2a"
        + width.to_bytes(2, "little")
        + height.to_bytes(2, "little")
    )


def _webp_vp8l(width: int, height: int) -> bytes:
    w, h = width - 1, height - 1
    return (
        b"RIFF" + bytes(4) + b"WEBP" + b"VP8L" + bytes(4) + b"\x2f"
        + bytes([w & 0xFF, ((w >> 8) & 0x3F) | ((h & 0x03) << 6), (h >> 2) & 0xFF,
                 ((h >> 10) & 0x0F)])
    )


# ---------- 各格式的偏移量 ----------


def test_png():
    assert read_image_size(_png(1280, 720)) == (1280, 720)
    assert read_image_size(_png(720, 1280)) == (720, 1280)


def test_gif():
    assert read_image_size(_gif(320, 240)) == (320, 240)


def test_bmp():
    assert read_image_size(_bmp(1024, 768)) == (1024, 768)


def test_bmp_with_top_down_height():
    """自顶向下的位图高度是负数，取绝对值。"""
    assert read_image_size(_bmp(800, -600)) == (800, 600)


def test_jpeg():
    assert read_image_size(_jpeg(1920, 1080)) == (1920, 1080)


def test_jpeg_without_leading_app0_segment():
    assert read_image_size(_jpeg(640, 480, with_app0=False)) == (640, 480)


def test_jpeg_skips_thumbnail_of_the_same_marker_type():
    """EXIF 缩略图里也有 SOFn，但它被包在 APP1 段里——必须靠段长跳过去。

    只看「第一个 0xFFC0」的实现会读成缩略图的尺寸。
    """
    thumb = _jpeg(160, 120, with_app0=False)[2:]      # 缩略图自己的 SOF0
    app1 = b"\xff\xe1" + (len(thumb) + 2).to_bytes(2, "big") + thumb
    real = _jpeg(4032, 3024)[2:]
    assert read_image_size(b"\xff\xd8" + app1 + real) == (4032, 3024)


def test_webp_variants():
    assert read_image_size(_webp_vp8x(1280, 720)) == (1280, 720)
    assert read_image_size(_webp_vp8(1280, 720)) == (1280, 720)
    assert read_image_size(_webp_vp8l(1280, 720)) == (1280, 720)


def test_unusual_sizes():
    assert read_image_size(_webp_vp8l(1, 1)) == (1, 1)
    assert read_image_size(_png(1, 1)) == (1, 1)


# ---------- 读不出来时必须安静地返回 None ----------


def test_unknown_and_broken_input_returns_none():
    cases = {
        "空": b"",
        "纯文本": b"hello world, not an image at all",
        "视频头": b"\x00\x00\x00\x18ftypmp42",
        "PNG 被截断": _png(1280, 720)[:20],
        "GIF 被截断": _gif(320, 240)[:6],
        "长得很像 JPEG 的垃圾": b"\xff\xd8" + b"\x00" * 40,
        "长得很像 WebP 的垃圾": b"RIFF" + bytes(4) + b"WEBP" + b"JUNK" + bytes(20),
        "零尺寸": _gif(0, 0),
    }
    for name, data in cases.items():
        assert read_image_size(data) is None, f"{name} 应当读不出来"


def test_does_not_raise_on_any_prefix_of_a_real_header():
    """逐字节截断也不能抛异常——这在「保存流程里顺手读一下」的位置上是硬要求。"""
    full = _png(1280, 720) + _jpeg(1920, 1080) + _webp_vp8x(1280, 720)
    for cut in range(len(full)):
        read_image_size(full[:cut])  # 不抛就算过


# ---------- 接上了没有 ----------


def test_storage_helper_returns_kwargs_only_for_images():
    from app.services import storage

    assert storage.image_size_kwargs(_png(1280, 720)) == {"width": 1280, "height": 720}
    # 视频/垃圾数据返回空字典，这样 `Asset(**kwargs)` 不会多出 None 列
    assert storage.image_size_kwargs(b"\x00\x00\x00\x18ftypmp42") == {}


def test_every_place_that_saves_an_image_records_its_size():
    """静态守卫：存图片的 Asset(...) 必须带上宽高。

    漏一处的后果不是「某个字段为空」，而是**预检里那条告警静默失效**——
    而且是那种跑起来不报错、只能靠人去比对数据的失效。

    允许两种写法：顺手从字节里读（`image_size_kwargs`），
    或者从已知来源显式搬运（如从视频截帧，宽高继承自那个视频）。
    """
    app = BACKEND / "app"
    offenders: list[str] = []
    for path in app.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, call in _asset_calls(text):
            if 'kind="image"' not in call and "kind=f.kind" not in call:
                continue
            if "image_size_kwargs" in call or ("width=" in call and "height=" in call):
                continue
            offenders.append(f"{path.relative_to(BACKEND)}:{lineno}")
    assert not offenders, (
        f"这些地方存图片却没记宽高：{offenders}——"
        "图生视频的「首帧与画幅不符」告警要靠它，缺了就等于没做"
    )


def _asset_calls(text: str) -> list[tuple[int, str]]:
    """取出每一处 `Asset(...)` 的完整实参（按括号配对，别用固定长度截断）。"""
    out: list[tuple[int, str]] = []
    start = 0
    while True:
        idx = text.find("Asset(", start)
        if idx < 0:
            return out
        depth = 0
        i = idx + len("Asset")
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append((text.count("\n", 0, idx) + 1, text[idx : i + 1]))
        start = i + 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001  一个用例炸了别把剩下的都带下去
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
