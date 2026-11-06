"""图片落盘渲染端。

无窗口 / 无 GPU 环境下验证「解码 -> Frame -> 输出」链路时使用：
把每帧写成 PNG，肉眼即可确认画面是否正确。窗口渲染器（D3D11/OpenGL）
实现同一套 ``open/render/close`` 契约，可在上层直接替换。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.media.frame import Frame, PixelFormat
from backend.media.video.filter.pixel_format import bgra_to_rgb

logger = get_logger(__name__)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class RenderError(ElinkError):
    code = ErrorCode.IO


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def write_png(
    path: str | Path, width: int, height: int, rgb: bytes, level: int = 1
) -> Path:
    """写 8bit RGB 的 PNG（每行滤波类型 0）。"""
    line_size = width * 3
    if len(rgb) < line_size * height:
        raise RenderError(
            "RGB 数据长度不足", detail=f"{len(rgb)} < {line_size * height}"
        )

    raw = b"".join(
        b"\x00" + rgb[row * line_size : (row + 1) * line_size]
        for row in range(height)
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as file:
        file.write(_PNG_SIGNATURE)
        file.write(_png_chunk(b"IHDR", header))
        file.write(_png_chunk(b"IDAT", zlib.compress(raw, level)))
        file.write(_png_chunk(b"IEND", b""))
    return target


class ImageSinkRenderer:
    """把 Frame 依次写成 ``frame_000001.png``。"""

    name = "image_sink"

    def __init__(self, output_dir: str | Path = "out/frames", every_n: int = 1) -> None:
        self.output_dir = Path(output_dir)
        self.every_n = max(1, every_n)
        self._count = 0
        self._written = 0
        self._opened = False

    def open(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._opened = True
        logger.info("渲染输出目录: %s", self.output_dir.resolve())

    def render(self, frame: Frame) -> Path | None:
        if not self._opened:
            raise RenderError("渲染器尚未打开")

        self._count += 1
        if (self._count - 1) % self.every_n:
            return None

        rgb = self._to_rgb(frame)
        path = self.output_dir / f"frame_{self._written:06d}.png"
        write_png(path, frame.width, frame.height, rgb)
        self._written += 1
        return path

    def close(self) -> None:
        if not self._opened:
            return
        self._opened = False
        logger.info("渲染输出完成: %d 帧", self._written)

    @staticmethod
    def _to_rgb(frame: Frame) -> bytes:
        data = bytes(frame.data)
        if frame.pixel_format is PixelFormat.RGB:
            return data
        if frame.pixel_format is PixelFormat.BGRA:
            return bgra_to_rgb(data, frame.width, frame.height)
        if frame.pixel_format is PixelFormat.RGBA:
            out = bytearray(frame.width * frame.height * 3)
            out[0::3] = data[0::4]
            out[1::3] = data[1::4]
            out[2::3] = data[2::4]
            return bytes(out)
        raise RenderError(f"渲染端不支持的像素格式: {frame.pixel_format}")

    def __enter__(self) -> "ImageSinkRenderer":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
