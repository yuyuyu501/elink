"""缩放（最近邻 / 双线性）。

窗口缩放这种「下行链路」场景用最近邻就够（快、无振铃）；需要保真时用双线性。
最近邻使用 NumPy 批量索引；双线性保留按行处理。
"""

from __future__ import annotations

import numpy as np

from backend.media.frame import Frame, PixelFormat
from backend.media.video.filter.pixel_format import BaseVideoFilter


class Scaler(BaseVideoFilter):
    name = "scaler"

    def __init__(
        self,
        width: int,
        height: int,
        *,
        bilinear: bool = False,
        keep_aspect: bool = False,
    ) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("缩放目标尺寸必须大于 0")
        self.width = width
        self.height = height
        self.bilinear = bilinear
        self.keep_aspect = keep_aspect

    def _target_size(self, frame: Frame) -> tuple[int, int]:
        if not self.keep_aspect:
            return self.width, self.height
        source_ratio = frame.width / frame.height
        target_ratio = self.width / self.height
        if source_ratio > target_ratio:
            return self.width, max(2, int(self.width / source_ratio))
        return max(2, int(self.height * source_ratio)), self.height

    def process(self, frame: Frame) -> Frame:
        if frame.pixel_format is not PixelFormat.BGRA:
            raise ValueError("Scaler 当前只支持 BGRA")

        target_width, target_height = self._target_size(frame)
        if (target_width, target_height) == (frame.width, frame.height):
            return frame

        source = bytes(frame.data)
        source_stride = frame.width * 4
        if not self.bilinear:
            pixels = np.frombuffer(source, dtype=np.uint8).reshape(frame.height, frame.width, 4)
            xs = np.minimum((np.arange(target_width) * (frame.width / target_width)).astype(int),
                            frame.width - 1)
            ys = np.minimum((np.arange(target_height) * (frame.height / target_height)).astype(int),
                            frame.height - 1)
            out = pixels[ys[:, None], xs].tobytes()
        else:
            out = bytearray(target_width * target_height * 4)
            for y in range(target_height):
                y_ratio = (y + 0.5) * frame.height / target_height - 0.5
                y0 = max(0, int(y_ratio))
                y1 = min(frame.height - 1, y0 + 1)
                wy = y_ratio - y0
                row0 = y0 * source_stride
                row1 = y1 * source_stride
                target_row = y * target_width * 4
                for x in range(target_width):
                    x_ratio = (x + 0.5) * frame.width / target_width - 0.5
                    x0 = max(0, int(x_ratio))
                    x1 = min(frame.width - 1, x0 + 1)
                    wx = x_ratio - x0

                    base0 = row0 + x0 * 4
                    base1 = row0 + x1 * 4
                    base2 = row1 + x0 * 4
                    base3 = row1 + x1 * 4
                    for channel in range(4):
                        top = source[base0 + channel] * (1 - wx) + source[base1 + channel] * wx
                        bottom = source[base2 + channel] * (1 - wx) + source[base3 + channel] * wx
                        value = top * (1 - wy) + bottom * wy
                        out[target_row + x * 4 + channel] = int(value)

        return frame.with_data(
            bytes(out),
            width=target_width,
            height=target_height,
            linesize=target_width * 4,
        )
