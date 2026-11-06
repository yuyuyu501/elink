"""帧率调整（丢帧 / 补帧）。

远端屏幕静止时采集端仍会以固定频率出帧，全部编码会浪费码率；这里按目标帧率
决定这一帧是「送」还是「丢」，并保证时间戳单调递增。

补帧只在目标帧率高于源帧率时才会发生，且必须是「上一个已发出的帧」——
用未来帧补会破坏时间顺序。
"""

from __future__ import annotations

from backend.media.frame import Frame
from backend.media.timebase import Timebase
from backend.media.video.filter.pixel_format import BaseVideoFilter


class FrameRateAdapter(BaseVideoFilter):
    name = "framerate"

    def __init__(self, target_fps: float, *, allow_duplicate: bool = False) -> None:
        if target_fps <= 0:
            raise ValueError("目标帧率必须大于 0")
        self.target_fps = target_fps
        self.allow_duplicate = allow_duplicate
        self._next_pts: int | None = None
        self._last_frame: Frame | None = None
        self._dropped = 0
        self._duplicated = 0

    @property
    def stats(self) -> dict[str, int]:
        return {"dropped": self._dropped, "duplicated": self._duplicated}

    def process(self, frame: Frame) -> Frame:
        timebase: Timebase = frame.timebase
        interval = timebase.ticks(1.0 / self.target_fps)

        if self._next_pts is None:
            self._next_pts = frame.pts
        if frame.pts + 1 < self._next_pts:
            # 源时间戳倒退（重连/回绕）：重置栅格而不是丢帧
            self._next_pts = frame.pts
        if frame.pts < self._next_pts:
            self._dropped += 1
            return self._last_frame or frame

        self._next_pts = frame.pts + interval
        self._last_frame = frame
        return frame

    def duplicate_if_needed(self, frame: Frame) -> list[Frame]:
        """需要补帧时返回 ``[上一帧, 当前帧]``，否则只返回当前帧。"""
        if not self.allow_duplicate or self._last_frame is None:
            return [frame]
        interval = frame.timebase.ticks(1.0 / self.target_fps)
        gap = frame.pts - self._last_frame.pts
        if gap <= interval * 1.5:
            return [frame]
        copies = min(4, int(gap / interval) - 1)
        output = []
        for index in range(copies):
            self._duplicated += 1
            output.append(
                self._last_frame.with_data(
                    bytes(self._last_frame.data),
                    pts=self._last_frame.pts + interval * (index + 1),
                )
            )
        output.append(frame)
        return output

    def close(self) -> None:
        self._last_frame = None
