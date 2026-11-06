"""像素格式转换。

滤镜层的共同契约（同目录下 scaler / crop / rotate 等都实现它）：

    ``process(frame: Frame) -> Frame``

只做数据形态变换，不改变时间戳语义（默认沿用输入的 pts/duration）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from backend.media.frame import Frame, PixelFormat


class BaseVideoFilter(ABC):
    """视频滤镜接口。"""

    name: str = ""

    @abstractmethod
    def process(self, frame: Frame) -> Frame:
        """输入一帧，输出一帧。"""

    def close(self) -> None:
        """释放资源；默认无资源可放。"""


def bgra_to_rgb(data: bytes, width: int, height: int) -> bytes:
    """BGRA -> RGB 紧密排列（去掉 alpha）。

    用步长切片赋值把逐像素搬运交给 C 层，避免 Python 循环成为渲染瓶颈。
    """
    out = bytearray(width * height * 3)
    out[0::3] = data[2::4]
    out[1::3] = data[1::4]
    out[2::3] = data[0::4]
    return bytes(out)


class PixelFormatConverter(BaseVideoFilter):
    """在 BGRA / RGBA / RGB 之间转换。"""

    name = "pixel_format"

    def __init__(self, target: PixelFormat) -> None:
        if target not in (PixelFormat.BGRA, PixelFormat.RGBA, PixelFormat.RGB):
            raise ValueError(f"不支持的转换目标: {target}")
        self.target = target

    def process(self, frame: Frame) -> Frame:
        if frame.pixel_format is self.target:
            return frame

        source = frame.pixel_format
        if source not in (PixelFormat.BGRA, PixelFormat.RGBA, PixelFormat.RGB):
            raise ValueError(f"不支持的源像素格式: {source}")

        data = bytes(frame.data)
        width, height = frame.width, frame.height

        if self.target is PixelFormat.RGB:
            if source is PixelFormat.BGRA:
                out = bgra_to_rgb(data, width, height)
            elif source is PixelFormat.RGBA:
                out = bytearray(width * height * 3)
                out[0::3] = data[0::4]
                out[1::3] = data[1::4]
                out[2::3] = data[2::4]
                out = bytes(out)
            else:
                out = data
        else:
            # 4 字节通道之间的重排
            if source is PixelFormat.RGB:
                raise ValueError("RGB -> BGRA/RGBA 请先补齐 alpha 通道")
            order = (
                (2, 1, 0, 3) if self.target is PixelFormat.BGRA else (0, 1, 2, 3)
            )
            out_array = bytearray(len(data))
            for channel in range(3):
                out_array[channel::4] = data[order[channel] :: 4]
            out_array[3::4] = b"\xFF" * (width * height)
            out = bytes(out_array)

        return frame.with_data(out, pixel_format=self.target, linesize=0)
