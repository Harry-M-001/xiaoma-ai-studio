"""从图片字节里读出宽高——纯 Python，不引入图像库。

为什么值得自己写：宽高不是装饰性字段。图生视频之前要拿首帧的**比例和分辨率**
跟所选画幅对帐（见 `preflight._first_frame_warnings`），读不到宽高，那条告警就
只能闭嘴——功能看着做了，实际是死的。

为什么不装 Pillow：本项目的分发形态里有「解压即用」的便携包，为了读两个整数
背一个十几 MB 的原生依赖不划算。这里支持的格式就是用户实际会碰到的那些，
每种格式的尺寸都在文件头附近，读几十个字节就够。

读不出来一律返回 `None`：宁可后面少提醒一句，也不要猜一个错的比例。
"""

from __future__ import annotations

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _png(data: bytes) -> tuple[int, int] | None:
    # 签名(8) + 长度(4) + 类型(4) + IHDR 宽高(8)
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return (width, height) if width and height else None


def _gif(data: bytes) -> tuple[int, int] | None:
    # GIF87a / GIF89a + 逻辑屏幕宽高（小端）
    if len(data) < 10:
        return None
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    return (width, height) if width and height else None


def _bmp(data: bytes) -> tuple[int, int] | None:
    if len(data) < 26:
        return None
    # 高度可能是负数（自顶向下的位图），取绝对值
    width = int.from_bytes(data[18:22], "little", signed=True)
    height = int.from_bytes(data[22:26], "little", signed=True)
    if width == 0 or height == 0:
        return None
    return (abs(width), abs(height))


def _jpeg(data: bytes) -> tuple[int, int] | None:
    """扫描到第一个 SOFn 段——只有它带真实尺寸。

    不能只看文件头：JPEG 开头是一串不定长的段（EXIF 缩略图里也有自己的
    SOFn），所以必须按段长一段段跳过去。
    """
    i = 2
    end = len(data)
    while i + 9 < end:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # 无参数段：填充字节 / 独立标记 / 重启标记
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            i += 2
            continue
        if marker == 0xD9:  # EOI：到头了还没见到 SOFn
            return None
        seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
        # SOF0–SOF15 里 0xC4(DHT) / 0xC8(JPG) / 0xCC(DAC) 不是帧头
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return (width, height) if width and height else None
        if seg_len < 2:
            return None
        i += 2 + seg_len
    return None


def _webp(data: bytes) -> tuple[int, int] | None:
    """WebP 有三种子格式，尺寸位置各不相同。

    各自的长度下限分开判：写一个「至少 30 字节」的总闸会把小图（纯色、图标）
    挡在外面——那种图恰好是最容易在测试和缩略图场景里出现的。
    """
    fourcc = data[12:16]
    if fourcc == b"VP8 ":  # 有损：帧头里 14 位宽高（小端）
        if len(data) < 30:
            return None
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
    elif fourcc == b"VP8L":  # 无损：宽高各 14 位，跨字节打包
        if len(data) < 25:
            return None
        b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
        width = 1 + (((b1 & 0x3F) << 8) | b0)
        height = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
    elif fourcc == b"VP8X":  # 扩展：24 位宽高（减一存储）
        if len(data) < 30:
            return None
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
    else:
        return None
    return (width, height) if width and height else None


def read_image_size(data: bytes, content_type: str = "") -> tuple[int, int] | None:
    """读图片宽高；不是支持的格式或数据不全时返回 None。"""
    if not data:
        return None
    try:
        if data.startswith(_PNG_SIG):
            return _png(data)
        if data.startswith((b"GIF87a", b"GIF89a")):
            return _gif(data)
        if data.startswith(b"BM"):
            return _bmp(data)
        if data.startswith(b"\xff\xd8"):
            return _jpeg(data)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return _webp(data)
    except (IndexError, ValueError):
        # 头部被截断的坏文件：当作读不出来，不要让它把保存流程带崩
        return None
    return None


# JPEG 的尺寸藏在可变长的 APPn 段（EXIF / ICC）之后，极端情况下 ICC 能有好几 MB。
# 读 2MB 是个「几乎一定够、又不会为一张图把内存吃满」的折中。
_HEAD_BYTES = 2 * 1024 * 1024


def read_image_size_from_file(path, head_bytes: int = _HEAD_BYTES) -> tuple[int, int] | None:
    """从磁盘上的图片文件读宽高，只读文件头那一段。

    存在的意义是**兜住历史数据**：`assets.width/height` 是后来才开始记的，
    在那之前入库的图片（以及从别的机器搬过来的库）这两个字段是空的。
    读不到库里的值就现读一次文件，总比让下游的告警静默失效要好。

    读不了一律返回 None——调用方必须把「读不出来」当成正常情况处理。
    """
    try:
        with open(path, "rb") as fh:
            return read_image_size(fh.read(head_bytes))
    except OSError:
        return None
