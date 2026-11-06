"""码率分配。

原则：**音频优先**。音频码率低但对连续性极敏感，所以先按固定值预留，
剩下的才给视频；视频拿到的是「估计可用带宽 - 音频 - 冗余开销」。

分配结果还要过三道夹子：

1. 下限：低于 ``min_video_kbps`` 时画面已经没有意义，宁可提示用户降分辨率；
2. 上限：不超过用户配置的目标码率（别把带宽吃满）；
3. 步进：单次调整不超过 ``max_step_ratio``，避免码率锯齿导致画面忽清忽糊。
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.logging.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class Allocation:
    """一次分配结果（kbps）。"""

    video_kbps: int
    audio_kbps: int
    overhead_kbps: int

    @property
    def total_kbps(self) -> int:
        return self.video_kbps + self.audio_kbps + self.overhead_kbps

    def describe(self) -> str:
        return (
            f"video={self.video_kbps}k audio={self.audio_kbps}k "
            f"overhead={self.overhead_kbps}k total={self.total_kbps}k"
        )


class BitrateAllocator:
    """把可用带宽切成「音频 + 视频 + 冗余」。"""

    def __init__(
        self,
        audio_kbps: int = 64,
        min_video_kbps: int = 200,
        max_video_kbps: int = 8000,
        overhead_ratio: float = 0.08,
        max_step_ratio: float = 0.5,
    ) -> None:
        self.audio_kbps = audio_kbps
        self.min_video_kbps = min_video_kbps
        self.max_video_kbps = max_video_kbps
        self.overhead_ratio = overhead_ratio
        self.max_step_ratio = max_step_ratio
        self._current_video_kbps = min_video_kbps

    @property
    def current_video_kbps(self) -> int:
        return self._current_video_kbps

    def allocate(self, available_kbps: float, *, audio_enabled: bool = True) -> Allocation:
        audio = self.audio_kbps if audio_enabled else 0
        overhead = max(0, int(available_kbps * self.overhead_ratio))
        budget = available_kbps - audio - overhead

        if budget < self.min_video_kbps:
            video = self.min_video_kbps
            logger.debug(
                "可用带宽 %.0fk 不足，视频按下限 %dk 分配", available_kbps, video
            )
        else:
            video = int(min(self.max_video_kbps, budget))

        # 平滑：单次变化不超过 max_step_ratio
        lower = int(self._current_video_kbps * (1 - self.max_step_ratio))
        upper = int(self._current_video_kbps * (1 + self.max_step_ratio))
        video = max(self.min_video_kbps, min(self.max_video_kbps, max(lower, min(upper, video))))
        self._current_video_kbps = video

        return Allocation(video_kbps=video, audio_kbps=audio, overhead_kbps=overhead)

    def reset(self) -> None:
        self._current_video_kbps = self.min_video_kbps
