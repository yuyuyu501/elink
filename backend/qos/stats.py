"""质量统计与质量分级。

把「丢包 / 抖动 / 时延 / 码率」合成一个可读的质量等级，供 UI 显示与
告警阈值判断。分级阈值参考 ITU-T G.114 与常见实时通信经验值：

- 良好：丢包 <1%、RTT <150ms、抖动 <30ms；
- 一般：丢包 <3%、RTT <300ms、抖动 <60ms；
- 较差：其余（应触发降码率或降分辨率）。
"""

from __future__ import annotations

import enum
import threading
import time
from dataclasses import dataclass, field

from backend.metrics.stats import LatencyTracker, LossTracker, SlidingWindow


class QualityLevel(str, enum.Enum):
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"
    UNKNOWN = "unknown"


@dataclass
class QualitySample:
    """一次采样（来自 RTCP 或本地统计）。"""

    loss_rate: float = 0.0
    rtt_ms: float = 0.0
    jitter_ms: float = 0.0
    bitrate_kbps: float = 0.0
    audio_enabled: bool = True
    timestamp: float = field(default_factory=time.monotonic)


class QualityStats:
    """滑动窗口统计 + 质量分级。"""

    def __init__(self, window_s: float = 5.0) -> None:
        self.loss = LossTracker()
        self.latency = LatencyTracker(window_s)
        self.jitter = SlidingWindow(window_s)
        self.bitrate = SlidingWindow(window_s)
        self._lock = threading.Lock()

    # ---- 输入 ----
    def observe_packet(self, sequence: int, rtt_ms: float = 0.0) -> None:
        self.loss.observe(sequence)
        if rtt_ms:
            self.latency.observe(rtt_ms)

    def observe_jitter(self, jitter_ms: float) -> None:
        self.jitter.add(jitter_ms)

    def observe_bitrate(self, kbps: float) -> None:
        self.bitrate.add(kbps)

    # ---- 输出 ----
    def sample(self, *, audio_enabled: bool = True) -> QualitySample:
        return QualitySample(
            loss_rate=self.loss.loss_rate,
            rtt_ms=self.latency.mean_ms,
            jitter_ms=self.jitter.mean(),
            bitrate_kbps=self.bitrate.mean(),
            audio_enabled=audio_enabled,
        )

    def level(self) -> QualityLevel:
        sample = self.sample()
        if sample.loss_rate > 0.03 or sample.rtt_ms > 300 or sample.jitter_ms > 60:
            return QualityLevel.POOR
        if sample.loss_rate > 0.01 or sample.rtt_ms > 150 or sample.jitter_ms > 30:
            return QualityLevel.FAIR
        return QualityLevel.GOOD

    def snapshot(self) -> dict[str, float]:
        sample = self.sample()
        return {
            "loss_rate": round(sample.loss_rate, 4),
            "rtt_ms": round(sample.rtt_ms, 1),
            "jitter_ms": round(sample.jitter_ms, 1),
            "bitrate_kbps": round(sample.bitrate_kbps, 1),
            "quality": self.level().value,
        }

    def describe(self) -> str:
        data = self.snapshot()
        return (
            f"{data['quality']} loss={data['loss_rate'] * 100:.2f}% "
            f"rtt={data['rtt_ms']}ms jitter={data['jitter_ms']}ms "
            f"{data['bitrate_kbps']}kbps"
        )
