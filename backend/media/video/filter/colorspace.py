"""色彩空间转换（BGRA <-> YUV420P，BT.601 / BT.709）。

为什么需要它：采集和渲染在 RGB 域，而 H.264 编码在 YUV 域。实时链路里通常
交给编码器内部转换（省一次拷贝），但当需要「编码前做 YUV 域处理」或
「确认色彩矩阵」时，必须显式转换——矩阵用错的表现就是肤色偏绿/偏红。

限制：逐像素 Python 实现，适合验证与离线处理，4K 实时请走 GPU 或 ffmpeg 滤镜。
"""

from __future__ import annotations

from backend.media.frame import Frame, PixelFormat
from backend.media.video.filter.pixel_format import BaseVideoFilter

#: BT.601（SD）与 BT.709（HD）的 YUV 系数
_MATRIX = {
    "bt601": (0.299, 0.587, 0.114),
    "bt709": (0.2126, 0.7152, 0.0722),
}


class ColorSpace:
    """色彩矩阵常量。"""

    BT601 = "bt601"
    BT709 = "bt709"


class ColorSpaceConverter(BaseVideoFilter):
    """在 BGRA 与 YUV420P 之间转换。"""

    name = "colorspace"

    def __init__(self, target: PixelFormat = PixelFormat.YUV420P, matrix: str = "bt709") -> None:
        if matrix not in _MATRIX:
            raise ValueError(f"未知色彩矩阵: {matrix}")
        if target not in (PixelFormat.YUV420P, PixelFormat.BGRA):
            raise ValueError(f"不支持的目标格式: {target}")
        self.target = target
        self.matrix = matrix

    def process(self, frame: Frame) -> Frame:
        if frame.pixel_format is self.target:
            return frame
        if frame.pixel_format is PixelFormat.BGRA and self.target is PixelFormat.YUV420P:
            return self.bgra_to_yuv420p(frame)
        if frame.pixel_format is PixelFormat.YUV420P and self.target is PixelFormat.BGRA:
            return self.yuv420p_to_bgra(frame)
        raise ValueError(f"不支持 {frame.pixel_format} -> {self.target}")

    # ---- BGRA -> YUV420P ----
    def bgra_to_yuv420p(self, frame: Frame) -> Frame:
        kr, kg, kb = _MATRIX[self.matrix]
        kb_coeff = 0.5 / (1 - kb) if kb < 1 else 0.5
        kr_coeff = 0.5 / (1 - kr) if kr < 1 else 0.5

        width, height = frame.width, frame.height
        source = bytes(frame.data)
        y_plane = bytearray(width * height)
        u_plane = bytearray((width // 2) * (height // 2))
        v_plane = bytearray(len(u_plane))

        for y in range(height):
            row = y * width * 4
            for x in range(width):
                base = row + x * 4
                blue = source[base]
                green = source[base + 1]
                red = source[base + 2]
                luma = kr * red + kg * green + kb * blue
                y_plane[y * width + x] = max(0, min(255, int(luma)))

        for y in range(0, height - 1, 2):
            for x in range(0, width - 1, 2):
                # 2x2 取平均后再算色度，避免单点采样引入噪点
                red = green = blue = 0
                for dy in (0, 1):
                    for dx in (0, 1):
                        base = ((y + dy) * width + (x + dx)) * 4
                        blue += source[base]
                        green += source[base + 1]
                        red += source[base + 2]
                red /= 4
                green /= 4
                blue /= 4
                luma = kr * red + kg * green + kb * blue
                u_plane[(y // 2) * (width // 2) + x // 2] = max(
                    0, min(255, int(128 + kb_coeff * (blue - luma)))
                )
                v_plane[(y // 2) * (width // 2) + x // 2] = max(
                    0, min(255, int(128 + kr_coeff * (red - luma)))
                )

        data = bytes(y_plane) + bytes(u_plane) + bytes(v_plane)
        return frame.with_data(
            data,
            pixel_format=PixelFormat.YUV420P,
            linesize=width,
        )

    # ---- YUV420P -> BGRA ----
    def yuv420p_to_bgra(self, frame: Frame) -> Frame:
        kr, kg, kb = _MATRIX[self.matrix]
        kb_coeff = 2 - 2 * kb
        kr_coeff = 2 - 2 * kr
        half_width = (frame.width + 1) // 2
        half_height = (frame.height + 1) // 2

        source = bytes(frame.data)
        y_size = frame.width * frame.height
        u_size = half_width * half_height
        y_plane = source[:y_size]
        u_plane = source[y_size : y_size + u_size]
        v_plane = source[y_size + u_size : y_size + 2 * u_size]

        out = bytearray(frame.width * frame.height * 4)
        for y in range(frame.height):
            for x in range(frame.width):
                luma = y_plane[y * frame.width + x]
                u = u_plane[(y // 2) * half_width + (x // 2)] - 128
                v = v_plane[(y // 2) * half_width + (x // 2)] - 128
                red = luma + kr_coeff * v
                blue = luma + kb_coeff * u
                green = (luma - kr * red - kb * blue) / kg if kg else luma
                base = (y * frame.width + x) * 4
                out[base] = max(0, min(255, int(blue)))
                out[base + 1] = max(0, min(255, int(green)))
                out[base + 2] = max(0, min(255, int(red)))
                out[base + 3] = 255

        return frame.with_data(
            bytes(out),
            pixel_format=PixelFormat.BGRA,
            linesize=frame.width * 4,
        )


__all__ = ["ColorSpaceConverter", "ColorSpace"]
