"""旋转（90 / 180 / 270 度）。

只做 90 的整数倍：任意角度需要重采样且分辨率会变化，属于另一个量级的实现，
当前用不着。90/270 会交换宽高。
"""

from __future__ import annotations

from backend.media.frame import Frame, PixelFormat
from backend.media.video.filter.pixel_format import BaseVideoFilter


class Rotate(BaseVideoFilter):
    name = "rotate"

    def __init__(self, degrees: int = 90) -> None:
        if degrees % 90 != 0:
            raise ValueError("只支持 90 度的整数倍")
        self.degrees = degrees % 360

    def process(self, frame: Frame) -> Frame:
        if self.degrees == 0:
            return frame
        if frame.pixel_format is not PixelFormat.BGRA:
            raise ValueError("Rotate 当前只支持 BGRA")

        source = bytes(frame.data)
        width, height = frame.width, frame.height
        stride = width * 4
        bpp = 4

        if self.degrees == 180:
            out = bytearray(len(source))
            for row in range(height):
                source_row = source[row * stride : (row + 1) * stride]
                target_row = (height - 1 - row) * stride
                # 逐像素反序
                for x in range(width):
                    src = (width - 1 - x) * bpp
                    out[target_row + x * bpp : target_row + x * bpp + bpp] = source_row[
                        src : src + bpp
                    ]
            return frame.with_data(bytes(out), linesize=stride)

        new_width, new_height = height, width
        new_stride = new_width * bpp
        out = bytearray(new_stride * new_height)

        if self.degrees == 90:  # 顺时针
            for y in range(height):
                source_row = y * stride
                target_column = (new_width - 1 - y) * bpp
                for x in range(width):
                    src = source_row + x * bpp
                    target = x * new_stride + target_column
                    out[target : target + bpp] = source[src : src + bpp]
        else:  # 270（逆时针 90）
            for y in range(height):
                source_row = y * stride
                target_column = y * bpp
                for x in range(width):
                    src = source_row + x * bpp
                    target = (new_height - 1 - x) * new_stride + target_column
                    out[target : target + bpp] = source[src : src + bpp]

        return frame.with_data(
            bytes(out),
            width=new_width,
            height=new_height,
            linesize=new_stride,
        )
