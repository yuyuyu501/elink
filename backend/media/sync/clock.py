"""参考时钟。

同步的正确顺序是「先有一个主时钟，再让各流向它看齐」：

- :class:`SystemClock` —— 单调墙钟，永远可用，作为兜底；
- :class:`StreamClock` —— 由某一流的时间戳驱动（音频通常做主机），
  内部对媒体时间做平滑，避免抖动传导给视频；
- :class:`ClockSynchronizer` —— 用往返时延估计对端时钟偏移（SR/RR 里的
  NTP 时间戳就是干这个的），供远端时间戳换算到本地时间轴。

所有时间都以「秒（float）」对外，内部按 ``time.monotonic()`` 计算，避免
系统时间被 NTP 调整时时间轴跳变。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


class Clock:
    """时钟接口：``now()`` 返回单调秒。"""

    def now(self) -> float:  # pragma: no cover - 抽象
        raise NotImplementedError

    def reset(self) -> None:
        pass


class SystemClock(Clock):
    """系统单调时钟，不随系统时间调整而跳变。"""

    def now(self) -> float:
        return time.monotonic()


@dataclass
class ClockSample:
    media_time: float
    local_time: float


class StreamClock(Clock):
    """由媒体时间戳推导的时钟。

    做法：收到带时间戳的帧时记录 ``(media_time, local_time)``，之后用
    ``local + (media - media_base) * rate`` 推算当前媒体时间。``rate`` 允许
    微调（常见 1.0 ± 0.01）以慢慢吃掉累积漂移，而不是突变。
    """

    def __init__(self, rate: float = 1.0, smoothing: float = 0.1) -> None:
        self.rate = rate
        self.smoothing = smoothing
        self._sample: ClockSample | None = None
        self._lock = threading.Lock()

    def update(self, media_time: float, local_time: float | None = None) -> None:
        local = time.monotonic() if local_time is None else local_time
        with self._lock:
            if self._sample is None:
                self._sample = ClockSample(media_time, local)
                return
            predicted = self._sample.media_time + (local - self._sample.local_time) * self.rate
            error = media_time - predicted
            # 用一阶平滑吸收误差，避免网络抖动直接变成时钟跳动
            self._sample = ClockSample(
                predicted + error * self.smoothing,
                local,
            )

    def now(self) -> float:
        with self._lock:
            if self._sample is None:
                return 0.0
            elapsed = (time.monotonic() - self._sample.local_time) * self.rate
            return self._sample.media_time + elapsed

    @property
    def started(self) -> bool:
        return self._sample is not None

    def reset(self) -> None:
        with self._lock:
            self._sample = None


class ClockSynchronizer:
    """估计「对端时钟 - 本地时钟」的偏移。

    输入是 RTCP SR 里的 (NTP 时间戳, RTP 时间戳) 与本地收到时刻，
    输出是偏移量；多次采样取最小 RTT 的那次（RTT 越小，偏移估计越准）。
    """

    def __init__(self, max_samples: int = 32) -> None:
        self.max_samples = max_samples
        self._samples: list[tuple[float, float]] = []  # (rtt, offset)
        self._lock = threading.Lock()

    def observe(
        self,
        remote_ntp: float,
        local_receive: float,
        round_trip: float = 0.0,
    ) -> float:
        offset = remote_ntp - local_receive + round_trip / 2
        with self._lock:
            self._samples.append((round_trip, offset))
            if len(self._samples) > self.max_samples:
                self._samples.pop(0)
        return offset

    @property
    def offset(self) -> float:
        with self._lock:
            if not self._samples:
                return 0.0
            return min(self._samples, key=lambda item: item[0])[1]

    def to_local(self, remote_time: float) -> float:
        """把对端时间戳换算到本地时间轴。"""
        return remote_time - self.offset
