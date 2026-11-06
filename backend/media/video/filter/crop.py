"""裁剪。

裁剪放在滤镜层而不是采集层，是为了「采整屏、只编感兴趣区域」这种用法：
采集一次可服务于多个裁剪视图（例如同时推整屏和某个窗口）。
区域越界时夹到画面内，不抛错——远端分辨率变化时链路不该中断。
"""

from __future__ import annotations

from backend.media.frame import Frame, PixelFormat
from backend.media.video.filter.pixel_format import BaseVideoFilter


class Crop(BaseVideoFilter):
    name = "crop"

    def __init__(self, x: int, y: int, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("裁剪尺寸必须大于 0")
        self.x = max(0, x)
        self.y = max(0, y)
        self.width = width
        self.height = height

    def process(self, frame: Frame) -> Frame:
        if frame.pixel_format is not PixelFormat.BGRA:
            raise ValueError("Crop 当前只支持 BGRA")

        bytes_per_pixel = 4
        source_stride = frame.width * bytes_per_pixel
        x = min(self.x, max(0, frame.width - 2))
        y = min(self.y, max(0, frame.height - 2))
        width = min(self.width, frame.width - x) & ~1  # 偶数宽，保证编码器可用
        height = min(self.height, frame.height - y) & ~1
        if width <= 0 or height <= 0:
            raise ValueError(
                f"裁剪区域超出画面: ({x},{y}) {self.width}x{self.height} "
                f"in {frame.width}x{frame.height}"
            )

        source = bytes(frame.data)
        out = bytearray(width * height * bytes_per_pixel)
        for row in range(height):
            source_start = (y + row) * source_stride + x * bytes_per_pixel
            target_start = row * width * bytes_per_pixel
            out[target_start : target_start + width * bytes_per_pixel] = source[
                source_start : source_start + width * bytes_per_pixel
            ]

        return frame.with_data(
            bytes(out), width=width, height=height, linesize=width * bytes_per_pixel
        )
