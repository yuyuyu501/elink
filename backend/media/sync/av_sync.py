"""音视频同步。

以主时钟为基准（默认音频，因为人耳对音频抖动更敏感）：

- 帧的媒体时间早于主时钟超过 ``drop_threshold`` → 直接丢（追不上就不追了）；
- 早但没超过阈值 → 不等，立刻送（轻微提前人眼察觉不到）；
- 晚于主时钟 → 等到该显示的时刻，最多等 ``max_wait``，超时也送。

视频丢帧策略独立于 QoS：这里只处理「时间上来不及」的帧，
网络丢包导致的数据缺失由 ``qos`` 负责。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from backend.logging.logger import get_logger
from backend.media.frame import Frame
from backend.media.sync.clock import Clock, SystemClock

logger = get_logger(__name__)


@dataclass
class SyncStats:
    presented: int = 0
    dropped_late: int = 0
    waited_ms_total: float = 0.0

    def summary(self) -> str:
        average_wait = self.waited_ms_total / self.presented if self.presented else 0.0
        return (
            f"送显={self.presented} 迟到丢弃={self.dropped_late} "
            f"平均等待={average_wait:.1f}ms"
        )


class AVSynchronizer:
    """按主时钟决定「立刻送 / 等一会 / 丢弃」。"""

    def __init__(
        self,
        clock: Clock | None = None,
        *,
        drop_threshold_ms: float = 200.0,
        max_wait_ms: float = 100.0,
    ) -> None:
        self.clock = clock or SystemClock()
        self.drop_threshold_ms = drop_threshold_ms
        self.max_wait_ms = max_wait_ms
        self.stats = SyncStats()

    def wait_until(self, frame: Frame) -> bool:
        """返回 True 表示这一帧应该送显，False 表示已丢弃。"""
        target = frame.seconds
        now = self.clock.now()
        diff_ms = (target - now) * 1000.0

        if diff_ms < -self.drop_threshold_ms:
            self.stats.dropped_late += 1
            logger.debug("丢帧: 迟到 %.1fms", -diff_ms)
            return False

        if diff_ms <= 0:
            self.stats.presented += 1
            return True

        wait_s = min(diff_ms, self.max_wait_ms) / 1000.0
        if wait_s > 0:
            begin = time.perf_counter()
            # 用短睡眠循环，避免长 sleep 被系统调度拖过头
            while True:
                remaining = wait_s - (time.perf_counter() - begin)
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 0.01))
            self.stats.waited_ms_total += (time.perf_counter() - begin) * 1000

        self.stats.presented += 1
        return True
