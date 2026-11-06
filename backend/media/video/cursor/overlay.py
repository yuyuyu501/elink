"""指针叠加：把光标图层合成到画面帧。

合成用「逐像素 alpha 混合」而不是覆盖，因为现代光标带抗锯齿边缘；
不留 alpha 混合会让光标边缘出现黑边。

性能：只遍历光标矩形范围内与其外接的行，不扫描整帧。
"""

from __future__ import annotations

from backend.logging.logger import get_logger
from backend.media.frame import Frame, PixelFormat
from backend.media.video.cursor.capture import CursorState

logger = get_logger(__name__)


class CursorOverlay:
    name = "cursor_overlay"

    def __init__(self, scale_x: float = 1.0, scale_y: float = 1.0) -> None:
        # 采集区域与屏幕原点不同、或画面被缩放过时，坐标需要换算
        self.scale_x = scale_x
        self.scale_y = scale_y
        self._last_state: CursorState | None = None

    def capture_state(self, state: CursorState) -> None:
        """记录最新指针状态（由采集线程更新，渲染线程读取）。"""
        self._last_state = state

    def process(self, frame: Frame) -> Frame:
        state = self._last_state
        if state is None or state.is_empty or not state.image:
            return frame
        return self.composite(frame, state)

    def composite(self, frame: Frame, state: CursorState) -> Frame:
        if frame.pixel_format is not PixelFormat.BGRA:
            raise ValueError("指针合成当前只支持 BGRA")

        x = int(state.x * self.scale_x) - state.hotspot_x
        y = int(state.y * self.scale_y) - state.hotspot_y
        if x >= frame.width or y >= frame.height or x + state.width <= 0 or y + state.height <= 0:
            return frame  # 完全在画面外，省一次拷贝

        canvas = bytearray(bytes(frame.data))
        cursor_pixels = state.image
        cursor_stride = state.width * 4
        target_stride = frame.width * 4

        for row in range(state.height):
            target_y = y + row
            if target_y < 0 or target_y >= frame.height:
                continue
            for column in range(state.width):
                target_x = x + column
                if target_x < 0 or target_x >= frame.width:
                    continue

                source_index = row * cursor_stride + column * 4
                blue = cursor_pixels[source_index]
                green = cursor_pixels[source_index + 1]
                red = cursor_pixels[source_index + 2]
                alpha = cursor_pixels[source_index + 3]
                if alpha == 0:
                    continue

                target_index = target_y * target_stride + target_x * 4
                if alpha == 255:
                    canvas[target_index] = blue
                    canvas[target_index + 1] = green
                    canvas[target_index + 2] = red
                    canvas[target_index + 3] = 255
                    continue

                inverse = 255 - alpha
                canvas[target_index] = (blue * alpha + canvas[target_index] * inverse) // 255
                canvas[target_index + 1] = (
                    green * alpha + canvas[target_index + 1] * inverse
                ) // 255
                canvas[target_index + 2] = (
                    red * alpha + canvas[target_index + 2] * inverse
                ) // 255
                canvas[target_index + 3] = 255

        return frame.with_data(bytes(canvas))

    def close(self) -> None:
        self._last_state = None
