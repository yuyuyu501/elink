"""指针叠加：把光标图层合成到画面帧。

合成用「逐像素 alpha 混合」而不是覆盖，因为现代光标带抗锯齿边缘；
不留 alpha 混合会让光标边缘出现黑边。

性能：只遍历光标矩形范围内与其外接的行，不扫描整帧。
"""

from __future__ import annotations

import numpy as np

from backend.logging.logger import get_logger
from backend.media.frame import Frame, PixelFormat
from backend.media.video.cursor.capture import CursorState

logger = get_logger(__name__)


class CursorOverlay:
    name = "cursor_overlay"

    def __init__(self, scale_x: float = 1.0, scale_y: float = 1.0,
                 origin: tuple[int, int] = (0, 0)) -> None:
        # 采集区域与屏幕原点不同、或画面被缩放过时，坐标需要换算
        self.scale_x = scale_x
        self.scale_y = scale_y
        self.origin = origin
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

        x = round((state.x - self.origin[0] - state.hotspot_x) * self.scale_x)
        y = round((state.y - self.origin[1] - state.hotspot_y) * self.scale_y)
        width, height = max(1, round(state.width * self.scale_x)), max(1, round(state.height * self.scale_y))
        if x >= frame.width or y >= frame.height or x + width <= 0 or y + height <= 0:
            return frame  # 完全在画面外，省一次拷贝

        left, top, right, bottom = max(0, x), max(0, y), min(frame.width, x + width), min(frame.height, y + height)
        rows = np.minimum(state.height - 1, np.arange(top - y, bottom - y) / self.scale_y).astype(int)
        cols = np.minimum(state.width - 1, np.arange(left - x, right - x) / self.scale_x).astype(int)
        source = np.frombuffer(state.image, np.uint8).reshape(state.height, state.width, 4)[rows[:, None], cols].astype(np.int32)
        canvas = np.frombuffer(bytes(frame.data), np.uint8).reshape(frame.height, frame.linesize).copy()
        pixels = canvas[:, :frame.width * 4].reshape(frame.height, frame.width, 4)
        patch = pixels[top:bottom, left:right]
        if state.background:
            white = np.frombuffer(state.background, np.uint8).reshape(state.height, state.width, 4)[rows[:, None], cols].astype(np.int32)
            patch[:, :, :3] = np.clip(source[:, :, :3] + patch[:, :, :3] * (white[:, :, :3] - source[:, :, :3]) // 255, 0, 255)
        else:
            alpha = source[:, :, 3:4]
            patch[:, :, :3] = (source[:, :, :3] * alpha + patch[:, :, :3] * (255 - alpha)) // 255
        patch[:, :, 3] = 255
        return frame.with_data(canvas.tobytes())

    def close(self) -> None:
        self._last_state = None
